"""Realistic (abbreviated) API payloads for adapter and pipeline tests."""

from typing import Any

GREENHOUSE_JOB: dict[str, Any] = {
    "id": 4000001,
    "title": "Software Engineering Intern (Summer 2027)",
    "absolute_url": "https://boards.greenhouse.io/acme/jobs/4000001?gh_src=abc123&utm_source=feed",
    "updated_at": "2026-07-20T12:00:00-04:00",
    "first_published": "2026-07-18T09:00:00-04:00",
    "location": {"name": "New York, NY"},
    "departments": [{"name": "Engineering"}],
    "offices": [{"name": "NYC"}],
    "content": "&lt;p&gt;Join our &lt;b&gt;Summer 2027&lt;/b&gt; software internship program.&lt;/p&gt;",
}

LEVER_JOB: dict[str, Any] = {
    "id": "abc-123",
    "text": "Backend Developer Intern",
    "hostedUrl": "https://jobs.lever.co/acme/abc-123",
    "applyUrl": "https://jobs.lever.co/acme/abc-123/apply",
    "createdAt": 1753500000000,
    "categories": {"location": "Remote — US", "team": "Platform", "commitment": "Intern"},
    "workplaceType": "remote",
    "descriptionPlain": "Winter 2027 internship on the platform team.",
    "lists": [{"text": "Requirements", "content": "<li>Python</li><li>SQL</li>"}],
}

ASHBY_JOB: dict[str, Any] = {
    "id": "8a5f2b10-1111-2222-3333-444455556666",
    "title": "Machine Learning Intern",
    "location": "San Francisco",
    "secondaryLocations": [],
    "isRemote": False,
    "employmentType": "Intern",
    "department": "AI",
    "team": "Research",
    "publishedAt": "2026-07-15T00:00:00Z",
    "jobUrl": "https://jobs.ashbyhq.com/acme/8a5f2b10-1111-2222-3333-444455556666",
    "applyUrl": "https://jobs.ashbyhq.com/acme/8a5f2b10-1111-2222-3333-444455556666/apply",
    "descriptionHtml": "<h2>About</h2><p>Spring 2027 ML internship.</p>",
    "compensation": {"compensationTierSummary": "$45-$55/hr"},
}

FULLTIME_JOB: dict[str, Any] = {
    "id": 4000002,
    "title": "Senior Staff Software Engineer",
    "absolute_url": "https://boards.greenhouse.io/acme/jobs/4000002",
    "location": {"name": "New York, NY"},
    "content": "Own large systems end to end.",
}

# Workday's search rows are thin: no description, and a relative date. `_base_url` is not
# from the API — the fetcher injects it so the adapter can resolve externalPath.
WORKDAY_JOB: dict[str, Any] = {
    "title": "Software Engineering Intern, Summer 2027",
    "externalPath": "/job/US-CA-Santa-Clara/Software-Engineering-Intern_JR2013673",
    "locationsText": "US, CA, Santa Clara",
    "postedOn": "Posted Today",
    "bulletFields": ["JR2013673"],
    "_base_url": "https://acme.wd5.myworkdayjobs.com/en-US/AcmeCareers",
}

# One page of the CxS search response, with the facet block the fetcher mines for ids.
WORKDAY_LIST_PAGE: dict[str, Any] = {
    "total": 2,
    "jobPostings": [
        {k: v for k, v in WORKDAY_JOB.items() if k != "_base_url"},
        {
            "title": "Senior Staff Software Engineer",
            "externalPath": "/job/US-CA-Santa-Clara/Senior-Staff-Software-Engineer_JR2011908",
            "locationsText": "US, CA, Santa Clara",
            "postedOn": "Posted 5 Days Ago",
            "bulletFields": ["JR2011908"],
        },
    ],
    "facets": [
        {
            "facetParameter": "workerSubType",
            "descriptor": "Job Type",
            "values": [
                {"id": "regular-id", "descriptor": "Regular Employee", "count": 2343},
                {"id": "ncg-id", "descriptor": "New College Graduate", "count": 81},
                {"id": "intern-id", "descriptor": "Intern (Fixed Term)", "count": 11},
            ],
        },
        {
            "facetParameter": "timeType",
            "descriptor": "Time Type",
            "values": [{"id": "ft-id", "descriptor": "Full time", "count": 2627}],
        },
    ],
}


# Gem's public GraphQL board query, one posting. Captured from jobs.gem.com; the
# description keeps its inline styling because that is what the endpoint really returns.
GEM_JOB: dict[str, Any] = {
    "id": "T2F0c0pvYlBvc3Q6OTI5MTI=",
    "extId": "5175038004",
    "title": "Software Engineering Intern",
    "descriptionHtml": (
        '<div><span style="color: rgb(38, 38, 38);">Join our Summer 2027 internship.'
        "</span></div><ul><li>Write <b>Python</b></li></ul>"
    ),
    # Epoch seconds as an int here; other boards send the same field as a string, which is
    # why the adapter coerces before parsing.
    "firstPublishedTsSec": 1775439282,
    "locations": [
        {
            "id": "20069",
            "name": "New York",
            "city": "New York",
            "isoCountry": "USA",
            "isRemote": False,
        },
        {"id": "20070", "name": "Remote (US)", "city": "", "isoCountry": None, "isRemote": True},
    ],
    "job": {
        "id": "T2F0c0pvYjoxMDA0NTQ=",
        "department": {"id": "32076", "name": "Content Engineering"},
        "locationType": "IN_OFFICE",
        "employmentType": "INTERN",
    },
    "_board_slug": "acme",
}

# Rippling's board list endpoint returns a bare array of these. Note there is no
# description and no timestamp — the list endpoint carries neither.
RIPPLING_JOB: dict[str, Any] = {
    "uuid": "9a4d79c0-d602-4cb4-a1d3-629b13faaa74",
    "name": "Software Engineering Intern",
    "department": {"id": "Build Engineering", "label": "Build Engineering"},
    "url": "https://ats.rippling.com/acme/jobs/9a4d79c0-d602-4cb4-a1d3-629b13faaa74",
    "workLocation": {"label": "Centennial, CO", "id": "Centennial, CO"},
}
