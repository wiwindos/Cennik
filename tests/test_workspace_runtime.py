import io
import json
import sys

import httpx
import pytest

from cennik.workspace import __main__ as launcher
from cennik.workspace import web


@pytest.mark.parametrize("running_revision", [None, "old-revision"])
def test_launcher_refuses_to_reuse_old_server_without_touching_workspace(tmp_path, monkeypatch, running_revision):
    status = {"workspace": str(tmp_path.resolve()), "profile": "cennik-next"}
    if running_revision is not None:
        status["runtime_revision"] = running_revision
    monkeypatch.setattr(sys, "argv",
                        ["cennik", "--root", str(tmp_path), "--no-browser"])
    monkeypatch.setattr(launcher.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(json.dumps(status).encode()))
    monkeypatch.setattr(launcher, "revision", lambda: "new-revision")
    monkeypatch.setattr(launcher, "create_app", lambda *a: pytest.fail("Must not open workspace databases"))
    with pytest.raises(SystemExit, match="Закройте её консоль"):
        launcher.main()
    assert not list(tmp_path.iterdir())


def test_launcher_can_reuse_current_server(tmp_path, monkeypatch, capsys):
    status = {"workspace": str(tmp_path.resolve()), "profile": "cennik-next", "runtime_revision": "current"}
    monkeypatch.setattr(sys, "argv", ["cennik", "--root", str(tmp_path), "--no-browser"])
    monkeypatch.setattr(launcher.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(json.dumps(status).encode()))
    monkeypatch.setattr(launcher, "revision", lambda: "current")
    monkeypatch.setattr(launcher, "create_app", lambda *a: pytest.fail("Must reuse the running server"))
    launcher.main()
    assert "уже работает" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_health_identifies_loaded_code_even_after_files_change(tmp_path, monkeypatch):
    stamp = {"value": "loaded-code"}
    monkeypatch.setattr(web, "revision", lambda: stamp["value"])
    app = web.create_app(tmp_path / "fixture")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
                                     base_url="http://127.0.0.1") as client:
            initial = (await client.get("/healthz")).json()
            assert initial["runtime_revision"] == "loaded-code" and not initial["restart_required"]
            stamp["value"] = "new-code-on-disk"
            stale = (await client.get("/healthz")).json()
            assert stale["runtime_revision"] == "loaded-code" and stale["restart_required"]
    finally:
        app.state.workspace.close()
