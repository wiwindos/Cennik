import json

from typer.testing import CliRunner

from ahledger.cli import app
from ahledger.config import Settings
from ahledger.planner import plan
from ahledger.storage import State


def test_catalog_items_without_current_price_are_planned(state, settings):
    state.set("markets", [{"id": "forever.normal.alliance.us"}, {"id": "forever.rp.horde.eu"}])
    state.set("active_markets", ["forever.normal.alliance.us"])
    state.candidates({99999: {"id": 99999, "name": "Not listed today"}}, catalogue=True)
    state.db.commit()
    plan(settings, state)
    jobs = list(state.db.execute("SELECT * FROM jobs WHERE dataset='history' AND item_id=99999"))
    assert len(jobs) == 4
    assert all(row["market"] == "forever.normal.alliance.us" for row in jobs)
    before = sum(state.counts().values())
    plan(settings, state)
    assert sum(state.counts().values()) == before


def test_reference_enrichment_and_dynamic_professions(state, settings):
    settings.enrich_items = "all"
    state.set("markets", [{"id": "forever.normal.alliance.us"}])
    state.set("active_markets", ["forever.normal.alliance.us"])
    state.set("professions", ["Tailoring", "Future Profession"])
    state.candidates({2589: {"id": 2589}}, catalogue=True)
    state.db.commit()
    plan(settings, state)
    assert state.db.execute("SELECT count(*) FROM jobs WHERE dataset='item'").fetchone()[0] == 1
    recipes = list(state.db.execute("SELECT spec FROM jobs WHERE dataset='crafting'"))
    assert {json.loads(row[0])["params"]["profession"] for row in recipes} == {
        "Tailoring",
        "Future Profession",
    }


def test_cli_help_and_dry_run_have_no_network(tmp_path):
    runner = CliRunner()
    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0 and "download" in help_result.output
    result = runner.invoke(app, ["--data-dir", str(tmp_path / "empty"), "download", "--dry-run"])
    assert result.exit_code == 0
    assert '"network": false' in result.output
    state = State(tmp_path / "empty")
    try:
        assert state.counts() == {}
    finally:
        state.close()


def test_settings_prevent_accidental_high_rates():
    import pytest

    with pytest.raises(ValueError):
        Settings(requests_per_second=100)


def test_market_selection_limits_new_jobs_and_keeps_existing_queue(state, settings):
    selected = "forever.pvp.horde.us"
    other = "forever.normal.alliance.us"
    state.set("markets", [{"id": selected}, {"id": other}])
    state.set("active_markets", [selected, other])
    state.candidates({2589: {"id": 2589}}, catalogue=True)
    state.db.commit()
    settings.markets = [selected]
    first = plan(settings, state)
    assert first["active_markets"] == [selected]
    assert state.db.execute("SELECT count(*) FROM jobs WHERE market=?", (other,)).fetchone()[0] == 0
    assert state.db.execute("SELECT count(*) FROM jobs WHERE dataset='history'").fetchone()[0] == 4
    settings.markets = []
    plan(settings, state)
    all_count = sum(state.counts().values())
    settings.markets = [selected]
    result = plan(settings, state)
    assert result["added"] == 0
    assert sum(state.counts().values()) == all_count
    assert sum(result["jobs"].values()) < all_count
    assert result["estimate_seconds_at_current_limit"] == sum(result["jobs"].values())


def test_new_catalog_references_keep_selected_market(state, settings):
    from ahledger.models import JobSpec
    from ahledger.parsers import Parsed
    from ahledger.planner import absorb

    selected = "forever.pvp.horde.us"
    state.set("active_markets", [selected, "forever.normal.alliance.us"])
    settings.markets = [selected]
    absorb(
        settings,
        state,
        JobSpec(dataset="catalog", url="https://ahledger.com/api/catalog"),
        Parsed(data={}, items={99999: {"id": 99999}}),
    )
    rows = list(state.db.execute("SELECT market FROM jobs WHERE dataset='history'"))
    assert len(rows) == 4 and {row[0] for row in rows} == {selected}


def test_cli_market_and_rate_override_config_without_network(tmp_path):
    root = tmp_path / "archive"
    config = tmp_path / "settings.toml"
    config.write_text('requests_per_second = 1.0\nmarkets = ["forever.normal.alliance.us"]\n')
    state = State(root)
    state.set("active_markets", ["forever.pvp.horde.us", "forever.normal.alliance.us"])
    state.candidates({2589: {"id": 2589}}, catalogue=True)
    state.db.commit()
    state.close()
    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config),
            "--data-dir",
            str(root),
            "--market",
            "forever.pvp.horde.us",
            "--requests-per-second",
            "2",
            "--tools-per-second",
            "0.75",
            "download",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    assert output["network"] is False
    assert output["selected_markets"] == ["forever.pvp.horde.us"]
    assert output["history_jobs_upper_bound"] == 4
    assert output["requests_per_second"] == 2
    assert output["tools_per_second"] == 0.75
    assert config.read_text().startswith("requests_per_second = 1.0")


def test_cli_rejects_rate_above_limit_before_opening_archive(tmp_path):
    root = tmp_path / "unused"
    result = CliRunner().invoke(
        app, ["--data-dir", str(root), "--requests-per-second", "6", "download", "--dry-run"]
    )
    assert result.exit_code == 2
    assert not root.exists()


def test_planner_rejects_unknown_market_id(state, settings):
    import pytest

    state.set("markets", [{"id": "forever.pvp.horde.us"}])
    settings.markets = ["typo"]
    with pytest.raises(ValueError, match="Unknown market IDs"):
        plan(settings, state)
    assert state.counts() == {}
