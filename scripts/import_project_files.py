#!/usr/bin/env python3
"""Import the legacy Project files into a domain database explicitly.

The server does this automatically for the default database under its own
directory. Use this when ``DASH_DOMAIN_DB`` points elsewhere (the server then
writes a ``skipped:not_paired`` marker) or to import into a copy for inspection.

    python scripts/import_project_files.py --db /path/to/domain.sqlite3 \
        --state-root /path/to/kaus [--hermes-home ~/.hermes] [--keep-files] [--dry-run]

Native Agent directories are only read. Kaus-owned legacy files are moved into
``<state-root>/state/legacy-project-files-<stamp>/`` unless ``--keep-files``.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for _root in (REPO_ROOT / "kernel", REPO_ROOT):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="domain database path")
    parser.add_argument("--state-root", required=True, help="directory holding the legacy files (the server directory)")
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
    parser.add_argument("--profiles-dir", default=None)
    parser.add_argument("--keep-files", action="store_true", help="do not move the legacy files afterwards")
    parser.add_argument("--dry-run", action="store_true", help="only list what would be imported")
    args = parser.parse_args(argv)

    import project_file_import
    from app.persistence.sqlite import SqliteDatabase

    state_root = Path(args.state_root).resolve()
    home = Path(args.hermes_home).expanduser().resolve() if args.hermes_home else None
    if args.dry_run:
        present = project_file_import.legacy_paths(state_root)
        print("legacy files:", *[str(p) for p in present] or ["(none)"], sep="\n  ")
        return 0
    database = SqliteDatabase(Path(args.db).expanduser().resolve())
    try:
        report = project_file_import.run_import(
            database=database, state_root=state_root, hermes_home=home,
            profiles_dir=Path(args.profiles_dir).resolve() if args.profiles_dir else None,
            log=print, move_files=not args.keep_files)
    finally:
        database.close()
    for warning in report.warnings:
        print("warning:", warning)
    print(report.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
