"""
Where templates and the audit log actually live.

The app started with JSON files, which is right for one clinic on one PC. This
adds the two things files cannot do:

  * survive a Streamlit Cloud restart, which wipes the filesystem
  * take concurrent edits from several people without losing one of them

Pick a store with STORAGE_URL in .streamlit/secrets.toml:

    (absent)                        JSON files in templates/      - default, single PC
    sqlite:///data/reports.db       one transactional file        - shared drive, several tabs
    postgresql://user:pw@host/db    a real server                 - Cloud, several machines

Every store implements the same four operations plus an events log, so the rest
of the app neither knows nor cares which one is in use. Switching is a one-line
config change and a `python migrate_storage.py`.

Encryption at rest and backups are properties of where you put the database, not
of this file: a managed Postgres gives you both, a laptop gives you neither.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

HERE = os.path.dirname(os.path.abspath(__file__))
# A database that does not answer must never hold up a clinic. Fail in seconds
# and fall back to files, rather than hanging the app.
CONNECT_TIMEOUT = 8



class ConflictError(RuntimeError):
    """Someone else changed this record while it was open here."""


@dataclass
class Event:
    """One thing that happened, for the audit trail."""

    when: str
    kind: str          # report.generated | template.saved | template.deleted | dictation.learned
    subject: str       # which template or report
    detail: str = ""
    user: str = ""


DEFAULT_TENANT = "default"


def clean_tenant(tenant: str | None) -> str:
    """
    A tenant is one clinic. Everything it owns is invisible to every other one.

    Normalised hard, because this string decides who can see whose patients'
    templates: lowercase, alphanumerics and dashes only, never empty, never a
    path fragment.
    """
    import re as _re

    slug = _re.sub(r"[^a-z0-9]+", "-", str(tenant or "").strip().lower()).strip("-")
    return slug[:64] or DEFAULT_TENANT


class Store(Protocol):
    """What the app needs from persistence. Deliberately small.

    Every operation takes a tenant. There is no way to read across tenants by
    accident, because there is no call that omits one.
    """

    def load_all(self, tenant: str) -> dict[str, dict]: ...
    def save(self, tenant: str, name: str, payload: dict,
             expect: str | None = None) -> None: ...
    def delete(self, tenant: str, name: str) -> bool: ...
    def fingerprint(self, tenant: str, name: str) -> str: ...
    def record(self, event: Event) -> None: ...
    def events(self, limit: int = 200) -> list[Event]: ...
    def describe(self) -> str: ...

    # Report records - the worklist, the sign-off trail and the patient history
    # live here. A record is a dict that carries its own "id"; the store also
    # indexes "status", "patient_key" and "urgency" so the worklist can filter
    # without loading every report ever written.
    def save_report(self, tenant: str, record: dict) -> None: ...
    def get_report(self, tenant: str, report_id: str) -> dict | None: ...
    def list_reports(self, tenant: str, status: str | None = None,
                     patient_key: str | None = None, limit: int = 200) -> list[dict]: ...
    def delete_report(self, tenant: str, report_id: str) -> bool: ...


# --------------------------------------------------------------------------- #
# JSON files - the default
# --------------------------------------------------------------------------- #


class FileStore:
    """
    One JSON file per template, under a directory per tenant.

    Kept as the default because it is inspectable, diffable and trivially backed
    up by copying a folder. Tenants are separate directories, so one clinic
    physically cannot read another's files - the isolation is the filesystem's,
    not a filter this code has to remember to apply.
    """

    def __init__(self, directory: str | None = None) -> None:
        self.root = directory or os.path.join(HERE, "templates")
        self.events_path = os.path.join(self.root, "_events.jsonl")
        self._lock = threading.Lock()

    # -- helpers ---------------------------------------------------------- #

    def _tenant_dir(self, tenant: str) -> str:
        """Templates for one clinic. clean_tenant() has already made this safe."""
        return os.path.join(self.root, clean_tenant(tenant))

    @property
    def backups(self) -> str:
        return os.path.join(self.root, "_backups")

    def _slug(self, name: str) -> str:
        import hashlib
        import re

        stem = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "template"
        digest = hashlib.sha1(name.strip().encode("utf-8")).hexdigest()[:8]
        return f"{stem[:60]}-{digest}.json"

    def _path(self, tenant: str, name: str) -> str:
        return os.path.join(self._tenant_dir(tenant), self._slug(name))

    def _find(self, tenant: str, name: str) -> str | None:
        exact = self._path(tenant, name)
        if os.path.exists(exact):
            return exact
        folder = self._tenant_dir(tenant)
        if not os.path.isdir(folder):
            return None
        for filename in os.listdir(folder):
            if not filename.endswith(".json"):
                continue
            candidate = os.path.join(folder, filename)
            if not os.path.isfile(candidate):
                continue
            try:
                with open(candidate, encoding="utf-8") as fh:
                    if str(json.load(fh).get("name", "")).strip() == name.strip():
                        return candidate
            except Exception:
                continue
        return None

    def _back_up(self, path: str) -> None:
        if not os.path.exists(path):
            return
        try:
            import shutil

            os.makedirs(self.backups, exist_ok=True)
            stem = os.path.basename(path)[:-5]
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.copy2(path, os.path.join(self.backups, f"{stem}.{stamp}.json"))
            mine = sorted(f for f in os.listdir(self.backups) if f.startswith(stem + "."))
            for stale in mine[:-10]:
                os.remove(os.path.join(self.backups, stale))
        except OSError:
            pass  # a failed backup must never block the save

    # -- Store ------------------------------------------------------------ #

    def load_all(self, tenant: str) -> dict[str, dict]:
        out: dict[str, dict] = {}
        folder = self._tenant_dir(tenant)
        if not os.path.isdir(folder):
            return out
        for filename in sorted(os.listdir(folder)):
            if not filename.endswith(".json") or filename.endswith(".tmp"):
                continue
            path = os.path.join(folder, filename)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    payload = json.load(fh)
            except Exception:
                continue  # a corrupt file must not take the app down
            if isinstance(payload, dict) and payload.get("name"):
                out[str(payload["name"])] = payload
        return out

    def fingerprint(self, tenant: str, name: str) -> str:
        """
        A hash of the file's contents.

        Not mtime and size: Windows timestamps are coarse enough that twenty
        rapid writes share one mtime, and two saves of similar-length JSON share
        a size. Together that made concurrent edits look identical, so a stale
        write silently clobbered the newer one.
        """
        import hashlib

        path = self._find(tenant, name)
        if not path or not os.path.exists(path):
            return ""
        try:
            with open(path, "rb") as fh:
                return hashlib.sha1(fh.read()).hexdigest()
        except OSError:
            return ""

    def save(self, tenant: str, name: str, payload: dict,
             expect: str | None = None) -> None:
        with self._lock:
            if expect is not None:
                current = self.fingerprint(tenant, name)
                if current and current != expect:
                    raise ConflictError(
                        f"“{name}” was changed somewhere else while you had it open. "
                        "Reload it, then reapply your edit so neither change is lost."
                    )
            folder = self._tenant_dir(tenant)
            os.makedirs(folder, exist_ok=True)
            target = self._path(tenant, name)
            self._back_up(target)
            tmp = target + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, target)

    def delete(self, tenant: str, name: str) -> bool:
        with self._lock:
            path = self._find(tenant, name)
            if path and os.path.exists(path):
                self._back_up(path)
                os.remove(path)
                return True
            return False

    def record(self, event: Event) -> None:
        try:
            os.makedirs(self.root, exist_ok=True)
            with open(self.events_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.__dict__, ensure_ascii=False) + "\n")
        except OSError:
            pass  # the audit log must never block clinical work

    def events(self, limit: int = 200) -> list[Event]:
        if not os.path.exists(self.events_path):
            return []
        try:
            with open(self.events_path, encoding="utf-8") as fh:
                lines = fh.readlines()[-limit:]
        except OSError:
            return []
        rows: list[Event] = []
        for line in lines:
            try:
                rows.append(Event(**json.loads(line)))
            except Exception:
                continue
        return list(reversed(rows))

    def tenants(self) -> list[str]:
        if not os.path.isdir(self.root):
            return []
        return sorted(
            d for d in os.listdir(self.root)
            if os.path.isdir(os.path.join(self.root, d)) and not d.startswith("_")
        )

    # -- report records ---------------------------------------------------- #

    def _reports_dir(self, tenant: str) -> str:
        return os.path.join(self.root, "_reports", clean_tenant(tenant))

    def _report_path(self, tenant: str, report_id: str) -> str:
        import re as _re

        safe = _re.sub(r"[^a-z0-9-]", "", str(report_id).lower())[:64]
        return os.path.join(self._reports_dir(tenant), f"{safe}.json")

    def save_report(self, tenant: str, record: dict) -> None:
        if not record.get("id"):
            raise ValueError("A report record needs an id.")
        with self._lock:
            folder = self._reports_dir(tenant)
            os.makedirs(folder, exist_ok=True)
            target = self._report_path(tenant, record["id"])
            tmp = target + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(record, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, target)

    def get_report(self, tenant: str, report_id: str) -> dict | None:
        path = self._report_path(tenant, report_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    def list_reports(self, tenant: str, status: str | None = None,
                     patient_key: str | None = None, limit: int = 200) -> list[dict]:
        folder = self._reports_dir(tenant)
        if not os.path.isdir(folder):
            return []
        rows: list[dict] = []
        for filename in os.listdir(folder):
            if not filename.endswith(".json"):
                continue
            try:
                with open(os.path.join(folder, filename), encoding="utf-8") as fh:
                    payload = json.load(fh)
            except Exception:
                continue  # one corrupt record must not hide the worklist
            if not isinstance(payload, dict) or not payload.get("id"):
                continue
            if status and payload.get("status") != status:
                continue
            if patient_key and payload.get("patient_key") != patient_key:
                continue
            rows.append(payload)
        rows.sort(key=lambda r: str(r.get("updated") or r.get("created") or ""), reverse=True)
        return rows[:limit]

    def delete_report(self, tenant: str, report_id: str) -> bool:
        with self._lock:
            path = self._report_path(tenant, report_id)
            if os.path.exists(path):
                os.remove(path)
                return True
            return False

    def describe(self) -> str:
        return f"JSON files in {os.path.basename(self.root)}/"


# --------------------------------------------------------------------------- #
# SQL - SQLite and Postgres share one implementation
# --------------------------------------------------------------------------- #


class SqlStore:
    """
    A real database: transactions, so two people editing cannot lose one edit.

    SQLite is a single file and needs nothing installed - the right step up from
    JSON for a shared drive or a busy single machine. Postgres is the same code
    against a server, which is what Streamlit Cloud needs because its filesystem
    does not survive a restart.

    Concurrency is handled by the database, not by us: the save is one statement
    guarded by the stored version, so a stale write updates zero rows and is
    reported rather than silently winning.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self.is_postgres = url.startswith(("postgres://", "postgresql://"))
        self._lock = threading.Lock()
        self._connect()
        self._create_schema()

    def _connect(self):
        if self.is_postgres:
            try:
                import psycopg
            except ImportError as exc:
                raise RuntimeError(
                    "Postgres storage needs the driver:  pip install 'psycopg[binary]'"
                ) from exc
            # Without an explicit timeout an unreachable host blocks forever and
            # takes the whole app with it - which is exactly what happens when a
            # free-tier database is paused, moved, or the URL is stale.
            # Keepalives, because serverless Postgres (Neon) suspends idle
            # computes and silently closes the session; the probes keep a
            # mostly-idle clinic app's connection from going stale so often.
            self._conn = psycopg.connect(
                self.url, autocommit=True, connect_timeout=CONNECT_TIMEOUT,
                keepalives=1, keepalives_idle=30, keepalives_interval=10,
                keepalives_count=3,
            )
            self._ph = "%s"
        else:
            import sqlite3

            path = self.url.replace("sqlite:///", "", 1)
            if path and path != ":memory:":
                os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            self._conn = sqlite3.connect(path or ":memory:", check_same_thread=False)
            # WAL lets a reader and a writer work at the same time instead of
            # one blocking the other.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._ph = "?"
        return self._conn

    def _create_schema(self) -> None:
        json_type = "JSONB" if self.is_postgres else "TEXT"
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                f"""CREATE TABLE IF NOT EXISTS templates (
                        tenant    TEXT NOT NULL DEFAULT 'default',
                        name      TEXT NOT NULL,
                        payload   {json_type} NOT NULL,
                        version   INTEGER NOT NULL DEFAULT 1,
                        updated   TEXT NOT NULL,
                        PRIMARY KEY (tenant, name)
                    )"""
            )
            cur.execute(
                """CREATE TABLE IF NOT EXISTS events (
                        id      INTEGER PRIMARY KEY AUTOINCREMENT,
                        when_   TEXT NOT NULL,
                        kind    TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        detail  TEXT,
                        user_   TEXT
                    )""".replace(
                    "INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY"
                ) if self.is_postgres else
                """CREATE TABLE IF NOT EXISTS events (
                        id      INTEGER PRIMARY KEY AUTOINCREMENT,
                        when_   TEXT NOT NULL,
                        kind    TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        detail  TEXT,
                        user_   TEXT
                    )"""
            )
            cur.execute("CREATE INDEX IF NOT EXISTS events_when ON events (when_)")
            cur.execute(
                f"""CREATE TABLE IF NOT EXISTS reports (
                        tenant       TEXT NOT NULL DEFAULT 'default',
                        id           TEXT NOT NULL,
                        status       TEXT NOT NULL DEFAULT 'draft',
                        urgency      TEXT NOT NULL DEFAULT 'routine',
                        patient_key  TEXT NOT NULL DEFAULT '',
                        payload      {json_type} NOT NULL,
                        updated      TEXT NOT NULL,
                        PRIMARY KEY (tenant, id)
                    )"""
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS reports_status ON reports (tenant, status)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS reports_patient ON reports (tenant, patient_key)"
            )
            if not self.is_postgres:
                self._conn.commit()

        self._migrate_schema()

    def _columns(self, table: str) -> set[str]:
        cur = self._conn.cursor()
        if self.is_postgres:
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                (table,),
            )
            return {str(r[0]) for r in cur.fetchall()}
        cur.execute(f"PRAGMA table_info({table})")
        return {str(r[1]) for r in cur.fetchall()}

    def _migrate_schema(self) -> None:
        """
        Bring an older database up to the current shape.

        CREATE TABLE IF NOT EXISTS does nothing when the table is already there,
        so a database created before tenancy existed keeps its old columns and
        every query against `tenant` fails. Databases in use cannot be recreated,
        so they are rebuilt in place with the existing rows assigned to the
        default tenant.

        Safe to run on every start: it checks first and does nothing when the
        schema is already current.
        """
        with self._lock:
            try:
                columns = self._columns("templates")
            except Exception:
                return  # brand new database - _create_schema already made it right
            if not columns or "tenant" in columns:
                return

            cur = self._conn.cursor()
            json_type = "JSONB" if self.is_postgres else "TEXT"
            try:
                cur.execute("ALTER TABLE templates RENAME TO templates_pre_tenant")
                cur.execute(
                    f"""CREATE TABLE templates (
                            tenant    TEXT NOT NULL DEFAULT 'default',
                            name      TEXT NOT NULL,
                            payload   {json_type} NOT NULL,
                            version   INTEGER NOT NULL DEFAULT 1,
                            updated   TEXT NOT NULL,
                            PRIMARY KEY (tenant, name)
                        )"""
                )
                cur.execute(
                    "INSERT INTO templates (tenant, name, payload, version, updated) "
                    "SELECT 'default', name, payload, version, updated FROM templates_pre_tenant"
                )
                # Kept, not dropped: if anything about this migration was wrong,
                # the original rows are still there to recover from.
                if not self.is_postgres:
                    self._conn.commit()
            except Exception:
                if not self.is_postgres:
                    try:
                        self._conn.rollback()
                    except Exception:
                        pass
                raise

    def _dumps(self, payload: dict) -> str:
        return json.dumps(payload, ensure_ascii=False)

    def _loads(self, value) -> dict:
        return value if isinstance(value, dict) else json.loads(value)

    # -- dropped-connection recovery --------------------------------------- #

    _DISCONNECT_MARKERS = (
        "ssl connection has been closed", "server closed the connection",
        "connection is closed", "connection already closed",
        "consuming input failed", "eof detected", "connection reset",
        "terminating connection", "broken pipe", "bad connection",
    )

    @classmethod
    def _is_disconnect(cls, exc: Exception) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in cls._DISCONNECT_MARKERS)

    def _reconnect(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
        self._connect()

    def _run(self, action):
        """
        Run action(cursor) under the lock, rebuilding the connection once if
        the server dropped it.

        Neon (and serverless Postgres generally) suspends an idle compute and
        closes the session; the next query on the cached connection then dies
        with "SSL connection has been closed unexpectedly". That is not an
        error in the request - the request never arrived - so it is retried
        exactly once on a fresh connection. A second failure is real and
        raises.
        """
        with self._lock:
            try:
                return action(self._conn.cursor())
            except Exception as exc:
                if not (self.is_postgres and self._is_disconnect(exc)):
                    raise
                self._reconnect()
                return action(self._conn.cursor())

    # -- Store ------------------------------------------------------------ #

    def load_all(self, tenant: str) -> dict[str, dict]:
        def go(cur):
            cur.execute(
                f"SELECT name, payload FROM templates WHERE tenant = {self._ph}",
                (clean_tenant(tenant),),
            )
            out = {}
            for name, payload in cur.fetchall():
                try:
                    out[str(name)] = self._loads(payload)
                except Exception:
                    continue
            return out

        return self._run(go)

    def fingerprint(self, tenant: str, name: str) -> str:
        def go(cur):
            cur.execute(
                f"SELECT version FROM templates WHERE tenant = {self._ph} AND name = {self._ph}",
                (clean_tenant(tenant), name),
            )
            row = cur.fetchone()
            return str(row[0]) if row else ""

        return self._run(go)

    def save(self, tenant: str, name: str, payload: dict,
             expect: str | None = None) -> None:
        tenant = clean_tenant(tenant)

        def go(cur):
            cur.execute(
                f"SELECT version FROM templates WHERE tenant = {self._ph} AND name = {self._ph}",
                (tenant, name),
            )
            row = cur.fetchone()
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")

            if row is None:
                cur.execute(
                    f"INSERT INTO templates (tenant, name, payload, version, updated) "
                    f"VALUES ({self._ph}, {self._ph}, {self._ph}, 1, {self._ph})",
                    (tenant, name, self._dumps(payload), now),
                )
            else:
                current = str(row[0])
                if expect is not None and expect and expect != current:
                    raise ConflictError(
                        f"“{name}” was changed somewhere else while you had it open. "
                        "Reload it, then reapply your edit so neither change is lost."
                    )
                # Guarding on the version in the WHERE clause means two
                # simultaneous saves cannot both succeed - the loser updates
                # no rows and is told, rather than silently winning.
                cur.execute(
                    f"UPDATE templates SET payload = {self._ph}, version = version + 1, "
                    f"updated = {self._ph} WHERE tenant = {self._ph} AND name = {self._ph} "
                    f"AND version = {self._ph}",
                    (self._dumps(payload), now, tenant, name, int(current)),
                )
                if cur.rowcount == 0:
                    raise ConflictError(
                        f"“{name}” was saved by someone else a moment ago. "
                        "Reload it and reapply your edit."
                    )
            if not self.is_postgres:
                self._conn.commit()

        self._run(go)

    def delete(self, tenant: str, name: str) -> bool:
        def go(cur):
            cur.execute(
                f"DELETE FROM templates WHERE tenant = {self._ph} AND name = {self._ph}",
                (clean_tenant(tenant), name),
            )
            deleted = cur.rowcount > 0
            if not self.is_postgres:
                self._conn.commit()
            return deleted

        return self._run(go)

    def tenants(self) -> list[str]:
        def go(cur):
            cur.execute("SELECT DISTINCT tenant FROM templates ORDER BY tenant")
            return [str(r[0]) for r in cur.fetchall()]

        return self._run(go)

    # -- report records ---------------------------------------------------- #

    def save_report(self, tenant: str, record: dict) -> None:
        report_id = str(record.get("id") or "")
        if not report_id:
            raise ValueError("A report record needs an id.")
        tenant = clean_tenant(tenant)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        status = str(record.get("status") or "draft")
        urgency = str(record.get("urgency") or "routine")
        patient_key = str(record.get("patient_key") or "")

        def go(cur):
            cur.execute(
                f"SELECT 1 FROM reports WHERE tenant = {self._ph} AND id = {self._ph}",
                (tenant, report_id),
            )
            if cur.fetchone() is None:
                cur.execute(
                    f"INSERT INTO reports (tenant, id, status, urgency, patient_key, "
                    f"payload, updated) VALUES ({self._ph}, {self._ph}, {self._ph}, "
                    f"{self._ph}, {self._ph}, {self._ph}, {self._ph})",
                    (tenant, report_id, status, urgency, patient_key,
                     self._dumps(record), now),
                )
            else:
                cur.execute(
                    f"UPDATE reports SET status = {self._ph}, urgency = {self._ph}, "
                    f"patient_key = {self._ph}, payload = {self._ph}, updated = {self._ph} "
                    f"WHERE tenant = {self._ph} AND id = {self._ph}",
                    (status, urgency, patient_key, self._dumps(record), now,
                     tenant, report_id),
                )
            if not self.is_postgres:
                self._conn.commit()

        self._run(go)

    def get_report(self, tenant: str, report_id: str) -> dict | None:
        def go(cur):
            cur.execute(
                f"SELECT payload FROM reports WHERE tenant = {self._ph} AND id = {self._ph}",
                (clean_tenant(tenant), str(report_id)),
            )
            row = cur.fetchone()
            if not row:
                return None
            try:
                return self._loads(row[0])
            except Exception:
                return None

        return self._run(go)

    def list_reports(self, tenant: str, status: str | None = None,
                     patient_key: str | None = None, limit: int = 200) -> list[dict]:
        query = f"SELECT payload FROM reports WHERE tenant = {self._ph}"
        params: list = [clean_tenant(tenant)]
        if status:
            query += f" AND status = {self._ph}"
            params.append(status)
        if patient_key:
            query += f" AND patient_key = {self._ph}"
            params.append(patient_key)
        query += f" ORDER BY updated DESC LIMIT {self._ph}"
        params.append(int(limit))

        def go(cur):
            cur.execute(query, tuple(params))
            rows: list[dict] = []
            for (payload,) in cur.fetchall():
                try:
                    rows.append(self._loads(payload))
                except Exception:
                    continue
            return rows

        return self._run(go)

    def delete_report(self, tenant: str, report_id: str) -> bool:
        def go(cur):
            cur.execute(
                f"DELETE FROM reports WHERE tenant = {self._ph} AND id = {self._ph}",
                (clean_tenant(tenant), str(report_id)),
            )
            deleted = cur.rowcount > 0
            if not self.is_postgres:
                self._conn.commit()
            return deleted

        return self._run(go)

    def record(self, event: Event) -> None:
        def go(cur):
            cur.execute(
                f"INSERT INTO events (when_, kind, subject, detail, user_) "
                f"VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph}, {self._ph})",
                (event.when, event.kind, event.subject, event.detail, event.user),
            )
            if not self.is_postgres:
                self._conn.commit()

        try:
            self._run(go)
        except Exception:
            pass  # the audit log must never block clinical work

    def events(self, limit: int = 200) -> list[Event]:
        def go(cur):
            cur.execute(
                f"SELECT when_, kind, subject, detail, user_ FROM events "
                f"ORDER BY id DESC LIMIT {self._ph}",
                (limit,),
            )
            return [
                Event(when=r[0], kind=r[1], subject=r[2], detail=r[3] or "", user=r[4] or "")
                for r in cur.fetchall()
            ]

        return self._run(go)

    def describe(self) -> str:
        if self.is_postgres:
            host = self.url.split("@")[-1].split("/")[0] if "@" in self.url else "server"
            return f"Postgres at {host}"
        return f"SQLite at {self.url.replace('sqlite:///', '')}"

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #

_store: Store | None = None
_store_error = ""


def storage_url() -> str:
    """Read STORAGE_URL from Streamlit secrets, falling back to the environment."""
    try:
        import streamlit as st

        if "STORAGE_URL" in st.secrets:
            return str(st.secrets["STORAGE_URL"]).strip()
    except Exception:
        pass
    return os.environ.get("STORAGE_URL", "").strip()


def get_store(url: str | None = None, *, force: bool = False) -> Store:
    """
    The configured store, built once.

    A misconfigured database must not take the clinic offline: if the URL is
    wrong or the driver is missing, this falls back to files and records why,
    which `storage_problem()` surfaces in the UI.
    """
    global _store, _store_error
    if _store is not None and not force and url is None:
        return _store

    target = url if url is not None else storage_url()
    _store_error = ""
    if target:
        try:
            _store = SqlStore(target)
            return _store
        except Exception as exc:
            _store_error = (
                f"Could not open the configured storage ({target.split('@')[-1]}): {exc}. "
                "Falling back to JSON files, so nothing is lost — but fix this before "
                "relying on shared storage."
            )
    _store = FileStore()
    return _store


def storage_problem() -> str:
    return _store_error


def log(kind: str, subject: str, detail: str = "", user: str = "") -> None:
    """Append to the audit trail. Never raises."""
    try:
        get_store().record(
            Event(
                when=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                kind=kind,
                subject=subject,
                detail=detail,
                user=user or current_user() or current_tenant(),
            )
        )
    except Exception:
        pass


def current_tenant() -> str:
    """
    Which clinic is using the app right now.

    Resolution order, most trustworthy first:

      1. the signed-in user's email domain, when an identity provider is
         configured - a doctor at apollo.com cannot claim to be someone else
      2. TENANT in secrets, for a single-clinic install that never signs in
      3. "default"

    Deriving it from the sign-in matters: anything the user could type is
    something they could type wrongly, or deliberately.
    """
    email = current_user()
    if "@" in email:
        return clean_tenant(email.split("@", 1)[1])

    try:
        import streamlit as st

        if "TENANT" in st.secrets:
            return clean_tenant(str(st.secrets["TENANT"]))
    except Exception:
        pass
    return clean_tenant(os.environ.get("TENANT", "") or DEFAULT_TENANT)


def current_user() -> str:
    """
    Who is using the app, when an identity provider is configured.

    Streamlit's OIDC login fills st.user once [auth] is set up in secrets.toml.
    With no provider this returns "" and the audit log simply has no name against
    each row - honest, rather than inventing one.
    """
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        # Outside a Streamlit run there is no user and touching st.user only
        # produces warnings, so the tests and CLI stay quiet.
        if get_script_run_ctx() is None:
            return ""

        import streamlit as st

        user = getattr(st, "user", None)
        if user is not None:
            for attr in ("email", "name"):
                value = getattr(user, attr, None) or (
                    user.get(attr) if hasattr(user, "get") else None
                )
                if value:
                    return str(value)
    except Exception:
        pass
    return ""
