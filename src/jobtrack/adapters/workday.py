"""Workday CxS search result → JobPosting.

The search endpoint returns a deliberately thin row — no description, and a *relative*
posting date ("Posted Today", "Posted 30+ Days Ago"). Both full text and an exact
startDate would cost one extra GET per posting; nothing downstream reads them today
(every seed rule matches on title, and the Discord embed never shows a description).
"""

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from jobtrack.adapters.base import as_str
from jobtrack.schema import JobPosting, RawPosting

# "Posted Today" | "Posted Yesterday" | "Posted 5 Days Ago" | "Posted 30+ Days Ago"
_RELATIVE_DAYS = re.compile(r"(\d+)\s*(\+?)\s*days?\s+ago", re.IGNORECASE)
# externalPath tail: .../Software-Engineer-Intern_JR2013673
_PATH_REQ_ID = re.compile(r"_([A-Za-z0-9-]+)$")


def parse_posted_on(value: Any, now: datetime | None = None) -> datetime | None:
    """Relative English → an approximate timestamp. None when unrecognized, which
    `is_recent` treats as keep — better a stale posting than a silently dropped one."""
    text = as_str(value)
    if text is None:
        return None
    now = now or datetime.now(UTC)
    lowered = text.lower()
    if "today" in lowered:
        return now
    if "yesterday" in lowered:
        return now - timedelta(days=1)
    match = _RELATIVE_DAYS.search(lowered)
    if match is None:
        return None
    days = int(match.group(1))
    # "30+ Days Ago" is Workday's oldest bucket; push it just past the boundary so the
    # default 30-day window rejects it rather than admitting it on the boundary.
    return now - timedelta(days=days + 1 if match.group(2) else days)


def _external_id(payload: dict[str, Any], external_path: str) -> str | None:
    bullets = payload.get("bulletFields")
    if isinstance(bullets, list):
        for bullet in bullets:
            if (value := as_str(bullet)) is not None:
                return value
    match = _PATH_REQ_ID.search(external_path)
    return match.group(1) if match else None


class WorkdayAdapter:
    def map(self, raw: RawPosting) -> JobPosting | None:
        p = raw.payload
        title = as_str(p.get("title"))
        external_path = as_str(p.get("externalPath"))
        base_url = as_str(p.get("_base_url"))  # injected by the fetcher
        if title is None or external_path is None or base_url is None:
            return None
        return JobPosting(
            external_id=_external_id(p, external_path),
            title=title,
            company=raw.source_name,
            url=f"{base_url.rstrip('/')}{external_path}",
            # Kept verbatim, including Workday's "3 Locations" placeholder: it carries no
            # geography either way, and is_us_location fails open on both.
            location=as_str(p.get("locationsText")),
            posted_at=parse_posted_on(p.get("postedOn")),
            source_kind=raw.source_kind,
            raw=p,
        )
