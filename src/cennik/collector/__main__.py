import argparse
import json
import os
from pathlib import Path

from .server import Hub, create_hub

parser = argparse.ArgumentParser(description="Cennik: отдельный архив сборщиков, без подключения рабочих БД")
parser.add_argument("action", choices=["serve", "invite"])
parser.add_argument("--root", type=Path, required=True)
parser.add_argument("--label", default="Сборщик")
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8768)
args = parser.parse_args()
if args.action == "invite":
    print(json.dumps(Hub(args.root).invite(args.label), ensure_ascii=False))
else:
    import uvicorn
    uvicorn.run(create_hub(args.root, os.environ.get("CENNIK_COLLECTOR_ADMIN_KEY", "")),
                host=args.host, port=args.port, proxy_headers=False, workers=1)
