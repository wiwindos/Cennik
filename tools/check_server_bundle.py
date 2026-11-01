"""Audit deployment artifacts and start the server from the actual extracted ZIP."""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    output = parser.parse_args().output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    for name in ("CennikServer.zip", "CennikWorker.exe", "CennikCollector.exe"):
        file = ROOT / "dist" / name
        expected = file.with_suffix(".sha256").read_text("ascii").split()[0]
        assert hashlib.sha256(file.read_bytes()).hexdigest() == expected, name
    with ZipFile(ROOT / "dist/CennikServer.zip") as archive:
        names = archive.namelist()
        allowed = {"src", "docs", "deploy", "tools"}
        for name in names:
            path = Path(name)
            assert not path.is_absolute() and ".." not in path.parts
            assert path.parts[0] in allowed or name in {"README.md", "pyproject.toml", "requirements.server.lock.txt"}, name
            assert path.suffix not in {".duckdb", ".sqlite", ".zst", ".parquet"}, name
        for name in ("src/cennik/server/app.py", "src/ahledger/static/server.html",
                     "src/ahledger/static/next.js", "deploy/cennik-server.service"):
            assert name in names
        archive.extractall(output / "unpacked")
    script = '''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from fastapi.testclient import TestClient
import cennik.server.app as module
assert Path(module.__file__).is_relative_to(Path(sys.argv[1]))
key = "bundle-smoke-"+"x"*40
app = module.create_server(Path(sys.argv[2]), key, public_origin="http://127.0.0.1")
with TestClient(app, base_url="http://127.0.0.1") as http:
    assert http.get("/healthz").json()["status"] == "ok"
    assert http.get("/").status_code == 200
    assert http.get("/static/server.js").status_code == 200
    state = http.get("/server/api/state", headers={"X-Owner-Key": key}).json()
    assert len(state["profiles"]) == 1
    profile = state["profiles"][0]
    result = http.get("/m/"+profile["id"]+"/api/v2/overview", headers={"X-Owner-Key": key})
    assert result.status_code == 200, result.text
    assert result.json()["server_mode"] is True
    assert result.json()["market"].endswith(".alliance.eu")
print(json.dumps({"zip_server": "passed", "source": str(Path(module.__file__).parent)}))
'''
    subprocess.run([sys.executable, "-I", "-c", script,
                    str(output / "unpacked/src"), str(output / "profile")], check=True)
    result = {"archive_files": len(names), "sha256": "passed", "zip_server": "passed"}
    (output / "result.json").write_text(json.dumps(result), "utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
