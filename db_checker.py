#!/usr/bin/env python3
"""
db-checker - read-only health checks for the databases MyApp talks to.

Finds the kind of problem that never raises an error: a large table with no index,
a table with no primary key, statistics that were never gathered, an export folder
that has been silently unusable for weeks, a scheduler that quietly stopped.

It NEVER fixes anything. Every fix is printed for a person to run. The tool that
finds a missing index must not be the tool that builds it - an index build locks or
loads a production table for minutes, and that decision needs someone who knows what
else is running right now.

No pip packages required. It drives psql, which is already installed and configured
on these machines, instead of pulling in a database driver.

    python db_checker.py                    read config from .env
    python db_checker.py --json             machine-readable, for scheduled runs
    python db_checker.py --only unindexed_tables
"""

import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPORTS_DIR = HERE / "reports"

# History is a convenience, the checks are the product. If store.py is missing or
# its database cannot be opened, every run still works - it just is not recorded.
try:
    import store as _store
except Exception:
    _store = None

BIG_TABLE_ROWS = 100_000
UNUSED_INDEX_MIN_BYTES = 10 * 1024 * 1024

# Server-side cap on any single query, and the outer cap on the psql process. The
# outer one must be LARGER, so a query that overruns is killed by Postgres (a clean
# error naming the statement) rather than by killing psql (which tells you nothing).
STATEMENT_TIMEOUT_MS = int(os.environ.get("DBC_STATEMENT_TIMEOUT_MS", "60000"))
SQL_TIMEOUT_SECONDS = int(os.environ.get("DBC_SQL_TIMEOUT", "90"))

# How many times an error must appear in the log window before it is worth naming.
# One timeout in a week is a blip; three is a fault.
LOG_ERROR_MIN_COUNT = 3

# How many of the below-threshold errors to spell out before falling back to a count.
RARE_ERRORS_LISTED = 5

EXCLUDED_SCHEMAS = "('pg_catalog','information_schema')"

# Where psql tends to live on these machines. PSQL_PATH in .env wins over all of it.
PSQL_CANDIDATES = [
    r"D:\pgsql\bin\psql.exe",
    r"D:\pgsql18\bin\psql.exe",
    r"D:\postgresql-17.11-1-windows-x64-binaries\pgsql\bin\psql.exe",
    r"C:\Program Files\PostgreSQL\18\bin\psql.exe",
    r"C:\Program Files\PostgreSQL\17\bin\psql.exe",
    "psql",
]


# --------------------------------------------------------------------- config


def load_env(path=None):
    """Read a .env file into os.environ without overwriting real env vars.

    Deliberately tiny rather than pulling in python-dotenv: one dependency is one
    more thing that has to install cleanly on someone else's machine.
    """
    env_file = Path(path) if path else HERE / ".env"
    if not env_file.exists():
        return False
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
    return True


def find_psql():
    configured = os.environ.get("PSQL_PATH")
    candidates = ([configured] if configured else []) + PSQL_CANDIDATES
    for c in candidates:
        if not c:
            continue
        if c == "psql" or Path(c).exists():
            try:
                subprocess.run(
                    [c, "--version"], capture_output=True, check=True, timeout=15
                )
                return c
            except Exception:
                continue
    return None


# ------------------------------------------------------------------ psql glue


def run_sql(psql, sql):
    """Run one query and return a list of dicts.

    --csv gives properly quoted output, so the csv module parses it reliably -
    unlike the default aligned format, which is for humans and breaks on any value
    containing whitespace.
    """
    cmd = [
        psql,
        "--csv",
        "--no-psqlrc",
        "--quiet",
        # -w is NOT optional. Without it, psql prompts for a password whenever it
        # decides it needs one - and a prompt with nobody to answer it waits
        # FOREVER. That is unacceptable here: this runs from a web request, a
        # scheduled task and an MCP tool, none of which has a person at a keyboard.
        # With -w a missing password is an instant, readable error instead.
        "-w",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        sql,
    ]
    # Read-only is enforced by the server, not by this script trusting its own SQL.
    # PGOPTIONS is the right lever: it applies to every transaction in the session,
    # produces no output of its own (unlike wrapping the query in BEGIN READ ONLY,
    # whose command tags would land in the CSV), and it covers the custom checks
    # from checks.json, whose SQL this tool did not write.
    env = dict(os.environ)
    env["PGOPTIONS"] = (
        f'{env.get("PGOPTIONS", "")} '
        "-c default_transaction_read_only=on "
        f"-c statement_timeout={STATEMENT_TIMEOUT_MS}"
    ).strip()
    # A server that is not answering should fail in seconds, not sit in a TCP
    # connect until the subprocess timeout. Only set if the caller has not.
    env.setdefault("PGCONNECT_TIMEOUT", "10")

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=SQL_TIMEOUT_SECONDS, env=env
        )
    except subprocess.TimeoutExpired:
        # Name the timeout and show the query. A bare "it hung" tells whoever is
        # reading this nothing about which check to look at.
        raise RuntimeError(
            f"psql did not finish within {SQL_TIMEOUT_SECONDS}s. "
            f"Query started: {' '.join(sql.split())[:120]}"
        )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "psql failed").strip())
    text = proc.stdout.strip()
    if not text:
        return []
    return list(csv.DictReader(io.StringIO(text)))


