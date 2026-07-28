"""Lever posting payload → JobPosting."""

from typing import Any

from jobtrack.adapters.base import as_str, dig, parse_dt, strip_html
from jobtrack.schema import JobPosting, RawPosting


def _description(p: dict[str, Any]) -> str | None:
    parts: list[str] = []
    if plain := as_str(p.get("descriptionPlain")):
        parts.append(plain)
    elif html := as_str(p.get("description")):
        parts.append(strip_html(html))
    lists = p.get("lists")
    if isinstance(lists, list):
        for section in lists:
            if isinstance(section, dict):
                heading = as_str(section.get("text"))
                body = section.get("content")
                if heading:
                    parts.append(heading)
                if isinstance(body, str):
                    parts.append(strip_html(body))
    return "\n".join(parts) or None


class LeverAdapter:
    def map(self, raw: RawPosting) -> JobPosting | None:
        p = raw.payload
        title = as_str(p.get("text"))
        url = as_str(p.get("hostedUrl")) or as_str(p.get("applyUrl"))
        if title is None or url is None:
            return None
        workplace = as_str(p.get("workplaceType"))
        return JobPosting(
            external_id=as_str(p.get("id")),
            title=title,
            company=raw.source_name,
            url=url,
            location=as_str(dig(p, "categories", "location")),
            remote=workplace == "remote" if workplace else None,
            description=_description(p),
            posted_at=parse_dt(p.get("createdAt")),
            department=as_str(dig(p, "categories", "team"))
            or as_str(dig(p, "categories", "department")),
            employment_type=as_str(dig(p, "categories", "commitment")),
            source_kind=raw.source_kind,
            raw=p,
        )
