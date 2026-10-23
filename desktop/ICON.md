# Kaus desktop icon

The application icon reuses the sidebar's KAUS wordmark: Anton Regular, 0.02em letter spacing, −7° screen-coordinate shear, and the light-theme green `#5e7a12`. Font outlines are preserved as vectors; no generated or raster-traced logo is used. Anton's existing SIL Open Font License is in `web/public/fonts/LICENSE-Anton.txt`.

`AppIcon.svg` is the vector source deliverable. `AppIcon.png` is a 1024×1024 RGBA export with transparent corners and a rounded-square cream face. `AppIcon.icns` contains all ten macOS 1×/2× representations from 16 through 1024 pixels.

Rebuild on macOS with Python's `fontTools` and Brotli installed:

```sh
python3 desktop/build_icon.py
```

The builder reads the existing web font and uses Core Text/Core Graphics, then `iconutil`. It writes only the three icon assets beside itself. The generated asset needs no font or build dependency at runtime.

The application bundle uses `Info.plist` for Kaus branding. The existing executable name and bundle identifier remain compatible with installed launchers; service identifiers and user data directories do not change.