def human_bytes(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    if n <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" and n < 10 else f"{n:.0f} {unit}"
        n /= 1024


def ident(schema, table):
    q = lambda s: '"' + str(s).replace('"', '""') + '"'
    return f"{q(schema)}.{q(table)}"


# -------------------------------------------------------------------- checks
# Each check returns a list of findings:
#   {object, detail, fix (SQL text or None), note}


def check_unindexed_tables(psql):
    rows = run_sql(
        psql,
        f"""
        SELECT n.nspname AS schema, c.relname AS tbl,
               c.reltuples::bigint AS approx_rows,
               EXISTS (SELECT 1 FROM pg_attribute a
                       WHERE a.attrelid = c.oid AND a.attname = 'id'
                         AND a.attnum > 0 AND NOT a.attisdropped) AS has_id
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'r'
          AND n.nspname NOT IN {EXCLUDED_SCHEMAS}
          AND c.reltuples > {BIG_TABLE_ROWS}
          AND NOT EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = c.oid)
        ORDER BY c.reltuples DESC
        """,
    )
    out = []
    for r in rows:
        has_id = str(r["has_id"]).lower() in ("t", "true")
        safe = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in r["tbl"].lower())
        out.append(
            {
                "object": f'{r["schema"]}.{r["tbl"]}',
                "detail": f'{int(float(r["approx_rows"])):,} rows, no indexes',
                # Only offer SQL when there is an obvious column. Guessing the right
                # column for someone else's table produces an index nobody uses.
                "fix": (
                    f"CREATE INDEX CONCURRENTLY idx_{safe}_id "
                    f'ON {ident(r["schema"], r["tbl"])} (id);'
                )
                if has_id
                else None,
                "note": None
                if has_id
                else 'No "id" column - someone needs to pick the column that gets filtered on.',
            }
        )
    return out


def check_tables_without_pk(psql):
    rows = run_sql(
        psql,
        f"""
        SELECT n.nspname AS schema, c.relname AS tbl,
               c.reltuples::bigint AS approx_rows
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'r'
          AND n.nspname NOT IN {EXCLUDED_SCHEMAS}
          AND c.reltuples > {BIG_TABLE_ROWS}
          AND NOT EXISTS (SELECT 1 FROM pg_constraint k
                          WHERE k.conrelid = c.oid AND k.contype = 'p')
        ORDER BY c.reltuples DESC
        """,
    )
    return [
        {
            "object": f'{r["schema"]}.{r["tbl"]}',
            "detail": f'{int(float(r["approx_rows"])):,} rows, no primary key',
            # Deliberately no copy-paste SQL. ADD PRIMARY KEY takes an ACCESS
            # EXCLUSIVE lock and rewrites the table; handing that over as a
            # one-liner invites someone to run it at 2pm on a working day.
            "fix": None,
            "note": (
                "ADD PRIMARY KEY locks the table while it builds - needs a maintenance "
                "window, unlike CREATE INDEX CONCURRENTLY. Check for duplicate or null "
                "ids first."
            ),
        }
        for r in rows
    ]


def check_stale_statistics(psql):
    rows = run_sql(
        psql,
        f"""
        SELECT schemaname AS schema, relname AS tbl, n_live_tup AS rows_live
        FROM pg_stat_user_tables
        WHERE n_live_tup > {BIG_TABLE_ROWS}
          AND last_analyze IS NULL AND last_autoanalyze IS NULL
        ORDER BY n_live_tup DESC
        """,
    )
    return [
        {
            "object": f'{r["schema"]}.{r["tbl"]}',
            "detail": f'{int(float(r["rows_live"])):,} rows, never analysed',
            "fix": f'ANALYZE {ident(r["schema"], r["tbl"])};',
            "note": "Without statistics the planner guesses, and picks bad plans even with indexes.",
        }
        for r in rows
    ]


def check_table_sizes(psql):
    rows = run_sql(
        psql,
        f"""
        SELECT n.nspname AS schema, c.relname AS tbl,
               c.reltuples::bigint AS approx_rows,
               pg_total_relation_size(c.oid) AS total_bytes,
               CASE WHEN c.reltuples > 0
                    THEN (pg_total_relation_size(c.oid) / c.reltuples)::bigint
               END AS bytes_per_row
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'r'
          AND n.nspname NOT IN {EXCLUDED_SCHEMAS}
        ORDER BY pg_total_relation_size(c.oid) DESC
        LIMIT 25
        """,
    )
    out = []
    for r in rows:
        bpr = float(r["bytes_per_row"] or 0)
        # What a 1,000,000-row export of this table would weigh. This is the number
        # that predicts an unopenable file before anyone makes one.
        million_gb = (bpr * 1_000_000) / 1024**3
        out.append(
            {
                "object": f'{r["schema"]}.{r["tbl"]}',
                "detail": (
                    f'{human_bytes(r["total_bytes"])} total, '
                    f'{int(float(r["approx_rows"])):,} rows, '
                    f"{human_bytes(bpr)}/row"
                ),
                "fix": None,
                "note": (
                    f"A 1,000,000-row export of this would be about {million_gb:.1f} GB - "
                    "no spreadsheet opens that. Split by size, or drop the wide columns."
                )
                if million_gb > 1
                else None,
            }
        )
    return out


def check_unused_indexes(psql):
    rows = run_sql(
        psql,
        f"""
        SELECT s.schemaname AS schema, s.relname AS tbl, s.indexrelname AS idx,
               pg_relation_size(s.indexrelid) AS bytes,
               i.indisprimary AS is_primary, i.indisunique AS is_unique
        FROM pg_stat_user_indexes s
        JOIN pg_index i ON i.indexrelid = s.indexrelid
        WHERE s.idx_scan = 0
          AND pg_relation_size(s.indexrelid) > {UNUSED_INDEX_MIN_BYTES}
        ORDER BY pg_relation_size(s.indexrelid) DESC
        """,
    )
    out = []
    for r in rows:
        # Never suggest touching a constraint's index, however unused it looks.
        if str(r["is_primary"]).lower() in ("t", "true"):
            continue
        if str(r["is_unique"]).lower() in ("t", "true"):
            continue
        out.append(
            {
                "object": f'{r["schema"]}.{r["idx"]}',
                "detail": f'on {r["tbl"]}, {human_bytes(r["bytes"])}, 0 scans',
                "fix": None,
                "note": (
                    "Scan counters reset when the server restarts. Do not act on this "
                    "until the server has been up for several weeks."
                ),
            }
        )
    return out


