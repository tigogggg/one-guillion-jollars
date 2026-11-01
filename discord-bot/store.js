// Where the members' portfolios live.
//
// With the D1 database bound as DB (created by the deploy workflow), each portfolio is a row
// with a version number: a save only succeeds if nobody saved in between (two tabs can no
// longer overwrite each other), and the last KEEP_VERSIONS versions stay for "undo".
// Once a day the Worker copies every portfolio and the members' KV keys into the backups table
// (KEEP_BACKUP_DAYS days). D1 itself also keeps 30 days of point-in-time history (Time Travel).
// Without DB, everything stays in KV as before. Portfolios still in KV move to D1 on first use.
//
// D1 also shares quotes and price histories between all visitors (quotes table), so Yahoo is
// asked at most once a minute per ticker.

const KEEP_VERSIONS = 30;
const KEEP_BACKUP_DAYS = 14;
const BACKUP_PREFIXES = ["portfolio-user:", "login-google:", "follows:"];

let schemaReady = null;

function schema(env) {
  if (!schemaReady) {
    schemaReady = env.DB.batch([
      env.DB.prepare(`CREATE TABLE IF NOT EXISTS portfolios (token TEXT PRIMARY KEY, owner TEXT, data TEXT NOT NULL,
        version INTEGER NOT NULL, updated TEXT NOT NULL)`),
      env.DB.prepare("CREATE INDEX IF NOT EXISTS portfolios_owner ON portfolios (owner)"),
      env.DB.prepare(`CREATE TABLE IF NOT EXISTS versions (token TEXT NOT NULL, version INTEGER NOT NULL, data TEXT NOT NULL,
        label TEXT, at TEXT NOT NULL, PRIMARY KEY (token, version))`),
      env.DB.prepare("CREATE TABLE IF NOT EXISTS backups (day TEXT NOT NULL, key TEXT NOT NULL, value TEXT, PRIMARY KEY (day, key))"),
      env.DB.prepare("CREATE TABLE IF NOT EXISTS quotes (key TEXT PRIMARY KEY, value TEXT NOT NULL, at INTEGER NOT NULL)"),
    ]).catch((e) => { schemaReady = null; throw e; });
  }
  return schemaReady;
}

export function hasDb(env) {
  return !!env.DB;
}

const now = () => new Date().toISOString();

// {data, version} or null. version 0 = KV (no versions).
export async function loadPortfolio(env, tok) {
  if (!env.DB) {
    const data = await env.CONFIG.get(`portfolio:${tok}`, "json");
    return data ? { data, version: 0 } : null;
  }
  await schema(env);
  const row = await env.DB.prepare("SELECT data, version FROM portfolios WHERE token = ?").bind(tok).first();
  if (row) return { data: JSON.parse(row.data), version: row.version };
  const old = await env.CONFIG.get(`portfolio:${tok}`, "json");  // not moved to D1 yet
  if (!old) return null;
  const text = JSON.stringify(old), at = now();
  await env.DB.batch([
    env.DB.prepare("INSERT OR IGNORE INTO portfolios (token, owner, data, version, updated) VALUES (?, ?, ?, 1, ?)").bind(tok, old.owner || null, text, at),
    env.DB.prepare("INSERT OR IGNORE INTO versions (token, version, data, label, at) VALUES (?, 1, ?, 'migrate', ?)").bind(tok, text, at),
  ]);
  const moved = await env.DB.prepare("SELECT data, version FROM portfolios WHERE token = ?").bind(tok).first();
  return moved ? { data: JSON.parse(moved.data), version: moved.version } : { data: old, version: 0 };
}

// The new version, or null if someone else saved since `version` was read.
export async function savePortfolio(env, tok, data, version, label = "") {
  if (!env.DB || !version) {
    await env.CONFIG.put(`portfolio:${tok}`, JSON.stringify(data));
    return version;
  }
  const text = JSON.stringify(data), at = now(), next = version + 1;
  const [update] = await env.DB.batch([
    env.DB.prepare("UPDATE portfolios SET data = ?, version = ?, updated = ?, owner = ? WHERE token = ? AND version = ?")
      .bind(text, next, at, data.owner || null, tok, version),
    env.DB.prepare(`INSERT OR IGNORE INTO versions (token, version, data, label, at)
      SELECT token, version, data, ?, updated FROM portfolios WHERE token = ? AND version = ? AND updated = ?`).bind(String(label).slice(0, 40), tok, next, at),
    env.DB.prepare("DELETE FROM versions WHERE token = ? AND version <= ?").bind(tok, next - KEEP_VERSIONS),
  ]);
  return update.meta.changes ? next : null;
}

