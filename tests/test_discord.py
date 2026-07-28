import httpx
import respx
from sqlalchemy import select

from jobtrack.config import Settings
from jobtrack.db import repo
from jobtrack.db.engine import SessionFactory
from jobtrack.db.models import Candidate
from jobtrack.notify.discord import DiscordSender, build_embed, drain_outbox
from jobtrack.schema import JobPosting

MESSAGES_URL = "https://discord.com/api/v10/channels/123456/messages"


def job(title: str = "SWE Intern", url: str = "https://x.example/j/1") -> JobPosting:
    return JobPosting(
        external_id=url,
        title=title,
        company="Acme",
        url=url,
        location="NYC",
        source_kind="greenhouse",
        raw={"title": title},
    )


async def seed_candidates(factory: SessionFactory, count: int) -> None:
    async with factory() as session:
        run = await repo.create_run(session)
        for i in range(count):
            j = job(title=f"Intern {i}", url=f"https://x.example/j/{i}")
            seen, _ = await repo.upsert_seen_job(
                session, source_id=1, job=j, run_id=run.id, matched=True
            )
            await repo.insert_candidate(session, run.id, seen.id, j)
        await session.commit()


@respx.mock
async def test_drain_sends_and_marks_rows(
    settings: Settings, session_factory: SessionFactory
) -> None:
    await seed_candidates(session_factory, 2)
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json={"id": "msg-1"}))
    async with session_factory() as session:
        sender = DiscordSender(settings)
        try:
            sent = await drain_outbox(session, sender, settings)
        finally:
            await sender.aclose()
    assert sent == 2
    assert route.call_count == 2
    async with session_factory() as session:
        rows = list(await session.scalars(select(Candidate)))
        assert all(r.send_status == "sent" for r in rows)
        assert all(r.discord_message_id == "msg-1" for r in rows)


@respx.mock
async def test_drain_handles_429_then_success(
    settings: Settings, session_factory: SessionFactory
) -> None:
    await seed_candidates(session_factory, 1)
    route = respx.post(MESSAGES_URL).mock(
        side_effect=[
            httpx.Response(429, json={"retry_after": 0.0}),
            httpx.Response(200, json={"id": "msg-2"}),
        ]
    )
    async with session_factory() as session:
        sender = DiscordSender(settings)
        try:
            sent = await drain_outbox(session, sender, settings)
        finally:
            await sender.aclose()
    assert sent == 1
    assert route.call_count == 2


@respx.mock
async def test_hard_failure_marks_failed_after_attempts(
    settings: Settings, session_factory: SessionFactory
) -> None:
    await seed_candidates(session_factory, 1)
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(403, text="missing access"))
    for _ in range(settings.discord_max_attempts):
        async with session_factory() as session:
            sender = DiscordSender(settings)
            try:
                sent = await drain_outbox(session, sender, settings)
            finally:
                await sender.aclose()
        assert sent == 0
    async with session_factory() as session:
        row = await session.scalar(select(Candidate))
        assert row is not None
        assert row.send_status == "failed"
        assert row.send_attempts == settings.discord_max_attempts


def test_embed_truncation() -> None:
    candidate = Candidate(
        run_id=1,
        seen_job_id=1,
        title="T" * 400,
        company="Acme",
        location="NYC",
        url="https://x.example/j/1",
        raw={},
    )
    embed = build_embed(candidate)
    assert len(embed["title"]) == 256
    assert embed["url"] == "https://x.example/j/1"
    assert "Acme" in embed["description"]
