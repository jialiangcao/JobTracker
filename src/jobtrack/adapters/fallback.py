"""Generic heuristic adapter: maps unknown payload shapes by probing common key names.

Used for unregistered source kinds and as a safety net via config.adapter_override.
Emits a JobPosting as long as it can find a title and a URL; returns None otherwise.
"""

from typing import Any

from jobtrack.adapters.base import as_str, dig, parse_dt, strip_html
from jobtrack.schema import JobPosting, RawPosting

_TITLE_KEYS = ("title", "text", "name", "position", "jobTitle", "job_title")
_URL_KEYS = (
    "url",
    "absolute_url",
    "absoluteUrl",
    "hostedUrl",
    "jobUrl",
    "job_url",
    "applyUrl",
    "apply_url",
    "application_url",
    "link",
    "href",
)
_ID_KEYS = ("id", "jobId", "job_id", "uuid", "shortcode", "slug", "refNumber")
_LOCATION_KEYS = ("location", "locations", "offices", "city")
_DESCRIPTION_KEYS = (
    "descriptionPlain",
    "description",
    "descriptionHtml",
    "content",
    "body",
)
_POSTED_KEYS = (
    "first_published",
    "publishedAt",
    "published_on",
    "releasedDate",
    "createdAt",
    "created_at",
    "postedAt",
    "posted_at",
    "updated_at",
    "updatedAt",
)


def _first(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _location_text(value: Any) -> str | None:
    """Handles 'NYC', {'name': ...}, ['NYC', ...], and [{'name'|'location': ...}, ...]."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = (
            value.get("name") or value.get("label") or value.get("location") or value.get("city")
        )
    if isinstance(value, dict):  # e.g. secondaryLocations-style nesting
        value = value.get("name") or value.get("location")
    return as_str(value)


class FallbackAdapter:
    def map(self, raw: RawPosting) -> JobPosting | None:
        p = raw.payload
        title = as_str(_first(p, _TITLE_KEYS))
        url = as_str(_first(p, _URL_KEYS))
        if title is None or url is None:
            return None
        external_id = _first(p, _ID_KEYS)
        description = _first(p, _DESCRIPTION_KEYS)
        return JobPosting(
            external_id=str(external_id) if external_id is not None else None,
            title=title,
            company=as_str(p.get("company")) or as_str(p.get("company_name")) or raw.source_name,
            url=url,
            location=_location_text(_first(p, _LOCATION_KEYS))
            or as_str(dig(p, "categories", "location")),  # Lever-style nesting
            description=strip_html(description) if isinstance(description, str) else None,
            posted_at=parse_dt(_first(p, _POSTED_KEYS)),
            source_kind=raw.source_kind,
            raw=p,
        )
