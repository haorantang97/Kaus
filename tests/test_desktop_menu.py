"""Desktop menu bar text and zoom steps (desktop/app_menu.py). No AppKit needed."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

import app_menu  # noqa: E402


def test_menu_follows_the_macos_language():
    assert app_menu.menu_language(["zh-Hans-CN", "en-US"]) == "zh"
    assert app_menu.menu_language(["en-US", "zh-Hans-CN"]) == "en"
    assert app_menu.menu_language([]) == "en"


def test_both_languages_name_every_menu_item():
    zh, en = app_menu._MENU_TEXT["zh"], app_menu._MENU_TEXT["en"]
    assert zh.keys() == en.keys()
    used = {"settings", "file"} | {key for key, *_ in app_menu.FILE_ITEMS} | {key for key, *_ in app_menu.ZOOM_ITEMS}
    assert used <= zh.keys()
    assert set(app_menu.LOCALIZATION) == {key for key in app_menu.TEXT if key.startswith("cocoa.")}


def test_zoom_steps():
    assert app_menu.next_zoom(1.0, 1) == 1.1
    assert app_menu.next_zoom(1.0, -1) == 0.9
    assert app_menu.next_zoom(2.0, 1) == 2.0
    assert app_menu.next_zoom(1.5, 0) == 1.0
