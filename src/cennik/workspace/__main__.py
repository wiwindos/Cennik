import argparse
import json
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

from .web import create_app


def main():
    parser = argparse.ArgumentParser(description="Cennik: отдельная локальная база и постоянный архив")
    parser.add_argument("--root", type=Path, default=Path("data/cennik-next"))
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    url = f"http://127.0.0.1:{args.port}"
    try:
        with urllib.request.urlopen(url + "/healthz", timeout=1) as response:
            running = json.load(response)
        if running.get("workspace") != str(args.root.resolve()) or running.get("profile") != "cennik-next":
            raise RuntimeError("Порт занят другим приложением; выберите --port")
        if not args.no_browser:
            webbrowser.open(url)
        print("Cennik уже работает: " + url)
        return
    except urllib.error.URLError:
        pass
    app = create_app(args.root)

    def open_when_ready():
        for _ in range(60):
            try:
                with urllib.request.urlopen(url + "/healthz", timeout=1):
                    webbrowser.open(url)
                    return
            except urllib.error.URLError:
                time.sleep(0.25)

    if not args.no_browser:
        threading.Thread(target=open_when_ready, daemon=True).start()
    print(f"Cennik: {url}\nНовая база: {args.root.resolve()}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False)


if __name__ == "__main__":
    main()
