# db-checker

Read-only health checks for the databases MyApp talks to.

It looks for problems that never raise an error — a large table with no index, a table
with no primary key, statistics never gathered, an export folder that has been silently
unusable for weeks, a scheduler that quietly stopped — and prints what it found together
with the SQL to fix it.

**It never fixes anything.** Every fix is printed for a person to run.

## Which file to double-click

| File | What it does |
|---|---|
| **`start.bat`** | **the app** — pick a server from a dropdown, click Run checks |
| `check-once.bat` | checks the one server in `.env` and writes report files, no UI |
| `mcp-server\install.bat` | one-time setup to use this from Claude instead |
| `install-schedule.bat` | register the weekly automatic run |
| `uninstall-schedule.bat` | remove it |
| `run-scheduled.bat` | what the schedule runs. Not for double-clicking |

Almost always you want `start.bat`.

## The app — pick a server, click run

Double-click **`start.bat`**. It starts a small local web server and opens
`http://127.0.0.1:8787` in your browser: choose a server from the dropdown, press
**Run checks**, and the findings appear as cards — counts at the top, critical first,
long lists collapsed, a **Copy SQL** button on every suggested fix.

First run creates `servers.json` from `servers.json.example` and opens it. Add one entry
per server you want to be able to check:

```json
{
  "servers": [
    {
      "name": "myapp - postgres",
      "host": "db.prod.example.internal", "port": 5432,
      "user": "myapp_svc", "database": "postgres", "password": "...",
      "sslmode": "require",
      "logsDir": "D:\\myapp\\database\\logs"
    }
  ]
}
```

`logsDir` is optional. Without it the export-folder and scheduler checks are skipped for
that server, and the page says so rather than leaving a blank space.

**The app binds to `127.0.0.1` only** — reachable from your machine and nowhere else,
the same posture as the bug tracker's local app. Don't change it to `0.0.0.0`:
`servers.json` holds real passwords, and it's gitignored for the same reason.

Close the console window to stop the server.

## Signing in, and the history

The first time you open the app it asks you to create an account. After that,
`start.bat` shows a sign-in page.

Be clear-eyed about what this is for. The app is on `127.0.0.1`, so sign-in is not
keeping an attacker out — anyone who can reach the page already has the machine, and
`servers.json` on that machine holds the database passwords in plaintext. Sign-in is
here so the **History** page can say *who*, and so the tool can later be shared without
a rewrite. Passwords are still hashed properly (PBKDF2-HMAC-SHA256, per-user salt,
240k rounds) rather than stored in the clear — people reuse passwords, and a local
tool storing one badly is a real risk to them somewhere else.

**History** (top right) shows two things:

- **Runs** — every check run, from *all four* callers: the app, `check-once.bat`, the
  scheduled task and the MCP server. Who started it, which server, what was found,
  how long it took. `db_checker.py` writes that row itself, which is why the CLI and
  scheduled runs appear too rather than only the ones someone remembered to log.
- **Sign-in activity** — sign-ins, sign-outs, and failed attempts.

It all lives in `db-checker.sqlite3` next to the script. `sqlite3` ships with Python,
so this added no dependency. The file is gitignored.

Sessions last 12 hours. A run recorded from the command line has no user against it,
which is correct — nobody signed in to start it.

**A failed history write never fails a check.** `finish_run` swallows its own errors on
purpose: the checks are the product and the history is a convenience, so a locked
database must not turn a working health check into a failed one.

Add another account from the command line:

```
python store.py adduser
```

## Or check one server without the UI

For scheduled runs, or when you just want the report files:

1. **Python 3.9+** installed, with "Add python.exe to PATH" ticked.
2. **psql** — you already have it at `D:\pgsql\bin\psql.exe`. Nothing to install.
3. Double-click **`check-once.bat`**.

First run creates `.env` from `.env.example` and opens it in Notepad. Fill in
`PGPASSWORD` (and `APP_LOGS_DIR` if you want the log checks), save, and run it again.

This path only ever checks the one connection in `.env` — it has no dropdown. That's
deliberate: a scheduled task shouldn't depend on which server someone last picked.

There is nothing to `pip install`. The script drives `psql` rather than using a database
driver, so there is no virtualenv, no wheels, and nothing that breaks when Python
updates.

## The report

Each run writes three files into `reports\`:

| File | For |
|---|---|
| `latest.html` | the one to look at — opens in your browser, always the newest run |
| `check-<timestamp>.html` | the same report, kept as history |
| `check-<timestamp>.txt` | plain text, for pasting into chat or a ticket |

`start.bat` opens the HTML automatically. It's a single self-contained file — no server,
no internet needed — so you can email it or drop it in Teams and it renders for whoever
opens it.

Counts at the top, critical first, long lists collapsed behind "Show more", and every
suggested fix has a **Copy SQL** button. Notes shared by every row in a section are shown
once at the top of that section rather than repeated down the list.

## How you know whether anything changed

Every run compares itself against the previous one and puts a **Since last run** block
at the top:

```
SINCE LAST RUN (2026-09-07T08:00):
  NEW    erp.tblZ  (tables_without_pk)
  FIXED  erp.tblC  (tables_without_pk)