export async function createPortfolio(env, tok, data) {
  if (!env.DB) return env.CONFIG.put(`portfolio:${tok}`, JSON.stringify(data));
  await schema(env);
  const text = JSON.stringify(data), at = now();
  await env.DB.batch([
    env.DB.prepare("INSERT INTO portfolios (token, owner, data, version, updated) VALUES (?, ?, ?, 1, ?)").bind(tok, data.owner || null, text, at),
    env.DB.prepare("INSERT OR IGNORE INTO versions (token, version, data, label, at) VALUES (?, 1, ?, 'create', ?)").bind(tok, text, at),
  ]);
}

// A new private link: the same portfolio and history under a new token.
export async function renamePortfolio(env, oldTok, newTok) {
  const cur = await loadPortfolio(env, oldTok);
  if (!env.DB) {
    if (cur) await env.CONFIG.put(`portfolio:${newTok}`, JSON.stringify(cur.data));
    await env.CONFIG.delete(`portfolio:${oldTok}`);
    return;
  }
  await env.DB.batch([
    env.DB.prepare("UPDATE portfolios SET token = ? WHERE token = ?").bind(newTok, oldTok),
    env.DB.prepare("UPDATE versions SET token = ? WHERE token = ?").bind(newTok, oldTok),
  ]);
  await env.CONFIG.delete(`portfolio:${oldTok}`);
}

export async function deletePortfolio(env, tok) {
  if (env.DB) {
    await schema(env);
    await env.DB.batch([
      env.DB.prepare("DELETE FROM portfolios WHERE token = ?").bind(tok),
      env.DB.prepare("DELETE FROM versions WHERE token = ?").bind(tok),
    ]);
  }
  await env.CONFIG.delete(`portfolio:${tok}`);
}

export async function listVersions(env, tok) {
  if (!env.DB) return [];
  await schema(env);
  const { results } = await env.DB.prepare("SELECT version, label, at FROM versions WHERE token = ? ORDER BY version DESC LIMIT ?")
    .bind(tok, KEEP_VERSIONS).all();
  return results;
}

export async function getVersion(env, tok, version) {
  if (!env.DB) return null;
  const row = await env.DB.prepare("SELECT data FROM versions WHERE token = ? AND version = ?").bind(tok, version).first();
  return row ? JSON.parse(row.data) : null;
}

// ---- Shared cache (quotes, price histories) ----

const memory = new Map();  // this Worker instance, on top of D1

export async function cached(env, key, seconds, compute) {
  const t = Date.now();
  const hit = memory.get(key);
  if (hit && t - hit.at < seconds * 1000) return hit.value;
  if (env.DB) {
    try {
      await schema(env);
      const row = await env.DB.prepare("SELECT value, at FROM quotes WHERE key = ?").bind(key).first();
      if (row && t - row.at < seconds * 1000) {
        const value = JSON.parse(row.value);
        memory.set(key, { value, at: row.at });
        return value;
      }
    } catch { /* the cache is optional */ }
  }
  const value = await compute();
  if (value == null) return hit ? hit.value : null;  // Yahoo failed: an older value beats nothing
  memory.set(key, { value, at: t });
  if (memory.size > 2000) memory.delete(memory.keys().next().value);
  if (env.DB) {
    try {
      await env.DB.prepare("INSERT INTO quotes (key, value, at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, at = excluded.at")
        .bind(key, JSON.stringify(value), t).run();
    } catch { /* idem */ }
  }
  return value;
}

// ---- Daily backup (from the cron) ----

async function kvList(env, prefix) {
  const out = [];
  let cursor;
  do {
    const page = await env.CONFIG.list({ prefix, cursor });
    out.push(...page.keys.map((k) => k.name));
    cursor = page.list_complete ? null : page.cursor;
  } while (cursor);
  return out;
}

export async function dailyBackup(env, day) {
  if (!env.DB) return null;
  await schema(env);
  if (await env.DB.prepare("SELECT 1 FROM backups WHERE day = ? LIMIT 1").bind(day).first()) return null;
  const rows = [];
  for (const prefix of BACKUP_PREFIXES) {
    for (const key of await kvList(env, prefix)) rows.push([key, await env.CONFIG.get(key)]);
  }
  for (const key of await kvList(env, "portfolio:")) await loadPortfolio(env, key.slice("portfolio:".length));  // move the rest to D1
  const stmts = [env.DB.prepare(`INSERT OR IGNORE INTO backups (day, key, value) SELECT ?, 'portfolio:' || token, data FROM portfolios`).bind(day)];
  for (const [key, value] of rows) stmts.push(env.DB.prepare("INSERT OR IGNORE INTO backups (day, key, value) VALUES (?, ?, ?)").bind(day, key, value));
  const cutoff = new Date(Date.parse(day) - KEEP_BACKUP_DAYS * 864e5).toISOString().slice(0, 10);
  stmts.push(env.DB.prepare("DELETE FROM backups WHERE day < ?").bind(cutoff));
  stmts.push(env.DB.prepare("DELETE FROM quotes WHERE at < ?").bind(Date.now() - 7 * 864e5));
  for (let i = 0; i < stmts.length; i += 50) await env.DB.batch(stmts.slice(i, i + 50));
  return rows.length;
}
