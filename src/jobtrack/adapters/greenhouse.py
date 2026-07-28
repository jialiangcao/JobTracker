"""Greenhouse job payload → JobPosting."""

from jobtrack.adapters.base import as_str, dig, parse_dt, strip_html
from jobtrack.schema import JobPosting, RawPosting


class GreenhouseAdapter:
    def map(self, raw: RawPosting) -> JobPosting | None:
        p = raw.payload
        title = as_str(p.get("title"))
        url = as_str(p.get("absolute_url"))
        if title is None or url is None:
            return None
        content = p.get("content")
        departments = p.get("departments")
        department = None
        if isinstance(departments, list) and departments:
            department = (
                as_str(dig(departments[0], "name")) if isinstance(departments[0], dict) else None
            )
        return JobPosting(
            external_id=str(p["id"]) if p.get("id") is not None else None,
            title=title,
            company=as_str(dig(p, "company_name")) or raw.source_name,
            url=url,
            location=as_str(dig(p, "location", "name")),
            description=strip_html(content) if isinstance(content, str) else None,
            posted_at=parse_dt(p.get("first_published")) or parse_dt(p.get("updated_at")),
            updated_at=parse_dt(p.get("updated_at")),
            department=department,
            source_kind=raw.source_kind,
            raw=p,
        )
