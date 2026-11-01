import argparse
import os
from pathlib import Path

from ahledger.util import atomic_json, exclusive_lock
from cennik.collector.desktop import CollectorWindow

from .agent import WorkerAgent


def main():
    parser = argparse.ArgumentParser(description="Cennik Worker: мост игровых окон с полным сервером")
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("LOCALAPPDATA", Path.home())) / "CennikWorker")
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--smoke-test", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        with exclusive_lock(args.root):
            agent = WorkerAgent(args.root)
            app = CollectorWindow(agent, background=args.background, worker=True)
            if args.smoke_test:
                app.root.after(16000, lambda: app.smoke(args.smoke_test))
            app.root.mainloop()
    except RuntimeError as error:
        if str(error) != "Another writer is using this archive":
            raise
        atomic_json(args.root / "show.request", {"show": True})


if __name__ == "__main__":
    main()
