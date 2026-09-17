#!/usr/bin/env python3
"""
db-checker - the small local database behind sign-in and run history.

SQLite, because sqlite3 ships with Python. The whole point of this tool is that
there is nothing to pip install and nothing that breaks when Python updates, and
adding a driver to store an audit log would have traded that away for very little.

It holds four things:

    users         who can sign in
    sessions      who is signed in right now
    events        sign-ins, sign-outs, and failed attempts
    runs          every time the checks ran, from any of the four callers

WHAT THIS IS AND IS NOT FOR

The app binds to 127.0.0.1, so sign-in is not keeping an attacker out - anyone who
can reach the page already has the machine, and servers.json on that machine holds
the database passwords in plaintext. Sign-in is here so the history can say WHO,
and so the same tool can later be shared without a rewrite.

Passwords are still hashed properly (PBKDF2-HMAC-SHA256, per-user salt) rather
than stored in the clear. People reuse passwords, so a throwaway local tool
storing one in plaintext is a real risk to them somewhere else - the fact that
this app is not worth attacking does not make the password worthless.
"""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("DBC_DB") or (HERE / "db-checker.sqlite3"))

SESSION_HOURS = 12
PBKDF2_ROUNDS = 240_000

# One writer at a time. SQLite handles concurrency itself, but db_checker.py can be
# running from the scheduler while someone clicks in the app, and a lock timeout
# would surface as a failed health check - which is a silly way to lose a run.
_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    password_hash BLOB NOT NULL,
    salt          BLOB NOT NULL,
    rounds        INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    username   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_username ON sessions(username);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    username TEXT,
    kind     TEXT NOT NULL,   -- login | logout | login_failed | account_created
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_at ON events(at DESC);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    seconds      REAL,
    source       TEXT NOT NULL,   -- app | cli | schedule | mcp | unknown
    username     TEXT,            -- null for runs with no session behind them
    server       TEXT,
    database     TEXT,
    critical     INTEGER DEFAULT 0,
    warn         INTEGER DEFAULT 0,
    info         INTEGER DEFAULT 0,
    new_count    INTEGER DEFAULT 0,
    fixed_count  INTEGER DEFAULT 0,
    errored      INTEGER DEFAULT 0,   -- checks that could not run
    error        TEXT,                -- set when the run itself failed
    detail       TEXT                 -- per-check counts, JSON
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        # WAL so a long check run writing its record cannot block the app from
        # reading history, which is the one contention this tool actually has.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    with _write_lock, connect() as conn:
        conn.executescript(SCHEMA)


# ----------------------------------------------------------------- passwords


def hash_password(password, salt=None, rounds=PBKDF2_ROUNDS):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return digest, salt, rounds


def check_password(password, digest, salt, rounds):
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    # compare_digest, not ==, so the comparison does not leak how much matched.
    return hmac.compare_digest(candidate, digest)


# --------------------------------------------------------------------- users


def user_count():
    init()
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def create_user(username, password):
    """Returns None on success, or a reason it was refused."""
    username = (username or "").strip()
    if not username:
        return "Username cannot be empty."
    if len(password or "") < 8:
        return "Password must be at least 8 characters."

    init()
    digest, salt, rounds = hash_password(password)
    with _write_lock, connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (username,)
        ).fetchone()
        if exists:
            return f'There is already an account called "{username}".'
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, rounds, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, digest, salt, rounds, now_iso()),
        )
        conn.execute(
            "INSERT INTO events (at, username, kind, detail) VALUES (?, ?, ?, ?)",
            (now_iso(), username, "account_created", None),
        )
    return None


def verify_user(username, password):
    init()
    username = (username or "").strip()
    with connect() as conn:
        row = conn.execute(
            "SELECT password_hash, salt, rounds FROM users WHERE username = ?",
            (username,),
        ).fetchone()

    # Hash even when the user does not exist, so a wrong username and a wrong
    # password take the same time and neither can be told apart from outside.
    if row is None:
        hash_password(password or "")
        return False
    return check_password(password or "", row["password_hash"], row["salt"], row["rounds"])


# ------------------------------------------------------------------ sessions


