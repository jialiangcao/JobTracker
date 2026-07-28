"""Compile filter_rules rows into regex predicates and apply them.

Semantics: a job matches iff every enabled include-group has at least one rule that
matches, and no enabled exclude rule matches. Rules named "group:variant" share the
"group" include-group (OR'd within it); groups are AND'd.
"""

import re
from dataclasses import dataclass

from jobtrack.db.models import FilterRule
from jobtrack.schema import JobPosting


@dataclass(frozen=True)
class CompiledRule:
    name: str
    group: str
    kind: str  # include | exclude
    field: str  # title | description | location | title+description
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class RuleSet:
    includes: dict[str, list[CompiledRule]]  # group -> rules (OR'd)
    excludes: list[CompiledRule]


def compile_rules(rows: list[FilterRule]) -> RuleSet:
    includes: dict[str, list[CompiledRule]] = {}
    excludes: list[CompiledRule] = []
    for row in sorted(rows, key=lambda r: r.priority):
        compiled = CompiledRule(
            name=row.name,
            group=row.name.split(":", 1)[0],
            kind=row.kind,
            field=row.field,
            pattern=re.compile(row.pattern, re.IGNORECASE),
        )
        if row.kind == "include":
            includes.setdefault(compiled.group, []).append(compiled)
        else:
            excludes.append(compiled)
    return RuleSet(includes=includes, excludes=excludes)


def _field_text(job: JobPosting, field: str) -> str:
    match field:
        case "title":
            return job.title
        case "description":
            return job.description or ""
        case "location":
            return job.location or ""
        case "title+description":
            return f"{job.title}\n{job.description or ''}"
        case _:
            return ""


def matches(job: JobPosting, rules: RuleSet) -> bool:
    for group_rules in rules.includes.values():
        if not any(r.pattern.search(_field_text(job, r.field)) for r in group_rules):
            return False
    return all(not r.pattern.search(_field_text(job, r.field)) for r in rules.excludes)


SEED_RULES: list[tuple[str, str, str, str]] = [
    # (name, kind, field, pattern)
    ("role", "include", "title", r"\bintern(ship)?\b|\bco[-\s]?op\b"),
    (
        "cs",
        "include",
        "title",
        r"software|swe\b|developer|engineer|data|machine\s*learning|\bml\b|\bai\b|security"
        r"|infra|backend|back[-\s]?end|front[-\s]?end|frontend|full[-\s]?stack|mobile|devops"
        r"|sre\b|platform|quant",
    ),
    ("season", "include", "title+description", r"winter|spring|summer|20(2[6-9])"),
    (
        "not-hardware",
        "exclude",
        "title",
        r"mechanical|electrical\s+eng|civil\b|chemical\b|hardware\b",
    ),
]
