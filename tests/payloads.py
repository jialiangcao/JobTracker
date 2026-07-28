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
