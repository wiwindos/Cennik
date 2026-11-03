"""Bounded source-change journal outside SQL; compare like datasets, not markets.

Timestamps and transport metadata never turn unchanged prices into changed items.
An omitted row means absent from a response, not deleted from our history.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time

from ahledger.util import atomic_json, dumps


def fingerprint(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def describe(spec):
    scope = {"dataset": spec.dataset, "market": spec.market, "item_id": spec.item_id, "params": spec.params}
    label = {"pricetable": "Цены AHL", "crafting": "Рецепты AHL", "history": "История AHL",
             "item": "Предмет AHL"}.get(spec.dataset, spec.dataset)
    if spec.params.get("profession"):
        label += " · " + str(spec.params["profession"])
    if spec.item_id:
        label += " · #" + str(spec.item_id)
    if spec.params.get("range"):
        label += " · " + str(spec.params["range"])
    return fingerprint(scope), label


def snapshot(spec, parsed):
    """Stable row fingerprints; avoid comparing generatedAt or craft valuations."""
    result = {}
    data = parsed.data
    if spec.dataset == "pricetable":
        for row in data.get("rows", []):
            identity = str(row["id"])
            values = {k: row.get(k) for k in ("median", "minBuyout", "quantity", "median7d", "median30d",
                                             "low30d", "high30d", "salesPerDayX10")}
            result[identity] = {"hash": fingerprint(values), "label": "#" + identity}
    elif spec.dataset == "crafting":
        for row in data.get("recipes", []):
            recipe, output = row["recipe"], row["result"]
            identity = str(recipe["spellId"])
            values = {"recipe": recipe,
                      "result": {k: v for k, v in output.items() if k not in {"url", "chartUrl"}},
                      "reagents": sorted(({k: v for k, v in r.items() if k != "price"}
                                          for r in row["reagents"]), key=lambda r: r["id"])}
            result[identity] = {"hash": fingerprint(values), "label": str(recipe.get("name") or identity)[:160]}
    elif spec.dataset == "history":
        for row in data.get("series", []):
            identity = str(row["t"])
            result[identity] = {"hash": fingerprint({k: v for k, v in row.items() if k != "t"}), "label": identity}
    else:
        return None
    return result


class SourceReports:
    def __init__(self, workspace):
        self.root = workspace.root / "source_reports"
        self.lock = threading.RLock()

    @property
    def path(self):
        return self.root / "journal.json"

    def read(self):
        return json.loads(self.path.read_text("utf-8")) if self.path.exists() else {"scopes": {}, "recent": []}

    def baseline(self, key):
        path = self.root / "baselines" / (key + ".json")
        return json.loads(path.read_text("utf-8")) if path.exists() else None

    def has_baseline(self, spec):
        return (self.root / "baselines" / (describe(spec)[0] + ".json")).exists()

    def seed(self, spec, parsed):
        with self.lock:
            key, _ = describe(spec)
            if not self.baseline(key):
                rows = snapshot(spec, parsed)
                if rows is not None:
                    atomic_json(self.root / "baselines" / (key + ".json"),
                                {"rows": rows, "complete": parsed.status == "COMPLETE", "data_at": parsed.maximum, "total": parsed.count})

    def _record(self, key, event):
        state = self.read()
        previous = state["scopes"].get(key, {})
        row = {**previous, "last": event}
        if event["state"] != "error":
            row["last_success"] = event
        if event["state"] in {"updated", "partial"} and any(event.get(k) for k in ("added", "changed", "removed", "metadata_changed")):
            row["last_change"] = event
        state["scopes"][key] = row
        if len(state["scopes"]) > 256:
            ordered = sorted(state["scopes"].items(), key=lambda pair: (
                pair[0] == key, pair[1]["last"]["checked_at"]), reverse=True)
            primary = [pair for pair in ordered if pair[1]["last"]["label"].startswith(("Цены AHL", "Рецепты AHL", "Каталог FYI"))]
            others = [pair for pair in ordered if pair not in primary]
            state["scopes"] = dict((primary + others)[:256])
        # Consecutive cache/unchanged checks should not push real changes out.
        if state["recent"] and state["recent"][0].get("scope") == key and event["state"] in {"cache", "unchanged"} and state["recent"][0]["state"] == event["state"]:
            state["recent"][0] = event
        else:
            state["recent"].insert(0, event)
        state["recent"] = state["recent"][:100]
        atomic_json(self.path, state)
        return event

    def _applied(self, key, label, rows, *, total, complete, data_at, metadata=None, metadata_changed=False, origin="network"):
        with self.lock:
            previous = self.baseline(key)
            old = previous["rows"] if previous else {}
            if rows is None:
                added = changed = removed = None
                samples = {}
            else:
                added_keys, changed_keys = rows.keys() - old.keys(), {k for k in rows.keys() & old.keys() if rows[k]["hash"] != old[k]["hash"]}
                removed_keys = old.keys() - rows.keys() if complete and (not previous or previous["complete"]) else None
                added, changed, removed = len(added_keys), len(changed_keys), len(removed_keys) if removed_keys is not None else None
                samples = {"added": [rows[k]["label"] for k in sorted(added_keys)[:5]],
                           "changed": [rows[k]["label"] for k in sorted(changed_keys)[:5]],
                           "removed": [old[k]["label"] for k in sorted(removed_keys or [])[:5]]}
                # A partial response preserves missing baseline rows and cannot
                # establish removals on the next full response either.
                baseline_rows = rows if complete else {**old, **rows}
                atomic_json(self.root / "baselines" / (key + ".json"),
                            {"rows": baseline_rows, "complete": complete, "data_at": data_at, "total": total})
            changed_values = bool(added or changed or removed or metadata_changed)
            event = {"scope": key, "label": label, "checked_at": int(time.time()), "data_at": data_at,
                     "state": "loaded" if rows is None else "partial" if not complete else "updated" if changed_values else "cache" if origin == "cache" else "unchanged", "origin": origin,
                     "total": total, "added": added, "changed": changed, "removed": removed,
                     "complete": complete, "first_comparison": previous is None,
                     "samples": samples, "metadata": metadata, "metadata_changed": metadata_changed}
            return self._record(key, event)

    def applied(self, spec, parsed):
        key, label = describe(spec)
        return self._applied(key, label, snapshot(spec, parsed), total=parsed.count,
                             complete=parsed.status == "COMPLETE", data_at=parsed.maximum)

    def checked(self, spec, *, cached=False):
        with self.lock:
            key, label = describe(spec)
            baseline = self.baseline(key) or {}
            return self._record(key, {"scope": key, "label": label, "checked_at": int(time.time()),
                "data_at": baseline.get("data_at"), "state": "cache" if cached else "unchanged",
                "origin": "cache" if cached else "not_modified", "total": baseline.get("total", len(baseline["rows"]) if "rows" in baseline else None),
                "added": 0, "changed": 0, "removed": 0, "complete": baseline.get("complete", False), "samples": {}})

    def failed(self, spec=None, *, message="Не удалось обновить источник; прежние данные сохранены"):
        with self.lock:
            key, label = describe(spec) if spec else ("fyi", "Каталог FYI")
            return self._record(key, {"scope": key, "label": label, "checked_at": int(time.time()),
                                      "state": "error", "message": message[:400]})

    def fyi(self, previous, document, *, origin="network"):
        def rows(doc):
            return {str(i): {"hash": fingerprint(v.model_dump(mode="json")), "label": v.name[:160] + " · #" + str(i)}
                    for i, v in doc.items.items()}
        with self.lock:
            # Compare the actually installed catalogue, including manual imports
            # made since the last automatic check.
            atomic_json(self.root / "baselines/fyi.json", {"rows": rows(previous), "complete": True,
                                                         "data_at": previous.retrieved_at.isoformat()})
            return self._applied("fyi", "Каталог FYI", rows(document), total=len(document.items), complete=True,
                                 data_at=document.retrieved_at.isoformat(),
                                 metadata={"version": document.version, "build": document.build, "phase": document.phase},
                                 metadata_changed=(previous.version, previous.build, previous.phase) != (document.version, document.build, document.phase), origin=origin)

    def overview(self):
        with self.lock:
            try:
                state = self.read()
                scopes = sorted(state["scopes"].values(), key=lambda r: r["last"]["checked_at"], reverse=True)
            except (OSError, ValueError, KeyError, TypeError):
                return {"scopes": [], "recent": [], "error": "Не удалось прочитать журнал обновлений; сами данные источников сохранены"}
        primary = [r for r in scopes if r["last"]["label"].startswith(("Цены AHL", "Рецепты AHL", "Каталог FYI"))]
        other = [r for r in scopes if r not in primary]
        return {"scopes": (primary + other)[:30], "recent": state.get("recent", [])[:20]}
