from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, Decimal)):
        dt = datetime.fromtimestamp(int(value), UTC)
    else:
        value = str(value)
        if len(value) == 10:
            value += "T00:00:00+00:00"
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError(f"Timestamp without timezone: {value}")
    return dt.astimezone(UTC).isoformat()


def dumps(value: Any) -> str:
    # Decimal strings preserve derived fractional copper without binary float conversion.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False)


def loads(body: bytes | str) -> Any:
    def reject_constant(value: str):
        raise ValueError(f"Non-finite JSON number: {value}")

    return json.loads(body, parse_float=Decimal, parse_constant=reject_constant)


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def copper(value: Any, *, estimated: bool = False) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Boolean is not a copper amount")
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Non-finite copper amount")
    if not estimated and (number != number.to_integral_value() or number < 0):
        raise ValueError(f"Invalid observed copper amount: {value}")
    integer = int(number.to_integral_value(rounding=ROUND_FLOOR))
    if not -(2**63) <= integer < 2**63:
        raise ValueError("Copper amount exceeds signed int64")
    return integer


def atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path: Path, value: Any) -> None:
    atomic_write(path, (dumps(value) + "\n").encode("utf-8"))


@contextmanager
def exclusive_lock(root: Path):
    """OS advisory lock survives process crashes without stale PID guesses."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".writer.lock").open("a+b") as stream:
        try:
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
        except OSError as exc:
            raise RuntimeError("Another writer is using this archive") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
