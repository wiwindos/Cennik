"""Verify deployment contents and boot only the staged Cennik sources."""
import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check(folder):
    sys.dont_write_bytecode = True
    folder = folder.resolve()
    hashes = json.loads((folder/"SHA256SUMS.json").read_text("utf-8"))
    actual = {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}
    assert actual == set(hashes) | {"SHA256SUMS.json"}, "Unexpected or missing release files"
    forbidden_parts = {".git", ".env", "research", "__pycache__", "vps", "outputs", ".aws", ".codex"}
    for name, expected in hashes.items():
        path = folder/name
        assert not path.is_symlink() and path.resolve().is_relative_to(folder)
        assert not forbidden_parts.intersection(Path(name).parts), name
        assert not name.startswith("cennik/data/"), name
        assert path.suffix.lower() not in {".zip", ".duckdb", ".sqlite3", ".pem", ".key", ".pyc"}, name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, name
    package = folder/"hub-integration"
    spec = importlib.util.spec_from_file_location("release_hub_installer", package/"apply.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    changes = json.loads((package/"changes.json").read_text("utf-8"))
    with tempfile.TemporaryDirectory(prefix="cennik-release-check-") as temporary:
        temp = Path(temporary)
        hub = temp/"hub"
        hub.mkdir()
        (hub/"compose.yaml").write_text("services: {}\n", "utf-8")
        (hub/".env").write_text("PRIVATE_SENTINEL=preserved\n", "utf-8")
        for name, item in changes.items():
            if item["before"] is not None:
                data = subprocess.run(["git", "-C", str(ROOT/"funpayparsers"), "show", "HEAD:"+name], check=True, capture_output=True).stdout
                target = hub/name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        first_target = hub/"marketwatch/config.py"
        before = first_target.read_bytes()
        first_target.write_bytes(before+b"\n# unrelated production change\n")
        try:
            installer.apply(package, hub)
        except ValueError:
            pass
        else:
            raise AssertionError("Installer overwrote different hub version")
        assert not (hub/"marketwatch/cennik.py").exists(), "Preflight wrote some files"
        first_target.write_bytes(before)
        assert installer.apply(package, hub)["changed"] == len(changes)
        assert installer.apply(package, hub)["changed"] == 0
        assert (hub/".env").read_text() == "PRIVATE_SENTINEL=preserved\n"
        script = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from cennik.server.prepare import prepare
from cennik.server.app import create_server
from fastapi.testclient import TestClient
from cennik import workspace
state=Path(sys.argv[2])
prepared=prepare(state,Path(sys.argv[3]))
app=create_server(state,'release-check-owner-'+'x'*40)
with TestClient(app) as http:
    response=http.get('/healthz')
    assert response.status_code==200,response.text
    page=http.get('/login')
    assert page.status_code==200,page.text
assert Path(workspace.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())
print(json.dumps({'staged_sources':True,'health':True,'seed':prepared['seed']['version'],'opportunities':prepared['opportunities']}))
'''
        result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(folder/"cennik/src"),
            str(temp/"server"), str(folder/"cennik/release-seed")], check=True, capture_output=True, text=True, encoding="utf-8", timeout=180)
        boot = json.loads(result.stdout.splitlines()[-1])
    assert {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()} == actual, "Verification changed release contents"
    return {"files": len(actual), "sha256": True, "no_archives_or_private_data": True,
            "hub_preflight": True, "hub_idempotent": True, "boot": boot}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    print(json.dumps(check(parser.parse_args().folder)))
