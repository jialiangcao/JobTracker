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


# Software role nouns, and the domain words that only count as CS when one of those nouns
# sits next to them. Bare "data", "platform", "security", "mobile", "ai" and "engineer" are
# never CS signals on their own — alone they match Data Entry, Platform Marketing, Physical
# Security, Mobile Marketing, AI Policy and Mechanical Engineer postings.
_ROLE_NOUN = r"(?:engineer(?:ing)?|developer|development|scien(?:ce|tist)|programm(?:er|ing))"
_CS_DOMAIN = (
    r"(?:data|ml|ai|artificial\s+intelligence|machine\s+learning|cloud|platform"
    r"|infrastructure|infra|security|cyber(?:security)?|mobile|graphics|game|database"
    r"|network(?:ing)?)"
)
# Up to two qualifier words (plus separators) may sit between the pair: "Data Platform
# Engineer", "Engineer, Machine Learning". Bounded so the two halves can't come from
# opposite ends of a long multi-role title.
_GAP = r"[\s,:/&+.-]{0,3}(?:[\w+#.]+[\s,:/&+.-]{1,3}){0,2}"

SEED_RULES: list[tuple[str, str, str, str]] = [
    # (name, kind, field, pattern)
    # Posting age and US-only location are NOT here — neither is a regex over a single
    # field. They run after these rules; see filtering/eligibility.py, tuned with the
    # MAX_POSTING_AGE_DAYS and US_ONLY settings.
    #
    # Both include groups are AND'd: a job needs an explicit intern signal AND a software
    # signal, both in the title. Titles only — descriptions say "interns" in benefits
    # boilerplate and "engineering" in company blurbs, so matching them was pure noise.
    (
        "role",
        "include",
        "title",
        r"\bintern(?:ship)?s?\b|\bco[-\s]?ops?\b|\bindustrial\s+placement\b",
    ),
    # Terms that mean software on their own, no qualifier needed.
    (
        "cs:core",
        "include",
        "title",
        r"\bsoftware\b|\bswe\b|\bsdet?\b|computer\s+scien(?:ce|tist)|\bprogramm(?:er|ing)\b"
        r"|back[-\s]?end|front[-\s]?end|full[-\s]?stack|web\s+dev(?:eloper|elopment)?"
        r"|\bdevops\b|\bsre\b|site\s+reliability|distributed\s+systems?|\bcompilers?\b"
        r"|operating\s+systems?|\bfirmware\b|embedded\s+(?:software|systems?)"
        r"|machine\s+learning|deep\s+learning|computer\s+vision|\bnlp\b|\bml\b|\bllm\b"
        r"|\bios\b|\bandroid\b",
    ),
    ("cs:domain", "include", "title", rf"\b{_CS_DOMAIN}\b{_GAP}{_ROLE_NOUN}\b"),
    ("cs:domain-reversed", "include", "title", rf"\b{_ROLE_NOUN}\b{_GAP}{_CS_DOMAIN}\b"),
    # Adjacent engineering and science disciplines, which reach cs:domain through their own
    # use of "engineering" — "Manufacturing Systems Engineering Intern" and the like.
    (
        "not-other-eng",
        "exclude",
        "title",
        r"\bmechanical\b|\belectrical\b|\bmechatronics\b|\bcivil\b|\bchemical\b|\bstructural\b"
        r"|\baerospace\b|\bnuclear\b|\bpetroleum\b|\bmaterials\b|\bmanufacturing\b|\bthermal\b"
        r"|\bindustrial\s+engineer|\bprocess\s+engineer|\bhardware\b|\bphysical\s+security\b"
        r"|\bbio(?:medical|logy|logical|informatics|chem\w*)\b|\bclinical\b|\bchemistry\b",
    ),
    # Non-engineering functions that borrow engineering vocabulary in their titles.
    (
        "not-business",
        "exclude",
        "title",
        r"\bsales\b|\bmarketing\b|\brecruit\w*|\btalent\b|\bhuman\s+resources\b"
        r"|\bbusiness\s+(?:development|analyst|operations)\b|\bcustomer\s+(?:success|service)\b"
        r"|\b(?:customer|technical)\s+support\b|\bsupply\s+chain\b|\bsolutions\s+engineer"
        r"|\b(?:product|program|project)\s+manage\w*|\bdeveloper\s+(?:relations|advocate)\b"
        r"|\bux\b|\bui\s*/\s*ux\b|\b(?:graphic|product|industrial)\s+design\b",
    ),
]
