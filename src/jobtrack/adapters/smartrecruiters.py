"""SmartRecruiters posting payload → JobPosting."""

from jobtrack.adapters.base import as_str, dig, parse_dt
from jobtrack.schema import JobPosting, RawPosting


class SmartRecruitersAdapter:
    def map(self, raw: RawPosting) -> JobPosting | None:
        p = raw.payload
        title = as_str(p.get("name"))
        if title is None:
            return None
        posting_id = as_str(p.get("id"))
        company_slug = as_str(dig(p, "company", "identifier"))
        url = as_str(p.get("applyUrl")) or as_str(p.get("postingUrl"))
        if url is None and posting_id and company_slug:
            url = f"https://jobs.smartrecruiters.com/{company_slug}/{posting_id}"
        if url is None:
            url = as_str(p.get("ref"))
        if url is None:
            return None
        city = as_str(dig(p, "location", "city"))
        country = as_str(dig(p, "location", "country"))
        location = ", ".join(x for x in (city, country) if x) or None
        remote = dig(p, "location", "remote")
        return JobPosting(
            external_id=posting_id,
            title=title,
            company=as_str(dig(p, "company", "name")) or raw.source_name,
            url=url,
            location=location,
            remote=remote if isinstance(remote, bool) else None,
            posted_at=parse_dt(p.get("releasedDate")),
            department=as_str(dig(p, "department", "label")),
            employment_type=as_str(dig(p, "typeOfEmployment", "label")),
            source_kind=raw.source_kind,
            raw=p,
        )
