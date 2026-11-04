"""Create a reviewable deployment folder; no archive, private DB or credentials."""
import argparse
import difflib
import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HUB_FILES = (
    "marketwatch/app.py", "marketwatch/config.py", "marketwatch/modules.py",
    "marketwatch/cennik.py", "pyproject.toml", "requirements-server.txt",
    "deploy/compose.cennik.yaml", "deploy/nginx-ip.conf", "deploy/nginx-webbox.conf",
)
LEGACY = {"ahledger/web.py", "ahledger/web_control.py"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def base_hub(name):
    result = subprocess.run(["git", "-C", str(ROOT/"funpayparsers"), "show", "HEAD:"+name],
                            capture_output=True, check=False)
    return result.stdout.replace(b"\r\n", b"\n") if result.returncode == 0 else None


def stage(destination):
    destination = destination.resolve()
    if not destination.is_relative_to(ROOT/"release") or destination.exists():
        raise ValueError("Нужен новый каталог внутри release/")
    seed = ROOT/"release-seed"
    from cennik.server.prepare import validate_package
    manifest, _, _, _ = validate_package(seed)
    destination.mkdir(parents=True)
    server = destination/"cennik"
    for source in sorted((ROOT/"src").rglob("*")):
        relative = source.relative_to(ROOT/"src")
        if source.is_file() and source.suffix in {".py", ".html", ".css", ".js", ".json", ".txt", ".md"} and "__pycache__" not in relative.parts and relative.as_posix() not in LEGACY:
            copy(source, server/"src"/relative)
    for name in ("pyproject.toml", "requirements.server.lock.txt", ".dockerignore",
                 "deploy/Cennik.Dockerfile", "deploy/hub-cennik.env.example"):
        copy(ROOT/name, server/name)
    for source in seed.iterdir():
        if source.is_file() and source.suffix == ".json":
            copy(source, server/"release-seed"/source.name)
    copy(ROOT/"docs/CENNIK_HUB_RELEASE.md", destination/"DEPLOY.md")
    quick = (ROOT/"docs/CENNIK_FOREVER_QUICKSTART.md").read_text("utf-8")
    (destination/"QUICKSTART.md").write_text(quick.replace("CENNIK_HUB_RELEASE.md", "DEPLOY.md").replace("CENNIK_FOREVER_VERIFIED.md", "VERIFIED.md"), "utf-8")
    copy(ROOT/"docs/CENNIK_FOREVER_VERIFIED.md", destination/"VERIFIED.md")
    copy(ROOT/"docs/CENNIK_GAME_NAMES.md", destination/"GAME_NAMES.md")
    copy(ROOT/"docs/CENNIK_CLIENT_QUICKSTART.md", destination/"CLIENT_QUICKSTART.md")
    copy(ROOT/"deploy/hub-cennik.env.example", destination/"hub-cennik.env.example")
    integration = destination/"hub-integration"
    changes, patch = {}, []
    for name in HUB_FILES:
        before = base_hub(name)
        after = (ROOT/"funpayparsers"/name).read_bytes().replace(b"\r\n", b"\n")
        changes[name] = {"before": sha(before) if before is not None else None, "after": sha(after)}
        target = integration/"files"/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(after)
        patch.extend(difflib.unified_diff((before or b"").decode().splitlines(True), after.decode().splitlines(True),
                     fromfile="a/"+name if before is not None else "/dev/null", tofile="b/"+name))
    (integration/"changes.json").write_text(json.dumps(changes, indent=2)+"\n", "utf-8")
    (integration/"hub-cennik.patch").write_text("".join(patch), "utf-8")
    copy(ROOT/"tools/apply_hub_cennik.py", integration/"apply.py")
    for product, folder in (("CennikWorker", "worker-0.6.0"), ("CennikCollector", "collector-0.6.0")):
        build = ROOT/"dist"/folder
        expected = (build/(product+".sha256")).read_text("ascii").split()[0]
        if sha((build/(product+".exe")).read_bytes()) != expected:
            raise ValueError("Повреждена сборка " + product)
        for suffix in (".exe", ".NOTICES.txt", ".sha256"):
            copy(build/(product+suffix), destination/"clients"/(product+suffix))
    addon = ROOT/"CennikAddon"
    for name in ["CennikAddon.toc"] + [line.strip() for line in (addon/"CennikAddon.toc").read_text("utf-8").splitlines() if line.strip() and not line.startswith("#")]:
        target = destination/"clients/CennikAddon"/name
        if name == "ImportedData.lua":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("CennikImport={format_version=1,packages={}}\n", "utf-8")
        else:
            copy(addon/name, target)
    release = {"format_version": 1, "prepared_at": datetime.now(UTC).isoformat(), "seed": manifest,
        "worker_version": "0.6.0", "collector_version": "0.6.0", "addon_version": "0.22.0",
        "archive": False, "server_modified": False, "hub_access": "administrator or cennik grant",
        "excluded_legacy_entrypoints": sorted(LEGACY)}
    (destination/"RELEASE.json").write_text(json.dumps(release, ensure_ascii=False, indent=2)+"\n", "utf-8")
    hashes = {p.relative_to(destination).as_posix(): sha(p.read_bytes()) for p in sorted(destination.rglob("*")) if p.is_file()}
    (destination/"SHA256SUMS.json").write_text(json.dumps(hashes, indent=2)+"\n", "utf-8")
    return {"folder": str(destination), "files": len(hashes)+1, "bytes": sum(p.stat().st_size for p in destination.rglob("*") if p.is_file()), "seed": manifest["version"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT/"release/forever-ready")
    print(json.dumps(stage(parser.parse_args().output), ensure_ascii=False))
