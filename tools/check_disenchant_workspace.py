"""Evaluate saved real evidence on a separate copy. Never mutates source DBs."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from cennik.workspace.models import MARKET
from cennik.workspace.service import Workspace


def hashes(directory):
    paths = [directory / name for name in ("market.duckdb", "market.duckdb.wal", "state.sqlite", "state.sqlite-wal", "workspace.json")]
    paths.extend((directory / "references").rglob("*.json"))
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths if path.exists()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--strategies", nargs="+", default=["buy_disenchant", "craft_disenchant"])
    args = parser.parse_args()
    source, output = args.source.resolve(strict=True), args.output.resolve()
    if output.exists() or not output.is_relative_to(Path.cwd().resolve() / "outputs"):
        raise ValueError("Choose a new directory inside this project's outputs folder")
    output.mkdir(parents=True)
    before = hashes(source)
    profile = output / "profile"
    profile.mkdir()
    for name in ("market.duckdb", "market.duckdb.wal", "workspace.json", "state.sqlite", "state.sqlite-wal"):
        if (source / name).exists():
            shutil.copy2(source / name, profile / name)
    quotes = source / "archive/quotes"
    for path in quotes.rglob("*.parquet"):
        target = profile / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    for path in (source / "references").rglob("*.json"):
        target = profile / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    after = hashes(source)
    assert after == before, "Source changed during snapshot: " + str([name for name in before.keys() | after.keys() if before.get(name) != after.get(name)])
    workspace = Workspace(profile)
    try:
        workspace.save_settings(workspace.settings().model_copy(update={"development_mode": True,
            "development_as_of": None, "auto_refresh": False, "auto_recalculate": False, "auto_import": False}))
        settings = workspace.store.settings()
        settings.config.enabled_strategies = args.strategies
        workspace.analytics.update_settings(settings)
        response = workspace.calculate()
        run = workspace.store.latest(MARKET)
        assert run is not None and run.status == "completed", response
        counts = {strategy: workspace.store.listing(MARKET, strategy=strategy, limit=3)["total"]
                  for strategy in settings.config.enabled_strategies}
        report = {"source_unchanged": hashes(source) == before, "run": run.model_dump(mode="json"),
                  "counts": counts, "source_hashes": before,
                  "examples": {strategy: workspace.store.listing(MARKET, strategy=strategy, limit=3)["items"]
                               for strategy in settings.config.enabled_strategies}}
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        assert report["source_unchanged"]
        print(json.dumps({"counts": counts, "source_unchanged": True, "rejections": run.diagnostics["rejections"],
                          "development_at": run.as_of.isoformat(), "report": str(output / "report.json")}, ensure_ascii=False))
    finally:
        workspace.close()


if __name__ == "__main__":
    main()
