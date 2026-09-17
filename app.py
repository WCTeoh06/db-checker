#!/usr/bin/env python3
"""
db-checker - local web UI.

Pick a server, run the checks, see the findings. Nothing is stored and nothing is
applied; the page is a view onto the same read-only checks db_checker.py runs.

    python app.py            then open http://127.0.0.1:8787

Binds to 127.0.0.1 only - reachable from this machine and nowhere else, the same
posture as the bug tracker's local app. Do not put this on 0.0.0.0: servers.json
holds real database passwords.

No pip packages. Standard library HTTP server, and the checks run by invoking
db_checker.py as a subprocess with that server's PG* variables - so the app and the
command line always agree, because they are the same code.
"""

import html
import json
import os
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path

import store

HERE = Path(__file__).resolve().parent
SERVERS_FILE = HERE / "servers.json"
PORT = int(os.environ.get("DBC_PORT", "8787"))

# 127.0.0.1 by default, and that default matters: servers.json holds real database
# passwords, so the app must not become network-reachable by accident.
#
# It is overridable only because inside a container "127.0.0.1" means the container
# itself, so binding there makes the app unreachable even from the machine running
# it. A container is already an isolation boundary, which is why 0.0.0.0 is
# reasonable THERE and nowhere else. Never set this on a normal machine.
HOST = os.environ.get("DBC_HOST", "127.0.0.1")
COOKIE = "dbc_session"
DASH = "\u2014"  # for "no user", kept out of f-strings (3.11 syntax)


def _local_time(iso):
    """An ISO timestamp as something a person reads. Blank stays blank."""
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(iso)

try:
    from db_checker import HTML_CSS, CHANGE_CSS  # one stylesheet, report and app
except Exception:
    HTML_CSS = CHANGE_CSS = ""


# ------------------------------------------------------------------ server list


def load_servers():
    """Read servers.json. Passwords never leave this process except into psql."""
    if not SERVERS_FILE.exists():
        return []
    try:
        data = json.loads(SERVERS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"servers.json is not valid JSON: {exc}")
    servers = data.get("servers") if isinstance(data, dict) else data
    return servers or []


def safe_server_list(servers):
    """What the browser is allowed to see: never the password."""
    return [
        {
            "id": i,
            "name": s.get("name") or f'{s.get("host")}/{s.get("database")}',
            "host": s.get("host"),
            "database": s.get("database"),
            "has_logs": bool(s.get("logsDir")),
        }
        for i, s in enumerate(servers)
    ]


