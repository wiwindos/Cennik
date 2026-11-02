"""Independent bounded schedules, persisted failures and last known good references."""

import time

from ahledger.util import atomic_json

from .models import RefreshRequest


class SourceUpdates:
    def __init__(self, workspace):
        self.w = workspace
        self.path = workspace.root / "references/update_status.json"
        self.ready_at = time.monotonic() + 60

    def status(self):
        import json

        return json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}

    def run(self, kind, *, force=False):
        status = self.status()
        status[kind] = {
            **status.get(kind, {}),
            "attempted_at": int(time.time()),
            "state": "running",
        }
        atomic_json(self.path, status)
        try:
            if kind == "fyi":
                previous = self.w.npc_catalog.snapshot()
                document = self.w.sources.fyi_document(previous, force=force)
                result = (
                    self.w.npc_catalog.install(document)
                    if document.sha256 != previous.sha256
                    else {"unchanged": True}
                )
                if self.w.settings().auto_recalculate and document.sha256 != previous.sha256:
                    self.w.submit("analytics", self.w.calculate, defer=True)
            elif kind == "references":
                result = self.w.refresh(RefreshRequest(dataset="references"))
            elif kind == "history":
                rules = self.w.sniper.listing(wire=True)["rules"]
                identities = {r["item_id"] for r in rules}
                for package in self.w.exchange.packages(self.w.market_key):
                    for task in package.tasks:
                        identities.add(task.item_id)
                # A single maintenance job must not hold the private exchange queue.
                batch = sorted(identities)
                cursor = status.get(kind, {}).get("cursor", 0)
                selected = (batch + batch)[cursor : cursor + min(5, len(batch))] if batch else []
                result = {
                    "items": selected,
                    "responses": [
                        self.w.refresh(RefreshRequest(dataset="history", item_id=i))
                        for i in selected
                    ],
                }
                status[kind]["cursor"] = (cursor + len(selected)) % len(batch) if batch else 0
            else:
                raise ValueError("Неизвестный источник")
            status[kind].update(state="ok", completed_at=int(time.time()), error=None)
            return result
        except Exception as error:
            status[kind].update(
                state="error",
                error=type(error).__name__,
                message=str(error)[:400]
                if isinstance(error, (ValueError, RuntimeError))
                else "Источник не обновлён; прежние данные сохранены",
            )
            raise
        finally:
            atomic_json(self.path, status)

    def due(self):
        if time.monotonic() < self.ready_at:
            return None
        settings = self.w.settings()
        ahl_allowed = True
        try:
            if self.w.updates_guard:
                self.w.updates_guard()
        except RuntimeError:
            ahl_allowed = False
        status = self.status()
        for kind, enabled, interval in [
            ("fyi", settings.auto_fyi, 3600),
            (
                "references",
                settings.auto_refresh
                and settings.auto_references
                and not settings.development_mode
                and not settings.own_data_only,
                3600,
            ),
            (
                "history",
                settings.auto_refresh
                and settings.auto_history
                and not settings.development_mode
                and not settings.own_data_only,
                600,
            ),
        ]:
            if kind != "fyi" and not self.w.server_mode and not settings.auto_refresh:
                continue
            previous = status.get(kind, {})
            delay = max(1800, interval) if previous.get("state") in {"error", "running"} else interval
            if (
                enabled
                and (kind == "fyi" or ahl_allowed)
                and time.time() - previous.get("attempted_at", 0) >= delay
            ):
                return kind
        return None
