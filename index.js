#!/usr/bin/env node
/**
 * db-checker MCP server.
 *
 * Puts the db-checker checks inside Claude: run them, and add or remove checks
 * and servers by asking.
 *
 * Same shape as the team's bug-tracker MCP server, with one difference. The bug
 * tracker's server is an HTTP adapter against a running Next.js app, so the app
 * has to be up first. This one shells out to db_checker.py directly, so there is
 * nothing to start - Claude Desktop launches this process, and this process
 * launches Python when a tool is called. start.bat and this server can both be
 * used, or either on its own.
 *
 * Read-only tools:
 *   list_servers  which databases can be checked
 *   list_checks   every check, built-in and custom, and whether it is on
 *   run_checks    run them against one server
 *
 * Editing tools (they change config files, never the database):
 *   add_server / remove_server        servers.json
 *   add_check / remove_check          checks.json
 *   set_check_enabled                 checks.json  (works on built-ins too)
 *
 * No tool here can change a database. db_checker.py opens every connection with
 * default_transaction_read_only=on, and the fixes it finds are printed for a
 * person to run - that is deliberate and this server does not add a way around it.
 */
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, existsSync, copyFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));

/**
 * Read an env var, treating an unsubstituted manifest placeholder as unset.
 *
 * A blank optional field in the extension's settings can arrive as the literal
 * text "${user_config.python_path}" rather than as an empty string. Passing that
 * to spawn() produces "spawn ${user_config.python_path} ENOENT", which sends
 * whoever reads it hunting for a Python problem that does not exist.
 */
function setting(name) {
  const value = process.env[name];
  if (!value || !value.trim() || value.includes("${")) return null;
  return value.trim();
}

/**
 * Work out where db_checker.py actually is.
 *
 * Two install routes put this file in two very different places. Wired up by hand
 * in claude_desktop_config.json, it runs from db-checker/mcp-server/ and the tool is
 * one level up. Installed from a .mcpb, Claude copies it into its own extensions
 * folder, where nothing is one level up - hence DB_CHECKER_DIR.
 *
 * Each candidate is checked for db_checker.py rather than assumed, because a wrong
 * directory that exists is worse than no directory: every tool would fail with a
 * confusing error instead of a clear one.
 */
function resolveDbCheckerDir() {
  const candidates = [];
  const configured = setting("DB_CHECKER_DIR");
  if (configured) {
    // A "directory" user_config can arrive as a JSON array of one path rather
    // than a bare string, depending on how the host serialises it.
    try {
      const parsed = JSON.parse(configured);
      if (Array.isArray(parsed)) candidates.push(...parsed);
      else candidates.push(configured);
    } catch {
      candidates.push(configured);
    }
  }
  candidates.push(join(HERE, ".."), HERE);

  for (const c of candidates) {
    if (c && existsSync(join(String(c), "db_checker.py"))) {
      return { dir: resolve(String(c)), tried: [] };
    }
  }
  // Nothing found. Keep the first candidate so the paths below still build, and
  // carry the full list so the error can name everywhere it looked.
  return {
    dir: resolve(String(candidates[0])),
    tried: candidates.map((c) => resolve(String(c))),
  };
}

const RESOLVED = resolveDbCheckerDir();
const DB_CHECKER_DIR = RESOLVED.dir;
const SCRIPT = join(DB_CHECKER_DIR, "db_checker.py");
const SERVERS_FILE = join(DB_CHECKER_DIR, "servers.json");
const CHECKS_FILE = join(DB_CHECKER_DIR, "checks.json");
const PYTHON = setting("PYTHON_PATH") || (process.platform === "win32" ? "python" : "python3");

const SEVERITIES = ["critical", "warn", "info"];

