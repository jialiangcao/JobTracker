from jobtrack.db.models import FilterRule
from jobtrack.filtering.dedup import canonical_url, content_hash
from jobtrack.filtering.rules import SEED_RULES, compile_rules, matches
from jobtrack.schema import JobPosting


def make_job(title: str, description: str | None = None, location: str | None = None) -> JobPosting:
    return JobPosting(
        title=title,
        company="Acme",
        url="https://example.com/job/1",
        description=description,
        location=location,
        source_kind="test",
        raw={},
    )


def rule(name: str, kind: str, field: str, pattern: str, priority: int = 0) -> FilterRule:
    return FilterRule(name=name, kind=kind, field=field, pattern=pattern, priority=priority)


def seed_ruleset():
    return compile_rules(
        [rule(name, kind, field, pattern) for name, kind, field, pattern in SEED_RULES]
    )


def test_seed_rules_match_cs_internship() -> None:
    rules = seed_ruleset()
    assert matches(make_job("Software Engineering Intern", "Summer 2027 program"), rules)
    assert matches(make_job("Backend Developer Co-op", "Winter term"), rules)
    assert matches(make_job("SDE Intern"), rules)
    assert matches(make_job("Intern, Data Engineering"), rules)  # domain + role noun
    assert matches(make_job("Engineer Intern, Machine Learning"), rules)  # reversed order
    assert matches(make_job("Android Developer Internship"), rules)


def test_seed_rules_require_an_intern_signal_in_the_title() -> None:
    rules = seed_ruleset()
    assert not matches(make_job("Senior Software Engineer", "Summer start"), rules)
    assert not matches(make_job("Software Engineer II"), rules)
    # An intern mention buried in the description is not enough — title only.
    assert not matches(make_job("Software Engineer", "Our internship program runs in June"), rules)


def test_seed_rules_reject_non_software_internships() -> None:
    rules = seed_ruleset()
    assert not matches(make_job("Marketing Intern", "Summer 2027"), rules)
    assert not matches(make_job("Mechanical Engineering Intern", "Summer 2027"), rules)
    # Domain words that only look technical on their own.
    assert not matches(make_job("Data Entry Intern"), rules)
    assert not matches(make_job("Platform Marketing Intern"), rules)
    assert not matches(make_job("Physical Security Intern"), rules)
    assert not matches(make_job("AI Policy Intern"), rules)
    # Engineering-adjacent titles vetoed by the exclude rules.
    assert not matches(make_job("Sales Engineer Intern"), rules)
    assert not matches(make_job("Manufacturing Systems Engineering Intern"), rules)
    assert not matches(make_job("Technical Program Manager Intern"), rules)


def test_include_groups_or_within_and_across() -> None:
    rules = compile_rules(
        [
            rule("season:summer", "include", "title", r"summer"),
            rule("season:winter", "include", "title", r"winter"),
            rule("role", "include", "title", r"intern"),
        ]
    )
    assert matches(make_job("Summer Intern"), rules)  # season via summer variant
    assert matches(make_job("Winter Intern"), rules)  # season via winter variant
    assert not matches(make_job("Summer Analyst"), rules)  # role group fails
    assert not matches(make_job("Fall Intern"), rules)  # season group fails


def test_exclude_rule_vetoes() -> None:
    rules = compile_rules(
        [
            rule("role", "include", "title", r"intern"),
            rule("no-hw", "exclude", "title", r"hardware"),
        ]
    )
    assert matches(make_job("Software Intern"), rules)
    assert not matches(make_job("Hardware Intern"), rules)


def test_empty_ruleset_matches_everything() -> None:
    assert matches(make_job("Anything"), compile_rules([]))


def test_canonical_url_normalization() -> None:
    assert (
        canonical_url("HTTPS://Boards.Greenhouse.io/acme/jobs/1/?utm_source=x&gh_src=y")
        == "https://boards.greenhouse.io/acme/jobs/1"
    )
    # identifying params survive, order-independently
    a = canonical_url("https://x.example/j?gh_jid=5&b=2")
    b = canonical_url("https://x.example/j?b=2&gh_jid=5")
    assert a == b
    assert "gh_jid=5" in a


def test_content_hash_stability() -> None:
    assert content_hash("Intern", "Acme", None) == content_hash("  intern ", "ACME", "")
    assert content_hash("Intern", "Acme", "NYC") != content_hash("Intern", "Acme", "SF")
