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
_ROLE_NOUN = (
    r"(?:engineer(?:ing)?|developer|development|scien(?:ce|tist)|programm(?:er|ing)"
    r"|analyst)"
)
_CS_DOMAIN = (
    r"(?:data|ml|ai|artificial\s+intelligence|machine\s+learning|cloud|platform"
    r"|infrastructure|infra|security|cyber(?:security)?|mobile|graphics|game|database"
    r"|network(?:ing)?|technolog(?:y|ies)|technical|information\s+systems?|analytics)"
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
        r"\bintern(?:ship)?s?\b|\bco[-\s]?ops?\b|\bindustrial\s+placement\b"
        # Banks and large enterprises label internships by season instead: "2027 Summer
        # Analyst - Technology", "Summer Associate, Engineering". The cs group still has
        # to match, so the non-technical majority of these never reaches the output.
        r"|\bsummer\s+(?:analyst|associate|scholar)\b|\b(?:spring|fall|winter)\s+analyst\b"
        r"|\bapprentice(?:ship)?\b|\bplacement\s+(?:student|year)\b|\bworking\s+student\b"
        r"|\bstudent\s+(?:worker|assistant|trainee)\b|\bgraduate\s+programme\b",
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
    # Bare role noun with no domain qualifier: "Engineering Intern", "Software Development
    # Co-op", "Developer Intern". At a software company these are almost always SWE; the
    # not-other-eng exclude below is what keeps the mechanical/civil/biomed ones out, so
    # these two rules are only as safe as that list is complete.
    ("cs:bare-eng", "include", "title", r"\bengineer(?:ing|s)?\b|\bdevelop(?:er|ment)\b"),
    # Enterprise/bank phrasing that never says "software": "2027 Summer Analyst —
    # Technology", "Intern - Technology Division", "Technical Intern", "IT Intern".
    (
        "cs:tech-bare",
        "include",
        "title",
        # (?-i:IT) stays case-sensitive inside the IGNORECASE compile so the pronoun "it"
        # in a title like "Build It" can't match.
        r"\btech(?:nolog(?:y|ies)|nical)?\b|\b(?-i:IT)\b|\bdigital\b",
    ),
    # Adjacent engineering and science disciplines, which reach cs:domain through their own
    # use of "engineering" — "Manufacturing Systems Engineering Intern" and the like.
    (
        "not-other-eng",
        "exclude",
        "title",
        r"\bmechanical\b|\belectrical\b|\bmechatronics\b|\bcivil\b|\bchemical\b|\bstructural\b"
        r"|\baerospace\b|\bnuclear\b|\bpetroleum\b|\bmaterials\b|\bmanufacturing\b|\bthermal\b"
        r"|\bindustrial\s+engineer|\bprocess\s+engineer|\bphysical\s+security\b"
        r"|\bbio(?:medical|logy|logical|informatics|chem\w*|tech\w*)\b|\bclinical\b|\bchemistry\b"
        # Added alongside cs:bare-eng — a bare "X Engineering Intern" now reaches the include
        # side, so every non-CS discipline has to be named here or it comes through.
        r"|\benvironmental\b|\bgeo(?:technical|logical|physics|spatial)\b|\bmining\b|\bmarine\b"
        r"|\bnaval\b|\bautomotive\b|\bagricultur\w*|\bagronom\w*|\bfood\b|\btextile\b"
        r"|\bpackaging\b|\bwelding\b|\btooling\b|\bfacilities\b|\bhvac\b|\bplumbing\b"
        r"|\bpower\s+systems?\b|\bwastewater\b|\bhydraulic\b|\btransportation\b|\btraffic\b"
        r"|\bsurvey(?:ing|or)\b|\bdrafting\b|\barchitectur(?:e|al)\b|\bconstruction\b"
        r"|\bmetallurg\w*|\bpolymer\b|\bceramic\w*|\bcorrosion\b|\bweld\w*"
        r"|\boptic(?:s|al)\b|\bphotonics?\b|\blasers?\b|\bacoustics?\b|\bnano\w*"
        r"|\bsemiconductor\b|\basics?\b|\bvlsi\b|\bpcb\b|\bcircuits?\b|\bantennas?\b|\brf\b"
        r"|\banalog\b|\bsilicon\b|\bwafer\b|\bfoundry\b|\bdigital\s+design\b"
        r"|\bphysics\b|\bpharma\w*|\bdrug\b|\bgenom\w*|\bprotein\b|\bmolecular\b|\bneuro\w*"
        r"|\bveterinary\b|\bnursing\b|\bdental\b|\bmedical\b|\bhealthcare\b",
    ),
    # "Hardware" alone excludes, but not when the title also carries an explicit software
    # signal — "Software Engineer Intern, Hardware Platforms" is a SWE role at Apple/NVIDIA.
    (
        "not-hardware",
        "exclude",
        "title",
        r"^(?!.*(?:\bsoftware\b|\bfirmware\b|\bembedded\b|\bswe\b))(?=.*\bhardware\b)",
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
        r"|\bux\b|\bui\s*/\s*ux\b|\b(?:graphic|product|industrial)\s+design\b"
        # Added alongside cs:tech-bare — bare "technology"/"digital" now reaches the include
        # side, and these are the functions that use those words without being CS roles.
        r"|\bwriter\b|\bwriting\b|\bcontent\b|\bcommunications\b|\bsocial\s+media\b"
        r"|\bpublic\s+relations\b|\bjournalis\w*|\beditor(?:ial)?\b|\bcopywrit\w*|\bseo\b"
        r"|\bconsult(?:ant|ing)\b|\bstrategy\b|\blegal\b|\bcounsel\b|\bparalegal\b"
        r"|\bcompliance\b|\baudit(?:or|ing)?\b|\baccounting\b|\btax\b|\bprocurement\b"
        r"|\blogistics\b|\bwarehouse\b|\bretail\b|\bmerchandis\w*|\bbuyer\b"
        r"|\binsurance\b|\bclaims\b|\bunderwrit\w*|\bactuarial\b|\breal\s+estate\b"
        r"|\bteach\w*|\btutor\w*|\binstructor\b|\bcurriculum\b|\badmissions\b"
        r"|\bevents?\b|\badministrative\b|\bexecutive\s+assistant\b|\btranslat\w*"
        r"|\bphotograph\w*|\bvideograph\w*|\bbrand\b|\bcreative\b|\bcopy\b"
        r"|\btraining\b|\bpolicy\b|\bgovernance\b|\bprivacy\b|\bethics\b",
    ),
]