const INSTRUCTIONS = `You help a data engineer look after the PostgreSQL databases behind MyApp, using the db-checker tool on this machine.

RUNNING CHECKS (list_servers, list_checks, run_checks): read-only. They never change a database and never change a config file. Call list_servers first if the user has not said which server they mean, and ask rather than guessing when more than one could fit.

READING RESULTS: severity is the tool's opinion, not the last word. When you report findings, say what the finding means for this system rather than restating the row - a table with no index is slow to filter, a table with no primary key is why it has no index, bytes-per-row times rows-per-file predicts whether an export will open. Findings come with SQL to fix them; show that SQL and let the user decide. Never present a fix as something you have done or will do.

EDITING (add_check, remove_check, set_check_enabled, add_server, remove_server): these write to checks.json and servers.json. Summarise exactly what will change and get an explicit go-ahead before calling any of them. For add_check, show the user the SQL you intend to store and confirm it, because a check that returns the wrong thing quietly reports the wrong thing every week.

A custom check's SQL must be a single read-only SELECT or WITH returning a column named "object"; every other column becomes the detail line. If the tool rejects your SQL, fix the SQL - do not look for a way around the restriction.

PASSWORDS: add_server does not take one, on purpose. It writes the entry with an empty password and the user fills it in themselves in servers.json. Do not ask the user to tell you a database password, and if they offer one, tell them to put it in servers.json directly instead - it does not belong in a chat transcript.

NEVER APPLY A FIX. There is no tool here that runs DDL, and that is the design, not an oversight: CREATE INDEX and ADD PRIMARY KEY lock or rewrite a production table for minutes, and that needs a person who knows what else is running. If asked to apply one, explain that and hand over the SQL.`;

// ------------------------------------------------------------------- helpers

function textResult(text, isError = false) {
  return { content: [{ type: "text", text }], isError };
}

function readJson(path, fallback) {
  if (!existsSync(path)) return fallback;
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new Error(`${path} is not valid JSON: ${error.message}`);
  }
}

/**
 * Write JSON back, keeping a .bak of what was there.
 *
 * These files are hand-edited by a person and hold the only copy of their
 * config - a bad write should be recoverable without asking them to remember
 * what it used to say.
 */
function writeJson(path, data) {
  if (existsSync(path)) copyFileSync(path, `${path}.bak`);
  writeFileSync(path, `${JSON.stringify(data, null, 2)}\n`, "utf8");
}

function loadServers() {
  const data = readJson(SERVERS_FILE, null);
  if (!data) throw new Error(`No servers.json in ${DB_CHECKER_DIR}. Run start.bat once to create it.`);
  const list = data.servers;
  if (!Array.isArray(list)) throw new Error('servers.json has no "servers" array.');
  return { data, list };
}

function loadChecks() {
  const data = readJson(CHECKS_FILE, null) ?? { disabled: [], checks: [] };
  data.disabled = Array.isArray(data.disabled) ? data.disabled : [];
  data.checks = Array.isArray(data.checks) ? data.checks : [];
  return data;
}

function findServer(list, name) {
  const wanted = String(name ?? "").trim().toLowerCase();
  const exact = list.find((s) => String(s.name ?? "").trim().toLowerCase() === wanted);
  if (exact) return exact;
  // People say "LEGACY" for an entry called "legacy - sql server". Match a
  // unique prefix, but refuse an ambiguous one rather than picking a database
  // for someone by coin toss.
  const partial = list.filter((s) => String(s.name ?? "").toLowerCase().includes(wanted));
  if (partial.length === 1) return partial[0];
  if (partial.length > 1) {
    throw new Error(
      `"${name}" matches ${partial.length} servers: ${partial.map((s) => s.name).join(", ")}. Be more specific.`,
    );
  }
  return null;
}

function runPython(args, extraEnv = {}) {
  return new Promise((resolve_, reject) => {
    const child = spawn(PYTHON, [SCRIPT, ...args], {
      cwd: DB_CHECKER_DIR,
      env: { ...process.env, ...extraEnv },
    });
    let out = "";
    let err = "";
    child.stdout.on("data", (d) => (out += d));
    child.stderr.on("data", (d) => (err += d));
    child.on("error", (e) =>
      reject(
        new Error(
          `Could not run ${PYTHON}. Is Python installed and on PATH? (${e.message})`,
        ),
      ),
    );
    child.on("close", (code) => {
      if (!out.trim()) {
        reject(new Error(err.trim() || `db_checker.py exited ${code} with no output`));
        return;
      }
      try {
        resolve_(JSON.parse(out));
      } catch {
        reject(new Error((err || out).trim().slice(0, 800)));
      }
    });
    // A check against a big catalog can take a while, but not forever.
    setTimeout(() => child.kill(), 600_000);
  });
}

