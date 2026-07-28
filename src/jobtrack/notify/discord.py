"""Discord delivery: embed builder + rate-limit-aware REST sender draining the outbox.

Message sends in one channel share a ~5 msgs / 5 s bucket; we pace conservatively and
additionally obey X-RateLimit-Remaining/Reset-After response headers and 429 retry_after.
"""

import asyncio
import contextlib
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from jobtrack.config import Settings
from jobtrack.db import repo
from jobtrack.db.models import Candidate
from jobtrack.observability.logging import get_logger

log = get_logger(__name__)

_API_BASE = "https://discord.com/api/v10"


class DiscordError(Exception):
    pass


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def build_embed(candidate: Candidate) -> dict[str, Any]:
    lines = [f"**{candidate.company}**"]
    if candidate.location:
        lines.append(candidate.location)
    embed: dict[str, Any] = {
        "title": _truncate(candidate.title, 256),
        "url": candidate.url,
        "description": _truncate("\n".join(lines), 4096),
        "color": 0x2ECC71,
    }
    if candidate.posted_at is not None:
        embed["timestamp"] = candidate.posted_at.isoformat()
    embed["footer"] = {"text": "jobtrack"}
    return embed


class DiscordSender:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=30)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send_embed(self, embed: dict[str, Any]) -> str:
        """POST one message; returns the message id. Raises DiscordError on failure."""
        s = self._settings
        url = f"{_API_BASE}/channels/{s.discord_channel_id}/messages"
        headers = {"Authorization": f"Bot {s.discord_bot_token}"}
        for attempt in range(s.discord_max_attempts):
            response = await self._client.post(url, headers=headers, json={"embeds": [embed]})
            if response.status_code == 429:
                body: dict[str, Any] = {}
                with contextlib.suppress(ValueError):
                    body = response.json()
                retry_after = float(body.get("retry_after", 1.0)) + 0.5
                log.warning("discord rate limited", retry_after=retry_after, attempt=attempt + 1)
                await asyncio.sleep(retry_after)
                continue
            if response.status_code >= 400:
                raise DiscordError(
                    f"Discord API HTTP {response.status_code}: {response.text[:300]}"
                )
            await self._respect_bucket(response)
            data = response.json()
            return str(data.get("id", ""))
        raise DiscordError(f"still rate limited after {s.discord_max_attempts} attempts")

    async def _respect_bucket(self, response: httpx.Response) -> None:
        remaining = response.headers.get("X-RateLimit-Remaining")
        reset_after = response.headers.get("X-RateLimit-Reset-After")
        if remaining == "0" and reset_after is not None:
            with contextlib.suppress(ValueError):
                await asyncio.sleep(float(reset_after))


async def drain_outbox(session: AsyncSession, sender: DiscordSender, settings: Settings) -> int:
    """Send all pending candidates (including leftovers from crashed runs). Each row is
    committed individually so a crash mid-drain never re-sends. Returns sent count."""
    pending = await repo.pending_candidates(session, settings.discord_max_attempts)
    sent = 0
    for candidate in pending:
        try:
            message_id = await sender.send_embed(build_embed(candidate))
        except (DiscordError, httpx.HTTPError) as exc:
            log.error("discord send failed", candidate_id=candidate.id, error=str(exc))
            await repo.mark_candidate_failed(session, candidate, settings.discord_max_attempts)
            await session.commit()
            continue
        await repo.mark_candidate_sent(session, candidate, message_id or None)
        await session.commit()
        sent += 1
        await asyncio.sleep(settings.discord_pace_seconds)
    return sent
