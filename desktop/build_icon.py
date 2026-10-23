#!/usr/bin/env python3
"""Build the desktop icon from Kaus's existing Anton wordmark on macOS.

Build-time dependencies: fontTools and Brotli. Runtime needs only AppIcon.icns.
The font is decoded losslessly; no raster asset or generated logo is edited.
"""
from pathlib import Path
import subprocess
import tempfile

from fontTools.ttLib import TTFont


def main() -> None:
    desktop = Path(__file__).resolve().parent
    font_path = desktop.parent / "web/public/fonts/anton-latin-400.woff2"
    with tempfile.TemporaryDirectory(prefix="kaus-app-icon-") as temporary:
        root = Path(temporary)
        font = TTFont(font_path)
        font.flavor = None
        font.save(root / "Anton.ttf")
        subprocess.run(
            ["swift", str(desktop / "render_icon.swift"), str(root / "Anton.ttf"), str(desktop), str(root)],
            check=True,
        )
        subprocess.run(
            ["iconutil", "--convert", "icns", "--output", str(desktop / "AppIcon.icns"), str(root / "AppIcon.iconset")],
            check=True,
        )


if __name__ == "__main__":
    main()
