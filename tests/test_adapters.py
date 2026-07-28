from datetime import UTC, datetime
from typing import Any

from jobtrack.adapters.ashby import AshbyAdapter
from jobtrack.adapters.fallback import FallbackAdapter
from jobtrack.adapters.greenhouse import GreenhouseAdapter
from jobtrack.adapters.lever import LeverAdapter
from jobtrack.adapters.registry import get_adapter
from jobtrack.schema import RawPosting
from tests.payloads import ASHBY_JOB, GREENHOUSE_JOB, LEVER_JOB


def raw(payload: dict[str, Any], kind: str = "test") -> RawPosting:
    return RawPosting(
        source_id=1,
        source_kind=kind,
        source_name="Acme",
        payload=payload,
        fetched_at=datetime.now(UTC),
    )


def test_greenhouse_adapter() -> None:
    job = GreenhouseAdapter().map(raw(GREENHOUSE_JOB, "greenhouse"))
    assert job is not None
    assert job.external_id == "4000001"
    assert job.title == "Software Engineering Intern (Summer 2027)"
    assert job.company == "Acme"
    assert job.location == "New York, NY"
    assert job.department == "Engineering"
    assert job.description is not None
    assert "Summer 2027" in job.description
    assert "<" not in job.description  # escaped HTML stripped
    assert job.posted_at is not None
    assert job.posted_at.tzinfo is not None


def test_lever_adapter() -> None:
    job = LeverAdapter().map(raw(LEVER_JOB, "lever"))
    assert job is not None
    assert job.external_id == "abc-123"
    assert job.url == "https://jobs.lever.co/acme/abc-123"
    assert job.remote is True
    assert job.employment_type == "Intern"
    assert job.description is not None
    assert "Winter 2027" in job.description
    assert "Python" in job.description  # lists content included
    assert job.posted_at == datetime.fromtimestamp(1753500000, tz=UTC)


def test_ashby_adapter() -> None:
    job = AshbyAdapter().map(raw(ASHBY_JOB, "ashby"))
    assert job is not None
    assert job.title == "Machine Learning Intern"
    assert job.remote is False
    assert job.compensation == "$45-$55/hr"
    assert job.department == "AI"
    assert job.description is not None
    assert "Spring 2027" in job.description


def test_adapter_returns_none_without_title_or_url() -> None:
    assert GreenhouseAdapter().map(raw({"id": 1})) is None
    assert LeverAdapter().map(raw({"text": "Intern"})) is None
    assert AshbyAdapter().map(raw({"jobUrl": "https://x.example"})) is None


def test_fallback_adapter_handles_mislabeled_payloads() -> None:
    """The generic adapter must extract title/url from every known ATS shape."""
    fallback = FallbackAdapter()
    for payload, expected_title in [
        (GREENHOUSE_JOB, "Software Engineering Intern (Summer 2027)"),
        (LEVER_JOB, "Backend Developer Intern"),
        (ASHBY_JOB, "Machine Learning Intern"),
    ]:
        job = fallback.map(raw(payload, "mystery-ats"))
        assert job is not None, f"fallback failed on {expected_title}"
        assert job.title == expected_title
        assert job.url.startswith("https://")
        assert job.company == "Acme"
        assert job.location is not None


def test_fallback_adapter_rejects_unusable_payload() -> None:
    assert FallbackAdapter().map(raw({"foo": "bar"})) is None


def test_registry_resolution() -> None:
    assert isinstance(get_adapter("greenhouse"), GreenhouseAdapter)
    assert isinstance(get_adapter("unknown-kind"), FallbackAdapter)
    assert isinstance(get_adapter("greenhouse", override="fallback"), FallbackAdapter)