def start_session(username, detail=None):
    init()
    token = secrets.token_urlsafe(32)
    created = datetime.now(timezone.utc)
    expires = created + timedelta(hours=SESSION_HOURS)
    with _write_lock, connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token, username, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (token, username, created.isoformat(), expires.isoformat()),
        )
        conn.execute(
            "INSERT INTO events (at, username, kind, detail) VALUES (?, ?, ?, ?)",
            (created.isoformat(), username, "login", detail),
        )
        conn.execute("UPDATE users SET last_login_at = ? WHERE username = ?",
                     (created.isoformat(), username))
        _purge_expired(conn)
    return token


def session_user(token):
    """Username for a live session, or None. Expired sessions are removed."""
    if not token:
        return None
    init()
    with connect() as conn:
        row = conn.execute(
            "SELECT username, expires_at FROM sessions WHERE token = ?", (token,)
        ).fetchone()
        if row is None:
            return None
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            return None
        return row["username"]


def end_session(token, detail=None):
    if not token:
        return None
    init()
    with _write_lock, connect() as conn:
        row = conn.execute(
            "SELECT username FROM sessions WHERE token = ?", (token,)
        ).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.execute(
            "INSERT INTO events (at, username, kind, detail) VALUES (?, ?, ?, ?)",
            (now_iso(), row["username"], "logout", detail),
        )
        return row["username"]


def record_failed_login(username, detail=None):
    init()
    with _write_lock, connect() as conn:
        conn.execute(
            "INSERT INTO events (at, username, kind, detail) VALUES (?, ?, ?, ?)",
            (now_iso(), (username or "").strip() or None, "login_failed", detail),
        )


def _purge_expired(conn):
    conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_iso(),))


# ---------------------------------------------------------------------- runs


def start_run(source, server=None, database=None, username=None):
    init()
    with _write_lock, connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (started_at, source, username, server, database) "
            "VALUES (?, ?, ?, ?, ?)",
            (now_iso(), source, username, server, database),
        )
        return cur.lastrowid


def finish_run(run_id, results=None, diff=None, error=None):
    """Close out a run. Never raises - a failed audit write must not fail a check."""
    if run_id is None:
        return
    try:
        counts = {"critical": 0, "warn": 0, "info": 0}
        errored = 0
        detail = []
        for r in results or []:
            n = len(r.get("findings") or [])
            if r.get("error"):
                errored += 1
            if n:
                sev = r.get("severity", "info")
                counts[sev] = counts.get(sev, 0) + n
            detail.append(
                {"check": r.get("name"), "findings": n, "error": bool(r.get("error"))}
            )

        new_count = fixed_count = 0
        if diff and not diff[0]:  # diff = (first_run, prev_at, new_ids, fixed_ids)
            new_count, fixed_count = len(diff[2]), len(diff[3])

        with _write_lock, connect() as conn:
            row = conn.execute(
                "SELECT started_at FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            seconds = None
            if row:
                started = datetime.fromisoformat(row["started_at"])
                seconds = round(
                    (datetime.now(timezone.utc) - started).total_seconds(), 2
                )
            conn.execute(
                "UPDATE runs SET finished_at = ?, seconds = ?, critical = ?, warn = ?, "
                "info = ?, new_count = ?, fixed_count = ?, errored = ?, error = ?, "
                "detail = ? WHERE id = ?",
                (
                    now_iso(),
                    seconds,
                    counts.get("critical", 0),
                    counts.get("warn", 0),
                    counts.get("info", 0),
                    new_count,
                    fixed_count,
                    errored,
                    error,
                    json.dumps(detail),
                    run_id,
                ),
            )
    except Exception:
        # Deliberately swallowed. The checks are the product; the history is a
        # convenience, and a locked database or a full disk must not turn a
        # working health check into a failed one.
        pass


# -------------------------------------------------------------------- history


def recent_runs(limit=50):
    init()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def recent_events(limit=50):
    init()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def active_sessions():
    init()
    with connect() as conn:
        _purge_expired(conn)
        rows = conn.execute(
            "SELECT username, created_at, expires_at FROM sessions "
            "ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    import sys

    init()
    if len(sys.argv) > 1 and sys.argv[1] == "adduser":
        import getpass

        name = input("Username: ").strip()
        pw = getpass.getpass("Password (min 8 chars): ")
        again = getpass.getpass("Again: ")
        if pw != again:
            print("Those do not match.")
            sys.exit(1)
        problem = create_user(name, pw)
        print(problem or f'Created "{name}".')
    else:
        print(f"Database: {DB_PATH}")
        print(f"Users:    {user_count()}")
        print(f"Runs:     {len(recent_runs(10_000))}")
        print(f"Sessions: {len(active_sessions())} active")