def run_checks_for(server, username=None):
    """Run db_checker.py --json against one server and return its parsed output."""
    env = dict(os.environ)
    env["DBC_VIA_APP"] = "1"  # so error messages name servers.json, not .env
    # db_checker.py writes the history row itself, so it needs to know who asked.
    env["DBC_SOURCE"] = "app"
    env["DBC_SERVER_NAME"] = str(server.get("name") or server.get("host") or "")
    if username:
        env["DBC_USER"] = str(username)
    env["PGHOST"] = str(server.get("host", ""))
    env["PGPORT"] = str(server.get("port", 5432))
    env["PGUSER"] = str(server.get("user", ""))
    env["PGDATABASE"] = str(server.get("database", ""))
    env["PGPASSWORD"] = str(server.get("password", ""))
    env["PGSSLMODE"] = str(server.get("sslmode", "require"))
    if server.get("logsDir"):
        env["APP_LOGS_DIR"] = str(server["logsDir"])
    else:
        env.pop("APP_LOGS_DIR", None)
    if server.get("psqlPath"):
        env["PSQL_PATH"] = str(server["psqlPath"])
    if server.get("logDays"):
        env["APP_LOG_DAYS"] = str(server["logDays"])

    proc = subprocess.run(
        [sys.executable, str(HERE / "db_checker.py"), "--json", "--no-save"],
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
        cwd=str(HERE),
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError((proc.stderr or "checks failed").strip())
    try:
        return json.loads(proc.stdout)
    except Exception:
        raise RuntimeError((proc.stderr or proc.stdout or "no output").strip()[:600])


# -------------------------------------------------------------------------- UI

APP_JS = r"""
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

let SERVERS = [];

// A 401 means the session ran out mid-visit. Showing "error" would be a lie -
// send them to sign in again instead.
const signedOut = r => { if (r.status === 401) { location.href = '/login'; return true; } return false; };

async function boot() {
  try {
    const r = await fetch('/api/servers');
    if (signedOut(r)) return;
    const d = await r.json();
    SERVERS = d.servers || [];
    const sel = $('#server');
    if (!SERVERS.length) {
      $('#out').innerHTML = card('warn', 'No servers configured',
        'Copy <code>servers.json.example</code> to <code>servers.json</code> and add ' +
        'your connection details, then reload this page.');
      $('#run').disabled = true;
      return;
    }
    sel.innerHTML = SERVERS.map(s =>
      `<option value="${s.id}">${esc(s.name)}</option>`).join('');
    updateHint();
  } catch (e) {
    $('#out').innerHTML = card('critical', 'Could not load server list', esc(e.message));
  }
}

function updateHint() {
  const s = SERVERS[$('#server').value];
  if (!s) return;
  $('#hint').textContent = `${s.host} / ${s.database}` +
    (s.has_logs ? '' : '  -  no log folder set, log checks will be skipped');
}

function card(sev, title, body) {
  return `<section class="card"><header><span class="pill ${sev}">${sev}</span>
    <h2>${esc(title)}</h2></header><div class="why">${body}</div></section>`;
}

async function run() {
  const id = $('#server').value;
  $('#run').disabled = true;
  $('#run').textContent = 'Running...';
  $('#out').innerHTML = `<section class="card"><div class="why">
    Running checks. A large catalog can take a few seconds.</div></section>`;
  try {
    const r = await fetch('/api/check', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ server: Number(id) })
    });
    if (signedOut(r)) return;
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    LAST = d;
    render(d);
  } catch (e) {
    $('#out').innerHTML = card('critical', 'Checks could not run', esc(e.message));
  } finally {
    $('#run').disabled = false;
    $('#run').textContent = 'Run checks';
  }
}

const RANK = { critical: 0, warn: 1, info: 2 };
// The last result, kept so changing the severity filter re-draws instantly instead
// of re-running the checks - which would hit the database again for no new answer.
let LAST = null;
const SHOW_FIRST = 8;

// What changed since the last recorded run. Runs from this page are read-only
// comparisons - they never overwrite the baseline, so the weekly scheduled run
// stays the reference point and ad-hoc clicking here cannot reset it.
function renderChanges(c) {
  if (!c) return '';
  const pretty = id => { const i = id.indexOf('|');
    return `${esc(id.slice(i + 1))} <span style="color:var(--muted)">(${esc(id.slice(0, i))})</span>`; };
  if (c.first_run) {
    return `<div class="chg"><h3>Since last run</h3><p class="nc">First recorded run -
      nothing to compare against yet.</p></div>`;
  }
  const when = (c.previous_ran_at || '').slice(0, 16);
  if (!(c.new || []).length && !(c.fixed || []).length) {
    return `<div class="chg"><h3>Since last run</h3>
      <p class="nc">No change since ${esc(when)}.</p></div>`;
  }
  const li = (id, tag) => `<li><span class="tag ${tag}">${tag.toUpperCase()}</span>
    <span class="o">${pretty(id)}</span></li>`;
  return `<div class="chg"><h3>Since last run &middot; ${esc(when)}</h3><ul>
    ${(c.new || []).map(id => li(id, 'new')).join('')}
    ${(c.fixed || []).map(id => li(id, 'fixed')).join('')}
  </ul></div>`;
}

function render(data) {
  const all = (data.results || []).slice()
    .sort((a, b) => (RANK[a.severity] ?? 9) - (RANK[b.severity] ?? 9));

  // Counts always describe the WHOLE run, never the filtered view. A tile that
  // changed with the filter would make "0 critical" mean two different things.
  const count = sev => all.filter(r => r.severity === sev)
    .reduce((n, r) => n + (r.findings || []).length, 0);

  const want = $('#sev').value;
  const results = want ? all.filter(r => r.severity === want) : all;

  const tile = (sev, cls, label) => {
    const on = want === sev ? ' on' : '';
    return `<button class="tile ${cls}${on}" data-sev="${sev}"
      title="${want === sev ? 'Show everything again' : 'Show only these'}">
      <div class="n">${count(sev)}</div><div class="l">${label}</div></button>`;
  };

  let html = `<div class="tiles">
    ${tile('critical', 'crit', 'Critical')}
    ${tile('warn', 'warn', 'Warnings')}
    ${tile('info', 'info', 'Informational')}
    <div class="tile static"><div class="n">${all.length}</div><div class="l">Checks run</div></div>
  </div>`;

  html += renderChanges(data.since_last_run);

  if (!count('critical') && !count('warn')) {
    html += `<div class="clean"><b>Nothing actionable.</b> No critical or warning
      findings on ${esc(data.database || '')}.</div>`;
  }

  if (want && !results.some(r => (r.findings || []).length)) {
    html += `<div class="clean">Nothing at <b>${esc(want)}</b> severity in this run.</div>`;
  }

  let boxes = 0;
  for (const r of results) {
    const f = r.findings || [];
    if (!f.length && !r.error) continue;

    // A note every row shares belongs to the section, once - not repeated down a
    // list of thirty-five tables, where it stops being read.
    const notes = new Set(f.filter(x => x.note).map(x => x.note));
    const shared = (f.length > 1 && notes.size === 1 && f.every(x => x.note))
      ? [...notes][0] : null;

    html += `<section class="card"><header>
      <span class="pill ${r.severity}">${r.severity}</span>
      <h2>${esc(r.title)}</h2>
      <span class="count">${f.length} found</span></header>`;
    if (r.error) html += `<div class="why">Check could not run: ${esc(r.error)}</div>`;
    if (r.why) html += `<div class="why">${esc(r.why)}</div>`;
    if (shared) html += `<div class="why"><b>All of these:</b> ${esc(shared)}</div>`;

    const rows = items => items.map(x => `<div class="row">
      <div class="obj">${esc(x.object)}</div>
      <div class="det">${esc(x.detail)}</div>
      ${x.note && x.note !== shared ? `<div class="note">${esc(x.note)}</div>` : ''}
      ${x.fix ? `<div class="fix"><code>${esc(x.fix)}</code>
        <button class="copy">Copy SQL</button></div>` : ''}
    </div>`).join('');

    html += `<div class="rows">${rows(f.slice(0, SHOW_FIRST))}</div>`;
    if (f.length > SHOW_FIRST) {
      const id = 'more' + (++boxes);
      const label = `Show ${f.length - SHOW_FIRST} more`;
      html += `<div class="rows" id="${id}" hidden>${rows(f.slice(SHOW_FIRST))}</div>
        <button class="more" data-target="${id}" data-label="${label}">${label}</button>`;
    }
    html += `</section>`;
  }

  const clean = results.filter(r => !(r.findings || []).length && !r.error).map(r => r.name);
  if (clean.length) html += `<div class="clean"><b>Clean:</b> ${esc(clean.join(', '))}</div>`;

  const skipped = !results.some(r => r.name === 'export_folder');
  if (skipped) {
    html += `<div class="clean"><b>Skipped:</b> export folder and scheduler checks -
      no log folder configured for this server.</div>`;
  }

  html += `<p class="foot">Read-only. Fixes are shown, never applied.
    Checked ${esc(new Date(data.ran_at || Date.now()).toLocaleString())}.</p>`;

  $('#out').innerHTML = html;
}

document.addEventListener('click', e => {
  const c = e.target.closest('button.copy');
  if (c) {
    const code = c.parentElement.querySelector('code');
    navigator.clipboard.writeText(code.textContent).then(() => {
      const t = c.textContent; c.textContent = 'Copied';
      setTimeout(() => c.textContent = t, 1200);
    }).catch(() => c.textContent = 'Select manually');
  }
  const m = e.target.closest('button.more');
  if (m) {
    const box = document.getElementById(m.dataset.target);
    const hidden = box.hasAttribute('hidden');
    if (hidden) { box.removeAttribute('hidden'); m.textContent = 'Show fewer'; }
    else { box.setAttribute('hidden', ''); m.textContent = m.dataset.label; }
  }
});

$('#run').addEventListener('click', run);
$('#server').addEventListener('change', updateHint);
$('#sev').addEventListener('change', () => { if (LAST) render(LAST); });

// The tiles are the obvious thing to click when you want "just the warnings", so
// make them do it. Clicking the active one clears the filter, so there is always
// a way back that does not need the dropdown.
$('#out').addEventListener('click', e => {
  const t = e.target.closest('.tile[data-sev]');
  if (!t || !LAST) return;
  const sev = t.dataset.sev;
  $('#sev').value = $('#sev').value === sev ? '' : sev;
  render(LAST);
});
boot();
"""

EXTRA_CSS = """
.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 8px}
/* Tiles double as the severity filter, so they need to look pressable. The
   fourth ("checks run") is not a severity and stays inert - a button that does
   nothing is worse than a label. */
button.tile{font:inherit;text-align:left;cursor:pointer;border:1px solid var(--line);
  transition:border-color .12s,background .12s}
button.tile:hover{border-color:var(--muted)}
button.tile.on{border-color:currentColor}
button.tile.on .l::after{content:" \2022 filtered";opacity:.75;font-weight:600}
.tile.static{cursor:default}
#sev{min-width:0}
select{font:inherit;padding:9px 12px;border-radius:8px;border:1px solid var(--line);
  background:var(--card);color:var(--ink);min-width:260px}
button.primary{font:inherit;font-weight:600;padding:9px 18px;border-radius:8px;border:0;
  background:var(--info);color:#fff;cursor:pointer}
button.primary:hover{filter:brightness(1.08)}
button.primary:disabled{opacity:.55;cursor:default;filter:none}
#hint{color:var(--muted);font-size:12.5px;font-family:var(--mono);margin:0 0 24px}
"""


# ------------------------------------------------------------------ auth pages

AUTH_CSS = """
.auth{max-width:380px;margin:14vh auto;padding:0 20px}
.auth h1{font-size:22px;margin:0 0 6px}
.auth p.sub{color:var(--muted);font-size:13.5px;margin:0 0 22px;line-height:1.5}
.auth label{display:block;font-size:12.5px;color:var(--muted);margin:14px 0 6px}
.auth input{width:100%;box-sizing:border-box;font:inherit;padding:10px 12px;
  border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink)}
.auth button{width:100%;margin-top:20px;font:inherit;font-weight:600;padding:11px;
  border-radius:8px;border:0;background:var(--info);color:#fff;cursor:pointer}
.auth button:hover{filter:brightness(1.08)}
.auth .err{margin-top:16px;padding:10px 12px;border-radius:8px;font-size:13px;
  background:rgba(220,60,60,.12);border:1px solid rgba(220,60,60,.35)}
.who{margin-left:auto;font-size:12.5px;color:var(--muted)}
.who a{color:var(--muted);margin-left:12px}
"""


def auth_page(mode, error=None):
    """mode: 'setup' for the first account, 'login' afterwards."""
    first = mode == "setup"
    title = "Create your account" if first else "Sign in"
    sub = (
        "No account exists yet, so this first one is yours. db-checker runs on this "
        "machine only \u2014 signing in is what lets the history say who ran what, "
        "rather than keeping anyone out."
        if first
        else "db-checker"
    )
    action = "/setup" if first else "/login"
    button = "Create account" if first else "Sign in"
    extra = (
        "<label for='confirm'>Confirm password</label>"
        "<input id='confirm' name='confirm' type='password' autocomplete='new-password' required>"
        if first
        else ""
    )
    autoc = "new-password" if first else "current-password"
    err = f"<div class='err'>{html.escape(error)}</div>" if error else ""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>DB Checker</title><style>{HTML_CSS}{AUTH_CSS}</style></head><body>"
        f"<form class='auth' method='post' action='{action}'>"
        f"<h1>{title}</h1><p class='sub'>{sub}</p>"
        "<label for='username'>Username</label>"
        "<input id='username' name='username' autocomplete='username' autofocus required>"
        "<label for='password'>Password</label>"
        f"<input id='password' name='password' type='password' autocomplete='{autoc}' required>"
        f"{extra}"
        f"<button type='submit'>{button}</button>{err}"
        "</form></body></html>"
    ).encode("utf-8")


