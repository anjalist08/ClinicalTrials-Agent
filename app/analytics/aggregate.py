"""Deterministic aggregations over raw study records.

Every aggregate is a :class:`Bucket`: the set of trials that contributed to a
datum, plus the per-trial evidence that put them there. Counting rule: a trial
is counted at most once per bucket, even if it matches through several values
(e.g. three sites in the same country).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

from ..ctgov import extract as ex


@dataclass
class Bucket:
    key: str
    labels: Counter = field(default_factory=Counter)
    evidence: dict[str, list[ex.Ev]] = field(default_factory=dict)  # nct_id -> evidence (insertion ordered)

    def add(self, nct: str, label: str, evidence: Iterable[ex.Ev]) -> None:
        self.labels[label] += 1
        evs = self.evidence.setdefault(nct, [])
        for e in evidence:
            if e not in evs:
                evs.append(e)

    @property
    def label(self) -> str:
        # most common surface form; ties broken alphabetically for determinism
        return sorted(self.labels.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if self.labels else self.key

    @property
    def count(self) -> int:
        return len(self.evidence)

    @property
    def nct_ids(self) -> list[str]:
        return list(self.evidence)


def categorical(studies: list[dict[str, Any]], dim: str) -> dict[str, Bucket]:
    spec = ex.DIMENSIONS[dim]
    buckets: dict[str, Bucket] = {}
    for s in studies:
        nct = ex.nct_id(s)
        for v in spec.extract(s):
            buckets.setdefault(v.key, Bucket(v.key)).add(nct, v.label, v.evidence)
    return buckets


def order_keys(buckets: dict[str, Bucket], dim: str | None, top_n: int | None) -> list[str]:
    """Category order: ordinal dims in natural order, nominal dims by count (desc)."""
    spec = ex.DIMENSIONS.get(dim) if dim else None
    by_count = sorted(buckets, key=lambda k: (-buckets[k].count, buckets[k].label))
    chosen = by_count[:top_n] if top_n else by_count
    if spec and spec.ordinal:
        if spec.order:
            rank = {lbl.lower(): i for i, lbl in enumerate(spec.order)}
            return sorted(chosen, key=lambda k: (rank.get(buckets[k].label.lower(), 999), buckets[k].label))
        return sorted(chosen, key=lambda k: buckets[k].label)
    return chosen


def temporal(studies: list[dict[str, Any]], time_field: str, granularity: str) -> tuple[dict[str, Bucket], int]:
    buckets: dict[str, Bucket] = {}
    skipped = 0
    for s in studies:
        hit = ex.time_bucket(s, time_field, granularity)
        if hit is None:
            skipped += 1
            continue
        key, ev = hit
        buckets.setdefault(key, Bucket(key)).add(ex.nct_id(s), key, [ev])
    return buckets, skipped


def cross(studies: list[dict[str, Any]], dim_x: str, dim_y: str) -> dict[tuple[str, str], Bucket]:
    sx, sy = ex.DIMENSIONS[dim_x], ex.DIMENSIONS[dim_y]
    cells: dict[tuple[str, str], Bucket] = {}
    for s in studies:
        nct = ex.nct_id(s)
        for vx in sx.extract(s):
            for vy in sy.extract(s):
                cell = cells.setdefault((vx.key, vy.key), Bucket(f"{vx.key}|{vy.key}"))
                cell.add(nct, f"{vx.label}␟{vy.label}", vx.evidence + vy.evidence)
    return cells


@dataclass
class NumericPoint:
    nct_id: str
    value: float | str
    evidence: list[ex.Ev]


def numeric_values(studies: list[dict[str, Any]], name: str) -> tuple[list[NumericPoint], int]:
    points, missing = [], 0
    for s in studies:
        hit = ex.numeric(s, name)
        if hit is None:
            missing += 1
            continue
        points.append(NumericPoint(ex.nct_id(s), hit[0], hit[1]))
    return points, missing


# ------------------------------------------------------------------ histogram
def nice_step(raw: float, integer: bool) -> float:
    if raw <= 0:
        return 1.0
    exp = math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        step = m * 10**exp
        if step >= raw:
            break
    return max(1.0, math.ceil(step)) if integer else step


def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    pos = (len(sorted_vals) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


@dataclass
class HistBin:
    start: float
    end: float | None  # None = open-ended overflow bin
    bucket: Bucket


def bin_edges(values: list[float], integer: bool, target_bins: int | None = None) -> tuple[float, float, float | None]:
    """Return (start, step, overflow_threshold). Long right tails (common for enrollment)
    are folded into one overflow bin at the 95th percentile so the body stays readable."""
    vals = sorted(values)
    lo, hi = vals[0], vals[-1]
    p95 = _quantile(vals, 0.95)
    cap = p95 if (hi > 3 * p95 and p95 > lo) else hi
    n = target_bins or min(20, max(5, math.ceil(math.sqrt(len(vals)))))
    step = nice_step((cap - lo) / n if cap > lo else 1.0, integer)
    start = math.floor(lo / step) * step
    overflow = None
    if cap < hi:
        overflow = start + math.ceil((cap - start) / step) * step
        if overflow <= cap:
            overflow += step
    return start, step, overflow


def histogram(points_by_group: dict[str | None, list[NumericPoint]], integer: bool) -> tuple[list[HistBin], dict[str | None, list[HistBin]]]:
    """Shared bins across groups so grouped histograms are comparable."""
    all_vals = [float(p.value) for pts in points_by_group.values() for p in pts]
    if not all_vals:
        return [], {g: [] for g in points_by_group}
    start, step, overflow = bin_edges(all_vals, integer)
    if overflow is not None:
        n_regular = max(1, round((overflow - start) / step))
    else:
        n_regular = int((max(all_vals) - start) // step) + 1
    per_group: dict[str | None, list[HistBin]] = {}
    for g, pts in points_by_group.items():
        bins = [HistBin(start + i * step, start + (i + 1) * step, Bucket(str(i))) for i in range(n_regular)]
        if overflow is not None:
            bins.append(HistBin(overflow, None, Bucket("overflow")))
        for p in pts:
            v = float(p.value)
            if overflow is not None and v >= overflow:
                idx = len(bins) - 1
            else:
                idx = min(int((v - start) // step), n_regular - 1)
            bins[idx].bucket.add(p.nct_id, bins[idx].bucket.key, p.evidence)
        per_group[g] = bins
    template = next(iter(per_group.values()))
    return template, per_group


# ------------------------------------------------------------------ network
@dataclass
class Network:
    nodes: dict[str, Bucket]  # node id -> bucket
    node_type: dict[str, str]
    edges: dict[tuple[str, str], Bucket]
    total_nodes: int
    total_edges: int
    min_edge_weight: int


def network(
    studies: list[dict[str, Any]],
    source: str,
    target: str,
    top_n: int,
    max_edges: int = 150,
) -> Network:
    s_spec, t_spec = ex.DIMENSIONS[source], ex.DIMENSIONS[target]
    co_occurrence = source == target
    nodes: dict[str, Bucket] = {}
    node_type: dict[str, str] = {}
    edges: dict[tuple[str, str], Bucket] = {}

    def node(kind: str, v: ex.Val, nct: str) -> str:
        nid = f"{kind}:{v.key}"
        nodes.setdefault(nid, Bucket(nid)).add(nct, v.label, v.evidence)
        node_type[nid] = kind
        return nid

    for s in studies:
        nct = ex.nct_id(s)
        src_vals = s_spec.extract(s)
        src = [(node(source, v, nct), v) for v in src_vals if v.label != ex.MISSING_LABEL]
        if co_occurrence:
            pairs = combinations(sorted(src, key=lambda x: x[0]), 2)
        else:
            tgt = [(node(target, v, nct), v) for v in t_spec.extract(s) if v.label != ex.MISSING_LABEL]
            pairs = ((a, b) for a in src for b in tgt)
        for (a_id, a_v), (b_id, b_v) in pairs:
            if a_id == b_id:
                continue
            key = (a_id, b_id)
            edges.setdefault(key, Bucket(f"{a_id}--{b_id}")).add(nct, "", a_v.evidence + b_v.evidence)

    total_nodes, total_edges = len(nodes), len(edges)

    # Select the most connected entities: top_n overall for co-occurrence, half per side for bipartite.
    def top(kind: str | None, n: int) -> list[str]:
        ids = [i for i in nodes if kind is None or node_type[i] == kind]
        return sorted(ids, key=lambda i: (-nodes[i].count, nodes[i].label))[:n]

    keep = set(top(None, top_n)) if co_occurrence else set(top(source, math.ceil(top_n / 2))) | set(top(target, top_n // 2 or 1))
    kept_edges = {k: b for k, b in edges.items() if k[0] in keep and k[1] in keep}

    # Raise the edge-weight threshold until the graph is legible.
    min_w = 1
    while len([b for b in kept_edges.values() if b.count >= min_w]) > max_edges:
        min_w += 1
    kept_edges = {k: b for k, b in kept_edges.items() if b.count >= min_w}
    connected = {n for k in kept_edges for n in k}
    kept_nodes = {i: nodes[i] for i in keep if i in connected}
    return Network(kept_nodes, node_type, kept_edges, total_nodes, total_edges, min_w)
