"""Runtime settings, read from environment variables with sensible defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import __version__

DEFAULT_MODEL = "claude-opus-5-5"


def _default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "discovery-agent"


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, default))


@dataclass(frozen=True)
class Settings:
    """All tunables in one place. Use :meth:`from_env` to build from the environment."""

    cache_dir: Path = field(default_factory=_default_cache_dir)
    cache_ttl_hours: float = 24.0
    http_timeout: float = 45.0
    http_retries: int = 4
    max_workers: int = 6
    ncbi_api_key: str | None = None
    contact_email: str | None = None
    model: str = DEFAULT_MODEL
    effort: str = "high"
    language: str = "lt"
    user_agent: str = (
        f"scientific-discovery-agent/{__version__} "
        "(+https://github.com/makistosas/scientific-discovery)"
    )

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            cache_dir=Path(os.environ.get("DISCOVERY_CACHE_DIR") or _default_cache_dir()),
            cache_ttl_hours=_env_float("DISCOVERY_CACHE_TTL_HOURS", 24.0),
            http_timeout=_env_float("DISCOVERY_HTTP_TIMEOUT", 45.0),
            http_retries=_env_int("DISCOVERY_HTTP_RETRIES", 4),
            max_workers=_env_int("DISCOVERY_MAX_WORKERS", 6),
            ncbi_api_key=os.environ.get("NCBI_API_KEY") or None,
            contact_email=os.environ.get("DISCOVERY_CONTACT_EMAIL") or None,
            model=os.environ.get("DISCOVERY_MODEL") or DEFAULT_MODEL,
            effort=os.environ.get("DISCOVERY_EFFORT") or "high",
            language=os.environ.get("DISCOVERY_LANGUAGE") or "lt",
        )

    def with_overrides(self, **kwargs) -> "Settings":
        clean = {k: v for k, v in kwargs.items() if v is not None}
        return replace(self, **clean)
