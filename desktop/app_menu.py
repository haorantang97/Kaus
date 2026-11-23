"""macOS menu bar for the desktop shell (2026-11-18).

pywebview 6.2.1 builds the App, Edit and View menus but its custom menu items take
no keyboard shortcuts, so the Kaus items are added here with AppKit once the app is
running: File (New Conversation ⌘N, New Project ⇧⌘N), zoom in View (Actual Size ⌘0,
Zoom In ⌘=, Zoom Out ⌘-) and Settings ⌘, in the app menu, titled in the macOS language. Page commands go to `window.kausCommand` in the web app;
zoom uses WKWebView's own page zoom so popover coordinates stay correct.
"""
from __future__ import annotations

import json
import threading

# Menu text follows the macOS language, like the interface's default「跟随系统」(2026-11-23).
# pywebview's default menus take their titles from LOCALIZATION; the Kaus items use TEXT.
_MENU_TEXT = {
    "zh": {
        "cocoa.menu.about": "关于", "cocoa.menu.services": "服务", "cocoa.menu.view": "显示",
        "cocoa.menu.edit": "编辑", "cocoa.menu.hide": "隐藏", "cocoa.menu.hideOthers": "隐藏其他",
        "cocoa.menu.showAll": "全部显示", "cocoa.menu.quit": "退出", "cocoa.menu.fullscreen": "进入全屏幕",
        "cocoa.menu.cut": "剪切", "cocoa.menu.copy": "拷贝", "cocoa.menu.paste": "粘贴", "cocoa.menu.selectAll": "全选",
        "settings": "设置", "file": "文件", "newConversation": "新对话", "newProject": "新建项目",
        "zoomReset": "实际大小", "zoomIn": "放大", "zoomOut": "缩小",
    },
    "en": {
        "cocoa.menu.about": "About", "cocoa.menu.services": "Services", "cocoa.menu.view": "View",
        "cocoa.menu.edit": "Edit", "cocoa.menu.hide": "Hide", "cocoa.menu.hideOthers": "Hide Others",
        "cocoa.menu.showAll": "Show All", "cocoa.menu.quit": "Quit", "cocoa.menu.fullscreen": "Enter Full Screen",
        "cocoa.menu.cut": "Cut", "cocoa.menu.copy": "Copy", "cocoa.menu.paste": "Paste", "cocoa.menu.selectAll": "Select All",
        "settings": "Settings", "file": "File", "newConversation": "New Conversation", "newProject": "New Project",
        "zoomReset": "Actual Size", "zoomIn": "Zoom In", "zoomOut": "Zoom Out",
    },
}


def menu_language(preferred: list[str] | None = None) -> str:
    """"zh" when the first macOS preferred language is Chinese, otherwise "en"."""
    if preferred is None:
        try:
            from Foundation import NSLocale  # type: ignore
            preferred = [str(item) for item in NSLocale.preferredLanguages()]
        except Exception:  # noqa: BLE001 - no AppKit (tests, other platforms)
            preferred = []
    return "zh" if preferred and preferred[0].lower().startswith("zh") else "en"


TEXT = _MENU_TEXT[menu_language()]
LOCALIZATION = {key: value for key, value in TEXT.items() if key.startswith("cocoa.")}

ZOOM_STEPS = (0.75, 0.85, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0)

# (text key, page command, key, extra modifiers): page commands are handled by the web app.
FILE_ITEMS = (
    ("newConversation", "newConversation", "n", ()),
    ("newProject", "newProject", "n", ("shift",)),
)
ZOOM_ITEMS = (
    ("zoomReset", 0, "0"),
    ("zoomIn", 1, "="),
    ("zoomOut", -1, "-"),
)


def next_zoom(current: float, direction: int) -> float:
    """The next step from `current`; direction 0 resets to 1.0."""
    if direction == 0:
        return 1.0
    if direction > 0:
        return next((step for step in ZOOM_STEPS if step > current + 1e-6), ZOOM_STEPS[-1])
    return next((step for step in reversed(ZOOM_STEPS) if step < current - 1e-6), ZOOM_STEPS[0])


def command_script(name: str) -> str:
    return f"window.kausCommand && window.kausCommand({json.dumps(name)})"


def install(window) -> None:
    """Call from `webview.start(func=...)`; builds the menus on the main thread."""
    try:
        from PyObjCTools import AppHelper  # type: ignore
    except ImportError:
        return
    AppHelper.callAfter(_build, window)


_target = None  # keep the Objective-C target alive


def _build(window) -> None:
    import AppKit  # type: ignore
    from Foundation import NSObject  # type: ignore

    class KausMenuTarget(NSObject):
        def runCommand_(self, sender):
            name = sender.representedObject()
            # evaluate_js waits for the page; never block the main thread on it.
            threading.Thread(target=window.evaluate_js, args=(command_script(name),), daemon=True).start()

        def zoom_(self, sender):
            view = _web_view(window)
            if view is None or not view.respondsToSelector_("setPageZoom:"):
                return
            view.setPageZoom_(next_zoom(float(view.pageZoom()), int(sender.tag())))

    global _target
    _target = KausMenuTarget.alloc().init()
    app = AppKit.NSApplication.sharedApplication()
    main = app.mainMenu()
    if main is None:
        return

    def item(title, action, key, modifiers=()):
        entry = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        mask = AppKit.NSEventModifierFlagCommand
        if "shift" in modifiers:
            mask |= AppKit.NSEventModifierFlagShift
        entry.setKeyEquivalentModifierMask_(mask)
        entry.setTarget_(_target)
        return entry

    # App menu: Settings ⌘, after About.
    app_menu = main.itemAtIndex_(0).submenu()
    if app_menu is not None:
        settings = item(TEXT["settings"], "runCommand:", ",")
        settings.setRepresentedObject_("settings")
        app_menu.insertItem_atIndex_(AppKit.NSMenuItem.separatorItem(), 1)
        app_menu.insertItem_atIndex_(settings, 2)

    # File, right after the app menu.
    file_menu = AppKit.NSMenu.alloc().initWithTitle_(TEXT["file"])
    for text_key, command, key, modifiers in FILE_ITEMS:
        entry = item(TEXT[text_key], "runCommand:", key, modifiers)
        entry.setRepresentedObject_(command)
        file_menu.addItem_(entry)
    file_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(TEXT["file"], None, "")
    file_item.setSubmenu_(file_menu)
    main.insertItem_atIndex_(file_item, 1)

    # View: zoom above Enter Full Screen.
    view_menu = next((main.itemAtIndex_(i).submenu() for i in range(main.numberOfItems())
                      if main.itemAtIndex_(i).submenu() is not None
                      and main.itemAtIndex_(i).submenu().title() == LOCALIZATION["cocoa.menu.view"]), None)
    if view_menu is not None:
        for index, (text_key, direction, key) in enumerate(ZOOM_ITEMS):
            entry = item(TEXT[text_key], "zoom:", key)
            entry.setTag_(direction)
            view_menu.insertItem_atIndex_(entry, index)
        view_menu.insertItem_atIndex_(AppKit.NSMenuItem.separatorItem(), len(ZOOM_ITEMS))


def _web_view(window):
    try:
        from webview.platforms.cocoa import BrowserView  # type: ignore
    except ImportError:
        return None
    instance = BrowserView.instances.get(window.uid)
    return getattr(instance, "webview", None)
