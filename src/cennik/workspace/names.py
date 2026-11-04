"""Locale-tagged presentation evidence. Never changes market facts or calculation IDs."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

LOCALES = {"ruRU", "enUS", "enGB"}


def locale_key(value):
    return "enUS" if value in {"enUS", "enGB"} else "ruRU" if value == "ruRU" else None


class GameNames:
    def __init__(self, root):
        self.path = root / "state.sqlite"
        self.lock = threading.RLock()
        reference = Path(__file__).resolve().parents[1] / "reference/game_names.json"
        self.reference = json.loads(reference.read_text("utf-8")) if reference.exists() else {}
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS game_names (
                    kind TEXT,identity INTEGER,locale TEXT,name TEXT,source TEXT,priority INTEGER,stamp REAL,
                    PRIMARY KEY(kind,identity,locale));
                CREATE TABLE IF NOT EXISTS game_names_meta(key TEXT PRIMARY KEY,value TEXT);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, rows, locale, *, source="client", stamp=0):
        locale = locale_key(locale)
        if not locale:
            return 0  # Old observations have no trustworthy language tag.
        if isinstance(stamp, str):
            stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        if not isinstance(stamp, (int, float)):
            return 0
        priority = {"beta": 1, "ahl": 2, "client": 3}[source]
        valid = {}
        for kind, identity, name in rows:
            if kind not in {"item", "recipe", "npc"} or type(identity) is not int or not 0 < identity < 2**31:
                continue
            if not isinstance(name, str) or not name.strip() or len(name) > 300 or re.search(r"[\x00-\x1f\x7f|]", name):
                continue
            valid[kind, identity] = (kind, identity, locale, name.strip(), source, priority, stamp)
        with self.lock, self.connect() as db:
            before = db.total_changes
            db.executemany("""INSERT INTO game_names VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(kind,identity,locale) DO UPDATE SET name=excluded.name,source=excluded.source,
                priority=excluded.priority,stamp=excluded.stamp
                WHERE (excluded.stamp>game_names.stamp OR
                    (excluded.stamp=game_names.stamp AND excluded.priority>=game_names.priority))
                    AND (excluded.name<>game_names.name OR excluded.priority<>game_names.priority
                         OR excluded.stamp>game_names.stamp)""", valid.values())
            changed = db.total_changes - before
            if changed:
                db.execute("INSERT INTO game_names_meta VALUES('revision','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")
        return changed

    def seed(self, document):
        if not document:
            return
        key = "beta:" + document.version + ":" + self.reference.get("version", "empty")
        with self.connect() as db:
            if db.execute("SELECT 1 FROM game_names_meta WHERE key=?", [key]).fetchone():
                return
        # Only this explicitly English reference is migrated. Mixed legacy DB names stay unclassified.
        rows = [("item", i, item.name) for i, item in document.items.items()
                if item.source in {"item", "catalog", "crafting", "pricetable", "disenchant"}]
        rows += [("recipe", r.spell_id, r.name) for r in document.recipes
                 if not (r.raw_id or "").startswith("observed-recipe-")]
        rows += [(kind, int(identity), name) for kind, values in self.reference.get("names", {}).items()
                 for identity, name in values.items()]
        self.put(rows, "enUS", source="beta")
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO game_names_meta VALUES(?, '1')", [key])

    def api(self, tables, stamp):
        rows = [("item", r.get("item_id"), r.get("name")) for r in tables.get("items", [])]
        rows += [("recipe", r.get("spell_id"), r.get("name")) for r in tables.get("recipes", [])]
        return self.put(rows, "enUS", source="ahl", stamp=stamp)

    def scan(self, scan):
        variants = scan.get("variants") or []
        rows = {}
        for line in (scan.get("data") or "").split(";"):
            try:
                fields = line.split(":")
                item, index = int(fields[0]), int(fields[5]) - 1
                if 0 <= index < len(variants):
                    variant = variants[index]
                    link = str(variant.get("link") or "").split("item:")[-1].split(":")
                    suffix = len(link) > 6 and link[6] not in {"", "0"}
                    name = variant.get("base_name") or (None if suffix else variant.get("name"))
                    if name:
                        rows[item] = ("item", item, name)
            except (ValueError, IndexError, TypeError):
                continue
        return self.put(rows.values(), scan.get("locale"), stamp=scan.get("observed_at", 0))

    def recipe(self, value, locale=None, stamp=0):
        rows = [("recipe", value.get("spell_id"), value.get("name")),
                ("item", value.get("item_id"), value.get("item_name"))]
        rows += [("item", int(i), name) for i, name in (value.get("item_names") or {}).items() if str(i).isdigit()]
        return self.put(rows, value.get("locale", locale), stamp=value.get("observed_at", stamp))

    def npc(self, value):
        return self.put([("npc", value.get("npc_id"), value.get("npc_name")),
                         ("item", value.get("item_id"), value.get("item_name", value.get("name")))],
                        value.get("locale"), stamp=value.get("observed_at", 0))

    def saved(self, saved):
        for scan in saved.get("market_scans") or []:
            self.scan(scan)
        for field in ("npc_quotes", "collector_npc"):
            for identity, value in (saved.get(field) or {}).items():
                item = value.get("item_id") or str(identity).rsplit(":", 1)[-1]
                self.npc({**value, "item_id": int(item) if str(item).isdigit() else None})
        reports = [saved.get("collector_recipes") or {}, (saved.get("production") or {}).get("recipes") or {}]
        for report in reports:
            for value in (report.get("recipes") or {}).values():
                self.recipe(value, report.get("locale"), report.get("observed_at", 0))
        snapshot = (saved.get("production") or {}).get("profession_snapshot") or {}
        for state in (snapshot.get("professions") or {}).values():
            for value in (state.get("recipes") or {}).values():
                self.recipe(value, stamp=snapshot.get("observed_at", 0))

    def search(self, query, limit):
        with self.connect() as db:
            # Python casefold handles Cyrillic consistently across SQLite builds.
            rows = db.execute("SELECT identity,name FROM game_names WHERE kind='item'")
            result = set()
            for identity, name in rows:
                if query.casefold() in name.casefold() or str(identity) == query:
                    result.add(identity)
                    if len(result) >= limit:
                        break
        return sorted(result)

    def document(self, revision=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM game_names_meta WHERE key='revision'").fetchone()
            current = row[0] if row else "0"
            result = {"revision": current, "unchanged": revision == current}
            if revision != current:
                names = {"item": {}, "recipe": {}, "npc": {}}
                for kind, identity, locale, name in db.execute("SELECT kind,identity,locale,name FROM game_names"):
                    names[kind].setdefault(str(identity), {})[locale] = name
                result["names"] = names
        return result