// --------------------------------------------------------------------- tools

const TOOLS = [
  {
    name: "list_servers",
    description:
      "List the databases db-checker can check, from servers.json: name, host, port, database, and whether log checks are available for it. Read-only. Passwords are never returned. Call this first when the user has not said which server they mean.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "list_checks",
    description:
      "List every check - the seven built-in ones and any custom ones from checks.json - with its severity, what it looks for, and whether it is currently switched on. Read-only, and needs no database connection.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "run_checks",
    description:
      "Run the health checks against one server and return the findings. Read-only: the connection is opened read-only, nothing is written to the database, and no report file or baseline is saved. Fixes come back as SQL for a person to run.",
    inputSchema: {
      type: "object",
      properties: {
        server: {
          type: "string",
          description: 'Server name from list_servers, e.g. "myapp - postgres".',
        },
        only: {
          type: "string",
          description: "Optional: run just this one check, by name from list_checks.",
        },
        save_baseline: {
          type: "boolean",
          description:
            "Default false. Set true ONLY for the scheduled weekly agent run: it records this run as the new 'since last run' baseline, so next week's diff is measured against it. An ad-hoc run must leave this false, or it silently consumes the diff the weekly run was going to report on.",
        },
      },
      required: ["server"],
    },
  },
  {
    name: "add_check",
    description:
      "Add a custom check to checks.json. The SQL must be a single read-only SELECT or WITH returning a column named \"object\"; every other column becomes the detail line. Confirm the SQL with the user before calling this.",
    inputSchema: {
      type: "object",
      properties: {
        name: {
          type: "string",
          description: "Short identifier, lowercase with underscores, e.g. long_running_queries.",
        },
        title: { type: "string", description: "One line describing what it looks for." },
        severity: { type: "string", enum: SEVERITIES },
        sql: {
          type: "string",
          description:
            'Single read-only SELECT or WITH. Must return a column named "object". No semicolons.',
        },
        fix: {
          type: "string",
          description:
            "Optional SQL shown as the suggested fix. {column} placeholders are filled from the row.",
        },
        note: {
          type: "string",
          description: "Optional explanation, shown once for the section rather than per row.",
        },
      },
      required: ["name", "title", "severity", "sql"],
    },
  },
  {
    name: "remove_check",
    description:
      "Delete a custom check from checks.json entirely. Only works on custom checks - a built-in check cannot be deleted, use set_check_enabled to switch it off instead. Confirm with the user first.",
    inputSchema: {
      type: "object",
      properties: { name: { type: "string" } },
      required: ["name"],
    },
  },
  {
    name: "set_check_enabled",
    description:
      "Switch any check on or off, built-in or custom, without deleting it. Use this to stop reporting something that is known and accepted. Confirm with the user first.",
    inputSchema: {
      type: "object",
      properties: {
        name: { type: "string" },
        enabled: { type: "boolean" },
      },
      required: ["name", "enabled"],
    },
  },
  {
    name: "add_server",
    description:
      "Add a database to servers.json so it appears in the dropdown and in list_servers. It takes no password: the entry is written with an empty one and the user fills it in. Confirm the details with the user first.",
    inputSchema: {
      type: "object",
      properties: {
        name: { type: "string", description: 'Label shown in the dropdown, e.g. "erp - postgres".' },
        host: { type: "string" },
        port: { type: "integer", description: "Default 5432." },
        user: { type: "string" },
        database: { type: "string" },
        sslmode: {
          type: "string",
          enum: ["disable", "allow", "prefer", "require", "verify-ca", "verify-full"],
          description: "Default require.",
        },
        logs_dir: {
          type: "string",
          description:
            "Optional path to that machine's MyApp database\\logs folder, which enables the export-folder and scheduler checks. Set it on ONE server entry only - several entries sharing a logs folder report the same log findings repeatedly.",
        },
      },
      required: ["name", "host", "user", "database"],
    },
  },
  {
    name: "remove_server",
    description:
      "Remove a database from servers.json. Nothing on the database itself is touched - this only stops db-checker checking it. Confirm with the user first.",
    inputSchema: {
      type: "object",
      properties: { name: { type: "string" } },
      required: ["name"],
    },
  },
];

