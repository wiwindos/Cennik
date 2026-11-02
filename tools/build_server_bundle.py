"""Allowlisted deployment ZIP: no databases, profiles, credentials or research."""
import hashlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]


def main():
    output = ROOT / "dist/CennikServer.zip"
    output.parent.mkdir(exist_ok=True)
    files = [ROOT / name for name in ("pyproject.toml", "requirements.server.lock.txt", "README.md", "docs/CENNIK_SERVER.md",
        "docs/CENNIK_WORKER.md", "docs/CENNIK_RELEASE_SETUP.md", "docs/CENNIK_COLLECTOR_APP.md", "docs/CENNIK_VERIFICATION.md",
        "docs/CENNIK_COLLECTORS.md", "docs/CENNIK_AUCTIONEERS.md", "docs/CENNIK_BULK.md",
        "docs/CENNIK_ACCOUNTING.md", "docs/CENNIK_PRODUCTION.md", "docs/CENNIK_CRAFT_GRAPH.md", "docs/CENNIK_PROFESSION_LEVELING.md", "deploy/cennik-server.service",
        "deploy/cennik-server.env.example", "deploy/CennikServer.Caddyfile", "deploy/CennikServer.IP.Caddyfile",
        "deploy/cennik-web.service", "docs/CENNIK_VPS.md", "tools/benchmark_server.py")]
    files.extend(p for p in (ROOT / "src").rglob("*") if p.is_file() and p.suffix in {".py", ".json", ".js", ".css", ".html", ".txt", ".md"} and "__pycache__" not in p.parts and not any(part.endswith('.egg-info') for part in p.parts))
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path in sorted(set(files)):
            archive.write(path, path.relative_to(ROOT).as_posix())
    (ROOT / "dist/CennikServer.sha256").write_text(hashlib.sha256(output.read_bytes()).hexdigest()+"  CennikServer.zip\n", "ascii")
    print(str(output))


if __name__ == "__main__":
    main()
