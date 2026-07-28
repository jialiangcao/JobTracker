"""Workable widget payload → JobPosting."""

from jobtrack.adapters.base import as_str, parse_dt, strip_html
from jobtrack.schema import JobPosting, RawPosting


class WorkableAdapter:
    def map(self, raw: RawPosting) -> JobPosting | None:
        p = raw.payload
        title = as_str(p.get("title"))
        url = as_str(p.get("url")) or as_str(p.get("application_url"))
        if title is None or url is None:
            return None
        location = (
            ", ".join(
                x
                for x in (as_str(p.get("city")), as_str(p.get("state")), as_str(p.get("country")))
                if x
            )
            or None
        )
        telecommuting = p.get("telecommuting")
        description = as_str(p.get("description"))
        return JobPosting(
            external_id=as_str(p.get("shortcode")) or as_str(p.get("code")),
            title=title,
            company=raw.source_name,
            url=url,
            location=location,
            remote=telecommuting if isinstance(telecommuting, bool) else None,
            description=strip_html(description) if description else None,
            posted_at=parse_dt(p.get("published_on")) or parse_dt(p.get("created_at")),
            department=as_str(p.get("department")),
            employment_type=as_str(p.get("employment_type")),
            source_kind=raw.source_kind,
            raw=p,
        )
