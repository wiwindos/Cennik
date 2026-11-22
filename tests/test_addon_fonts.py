from pathlib import Path

import pytest
from lupa.lua51 import LuaRuntime
from test_addon import exchange as exchange
from test_cennik import analytics_root as analytics_root
from test_sniper import game

from cennik.workspace.install import addon_files, install_addon

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "CennikAddon"


def test_font_preserves_sizes_colors_and_uses_private_button_styles():
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute("""
        C={}; created=0
        function makeFont(path,size,flags,color)
          return {path=path,size=size,flags=flags,color=color,
            GetFont=function(self) return self.path,self.size,self.flags end,
            SetFont=function(self,p,s,f) self.path,self.size,self.flags=p,s,f;return true end,
            CopyFontObject=function(self,other)
              self.path,self.size,self.flags,self.color=other.path,other.size,other.flags,other.color
            end}
        end
        function CreateFont() created=created+1;return makeFont() end
        normal=makeFont('latin-only.ttf',14,'OUTLINE','gold')
        disabled=makeFont('latin-only.ttf',14,'','grey')
        button={normal=normal,disabled=disabled,
          GetNormalFontObject=function(self) return self.normal end,
          SetNormalFontObject=function(self,f) self.normal=f end,
          GetDisabledFontObject=function(self) return self.disabled end,
          SetDisabledFontObject=function(self,f) self.disabled=f end}
    """)
    lua.execute("assert(loadstring(...))('CennikAddon',C)", (ADDON / "UI/Fonts.lua").read_text("utf-8"))
    lua.execute("C.Fonts:Apply(button);C.Fonts:Apply(button)")
    assert lua.eval("button.normal.path") == "Interface\\AddOns\\CennikAddon\\Media\\DejaVuSans.ttf"
    assert lua.eval("button.normal.size") == 14
    assert lua.eval("button.normal.flags") == "OUTLINE"
    assert lua.eval("button.disabled.color") == "grey"
    assert lua.eval("normal.path") == "latin-only.ttf"
    assert lua.eval("disabled.path") == "latin-only.ttf"
    assert lua.eval("created") == 2  # Re-rendering must not create more font objects.


def test_sniper_text_and_input_controls_use_the_bundled_font(exchange):
    lua = game(exchange)
    lua.execute("""
        Cennik.Sniper:ShowFindings()
        Cennik.Sniper:ShowEditor()
        Cennik.CollectorUI:Show(true)
        checked=0
        for _,frame in ipairs(Game.frames) do
          if (frame.kind=='FontString' or frame.kind=='EditBox') and rawget(frame,'text') then
            local font=rawget(frame,'font')
            assert(font and font[1]==Cennik.Fonts.path,'A text control retained the client font')
            checked=checked+1
          end
        end
    """)
    assert lua.eval("checked") > 15


def test_font_contains_all_ui_characters_and_russian_alphabet():
    tt = pytest.importorskip("fontTools.ttLib")
    font = tt.TTFont(ADDON / "Media/DejaVuSans.ttf")
    try:
        cmap = font.getBestCmap()
        text = "".join(p.read_text("utf-8") for p in ADDON.rglob("*.lua"))
        needed = {ord(c) for c in text if ord(c) > 32} | set(range(0x410, 0x450)) | {0x401, 0x451}
        assert not needed.difference(cmap)
    finally:
        font.close()


def test_installer_copies_media_and_keeps_addressed_import(tmp_path):
    game_root = tmp_path / "game"
    addons = game_root / "Interface/AddOns"
    (addons / "Auctionator").mkdir(parents=True)
    destination = addons / "CennikAddon"
    destination.mkdir()
    (destination / "CennikAddon.toc").write_text("## Title: Cennik\nImportedData.lua\n", "utf-8")
    (destination / "ImportedData.lua").write_text("PRIVATE_EXISTING_IMPORT", "utf-8")
    result = install_addon(str(game_root), tmp_path / "backups", source=ADDON)
    for name in ("Media/DejaVuSans.ttf", "Media/DejaVu-LICENSE.txt", "UI/Fonts.lua"):
        assert name in result["files_updated"]
        assert (destination / name).read_bytes() == (ADDON / name).read_bytes()
    assert (destination / "ImportedData.lua").read_text() == "PRIVATE_EXISTING_IMPORT"
    assert "Media/DejaVuSans.ttf" in addon_files(ADDON)


def test_missing_font_is_detected_before_update(tmp_path):
    (tmp_path / "CennikAddon.toc").write_text("## X-Cennik-Assets: Media/missing.ttf\n", "utf-8")
    with pytest.raises(ValueError, match="missing.ttf"):
        addon_files(tmp_path)
