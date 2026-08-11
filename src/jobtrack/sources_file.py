"""Declarative source list: parse sources.toml into specs for `jobtrack sources sync`.

The file owns MANAGED_KEYS; the app owns RUNTIME_KEYS (what a fetch writes back: validators
and Workday's discovered facet). Sync merges rather than replaces so syncing never clobbers
a stored ETag.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jobtrack.sources.registry import FETCHERS

MANAGED_KEYS = (
    "slug",
    "url",
    "adapter_override",
    "politeness",
    "needs_browser",
    "proxy_pool",
    # Workday: overrides for the tenant/site derived from the careers URL, plus crawl tuning.
    "tenant",
    "site",
    "worker_sub_types",
    "max_pages",
)
# "facet" is Workday's cached facet discovery, stamped with the time it was found — pin ids
# by hand with the file-managed "worker_sub_types" instead, which takes precedence over it.
RUNTIME_KEYS = ("etag", "last_modified", "facet")

# Kinds addressed by a full URL rather than a board slug.
URL_KINDS = frozenset({"scrape", "workday"})

# TOML key → config key, where they differ.
_ALIASES = {"adapter": "adapter_override"}


class SourcesFileError(Exception):
    pass


@dataclass(frozen=True)
class SourceSpec:
    kind: str
    name: str
    config: dict[str, Any]
    enabled: bool | None  # None = not declared; leave whatever the DB has


def load_sources_file(path: Path) -> list[SourceSpec]:
    """Parse and validate sources.toml. Raises SourcesFileError on any problem."""
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise SourcesFileError(f"{path} not found") from exc
    except tomllib.TOMLDecodeError as exc:
        raise SourcesFileError(f"{path} is not valid TOML: {exc}") from exc

    entries = data.get("source")
    if not isinstance(entries, list):
        raise SourcesFileError(f"{path} must contain at least one [[source]] table")

    specs: list[SourceSpec] = []
    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(entries, start=1):
        spec = _spec_from(entry, index)
        key = (spec.kind, spec.name)
        if key in seen:
            raise SourcesFileError(f"duplicate source {spec.kind}/{spec.name}")
        seen.add(key)
        specs.append(spec)
    return specs


def _spec_from(entry: Any, index: int) -> SourceSpec:
    where = f"[[source]] #{index}"
    if not isinstance(entry, dict):
        raise SourcesFileError(f"{where} must be a table")

    kind = entry.get("kind")
    name = entry.get("name")
    if not isinstance(kind, str) or not kind:
        raise SourcesFileError(f"{where} is missing a 'kind'")
    if not isinstance(name, str) or not name:
        raise SourcesFileError(f"{where} ({kind}) is missing a 'name'")
    if kind not in FETCHERS:
        raise SourcesFileError(
            f"{where} has unknown kind {kind!r}; expected one of {', '.join(sorted(FETCHERS))}"
        )

    enabled = entry.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise SourcesFileError(f"{kind}/{name}: 'enabled' must be true or false")

    config: dict[str, Any] = {}
    for key, value in entry.items():
        if key in ("kind", "name", "enabled"):
            continue
        config_key = _ALIASES.get(key, key)
        if config_key in RUNTIME_KEYS:
            raise SourcesFileError(
                f"{kind}/{name}: {key!r} is written by the app and cannot be set here"
            )
        if config_key not in MANAGED_KEYS:
            raise SourcesFileError(f"{kind}/{name}: unknown key {key!r}")
        config[config_key] = value

    if kind in URL_KINDS:
        if "url" not in config:
            raise SourcesFileError(f"{kind}/{name}: {kind} sources need a 'url'")
    elif "slug" not in config:
        raise SourcesFileError(f"{kind}/{name}: {kind} sources need a 'slug'")

    return SourceSpec(kind=kind, name=name, config=config, enabled=enabled)


def merge_config(existing: dict[str, Any], declared: dict[str, Any]) -> dict[str, Any]:
    """File-managed keys come from `declared`; everything else (ETags) is preserved."""
    kept = {k: v for k, v in existing.items() if k not in MANAGED_KEYS}
    return kept | declared
