"""Update only Cennik-owned addon files, retaining imports and a rollback ZIP."""
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from ahledger.util import atomic_write as atomic_bytes


def addon_files(source: Path) -> list[str]:
    """TOC load units and declared media; assets are not executed as Lua."""
    source = source.resolve()
    lines = (source / "CennikAddon.toc").read_text("utf-8").splitlines()
    files = ["CennikAddon.toc"]
    for line in lines:
        line = line.strip()
        if line.startswith("## X-Cennik-Assets:"):
            files.extend(name.strip().replace("\\", "/") for name in line.split(":", 1)[1].split(",") if name.strip())
        elif line and not line.startswith("#"):
            files.append(line.replace("\\", "/"))
    for name in files:
        path = source / name
        if not path.resolve().is_relative_to(source) or path.is_symlink() or not path.is_file():
            raise ValueError("Неверный или отсутствующий файл аддона: " + name)
    return list(dict.fromkeys(files))


def install_addon(game_root: str, backup_root: Path, *, source: Path | None = None) -> dict:
    game = Path(game_root).resolve(strict=True)
    source = source or Path(__file__).resolve().parents[3] / "CennikAddon"
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
    files = addon_files(source)
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