HISTORY_CSS = """
table.hist{width:100%;border-collapse:collapse;font-size:13px;margin:0 0 34px}
table.hist th{text-align:left;font-weight:600;color:var(--muted);font-size:11.5px;
  text-transform:uppercase;letter-spacing:.04em;padding:0 12px 8px 0;
  border-bottom:1px solid var(--line)}
table.hist td{padding:9px 12px 9px 0;border-bottom:1px solid var(--line);
  vertical-align:top}
table.hist td.num{font-family:var(--mono)}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11px;
  font-family:var(--mono)}
.pill.crit{background:rgba(220,60,60,.16);color:var(--crit)}
.pill.warn{background:rgba(210,150,30,.16);color:var(--warn)}
.pill.ok{background:rgba(120,120,120,.14);color:var(--muted)}
.empty{color:var(--muted);font-size:13.5px;margin:0 0 34px}
"""


def history_page(username):
    runs = store.recent_runs(60)
    events = store.recent_events(60)

    if runs:
        rows = []
        for r in runs:
            if r["error"]:
                found = "<span class='pill crit'>failed</span>"
            elif r["critical"]:
                found = f"<span class='pill crit'>{r['critical']} critical</span>"
            elif r["warn"]:
                found = f"<span class='pill warn'>{r['warn']} warn</span>"
            elif r["finished_at"]:
                found = "<span class='pill ok'>clean</span>"
            else:
                found = "<span class='pill ok'>did not finish</span>"
            changed = ""
            if r["new_count"] or r["fixed_count"]:
                changed = f"{r['new_count']} new / {r['fixed_count']} fixed"
            rows.append(
                "<tr>"
                f"<td class='num'>{_local_time(r['started_at'])}</td>"
                f"<td>{html.escape(r['source'] or '')}</td>"
                f"<td>{html.escape(r['username'] or DASH)}</td>"
                f"<td>{html.escape(r['server'] or '')}</td>"
                f"<td>{found}</td>"
                f"<td class='num'>{changed}</td>"
                f"<td class='num'>{r['seconds'] if r['seconds'] is not None else ''}</td>"
                "</tr>"
            )
        runs_html = (
            "<table class='hist'><thead><tr><th>When</th><th>Started by</th>"
            "<th>User</th><th>Server</th><th>Result</th><th>Changed</th><th>Secs</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        )
    else:
        runs_html = "<p class='empty'>No runs recorded yet.</p>"

    if events:
        erows = "".join(
            "<tr>"
            f"<td class='num'>{_local_time(e['at'])}</td>"
            f"<td>{html.escape(e['username'] or DASH)}</td>"
            f"<td>{html.escape(e['kind'])}</td>"
            f"<td class='num'>{html.escape(e['detail'] or '')}</td>"
            "</tr>"
            for e in events
        )
        events_html = (
            "<table class='hist'><thead><tr><th>When</th><th>User</th>"
            "<th>What</th><th>From</th></tr></thead><tbody>"
            + erows
            + "</tbody></table>"
        )
    else:
        events_html = "<p class='empty'>Nothing yet.</p>"

    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>DB Checker</title>"
        f"<style>{HTML_CSS}{CHANGE_CSS}{EXTRA_CSS}{TOPBAR_CSS}{HISTORY_CSS}</style>"
        "</head><body><div class='wrap'>"
        f"{topbar(username, 'history')}"
        "<h2>Runs</h2>" + runs_html +
        "<h2>Sign-in activity</h2>" + events_html +
        "</div></body></html>"
    ).encode("utf-8")