# ------------------------------------------------------- checks over the logs
# database/logs/ is plaintext by design. The rest of database/ is JSONLOCK
# ciphertext and must never be parsed directly.


def _read_log(logs_dir, window_days):
    """Entries from sync.log. window_days=None reads the whole file.

    The whole file matters for the scheduler check: 'it used to run and stopped'
    is a real finding, while 'it was never configured' is not, and you cannot
    tell those apart by looking only at the last seven days.
    """
    path = Path(logs_dir) / "sync.log"
    if not path.exists():
        return None
    since = (
        datetime.min.replace(tzinfo=timezone.utc)
        if window_days is None
        else datetime.now(timezone.utc) - timedelta(days=window_days)
    )
    entries = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue  # partial write at the tail, or a rotation artefact
            ts = obj.get("timestamp")
            try:
                when = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except Exception:
                continue
            if when >= since:
                obj["_when"] = when
                entries.append(obj)
    return entries


def check_export_folder(logs_dir, window_days):
    entries = _read_log(logs_dir, window_days)
    if entries is None:
        return [
            {
                "object": "sync.log",
                "detail": "Log file not found - export status UNKNOWN",
                "fix": None,
                "note": f"Looked in {logs_dir}",
            }
        ]

    hits = [e for e in entries if "cannot be used" in str(e.get("message", ""))]
    if not hits:
        return []

    # One finding with a count, not one per export. Twenty identical lines is how a
    # report teaches people to stop reading it.
    last = str(hits[-1].get("message", ""))
    configured = last.split('folder "')[1].split('"')[0] if 'folder "' in last else "unknown"
    actual = last.split('written to "')[1].split('"')[0] if 'written to "' in last else None

    # WHEN it last happened, not just how often.
    #
    # "5 exports in 7 days" reads as "this is happening now", and a count alone
    # cannot tell a live fault from one that stopped days ago and is still inside
    # the window. That difference is the whole question - one needs fixing, the
    # other needs nothing - and reading it wrong costs someone an afternoon.
    last_when = hits[-1].get("_when")
    age_days = None
    if last_when is not None:
        age_days = (datetime.now(timezone.utc) - last_when).days
        stamp = last_when.astimezone().strftime("%Y-%m-%d %H:%M")
        when = f"last on {stamp}"
        when += " (today)" if age_days == 0 else f" ({age_days} day(s) ago)"
    else:
        when = "timing unknown"

    note = (
        "Fix in V-ExportTools > Settings - though the setting has no field there, so "
        "it has to go through the API or backend/modules/v-exporttools/settings.js. "
        "It is stored per install, so a path carried over from another machine fails "
        "on this one."
    )
    # Nothing since yesterday means it is already over; say so rather than leaving
    # someone to fix a setting that is already correct.
    if age_days is not None and age_days >= 2:
        note = (
            f"Nothing in the last {age_days} days - this looks already resolved, and "
            f"will drop off the report once it leaves the {window_days}-day window. "
            f"Check the current setting before changing anything."
        )

    return [
        {
            "object": configured,
            "detail": (
                f"{len(hits)} export(s) in {window_days} days could not use the "
                f"configured folder, {when}"
                + (f"; written to {actual} instead" if actual else "")
            ),
            "fix": None,
            "note": note,
        }
    ]


def check_scheduler_health(logs_dir, window_days):
    entries = _read_log(logs_dir, window_days)
    if entries is None:
        return [
            {
                "object": "sync.log",
                "detail": "Log file not found - scheduler state UNKNOWN",
                "fix": None,
                "note": f"Looked in {logs_dir}",
            }
        ]

    findings = []
    history = _read_log(logs_dir, None) or []

    # "It used to run and stopped" is a finding. "It was never configured" is not.
    # Reporting the second one every week is how a report earns being ignored -
    # so a scheduler with no history at all is passed over in silence.
    def scheduler_state(match, label, note):
        recent = [e for e in entries if match(str(e.get("message", "")))]
        if recent:
            return None  # running, nothing to say
        ever = [e for e in history if match(str(e.get("message", "")))]
        if not ever:
            return None  # never ran - not configured, not a problem
        last = max(ever, key=lambda e: e["_when"])
        gap = (datetime.now(timezone.utc) - last["_when"]).days
        return {
            "object": label,
            "detail": (
                f"Ran until {last['_when'].astimezone().strftime('%Y-%m-%d %H:%M')} "
                f"({gap} days ago), nothing since"
            ),
            "fix": None,
            "note": note,
        }

    mirror = scheduler_state(
        lambda m: "[mirror-scheduler]" in m,
        "v-mirror scheduler",
        "It ticks every 30s while running, so a gap this long means it stopped.",
    )
    if mirror:
        findings.append(mirror)

    backup = scheduler_state(
        lambda m: "backup" in m.lower(),
        "v-backup scheduler",
        (
            "This scheduler discards tick errors without logging "
            "(tick().catch(() => {})), so it can fail on every tick and leave a clean "
            "log. Check database/backup/schedule_state.json is advancing."
        ),
    )
    if backup:
        findings.append(backup)

    return findings


