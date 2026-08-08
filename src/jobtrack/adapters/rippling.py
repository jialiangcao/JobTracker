"""Rippling ATS posting → JobPosting."""

import re

from jobtrack.adapters.base import as_str, dig
from jobtrack.schema import JobPosting, RawPosting

# Rippling has no remote flag on the list endpoint; the work location label is the only
# signal ("Remote - US", "Remote (Canada)"). Matched at a word boundary so a genuine
# place name that merely contains the letters cannot trip it.
_REMOTE = re.compile(r"\bremote\b", re.IGNORECASE)


class RipplingAdapter:
    def map(self, raw: RawPosting) -> JobPosting | None:
        p = raw.payload
        title = as_str(p.get("name"))
        url = as_str(p.get("url"))
        if title is None or url is None:
            return None

        location = as_str(dig(p, "workLocation", "label")) or as_str(dig(p, "workLocation", "id"))
        return JobPosting(
            external_id=as_str(p.get("uuid")),
            title=title,
            company=as_str(p.get("companyName")) or raw.source_name,
            url=url,
            location=location,
            # None rather than False when there is no location at all: absent, not on-site.
            remote=bool(_REMOTE.search(location)) if location else None,
            # The list endpoint carries no description — see sources/rippling.py.
            description=None,
            # Nor any timestamp. is_recent fails open on posted_at=None, so MAX_POSTING_AGE_DAYS
            # simply doesn't constrain these sources — they are never dropped for being stale.
            posted_at=None,
            department=as_str(dig(p, "department", "label")) or as_str(dig(p, "department", "id")),
            employment_type=as_str(dig(p, "employmentType", "id")),
            source_kind=raw.source_kind,
            raw=p,
        )