TOPBAR_CSS = """
.top{display:flex;align-items:baseline;gap:16px;margin:0 0 22px;
  padding:0 0 14px;border-bottom:1px solid var(--line)}
.top h1{margin:0;font-size:20px;letter-spacing:-.01em}
.top nav{margin-left:auto;display:flex;align-items:center;gap:4px}
.top .user{font-size:12.5px;color:var(--muted);font-family:var(--mono);
  padding-right:12px;margin-right:6px;border-right:1px solid var(--line)}
.top nav a{font-size:12.5px;color:var(--muted);text-decoration:none;
  padding:5px 10px;border-radius:6px;transition:background .12s,color .12s}
.top nav a:hover{background:var(--card);color:var(--ink)}
.top nav a.here{color:var(--ink);background:var(--card)}
"""


def topbar(username, page_name):
    """Title on the left, who you are and where you can go on the right."""
    def link(href, label, key):
        here = " class='here'" if key == page_name else ""
        return f"<a href='{href}'{here}>{label}</a>"

    title = "Database checks" if page_name == "checks" else "History"
    return (
        f"<div class='top'><h1>{title}</h1><nav>"
        f"<span class='user'>{html.escape(username)}</span>"
        + link("/", "Checks", "checks")
        + link("/history", "History", "history")
        + "<a href='/logout'>Sign out</a>"
        "</nav></div>"
    )