// ------------------------------------------------------------ tool handlers

async function listServers() {
  const { list } = loadServers();
  if (list.length === 0) return textResult("servers.json has no entries yet.");
  const lines = list.map((s) => {
    const bits = [
      `${s.name}`,
      `  ${s.user}@${s.host}:${s.port ?? 5432}/${s.database}  sslmode=${s.sslmode ?? "require"}`,
      `  password ${s.password ? "set" : "NOT SET - fill it in in servers.json"}`,
      `  log checks ${s.logsDir ? `on (${s.logsDir})` : "off (no logsDir)"}`,
    ];
    return bits.join("\n");
  });
  return textResult(`${list.length} server(s) in servers.json:\n\n${lines.join("\n\n")}`);
}

async function listChecks() {
  const payload = await runPython(["--list-checks"]);
  const rows = payload.checks ?? [];
  const lines = rows.map((c) => {
    const flags = [
      c.enabled ? "on" : "OFF",
      c.builtin ? "built-in" : "custom",
      c.kind === "log" ? "needs logsDir" : null,
      c.sql_problem ? `BROKEN: ${c.sql_problem}` : null,
    ].filter(Boolean);
    return `${c.name}  [${c.severity}]  (${flags.join(", ")})\n    ${c.title}`;
  });
  const head = payload.config_error ? `WARNING: ${payload.config_error}\n\n` : "";
  return textResult(`${head}${rows.length} check(s):\n\n${lines.join("\n")}`);
}

async function runChecks(args) {
  const { list } = loadServers();
  const server = findServer(list, args.server);
  if (!server) {
    return textResult(
      `No server named "${args.server}". Known: ${list.map((s) => s.name).join(", ")}`,
      true,
    );
  }
  if (!server.password) {
    return textResult(
      `"${server.name}" has no password in servers.json. Fill it in there - I cannot take it from chat.`,
      true,
    );
  }

  const env = {
    DBC_VIA_APP: "1", // so an error message names servers.json, not .env
    PGHOST: String(server.host ?? ""),
    PGPORT: String(server.port ?? 5432),
    PGUSER: String(server.user ?? ""),
    PGDATABASE: String(server.database ?? ""),
    PGPASSWORD: String(server.password ?? ""),
    PGSSLMODE: String(server.sslmode ?? "require"),
  };
  if (server.logsDir) env.APP_LOGS_DIR = String(server.logsDir);
  if (server.psqlPath) env.PSQL_PATH = String(server.psqlPath);
  if (server.logDays) env.APP_LOG_DAYS = String(server.logDays);

  // --no-save by default: a run from chat should not move the weekly baseline that
  // "since last run" is measured against, the same rule the web app follows.
  //
  // The weekly agent is the exception. It is the thing that READS the diff and acts
  // on it, so it has to be the thing that advances the baseline too - otherwise a
  // week it fails to run is a week whose changes are silently never judged.
  // In --json mode the script writes no report files at all, so --no-save there
  // controls exactly one thing: whether this run becomes the new baseline.
  const flags = ["--json"];
  if (!args.save_baseline) flags.push("--no-save");
  if (args.only) flags.push("--only", String(args.only));

  let payload;
  try {
    payload = await runPython(flags, env);
  } catch (error) {
    return textResult(`Checks failed on "${server.name}": ${error.message}`, true);
  }

  const results = payload.results ?? [];
  const rank = { critical: 0, warn: 1, info: 2 };
  const withFindings = results
    .filter((r) => (r.findings ?? []).length > 0)
    .sort((a, b) => (rank[a.severity] ?? 3) - (rank[b.severity] ?? 3));
  const clean = results.filter((r) => !r.error && (r.findings ?? []).length === 0);
  const broken = results.filter((r) => r.error);

  const out = [`db-checker  ${payload.database}  (${new Date(payload.ran_at).toLocaleString()})`];

  const since = payload.since_last_run ?? {};
  if (since.partial_run) {
    out.push(
      `Only the "${since.only}" check ran, so there is no comparison with last week - ` +
        "a single-check run cannot tell what changed elsewhere.",
    );
  } else if (since.first_run) {
    out.push("Since last run: first run, nothing to compare.");
  } else if ((since.new ?? []).length || (since.fixed ?? []).length) {
    out.push(
      `Since ${since.previous_ran_at}: ${(since.new ?? []).length} new, ${(since.fixed ?? []).length} fixed`,
      ...(since.new ?? []).map((f) => `  NEW    ${f}`),
      ...(since.fixed ?? []).map((f) => `  FIXED  ${f}`),
    );
  } else {
    out.push(`Since ${since.previous_ran_at}: no change.`);
  }

  if (withFindings.length === 0) {
    out.push("", "Nothing actionable found.");
  }
  for (const r of withFindings) {
    out.push("", `[${r.severity.toUpperCase()}] ${r.title}  (${r.findings.length})`);
    for (const f of r.findings) {
      out.push(`  - ${f.object}: ${f.detail}`);
      if (f.fix) out.push(`      fix: ${f.fix}`);
      if (f.note) out.push(`      note: ${f.note}`);
    }
  }

  if (clean.length) out.push("", `Clean: ${clean.map((r) => r.name).join(", ")}`);
  if (broken.length) {
    out.push("", "Could not run:");
    out.push(...broken.map((r) => `  - ${r.name}: ${r.error}`));
  }
  out.push("", "Fixes are suggestions. Nothing was applied and nothing will be.");

  return textResult(out.join("\n"));
}

