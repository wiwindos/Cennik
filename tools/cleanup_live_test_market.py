"""Remove only the named disposable deployment fixture; restart Cennik in finally."""

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path("/var/lib/cennik-server")
COMPOSE = [
    "docker",
    "compose",
    "-f",
    "compose.yaml",
    "-f",
    "deploy/compose.webbox.yaml",
    "-f",
    "deploy/compose.cennik.yaml",
    "--profile",
    "ip",
]


def cleanup(identity):
    assert re.fullmatch("[a-f0-9]{32}", identity)
    assert identity != "91b1ea41e8235e73b823d87a6065b75c"
    assert not ROOT.is_symlink() and ROOT.resolve() == ROOT
    catalog = json.loads((ROOT / "markets.json").read_text())
    profile = next((p for p in catalog["profiles"] if p["id"] == identity), None)
    if profile is None:
        return {"already_absent": True}
    assert profile["label"] == "Проверка развёртывания · временный рынок"
    assert profile["client_family"] == "test" and profile["realm_names"] == ["DeploymentFixture"]
    assert catalog["ahl_profile_id"] != identity
    workspace = ROOT / "markets" / identity
    archive = ROOT / "collector-archive"
    members = set()
    internal = workspace / "collectors/internal_member.json"
    if internal.exists():
        members.add(json.loads(internal.read_text())["id"])
    for p in (archive / "members").glob("*.json"):
        value = json.loads(p.read_text())
        if value.get("profile_id") == identity:
            members.add(value["id"])
    paths = [workspace]
    for p in (ROOT / "workers").glob("*.json"):
        if json.loads(p.read_text()).get("profile_id") == identity:
            paths.extend([p, ROOT / "workers/exchanges" / p.stem])
    record_ids = set()
    for member in members:
        assert re.fullmatch("[a-f0-9]{32}", member)
        paths.extend([archive / "members" / (member + ".json"), archive / "receipts" / member])
        for receipt in (archive / "receipts" / member).glob("*.json"):
            record_ids.add(receipt.stem)
            raw = json.loads(receipt.read_text()).get("raw_path")
            if raw:
                paths.append(archive / raw)
    for p in (archive / "auctioneers/proposals").glob("*.json"):
        value = json.loads(p.read_text())
        if value.get("data", {}).get("server_id") == identity:
            paths.extend([p, archive / "auctioneers/reviews" / p.name])
    for p in (archive / "auctioneers/registry").glob("*.json"):
        if json.loads(p.read_text()).get("data", {}).get("server_id") == identity:
            paths.append(p)
    for record in record_ids:
        for kind in ["pending", "done", "failures"]:
            paths.append(ROOT / "forwarding" / kind / (record + ".json"))
    # Do not remove a content-addressed raw file also referenced by another member.
    others = set()
    for p in (archive / "receipts").glob("*/*.json"):
        if p.parent.name not in members:
            raw = json.loads(p.read_text()).get("raw_path")
            if raw:
                others.add((archive / raw).resolve())
    paths = [p for p in paths if p.resolve() not in others]
    for p in paths:
        assert not p.is_symlink() and p.resolve().is_relative_to(ROOT), str(p)
        assert p != ROOT and p != ROOT / "markets"
    subprocess.run(
        COMPOSE + ["stop", "cennik"], cwd="/opt/goldscope", check=True, capture_output=True
    )
    try:
        for p in paths:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink(missing_ok=True)
        # Re-read after stopping; never overwrite unrelated concurrent profile changes.
        current = json.loads((ROOT / "markets.json").read_text())
        assert current["ahl_profile_id"] != identity
        current["profiles"] = [p for p in current["profiles"] if p["id"] != identity]
        temporary = ROOT / "markets.json.deploy-tmp"
        temporary.write_text(json.dumps(current, ensure_ascii=False) + "\n")
        temporary.replace(ROOT / "markets.json")
    finally:
        subprocess.run(
            COMPOSE + ["up", "-d", "--wait", "--wait-timeout", "180", "cennik"],
            cwd="/opt/goldscope",
            check=True,
            capture_output=True,
        )
    assert all(
        p["id"] != identity for p in json.loads((ROOT / "markets.json").read_text())["profiles"]
    )
    return {
        "removed_test_profile": identity,
        "working_profile_preserved": True,
        "cennik_restarted": True,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    print(json.dumps(cleanup(parser.parse_args().profile)))