def page(username):
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>DB Checker</title>"
        f"<style>{HTML_CSS}{CHANGE_CSS}{EXTRA_CSS}{TOPBAR_CSS}{HISTORY_CSS}</style></head><body><div class='wrap'>"
        f"{topbar(username, 'checks')}"
        "<div class='bar'>"
        "<select id='server'></select>"
        "<select id='sev' title='Filter the findings below'>"
        "<option value=''>All severities</option>"
        "<option value='critical'>Critical only</option>"
        "<option value='warn'>Warnings only</option>"
        "<option value='info'>Informational only</option>"
        "</select>"
        "<button class='primary' id='run'>Run checks</button>"
        "</div><p id='hint'></p>"
        "<div id='out'></div>"
        f"</div><script>{APP_JS}</script></body></html>"
    ).encode("utf-8")


# ---------------------------------------------------------------------- server


class Handler(BaseHTTPRequestHandler):
    server_version = "db-checker"

    def log_message(self, fmt, *a):  # quieter console
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", cookie=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, to, cookie=None):
        self.send_response(303)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    # ------------------------------------------------------------- session

    def _token(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            return SimpleCookie(raw).get(COOKIE).value
        except Exception:
            return None

    def _user(self):
        return store.session_user(self._token())

    @staticmethod
    def _cookie_for(token):
        # HttpOnly so page scripts cannot read it, SameSite=Lax so another site
        # cannot make authenticated requests on the user's behalf. No Secure flag:
        # this is plain http on 127.0.0.1, and setting it would stop the cookie
        # being sent at all.
        return (
            f"{COOKIE}={token}; HttpOnly; SameSite=Lax; Path=/; "
            f"Max-Age={store.SESSION_HOURS * 3600}"
        )

    def _form(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def _peer(self):
        try:
            return self.client_address[0]
        except Exception:
            return None

    # ----------------------------------------------------------------- GET

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/favicon.ico":
            # Browsers ask for this unprompted; answering keeps the console clean.
            return self._send(204, b"", "image/x-icon")

        # No account yet: the only thing you can do is make one. On 127.0.0.1 the
        # first person here is the person at the keyboard.
        if store.user_count() == 0:
            if path == "/setup":
                return self._send(200, auth_page("setup"), "text/html; charset=utf-8")
            return self._redirect("/setup")

        if path == "/login":
            if self._user():
                return self._redirect("/")
            return self._send(200, auth_page("login"), "text/html; charset=utf-8")

        if path == "/logout":
            store.end_session(self._token(), self._peer())
            return self._redirect("/login", f"{COOKIE}=; Path=/; Max-Age=0")

        user = self._user()
        if not user:
            # Pages redirect so the browser lands somewhere useful; API calls get a
            # 401 so the page script can tell "signed out" from "broken".
            if path.startswith("/api/"):
                return self._send(401, b'{"error":"not signed in"}')
            return self._redirect("/login")

        if path in ("/", "/index.html"):
            return self._send(200, page(user), "text/html; charset=utf-8")
        if path == "/history":
            return self._send(200, history_page(user), "text/html; charset=utf-8")
        if path == "/api/servers":
            try:
                body = json.dumps({"servers": safe_server_list(load_servers())})
            except Exception as exc:
                body = json.dumps({"error": str(exc)})
            return self._send(200, body.encode("utf-8"))

        self._send(404, b'{"error":"not found"}')

    # ---------------------------------------------------------------- POST

    def do_POST(self):
        path = self.path.split("?", 1)[0]

        if path == "/setup":
            if store.user_count() > 0:
                return self._redirect("/login")
            form = self._form()
            username = form.get("username", "")
            password = form.get("password", "")
            if password != form.get("confirm", ""):
                return self._send(
                    200,
                    auth_page("setup", "Those two passwords do not match."),
                    "text/html; charset=utf-8",
                )
            problem = store.create_user(username, password)
            if problem:
                return self._send(
                    200, auth_page("setup", problem), "text/html; charset=utf-8"
                )
            token = store.start_session(username.strip(), self._peer())
            return self._redirect("/", self._cookie_for(token))

        if path == "/login":
            form = self._form()
            username = form.get("username", "")
            if store.verify_user(username, form.get("password", "")):
                token = store.start_session(username.strip(), self._peer())
                return self._redirect("/", self._cookie_for(token))
            store.record_failed_login(username, self._peer())
            # One message for both wrong-user and wrong-password, so the page does
            # not confirm which usernames exist.
            return self._send(
                200,
                auth_page("login", "That username and password do not match."),
                "text/html; charset=utf-8",
            )

        user = self._user()
        if not user:
            return self._send(401, b'{"error":"not signed in"}')

        if path != "/api/check":
            return self._send(404, b'{"error":"not found"}')
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            servers = load_servers()
            idx = int(payload.get("server", 0))
            if idx < 0 or idx >= len(servers):
                raise RuntimeError("unknown server")
            data = run_checks_for(servers[idx], username=user)
            body = json.dumps(data, default=str).encode("utf-8")
        except Exception as exc:
            body = json.dumps({"error": str(exc)}).encode("utf-8")
        self._send(200, body)


def main():
    if not SERVERS_FILE.exists():
        print(f"No servers.json found in {HERE}.")
        print("Copy servers.json.example to servers.json and fill it in.")
    store.init()
    url = f"http://127.0.0.1:{PORT}"
    if store.user_count() == 0:
        print("No account yet - the first page will ask you to create one.")
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    where = "this machine only" if HOST == "127.0.0.1" else f"bound to {HOST}"
    print(f"db-checker UI on {url}   ({where})")
    print("Ctrl+C to stop.")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
