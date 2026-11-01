"""Explicit owner approvals for the fake auctioneer in the Lua test world."""
import time

from ahledger.util import atomic_json
from cennik.collector.auctioneers import AuctioneerProposal, Review, validate_snapshot


def proposal(kind="ordinary"):
    return AuctioneerProposal(npc_id=12345, npc_name="FixtureAuctioneer", realm="FixtureRealm",
        map_id=84, map_name="FixtureCity", x=50, y=60, proposed_type=kind,
        observed_at=int(time.time())-30, build="fixture")


def registry(kind="ordinary"):
    p = proposal(kind)
    return {"format_version": 1, "version": "fixture", "fetched_at": int(time.time()), "entries": [
        {"scope_id": p.scope_id(), "revision": 1, "approved": True, "auction_type": kind,
            "data": p.model_dump(mode="json"), "reviewed_at": int(time.time())}]}


def context(kind="ordinary"):
    p = proposal(kind)
    return {"scope_id": p.scope_id(), "revision": 1, "auction_type": kind,
        "npc_id": p.npc_id, "realm": p.realm, "map_id": p.map_id, "x": p.x, "y": p.y,
        "session_id": "fixture:auction:1"}


def approve_hub(hub, kind="ordinary"):
    from cennik.collector.protocol import Observation
    p = proposal(kind)
    record = Observation(kind="auctioneer_proposal", data=p.model_dump(mode="json"))
    hub.auctioneers.suggest(record.identity(), record.data, "fixture")
    hub.auctioneers.review(record.identity(), Review(action="approve", auction_type=kind))
    return validate_snapshot(hub.auctioneers.wire())


def approve_workspace(workspace):
    atomic_json(workspace.root / "collectors/auctioneer_registry.json", registry())