async function addCheck(args) {
  const name = String(args.name).trim();
  if (!/^[a-z][a-z0-9_]*$/.test(name)) {
    return textResult(
      `"${name}" is not a usable name. Use lowercase letters, digits and underscores, e.g. long_running_queries.`,
      true,
    );
  }

  const builtin = await runPython(["--list-checks"]);
  const clash = (builtin.checks ?? []).find((c) => c.name === name);
  if (clash) {
    return textResult(
      `A ${clash.builtin ? "built-in" : "custom"} check called "${name}" already exists. Pick another name, or remove that one first.`,
      true,
    );
  }

  // Validate through the Python validator rather than a second copy of the rules
  // here - two implementations of "is this SQL safe" would drift apart.
  const data = loadChecks();
  const entry = {
    name,
    title: String(args.title),
    severity: SEVERITIES.includes(args.severity) ? args.severity : "warn",
    enabled: true,
    sql: String(args.sql).trim(),
  };
  if (args.fix) entry.fix = String(args.fix);
  if (args.note) entry.note = String(args.note);

  data.checks.push(entry);
  writeJson(CHECKS_FILE, data);

  const after = await runPython(["--list-checks"]);
  const added = (after.checks ?? []).find((c) => c.name === name);
  if (added?.sql_problem) {
    // Put the file back rather than leaving a check that will error every week.
    data.checks = data.checks.filter((c) => c.name !== name);
    writeJson(CHECKS_FILE, data);
    return textResult(
      `Rejected and not saved: ${added.sql_problem}\n\nThe SQL must be one read-only SELECT or WITH returning a column named "object".`,
      true,
    );
  }

  return textResult(
    `Added "${name}" [${entry.severity}] to checks.json.\n\n${entry.title}\n\nIt runs on the next run_checks or the next scheduled run. Worth running it once now against a real server to see what it actually returns.`,
  );
}

async function removeCheck(args) {
  const name = String(args.name).trim();
  const data = loadChecks();
  const before = data.checks.length;
  data.checks = data.checks.filter((c) => String(c.name) !== name);
  if (data.checks.length === before) {
    const all = await runPython(["--list-checks"]);
    const isBuiltin = (all.checks ?? []).some((c) => c.name === name && c.builtin);
    return textResult(
      isBuiltin
        ? `"${name}" is a built-in check and cannot be deleted. Switch it off with set_check_enabled instead - that keeps it available if you want it back.`
        : `No custom check called "${name}" in checks.json.`,
      true,
    );
  }
  writeJson(CHECKS_FILE, data);
  return textResult(`Removed custom check "${name}" from checks.json. Previous file kept as checks.json.bak.`);
}