def check_log_errors(logs_dir, window_days):
    """Errors that keep coming back in sync.log.

    Split out of scheduler_health, where it did not belong: a connection timeout
    is not a stopped scheduler, and showing it under that heading at CRITICAL made
    one bad minute look like a dead job.

    Only repeats are reported. Something that happened once in a week is usually a
    blip - a machine asleep, a cable, a restart - and reporting it teaches people
    to skim past the section that also holds the real faults. One-offs are counted
    at the end so nothing is actually hidden.
    """
    entries = _read_log(logs_dir, window_days)
    if entries is None:
        raise RuntimeError(f"No sync.log in {logs_dir}")

    grouped = {}
    for e in entries:
        if e.get("level") != "error":
            continue
        key = str(e.get("message", ""))[:160]
        grouped[key] = grouped.get(key, 0) + 1

    findings = []
    rare = []
    for msg, count in sorted(grouped.items(), key=lambda kv: -kv[1]):
        if count < LOG_ERROR_MIN_COUNT:
            rare.append((msg, count))
            continue
        findings.append(
            {
                "object": "sync.log",
                "detail": f"{count}x in {window_days} days: {msg}",
                "fix": None,
                "note": None,
            }
        )

    # Name the rare ones rather than only counting them.
    #
    # An earlier version said "1 other error occurred and is not listed", which is
    # the worst of both: it costs the same line, tells you something happened, then
    # makes you open the log to find out it was the same harmless timeout as last
    # week. Quiet should mean short, not withheld.
    for msg, count in rare[:RARE_ERRORS_LISTED]:
        findings.append(
            {
                "object": "sync.log",
                "detail": f"{count}x (under the {LOG_ERROR_MIN_COUNT}x threshold): {msg[:120]}",
                "fix": None,
                "note": None,
            }
        )

    hidden = len(rare) - RARE_ERRORS_LISTED
    if hidden > 0:
        # A log full of distinct one-offs is its own kind of noise, so there is
        # still a cap - but it names most of them first.
        findings.append(
            {
                "object": "sync.log",
                "detail": f"and {hidden} more one-off error(s) - open sync.log for the rest",
                "fix": None,
                "note": None,
            }
        )

    return findings


# ------------------------------------------------------------ run-to-run diff
# A report that says the same twenty things every week trains people to stop
# reading it. What is worth someone's attention is what CHANGED: what is new,
# and what got fixed. So each run remembers its findings and the next one
# compares against them.

STATE_FILE = REPORTS_DIR / "_state.json"


def _fingerprints(results):
    """A stable id per finding, so the same problem matches across runs."""
    out = set()
    for r in results:
        for f in r.get("findings", []):
            out.add(f'{r["name"]}|{f["object"]}')
    return out


def load_previous(db_label):
    if not STATE_FILE.exists():
        return None
    try:
        blob = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    entry = blob.get(db_label)
    if not entry:
        return None
    return entry


def save_current(db_label, results):
    REPORTS_DIR.mkdir(exist_ok=True)
    blob = {}
    if STATE_FILE.exists():
        try:
            blob = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            blob = {}
    # Keyed by database, so several servers can share one state file without
    # one overwriting another's history.
    blob[db_label] = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "fingerprints": sorted(_fingerprints(results)),
    }
    STATE_FILE.write_text(json.dumps(blob, indent=2), encoding="utf-8")


def diff_against_previous(db_label, results):
    """Returns (first_run, previous_ran_at, new_ids, fixed_ids)."""
    prev = load_previous(db_label)
    now = _fingerprints(results)
    if not prev:
        return True, None, set(), set()
    before = set(prev.get("fingerprints", []))
    return False, prev.get("ran_at"), now - before, before - now


def describe(fid):
    """'check|object' -> something readable."""
    check, _, obj = fid.partition("|")
    return f"{obj}  ({check})"


DB_CHECKS = [
    ("unindexed_tables", "warn", "Large tables with no index at all", check_unindexed_tables),
    ("tables_without_pk", "warn", "Tables with no primary key", check_tables_without_pk),
    ("stale_statistics", "warn", "Large tables never analysed", check_stale_statistics),
    ("table_sizes", "info", "Largest tables, and bytes per row", check_table_sizes),
    ("unused_indexes", "info", "Large indexes never read", check_unused_indexes),
]

LOG_CHECKS = [
    # warn, not critical: a fallback folder means exports still succeed, they just
    # land somewhere nobody is looking. Worth fixing, not worth an alarm - and a
    # severity that overstates the problem is how a report loses its credibility.
    ("export_folder", "warn", "Exports falling back to another folder", check_export_folder),
    ("scheduler_health", "critical", "Schedulers that stopped running", check_scheduler_health),
    ("log_errors", "warn", "Errors repeating in sync.log", check_log_errors),
]

SEVERITY_RANK = {"critical": 0, "warn": 1, "info": 2}


# ------------------------------------------------------------- custom checks
# checks.json lets a check be added or switched off without editing this file.
#
# A check is data, not code. That is the whole point: adding one is writing a
# SELECT into a config file, which can be reviewed, reverted, and cannot break
# the checks that already work. It also means the MCP server can add and remove
# checks on request without ever editing Python.

CHECKS_FILE = HERE / "checks.json"

# Anything that could change the database. The server-side read-only transaction
# (see run_sql) is the real guard; this exists to fail early with a clear reason
# instead of a Postgres permission error nobody can act on.
FORBIDDEN_SQL = (
    "insert", "update", "delete", "truncate", "drop", "alter", "create",
    "grant", "revoke", "copy", "vacuum", "reindex", "cluster", "refresh",
    "call", "do", "commit", "rollback", "begin", "savepoint", "set",
)


def load_checks_config():
    """{'disabled': [...], 'checks': [...]}. A missing or broken file is not fatal.

    A syntax error in checks.json must not stop the built-in checks from running -
    the file is optional, so the failure is reported as a finding and everything
    else carries on.
    """
    if not CHECKS_FILE.exists():
        return {"disabled": [], "checks": [], "error": None}
    try:
        data = json.loads(CHECKS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"disabled": [], "checks": [], "error": f"checks.json is not valid JSON: {exc}"}
    return {
        "disabled": list(data.get("disabled") or []),
        "checks": list(data.get("checks") or []),
        "error": None,
    }


