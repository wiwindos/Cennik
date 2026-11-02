"""Build a standalone collector EXE; never bundle game saves or user profiles."""
import argparse
import hashlib
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help="Build the private auctioneer/crafter bridge")
    parser.add_argument("--output", type=Path, default=ROOT / "dist", help="Output folder; use a new folder while the old EXE is running")
    args = parser.parse_args()
    product_name = "CennikWorker" if args.worker else "CennikCollector"
    output = args.output.resolve()
    build = ROOT / "build" / ("worker" if args.worker else "collector")
    output.mkdir(parents=True, exist_ok=True)
    build.mkdir(parents=True, exist_ok=True)
    # Bundle only the addon files declared by its TOC, excluding live imports.
    addon = build / "payload/CennikAddon"
    addon.mkdir(parents=True, exist_ok=True)
    source = ROOT / "CennikAddon"
    files = ["CennikAddon.toc"]+[s.strip().replace("\\", "/") for s in (source / "CennikAddon.toc").read_text("utf-8").splitlines() if s and not s.startswith("#")]
    for stale in addon.rglob("*"):
        if stale.is_file() and stale.relative_to(addon).as_posix() not in files:
            if not stale.resolve().is_relative_to(addon.resolve()):
                raise ValueError("Unexpected path in addon build staging")
            stale.unlink()
    for name in files:
        target = addon / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if name == "ImportedData.lua":
            target.write_text("CennikImport={format_version=1,packages={}}\n", "utf-8")
        else:
            target.write_bytes((source / name).read_bytes())
    from cennik.collector.desktop import icon_image
    icon = build / "collector.ico"
    icon_image().save(icon, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    notices = [product_name+" — third-party notices\n"]
    for package_name in ("pystray", "Pillow", "httpx", "httpcore", "anyio", "pydantic", "pydantic_core", "annotated-types", "typing_extensions", "typing-inspection", "certifi", "idna", "h11", "six", "PyInstaller"):
        distribution = importlib.metadata.distribution(package_name)
        notices.append(f"\n{package_name} {distribution.version}\n")
        for file in distribution.files or []:
            if file.name.lower().startswith(("license", "copying")):
                notices.append(distribution.locate_file(file).read_text("utf-8", errors="replace"))
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.exists():
        notices.extend(["\nPython runtime\n", python_license.read_text("utf-8")])
    notice_file = output / (product_name+".NOTICES.txt")
    notice_file.write_text("\n".join(notices), "utf-8")
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed", "--name", product_name,
        "--paths", str(ROOT / "src"), "--distpath", str(output), "--workpath", str(build / "work"), "--specpath", str(build),
        "--icon", str(icon), "--add-data", str(addon)+":CennikAddon", "--add-data", str(notice_file)+":.",
        "--hidden-import", "pystray._win32"]
    for module_name in ("duckdb", "pyarrow", "sqlite3", "fastapi", "uvicorn", "pytest", "numpy", "pandas", "matplotlib", "IPython"):
        command.extend(["--exclude-module", module_name])
    subprocess.run([*command, str(ROOT / "tools" / ("worker_entry.py" if args.worker else "collector_entry.py"))], cwd=ROOT, check=True)
    executable = output / (product_name+".exe")
    (output / (product_name+".sha256")).write_text(hashlib.sha256(executable.read_bytes()).hexdigest()+"  "+product_name+".exe\n", "ascii")
    print(json.dumps({"exe": str(executable), "bytes": executable.stat().st_size}, ensure_ascii=False))


if __name__ == "__main__":
    main()
