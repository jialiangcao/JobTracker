"""Gem GraphQL job posting → JobPosting."""

from typing import Any

from jobtrack.adapters.base import as_str, dig, parse_dt, strip_html
from jobtrack.schema import JobPosting, RawPosting


def _epoch_seconds(value: Any) -> int | None:
    """firstPublishedTsSec is epoch seconds, but the type varies by board — an int on some,
    a decimal *string* on others. parse_dt reads strings as ISO-8601 and would return None
    for the string form, so digits are coerced to int first."""
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return value if isinstance(value, int) else None


def _locations(p: dict[str, Any]) -> list[dict[str, Any]]:
    raw = p.get("locations")
    return [loc for loc in raw if isinstance(loc, dict)] if isinstance(raw, list) else []


def _location(locations: list[dict[str, Any]]) -> str | None:
    """Join every listed location. Gem splits city/country into separate fields and a
    posting can carry several, so the eligibility pass gets all of them rather than the
    first — a US-only filter would otherwise drop "London; New York" on its London half."""
    names: list[str] = []
    for loc in locations:
        if name := as_str(loc.get("name")) or as_str(loc.get("city")):
            country = as_str(loc.get("isoCountry"))
            names.append(f"{name}, {country}" if country and country not in name else name)
    # dict.fromkeys dedups while preserving order — boards repeat a city per office.
    return "; ".join(dict.fromkeys(names)) or None


class GemAdapter:
    def map(self, raw: RawPosting) -> JobPosting | None:
        p = raw.payload
        title = as_str(p.get("title"))
        ext_id = as_str(p.get("extId"))
        slug = as_str(p.get("_board_slug"))
        # No apply URL in the payload: the public job page is /<board slug>/<extId>, and
        # the slug is injected by the fetcher. Without both there is no addressable job.
        if title is None or ext_id is None or slug is None:
            return None

        locations = _locations(p)
        location_type = as_str(dig(p, "job", "locationType"))
        remote = any(loc.get("isRemote") is True for loc in locations) or location_type == "REMOTE"
        description = as_str(p.get("descriptionHtml"))

        return JobPosting(
            external_id=ext_id,
            title=title,
            company=raw.source_name,
            url=f"https://jobs.gem.com/{slug}/{ext_id}",
            location=_location(locations),
            remote=remote,
            description=strip_html(description) if description else None,
            posted_at=parse_dt(_epoch_seconds(p.get("firstPublishedTsSec"))),
            department=as_str(dig(p, "job", "department", "name")),
            employment_type=as_str(dig(p, "job", "employmentType")),
            source_kind=raw.source_kind,
            raw=p,
        )
