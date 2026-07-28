"""Dedup keys: canonical URL normalization and content hashing."""

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query params that vary per-visit without identifying a different job.
_TRACKING_PARAMS = {"ref", "referrer", "source", "src", "gh_src", "lever-origin", "lever-source"}


def canonical_url(url: str) -> str:
    """Normalize a job URL: lowercase scheme/host, drop tracking params + fragment,
    sort remaining params, strip trailing slash."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.endswith(":80") or host.endswith(":443"):
        host = host.rsplit(":", 1)[0]
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    query.sort()
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(query), ""))


def content_hash(title: str, company: str, location: str | None) -> str:
    """Fallback dedup key when a source exposes neither stable ids nor stable URLs."""
    basis = f"{title.strip().lower()}|{company.strip().lower()}|{(location or '').strip().lower()}"
    return hashlib.sha256(basis.encode()).hexdigest()