def validate_custom_sql(sql):
    """Return an error string, or None if the SQL looks like a single read-only query."""
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        return "sql is empty"
    if ";" in text:
        return "sql must be a single statement (no semicolons)"
    # Strip comments before keyword-matching, so "-- update the docs" is not a
    # forbidden word and "/* */ DELETE" cannot hide behind one.
    stripped = re.sub(r"--[^\n]*", " ", text)
    stripped = re.sub(r"/\*.*?\*/", " ", stripped, flags=re.S)
    if not re.match(r"^\s*(select|with)\b", stripped, re.I):
        return "sql must start with SELECT or WITH"
    for word in FORBIDDEN_SQL:
        if re.search(rf"\b{word}\b", stripped, re.I):
            return f"sql must not contain {word.upper()}"
    return None


def run_custom_check(psql, spec):
    """Run one checks.json entry.

    The query must return a column named "object" - what the finding is about.
    Every other column becomes part of the detail line, so the shape of the
    output is decided by the SELECT rather than by extra configuration.
    """
    problem = validate_custom_sql(spec.get("sql"))
    if problem:
        raise RuntimeError(problem)

    rows = run_sql(psql, spec["sql"].strip().rstrip(";"))
    fix_template = spec.get("fix")
    out = []
    for r in rows:
        if "object" not in r:
            raise RuntimeError('the query must return a column named "object"')
        rest = {k: v for k, v in r.items() if k != "object"}
        detail = ", ".join(f"{k}: {v}" for k, v in rest.items()) or "-"
        fix = None
        if fix_template:
            try:
                fix = fix_template.format(**r)
            except (KeyError, IndexError):
                # A placeholder naming a column the query does not return is a
                # config mistake; show the finding without a fix rather than
                # losing the whole check to it.
                fix = None
        out.append(
            {
                "object": str(r["object"]),
                "detail": detail,
                "fix": fix,
                "note": spec.get("note"),
            }
        )
    return out


def active_checks():
    """(db_checks, log_checks, custom_checks, config_error) after checks.json."""
    cfg = load_checks_config()
    off = {str(n).strip().lower() for n in cfg["disabled"]}
    db = [c for c in DB_CHECKS if c[0] not in off]
    logs = [c for c in LOG_CHECKS if c[0] not in off]
    custom = [
        c
        for c in cfg["checks"]
        if c.get("name")
        and c.get("enabled", True)
        and str(c["name"]).strip().lower() not in off
    ]
    return db, logs, custom, cfg["error"]


# -------------------------------------------------------------------- report


