"""Update only Cennik-owned addon files, retaining imports and a rollback ZIP."""
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from .data import atomic_bytes


def install_addon(game_root: str, backup_root: Path) -> dict:
    game = Path(game_root).resolve(strict=True)
    source = Path(__file__).resolve().parents[3] / "CennikAddon"
    if not (source / "CennikAddon.toc").is_file():
        raise ValueError("Исходники аддона не найдены рядом с проектом")
    addons = (game / "Interface/AddOns").resolve(strict=True)
    if not (addons / "Auctionator").is_dir():
        raise ValueError("В выбранной игре не найден Auctionator")
    destination = (addons / "CennikAddon").resolve()
    if not destination.is_relative_to(addons):
        raise ValueError("Папка аддона выходит за пределы AddOns")
    toc = destination / "CennikAddon.toc"
    if destination.exists() and (not toc.is_file() or "## Title: Cennik" not in toc.read_text("utf-8")):
        raise ValueError("Папка CennikAddon не содержит распознанный аддон; автоматическая замена запрещена")
    files = ["CennikAddon.toc"] + [line.strip().replace("\\", "/") for line in
        (source / "CennikAddon.toc").read_text("utf-8").splitlines() if line.strip() and not line.startswith("#")]
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = backup_root / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".zip")
    with ZipFile(backup, "w", ZIP_DEFLATED) as archive:
        for name in files:
            path = (destination / name).resolve()
            if not path.is_relative_to(destination):
                raise ValueError("Неверный путь файла аддона")
            if path.is_file():
                archive.write(path, name)
    destination.mkdir(exist_ok=True)
    installed = []
    for name in files:
        path = destination / name
        if name == "ImportedData.lua" and path.exists():
            continue
        atomic_bytes(path, (source / name).read_bytes())
        installed.append(name)
    return {"addon_dir": str(destination), "backup": str(backup), "files_updated": installed,
            "note": "Auctionator и AHledger не изменялись. Полностью перезапустите игру, чтобы загрузился новый файл из TOC."}
