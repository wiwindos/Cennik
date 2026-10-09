"""One bounded, offline screenshot of the locally generated report."""

import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
if not edge.exists():
    edge = Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe")
if not edge.exists():
    raise SystemExit("Edge not installed; report HTML is still available")
report = root / "data/reports/download_report.html"
output = root / "data/reports/report-preview.png"
command = [
    str(edge),
    "--headless",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-component-update",
    "--window-size=1600,1050",
    f"--user-data-dir={root / 'data/render-profile'}",
    f"--screenshot={output}",
    report.as_uri(),
]
result = subprocess.run(command, timeout=20, capture_output=True)
if not output.exists():
    raise SystemExit(f"Screenshot failed, browser exit={result.returncode}")
print(output)
