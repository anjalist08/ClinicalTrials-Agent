"""Headless-browser test of the demo UI renderer against the fake API.

Starts the real FastAPI app (fixture-backed engine) with uvicorn in a thread,
drives it with Playwright/Chromium, and checks that every visualization type
renders without JavaScript errors and that clicking a datum shows citations.
Skipped if Playwright/Chromium is unavailable.
"""

import socket
import threading
import time

import httpx
import pytest

pw = pytest.importorskip("playwright.sync_api")

from conftest import BASE, FakeCTGov  # noqa: E402

from app.config import Settings  # noqa: E402
from app.ctgov.client import CTGovClient  # noqa: E402
from app.main import create_app  # noqa: E402
from app.pipeline import Engine  # noqa: E402
from app.planner import Planner  # noqa: E402


@pytest.fixture(scope="module")
def server():
    import uvicorn

    settings = Settings(planner="rules", ctgov_base_url=BASE)
    engine = Engine(settings, CTGovClient(BASE, transport=httpx.MockTransport(FakeCTGov().handler)), Planner(settings))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(create_app(settings, engine), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # pragma: no cover - environment specific
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


QUERIES = [
    ("How are pembrolizumab trials distributed across phases?", "svg .mark-rect path, svg .mark-rect"),
    ("How many trials started each year for melanoma since 2014?", "svg .mark-line"),
    ("Compare phases for trials involving nivolumab vs pembrolizumab", "svg .mark-rect"),
    ("Show a network of sponsors and drugs", ".network canvas"),
    ("Distribution of enrollment sizes", "svg .mark-rect"),
    ("Scatter of enrollment vs duration", "svg .mark-symbol"),
    ("Show a world map of trial countries", "svg .mark-shape"),
    ("How many recruiting trials are there?", ".kpi"),
    ("Show the largest trials", "table.data tbody tr"),
    ("Breakdown of trials by phase and status", "svg .mark-rect"),
]


@pytest.mark.parametrize("query,selector", QUERIES)
def test_ui_renders_each_type(server, browser, query, selector):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(server)
    page.fill("#query", query)
    page.click("#run")
    page.wait_for_selector("#result:not([hidden])", timeout=10000)
    page.wait_for_selector(selector, timeout=10000)
    assert page.inner_text("#vis-title")
    assert not errors, errors
    page.close()


def test_clicking_a_row_shows_citations(server, browser):
    page = browser.new_page()
    page.goto(server)
    page.fill("#query", "Show the largest trials")
    page.click("#run")
    page.wait_for_selector("table.data tbody tr")
    page.click("table.data tbody tr >> nth=0")
    page.wait_for_selector("#citations .cite")
    text = page.inner_text("#citations")
    assert "NCT90000006" in text and "enrollmentInfo.count" in text
    page.close()