```

Three possible states: **first run** (nothing to compare yet), **no change**, or a list
of what appeared and what went away. That block is the part worth reading — a report
that repeats the same thirty findings every week gets ignored.

The baseline lives in `reports\_state.json`, keyed by database, so several servers share
one file without overwriting each other.

**Runs from the app never touch the baseline.** Clicking Run checks shows you the
comparison but doesn't record a new one, so the scheduled weekly run stays the reference
point and ad-hoc clicking can't reset it. Only `check-once.bat` and the scheduled run
update it.

## Automatic runs

```
install-schedule.bat
```

Registers a Windows scheduled task: every Monday 08:00, writing `reports\latest.txt` and
`reports\latest.json`. No admin rights needed — the checks are read-only.

Test it immediately with:

```
schtasks /Run /TN "DbChecker-Weekly"
```

Remove it with `uninstall-schedule.bat`.

## Command line

```
python db_checker.py                      full report, saved to reports\
python db_checker.py --json               machine-readable
python db_checker.py --only table_sizes   one check
python db_checker.py --days 30            wider log window
python db_checker.py --no-save            print only
python db_checker.py --list-checks        what checks exist, as JSON. No database needed
```

## Using it from Claude

`mcp-server\` makes db-checker an MCP server, the same shape as the team's bug
tracker. Once it's wired up you ask instead of clicking:

> *"run db-checker on v-one"*
> *"which checks are switched on?"*
> *"add a check for queries running longer than five minutes"*
> *"stop reporting unused indexes, we know about those"*
> *"add the erp database"*

Setup, once — install it as a Claude Desktop extension, the same way the bug tracker
was installed:

1. Double-click **`mcp-server\install.bat`** — installs the MCP SDK and checks the
   server starts.
2. In PowerShell, from the `mcp-server` folder:
   ```
   npx @anthropic-ai/mcpb pack
   ```
   That produces `mcp-server.mcpb`.
3. Claude Desktop → Settings → Extensions → Advanced settings → **Install extension**,
   and pick that `.mcpb`. Windows doesn't know what a `.mcpb` is, so double-clicking it
   only offers you Notepad — install it from inside Claude.
4. When it asks for the **db-checker folder**, browse to the folder holding
   `db_checker.py`. This is **required**: installing from a `.mcpb` copies the server
   into Claude's own extensions folder, where nothing sits next to `db_checker.py`.
5. Quit Claude Desktop **completely** (right-click the tray icon → Quit; closing the
   window isn't enough) and open it again. Toggling the extension off and on does not
   reliably restart it.

If a tool answers "cannot find db_checker.py", it lists every folder it looked in — the
fix is step 4, then step 5.

**It does not need `start.bat` running.** The bug tracker's MCP server is an HTTP
adapter, so its app has to be up first; this one runs `db_checker.py` directly.
The app, the scheduled task and this all work independently.

| Tool | |
|---|---|
| `list_servers` | which databases can be checked. Never returns passwords |
| `list_checks` | every check, on or off, built-in or custom. No database needed |
| `run_checks` | run them against one server |
| `add_check` / `remove_check` | write a new check, delete a custom one |
| `set_check_enabled` | switch any check off without losing it |
| `add_server` / `remove_server` | edit `servers.json` |

Every edit keeps the previous file as `.bak`.

**`add_server` takes no password, deliberately.** It writes the entry with an empty
one and tells you to fill it in yourself. A database password typed into a chat is
a database password in a transcript, and that's not a trade worth making for saving
one edit.

**No tool here can change a database.** Not even the `CREATE INDEX` it suggests —
see *Things it deliberately does not do* below. That isn't a gap left to fill later;
adding a fix-it tool would be the wrong feature.

## Adding your own checks

`checks.json` — copy it from `checks.json.example` — does two things:

```json
{
  "disabled": ["unused_indexes"],
  "checks": [
    {
      "name": "long_running_queries",
      "title": "Queries running longer than 5 minutes",
      "severity": "warn",
      "enabled": true,
      "note": "Usually a missing index.",
      "sql": "SELECT pid::text AS object, state, round(extract(epoch FROM now() - query_start)/60) AS minutes FROM pg_stat_activity WHERE state <> 'idle' AND query_start < now() - interval '5 minutes'"
    }
  ]
}
```

`disabled` silences any check by name, built-in ones included. `checks` adds new
ones. The file is optional — without it you get the seven built-ins.

A check is **one query, not code**. That's the point: adding one can't break the
checks that already work, it's a diff you can read, and `git revert` undoes it.

Rules for the SQL:

- One statement, starting `SELECT` or `WITH`. No semicolons.
- It must return a column named **`object`** — what the finding is about. Every
  other column becomes the detail line, so the SELECT decides the output shape.
- `fix` is optional; `{column}` placeholders are filled from the row.
- `note` is optional and printed once per section, not once per row.

`checks.json.example` ships three worked examples — long-running queries, table
bloat, connection headroom — two of them switched off, so you can turn one on and
see what it does before writing your own.

Two guards, and they're different in kind. The tool refuses SQL containing anything
that could write, which is a fast clear error; and **every connection is opened with
`default_transaction_read_only=on`**, which is Postgres itself refusing, and applies
to SQL this tool didn't write. The first is convenience, the second is the actual
protection.

The keyword check is deliberately blunt and will reject a valid query with the word
`update` inside a string literal. Rephrase it — a false refusal costs you a minute,
and loosening the rule to allow it costs more than that.

A broken `checks.json` doesn't stop the run: it's reported as a finding and the
built-in checks carry on.

## The checks

| Name | Severity | Looks for |
|---|---|---|
| `unindexed_tables` | warn | tables over 100k rows with no index at all |
| `tables_without_pk` | warn | tables over 100k rows with no primary key — the cause of the above |
| `stale_statistics` | warn | large tables never analysed, so the planner is guessing |
| `table_sizes` | info | biggest tables, and **bytes per row** |
| `unused_indexes` | info | large indexes with zero scans, costing writes and disk |
| `export_folder` | warn | exports falling back because the configured folder is unusable |
| `scheduler_health` | critical | a scheduler that used to run and stopped |
| `log_errors` | warn | the same error repeating in `sync.log` |

`bytes_per_row` is the one that prevents unopenable exports: multiply it by your
rows-per-file setting and you know the file size before exporting. The report flags any
table where a 1,000,000-row export would exceed 1 GB.

## Why each check exists

Every one of these caught something real in September 2026:

| Found | How long it was true | How it was actually noticed |
|---|---|---|
| 20+ tables with zero indexes, one of them 2.8M rows | months | a user said one export felt slow |
| Backup scheduler discarding tick errors with no log line | unknown | reading the source by hand |
| Export folder pointing at a path that exists on no machine | ~2 weeks | a warning nobody read |
| A 16 GB export, because rows are ~16 KB and the split limit is row-based | ongoing | the file wouldn't open |

Tracing the first one took most of two days. This run takes seconds.

## Things it deliberately does not do

**No DDL, ever** — not even the `CREATE INDEX CONCURRENTLY` it suggests. An index build
locks or loads a production table for minutes; that needs a person who knows what else is
running.

**No `ADD PRIMARY KEY` as copy-paste SQL.** That statement takes an exclusive lock and
rewrites the table, so it is reported with a warning instead of a ready-to-run line.

**No reading `database/*.json`.** Those are `JSONLOCK::v1::` ciphertext. Only
`database/logs/` is plaintext, and that is all this reads.

**No dropping unused indexes.** Counters reset on restart, and any index backing a
primary key or unique constraint is excluded from the report entirely.

**No crying wolf about schedulers that were never set up.** A scheduler with no history at
all in the log is passed over in silence — "it was never configured" is not a finding.
A scheduler that ran until some date and then stopped *is*, and gets reported with the
last time it ran. A skipped check is still named as skipped rather than quietly omitted.

**No shouting about one bad minute.** An error has to appear three times in the window
before it counts as a fault. One connection timeout in a week is a machine asleep or a
cable, and reporting it at the same volume as a real fault is how people learn to skim
the section.

Below-threshold errors are still *named*, marked as under the threshold, just not
treated as faults. An earlier version only counted them — "1 other error occurred and is
not listed" — which was the worst of both: same line, no information, and you had to
open the log every week to rediscover it was the same harmless timeout. Quiet means
short, not withheld. Past five distinct one-offs it does fall back to a count, since a
log full of unique errors is its own kind of noise.

**Severity means something.** `critical` is reserved for work that has stopped. Exports
landing in a fallback folder are a `warn`: they still succeed, they just go somewhere
nobody is looking. A severity that overstates the problem costs the report its credibility,
which is the only thing it has.

**Exit code 0 whenever the tool itself worked**, even when it finds problems — otherwise
Task Scheduler would show every useful run as a failure.

## Not covered

Postgres only. `legacy-mssql-host` is SQL Server and needs its own catalog queries — different
system views, same idea.
