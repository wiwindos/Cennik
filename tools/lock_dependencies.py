"""Record exactly the tested environment, excluding this machine's editable path."""

from importlib.metadata import distributions
from pathlib import Path

packages = sorted(
    {
        f"{dist.metadata['Name']}=={dist.version}"
        for dist in distributions()
        if dist.metadata["Name"].lower() not in {"ahledger-historical", "pip", "setuptools"}
    },
    key=str.lower,
)
Path(__file__).resolve().parents[1].joinpath("requirements.lock.txt").write_text(
    "# Tested on Python 3.12 / Windows 11; install project with pip install -e . --no-deps\n"
    + "\n".join(packages)
    + "\n",
    encoding="utf-8",
)
