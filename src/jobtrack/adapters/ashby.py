"""Ashby job-board payload → JobPosting."""

from jobtrack.adapters.base import as_str, dig, parse_dt, strip_html
from jobtrack.schema import JobPosting, RawPosting


class AshbyAdapter:
    def map(self, raw: RawPosting) -> JobPosting | None:
        p = raw.payload
        title = as_str(p.get("title"))
        url = as_str(p.get("jobUrl")) or as_str(p.get("applyUrl"))
        if title is None or url is None:
            return None
        description = as_str(p.get("descriptionPlain"))
        if description is None:
            html = as_str(p.get("descriptionHtml")) or as_str(p.get("description"))
            description = strip_html(html) if html else None
        remote = p.get("isRemote")
        return JobPosting(
            external_id=as_str(p.get("id")),
            title=title,
            company=raw.source_name,
            url=url,
            location=as_str(p.get("location")),
            remote=remote if isinstance(remote, bool) else None,
            description=description,
            posted_at=parse_dt(p.get("publishedAt")),
            department=as_str(p.get("department")) or as_str(p.get("team")),
            employment_type=as_str(p.get("employmentType")),
            compensation=as_str(dig(p, "compensation", "compensationTierSummary")),
            source_kind=raw.source_kind,
            raw=p,
        )
