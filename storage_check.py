"""
Storage backends: same behaviour from JSON files, SQLite and Postgres.

The whole point of storage.py is that the app cannot tell which store is in use.
This runs the identical suite against each one, then checks the migration path
between them.

    python storage_check.py

Files and SQLite run offline with nothing installed. Postgres is skipped unless
TEST_POSTGRES_URL points at a throwaway database.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading

import storage
import templates

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("  ok    " if condition else "  FAIL  ") + message)
    if not condition:
        failures.append(message)


def suite(store: storage.Store, label: str) -> None:
    print(f"\n{label} — {store.describe()}")

    # -- create, read back -------------------------------------------------- #
    store.save("Dr A", {"name": "Dr A", "font_name": "Arial", "font_size": 12})
    store.save("Dr B", {"name": "Dr B", "font_name": "Times New Roman"})
    loaded = store.load_all()
    check(set(loaded) == {"Dr A", "Dr B"}, f"both templates come back ({sorted(loaded)})")
    check(loaded["Dr A"]["font_name"] == "Arial", "payload survives the round trip")

    # -- optimistic locking ------------------------------------------------- #
    version = store.fingerprint("Dr A")
    check(bool(version), "a stored template has a fingerprint")
    store.save("Dr A", {"name": "Dr A", "font_name": "Calibri"}, expect=version)
    check(store.load_all()["Dr A"]["font_name"] == "Calibri", "a matching save goes through")

    try:
        store.save("Dr A", {"name": "Dr A", "font_name": "Hacked"}, expect=version)
        check(False, "a stale write was allowed to clobber a newer version")
    except storage.ConflictError:
        check(True, "a stale write is refused")
    check(store.load_all()["Dr A"]["font_name"] == "Calibri",
          "the refused write left the record untouched")

    # -- concurrent writers: exactly one must win --------------------------- #
    store.save("Race", {"name": "Race", "n": 0})
    base = store.fingerprint("Race")
    results: list[str] = []
    lock = threading.Lock()

    def writer(n: int) -> None:
        try:
            store.save("Race", {"name": "Race", "n": n}, expect=base)
            with lock:
                results.append("won")
        except storage.ConflictError:
            with lock:
                results.append("refused")
        except Exception as exc:
            with lock:
                results.append(f"error:{exc}")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wins = results.count("won")
    check(wins == 1, f"exactly one of 5 concurrent writers wins (got {wins}: {results})")

    # -- events ------------------------------------------------------------- #
    store.record(storage.Event(when="2026-01-01T09:00:00", kind="report.generated",
                               subject="USG Abdomen", detail="audit PASS", user="dr@clinic"))
    store.record(storage.Event(when="2026-01-01T09:05:00", kind="template.saved",
                               subject="Dr A"))
    rows = store.events(limit=10)
    check(len(rows) >= 2, f"events are recorded ({len(rows)})")
    check(rows[0].kind == "template.saved", "events come back newest first")
    check(any(e.user == "dr@clinic" for e in rows), "the user is recorded when known")

    # -- delete ------------------------------------------------------------- #
    check(store.delete("Dr B") is True, "delete reports success")
    check("Dr B" not in store.load_all(), "the deleted template is gone")
    check(store.delete("Dr B") is False, "deleting twice is not an error")

    # -- a real Template round-trips through this store --------------------- #
    tpl = templates.copy_of(templates.HC_FORMAT, "Dr Real", doctor="Dr Real")
    tpl = templates.remember_dictation_fix(tpl, "colic list", "cholelithiasis")
    tpl = templates.remember_correction(tpl, "before", "after", rules=["Write calculi."])
    store.save("Dr Real", templates.to_dict(tpl))
    restored = templates.from_dict(store.load_all()["Dr Real"])
    check(restored.vocabulary == tpl.vocabulary, "learned vocabulary survives this store")
    check(restored.preferences == tpl.preferences, "learned rules survive this store")
    check(len(restored.corrections) == len(tpl.corrections), "corrections survive this store")


def check_migration() -> None:
    print("\nMigration between stores")
    workdir = tempfile.mkdtemp(prefix="hcfmt_migrate_")
    try:
        files = storage.FileStore(os.path.join(workdir, "templates"))
        tpl = templates.copy_of(templates.HC_FORMAT, "Dr Move", doctor="Dr Move")
        tpl = templates.remember_vocabulary(tpl, ["cholelithiasis", "hydronephrosis"])
        files.save("Dr Move", templates.to_dict(tpl))

        db = storage.SqlStore(f"sqlite:///{os.path.join(workdir, 'moved.db')}")
        for name, payload in files.load_all().items():
            db.save(name, payload)

        moved = templates.from_dict(db.load_all()["Dr Move"])
        check(moved.doctor == "Dr Move", "the template arrived in the database")
        check(moved.vocabulary == tpl.vocabulary, "its learned vocabulary came with it")

        # …and back again, so nobody is locked in.
        back = storage.FileStore(os.path.join(workdir, "back"))
        for name, payload in db.load_all().items():
            back.save(name, payload)
        returned = templates.from_dict(back.load_all()["Dr Move"])
        check(returned.vocabulary == tpl.vocabulary, "and it survives the trip back to files")
        db.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def check_fallback() -> None:
    print("\nA broken storage URL must not take the clinic offline")
    store = storage.get_store("postgresql://nobody@127.0.0.1:1/nothing", force=True)
    check(isinstance(store, storage.FileStore), "falls back to files when the database is unreachable")
    check(bool(storage.storage_problem()), "and says why, rather than failing silently")
    storage.get_store("", force=True)  # restore the default for anything after this


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="hcfmt_storage_")
    try:
        suite(storage.FileStore(os.path.join(workdir, "files")), "JSON files")

        sqlite = storage.SqlStore(f"sqlite:///{os.path.join(workdir, 'test.db')}")
        suite(sqlite, "SQLite")
        sqlite.close()

        pg_url = os.environ.get("TEST_POSTGRES_URL", "").strip()
        if pg_url:
            pg = storage.SqlStore(pg_url)
            for name in list(pg.load_all()):
                pg.delete(name)
            suite(pg, "Postgres")
            for name in list(pg.load_all()):
                pg.delete(name)
            pg.close()
        else:
            print("\nPostgres — skipped (set TEST_POSTGRES_URL to a throwaway database to run it)")

        check_migration()
        check_fallback()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n" + "=" * 70)
    if failures:
        print(f"{len(failures)} failure(s):")
        for message in failures:
            print("  -", message)
        return 1
    print("Every store behaves identically, and migration is lossless both ways.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
