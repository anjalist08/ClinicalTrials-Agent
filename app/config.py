"""Runtime configuration, read from environment variables (and an optional ``.env`` file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines); never overrides real environment variables."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"
    planner: str = "auto"  # auto | llm | rules
    ctgov_base_url: str = "https://clinicaltrials.gov/api/v2"
    ctgov_timeout_s: float = 30.0
    ctgov_max_retries: int = 3
    default_max_records: int = 1000
    cache_dir: Path | None = None
    cache_ttl_s: int = 6 * 3600
    llm_timeout_s: float = 30.0
    allowed_models: tuple[str, ...] = field(
        default=(
            "gpt-4o-mini",
            "gpt-4o-2024-08-06",
            "gpt-4.1-nano",
            "gpt-4.1-mini",
            "gpt-4.1",
            "gpt-5-mini",
            "gpt-5",
            "gpt-5.1",
            "gpt-5.2",
            "gpt-5.4-nano",
            "gpt-5.4-mini",
            "gpt-5.4",
        )
    )

    @classmethod
    def from_env(cls) -> Settings:
        _load_dotenv(PROJECT_ROOT / ".env")
        cache_dir = os.getenv("CTGOV_CACHE_DIR")
        return cls(
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            openai_model=os.getenv("OPENAI_MODEL", cls.openai_model),
            planner=os.getenv("PLANNER", cls.planner),
            ctgov_base_url=os.getenv("CTGOV_BASE_URL", cls.ctgov_base_url).rstrip("/"),
            ctgov_timeout_s=float(os.getenv("CTGOV_TIMEOUT_S", cls.ctgov_timeout_s)),
            default_max_records=int(os.getenv("MAX_RECORDS", cls.default_max_records)),
            cache_dir=Path(cache_dir) if cache_dir else None,
            cache_ttl_s=int(os.getenv("CTGOV_CACHE_TTL_S", cls.cache_ttl_s)),
        )
