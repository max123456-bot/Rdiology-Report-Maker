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


class Store(Protocol):
    """What the app needs from persistence. Deliberately small."""

    def load_all(self) -> dict[str, dict]: ...
    def save(self, name: str, payload: dict, expect: str | None = None) -> None: ...
    def delete(self, name: str) -> bool: ...
    def fingerprint(self, name: str) -> str: ...
    def record(self, event: Event) -> None: ...
    def events(self, limit: int = 200) -> list[Event]: ...
    def describe(self) -> str: ...


# --------------------------------------------------------------------------- #
# JSON files - the default
# --------------------------------------------------------------------------- #


class FileStore:
    """
    One JSON file per template, exactly as the app has always worked.

    Kept as the default because it is inspectable, diffable, trivially backed up
    by copying a folder, and needs nothing installed. Its limits are real though:
    no transactions, and an ephemeral filesystem loses everything.
    """

    def __init__(self, directory: str | None = None) -> None:
        self.dir = directory or os.path.join(HERE, "templates")
        self.backups = os.path.join(self.dir, "_backups")
        self.events_path = os.path.join(self.dir, "_events.jsonl")
        self._lock = threading.Lock()

    # -- helpers ---------------------------------------------------------- #

    def _slug(self, name: str) -> str:
        import hashlib
        import re

        stem = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "template"
        digest = hashlib.sha1(name.strip().encode("utf-8")).hexdigest()[:8]
        return f"{stem[:60]}-{digest}.json"

    def _path(self, name: str) -> str:
        return os.path.join(self.dir, self._slug(name))

    def _find(self, name: str) -> str | None:
        exact = self._path(name)
        if os.path.exists(exact):
            return exact
        if not os.path.isdir(self.dir):
            return None
        for filename in os.listdir(self.dir):
            if not filename.endswith(".json"):
                continue
            candidate = os.path.join(self.dir, filename)
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

    def load_all(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        if not os.path.isdir(self.dir):
            return out
        for filename in sorted(os.listdir(self.dir)):
            if not filename.endswith(".json") or filename.endswith(".tmp"):
                continue
            path = os.path.join(self.dir, filename)
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

    def fingerprint(self, name: str) -> str:
        """
        A hash of the file's contents.

        Not mtime and size: Windows file timestamps are coarse enough that twenty
        rapid writes can share one mtime, and two saves of similar-length JSON
        share a size. Together that made two concurrent edits look identical, so
        a stale write was accepted and silently clobbered the newer one. Hashing
        the bytes is exact and costs well under a millisecond for a template.
        """
        import hashlib

        path = self._find(name)
        if not path or not os.path.exists(path):
            return ""
        try:
            with open(path, "rb") as fh:
                return hashlib.sha1(fh.read()).hexdigest()
        except OSError:
            return ""

    def save(self, name: str, payload: dict, expect: str | None = None) -> None:
        with self._lock:
            if expect is not None:
                current = self.fingerprint(name)
                if current and current != expect:
                    raise ConflictError(
                        f"“{name}” was changed somewhere else while you had it open. "
                        "Reload it, then reapply your edit so neither change is lost."
                    )
            os.makedirs(self.dir, exist_ok=True)
            target = self._path(name)
            self._back_up(target)
            tmp = target + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, target)

    def delete(self, name: str) -> bool:
        with self._lock:
            path = self._find(name)
            if path and os.path.exists(path):
                self._back_up(path)
                os.remove(path)
                return True
            return False

    def record(self, event: Event) -> None:
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(self.events_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.__dict__, ensure_ascii=False) + "\n")
        except OSError:
            pass  # the audit log must never block clinical work

    def events(self, limit: int = 200) -> list[Event]:
        if not os.path.exists(self.events_path):
            return []
        rows: list[Event] = []
        try:
            with open(self.events_path, encoding="utf-8") as fh:
                lines = fh.readlines()[-limit:]
        except OSError:
            return []
        for line in lines:
            try:
                rows.append(Event(**json.loads(line)))
            except Exception:
                continue
        return list(reversed(rows))

    def describe(self) -> str:
        return f"JSON files in {os.path.basename(self.dir)}/"


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
            self._conn = psycopg.connect(
                self.url, autocommit=True, connect_timeout=CONNECT_TIMEOUT
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
                        name      TEXT PRIMARY KEY,
                        payload   {json_type} NOT NULL,
                        version   INTEGER NOT NULL DEFAULT 1,
                        updated   TEXT NOT NULL
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
            if not self.is_postgres:
                self._conn.commit()

    def _dumps(self, payload: dict) -> str:
        return json.dumps(payload, ensure_ascii=False)

    def _loads(self, value) -> dict:
        return value if isinstance(value, dict) else json.loads(value)

    # -- Store ------------------------------------------------------------ #

    def load_all(self) -> dict[str, dict]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT name, payload FROM templates")
            out = {}
            for name, payload in cur.fetchall():
                try:
                    out[str(name)] = self._loads(payload)
                except Exception:
                    continue
            return out

    def fingerprint(self, name: str) -> str:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(f"SELECT version FROM templates WHERE name = {self._ph}", (name,))
            row = cur.fetchone()
            return str(row[0]) if row else ""

    def save(self, name: str, payload: dict, expect: str | None = None) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(f"SELECT version FROM templates WHERE name = {self._ph}", (name,))
            row = cur.fetchone()
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")

            if row is None:
                cur.execute(
                    f"INSERT INTO templates (name, payload, version, updated) "
                    f"VALUES ({self._ph}, {self._ph}, 1, {self._ph})",
                    (name, self._dumps(payload), now),
                )
            else:
                current = str(row[0])
                if expect is not None and expect and expect != current:
                    raise ConflictError(
                        f"“{name}” was changed somewhere else while you had it open. "
                        "Reload it, then reapply your edit so neither change is lost."
                    )
                # Guarding on the version in the WHERE clause means two simultaneous
                # saves cannot both succeed - the loser updates no rows.
                cur.execute(
                    f"UPDATE templates SET payload = {self._ph}, version = version + 1, "
                    f"updated = {self._ph} WHERE name = {self._ph} AND version = {self._ph}",
                    (self._dumps(payload), now, name, int(current)),
                )
                if cur.rowcount == 0:
                    raise ConflictError(
                        f"“{name}” was saved by someone else a moment ago. Reload it and "
                        "reapply your edit."
                    )
            if not self.is_postgres:
                self._conn.commit()

    def delete(self, name: str) -> bool:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(f"DELETE FROM templates WHERE name = {self._ph}", (name,))
            deleted = cur.rowcount > 0
            if not self.is_postgres:
                self._conn.commit()
            return deleted

    def record(self, event: Event) -> None:
        try:
            with self._lock:
                cur = self._conn.cursor()
                cur.execute(
                    f"INSERT INTO events (when_, kind, subject, detail, user_) "
                    f"VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph}, {self._ph})",
                    (event.when, event.kind, event.subject, event.detail, event.user),
                )
                if not self.is_postgres:
                    self._conn.commit()
        except Exception:
            pass  # the audit log must never block clinical work

    def events(self, limit: int = 200) -> list[Event]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                f"SELECT when_, kind, subject, detail, user_ FROM events "
                f"ORDER BY id DESC LIMIT {self._ph}",
                (limit,),
            )
            return [
                Event(when=r[0], kind=r[1], subject=r[2], detail=r[3] or "", user=r[4] or "")
                for r in cur.fetchall()
            ]

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
                user=user or current_user(),
            )
        )
    except Exception:
        pass


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
