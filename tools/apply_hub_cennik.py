"""Apply only the prepared hub integration, after checking every target file."""
import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path


def normalized(data):
    return data.replace(b"\r\n", b"\n")


def digest(data):
    return hashlib.sha256(normalized(data)).hexdigest()


def apply(package, hub):
    package, hub = package.resolve(), hub.resolve(strict=True)
    if not (hub / "marketwatch/app.py").is_file() or not (hub / "compose.yaml").is_file():
        raise ValueError("Укажите корень существующего WIW Hub")
    changes = json.loads((package / "changes.json").read_text("utf-8"))
    writes = []
    for name, hashes in changes.items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Недопустимый путь: " + name)
        target = hub / relative
        source = package / "files" / relative
        if target.is_symlink() or not target.resolve().is_relative_to(hub):
            raise ValueError("Ссылка за пределы хаба: " + name)
        payload = source.read_bytes()
        if digest(payload) != hashes["after"]:
            raise ValueError("Повреждён файл выпуска: " + name)
        previous = digest(target.read_bytes()) if target.exists() else None
        if previous == hashes["after"]:
            continue
        if previous != hashes["before"]:
            raise ValueError("Версия хаба отличается, изменений не записано: " + name)
        writes.append((target, payload))
    # No mutation is attempted before the complete preflight has succeeded.
    for target, payload in writes:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".cennik-")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
            if target.exists():
                os.chmod(temporary, target.stat().st_mode & 0o777)
            else:
                os.chmod(temporary, 0o644)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return {"changed": len(writes), "already_current": len(changes) - len(writes)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(apply(Path(__file__).resolve().parent, args.hub)))
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
