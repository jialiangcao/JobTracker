from pathlib import Path

import pytest

from jobtrack.db import repo
from jobtrack.db.engine import SessionFactory
from jobtrack.sources_file import SourcesFileError, load_sources_file

VALID = """
[[source]]
kind = "greenhouse"
name = "Stripe"
slug = "stripe"

[[source]]
kind = "scrape"
name = "Acme"
url = "https://acme.example/careers"
enabled = false
politeness = { max_concurrency = 1 }
"""


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "sources.toml"
    path.write_text(body)
    return path


def test_parses_ats_and_scrape_entries(tmp_path: Path) -> None:
    specs = load_sources_file(write(tmp_path, VALID))

    assert [(s.kind, s.name) for s in specs] == [("greenhouse", "Stripe"), ("scrape", "Acme")]
    assert specs[0].config == {"slug": "stripe"}
    assert specs[0].enabled is None  # not declared
    assert specs[1].enabled is False
    assert specs[1].config["politeness"] == {"max_concurrency": 1}


def test_adapter_alias_maps_to_config_key(tmp_path: Path) -> None:
    body = '[[source]]\nkind = "lever"\nname = "Plaid"\nslug = "plaid"\nadapter = "fallback"\n'
    (spec,) = load_sources_file(write(tmp_path, body))
    assert spec.config == {"slug": "plaid", "adapter_override": "fallback"}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('[[source]]\nname = "Stripe"\nslug = "s"\n', "missing a 'kind'"),
        ('[[source]]\nkind = "greenhouse"\nslug = "s"\n', "missing a 'name'"),
        ('[[source]]\nkind = "greenhosue"\nname = "S"\nslug = "s"\n', "unknown kind"),
        ('[[source]]\nkind = "greenhouse"\nname = "S"\n', "need a 'slug'"),
        ('[[source]]\nkind = "scrape"\nname = "S"\nslug = "s"\n', "need a 'url'"),
        ('[[source]]\nkind = "workday"\nname = "S"\nslug = "s"\n', "workday sources need a 'url'"),
        ('[[source]]\nkind = "greenhouse"\nname = "S"\nslug = "s"\netag = "x"\n', "cannot be set"),
        ('[[source]]\nkind = "greenhouse"\nname = "S"\nslug = "s"\nnope = 1\n', "unknown key"),
        ('title = "oops"\n', "must contain at least one"),
    ],
)
def test_rejects_invalid_files(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(SourcesFileError, match=message):
        load_sources_file(write(tmp_path, body))


def test_workday_tuning_keys_are_file_managed(tmp_path: Path) -> None:
    body = (
        '[[source]]\nkind = "workday"\nname = "Acme"\n'
        'url = "https://acme.wd5.myworkdayjobs.com/en-US/AcmeCareers"\n'
        'tenant = "acme-inc"\nsite = "Careers"\nworker_sub_types = ["abc"]\nmax_pages = 5\n'
    )
    (spec,) = load_sources_file(write(tmp_path, body))
    assert spec.config == {
        "url": "https://acme.wd5.myworkdayjobs.com/en-US/AcmeCareers",
        "tenant": "acme-inc",
        "site": "Careers",
        "worker_sub_types": ["abc"],
        "max_pages": 5,
    }


def test_rejects_duplicates(tmp_path: Path) -> None:
    body = VALID + '\n[[source]]\nkind = "greenhouse"\nname = "Stripe"\nslug = "other"\n'
    with pytest.raises(SourcesFileError, match="duplicate source"):
        load_sources_file(write(tmp_path, body))


def test_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(SourcesFileError, match="not found"):
        load_sources_file(tmp_path / "absent.toml")


async def test_sync_adds_updates_and_preserves_runtime_keys(
    tmp_path: Path, session_factory: SessionFactory
) -> None:
    (spec,) = load_sources_file(
        write(tmp_path, '[[source]]\nkind = "greenhouse"\nname = "Stripe"\nslug = "stripe"\n')
    )
    async with session_factory() as session:
        assert await repo.sync_source(session, spec) == "added"
        assert await repo.sync_source(session, spec) == "unchanged"

        # The pipeline stores a fetch validator; syncing a changed slug must not drop it.
        source = await repo.find_source(session, "greenhouse", "Stripe")
        assert source is not None
        await repo.record_source_success(session, source, etag='W/"abc"')
        await session.commit()

        (renamed,) = load_sources_file(
            write(tmp_path, '[[source]]\nkind = "greenhouse"\nname = "Stripe"\nslug = "stripe2"\n')
        )
        assert await repo.sync_source(session, renamed) == "updated"
        assert source.config == {"slug": "stripe2", "etag": 'W/"abc"'}


async def test_config_updates_are_stored_retracted_and_survive_a_sync(
    tmp_path: Path, session_factory: SessionFactory
) -> None:
    """Workday caches its discovered facet through this path — it has to outlive a sync
    the same way an ETag does, and a None must retract it rather than store a null."""
    body = '[[source]]\nkind = "workday"\nname = "Acme"\nurl = "https://acme.wd5.myworkdayjobs.com/en-US/C"\n'
    (spec,) = load_sources_file(write(tmp_path, body))
    async with session_factory() as session:
        await repo.sync_source(session, spec)
        source = await repo.find_source(session, "workday", "Acme")
        assert source is not None

        facet = {"parameter": "workerSubType", "values": ["intern-id"]}
        await repo.record_source_success(session, source, config_updates={"facet": facet})
        await session.commit()
        assert source.config["facet"] == facet

        assert await repo.sync_source(session, spec) in ("unchanged", "updated")
        assert source.config["facet"] == facet

        await repo.record_source_success(session, source, config_updates={"facet": None})
        await session.commit()
        assert "facet" not in source.config


async def test_sync_leaves_enabled_alone_unless_declared(
    tmp_path: Path, session_factory: SessionFactory
) -> None:
    body = '[[source]]\nkind = "ashby"\nname = "Ramp"\nslug = "ramp"\n'
    (spec,) = load_sources_file(write(tmp_path, body))
    async with session_factory() as session:
        await repo.sync_source(session, spec)
        source = await repo.find_source(session, "ashby", "Ramp")
        assert source is not None

        # Circuit breaker disables it; a plain sync must not resurrect it.
        source.enabled = False
        source.disabled_reason = "circuit breaker"
        source.consecutive_failures = 5
        assert await repo.sync_source(session, spec) == "unchanged"
        assert source.enabled is False

        (explicit,) = load_sources_file(write(tmp_path, body + "enabled = true\n"))
        assert await repo.sync_source(session, explicit) == "updated"
        assert source.enabled is True
        assert source.consecutive_failures == 0
        assert source.disabled_reason is None
