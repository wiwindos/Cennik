"""Prepare a manual addon update folder, preserving the game's existing import."""
import argparse
import hashlib
import json
import shutil
from pathlib import Path

from cennik.workspace.install import addon_files

ROOT = Path(__file__).resolve().parents[1]


def stage(output):
    output = output.resolve()
    if output.exists() or not output.is_relative_to(ROOT / "release"):
        raise ValueError("Нужна новая папка внутри release/")
    source = ROOT / "CennikAddon"
    files = addon_files(source)
    version = next(line.split(":", 1)[1].strip() for line in (source / "CennikAddon.toc").read_text("utf-8").splitlines() if line.startswith("## Version:"))
    output.mkdir(parents=True)
    for name in files:
        if name == "ImportedData.lua":
            continue
        target = output / "CennikAddon" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
    (output / "README.txt").write_text(
        f"CennikAddon {version} — обновление аддона\n\n"
        "1. Полностью закройте все окна WoW.\n"
        "2. Скопируйте папку CennikAddon отсюда в _forever_/Interface/AddOns/ ПОВЕРХ существующей, с заменой файлов.\n"
        "3. Не удаляйте старую папку перед копированием: ImportedData.lua намеренно не включён, чтобы сохранить подключение.\n"
        f"4. Запустите игру и приложение обычным способом. В отчёте /cennik diagnostics должна быть версия {version}.\n\n"
        "Настройки, цели снайпера и игровые сохранения сохраняются. Файлы в WTF менять не нужно.\n"
        "Пакет предназначен для обновления уже установленного CennikAddon.\n"
        "Используйте актуальный Worker/Collector: старый EXE при установке может вернуть встроенную в него старую версию аддона.\n"
        "В этом случае снова скопируйте эту папку поверх и перезапустите игру.\n",
        "utf-8",
    )
    hashes = {p.relative_to(output).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.rglob("*")) if p.is_file()}
    (output / "SHA256SUMS.json").write_text(json.dumps(hashes, indent=2) + "\n", "utf-8")
    return {"folder": str(output), "version": version, "files": len(hashes), "import_preserved": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(stage(parser.parse_args().output), ensure_ascii=False))
