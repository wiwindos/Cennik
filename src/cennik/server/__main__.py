import argparse
import os
from pathlib import Path

import uvicorn

from ahledger.util import exclusive_lock

from .app import create_server


def main():
    parser = argparse.ArgumentParser(description="Cennik: полный сервер аналитики и игровых клиентов")
    parser.add_argument("--root", type=Path, default=Path("data/cennik-server"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--origin", default="http://127.0.0.1:8770")
    args = parser.parse_args()
    key = os.environ.get("CENNIK_OWNER_KEY", "")
    with exclusive_lock(args.root):
        app = create_server(args.root, key, public_origin=args.origin)
        uvicorn.run(app, host=args.host, port=args.port, proxy_headers=False, workers=1)


if __name__ == "__main__":
    main()