def build_report(db_label, results, logs_checked, diff=None):
    lines = []
    bar = "=" * 72
    lines += [bar, f"DB CHECKER   {db_label}", datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"), bar]

    if diff:
        first, prev_at, new_ids, fixed_ids = diff
        lines.append("")
        if first:
            lines.append("SINCE LAST RUN: this is the first run - nothing to compare yet.")
        elif not new_ids and not fixed_ids:
            lines.append(f"SINCE LAST RUN ({prev_at[:16]}): no change.")
        else:
            lines.append(f"SINCE LAST RUN ({prev_at[:16]}):")
            for fid in sorted(new_ids):
                lines.append(f"  NEW    {describe(fid)}")
            for fid in sorted(fixed_ids):
                lines.append(f"  FIXED  {describe(fid)}")

    ordered = sorted(results, key=lambda r: SEVERITY_RANK.get(r["severity"], 9))
    actionable = [r for r in ordered if r["findings"] and r["severity"] != "info"]
    info = [r for r in ordered if r["findings"] and r["severity"] == "info"]
    clean = [r for r in ordered if not r["findings"] and not r.get("error")]
    failed = [r for r in ordered if r.get("error")]

    if not actionable:
        lines += ["", "Nothing actionable found."]

    for r in actionable:
        lines += ["", f'[{r["severity"].upper()}] {r["title"]}  ({len(r["findings"])})', ""]
        for f in r["findings"]:
            lines.append(f'  - {f["object"]}')
            lines.append(f'      {f["detail"]}')
            if f.get("note"):
                lines.append(f'      note: {f["note"]}')
            if f.get("fix"):
                lines.append(f'      fix:  {f["fix"]}')

    for r in info:
        lines += ["", f'[INFO] {r["title"]}']
        for f in r["findings"][:10]:
            lines.append(f'  - {f["object"]}   {f["detail"]}')
            if f.get("note"):
                lines.append(f'      {f["note"]}')
        if len(r["findings"]) > 10:
            lines.append(f'  ... {len(r["findings"]) - 10} more')

    if clean:
        lines += ["", "Clean: " + ", ".join(r["name"] for r in clean)]
    if failed:
        lines += ["", "Checks that could not run:"]
        lines += [f'  - {r["name"]}: {r["error"]}' for r in failed]

    # An unrun check is not a passed check. Say so, rather than letting a missing
    # section read as good news.
    if not logs_checked:
        lines += [
            "",
            "Log checks were SKIPPED (export folder, scheduler health).",
            "  Set APP_LOGS_DIR in .env to include them.",
        ]

    lines += ["", bar, "Fixes are printed, never applied. Review before running anything.", bar]
    return "\n".join(lines)


# ----------------------------------------------------------------- html report


def _esc(s):
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


HTML_CSS = """
:root{
  --bg:#f5f6f8; --card:#fff; --ink:#16181d; --muted:#6b7280; --line:#e5e7eb;
  --crit:#c0392b; --crit-bg:#fdecea; --warn:#a05a00; --warn-bg:#fff6e5;
  --info:#3f6bef; --info-bg:#eef2ff; --mono:ui-monospace,"Cascadia Mono",Consolas,monospace;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#0e1015; --card:#151821; --ink:#e8eaef; --muted:#98a0ad; --line:#242835;
    --crit:#ff7b6b; --crit-bg:#2a1512; --warn:#ffc061; --warn-bg:#2a2011;
    --info:#8ea6ff; --info-bg:#161c33;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;padding:28px 20px 60px}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13px;margin:0 0 22px;font-family:var(--mono)}
.tiles{display:flex;gap:12px;flex-wrap:wrap;margin:0 0 26px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:12px 18px;min-width:120px}
.tile .n{font-size:26px;font-weight:700;line-height:1.1;font-variant-numeric:tabular-nums}
.tile .l{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin-top:2px}
.tile.crit .n{color:var(--crit)} .tile.warn .n{color:var(--warn)} .tile.info .n{color:var(--info)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
  margin:0 0 16px;overflow:hidden}
.card>header{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;
  align-items:baseline;gap:10px;flex-wrap:wrap}
.card>header h2{font-size:15px;margin:0;font-weight:650}
.pill{font-size:10.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
  padding:3px 8px;border-radius:999px}
.pill.critical{background:var(--crit-bg);color:var(--crit)}
.pill.warn{background:var(--warn-bg);color:var(--warn)}
.pill.info{background:var(--info-bg);color:var(--info)}
.count{margin-left:auto;color:var(--muted);font-size:12.5px;font-variant-numeric:tabular-nums}
.why{padding:10px 18px 0;color:var(--muted);font-size:13px;max-width:78ch}
.rows{padding:8px 18px 16px}
.row{padding:11px 0;border-bottom:1px dashed var(--line)}
.row:last-child{border-bottom:0}
.obj{font-family:var(--mono);font-size:13.5px;font-weight:600;word-break:break-all}
.det{color:var(--ink);font-size:13px;margin-top:2px;font-variant-numeric:tabular-nums}
.note{color:var(--muted);font-size:12.5px;margin-top:4px;max-width:88ch}
.fix{margin-top:8px;display:flex;gap:8px;align-items:flex-start}
.fix code{flex:1;background:var(--bg);border:1px solid var(--line);border-radius:7px;
  padding:8px 10px;font-family:var(--mono);font-size:12.5px;overflow-x:auto;white-space:pre}
button.copy{border:1px solid var(--line);background:var(--card);color:var(--ink);
  border-radius:7px;padding:7px 11px;font-size:12px;cursor:pointer;white-space:nowrap}
button.copy:hover{background:var(--bg)}
button.more{margin:6px 18px 16px;border:1px solid var(--line);background:var(--card);
  color:var(--ink);border-radius:7px;padding:7px 12px;font-size:12.5px;cursor:pointer}
.clean{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:12px 18px;color:var(--muted);font-size:13px;margin-bottom:16px}
.clean b{color:var(--ink)}
.foot{color:var(--muted);font-size:12.5px;margin-top:24px;text-align:center}
[hidden]{display:none!important}
@media print{body{background:#fff}.card{break-inside:avoid}button{display:none}}
"""

HTML_JS = """
document.addEventListener('click', function(e){
  var c = e.target.closest('button.copy');
  if (c){
    var code = c.parentElement.querySelector('code');
    navigator.clipboard.writeText(code.textContent).then(function(){
      var t = c.textContent; c.textContent = 'Copied';
      setTimeout(function(){ c.textContent = t; }, 1200);
    }).catch(function(){ c.textContent = 'Select manually'; });
  }
  var m = e.target.closest('button.more');
  if (m){
    var box = document.getElementById(m.dataset.target);
    var hidden = box.hasAttribute('hidden');
    if (hidden) { box.removeAttribute('hidden'); m.textContent = 'Show fewer'; }
    else { box.setAttribute('hidden',''); m.textContent = m.dataset.label; }
  }
});
"""

SHOW_FIRST = 8


CHANGE_CSS = """
.chg{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:14px 18px;margin:0 0 20px}
.chg h3{margin:0 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--muted);font-weight:700}
.chg ul{margin:0;padding-left:0;list-style:none}
.chg li{padding:3px 0;font-size:13.5px;display:flex;gap:9px;align-items:baseline}
.tag{font-size:10px;font-weight:700;letter-spacing:.05em;padding:2px 7px;border-radius:999px;
  flex-shrink:0}
.tag.new{background:var(--warn-bg);color:var(--warn)}
.tag.fixed{background:var(--info-bg);color:var(--info)}
.chg .o{font-family:var(--mono);font-size:12.5px;word-break:break-all}
.chg .nc{color:var(--muted);font-size:13px}
"""


def render_changes(diff):
    if not diff:
        return ""
    first, prev_at, new_ids, fixed_ids = diff
    if first:
        return (
            "<div class='chg'><h3>Since last run</h3>"
            "<p class='nc'>First run - nothing to compare against yet. "
            "The next run will show what changed.</p></div>"
        )
    if not new_ids and not fixed_ids:
        return (
            "<div class='chg'><h3>Since last run</h3>"
            f"<p class='nc'>No change since {_esc(prev_at[:16])}.</p></div>"
        )
    items = []
    for fid in sorted(new_ids):
        items.append(
            f"<li><span class='tag new'>NEW</span><span class='o'>{_esc(describe(fid))}</span></li>"
        )
    for fid in sorted(fixed_ids):
        items.append(
            f"<li><span class='tag fixed'>FIXED</span><span class='o'>{_esc(describe(fid))}</span></li>"
        )
    return (
        "<div class='chg'><h3>Since last run"
        f" &middot; {_esc(prev_at[:16])}</h3><ul>" + "".join(items) + "</ul></div>"
    )


def build_html(db_label, results, logs_checked, diff=None):
    """A single self-contained HTML file - no CDN, no server, works offline.

    The point of this over the text report is triage at a glance: counts first,
    critical first, and every suggested fix one click from the clipboard.
    """
    ordered = sorted(results, key=lambda r: SEVERITY_RANK.get(r["severity"], 9))
    n_crit = sum(len(r["findings"]) for r in ordered if r["severity"] == "critical")
    n_warn = sum(len(r["findings"]) for r in ordered if r["severity"] == "warn")
    n_info = sum(len(r["findings"]) for r in ordered if r["severity"] == "info")

    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>DB Checker - {_esc(db_label)}</title>",
        f"<style>{HTML_CSS}{CHANGE_CSS}</style></head><body><div class='wrap'>",
        f"<h1>Database checks</h1>",
        f"<p class='sub'>{_esc(db_label)} &middot; "
        f"{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}</p>",
        "<div class='tiles'>",
        f"<div class='tile crit'><div class='n'>{n_crit}</div><div class='l'>Critical</div></div>",
        f"<div class='tile warn'><div class='n'>{n_warn}</div><div class='l'>Warnings</div></div>",
        f"<div class='tile info'><div class='n'>{n_info}</div><div class='l'>Informational</div></div>",
        "</div>",
        render_changes(diff),
    ]

    if n_crit == 0 and n_warn == 0:
        parts.append(
            "<div class='clean'><b>Nothing actionable.</b> No critical or warning "
            "findings in this run.</div>"
        )

    box_id = 0
    for r in ordered:
        if not r["findings"] and not r.get("error"):
            continue
        sev = r["severity"]
        parts.append("<section class='card'><header>")
        parts.append(f"<span class='pill {sev}'>{sev}</span>")
        parts.append(f"<h2>{_esc(r['title'])}</h2>")
        parts.append(f"<span class='count'>{len(r['findings'])} found</span>")
        parts.append("</header>")

        if r.get("error"):
            parts.append(f"<div class='why'>Check could not run: {_esc(r['error'])}</div>")

        if r.get("why"):
            parts.append(f"<div class='why'>{_esc(r['why'])}</div>")

        # When every finding carries the same note, say it once for the section
        # instead of 35 times. A note repeated down a long list stops being read,
        # and it buries the thing that actually differs between the rows.
        notes = {f.get("note") for f in r["findings"] if f.get("note")}
        shared_note = (
            notes.pop()
            if len(r["findings"]) > 1
            and len(notes) == 1
            and all(f.get("note") for f in r["findings"])
            else None
        )
        if shared_note:
            parts.append(f"<div class='why'><b>All of these:</b> {_esc(shared_note)}</div>")

        shown = r["findings"][:SHOW_FIRST]
        rest = r["findings"][SHOW_FIRST:]

        def render_rows(items):
            out = []
            for f in items:
                out.append("<div class='row'>")
                out.append(f"<div class='obj'>{_esc(f['object'])}</div>")
                out.append(f"<div class='det'>{_esc(f['detail'])}</div>")
                if f.get("note") and f.get("note") != shared_note:
                    out.append(f"<div class='note'>{_esc(f['note'])}</div>")
                if f.get("fix"):
                    out.append(
                        "<div class='fix'><code>"
                        + _esc(f["fix"])
                        + "</code><button class='copy'>Copy SQL</button></div>"
                    )
                out.append("</div>")
            return "".join(out)

        parts.append(f"<div class='rows'>{render_rows(shown)}</div>")
        if rest:
            box_id += 1
            bid = f"more{box_id}"
            label = f"Show {len(rest)} more"
            parts.append(f"<div class='rows' id='{bid}' hidden>{render_rows(rest)}</div>")
            parts.append(
                f"<button class='more' data-target='{bid}' "
                f"data-label='{label}'>{label}</button>"
            )
        parts.append("</section>")

    clean = [r["name"] for r in ordered if not r["findings"] and not r.get("error")]
    if clean:
        parts.append(
            "<div class='clean'><b>Clean:</b> " + ", ".join(_esc(c) for c in clean) + "</div>"
        )

    # A skipped check is not a passed check - say so on the page, not just in the log.
    if not logs_checked:
        parts.append(
            "<div class='clean'><b>Skipped:</b> export folder and scheduler checks. "
            "Set APP_LOGS_DIR in .env to include them.</div>"
        )

    parts.append(
        "<p class='foot'>Read-only. Fixes are shown, never applied &mdash; "
        "review before running anything.</p>"
    )
    parts.append(f"</div><script>{HTML_JS}</script></body></html>")
    return "".join(parts)


