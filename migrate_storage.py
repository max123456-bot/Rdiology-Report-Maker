"""
Move templates from one store to another.

Use this when you outgrow JSON files - a second machine appears, two people start
editing at once, or you deploy somewhere the filesystem does not survive a restart.

    # files -> a SQLite file on a shared drive
    python migrate_storage.py --to "sqlite:///data/reports.db"

    # files -> Postgres, which is what Streamlit Cloud needs
    python migrate_storage.py --to "postgresql://user:pw@host/dbname"

    # and back again, if you change your mind
    python migrate_storage.py --from "sqlite:///data/reports.db" --to files

    # see what would happen, change nothing
    python migrate_storage.py --to "sqlite:///data/reports.db" --dry-run

Nothing is deleted from the source. Migrate, point STORAGE_URL at the new store,
check the app, and only then clean up.
"""

from __future__ import annotations

import argparse
import sys

import storage
import templates


def build(url: str) -> storage.Store:
    if url in ("", "files", "file", "json"):
        return storage.FileStore()
    return storage.SqlStore(url)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="source", default="files",
                        help="Where templates are now. Default: files")
    parser.add_argument("--to", dest="target", required=True,
                        help="Where they should go: 'files', a sqlite:/// path, or a postgresql:// URL")
    parser.add_argument("--overwrite", action="store_true",
                        help="Replace templates that already exist in the target")
    parser.add_argument("--dry-run", action="store_true", help="Report only, change nothing")
    args = parser.parse_args()

    try:
        source = build(args.source)
        target = build(args.target)
    except Exception as exc:
        print(f"Could not open a store: {exc}")
        return 2

    print(f"From: {source.describe()}")
    print(f"To:   {target.describe()}")

    records = source.load_all()
    if not records:
        print("\nNothing to migrate — the source has no templates.")
        return 0

    existing = target.load_all()
    moved = skipped = failed = 0

    print(f"\n{len(records)} template(s) found:\n")
    for name, payload in sorted(records.items()):
        # Validate before writing: a corrupt record should be reported here, not
        # discovered by a doctor mid-clinic.
        try:
            parsed = templates.from_dict(payload)
        except Exception as exc:
            print(f"  SKIP  {name} — unreadable ({exc})")
            failed += 1
            continue

        if name in existing and not args.overwrite:
            print(f"  SKIP  {name} — already in the target (use --overwrite to replace)")
            skipped += 1
            continue

        summary = (
            f"{parsed.font_name} {parsed.font_size:g}pt · "
            f"{templates.learning_summary(parsed)}"
        )
        if args.dry_run:
            print(f"  WOULD {name} — {summary}")
            moved += 1
            continue

        try:
            target.save(name, payload)
            print(f"  OK    {name} — {summary}")
            moved += 1
        except Exception as exc:
            print(f"  FAIL  {name} — {exc}")
            failed += 1

    print(f"\n{moved} migrated, {skipped} skipped, {failed} failed.")

    if args.dry_run:
        print("\nDry run — nothing was written.")
    elif moved and args.target not in ("files", "file", "json"):
        print(
            "\nNext: put this in .streamlit/secrets.toml, then restart the app —\n"
            f'    STORAGE_URL = "{args.target}"\n\n'
            "Check the sidebar reads the new store and your templates are all there.\n"
            "The originals are untouched, so you can switch back by removing that line."
        )

    for store in (source, target):
        close = getattr(store, "close", None)
        if callable(close):
            close()

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
