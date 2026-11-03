"""Explicit offline test reset and validated Forever seed installation. No backup."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from ahledger.util import atomic_json, atomic_write, exclusive_lock
from cennik.engines.disenchant import DisenchantRules
from cennik.engines.npc_catalog import NpcPriceDocument
from cennik.workspace.bootstrap import BootstrapDocument

from .catalog import Catalog, MarketProfile

PROFILE_ID = uuid5(NAMESPACE_URL, "cennik/forever/pvp/alliance/eu/release").hex
RESET_PATHS = ("markets", "markets.json", "workers", "pairing", "collector-archive", "forwarding")


def validate_package(package):
    manifest = json.loads((package / "manifest.json").read_text("utf-8"))
    expected = {"baseline.json", "npc_catalog.json", "disenchant.json"}
    if manifest.get("format_version") != 1 or set(manifest.get("files", {})) != expected:
        raise ValueError("Неполный манифест стартового пакета")
    for name, digest in manifest["files"].items():
        path = package / name
        if path.is_symlink() or path.stat().st_size > 64_000_000 or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Повреждён стартовый файл: " + name)
    baseline = BootstrapDocument.model_validate_json((package / "baseline.json").read_bytes())
    if manifest.get("version") != baseline.version:
        raise ValueError("Версия стартового пакета не совпадает с манифестом")
    npc = NpcPriceDocument.model_validate_json((package / "npc_catalog.json").read_bytes())
    rules = DisenchantRules.model_validate_json((package / "disenchant.json").read_bytes())
    return manifest, baseline, npc, rules


def prepare(root, package, *, reset=False, confirmation=""):
    if root.is_symlink():
        raise ValueError("Папка серверных данных не должна быть символической ссылкой")
    root, package = root.resolve(), package.resolve()
    manifest, baseline, npc, rules = validate_package(package)
    if root == Path(root.anchor) or len(root.parts) < 2 or package.is_relative_to(root):
        raise ValueError("Нужна отдельная папка серверных данных и внешний стартовый пакет")
    if (root / "workspace.json").exists() or (root / "market.duckdb").exists():
        raise ValueError("Нельзя очищать локальное исследовательское пространство")
    if root.exists() and not (root / "markets.json").exists() and any(p.name not in {"upstreams", ".writer.lock", "release-ready.json"} for p in root.iterdir()):
        raise ValueError("Папка не похожа на отдельный серверный профиль Cennik")
    root.mkdir(parents=True, exist_ok=True)
    targets = [root / name for name in RESET_PATHS]
    if any(p.is_symlink() or not p.resolve().is_relative_to(root) for p in targets):
        raise ValueError("Пути очистки выходят за пределы серверного профиля")
    if reset and confirmation != "RESET_TEST_DATA":
        raise ValueError("Для удаления тестовых данных нужен --confirm RESET_TEST_DATA")
    with exclusive_lock(root):
        if reset:
            for path in targets:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
        catalog = Catalog(root)
        try:
            if not reset and any((root / "markets").glob("*/workspace.json")):
                raise ValueError("Существующие рынки не заменяются без явного сброса тестовых данных")
            profile = MarketProfile(id=PROFILE_ID, label="Forever · PvP · Альянс · EU", realm_names=[])
            atomic_json(catalog.path, {"profiles": [profile.model_dump(mode="json")], "ahl_profile_id": profile.id})
            w = catalog.workspace(profile.id)
            atomic_write(w.bootstrap.path, baseline.model_dump_json().encode())
            w.npc_catalog.install(npc)
            w.analytics.disenchant.install(rules)
            w.save_settings(w.settings().model_copy(update={"auto_refresh": True, "auto_recalculate": True,
                "auto_fyi": True, "auto_references": True, "auto_history": True,
                "auto_disenchant_check": True, "use_beta_prices": True}))
            result = w.calculate()
            report = {"reset_test_data": reset, "backup_created": False, "profile": profile.public(),
                "seed": manifest, "opportunities": result.get("run", {}).get("signal_count"),
                "preserved": ["upstreams/credentials.json", "external owner key / server environment", "hub users and Goldscope data"]}
            atomic_json(root / "release-ready.json", report)
            return report
        finally:
            catalog.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--reset-test-data", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    print(json.dumps(prepare(args.root, args.package, reset=args.reset_test_data, confirmation=args.confirm), ensure_ascii=False))


if __name__ == "__main__":
    main()
