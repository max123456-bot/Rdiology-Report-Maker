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

TENANT = "test-clinic"
import templates

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("  ok    " if condition else "  FAIL  ") + message)
    if not condition:
        failures.append(message)


def suite(store: storage.Store, label: str) -> None:
    print(f"\n{label} — {store.describe()}")

    # -- create, read back -------------------------------------------------- #
    store.save(TENANT, "Dr A", {"name": "Dr A", "font_name": "Arial", "font_size": 12})
    store.save(TENANT, "Dr B", {"name": "Dr B", "font_name": "Times New Roman"})
    loaded = store.load_all(TENANT)
    check(set(loaded) == {"Dr A", "Dr B"}, f"both templates come back ({sorted(loaded)})")
    check(loaded["Dr A"]["font_name"] == "Arial", "payload survives the round trip")

    # -- optimistic locking ------------------------------------------------- #
    version = store.fingerprint(TENANT, "Dr A")
    check(bool(version), "a stored template has a fingerprint")
    store.save(TENANT, "Dr A", {"name": "Dr A", "font_name": "Calibri"}, expect=version)
    check(store.load_all(TENANT)["Dr A"]["font_name"] == "Calibri", "a matching save goes through")

    try:
        store.save(TENANT, "Dr A", {"name": "Dr A", "font_name": "Hacked"}, expect=version)
        check(False, "a stale write was allowed to clobber a newer version")
    except storage.ConflictError:
        check(True, "a stale write is refused")
    check(store.load_all(TENANT)["Dr A"]["font_name"] == "Calibri",
          "the refused write left the record untouched")

    # -- concurrent writers: exactly one must win --------------------------- #
    # n=0 is the baseline. Writers use 1..5 so none of them writes content
    # identical to it - an idempotent re-write changes no bytes and therefore
    # correctly raises no conflict, which would muddy what this is testing.
    store.save(TENANT, "Race", {"name": "Race", "n": 0})
    base = store.fingerprint(TENANT, "Race")
    results: list[str] = []
    lock = threading.Lock()

    def writer(n: int) -> None:
        try:
            store.save(TENANT, "Race", {"name": "Race", "n": n}, expect=base)
            with lock:
                results.append("won")
        except storage.ConflictError:
            with lock:
                results.append("refused")
        except Exception as exc:
            with lock:
                results.append(f"error:{exc}")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(1, 6)]
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
    check(store.delete(TENANT, "Dr B") is True, "delete reports success")
    check("Dr B" not in store.load_all(TENANT), "the deleted template is gone")
    check(store.delete(TENANT, "Dr B") is False, "deleting twice is not an error")

    # -- a real Template round-trips through this store --------------------- #
    tpl = templates.copy_of(templates.HC_FORMAT, "Dr Real", doctor="Dr Real")
    tpl = templates.remember_dictation_fix(tpl, "colic list", "cholelithiasis")
    tpl = templates.remember_correction(tpl, "before", "after", rules=["Write calculi."])
    store.save(TENANT, "Dr Real", templates.to_dict(tpl))
    restored = templates.from_dict(store.load_all(TENANT)["Dr Real"])
    check(restored.vocabulary == tpl.vocabulary, "learned vocabulary survives this store")
    check(restored.preferences == tpl.preferences, "learned rules survive this store")
    check(len(restored.corrections) == len(tpl.corrections), "corrections survive this store")


def check_isolation(store: storage.Store, label: str) -> None:
    """One clinic must not see, change or delete another's templates."""
    print(f"\n{label} — tenant isolation")
    apollo, fortis = "apollo-com", "fortis-in"

    # Deliberately the same template name in both clinics.
    store.save(apollo, "Dr. Sharad", {"name": "Dr. Sharad", "doctor": "Apollo",
                                      "vocabulary": ["cholelithiasis"]})
    store.save(fortis, "Dr. Sharad", {"name": "Dr. Sharad", "doctor": "Fortis"})

    a, b = store.load_all(apollo), store.load_all(fortis)
    check(a["Dr. Sharad"]["doctor"] == "Apollo" and b["Dr. Sharad"]["doctor"] == "Fortis",
          "the same template name in two clinics stays separate")
    check(not b["Dr. Sharad"].get("vocabulary"),
          "one clinic's learned vocabulary does not leak into another's")

    check(store.load_all("never-used-this") == {},
          "a clinic that has saved nothing sees nothing")

    # A save in one must not disturb the other.
    store.save(apollo, "Dr. Sharad", {"name": "Dr. Sharad", "doctor": "Apollo edited"})
    check(store.load_all(fortis)["Dr. Sharad"]["doctor"] == "Fortis",
          "editing in one clinic leaves the other untouched")

    # Nor must a delete.
    check(store.delete(apollo, "Dr. Sharad") is True, "delete works within a clinic")
    check("Dr. Sharad" in store.load_all(fortis),
          "deleting in one clinic does not delete the other's")
    check(store.delete("never-used-this", "Dr. Sharad") is False,
          "a clinic cannot delete a template it does not own")

    # Fingerprints are per clinic too, or a conflict check would compare
    # across clinics and refuse a perfectly good save.
    fortis_version = store.fingerprint(fortis, "Dr. Sharad")
    check(bool(fortis_version), "a clinic can fingerprint its own template")
    check(store.fingerprint(apollo, "Dr. Sharad") == "",
          "a deleted template has no fingerprint in its own clinic")

    store.delete(fortis, "Dr. Sharad")


