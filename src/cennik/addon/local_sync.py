"""Local page-to-addon exchange using the existing Bridge state and protocol."""

import json
from pathlib import Path

from cennik.addon.bridge import Bridge
from cennik.addon.service import AddonExchange


class LocalBridge:
    def __init__(self, root: Path, exchange: AddonExchange):
        self.root = root
        self.exchange = exchange
        self.config_path = root / "bridge/local.json"

    def paths(self) -> tuple[Path, Path]:
        if not self.config_path.is_file():
            raise ValueError("Не настроен путь к установленной игре (bridge/local.json).")
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        game = Path(config["game_root"])
        account = config["account"]
        if not game.is_absolute() or not isinstance(account, str) or not account or any(
            c in account for c in '/\\:<>"|?*'
        ) or account in (".", ".."):
            raise ValueError("Неверный путь игры или имя аккаунта в bridge/local.json.")
        addon = game / "Interface/AddOns/CennikAddon"
        if not (addon / "CennikAddon.toc").is_file():
            raise ValueError("В настроенной папке игры не найден CennikAddon.toc.")
        return addon, game / "WTF/Account" / account / "SavedVariables/CennikAddon.lua"

    def status(self) -> dict:
        try:
            addon, saved = self.paths()
            return {"available": True, "ready": True, "addon_dir": str(addon),
                    "saved_variables": str(saved), "saved_variables_present": saved.is_file()}
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return {"available": True, "ready": False, "error": str(exc)}

    def synchronize(self) -> dict:
        addon, saved = self.paths()
        market = self.exchange.store.settings().active_market
        if not market:
            raise ValueError("Выберите активный рынок перед синхронизацией.")
        bridge = Bridge("http://127.0.0.1:8765", addon, saved, market,
                        self.root / "bridge/state.json", exchange=self.exchange)
        try:
            return bridge.synchronize()
        finally:
            bridge.close()
