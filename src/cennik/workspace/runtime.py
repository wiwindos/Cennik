"""Identify the code loaded by a local server without opening its databases."""
import hashlib
from pathlib import Path


def revision() -> str:
    package = Path(__file__).resolve().parents[1]
    static = package.parent / "ahledger/static"
    paths = sorted(package.rglob("*.py")) + sorted(p for p in static.glob("next*") if p.suffix in {".js", ".css", ".html"})
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(package.parent)).replace("\\", "/").encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