def check_schema_upgrade() -> None:
    """A database created before tenancy must upgrade itself, losing nothing."""
    print("\nUpgrading a pre-tenancy database")
    import json
    import sqlite3

    workdir = tempfile.mkdtemp(prefix="hcfmt_upgrade_")
    path = os.path.join(workdir, "old.db")
    try:
        # The exact old shape: no tenant column, primary key on name alone.
        con = sqlite3.connect(path)
        con.execute("""CREATE TABLE templates (
            name TEXT PRIMARY KEY, payload TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1, updated TEXT NOT NULL)""")
        con.execute("""CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, when_ TEXT NOT NULL, kind TEXT NOT NULL,
            subject TEXT NOT NULL, detail TEXT, user_ TEXT)""")
        original = templates.copy_of(templates.HC_FORMAT, "Dr. Old", doctor="Before tenancy")
        original = templates.remember_dictation_fix(original, "colic list", "cholelithiasis")
        con.execute("INSERT INTO templates VALUES (?,?,?,?)",
                    ("Dr. Old", json.dumps(templates.to_dict(original)), 7, "2026-01-01"))
        con.commit()
        con.close()

        store = storage.SqlStore(f"sqlite:///{path}")
        rows = store.load_all(storage.DEFAULT_TENANT)
        check("Dr. Old" in rows, "the existing template survived the upgrade")
        if "Dr. Old" in rows:
            restored = templates.from_dict(rows["Dr. Old"])
            check(restored.doctor == "Before tenancy", "its contents are intact")
            check(bool(restored.vocabulary), "its learned vocabulary is intact")
        check(store.fingerprint(storage.DEFAULT_TENANT, "Dr. Old") == "7",
              "the version counter was carried over, so locking still works")

        # The whole point: two clinics can now share a name.
        store.save("other-clinic", "Dr. Old", {"name": "Dr. Old", "doctor": "Someone else"})
        check(store.load_all(storage.DEFAULT_TENANT)["Dr. Old"]["doctor"] == "Before tenancy",
              "adding another clinic did not touch the migrated rows")

        # The original rows are kept, so a bad migration is recoverable.
        con = sqlite3.connect(path)
        kept = con.execute("SELECT count(*) FROM templates_pre_tenant").fetchone()[0]
        con.close()
        check(kept == 1, "the pre-migration rows are kept for recovery")

        # Opening again must not migrate a second time.
        store.close()
        again = storage.SqlStore(f"sqlite:///{path}")
        check(len(again.load_all(storage.DEFAULT_TENANT)) == 1,
              "re-opening does not re-run the migration")
        again.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def check_migration() -> None:
    print("\nMigration between stores")
    workdir = tempfile.mkdtemp(prefix="hcfmt_migrate_")
    try:
        files = storage.FileStore(os.path.join(workdir, "templates"))
        tpl = templates.copy_of(templates.HC_FORMAT, "Dr Move", doctor="Dr Move")
        tpl = templates.remember_vocabulary(tpl, ["cholelithiasis", "hydronephrosis"])
        files.save(TENANT, "Dr Move", templates.to_dict(tpl))

        db = storage.SqlStore(f"sqlite:///{os.path.join(workdir, 'moved.db')}")
        for name, payload in files.load_all(TENANT).items():
            db.save(TENANT, name, payload)

        moved = templates.from_dict(db.load_all(TENANT)["Dr Move"])
        check(moved.doctor == "Dr Move", "the template arrived in the database")
        check(moved.vocabulary == tpl.vocabulary, "its learned vocabulary came with it")

        # …and back again, so nobody is locked in.
        back = storage.FileStore(os.path.join(workdir, "back"))
        for name, payload in db.load_all(TENANT).items():
            back.save(TENANT, name, payload)
        returned = templates.from_dict(back.load_all(TENANT)["Dr Move"])
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
        files = storage.FileStore(os.path.join(workdir, "files"))
        suite(files, "JSON files")
        check_isolation(files, "JSON files")

        sqlite = storage.SqlStore(f"sqlite:///{os.path.join(workdir, 'test.db')}")
        suite(sqlite, "SQLite")
        check_isolation(sqlite, "SQLite")
        sqlite.close()

        pg_url = os.environ.get("TEST_POSTGRES_URL", "").strip()
        if pg_url:
            pg = storage.SqlStore(pg_url)
            for name in list(pg.load_all(TENANT)):
                pg.delete(TENANT, name)
            suite(pg, "Postgres")
            for name in list(pg.load_all(TENANT)):
                pg.delete(TENANT, name)
            pg.close()
        else:
            print("\nPostgres — skipped (set TEST_POSTGRES_URL to a throwaway database to run it)")

        check_schema_upgrade()
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