# ---------------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(description="Read-only database health checks.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--only", help="run a single check by name")
    ap.add_argument("--days", type=int, default=None, help="log window in days")
    ap.add_argument("--env", help="path to a .env file")
    ap.add_argument("--no-save", action="store_true", help="do not write a report file")
    ap.add_argument(
        "--no-baseline",
        action="store_true",
        help="write reports but do not become the new 'since last run' baseline",
    )
    ap.add_argument(
        "--source",
        default=None,
        help="who is running this: app, cli, schedule or mcp. Recorded in the history.",
    )
    ap.add_argument(
        "--no-store", action="store_true", help="do not record this run in the history"
    )
    ap.add_argument(
        "--list-checks",
        action="store_true",
        help="print every check and whether it is on, as JSON. No database needed.",
    )
    args = ap.parse_args()

    if args.list_checks:
        # Deliberately before load_env and find_psql: listing what checks exist is
        # a question about configuration, and it should answer even on a machine
        # with no password set and no psql installed.
        cfg = load_checks_config()
        off = {str(n).strip().lower() for n in cfg["disabled"]}
        out = []
        for group, kind in ((DB_CHECKS, "database"), (LOG_CHECKS, "log")):
            for name, severity, title, _ in group:
                out.append(
                    {
                        "name": name,
                        "severity": severity,
                        "title": title,
                        "kind": kind,
                        "builtin": True,
                        "enabled": name not in off,
                    }
                )
        for c in cfg["checks"]:
            name = str(c.get("name", "")).strip()
            if not name:
                continue
            out.append(
                {
                    "name": name,
                    "severity": c.get("severity", "warn"),
                    "title": c.get("title", name),
                    "kind": "database",
                    "builtin": False,
                    "enabled": bool(c.get("enabled", True)) and name.lower() not in off,
                    "sql": c.get("sql"),
                    "sql_problem": validate_custom_sql(c.get("sql")),
                }
            )
        print(json.dumps({"checks": out, "config_error": cfg["error"]}, indent=2))
        return 0

    load_env(args.env)

    psql = find_psql()
    if not psql:
        print("Could not find psql.exe. Set PSQL_PATH in .env.", file=sys.stderr)
        return 2

    if not os.environ.get("PGPASSWORD"):
        # Two callers, two config files - name the right one rather than sending
        # someone to edit a file that has nothing to do with how they ran this.
        where = "servers.json" if os.environ.get("DBC_VIA_APP") else ".env"
        print(
            f"No password for this connection. Set it in {where}.",
            file=sys.stderr,
        )
        return 2

    logs_dir = os.environ.get("APP_LOGS_DIR") or None
    window_days = args.days or int(os.environ.get("APP_LOG_DAYS", "7"))
    db_label = f'{os.environ.get("PGHOST", "?")}/{os.environ.get("PGDATABASE", "?")}'

    db_checks, log_checks, custom_checks, cfg_error = active_checks()

    # Record the run from here rather than from each caller, so the history covers
    # all four - app, command line, scheduled task and MCP - instead of only the
    # one someone remembered to instrument.
    run_id = None
    if _store and not args.no_store:
        try:
            run_id = _store.start_run(
                source=args.source or os.environ.get("DBC_SOURCE") or "cli",
                server=os.environ.get("DBC_SERVER_NAME") or os.environ.get("PGHOST"),
                database=os.environ.get("PGDATABASE"),
                username=os.environ.get("DBC_USER") or None,
            )
        except Exception:
            run_id = None

    results = []

    if cfg_error:
        # Surface it as a finding rather than a crash: the built-in checks are
        # still worth running, and a broken config file is itself worth reporting.
        results.append(
            {
                "name": "checks_config",
                "severity": "warn",
                "title": "checks.json could not be read",
                "findings": [
                    {"object": "checks.json", "detail": cfg_error, "fix": None, "note": None}
                ],
            }
        )

    for name, severity, title, fn in db_checks:
        if args.only and name != args.only:
            continue
        entry = {"name": name, "severity": severity, "title": title, "findings": []}
        try:
            entry["findings"] = fn(psql)
        except Exception as exc:
            entry["error"] = str(exc)
        results.append(entry)

    for spec in custom_checks:
        name = str(spec["name"])
        if args.only and name != args.only:
            continue
        entry = {
            "name": name,
            "severity": spec.get("severity", "warn"),
            "title": spec.get("title", name),
            "custom": True,
            "findings": [],
        }
        try:
            entry["findings"] = run_custom_check(psql, spec)
        except Exception as exc:
            entry["error"] = str(exc)
        results.append(entry)

    if logs_dir:
        for name, severity, title, fn in log_checks:
            if args.only and name != args.only:
                continue
            entry = {"name": name, "severity": severity, "title": title, "findings": []}
            try:
                entry["findings"] = fn(logs_dir, window_days)
            except Exception as exc:
                entry["error"] = str(exc)
            results.append(entry)

    # Compare with the previous run before recording this one.
    #
    # A --only run is deliberately NOT compared. It ran one check, so every finding
    # from every other check is missing from the results - and a naive diff reads
    # that absence as "fixed". Running --only export_folder once reported 76 things
    # fixed, none of which were. Saving such a run would be worse still: it would
    # overwrite the baseline with a single check and lose the rest permanently.
    partial = bool(args.only)
    diff = None if partial else diff_against_previous(db_label, results)

    if args.json:
        if partial:
            since = {"partial_run": True, "only": args.only}
        else:
            first, prev_at, new_ids, fixed_ids = diff
            since = {
                "first_run": first,
                "previous_ran_at": prev_at,
                "new": sorted(new_ids),
                "fixed": sorted(fixed_ids),
            }
        payload = {
            "database": db_label,
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "since_last_run": since,
            "results": results,
        }
        print(json.dumps(payload, indent=2, default=str))
        if not args.no_save and not args.no_baseline and not partial:
            save_current(db_label, results)
        if _store and run_id:
            _store.finish_run(run_id, results, None if partial else diff)
        return 0

    report = build_report(db_label, results, bool(logs_dir), diff)
    print(report)

    if not args.no_save:
        REPORTS_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")

        txt_out = REPORTS_DIR / f"check-{stamp}.txt"
        txt_out.write_text(report, encoding="utf-8")

        html = build_html(db_label, results, bool(logs_dir), diff)
        html_out = REPORTS_DIR / f"check-{stamp}.html"
        html_out.write_text(html, encoding="utf-8")

        # A stable name too, so a bookmark or a scheduled run always points at the
        # newest report without anyone hunting through timestamps.
        (REPORTS_DIR / "latest.html").write_text(html, encoding="utf-8")

        # Record this run last, so a crash mid-report does not lose the baseline.
        #
        # --no-baseline exists because saving is what CONSUMES a diff: once a run is
        # recorded, the next one compares against it and sees no change. When something
        # else is responsible for reading the diff and acting on it - the weekly agent -
        # the report run must not eat the diff before the agent has seen it.
        if not args.no_baseline and not partial:
            save_current(db_label, results)

        print(f"\nSaved:\n  {html_out}\n  {txt_out}")

    if _store and run_id:
        _store.finish_run(run_id, results, None if partial else diff)

    # Always exit 0 when the tool itself worked. Finding problems is success, and a
    # non-zero exit would make Task Scheduler show the run as failed.
    return 0


if __name__ == "__main__":
    sys.exit(main())
