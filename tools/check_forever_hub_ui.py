"""Two real HTTP services: existing hub login -> Cennik -> all workspace pages."""
import argparse
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import uvicorn

from ahledger.util import atomic_json
from cennik.core.models import Item, PriceObservation, Recipe
from cennik.server.app import create_server
from cennik.workspace.bootstrap import BootstrapDocument

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "funpayparsers"))


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    from marketwatch import auth
    from marketwatch.app import create_app
    from marketwatch.config import Config
    from marketwatch.db import Database

    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('node', 'playwright', 'browser'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    first, second = port(), port()
    origin = f'http://127.0.0.1:{first}'
    secret = 'hub-ui-fixture-'+'x'*40
    backend = create_server(args.output/'server', 'owner-ui-fixture-'+'x'*40, public_origin=origin, hub_secret=secret)
    profile = backend.state.catalog.profiles()[0]
    workspace = backend.state.catalog.workspace(profile.id)
    stamp = datetime.now(UTC) - timedelta(days=7)
    fixture = BootstrapDocument(version='hub-ui-fixture', created_at=datetime.now(UTC),
        source_market='forever.pvp.alliance.us',
        items={2589: Item(item_id=2589, name='Linen Cloth', vendor_sell_copper=13, observed_at=stamp)},
        prices={2589: PriceObservation(item_id=2589, market='forever.pvp.alliance.us', observed_at=stamp,
            granularity='scan', min_copper=10, median_copper=12, quantity=100, source='AHL1')},
        references={2589: 20}, recipes=[Recipe(spell_id=100, result_item_id=4238, qty_min=1, qty_max=1,
            version_hash='a'*64, profession='Tailoring', inputs=[{'item_id': 2589, 'quantity': 3}])])
    atomic_json(workspace.bootstrap.path, fixture.model_dump(mode='json'))
    workspace.market.names.put([('item', 2589, 'Льняная ткань')], 'ruRU', stamp=time.time())
    workspace.save_settings(workspace.settings().model_copy(update={'auto_refresh': False, 'auto_fyi': False,
        'use_beta_prices': True}))
    workspace.calculate()
    workspace.source_updates.ready_at = float('inf')
    workspace.recalculate_pending.clear()
    workspace.sources.transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        'markets': [{'id': 'forever.pvp.alliance.eu', 'label': 'Forever PvP EU'}]}))
    db = Database(args.output/'hub.sqlite3')
    db.initialize()
    auth.set_admin(db, 'owner', 'hub-ui-password-123')
    hub = create_app(Config(db_path=db.path, environment='development', collect=False, public_url=origin,
        cennik_url=f'http://127.0.0.1:{second}', cennik_secret=secret))
    servers, threads = [], []
    try:
        for app, number in ((backend, second), (hub, first)):
            server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=number, log_level='warning', proxy_headers=False))
            thread = threading.Thread(target=server.run)
            servers.append(server)
            threads.append(thread)
            thread.start()
            for _ in range(100):
                if server.started:
                    break
                time.sleep(.05)
            assert server.started
        subprocess.run([args.node, str(Path(__file__).with_suffix('.cjs')), args.playwright, args.browser,
            str(args.output.resolve()), origin, profile.id], check=True, timeout=240)
    finally:
        for server in reversed(servers):
            server.should_exit = True
        for thread in reversed(threads):
            thread.join(timeout=30)


if __name__ == '__main__':
    main()
