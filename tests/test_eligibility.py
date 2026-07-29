"""Location/age filters, exercised against location strings sampled from live boards."""

from datetime import UTC, datetime, timedelta

import pytest

from jobtrack.filtering.eligibility import is_eligible, is_recent, is_us_location
from jobtrack.schema import JobPosting

NOW = datetime(2026, 7, 28, tzinfo=UTC)


def job(
    *, title: str = "Software Engineer Intern", location: str | None = None, posted: int | None = 0
) -> JobPosting:
    """`posted` is days before NOW; None means the board exposed no date."""
    return JobPosting(
        title=title,
        company="Acme",
        url="https://example.test/1",
        location=location,
        posted_at=None if posted is None else NOW - timedelta(days=posted),
        source_kind="greenhouse",
        raw={},
    )


@pytest.mark.parametrize(
    "location",
    [
        "Chicago, IL",
        "Chicago, IL ",  # trailing space, as Akuna emits
        "Portland, OR",  # comma-preceded, so it reads as Oregon
        "San Francisco, CA; New York, NY",
        "San Francisco, CA • New York, NY • United States",
        "Chicago, United States",
        "Bellevue, Washington; Mountain View, California",
        "Atlanta; New York",
        "Toronto, New York, San Francisco",  # multi-site including the US
        "US / Canada",
        "Chicago",
        "Chicago, New York City",
        "Remote - US",
    ],
)
def test_us_locations_are_kept(location: str) -> None:
    assert is_us_location(job(location=location)) is True


@pytest.mark.parametrize(
    "location",
    [
        "London",
        "London, UK",
        "Singapore",
        "Montreal",
        "Amsterdam",
        "Amsterdam, Netherlands",
        "Toronto, Canada",
        "Sydney, Australia",
        "Belgrade, Serbia",
        "Dublin, IE",
        "Dublin OR London",  # the "OR" must not read as Oregon
        "London, Dublin",
        "Bengaluru",
        "Mumbai, India",
        "Gurugram, Haryana, India",
    ],
)
def test_non_us_locations_are_dropped(location: str) -> None:
    assert is_us_location(job(location=location)) is False


def test_falls_back_to_title_when_location_is_a_work_model() -> None:
    # Cloudflare puts "In-Office"/"Hybrid" in location and the city in the title.
    assert is_us_location(
        job(
            title="Software Engineer Intern (Fall 2026) - Austin, TX (In-Office)",
            location="In-Office",
        )
    )
    assert not is_us_location(
        job(title="Software Engineer Intern - London (Hybrid)", location="Hybrid")
    )


@pytest.mark.parametrize("location", [None, "", "Remote", "In-Office", "Hybrid"])
def test_unknown_geography_is_kept(location: str | None) -> None:
    assert is_us_location(job(location=location)) is True


def test_indiana_is_not_read_as_india() -> None:
    assert is_us_location(job(location="Indianapolis, Indiana")) is True


def test_recency_window() -> None:
    assert is_recent(job(posted=0), 30, NOW) is True
    assert is_recent(job(posted=29), 30, NOW) is True
    assert is_recent(job(posted=31), 30, NOW) is False
    assert is_recent(job(posted=None), 30, NOW) is True  # no date → keep
    assert is_recent(job(posted=400), 0, NOW) is True  # 0 disables the window


def test_is_eligible_requires_both() -> None:
    fresh_us = job(location="Chicago, IL", posted=1)
    stale_us = job(location="Chicago, IL", posted=90)
    fresh_uk = job(location="London", posted=1)

    assert is_eligible(fresh_us, max_age_days=30, us_only=True, now=NOW) is True
    assert is_eligible(stale_us, max_age_days=30, us_only=True, now=NOW) is False
    assert is_eligible(fresh_uk, max_age_days=30, us_only=True, now=NOW) is False
    assert is_eligible(fresh_uk, max_age_days=30, us_only=False, now=NOW) is True
