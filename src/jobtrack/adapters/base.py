"""Adapter protocol plus small parsing helpers shared by concrete adapters."""

import html as html_module
from datetime import UTC, datetime
from typing import Any, Protocol

from selectolax.parser import HTMLParser

from jobtrack.schema import JobPosting, RawPosting


class Adapter(Protocol):
    def map(self, raw: RawPosting) -> JobPosting | None: ...


def strip_html(value: str) -> str:
    """HTML → plain text (also unescapes entity-encoded HTML, e.g. Greenhouse content)."""
    unescaped = html_module.unescape(value)
    if "<" not in unescaped:
        return unescaped.strip()
    text = HTMLParser(unescaped).text(separator="\n")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def parse_dt(value: Any) -> datetime | None:
    """Best-effort datetime parsing: ISO-8601 strings or epoch seconds/milliseconds."""
    if value is None:
        return None
    if isinstance(value, int | float):
        seconds = float(value) / 1000.0 if value > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def dig(payload: dict[str, Any], *path: str) -> Any:
    """payload["a"]["b"]... with None on any miss/type mismatch."""
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def as_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