async function setCheckEnabled(args) {
  const name = String(args.name).trim();
  const enabled = Boolean(args.enabled);

  const all = await runPython(["--list-checks"]);
  const target = (all.checks ?? []).find((c) => c.name === name);
  if (!target) {
    return textResult(
      `No check called "${name}". Known: ${(all.checks ?? []).map((c) => c.name).join(", ")}`,
      true,
    );
  }

  const data = loadChecks();
  const off = new Set(data.disabled.map((n) => String(n)));
  if (enabled) off.delete(name);
  else off.add(name);
  data.disabled = [...off];

  // A custom check carries its own enabled flag too; keep the two agreeing so
  // the state does not depend on which one you happen to read.
  for (const c of data.checks) {
    if (String(c.name) === name) c.enabled = enabled;
  }

  writeJson(CHECKS_FILE, data);
  return textResult(
    `"${name}" is now ${enabled ? "ON" : "OFF"}.${
      enabled ? "" : " It stays in the file, so set_check_enabled can turn it back on."
    }`,
  );
}

async function addServer(args) {
  const name = String(args.name).trim();
  if (!name) return textResult("A server needs a name.", true);

  let data, list;
  try {
    ({ data, list } = loadServers());
  } catch {
    data = { servers: [] };
    list = data.servers;
  }

  if (list.some((s) => String(s.name).trim().toLowerCase() === name.toLowerCase())) {
    return textResult(`servers.json already has an entry called "${name}".`, true);
  }

  const entry = {
    name,
    host: String(args.host),
    port: Number(args.port ?? 5432),
    user: String(args.user),
    database: String(args.database),
    password: "",
    sslmode: String(args.sslmode ?? "require"),
  };
  if (args.logs_dir) entry.logsDir = String(args.logs_dir);

  const sharing = args.logs_dir
    ? list.filter((s) => s.logsDir && String(s.logsDir) === String(args.logs_dir))
    : [];

  list.push(entry);
  data.servers = list;
  writeJson(SERVERS_FILE, data);

  const notes = [
    `Added "${name}" (${entry.user}@${entry.host}:${entry.port}/${entry.database}) to servers.json.`,
    "",
    `Open servers.json and put the password in the "password" field for this entry - I deliberately do not take passwords, so it is empty until you do. The checks will refuse to run for it until then.`,
  ];
  if (sharing.length) {
    notes.push(
      "",
      `Heads up: ${sharing.map((s) => `"${s.name}"`).join(", ")} already use that same logs folder. Every entry sharing it reports the same log findings, which reads as the same problem on several servers. Usually you want logsDir on one entry only.`,
    );
  }
  return textResult(notes.join("\n"));
}

async function removeServer(args) {
  const { data, list } = loadServers();
  const target = findServer(list, args.name);
  if (!target) {
    return textResult(
      `No server named "${args.name}". Known: ${list.map((s) => s.name).join(", ")}`,
      true,
    );
  }
  data.servers = list.filter((s) => s !== target);
  writeJson(SERVERS_FILE, data);
  return textResult(
    `Removed "${target.name}" from servers.json. The database itself is untouched - it just is not checked any more. Previous file kept as servers.json.bak.`,
  );
}

// ---------------------------------------------------------------- transport

const server = new Server(
  { name: "db-checker", version: "1.0.0" },
  { capabilities: { tools: {} }, instructions: INSTRUCTIONS },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));

const HANDLERS = {
  list_servers: listServers,
  list_checks: listChecks,
  run_checks: runChecks,
  add_check: addCheck,
  remove_check: removeCheck,
  set_check_enabled: setCheckEnabled,
  add_server: addServer,
  remove_server: removeServer,
};

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const handler = HANDLERS[request.params.name];
  if (!handler) throw new Error(`Unknown tool: ${request.params.name}`);
  if (!existsSync(SCRIPT)) {
    return textResult(
      [
        "Cannot find db_checker.py. Looked in:",
        ...RESOLVED.tried.map((p) => `  ${p}`),
        "",
        'Set the "db-checker folder" setting for this extension to the folder holding',
        "db_checker.py, servers.json and reports\\ - then fully quit and reopen Claude",
        "Desktop, since toggling the extension does not always restart it.",
      ].join("\n"),
      true,
    );
  }
  try {
    return await handler(request.params.arguments ?? {});
  } catch (error) {
    // A thrown error would show up as a protocol failure; an error result shows
    // the reason, which is what someone needs in order to fix it.
    return textResult(error.message, true);
  }
});

await server.connect(new StdioServerTransport());
