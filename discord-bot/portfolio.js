// Personal portfolios for the Discord server's members, served by the Worker.
//
// /carteira (only inside the server, so only its members) gives each member one
// private link, /c/<token>, tied to their Discord account: KV keeps
// portfolio-user:<discord id> -> token and portfolio:<token> -> {owner, name, tx}.
// /carteira novo_link:true replaces the link (the old one stops working).
//
// The page lists the member's buys and sells, values the holdings with Yahoo
// Finance quotes (refreshed every minute, usually ~15 min behind the exchange),
// converts everything to euros, and draws the value since the first purchase.

import { avatarUrl, esc, html, memberFace, page, safeJson, siteTopbar, staticAsset } from "./chart_page.js";
import { currentSession, loginActive } from "./session.js";
import { cached, createPortfolio, deletePortfolio, getVersion, hasDb, listVersions, loadPortfolio, renamePortfolio, savePortfolio } from "./store.js";

const MAX_TX = 3000;
const MAX_IMPORT = 2000;
const ACCOUNT = /^[\p{L}\p{N} ._\-]{1,40}$/u;
const CURRENCY = /^[A-Z]{3}$/;
const SYMBOL = /^[A-Z0-9][A-Z0-9.\-^=]{0,14}$/;
const DAY = /^\d{4}-\d{2}-\d{2}$/;
const UA = { "User-Agent": "Mozilla/5.0 (compatible; CoutoTradingBot/1.0)" };

// Display name ("Nome a apresentar"): what other members see. 2-24 characters, no @ or links, not an agent's
// name, and unique among members (KV display-name:<key> -> Discord id, the key without case, accents or symbols).
const NAME_MIN = 2, NAME_MAX = 24;
const NAME_LINK = /:\/\/|www\.|discord\.gg|\.(com|pt|net|org|io|gg|me|ly|xyz|app|dev)\b/i;
const RESERVED = ["One Guillion Jollars", "Indexador", "Guardião", "Rotador", "Pêndulo", "Foguetão", "Campeão Setorial", "Kamikaze",
                  "Caçador", "Sniper", "Pescador de Quedas", "Relâmpago", "Joalheiro", "Analista", "Quant", "Monge", "Abutre",
                  "Jornalista", "Deriva", "Catalisador", "Cético", "Conselho", "Maestro"];

const AVATAR = /^(a_)?[0-9a-f]{32}$/;  // a Discord avatar hash (the portal draws it with avatarUrl)

// Settings kept in the account (the portal and every device read them): capital and risk for sizing the proposals,
// and the portal tab that opens first. Only the fields sent are changed; an error message, or null.
export function setPreferences(data, body) {
  const s = { capital: 10000, risk: 1, ...(data.settings || {}) };
  if (body.capital !== undefined) {
    const capital = Number(body.capital);
    if (!(capital >= 0 && capital <= 1e9)) return "Capital inválido.";
    s.capital = capital;
  }
  if (body.risk !== undefined) {
    const risk = Number(body.risk);
    if (!(risk > 0 && risk <= 5)) return "Risco inválido (entre 0,1% e 5%).";
    s.risk = risk;
  }
  if (body.start !== undefined) {
    const start = String(body.start);
    if (start && !/^[a-z]{3,15}$/.test(start)) return "Separador inválido.";
    if (start) s.start = start; else delete s.start;
  }
  data.settings = s;
  return null;
}

export function nameKey(name) {
  return String(name).normalize("NFD").replace(/\p{M}/gu, "").toLowerCase().replace(/[^\p{L}\p{N}]/gu, "");
}

// [clean name, null] or [null, error]; "" means "go back to the Discord name".
export function checkDisplayName(raw, agentNames = []) {
  const name = String(raw ?? "").replace(/\s+/g, " ").trim();
  if (!name) return ["", null];
  const len = [...name].length;
  if (len < NAME_MIN || len > NAME_MAX) return [null, `O nome tem de ter entre ${NAME_MIN} e ${NAME_MAX} caracteres.`];
  if (/[\p{C}<>]/u.test(name)) return [null, "O nome tem caracteres que não são permitidos."];
  if (name.includes("@") || NAME_LINK.test(name)) return [null, "O nome não pode ter @ nem links."];
  const key = nameKey(name);
  if (key.length < NAME_MIN) return [null, "O nome tem de ter pelo menos 2 letras ou números."];
  if (RESERVED.concat(agentNames).some((n) => nameKey(n) === key)) return [null, "Esse nome é de um agente da liga. Escolhe outro."];
  return [name, null];
}

// The current agents' names (agents can be created with /agente), from the latest league page.
async function agentNames(env) {
  const id = await env.CONFIG.get("chart-latest:liga", "json");
  const page = id ? await env.CONFIG.get(`chart:${id}`, "json") : null;
  return (page?.agents || []).map((a) => a.name).filter(Boolean);
}

async function setDisplayName(env, tok, data, raw) {
  const [name, error] = checkDisplayName(raw, await agentNames(env));
  if (error) return { error };
  const oldKey = data.display_name ? nameKey(data.display_name) : null, newKey = name ? nameKey(name) : null;
  if (newKey && newKey !== oldKey) {
    const holder = await env.CONFIG.get(`display-name:${newKey}`);
    if (holder && holder !== data.owner) return { error: "Esse nome já está a ser usado por outro membro." };
    await env.CONFIG.put(`display-name:${newKey}`, data.owner);
  }
  const out = await mutate(env, tok, "display_name", (d) => { if (name) d.display_name = name; else delete d.display_name; });
  if (out.error) {
    if (newKey && newKey !== oldKey) await env.CONFIG.delete(`display-name:${newKey}`);
    return out;
  }
  if (oldKey && oldKey !== newKey) await env.CONFIG.delete(`display-name:${oldKey}`);
  return out;
}

function token() {
  const bytes = crypto.getRandomValues(new Uint8Array(12));
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// The member's portfolio token, creating the portfolio on first use; fresh replaces the link.
export async function ensurePortfolio(env, user, fresh = false) {
  const userKey = `portfolio-user:${user.id}`;
  let current = await env.CONFIG.get(userKey);
  const name = user.global_name || user.username;
  const avatar = AVATAR.test(user.avatar || "") ? user.avatar : null;  // the Discord avatar's hash, kept fresh at every sign-in
  let cur = current ? await loadPortfolio(env, current) : null;
  if (!cur) {
    current = token();
    await createPortfolio(env, current, { owner: user.id, name, avatar, created: new Date().toISOString(), tx: [] });
    await env.CONFIG.put(userKey, current);
    return current;
  }
  if (fresh) {
    const next = token();
    await renamePortfolio(env, current, next);
    await env.CONFIG.put(userKey, next);
    current = next;
    cur = await loadPortfolio(env, current);
  }
  if ((name && cur.data.name !== name) || (cur.data.avatar ?? null) !== avatar) {
    if (name) cur.data.name = name;
    cur.data.avatar = avatar;
    await savePortfolio(env, current, cur.data, cur.version, "name");
  }
  return current;
}

// /carteira: the member's link (created on first use, replaced with novo_link).
export async function portfolioCommand(env, interaction, origin) {
  const member = interaction.member?.user;
  if (!member) return "⚠️ Usa `/carteira` dentro do servidor: as carteiras são só para os membros.";
  const fresh = (interaction.data.options || []).some((o) => o.name === "novo_link" && o.value);
  const current = await ensurePortfolio(env, member, fresh);
  return [
    `💼 **A tua carteira** (só tu vês esta mensagem)`,
    `${origin}/c/${current}`,
    fresh ? "🔄 Link novo: o anterior deixou de funcionar." : "",
    `-# Abre com a tua sessão do Discord ou Google (entras em ${origin}/). O link sozinho não chega.`,
  ].filter(Boolean).join("\n");
}

// Load, change and save, again from the start if someone saved in between (another tab).
// fn(data) returns {error} or a result; nothing is saved on error.
async function mutate(env, tok, label, fn) {
  for (let attempt = 0; attempt < 3; attempt++) {
    const cur = await loadPortfolio(env, tok);
    if (!cur) return { error: "Carteira não encontrada.", status: 404 };
    const out = (await fn(cur.data)) || {};
    if (out.error) return { ...out, data: cur.data };
    if ((await savePortfolio(env, tok, cur.data, cur.version, label)) != null) return { ...out, data: cur.data };
  }
  return { error: "A carteira mudou noutro sítio ao mesmo tempo. Tenta outra vez.", status: 409 };
}

function redirect(location, headers = {}) {
  return new Response(null, { status: 302, headers: { Location: location, "Cache-Control": "no-store", ...headers } });
}

// /perfil: the signed-in member's profile, with their portfolio (its API stays at /c/<token>/...).
export async function profileRoute(request, env) {
  const session = await currentSession(env, request);
  if (!session) return redirect(`/entrar?voltar=${encodeURIComponent("/perfil")}`);
  const login = await loginActive(env);
  const resp = html(portfolioPage(session.data, session.tok, { signedIn: true, loginOn: login.any, googleOn: login.google, versions: hasDb(env), profile: true }));
  resp.headers.set("Cache-Control", "no-store");
  return resp;
}

// /c/<token>, /c/<token>/data (GET quotes, POST changes), /history, /analysis, /versions
export async function portfolioRoute(request, env, tok, rest, dispatch) {
  ENV = env;
  const cur = await loadPortfolio(env, tok);
  if (!cur) return html(page("Carteira", "<p class='muted'>Link inválido ou substituído. Entra em <a href='/entrar'>/entrar</a> ou pede o teu link com /carteira no Discord.</p>"), 404);
  let data = cur.data;
  const session = await currentSession(env, request);
  const mine = session?.owner === data.owner;
  if (!mine && (await loginActive(env)).any) {  // the link alone is not enough: sign in as the owner
    return rest === "" ? redirect(`/entrar?voltar=${encodeURIComponent(`/c/${tok}`)}`) : json({ error: "Sessão terminada: entra outra vez em /entrar." }, 401);
  }
  if (rest === "" && request.method === "GET") {
    const login = await loginActive(env);
    const embed = new URL(request.url).searchParams.get("embed") === "1";
    if (mine && !embed) return redirect("/perfil");  // the owner's portfolio lives in their profile page; old links land there
    const resp = html(portfolioPage(data, tok, { signedIn: mine, loginOn: login.any, googleOn: login.google, versions: hasDb(env), embed }));
    if (embed) resp.headers.set("X-Embed", "1");  // the worker allows this page inside the portal (same site only)
    return resp;
  }
  if (rest === "/data" && request.method === "GET") return json(await snapshot(env, data));
  if (rest === "/history" && request.method === "GET") return json(await history(env, data, new URL(request.url).searchParams.get("escala") === "hora"));
  if (rest === "/analysis" && request.method === "GET") return json(await analysisStatus(env, data));
  if (rest === "/versions" && request.method === "GET") return json({ versions: await listVersions(env, tok), current: cur.version });
  if (rest === "/export" && request.method === "GET") {
    const day = new Date().toISOString().slice(0, 10);
    const { session_epoch, google, analyses, ...out } = data;
    return new Response(JSON.stringify({ exported: new Date().toISOString(), ...out, google: google ? { email: google.email } : undefined }, null, 1), {
      headers: { "Content-Type": "application/json", "Content-Disposition": `attachment; filename="carteira-${day}.json"`, "Cache-Control": "no-store" } });
  }
  if (rest === "/analysis" && request.method === "POST") {
    const out = await requestAnalysis(env, data, dispatch);
    if (out.error) return json({ error: out.error }, 400);
    const saved = await mutate(env, tok, "analysis", (d) => { d.analyses = (d.analyses || []).concat([out.entry]).slice(-20); });
    return json(await analysisStatus(env, saved.data || data));
  }
  if (rest === "/manifest.webmanifest") return manifest(data, tok);
  if (rest !== "/data" || request.method !== "POST") return json({ error: "Não encontrado." }, 404);

  let body;
  try { body = await request.json(); } catch { return json({ error: "Pedido inválido." }, 400); }
  const fail = (out) => json({ error: out.error }, out.status || 400);
  const action = String(body.action || "add");

  if (action === "follow_add" || action === "follow_remove") {
    const error = await follow(env, data, body);
    return error ? fail({ error }) : json(await snapshot(env, data));
  }
  if (action === "copy") {
    const error = await setCopy(env, data, body);
    return error ? fail({ error }) : json(await snapshot(env, data));
  }
  if (action === "display_name") {
    const out = await setDisplayName(env, tok, data, body.name);
    return out.error ? fail(out) : json({ ok: true, display_name: out.data.display_name || "", name: out.data.name || "" });
  }
  if (action === "preferences") {  // answered without the quotes
    const out = await mutate(env, tok, "preferences", (d) => { const error = setPreferences(d, body); return error ? { error } : null; });
    return out.error ? fail(out) : json({ ok: true, settings: out.data.settings });
  }
  if (action === "security") {
    if (body.require_login && !mine) return fail({ error: "Entra primeiro com o Discord ou o Google neste aparelho (em /entrar): assim confirmas que consegues entrar antes de trancar o link." });
    const out = await mutate(env, tok, "security", (d) => { d.require_login = !!body.require_login; });
    return out.error ? fail(out) : json({ ok: true, require_login: out.data.require_login });
  }
  if (action === "logout_all") {
    const out = await mutate(env, tok, "logout_all", (d) => { d.session_epoch = (d.session_epoch || 0) + 1; });
    return out.error ? fail(out) : json({ ok: true });
  }
  if (action === "google_unlink") {
    const out = await mutate(env, tok, "google_unlink", (d) => {
      const sub = d.google?.sub;
      delete d.google;
      return { sub };
    });
    if (out.error) return fail(out);
    if (out.sub) await env.CONFIG.delete(`login-google:${out.sub}`);
    return json({ ok: true });
  }
  if (action === "delete_all") {
    if (body.confirm !== "APAGAR") return fail({ error: "Escreve APAGAR para confirmar." });
    await deleteEverything(env, tok, data);
    return json({ ok: true, deleted: true });
  }
  if (action === "restore") {
    const version = Number(body.version);
    const old = Number.isInteger(version) ? await getVersion(env, tok, version) : null;
    if (!old) return fail({ error: "Versão não encontrada." });
    const out = await mutate(env, tok, `restore:${version}`, (d) => {
      for (const k of ["tx", "accounts", "dividends", "settings", "profile"]) {
        if (old[k] === undefined) delete d[k]; else d[k] = old[k];
      }
    });
    return out.error ? fail(out) : json(await snapshot(env, out.data));
  }

  let extra = {};
  const out = await mutate(env, tok, action, async (d) => {
    if (action === "import") {
      const r = await importRows(d, body);
      if (r.error) return r;
      extra = { imported: r };
    } else if (action === "dedupe") {
      const r = dedupe(d);
      if (r.error) return r;
      extra = { deduped: r.removed };
    } else if (action === "account") {
      const error = setAccount(d, body);
      if (error) return { error };
    } else {
      const error = await change(d, body);
      if (error) return { error };
    }
    return null;
  });
  if (out.error) return fail(out);
  return json({ ...(await snapshot(env, out.data)), ...extra });
}

// RGPD: the portfolio, its versions, the links to Discord and Google, the follow list and the analyses.
async function deleteEverything(env, tok, data) {
  const owner = data.owner;
  await deletePortfolio(env, tok);
  if (owner && (await env.CONFIG.get(`portfolio-user:${owner}`)) === tok) await env.CONFIG.delete(`portfolio-user:${owner}`);
  if (data.google?.sub) await env.CONFIG.delete(`login-google:${data.google.sub}`);
  if (data.display_name) await env.CONFIG.delete(`display-name:${nameKey(data.display_name)}`);
  if (owner) await env.CONFIG.delete(`follows:${owner}`);
  if (owner) await env.CONFIG.delete(`copiar:${owner}`);
  for (const a of data.analyses || []) {
    await env.CONFIG.delete(`analysis-res:${a.rid}`);
    await env.CONFIG.delete(`analysis-req:${a.rid}`);
  }
}

const MAX_FOLLOWS = 20;
const FOLLOW = /^[A-Z0-9][A-Z0-9.\-]{0,9}$/;

// The member's /seguir list (the same KV key the Discord command and the DM alerts use).
async function follow(env, data, body) {
  if (!data.owner) return "Carteira sem dono no Discord.";
  const key = `follows:${data.owner}`;
  const list = (await env.CONFIG.get(key, "json")) || [];
  const t = String(body.symbol || "").trim().toUpperCase();
  if (!FOLLOW.test(t)) return "Ticker inválido (ex.: NVDA).";
  if (body.action === "follow_add") {
    if (list.includes(t)) return null;
    if (list.length >= MAX_FOLLOWS) return `A lista tem no máximo ${MAX_FOLLOWS} ações.`;
    if (!(await yahooQuote(t))) return `Não encontrei ${t} no Yahoo Finance.`;
    list.push(t);
  } else {
    const i = list.indexOf(t);
    if (i >= 0) list.splice(i, 1);
  }
  if (list.length) await env.CONFIG.put(key, JSON.stringify(list));
  else await env.CONFIG.delete(key);
  return null;
}

// The agents a member can follow: portal:agentes (scripts/agents.py publish_agent_list), or the latest league page's
// agents until that list exists.
async function followableAgents(env) {
  const list = await env.CONFIG.get("portal:agentes", "json");
  if (Array.isArray(list) && list.length) return list;
  const latest = await env.CONFIG.get("chart-latest:liga", "json");
  const chart = latest ? await env.CONFIG.get(`chart:${latest}`, "json") : null;
  return ((chart && chart.agents) || []).filter((a) => a.id && a.active !== false)
    .map((a) => ({ id: a.id, name: a.name, emoji: a.emoji || "🤖", perfil: a.perfil, congelado: !!a.congelado }));
}

// The agent the member follows (KV copiar:<discord id>): scripts/copy_alerts.py sends them a private message on
// Discord each time it buys or sells. An empty agent stops it.
async function setCopy(env, data, body) {
  if (!data.owner) return "Carteira sem dono no Discord.";
  const key = `copiar:${data.owner}`;
  const id = String(body.agent || "").trim();
  if (!id) { await env.CONFIG.delete(key); return null; }
  const list = await followableAgents(env);
  if (!list.some((a) => a.id === id)) return "Esse agente não está na liga.";
  const valor = body.valor === "" || body.valor == null ? null : Number(body.valor);
  if (valor != null && !(valor >= 0 && valor <= 10000000)) return "Valor inválido.";
  await env.CONFIG.put(key, JSON.stringify({ agente: id, valor_eur: valor || null, desde: new Date().toISOString().slice(0, 10) }));
  return null;
}

function manifest(data, tok) {
  return new Response(JSON.stringify({
    name: `Carteira de ${data.name || "membro"}`, short_name: "Carteira", start_url: `/c/${tok}`, scope: `/c/${tok}`,
    display: "standalone", background_color: "#0b0820", theme_color: "#0b0820",
    icons: [{ src: "/icon-192.png?v=1gj", sizes: "192x192", type: "image/png" }, { src: "/icon-512.png?v=1gj", sizes: "512x512", type: "image/png" }],
  }), { headers: { "Content-Type": "application/manifest+json" } });
}

function json(value, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });
}

// ---------------------------------------------------------------------------
// Transactions
// ---------------------------------------------------------------------------

function heldOn(tx, symbol, day, skipId) {
  let qty = 0;
  for (const t of tx.filter((t) => t.symbol === symbol && t.id !== skipId).sort((a, b) => a.date.localeCompare(b.date) || a.seq - b.seq)) {
    if (t.date > day) break;
    qty += t.side === "buy" ? t.qty : -t.qty;
  }
  return qty;
}

// Brokerage accounts with their available cash, e.g. one per XTB currency account.
function setAccount(data, body) {
  const name = String(body.name || "").trim();
  if (!ACCOUNT.test(name)) return "Nome de conta inválido.";
  data.accounts ||= {};
  if (body.remove) {
    if (body.withTx) {
      const left = data.tx.filter((t) => t.account !== name);
      for (const t of left.filter((x) => x.side === "sell")) {  // other accounts' sales must still have their shares
        if (heldOn(left, t.symbol, t.date, t.id) < t.qty - 1e-6) return `Não posso apagar: a venda de ${t.symbol} a ${t.date} ficaria sem ações.`;
      }
      data.tx = left;
    }
    delete data.accounts[name];
    return null;
  }
  const currency = String(body.currency || data.accounts[name]?.currency || "EUR").toUpperCase();
  const cash = Number(body.cash);
  if (!CURRENCY.test(currency)) return "Moeda inválida (ex.: EUR, USD).";
  if (!isFinite(cash) || Math.abs(cash) > 1e10) return "Valor do dinheiro inválido.";
  data.accounts[name] = { ...(data.accounts[name] || {}), currency, cash, updated: new Date().toISOString().slice(0, 10), source: body.source || "manual",
                          ...(body.number ? { number: String(body.number).slice(0, 20) } : {}) };
  return null;
}

// A broker export already read in the browser: {account, currency, cash?, rows: [{side, symbol, date, qty, price, fee, note, ext}]}.
// Rows already imported (same account and ext) are skipped; unknown tickers and sales without the shares are reported.
// The same operation, whatever account or import it came from.
const fingerprint = (t) => [t.side, t.symbol, t.date, Number(t.qty).toFixed(6), Number(t.price).toFixed(4)].join("|");

// Remove repeated operations (same side, ticker, date, quantity and price), keeping the first.
function dedupe(data) {
  const seen = new Set(), keep = [];
  for (const t of data.tx.slice().sort((a, b) => (a.seq || 0) - (b.seq || 0))) {
    const f = fingerprint(t);
    if (seen.has(f)) continue;
    seen.add(f);
    keep.push(t);
  }
  for (const t of keep.filter((x) => x.side === "sell")) {
    if (heldOn(keep, t.symbol, t.date, t.id) < t.qty - 1e-6) return { error: `Não posso limpar: a venda de ${t.symbol} a ${t.date} ficaria sem ações.` };
  }
  const removed = data.tx.length - keep.length;
  data.tx = keep;
  return { removed };
}

async function importRows(data, body) {
  let account = String(body.account || "").trim();
  if (!ACCOUNT.test(account)) return { error: "Indica o nome da conta (ex.: XTB EUR)." };
  const number = String(body.number || "").replace(/\D/g, "").slice(0, 20);
  const sameNumber = number && Object.entries(data.accounts || {}).find(([, a]) => a.number === number);
  if (sameNumber) account = sameNumber[0];  // the same XTB account, whatever name was typed this time
  const rows = Array.isArray(body.rows) ? body.rows.slice(0, MAX_IMPORT) : [];
  const have = new Set(data.tx.filter((t) => t.ext).map((t) => t.ext));
  const prints = new Set(data.tx.map(fingerprint));
  const skipped = [], quotes = {};
  let seq = data.tx.reduce((m, x) => Math.max(m, x.seq || 0), 0);
  const fresh = [];
  for (const r of rows) {
    const symbol = String(r.symbol || "").trim().toUpperCase();
    const side = r.side === "sell" ? "sell" : r.side === "buy" ? "buy" : null;
    const qty = Number(r.qty), price = Number(r.price), fee = Number(r.fee || 0), date = String(r.date || "");
    const ext = String(r.ext || "").slice(0, 60);
    if (ext && have.has(ext)) { skipped.push({ symbol, date, reason: "já importada" }); continue; }
    if (prints.has(fingerprint({ side, symbol, date, qty, price }))) { skipped.push({ symbol, date, reason: "já existe na carteira" }); continue; }
    if (!side || !SYMBOL.test(symbol) || !DAY.test(date) || !(qty > 0) || !(price > 0) || !(fee >= 0)) {
      skipped.push({ symbol, date, reason: "linha incompleta" }); continue;
    }
    if (!(symbol in quotes)) quotes[symbol] = await yahooQuote(symbol);
    const q = quotes[symbol];
    if (!q) { skipped.push({ symbol, date, reason: "ticker desconhecido no Yahoo Finance" }); continue; }
    fresh.push({ id: token().slice(0, 12), seq: ++seq, side, symbol, date, qty, price, fee, currency: q.currency, name: q.name,
                 note: String(r.note || "").slice(0, 120), account, ext });
    if (ext) have.add(ext);
    prints.add(fingerprint({ side, symbol, date, qty, price }));
  }
  if (data.tx.length + fresh.length > MAX_TX) return { error: `Limite de ${MAX_TX} operações na carteira.` };
  // a sale needs the shares on its date: drop the ones the file cannot back (e.g. history cut short)
  let next = data.tx.concat(fresh);
  for (const t of fresh.filter((x) => x.side === "sell").sort((a, b) => a.date.localeCompare(b.date))) {
    if (heldOn(next, t.symbol, t.date, t.id) < t.qty - 1e-6) {
      next = next.filter((x) => x.id !== t.id);
      skipped.push({ symbol: t.symbol, date: t.date, reason: "venda sem compra correspondente no ficheiro" });
    }
  }
  const added = next.length - data.tx.length;
  data.dividends ||= [];
  const seenDiv = new Set(data.dividends.map((d) => d.ext));
  for (const d of (Array.isArray(body.dividends) ? body.dividends : []).slice(0, 2000)) {
    const ext = String(d.ext || "").slice(0, 60), amount = Number(d.amount), symbol = String(d.symbol || "").toUpperCase();
    if (!ext || seenDiv.has(ext) || !DAY.test(String(d.date || "")) || !isFinite(amount) || !SYMBOL.test(symbol)) continue;
    data.dividends.push({ symbol, date: d.date, amount, currency: body.currency || "EUR", account, ext });
    seenDiv.add(ext);
  }
  data.tx = next;
  if (body.cash != null && body.cash !== "") {
    setAccount(data, { name: account, currency: body.currency, cash: body.cash, source: "importado", number });
  } else if (!data.accounts?.[account]) {
    setAccount(data, { name: account, currency: body.currency || "EUR", cash: 0, source: "importado sem saldo", number });
  }
  return { added, account, merged: !!sameNumber, skipped: skipped.slice(0, 200), skippedCount: skipped.length };
}

async function change(data, body) {
  if (body.action === "delete") {
    const t = data.tx.find((x) => x.id === body.id);
    if (!t) return "Operação não encontrada.";
    if (t.side === "buy") {  // removing a buy must not leave a later sell without shares
      const rest = data.tx.filter((x) => x.id !== t.id);
      for (const s of rest.filter((x) => x.symbol === t.symbol && x.side === "sell")) {
        if (heldOn(rest, t.symbol, s.date, s.id) < s.qty - 1e-9) return `Primeiro apaga a venda de ${s.date}: ficaria sem ações para vender.`;
      }
    }
    data.tx = data.tx.filter((x) => x.id !== body.id);
    return null;
  }
  if (body.action === "profile") {
    const p = body.profile || {};
    const pick = (v, ok) => (ok.includes(v) ? v : null);
    const n = (v, lo, hi) => (v === "" || v == null ? null : Number.isFinite(+v) && +v >= lo && +v <= hi ? +v : null);
    data.profile = {
      horizon_years: n(p.horizon_years, 0, 80),
      goal: pick(p.goal, ["crescimento", "reforma", "casa", "rendimento", "preservar", "outro"]),
      max_drawdown_pct: n(p.max_drawdown_pct, 0, 100),
      monthly_eur: n(p.monthly_eur, 0, 1e7),
      emergency_months: n(p.emergency_months, 0, 120),
      experience: pick(p.experience, ["iniciante", "intermedio", "avancado"]),
      etf_preference: pick(p.etf_preference, ["acumulacao", "distribuicao", "indiferente"]),
      other: String(p.other || "").slice(0, 300),
      avoid: String(p.avoid || "").slice(0, 200),
      notes: String(p.notes || "").slice(0, 500),
      updated: new Date().toISOString().slice(0, 10),
    };
    return null;
  }
  if (body.action === "settings") {
    const capital = Number(body.capital), risk = Number(body.risk);
    if (!(capital >= 0 && capital <= 1e9) || !(risk > 0 && risk <= 5)) return "Capital ou risco inválido (risco entre 0,1% e 5%).";
    data.settings = { ...(data.settings || {}), capital, risk };  // keeps the start tab
    return null;
  }
  if (body.action === "preferences") return setPreferences(data, body);
  if (body.action !== "add" && body.action !== "edit") return "Ação desconhecida.";
  const editing = body.action === "edit" ? data.tx.find((x) => x.id === body.id) : null;
  if (body.action === "edit" && !editing) return "Operação não encontrada.";
  if (!editing && data.tx.length >= MAX_TX) return `Limite de ${MAX_TX} operações.`;
  const t = body.tx || {};
  const symbol = String(t.symbol || "").trim().toUpperCase();
  const side = t.side === "sell" ? "sell" : t.side === "buy" ? "buy" : null;
  const qty = Number(t.qty), price = Number(t.price), fee = Number(t.fee || 0);
  const date = String(t.date || "");
  if (!side) return "Escolhe compra ou venda.";
  if (!SYMBOL.test(symbol)) return "Ticker inválido (ex.: AAPL, NVDA, VWCE.DE, SXR8.DE).";
  if (!DAY.test(date) || date > new Date().toISOString().slice(0, 10) || date < "1990-01-01") return "Data inválida.";
  if (!(qty > 0) || qty > 1e9) return "Quantidade inválida.";
  if (!(price > 0) || price > 1e9) return "Preço inválido.";
  if (!(fee >= 0) || fee > 1e7) return "Comissão inválida.";
  const quote = await yahooQuote(symbol);
  if (!quote) return `Não encontrei ${symbol} no Yahoo Finance. Nas bolsas europeias junta o sufixo (ex.: VWCE.DE, ASML.AS).`;
  const others = editing ? data.tx.filter((x) => x.id !== editing.id) : data.tx;
  if (side === "sell" && heldOn(others, symbol, date) < qty - 1e-9) return `Em ${date} não tinhas ${qty} ${symbol} para vender.`;
  const seq = editing ? editing.seq : data.tx.reduce((m, x) => Math.max(m, x.seq || 0), 0) + 1;
  const row = { id: editing ? editing.id : token().slice(0, 12), seq, side, symbol, date, qty, price, fee, currency: quote.currency,
                name: quote.name, note: String(t.note || "").slice(0, 120) };
  const next = others.concat([row]);
  // every sale, of this symbol and of the one it replaced, must still have the shares on its date
  for (const sym of new Set([symbol, editing?.symbol].filter(Boolean))) {
    for (const x of next.filter((y) => y.symbol === sym && y.side === "sell")) {
      if (heldOn(next, sym, x.date, x.id) < x.qty - 1e-9) return `Assim a venda de ${x.qty} ${sym} a ${x.date} ficaria sem ações.`;
    }
  }
  data.tx = next;
  return null;
}

// ---------------------------------------------------------------------------
// Prices (Yahoo Finance chart API, cached a minute at Cloudflare's edge)
// ---------------------------------------------------------------------------

async function yahoo(symbol, params) {
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}?${params}`;
  try {
    const resp = await fetch(url, { headers: UA, cf: { cacheTtl: 60, cacheEverything: true } });
    if (!resp.ok) return null;
    return (await resp.json())?.chart?.result?.[0] || null;
  } catch {
    return null;
  }
}

// One shared copy per ticker: quotes for a minute, daily histories for 6 hours (see store.js).
let ENV = null;  // set by the route; the price helpers below are called from many places

async function yahooCached(symbol, params, seconds) {
  if (!ENV) return yahoo(symbol, params);
  return cached(ENV, `y:${symbol}:${params}`, seconds, async () => {
    const r = await yahoo(symbol, params);
    if (!r) return null;
    const q = r.indicators?.quote?.[0] || {};  // keep only what is used, small enough to share
    return { meta: r.meta, timestamp: r.timestamp, indicators: { quote: [{ close: q.close, high: q.high, low: q.low }] } };
  });
}

async function yahooQuote(symbol) {
  const r = await yahooCached(symbol, "range=5d&interval=1d", 60);
  const m = r?.meta;
  if (!m || m.regularMarketPrice == null) return null;
  const closes = (r.indicators?.quote?.[0]?.close || []).filter((x) => x != null);
  const prev = m.chartPreviousClose != null && closes.length < 2 ? m.chartPreviousClose : closes.length >= 2 ? closes[closes.length - 2] : m.chartPreviousClose;
  return { price: m.regularMarketPrice, prev, currency: m.currency || "USD", name: m.shortName || m.longName || "",
           time: (m.regularMarketTime || 0) * 1000, exchange: m.exchangeName || "" };
}

// Euros per unit of a currency (GBp = pence).
async function fxToEur(currency) {
  if (!currency || currency === "EUR") return 1;
  const base = currency === "GBp" || currency === "GBX" ? "GBP" : currency;
  const q = await yahooQuote(`EUR${base}=X`);
  if (!q) return null;
  return (currency === "GBp" || currency === "GBX" ? 0.01 : 1) / q.price;
}

async function snapshot(env, data) {
  const symbols = [...new Set(data.tx.map((t) => t.symbol))];
  const [quotes, proposals, news] = await Promise.all([
    Promise.all(symbols.map(async (s) => [s, await yahooQuote(s)])).then(Object.fromEntries),
    env.CONFIG.get("portal:proposals", "json"),
    env.CONFIG.get("portal:news", "json"),
  ]);
  const currencies = [...new Set(data.tx.map((t) => t.currency).concat(Object.values(quotes).filter(Boolean).map((q) => q.currency))
    .concat((proposals?.buys || []).map((b) => b.plan.currency)).concat(Object.values(data.accounts || {}).map((a) => a.currency)))];
  const fx = Object.fromEntries(await Promise.all(currencies.map(async (c) => [c, await fxToEur(c)])));
  const verdicts = await judge(data, quotes, fx, proposals, news);
  const followList = data.owner ? ((await env.CONFIG.get(`follows:${data.owner}`, "json")) || []) : [];
  const follows = await Promise.all(followList.map(async (t) => {
    const q = quotes[t] || (await yahooQuote(t));
    return { symbol: t, name: q?.name || "", price: q?.price ?? null, currency: q?.currency || "", change: q?.prev ? q.price / q.prev - 1 : null,
             news: news?.scores?.[t] ?? null, proposed: (proposals?.buys || []).some((b) => b.symbol === t) };
  }));
  const [copy, agentList] = await Promise.all([data.owner ? env.CONFIG.get(`copiar:${data.owner}`, "json") : null,
    followableAgents(env)]);
  return { name: data.name, display_name: data.display_name || "", tx: data.tx, quotes, fx, verdicts, follows, copy: copy || null, agent_list: agentList || [], accounts: data.accounts || {}, profile: data.profile || null,
           dividends: data.dividends || [], settings: data.settings || { capital: 10000, risk: 1 },
           proposals: proposals ? { as_of: proposals.as_of, zone: proposals.zone, buys: proposals.buys.slice(0, 12) } : null, at: Date.now() };
}

// ---------------------------------------------------------------------------
// Keep, reduce or sell: one verdict per open position, with its reasons
// ---------------------------------------------------------------------------

const VERDICT_RULES = [
  "🔴 Vender: o preço está abaixo da média de 200 dias e a de 50 abaixo da de 200 (tendência de fundo perdida); ou perde 15% ou mais face ao custo médio; ou agentes que pesam 25% ou mais da liga estão a vender esta ação; ou notícias claramente negativas com o preço abaixo da média de 50 dias.",
  "🟠 Reduzir: abaixo da média de 50 dias (mas acima da de 200); ou mais de 25% da carteira numa só posição; ou ganho de 30%+ com o preço 15%+ acima da média de 50 dias (esticada: realizar parte).",
  "🟢 Manter: nenhum dos sinais acima.",
  "Stop sugerido: ações, o mais alto entre 2,5× a variação diária média abaixo do preço e a média de 200 dias; ETFs de índice (mexem menos por dia), o mais baixo entre 4× a variação diária e a média de 200 dias, para não sair por ruído.",
];

// ---------------------------------------------------------------------------
// Claude's analysis of the portfolio: the Worker packs what it knows, a GitHub workflow
// (portfolio-analysis.yml, scripts/portfolio_analysis.py) asks Claude with the owner's
// subscription and stores the answer in KV; the page polls /analysis until it is there.
// ---------------------------------------------------------------------------

const ANALYSES_PER_DAY = 3;

// Basic facts of common UCITS ETFs (policy: acc = accumulating, dist = distributing; TER in % a year).
// A convenience for the analysis, not a source of truth: the fund's KID is.
const ETF_FACTS = {
  "VUSA.DE": { index: "S&P 500", policy: "dist", ter: 0.07, issuer: "Vanguard", domicile: "IE" },
  "VUSA.AS": { index: "S&P 500", policy: "dist", ter: 0.07, issuer: "Vanguard", domicile: "IE" },
  "VUAA.DE": { index: "S&P 500", policy: "acc", ter: 0.07, issuer: "Vanguard", domicile: "IE" },
  "SXR8.DE": { index: "S&P 500", policy: "acc", ter: 0.07, issuer: "iShares", domicile: "IE" },
  "CSPX.AS": { index: "S&P 500", policy: "acc", ter: 0.07, issuer: "iShares", domicile: "IE" },
  "EUNL.DE": { index: "MSCI World", policy: "acc", ter: 0.20, issuer: "iShares", domicile: "IE" },
  "IWDA.AS": { index: "MSCI World", policy: "acc", ter: 0.20, issuer: "iShares", domicile: "IE" },
  "XDWD.DE": { index: "MSCI World", policy: "acc", ter: 0.19, issuer: "Xtrackers", domicile: "IE" },
  "VWCE.DE": { index: "FTSE All-World", policy: "acc", ter: 0.22, issuer: "Vanguard", domicile: "IE" },
  "VWRL.AS": { index: "FTSE All-World", policy: "dist", ter: 0.22, issuer: "Vanguard", domicile: "IE" },
  "IUSQ.DE": { index: "MSCI ACWI", policy: "acc", ter: 0.20, issuer: "iShares", domicile: "IE" },
  "IS3N.DE": { index: "MSCI EM IMI", policy: "acc", ter: 0.18, issuer: "iShares", domicile: "IE" },
  "EIMI.L": { index: "MSCI EM IMI", policy: "acc", ter: 0.18, issuer: "iShares", domicile: "IE" },
  "SXRV.DE": { index: "Nasdaq-100", policy: "acc", ter: 0.33, issuer: "iShares", domicile: "IE" },
  "EQQQ.DE": { index: "Nasdaq-100", policy: "dist", ter: 0.30, issuer: "Invesco", domicile: "IE" },
  "VHYL.AS": { index: "FTSE All-World High Dividend Yield", policy: "dist", ter: 0.29, issuer: "Vanguard", domicile: "IE" },
  "MEUD.PA": { index: "STOXX Europe 600", policy: "acc", ter: 0.07, issuer: "Amundi", domicile: "LU" },
};

function recentAnalyses(data) {
  const since = Date.now() - 864e5;
  return (data.analyses || []).filter((a) => Date.parse(a.at) > since);
}

async function analysisStatus(env, data) {
  const last = (data.analyses || []).slice(-1)[0];
  const remaining = Math.max(0, ANALYSES_PER_DAY - recentAnalyses(data).length);
  if (!last) return { status: "none", remaining, limit: ANALYSES_PER_DAY };
  const res = await env.CONFIG.get(`analysis-res:${last.rid}`, "json");
  const stale = !res && Date.now() - Date.parse(last.at) > 15 * 60e3;
  return { status: res?.status || (stale ? "error" : "pending"), at: last.at, result: res?.result || null, model: res?.model || null,
           error: res?.error || (stale ? "A análise não chegou em 15 minutos. Tenta outra vez." : null), remaining, limit: ANALYSES_PER_DAY };
}

function holdings(data, quotes, fx) {
  const pos = {};
  for (const t of data.tx.slice().sort((a, b) => a.date.localeCompare(b.date) || a.seq - b.seq)) {
    const p = pos[t.symbol] ||= { symbol: t.symbol, name: t.name, currency: t.currency, qty: 0, cost: 0, realized: 0, first: t.date, buys: 0, sells: 0 };
    if (t.side === "buy") { p.qty += t.qty; p.cost += t.qty * t.price + t.fee; p.buys++; }
    else { const avg = p.qty ? p.cost / p.qty : 0; p.realized += t.qty * t.price - t.fee - avg * t.qty; p.cost -= avg * t.qty; p.qty -= t.qty; p.sells++; }
  }
  const open = Object.values(pos).filter((p) => p.qty > 1e-9).map((p) => {
    const q = quotes[p.symbol], rate = fx[p.currency] ?? null;
    const value = q && rate != null ? p.qty * q.price * rate : null;
    return { symbol: p.symbol, name: q?.name || p.name, currency: p.currency, qty: +p.qty.toFixed(6), avg_cost: +(p.cost / p.qty).toFixed(4),
             price: q?.price ?? null, day_change_pct: q?.prev ? +((q.price / q.prev - 1) * 100).toFixed(2) : null,
             value_eur: value != null ? +value.toFixed(2) : null, cost_eur: rate != null ? +(p.cost * rate).toFixed(2) : null,
             gain_pct: q ? +((q.price / (p.cost / p.qty) - 1) * 100).toFixed(2) : null, first_buy: p.first, buys: p.buys };
  });
  const total = open.reduce((s, h) => s + (h.value_eur || 0), 0);
  open.forEach((h) => { h.weight_pct = total ? +((h.value_eur || 0) / total * 100).toFixed(1) : null; });
  const closed = Object.values(pos).filter((p) => p.qty <= 1e-9).map((p) => ({ symbol: p.symbol, realized: +p.realized.toFixed(2), currency: p.currency }));
  return { open, closed, invested_eur: +total.toFixed(2) };
}

async function requestAnalysis(env, data, dispatch) {
  if (!dispatch) return { error: "Análises indisponíveis." };
  if (recentAnalyses(data).length >= ANALYSES_PER_DAY) return { error: `Limite de ${ANALYSES_PER_DAY} análises por dia. Tenta mais tarde.` };
  const last = (data.analyses || []).slice(-1)[0];
  if (last && Date.now() - Date.parse(last.at) < 15 * 60e3 && !(await env.CONFIG.get(`analysis-res:${last.rid}`))) return { error: "Já há uma análise a caminho." };
  const snap = await snapshot(env, data);
  if (!snap.tx.length && !Object.keys(snap.accounts).length) return { error: "A carteira está vazia: importa ou adiciona operações primeiro." };
  const [market, news, calendar, proposals, funds, geo] = await Promise.all(["market", "news", "calendar", "proposals", "fundamentals", "geo"].map((k) => env.CONFIG.get(`portal:${k}`, "json")));
  const h = holdings(data, snap.quotes, snap.fx);
  const held = new Set(h.open.map((x) => x.symbol));
  const cash = Object.entries(snap.accounts).map(([name, a]) => ({ account: name, currency: a.currency, cash: a.cash,
    cash_eur: +(a.cash * (snap.fx[a.currency] ?? (a.currency === "EUR" ? 1 : 0))).toFixed(2) }));
  const pick = (rows) => (rows || []).filter((r) => held.has(r.symbol)).map((r) => ({ symbol: r.symbol, safety: r.safety, level: r.level,
    parts: (r.parts || []).map((p) => `${p.label} ${p.points}/${p.max}: ${p.why}`), warnings: r.warnings, reasons: r.reasons }));
  const payload = {
    created: new Date().toISOString(),
    portfolio: { holdings: h.open, closed: h.closed, invested_eur: h.invested_eur, cash,
                 total_eur: +(h.invested_eur + cash.reduce((s, c) => s + c.cash_eur, 0)).toFixed(2), settings: snap.settings },
    investor_profile: data.profile || null,
    dividends_received: Object.values((data.dividends || []).reduce((m, d) => {
      const r = (m[d.symbol] ||= { symbol: d.symbol, payments: 0, total: 0, last_12m: 0, last: null, currency: d.currency });
      r.payments++; r.total = +(r.total + d.amount).toFixed(2); if (!r.last || d.date > r.last) r.last = d.date;
      if (Date.parse(d.date) > Date.now() - 365 * 864e5) r.last_12m = +(r.last_12m + d.amount).toFixed(2);
      return m; }, {})),
    fund_facts: Object.fromEntries(h.open.filter((x) => ETF_FACTS[x.symbol]).map((x) => [x.symbol, { ...ETF_FACTS[x.symbol], source: "tabela interna: confirmar no KID do fundo" }])),
    verdicts: Object.fromEntries(Object.entries(snap.verdicts || {}).filter(([k]) => k !== "rules").map(([k, v]) => [k, { verdict: v.verdict, reasons: v.reasons, stop: v.stop }])),
    market: market ? { as_of: market.as_of, zone: market.zone, stage2_pct: market.stage2_pct, context: market.context } : null,
    safety_of_holdings: pick((market?.buy || []).concat(market?.sell || [], market?.watch || [])),
    news_of_holdings: Object.fromEntries([...held].map((s) => [s, (news?.timeline?.[s] || []).slice(0, 6).map((i) => ({ date: i.published?.slice(0, 10),
      sentiment: i.sentimento, importance: i.importancia, summary: i.resumo || i.title, reaction_next_day: i.r1 }))])),
    news_score_36h: Object.fromEntries([...held].map((s) => [s, news?.scores?.[s] ?? 0])),
    calendar: calendar ? { macro: calendar.macro, earnings_of_holdings: (calendar.earnings || []).filter((e) => held.has(e.symbol)) } : null,
    agents_proposals_today: (proposals?.buys || []).slice(0, 8).map((b) => ({ symbol: b.symbol, name: b.name, confidence_pct: b.confidence,
      entry_max: b.plan.entry_max, stop: b.plan.stop, target: b.plan.target, currency: b.plan.currency, safety: b.safety, agents: b.agents.map((a) => a.name) })),
    agents_selling_today: (proposals?.sells || []).map((x) => ({ symbol: x.symbol, agents: x.agents.map((a) => a.name) })),
    // SEC filings as of today: quality and value 0-100, margin of safety vs a DCF value, P/E, FCF yield, growth, margins, liquidity
    fundamentals_of_holdings: Object.fromEntries([...held].map((s) => [s, (funds || {})[s] || null]).filter(([, v]) => v)),
    sectors: market?.sectors || null,
    geopolitics: geo ? { risk_0_5: geo.risk, summary: geo.summary, themes: geo.themes, at: geo.at } : null,
  };
  const rid = token();
  await env.CONFIG.put(`analysis-req:${rid}`, JSON.stringify(payload), { expirationTtl: 86400 });
  const resp = await dispatch("portfolio-analysis.yml", { rid });
  if (!resp || !resp.ok) return { error: "Não consegui iniciar a análise (GitHub)." };
  return { entry: { rid, at: new Date().toISOString() } };
}

async function judge(data, quotes, fx, proposals, news) {
  const pos = {};
  for (const t of data.tx.slice().sort((a, b) => a.date.localeCompare(b.date) || a.seq - b.seq)) {
    const p = pos[t.symbol] ||= { qty: 0, cost: 0, currency: t.currency };
    if (t.side === "buy") { p.qty += t.qty; p.cost += t.qty * t.price + t.fee; }
    else { const avg = p.qty ? p.cost / p.qty : 0; p.cost -= avg * t.qty; p.qty -= t.qty; }
  }
  const open = Object.entries(pos).filter(([, p]) => p.qty > 1e-9);
  const total = open.reduce((sum, [s, p]) => sum + (quotes[s]?.price ?? 0) * p.qty * (fx[p.currency] ?? 0), 0);
  const selling = Object.fromEntries((proposals?.sells || []).map((x) => [x.symbol, x]));
  const scores = news?.scores || {};
  const out = { rules: VERDICT_RULES };
  await Promise.all(open.map(async ([sym, p]) => {
    const q = quotes[sym];
    const r = await yahooCached(sym, "range=1y&interval=1d", 3600);
    const c = (r?.indicators?.quote?.[0]?.close || []).filter((x) => x != null);
    const hi = r?.indicators?.quote?.[0]?.high || [], lo = r?.indicators?.quote?.[0]?.low || [], cl = r?.indicators?.quote?.[0]?.close || [];
    if (!q || c.length < 60) { out[sym] = { verdict: "?", reasons: ["sem histórico suficiente"] }; return; }
    const mean = (xs) => xs.reduce((a, b) => a + b, 0) / xs.length;
    const price = q.price, sma50 = mean(c.slice(-50)), sma200 = c.length >= 200 ? mean(c.slice(-200)) : null;
    const trs = [];
    for (let i = Math.max(1, cl.length - 14); i < cl.length; i++) {
      if (hi[i] == null || lo[i] == null || cl[i - 1] == null) continue;
      trs.push(Math.max(hi[i] - lo[i], Math.abs(hi[i] - cl[i - 1]), Math.abs(lo[i] - cl[i - 1])));
    }
    const atr = trs.length ? mean(trs) : null;
    const avg = p.cost / p.qty, gain = price / avg - 1;
    const weight = total ? price * p.qty * (fx[p.currency] ?? 0) / total : 0;
    const sell = [], reduce = [], good = [];
    if (sma200 && price < sma200 && sma50 < sma200) sell.push("abaixo da média de 200 dias, com a de 50 abaixo da de 200");
    if (gain <= -0.15) sell.push(`perde ${(gain * 100).toFixed(1)}% face ao custo médio`);
    const sellers = selling[sym] ? selling[sym].agents.map((a) => a.emoji + " " + a.name + (a.reason ? " (" + a.reason + ")" : "")).join(", ") : "";
    const share = selling[sym] && proposals?.total_weight ? selling[sym].support / proposals.total_weight : 0;
    if (share >= 0.25) sell.push(`agentes com ${(share * 100).toFixed(0)}% do peso da liga estão a vender: ${sellers}`);
    if ((scores[sym] ?? 0) <= -3 && price < sma50) sell.push(`notícias negativas (score ${scores[sym]}) e preço abaixo da média de 50 dias`);
    if (price < sma50 && !(sma200 && price < sma200)) reduce.push("abaixo da média de 50 dias");
    if (weight > 0.25) reduce.push(`${(weight * 100).toFixed(0)}% da carteira numa só posição`);
    if (gain >= 0.3 && price > sma50 * 1.15) reduce.push(`ganho de ${(gain * 100).toFixed(0)}% e ${((price / sma50 - 1) * 100).toFixed(0)}% acima da média de 50 dias`);
    if (price > sma50) good.push("acima da média de 50 dias");
    if (sma200 && price > sma200) good.push("acima da média de 200 dias");
    if ((scores[sym] ?? 0) >= 3) good.push(`notícias positivas (score +${scores[sym]})`);
    const etf = /ETF|UCITS|Index|iShares|Vanguard|Xtrackers|Amundi|SPDR|Lyxor|Invesco/i.test(q.name || "");
    const atrStop = atr ? price - (etf ? 4 : 2.5) * atr : 0;
    const stop = etf ? Math.min(atrStop || Infinity, sma200 || Infinity) : Math.max(atrStop, sma200 || 0);
    out[sym] = {
      verdict: sell.length ? "Vender" : reduce.length ? "Reduzir" : "Manter",
      reasons: (sell.length ? sell : reduce.length ? reduce : good.length ? good : ["sem sinais de alerta"])
        .concat(sellers && share < 0.25 ? [`nota: ${sellers} vendeu (${(share * 100).toFixed(0)}% do peso, abaixo dos 25%)`] : []),
      sma50, sma200, atr, stop: stop > 0 && stop < price ? stop : null, gain, weight,
    };
  }));
  return out;
}

// Each symbol's closes since the first purchase: daily, or hourly over the last month (hourly: times in Lisbon,
// "2026-11-05 14:30", so a purchase dated that day counts from its first hour).
const LISBON_TIME = new Intl.DateTimeFormat("sv-SE", { timeZone: "Europe/Lisbon", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
async function history(env, data, hourly = false) {
  if (!data.tx.length) return { series: {}, fx: {} };
  const first = data.tx.reduce((m, t) => (t.date < m ? t.date : m), data.tx[0].date);
  const p1 = Math.floor(Date.parse(first + "T00:00:00Z") / 1000) - 7 * 86400;
  const p2 = (Math.floor(Date.now() / 864e5) + 2) * 86400;  // whole days, so the shared copy is reused all day
  const params = hourly ? "range=1mo&interval=60m" : `period1=${p1}&period2=${p2}&interval=1d`;
  const grab = async (sym) => {
    const r = await yahooCached(sym, params, hourly ? 900 : 6 * 3600);
    const ts = r?.timestamp || [], cl = r?.indicators?.quote?.[0]?.close || [];
    return { t: ts.map((x) => hourly ? LISBON_TIME.format(new Date(x * 1000)) : new Date(x * 1000).toISOString().slice(0, 10)), c: cl };
  };
  const symbols = [...new Set(data.tx.map((t) => t.symbol))];
  const currencies = [...new Set(data.tx.map((t) => t.currency))].filter((c) => c && c !== "EUR");
  const [series, fxs] = await Promise.all([
    Promise.all(symbols.map(async (s) => [s, await grab(s)])),
    Promise.all(currencies.map(async (c) => [c, await grab(`EUR${c === "GBp" || c === "GBX" ? "GBP" : c}=X`)])),
  ]);
  // the money invested converts each purchase at its own day's rate, older than the hourly month too
  const foreign = data.tx.filter((t) => t.currency && t.currency !== "EUR");
  const fxDay = hourly && foreign.length ? (await history(env, { tx: foreign }, false)).fx : null;  // daily copy, cached
  return { series: Object.fromEntries(series), fx: Object.fromEntries(fxs), ...(fxDay ? { fx_dia: fxDay } : {}) };
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

// The portfolio page. On /perfil (opts.profile) it is the member's profile: the site's top bar, who they are, and a
// sidebar with the profile's parts (⚙️ Definições, 👀 Seguir, 🔒 Privacidade) and the portfolio's sections. Elsewhere (a link opened
// without the owner's session, while sign-in is off) it keeps its own header and tab bar.
const WALLET_TABS = [["resumo", "📊 Resumo"], ["posicoes", "💼 Posições"], ["operacoes", "🧾 Operações"], ["adicionar", "➕ Adicionar"],
                     ["propostas", "🧠 Propostas para ti"], ["importar", "📥 Importar"], ["analise", "🤖 Análise do Claude"]];
const PROFILE_TABS = [["definicoes", "⚙️ Definições"], ["seguir", "👀 Seguir"], ["conta", "🔒 Privacidade"]];
const START_TABS = [["", "🏠 Hoje"], ["corrida", "🏁 Corrida"], ["pretreino", "⏪ Pré-treino"], ["agentes", "🧾 Agentes"], ["propostas", "🧠 Propostas"],
                    ["noticias", "📰 Notícias"], ["oportunidades", "🎯 Oportunidades"], ["ajuda", "❓ Glossário"], ["estado", "🩺 Estado"]];

function portfolioPage(data, tok, opts = {}) {
  const tabButton = ([t, label], on) => `<button class="tab${on ? " on" : ""}" data-tab="${t}">${label}</button>`;
  const me = { name: data.display_name || data.name || "membro", avatar: avatarUrl(data.owner, data.avatar) };
  const prefs = { capital: 10000, risk: 1, start: "", ...(data.settings || {}) };
  const since = data.created ? "Membro desde " + new Date(data.created).toLocaleDateString("pt-PT", { month: "long", year: "numeric", timeZone: "Europe/Lisbon" }) + " · " : "";
  const top = opts.profile ? `
<header>${siteTopbar(me)}</header>
<div class="profile-card panel">
  ${memberFace(me, true)}
  <div class="profile-who">
    <h2 class="me-name">${esc(me.name)}</h2>
    <p class="muted small">${esc(since)}Discord ✓${data.google ? " · Google ✓" : ""}</p>
  </div>
</div>
<div class="profile-layout">
<nav class="side" id="tabs" aria-label="Perfil">
  <p class="side-h">👤 Perfil</p>
  ${PROFILE_TABS.map((t, i) => tabButton(t, i === 0)).join("")}
  <p class="side-h">💼 Carteira</p>
  ${WALLET_TABS.map((t) => tabButton(t, false)).join("")}
</nav>
<div class="side-main">
<section data-tab="definicoes">
  <h2>⚙️ Definições</h2>
  <form id="display-name" class="panel profile-form" autocomplete="off">
    <label for="dn-input"><b>🏷️ Nome a apresentar</b> <span class="muted small">· é assim que os outros membros te veem</span></label>
    <div class="row-btns"><input id="dn-input" name="name" maxlength="24" value="${esc(data.display_name || "")}" placeholder="${esc(data.name || "o teu nome no Discord")}">
      <button class="btn" type="submit">GUARDAR</button></div>
    <p class="muted small" id="dn-msg">2 a 24 caracteres, sem @ nem links, diferente dos agentes e dos outros membros. Vazio usa o teu nome do Discord${data.name ? " (" + esc(data.name) + ")" : ""}.</p>
  </form>
  <form id="prefs" class="panel profile-form" autocomplete="off">
    <p><b>📐 Tamanho das posições</b> <span class="muted small">· as propostas dos agentes passam a número de ações com estes valores, no portal e aqui</span></p>
    <div class="row-btns"><label class="small">Capital (€)<br><input name="capital" type="number" min="0" max="1000000000" step="100" value="${esc(prefs.capital)}"></label>
      <label class="small">Risco por operação (%)<br><input name="risk" type="number" min="0.1" max="5" step="0.1" value="${esc(prefs.risk)}"></label></div>
    <p><b>🏠 Separador do portal ao entrar</b><br><select name="start">${START_TABS.map(([t, label]) => `<option value="${t}"${(prefs.start || "") === t ? " selected" : ""}>${esc(label)}</option>`).join("")}</select></p>
    <div class="row-btns"><button class="btn" type="submit">GUARDAR DEFINIÇÕES</button></div>
    <p class="muted small" id="prefs-msg">Ficam na tua conta: valem em todos os aparelhos onde entras.</p>
  </form>
</section>` : `
<header class="embed-hide">
  <p class="small"><a href="/">🏟️ One Guillion Jollars</a> · ${opts.signedIn ? '<a href="/sair">Sair</a>' : '<a href="/entrar">Entrar</a>'}</p>
  <h1>💼 Carteira de ${esc(data.name || "membro")}</h1>
  <p class="muted small">Privada: só quem tem este link a vê. Preços do Yahoo Finance, atualizados a cada minuto (normalmente com ~15 min de atraso face à bolsa). Valores totais em euros ao câmbio atual. Ferramenta educativa, não é aconselhamento.</p>
</header>
<nav class="tabs" id="tabs">
  ${WALLET_TABS.concat([["seguir", "👀 Seguir"], ["conta", "🔒 Conta"]]).map((t, i) => tabButton(t, i === 0)).join("")}
</nav>`;
  const body = `${top}
<section data-tab="conta" hidden>
  <h2>🔑 Entrar</h2>
  <div class="panel small">
    <p>Além deste link, entras em <a href="/entrar">/entrar</a> com o <b>Discord</b> (só membros do servidor)${data.google ? ` ou com a conta Google <b>${esc(data.google.email)}</b>.` : "."}
      ${opts.signedIn ? "Este aparelho tem sessão iniciada." : "Este aparelho não tem sessão iniciada."}</p>
    <p class="row-btns">${data.google
      ? '<button class="btn alt" id="g-unlink" type="button">DESASSOCIAR GOOGLE</button>'
      : opts.googleOn ? `<a class="btn" style="display:inline-block;text-decoration:none" href="/entrar/google?ligar=${tok}">ASSOCIAR CONTA GOOGLE</a>` : ""}
      ${opts.signedIn ? '<a class="btn alt" style="display:inline-block;text-decoration:none" href="/sair">TERMINAR SESSÃO AQUI</a>' : '<a class="btn alt" style="display:inline-block;text-decoration:none" href="/entrar">INICIAR SESSÃO</a>'}
      <button class="btn alt" id="logout-all" type="button">TERMINAR SESSÃO EM TODOS OS APARELHOS</button></p>
  </div>
  <h2>🔒 Segurança</h2>
  <div class="panel small">
    <p>🔒 A carteira só abre com a tua sessão (Discord ou Google): o link sozinho não chega. Se achares que alguém entrou na tua conta, usa
      <b>Terminar sessão em todos os aparelhos</b> acima.</p>
  </div>
  <h2>🕘 Histórico de alterações</h2>
  <div class="panel small" id="versions">${opts.versions ? '<p class="muted">A carregar…</p>' : '<p class="muted">Disponível quando a base de dados (D1) estiver ligada.</p>'}</div>
  <h2>📤 Exportar</h2>
  <div class="panel small"><p class="row-btns"><button class="btn alt" id="export-csv" type="button">OPERAÇÕES EM CSV</button>
    <a class="btn alt" style="display:inline-block;text-decoration:none" href="/c/${tok}/export">TUDO EM JSON</a></p>
    <p class="muted">O CSV abre no Excel; o JSON tem tudo (operações, contas, dividendos, perfil).</p></div>
  <h2>🛡️ Privacidade</h2>
  <div class="panel small prose">
    <p>Guardamos: as operações, contas, dividendos, perfil e definições que puseste aqui, o teu id e nome do Discord, o email da conta Google se a associaste, a lista do /seguir, o agente que segues (e o valor que lhe dedicas) e as análises pedidas ao Claude. Nada disto é mostrado a outros membros.</p>
    <p>As análises do Claude enviam os dados da carteira para a Anthropic (sem o teu nome). As cópias de segurança diárias são apagadas ao fim de 14 dias.</p>
    <p><button class="btn" id="delete-all" type="button" style="background:#b3122e;border-bottom-color:#5a0917">APAGAR A MINHA CARTEIRA</button></p>
    <p class="muted">Apaga tudo de imediato: carteira, histórico de alterações, ligação ao Discord e Google, /seguir, agente seguido e análises. Não dá para desfazer (exporta antes).</p>
  </div>
</section>
<section data-tab="resumo"${opts.profile ? " hidden" : ""}>
  <div class="resumo-layout">
    <div class="resumo-main">
      <h2>📈 Evolução <span class="scale-pick"><button type="button" class="btn ghost on" data-scale="dia">Diária</button><button type="button" class="btn ghost" data-scale="hora">Horária</button></span></h2>
      <div id="chart" class="chart"></div>
      <p class="muted small" id="chart-note">Valor da carteira em euros vs. dinheiro investido (compras menos vendas), dia a dia desde a primeira compra.</p>
    </div>
    <div id="totals" class="totals-col"></div>
  </div>
  <div id="accounts"></div>
</section>
<section data-tab="posicoes" hidden><div id="positions"></div><div id="verdict-rules"></div><h2>✅ Posições fechadas</h2><div id="closed"></div></section>
<section data-tab="analise" hidden>
  <h2>🤖 Análise da carteira pelo Claude</h2>
  <div class="panel prose small">
    <p>O Claude recebe as tuas posições, custos médios, pesos, ganhos, o dinheiro de cada conta, o capital e o risco que definiste, os vereditos, o score de segurança e as notícias de cada posição, o calendário, o contexto de mercado e o que os agentes propõem hoje. Devolve um diagnóstico, os riscos e movimentos concretos (o quê, quanto e porquê).</p>
    <p class="muted">Preenche o teu perfil (abaixo) para uma análise à tua medida. Demora 1 a 2 minutos. Máximo de 3 análises por dia. É uma análise educativa gerada por IA: não é aconselhamento financeiro personalizado e a decisão é sempre tua.</p>
  </div>
  <details class="panel" id="profile-box"><summary><b>👤 O teu perfil de investidor</b> <span id="pf-state" class="muted small"></span></summary>
    <p class="muted small">Com isto o Claude adapta a análise ao teu caso (o mesmo conselho não serve a quem investe para a reforma daqui a 25 anos e a quem compra casa daqui a 3). Fica só na tua carteira.</p>
    <form id="profile" class="form">
      <label>Horizonte (anos até precisares do dinheiro) <input name="horizon_years" type="number" min="0" max="80"></label>
      <label>Objetivo <select name="goal"><option value="">—</option><option value="crescimento">Fazer crescer o património</option><option value="reforma">Reforma</option>
        <option value="casa">Comprar casa / grande despesa</option><option value="rendimento">Rendimento regular (dividendos)</option><option value="preservar">Preservar o capital</option><option value="outro">Outro</option></select></label>
      <label>Queda que aguentas sem vender (%) <input name="max_drawdown_pct" type="number" min="0" max="100" placeholder="ex.: 20"></label>
      <label>Entradas mensais (€) <input name="monthly_eur" type="number" min="0" step="10"></label>
      <label>Fundo de emergência (meses de despesas) <input name="emergency_months" type="number" min="0" max="120"></label>
      <label>Experiência <select name="experience"><option value="">—</option><option value="iniciante">Iniciante</option><option value="intermedio">Intermédio</option><option value="avancado">Avançado</option></select></label>
      <label>ETFs: prefiro <select name="etf_preference"><option value="">—</option><option value="acumulacao">Acumulação</option><option value="distribuicao">Distribuição</option><option value="indiferente">Indiferente</option></select></label>
      <label>Outros investimentos (imobiliário, PPR, depósitos…) <input name="other" maxlength="300"></label>
      <label>A evitar (setores, países, temas) <input name="avoid" maxlength="200"></label>
      <label>Notas para o Claude <input name="notes" maxlength="500" placeholder="ex.: o VUSA é de distribuição"></label>
      <button class="btn" type="submit">GUARDAR PERFIL</button>
    </form>
  </details>
  <p><button class="btn" id="an-go">🤖 PEDIR ANÁLISE</button> <span id="an-left" class="muted small"></span></p>
  <div id="an-out"></div>
</section>
<section data-tab="importar" hidden>
  <h2>📥 Importar da XTB</h2>
  <div class="panel prose small">
    <p><b>1.</b> No xStation 5 (computador): <b>Histórico</b> (ou o teu nome no canto → Histórico da conta) → escolhe o período <b>desde a abertura da conta</b> → <b>Exportar</b>. A XTB entrega um <b>zip</b> com um Excel por conta: podes escolher o zip diretamente.</p>
    <p><b>2.</b> Cada conta da XTB (uma por moeda) entra com o seu nome e o seu dinheiro, reconhecida pelo número da conta.</p>
    <p><b>3.</b> Escolhe o ficheiro aqui. É lido <b>no teu navegador</b>: o ficheiro não é enviado; só as compras, vendas e o saldo que confirmares.</p>
    <p class="muted">As posições fechadas entram como compra + venda; as abertas como compra; o saldo da conta como dinheiro disponível. Importar o mesmo ficheiro outra vez não duplica nada. Posições a descoberto (venda) e CFDs ficam de fora.</p>
  </div>
  <form id="import-form" class="panel form">
    <label>Nome da conta <input name="account" value="XTB EUR" maxlength="40" required></label>
    <label>Ficheiro da XTB (zip ou xlsx) <input name="file" type="file" accept=".zip,.xlsx,.xls,.csv" required></label>
  </form>
  <div id="import-preview"></div>
</section>
<section data-tab="seguir" hidden>
  <h2>🤖 Agente a seguir</h2>
  <p class="muted small">Recebes uma mensagem privada no Discord sempre que este agente compra ou vende, na ronda em que decide (de 10 em 10 minutos com a bolsa aberta; os agentes diários, com as ordens para a abertura seguinte). Cada aviso diz o tamanho em % da carteira do agente e, se puseres o valor que dedicas, quanto seria em euros. Não é recomendação: decides tu e executas na tua corretora. O Discord tem de aceitar mensagens privadas de membros do servidor.</p>
  <form id="copy-form" class="panel filters"><select name="agent" id="copy-agent"></select><input name="valor" type="number" min="0" step="50" placeholder="Valor que dedicas (€, opcional)"><button class="btn" type="submit">GUARDAR</button><span id="copy-msg" class="small"></span></form>
  <h2>👀 Ações que segues</h2>
  <p class="muted small">A mesma lista do <code>/seguir</code> no Discord: recebes uma mensagem privada quando uma destas ações rompe com volume ou mexe 5% ou mais no dia.</p>
  <form id="follow-form" class="panel filters"><input name="symbol" placeholder="Ticker, ex.: NVDA" required autocapitalize="characters"><button class="btn" type="submit">SEGUIR</button><span id="follow-msg" class="small"></span></form>
  <div id="follows"></div>
</section>
<section data-tab="propostas" hidden>
  <h2>🧠 Propostas dos agentes para o teu capital</h2>
  <form id="settings" class="panel form">
    <label>O teu capital (€) <input name="capital" type="number" min="0" step="100"></label>
    <label>Risco por operação (%) <input name="risk" type="number" min="0.1" max="5" step="0.1"></label>
    <button class="btn" type="submit">GUARDAR</button>
    <p class="muted small">Quantas ações: risco em euros ÷ (entrada − stop), no máximo 20% do capital numa posição.</p>
  </form>
  <div id="mine"></div>
</section>
<section data-tab="operacoes" hidden><div id="txs"></div></section>
<section data-tab="adicionar" hidden>
  <h2 id="form-title">➕ Registar uma compra ou venda</h2>
  <form id="form" class="panel form">
    <input type="hidden" name="edit_id" value="">
    <label>Tipo <select name="side"><option value="buy">🟢 Compra</option><option value="sell">🔴 Venda</option></select></label>
    <label>Ticker <input name="symbol" placeholder="AAPL, NVDA, VWCE.DE" required autocapitalize="characters"></label>
    <label>Data <input name="date" type="date" required></label>
    <label>Quantidade <input name="qty" type="number" step="any" min="0" required></label>
    <label>Preço por unidade <input name="price" type="number" step="any" min="0" required><span class="muted small">na moeda da bolsa (USD nos EUA, EUR na Xetra/Euronext)</span></label>
    <label>Comissão <input name="fee" type="number" step="any" min="0" value="0"><span class="muted small">na mesma moeda</span></label>
    <label>Nota <input name="note" maxlength="120" placeholder="opcional"></label>
    <button class="btn" type="submit">GUARDAR</button>
    <button class="btn alt" type="button" id="form-cancel" hidden>CANCELAR EDIÇÃO</button>
    <p id="form-msg" class="small"></p>
  </form>
  <p class="muted small">Ações europeias: junta o sufixo da bolsa do Yahoo Finance (.DE Xetra, .AS Amesterdão, .PA Paris, .MI Milão, .L Londres, .LS Lisboa).</p>
</section>
${opts.profile ? "</div></div>" : ""}
<script>
const TOKEN = ${safeJson(tok)};
const PROFILE = ${opts.profile ? "true" : "false"};
</script>
<script src="${CLIENT_PATH}"></script>`;
  return page(opts.profile ? `Perfil de ${me.name}` : `Carteira de ${data.name || "membro"}`, body, `${opts.embed ? '<base target="_top">' : ""}<link rel="manifest" href="/c/${tok}/manifest.webmanifest"><meta name="theme-color" content="#0b0820"><link rel="apple-touch-icon" href="/icon-192.png?v=1gj">
<link href="https://fonts.googleapis.com/css2?family=Press+Start+2P&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js"></script>
<style>.scale-pick { float:right; display:flex; gap:6px; } .scale-pick .btn { background:#3a2f86; border-bottom-color:#1d1650; padding:6px 10px; } .scale-pick .btn.on { background:var(--accent); border-bottom-color:#a3004f; }
.form { display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:10px; align-items:end; }
.form label { display:grid; gap:4px; font-size:.85rem; color:var(--muted); }
.form input, .form select { background:#0b0820; color:var(--fg); border:1px solid var(--line); border-radius:6px; padding:8px; font:inherit; }
.del { background:none; border:1px solid var(--down); color:var(--down); border-radius:4px; cursor:pointer; padding:2px 6px; }
.act { background:none; border:1px solid var(--neon); color:var(--neon); border-radius:4px; cursor:pointer; padding:2px 6px; }</style>`, { embed: !!opts.embed });
}

const CLIENT = String.raw`
const $ = (id) => document.getElementById(id);
const BASE = "/c/" + TOKEN;  // the API, also from /perfil
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const nf = (x, d = 2) => x == null || !isFinite(x) ? "—" : new Intl.NumberFormat("pt-PT", { minimumFractionDigits: d, maximumFractionDigits: d }).format(x);
const eur = (x) => x == null || !isFinite(x) ? "—" : new Intl.NumberFormat("pt-PT", { style: "currency", currency: "EUR" }).format(x);
const pct = (x) => x == null || !isFinite(x) ? "—" : (x > 0 ? "+" : "") + nf(x * 100, 2) + "%";
const cls = (x) => x > 0 ? "up" : x < 0 ? "down" : "";
const tile = (k, v, s = "", c = "") => '<div class="tile"><div class="k">' + k + '</div><div class="v ' + c + '">' + v + '</div>' + (s ? '<div class="s">' + s + '</div>' : "") + '</div>';
const table = (head, rows, numeric = []) => rows.length
  ? '<div class="scroll"><table><tr>' + head.map((h, i) => '<th class="' + (numeric.includes(i) ? "num" : "") + '">' + h + '</th>').join("") + '</tr>' +
    rows.map((r) => '<tr>' + r.map((c, i) => '<td class="' + (numeric.includes(i) ? "num" : "") + '">' + c + '</td>').join("") + '</tr>').join("") + '</table></div>'
  : '<p class="muted">Nada ainda.</p>';
document.querySelector('input[name=date]').value = new Date().toISOString().slice(0, 10);

function openTab(name) {
  document.querySelectorAll("#tabs .tab").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  document.querySelectorAll("section[data-tab]").forEach((s) => { s.hidden = s.dataset.tab !== name; });
  if (name === "resumo" && window.Plotly && $("chart").data) Plotly.Plots.resize("chart");
  if (PROFILE) try { history.replaceState(null, "", "#" + name); } catch (e) {}  // /perfil#seguir opens that part
}
$("tabs").addEventListener("click", (e) => { const b = e.target.closest(".tab"); if (b) openTab(b.dataset.tab); });

let SNAP = null;

function renderProfile() {
  const p = SNAP.profile || {}, f = $("profile");
  if (document.activeElement?.form === f) return;
  for (const el of f.elements) if (el.name && el.name in p) el.value = p[el.name] ?? "";
  const filled = Object.entries(p).filter(([k, v]) => k !== "updated" && v !== null && v !== "").length;
  $("pf-state").textContent = filled ? "· " + filled + " campos preenchidos (" + p.updated + ")" : "· por preencher";
}
$("profile").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { SNAP = await post({ action: "profile", profile: Object.fromEntries(new FormData(e.target).entries()) }); render(); alert("Perfil guardado. A próxima análise já o usa."); }
  catch (err) { alert(err.message); }
});

// ---- Claude's analysis ----
let anTimer = null;
function renderAnalysis(a) {
  $("an-left").textContent = a.remaining + " de " + a.limit + " análises disponíveis hoje";
  $("an-go").disabled = a.status === "pending" || a.remaining <= 0;
  if (a.status === "none") { $("an-out").innerHTML = '<p class="muted">Ainda não pediste nenhuma análise.</p>'; return; }
  if (a.status === "pending") { $("an-out").innerHTML = '<p class="muted">⏳ O Claude está a analisar a tua carteira… (pedida às ' + new Date(a.at).toLocaleTimeString("pt-PT", { hour: "2-digit", minute: "2-digit" }) + ')</p>'; return; }
  if (a.status === "error") { $("an-out").innerHTML = '<p class="down">⚠️ ' + esc(a.error || "A análise falhou.") + '</p>'; return; }
  const r = a.result || {};
  const badge = { bom: "🟢", "atenção": "🟠", problema: "🔴" };
  const act = { comprar: "🟢 Comprar", "reforçar": "🟢 Reforçar", reduzir: "🟠 Reduzir", vender: "🔴 Vender", manter: "⚪ Manter", trocar: "🔄 Trocar" };
  const pr = { alta: "🔥 alta", "média": "média", baixa: "baixa" };
  $("an-out").innerHTML = '<p class="muted small">Análise de ' + new Date(a.at).toLocaleString("pt-PT", { dateStyle: "medium", timeStyle: "short" }) + (a.model ? ' · Claude ' + esc(a.model) : '') + '</p>' +
    '<div class="grid">' + tile("Nota da carteira", (r.nota ?? "—") + "/10", esc(r.nota_porque || ""), (r.nota ?? 5) >= 7 ? "up" : (r.nota ?? 5) <= 4 ? "down" : "") + '</div>' +
    '<div class="panel prose"><p>' + esc(r.resumo || "") + '</p></div>' +
    ((r.movimentos || []).length ? '<h2>🎯 Movimentos propostos</h2><div class="cards">' + r.movimentos.map((m) => '<div class="opp"><div class="top"><span class="sym">' +
      esc(act[m.acao] || m.acao) + ' ' + esc(m.ativo || "") + (m.para ? ' → ' + esc(m.para) : '') + '</span><span class="tag">prioridade ' + esc(pr[m.prioridade] || m.prioridade || "") + '</span></div>' +
      ((m.montante_eur != null || m.percentagem != null) ? '<p><b>' + (m.montante_eur != null ? eur(m.montante_eur) : '') + (m.montante_eur != null && m.percentagem != null ? ' · ' : '') + (m.percentagem != null ? nf(m.percentagem, 0) + '%' : '') + '</b></p>' : '') +
      '<p class="small">' + esc(m.porque || "") + '</p><p class="small muted">⏱️ ' + esc(m.quando || "") + (m.condicao ? ' · 🔁 ' + esc(m.condicao) : '') + '</p></div>').join("") + '</div>' : '') +
    ((r.diagnostico || []).length ? '<h2>🩺 Diagnóstico</h2>' + table(["", "Tema", "Avaliação"], r.diagnostico.map((d) => [badge[d.avaliacao] || "⚪", '<b>' + esc(d.tema) + '</b>',
      '<span style="white-space:normal">' + esc(d.texto) + '</span>'])) : '') +
    ((r.riscos || []).length ? '<h2>⚠️ Riscos</h2><ul>' + r.riscos.map((x) => '<li>' + esc(x) + '</li>').join("") + '</ul>' : '') +
    ((r.cenarios || []).length ? '<h2>🌪️ Cenários</h2>' + table(["Cenário", "Impacto"], r.cenarios.map((c) => [esc(c.cenario), '<span style="white-space:normal">' + esc(c.impacto) + '</span>'])) : '') +
    ((r.perguntas || []).length ? '<h2>❓ Para afinar a análise</h2><ul>' + r.perguntas.map((x) => '<li>' + esc(x) + '</li>').join("") + '</ul>' : '') +
    '<p class="muted small">' + esc(r.aviso || "Análise educativa gerada por IA, não é aconselhamento financeiro personalizado.") + '</p>';
}
async function loadAnalysis() {
  try {
    const a = await (await fetch(BASE + "/analysis", { cache: "no-store" })).json();
    renderAnalysis(a);
    clearTimeout(anTimer);
    if (a.status === "pending") anTimer = setTimeout(loadAnalysis, 8000);
  } catch (e) {}
}
$("an-go").addEventListener("click", async () => {
  $("an-go").disabled = true;
  try {
    const r = await fetch(BASE + "/analysis", { method: "POST" });
    const a = await r.json();
    if (!r.ok) { alert(a.error); $("an-go").disabled = false; return; }
    renderAnalysis(a); anTimer = setTimeout(loadAnalysis, 8000);
  } catch (e) { $("an-go").disabled = false; }
});
loadAnalysis();

function renderAccounts() {
  const acc = Object.entries(SNAP.accounts || {});
  $("accounts").innerHTML = acc.length ? '<h2>🏦 Contas</h2>' + table(["Conta", "Dinheiro disponível", "Em euros", "Atualizado", ""], acc.map(([name, a]) =>
    ['<b>' + esc(name) + '</b>', '<input class="cash" data-acc="' + esc(name) + '" data-cur="' + esc(a.currency) + '" type="number" step="0.01" value="' + a.cash + '" style="width:120px"> ' + esc(a.currency),
     eur(a.cash * (SNAP.fx[a.currency] ?? (a.currency === "EUR" ? 1 : 0))), esc(a.updated + " · " + (a.source || "")),
     '<button class="del" data-delacc="' + esc(name) + '">remover</button>']), [2]) + '<p class="muted small">Corrige o dinheiro de uma conta escrevendo o valor e saindo da caixa.</p>' : "";
}
async function post(body) {
  const r = await fetch(BASE + "/data", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const out = await r.json();
  if (!r.ok) throw new Error(out.error || "erro");
  return out;
}
const LABELS = { add: "operação adicionada", edit: "operação editada", delete: "operação apagada", import: "importação", dedupe: "repetidas removidas",
  account: "conta alterada", settings: "capital e risco", profile: "perfil", analysis: "pedido de análise", security: "segurança", logout_all: "sessões terminadas",
  google_link: "Google associado", google_unlink: "Google desassociado", create: "carteira criada", migrate: "passou para a base de dados", name: "nome" };
async function loadVersions() {
  const box = $("versions");
  if (!box || /D1/.test(box.textContent)) return;
  try {
    const r = await (await fetch(BASE + "/versions", { cache: "no-store" })).json();
    const rows = r.versions || [];
    if (!rows.length) { box.innerHTML = '<p class="muted">Sem alterações registadas.</p>'; return; }
    box.innerHTML = '<p class="muted">As últimas ' + rows.length + ' alterações. "Repor" volta ao estado logo a seguir a essa alteração (a reposição também fica no histórico, por isso dá para voltar atrás).</p>' +
      (rows.length > 1 ? '<p><button class="btn" data-restore="' + rows[1].version + '">↶ DESFAZER A ÚLTIMA ALTERAÇÃO</button></p>' : '') +
      table(["Versão", "Quando", "O quê", ""], rows.map((v) => [String(v.version), esc(new Date(v.at).toLocaleString("pt-PT")),
        esc(/^restore:/.test(v.label || "") ? "reposta a versão " + v.label.slice(8) : (LABELS[v.label] || v.label || "—")),
        v.version === r.current ? '<span class="muted">atual</span>' : '<button class="del" data-restore="' + v.version + '">repor</button>']));
  } catch (e) { box.innerHTML = '<p class="muted">Não consegui carregar o histórico.</p>'; }
}
$("versions").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-restore]"); if (!b) return;
  if (!confirm("Repor a carteira como estava na versão " + b.dataset.restore + "?")) return;
  try { SNAP = await post({ action: "restore", version: +b.dataset.restore }); render(); drawHistory(); loadVersions(); } catch (err) { alert(err.message); }
});
document.querySelector('[data-tab="conta"]').addEventListener("click", loadVersions);
if (PROFILE && location.hash.length > 1 && document.querySelector('#tabs .tab[data-tab="' + location.hash.slice(1) + '"]')) {
  openTab(location.hash.slice(1));
  if (location.hash === "#conta") loadVersions();
}

// ---- /perfil: ⚙️ Definições (the display name and the settings kept in the account) ----
async function savePrefs(fields) {
  const out = await post({ action: "preferences", ...fields });
  return out.settings;
}
function formStatus(msg) {
  const hint = msg.textContent;
  return (cls, text, back) => {
    msg.className = cls + " small"; msg.textContent = text;
    if (back) setTimeout(() => { msg.className = "muted small"; msg.textContent = hint; }, back);
  };
}
if ($("prefs")) {
  const say = formStatus($("prefs-msg"));
  $("prefs").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target.elements, btn = e.target.querySelector("button");
    btn.disabled = true; say("muted", "A guardar…");
    try {
      const s = await savePrefs({ capital: +f.capital.value, risk: +f.risk.value, start: f.start.value });
      try { localStorage.setItem("capital", JSON.stringify(s.capital)); localStorage.setItem("risk", JSON.stringify(s.risk)); } catch (err) {}
      if (SNAP) { SNAP.settings = s; render(); }  // Propostas para ti follows at once
      say("up", "✅ Guardado na tua conta.", 5000);
    } catch (err) { say("down", "⚠️ " + err.message); } finally { btn.disabled = false; }
  });
}
if ($("display-name")) {
  const say = formStatus($("dn-msg"));
  $("display-name").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button");
    btn.disabled = true; say("muted", "A guardar…");
    try {
      const out = await post({ action: "display_name", name: $("dn-input").value });
      const shown = out.display_name || out.name || "membro";
      $("dn-input").value = out.display_name;
      document.querySelectorAll(".me-name").forEach((el) => { el.textContent = shown; });
      document.querySelectorAll("span.initial").forEach((el) => { el.textContent = ([...shown][0] || "?").toUpperCase(); });
      say("up", out.display_name ? "✅ Guardado: os outros membros veem-te como " + shown + "." : "✅ Voltaste ao teu nome do Discord.", 6000);
    } catch (err) { say("down", "⚠️ " + err.message); } finally { btn.disabled = false; }
  });
}
if ($("require-login")) $("require-login").onchange = async (e) => {
  try { await post({ action: "security", require_login: e.target.checked }); alert(e.target.checked ? "Feito: a partir de agora é preciso entrar com o Discord ou o Google." : "Feito: o link volta a abrir a carteira sem login."); }
  catch (err) { e.target.checked = !e.target.checked; alert(err.message); }
};
$("logout-all").onclick = async () => {
  if (!confirm("Terminar a sessão em todos os aparelhos (incluindo este)?")) return;
  try { await post({ action: "logout_all" }); alert("Sessões terminadas."); (window.top || window).location.reload(); } catch (err) { alert(err.message); }
};
$("export-csv").onclick = () => {
  const cols = ["date", "side", "symbol", "qty", "price", "fee", "currency", "account", "note"];
  const cell = (v) => { const t = String(v ?? ""); return /[";\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t; };
  const lines = [cols.join(";")].concat((SNAP.tx || []).slice().sort((a, b) => a.date.localeCompare(b.date))
    .map((t) => cols.map((c) => cell(typeof t[c] === "number" ? String(t[c]).replace(".", ",") : t[c])).join(";")));
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob(["\ufeff" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" }));
  a.download = "operacoes-" + new Date().toISOString().slice(0, 10) + ".csv"; a.click();
};
$("delete-all").onclick = async () => {
  const typed = prompt("Isto apaga a carteira e tudo o que está associado, sem volta.\nEscreve APAGAR para confirmar:");
  if (typed !== "APAGAR") return;
  try { await post({ action: "delete_all", confirm: "APAGAR" }); try { localStorage.removeItem("carteira"); } catch (e) {} (window.top || window).location.href = "/sair"; } catch (err) { alert(err.message); }
};
if ($("g-unlink")) $("g-unlink").onclick = async () => {
  if (!confirm("Desassociar a conta Google? Deixas de poder entrar com ela.")) return;
  try { await post({ action: "google_unlink" }); location.reload(); } catch (err) { alert(err.message); }
};
$("accounts").addEventListener("change", async (e) => {
  const i = e.target.closest(".cash"); if (!i) return;
  try { SNAP = await post({ action: "account", name: i.dataset.acc, currency: i.dataset.cur, cash: +i.value }); render(); } catch (err) { alert(err.message); }
});
$("accounts").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-delacc]"); if (!b) return;
  const name = b.dataset.delacc, n = SNAP.tx.filter((t) => t.account === name).length;
  if (!confirm("Remover a conta " + name + "?")) return;
  const withTx = n > 0 && confirm("Apagar também as " + n + " operações importadas para esta conta?\n\nOK = apagar as operações · Cancelar = manter as operações");
  try { SNAP = await post({ action: "account", name, remove: true, withTx }); render(); drawHistory(); } catch (err) { alert(err.message); }
});

// ---- XTB import: read the export in the browser, show what was understood, send only the rows ----
const XTB_SUFFIX = { US: "", DE: ".DE", NL: ".AS", FR: ".PA", IT: ".MI", UK: ".L", ES: ".MC", PT: ".LS", BE: ".BR", CH: ".SW", DK: ".CO",
                     SE: ".ST", NO: ".OL", FI: ".HE", PL: ".WA", CZ: ".PR", AT: ".VI", IE: ".IR" };
function xtbSymbol(raw) {
  const s = String(raw || "").trim().toUpperCase().replace(/_\d+$/, "");
  const m = s.match(/^([A-Z0-9.\-]+?)\.([A-Z]{2})$/);
  if (!m) return s;
  const base = m[1].replace(/\./g, "-");
  return m[2] in XTB_SUFFIX ? base + XTB_SUFFIX[m[2]] : s;
}
function toDay(v) {
  if (v instanceof Date && !isNaN(v)) return new Date(v.getTime() - v.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  if (typeof v === "number" && v > 20000 && v < 80000) return new Date(Math.round((v - 25569) * 864e5)).toISOString().slice(0, 10);
  const t = String(v || "").trim();
  let m = t.match(/^(\d{1,2})[./-](\d{1,2})[./-](\d{4})/);
  if (m) return m[3] + "-" + m[2].padStart(2, "0") + "-" + m[1].padStart(2, "0");
  m = t.match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? m[0] : null;
}
const num = (v) => typeof v === "number" ? v : v == null ? NaN : parseFloat(String(v).replace(/\s/g, "").replace(/,(?=\d{1,8}$)/, "."));
// Column titles in English or Portuguese (XTB's export language follows the account); accents and case ignored.
const plain = (t) => String(t ?? "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").replace(/\s+/g, " ").trim().toLowerCase();
const COLS = {
  id: /^(position|posicao|order|ordem|id|position id|id da posicao|id posicao|n\.? ?(da )?posicao|numero)$/,
  symbol: /^(ticker|symbol|simbolo)( .*)?$/,  // the exchange code (XTB: "Ticker", e.g. VUSA.DE)
  name: /^(instrument|instrumento|ativo|activo)$/,  // the product name ("S&P 500"), used only if there is no ticker column
  idAlt: /position/,  // XTB's open positions: "Instrument/Position" holds the position number on each buy row
  type: /^(type|tipo|direction|direcao|side|lado)$/,
  volume: /^(volume|quantidade|quantity|qtd\.?|units|unidades|lotes?|nominal)( .*)?$/,
  openTime: /(open|opening|abertura).*(time|date|hora|data)|(time|date|hora|data).*(open|abertura)/,
  openPrice: /(open|opening|abertura).*(price|preco)|(price|preco).*(open|abertura|medio|average)/,
  closeTime: /(close|closing|fecho|encerramento).*(time|date|hora|data)|(time|date|hora|data).*(close|fecho|encerramento)/,
  closePrice: /(close|closing|fecho|encerramento).*(price|preco)|(price|preco).*(close|fecho|encerramento)/,
  amount: /^(amount|montante|valor|quantia|importancia)( .*)?$/,
  time: /^(time|date|data|hora)( .*)?$/,
};
function findHeader(rows) {
  for (let i = 0; i < Math.min(rows.length, 120); i++) {
    const cells = (rows[i] || []).map(plain);
    const at = (re) => cells.findIndex((c) => re.test(c));
    const symbolAt = at(COLS.symbol) >= 0 ? at(COLS.symbol) : at(COLS.name);
    if (symbolAt >= 0 && (at(COLS.openTime) >= 0 || at(COLS.openPrice) >= 0)) {
      const col = Object.fromEntries(Object.entries(COLS).map(([k, re]) => [k, at(re)]));
      col.symbol = symbolAt;
      return { row: i, kind: "positions", col };
    }
    if (at(COLS.type) >= 0 && at(COLS.amount) >= 0) return { row: i, kind: "cash", col: { type: at(COLS.type), amount: at(COLS.amount),
      symbol: at(COLS.symbol), time: at(COLS.time), id: at(COLS.id) } };
  }
  return null;
}
function labelled(rows, re) {  // a value next to (or under) a label like "Balance" or "Saldo"
  for (let i = 0; i < Math.min(rows.length, 40); i++) {
    const r = rows[i] || [];
    for (let j = 0; j < r.length; j++) {
      if (!re.test(plain(r[j]))) continue;
      const right = r.slice(j + 1).find((x) => x != null && String(x).trim() !== "");
      if (right != null) return right;
      const below = (rows[i + 1] || [])[j];
      if (below != null) return below;
    }
  }
  return null;
}
function titleRow(rows) {  // the row with most text cells and no numbers: the column titles, never values
  let best = [];
  for (const r of rows.slice(0, 120)) {
    const cells = (r || []).filter((c) => c != null && String(c).trim() !== "");
    if (cells.some((c) => typeof c === "number" || c instanceof Date || /\d{3,}/.test(String(c)))) continue;
    if (cells.length > best.length) best = cells.map((c) => String(c).trim().slice(0, 40));
  }
  return best;
}
function parseXtb(wb) {
  const out = { rows: [], cash: null, cashFrom: "", currency: null, notes: [], open: 0, closed: 0, shorts: 0, sheets: [], dividends: [] };
  let cashSum = null;
  for (const name of wb.SheetNames) {
    const rows = XLSX.utils.sheet_to_json(wb.Sheets[name], { header: 1, raw: true, defval: null });
    out.sheets.push({ name, rows: rows.length, titles: titleRow(rows), found: null });
    const cur = labelled(rows, /^(currency|moeda|divisa)$/);
    if (cur && /^[A-Z]{3}$/.test(String(cur).trim())) out.currency ||= String(cur).trim();
    const bal = num(labelled(rows, /^(balance|saldo|saldo da conta|account balance)$/));
    if (out.cash == null && isFinite(bal)) { out.cash = bal; out.cashFrom = "saldo do ficheiro (" + name + ")"; }
    const h = findHeader(rows);
    if (!h) continue;
    out.sheets[out.sheets.length - 1].found = h.kind;
    if (h.kind === "cash") {
      cashSum = (cashSum || 0) + rows.slice(h.row + 1).reduce((sum, r) => sum + (isFinite(num(r?.[h.col.amount])) ? num(r[h.col.amount]) : 0), 0);
      for (const r of rows.slice(h.row + 1)) {  // dividends: proof that a fund distributes, and income
        if (!r || !/dividend|dividendo/.test(plain(r[h.col.type])) || h.col.symbol < 0) continue;
        const symbol = xtbSymbol(r[h.col.symbol]), amount = num(r[h.col.amount]), date = h.col.time >= 0 ? toDay(r[h.col.time]) : null;
        if (!symbol || !isFinite(amount) || !date) continue;
        const id = h.col.id >= 0 && r[h.col.id] != null ? String(r[h.col.id]) : symbol + "-" + date + "-" + amount;
        out.dividends.push({ symbol, date, amount, ext: "xtb-div-" + id });
      }
      continue;
    }
    const c = h.col;
    for (const r of rows.slice(h.row + 1)) {
      if (!r || r[c.symbol] == null) continue;
      const type = plain(c.type >= 0 ? r[c.type] : "buy");
      const symbol = xtbSymbol(r[c.symbol]), qty = num(r[c.volume]), openDay = toDay(r[c.openTime]), openPrice = num(r[c.openPrice]);
      if (/sell|venda|short|curta/.test(type)) { out.shorts++; continue; }
      if (!/buy|compra|long|longa/.test(type)) continue;  // XTB's summary row per instrument has no type
      if (!symbol || !(qty > 0) || !openDay || !(openPrice > 0)) continue;
      const altId = c.idAlt >= 0 && /^\d{5,}$/.test(String(r[c.idAlt] ?? "").trim()) ? String(r[c.idAlt]).trim() : null;
      const id = c.id >= 0 && r[c.id] != null && String(r[c.id]).trim() !== "" ? String(r[c.id]).trim()
        : altId || symbol + "-" + openDay + "-" + qty + "-" + openPrice;
      out.rows.push({ side: "buy", symbol, date: openDay, qty, price: openPrice, fee: 0, note: "XTB " + id, ext: "xtb-" + id + "-b" });
      const closeDay = c.closeTime >= 0 ? toDay(r[c.closeTime]) : null, closePrice = c.closePrice >= 0 ? num(r[c.closePrice]) : NaN;
      if (closeDay && closePrice > 0) {
        out.rows.push({ side: "sell", symbol, date: closeDay, qty, price: closePrice, fee: 0, note: "XTB " + id, ext: "xtb-" + id + "-s" });
        out.closed++;
      } else out.open++;
    }
  }
  if (out.cash == null && cashSum != null) { out.cash = Math.round(cashSum * 100) / 100; out.cashFrom = "soma das operações de dinheiro"; }
  // the same position can be listed in two sheets: keep each ext once
  const seen = new Set();
  out.rows = out.rows.filter((r) => !seen.has(r.ext) && seen.add(r.ext)).sort((a, b) => a.date.localeCompare(b.date));
  return out;
}
function loadSheetJs() {
  return window.XLSX ? Promise.resolve() : new Promise((ok, fail) => {
    const s = document.createElement("script");
    s.src = "https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js"; s.onload = ok; s.onerror = () => fail(new Error("não consegui carregar o leitor de Excel"));
    document.head.appendChild(s);
  });
}
let PARSED = [];  // one entry per account file: {file, account, parsed}
$("import-form").account.addEventListener("input", (e) => { e.target.dataset.touched = "1"; });
function loadJsZip() {
  return window.JSZip ? Promise.resolve() : new Promise((ok, fail) => {
    const s = document.createElement("script");
    s.src = "https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js"; s.onload = ok; s.onerror = () => fail(new Error("não consegui carregar o leitor de zip"));
    document.head.appendChild(s);
  });
}
// XTB names each account's file <currency>_<account number>_<from>_<to>.xlsx (inside a zip with a folder per account)
async function readExport(f) {
  await loadSheetJs();
  const files = [];
  if (/\.zip$/i.test(f.name)) {
    await loadJsZip();
    const zip = await JSZip.loadAsync(await f.arrayBuffer());
    for (const entry of Object.values(zip.files)) {
      if (!entry.dir && /\.(xlsx|xls|csv)$/i.test(entry.name)) files.push({ name: entry.name.split("/").pop(), data: await entry.async("arraybuffer") });
    }
    if (!files.length) throw new Error("o zip não tem nenhum ficheiro Excel da XTB");
  } else {
    files.push({ name: f.name, data: await f.arrayBuffer() });
  }
  const typed = $("import-form").account;
  return files.map((x) => {
    const parsed = parseXtb(XLSX.read(x.data, { cellDates: true }));
    const hint = x.name.match(/^([A-Z]{3})_(\d+)_/);
    if (hint) { parsed.currency ||= hint[1]; parsed.number = hint[2]; }
    const account = hint && (files.length > 1 || !typed.dataset.touched) ? "XTB " + hint[1] + " " + hint[2] : typed.value.trim();
    return { file: x.name, account, parsed };
  });
}
$("import-form").file.addEventListener("change", async (e) => {
  const f = e.target.files[0]; if (!f) return;
  $("import-preview").innerHTML = '<p class="muted">A ler o ficheiro…</p>';
  try { PARSED = await readExport(f); } catch (err) { $("import-preview").innerHTML = '<p class="down">⚠️ ' + esc(err.message) + '</p>'; return; }
  if (PARSED.length === 1 && !$("import-form").account.dataset.touched) $("import-form").account.value = PARSED[0].account;
  const diagnosis = (P) => '<details class="panel"' + (P.rows.length ? '' : ' open') + '><summary><b>🔎 Diagnóstico da leitura</b> (só os títulos das colunas, sem valores)</summary>' +
    table(["Folha", "Linhas", "Títulos das colunas", "Reconhecida como"], P.sheets.map((x) => [esc(x.name), x.rows, esc(x.titles.join(" | ") || "—"),
      x.found === "positions" ? "✅ posições" : x.found === "cash" ? "✅ dinheiro" : "—"])) + '</details>';
  const total = PARSED.reduce((n, x) => n + x.parsed.rows.length, 0);
  if (!total && PARSED.every((x) => x.parsed.cash == null)) {
    $("import-preview").innerHTML = '<p class="down">⚠️ Não reconheci posições nem dinheiro neste ficheiro. Envia um print do diagnóstico no Discord para eu ajustar a leitura.</p>' +
      PARSED.map((x) => '<h3>' + esc(x.file) + '</h3>' + diagnosis(x.parsed)).join("");
    return;
  }
  $("import-preview").innerHTML = '<h3>' + PARSED.length + (PARSED.length === 1 ? ' conta encontrada' : ' contas encontradas') + '</h3>' +
    table(["Ficheiro", "Conta", "Moeda", "Fechadas", "Abertas", "Operações", "Dinheiro disponível"], PARSED.map((x, i) => [esc(x.file),
      '<input class="imp-acc" data-i="' + i + '" value="' + esc(x.account) + '" maxlength="40" style="width:190px">',
      '<input class="imp-cur" data-i="' + i + '" value="' + esc(x.parsed.currency || "EUR") + '" maxlength="3" style="width:56px">',
      x.parsed.closed, x.parsed.open, x.parsed.rows.length,
      '<input class="imp-cash" data-i="' + i + '" type="number" step="0.01" value="' + (x.parsed.cash ?? "") + '" style="width:110px"> <span class="muted small">' + esc(x.parsed.cashFrom || "não encontrado") + '</span>']), [3, 4, 5]) +
    (PARSED.some((x) => x.parsed.shorts) ? '<p class="small muted">Posições a descoberto ficam de fora.</p>' : '') +
    '<p><button class="btn" id="imp-go">IMPORTAR ' + total + ' OPERAÇÕES</button></p>' +
    '<p class="muted small">O que já existir na carteira (mesma operação, de qualquer conta ou importação anterior) é ignorado.</p>' +
    PARSED.map((x) => '<details><summary>Ver as operações de ' + esc(x.file) + '</summary>' + table(["Data", "Tipo", "Ativo", "Qtd.", "Preço", "Ref."], x.parsed.rows.slice(0, 500).map((r) =>
      [esc(r.date), r.side === "buy" ? "🟢 compra" : "🔴 venda", '<b>' + esc(r.symbol) + '</b>', nf(r.qty, 4), nf(r.price), esc(r.note)]), [3, 4]) + '</details>' + diagnosis(x.parsed)).join("");
  $("imp-go").addEventListener("click", async () => {
    const btn = $("imp-go"); btn.disabled = true; btn.textContent = "A IMPORTAR…";
    const results = [];
    try {
      for (const [i, x] of PARSED.entries()) {
        const val = (cls) => document.querySelector("." + cls + '[data-i="' + i + '"]').value.trim();
        const out = await post({ action: "import", account: val("imp-acc"), currency: val("imp-cur").toUpperCase(),
                                 cash: val("imp-cash") === "" ? null : +val("imp-cash"), rows: x.parsed.rows, number: x.parsed.number || "", dividends: x.parsed.dividends });
        SNAP = out; results.push(out.imported);
      }
      render(); drawHistory();
      $("import-form").file.value = "";  // so the same file can be chosen again
      $("import-preview").innerHTML = results.map((im) => '<p class="up">✅ ' + im.added + ' operações importadas na conta ' + esc(im.account) +
        (im.merged ? ' (a mesma conta da XTB que já tinhas)' : '') + '.' + (im.skippedCount ? ' ' + im.skippedCount + ' ficaram de fora:' : '') + '</p>' +
        (im.skippedCount ? '<details><summary>Ver quais e porquê</summary>' + table(["Ativo", "Data", "Porquê"], im.skipped.map((y) => [esc(y.symbol), esc(y.date), esc(y.reason)])) + '</details>' : '')).join("");
    } catch (err) { btn.disabled = false; btn.textContent = "TENTAR DE NOVO"; $("import-preview").insertAdjacentHTML("beforeend", '<p class="down">⚠️ ' + esc(err.message) + '</p>'); }
  });
});
try { localStorage.setItem("carteira", JSON.stringify(BASE)); } catch (e) {}  // the portal's "Hoje" shows it
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

function renderCopy() {
  const sel = $("copy-agent");
  if (!sel || document.activeElement === sel) return;
  const cur = SNAP.copy || {};
  const agents = (SNAP.agent_list || []).slice().sort((a, b) => (b.congelado - a.congelado) || a.name.localeCompare(b.name));
  sel.innerHTML = '<option value="">— nenhum (sem avisos) —</option>' + agents.map((a) => '<option value="' + esc(a.id) + '"' + (a.id === cur.agente ? " selected" : "") + '>' +
    esc(a.emoji + " " + a.name) + (a.congelado ? " · candidato fixo" : "") + (a.perfil && a.perfil !== "equilibrado" ? " · " + esc(a.perfil) : "") + '</option>').join("");
  const v = $("copy-form").valor;
  if (document.activeElement !== v) v.value = cur.valor_eur || "";
  $("copy-msg").textContent = cur.agente ? "A seguir desde " + (cur.desde || "") : "";
  $("copy-msg").className = "small muted";
}
$("copy-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const r = await fetch(BASE + "/data", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "copy", agent: e.target.agent.value, valor: e.target.valor.value }) });
  const out = await r.json();
  if (!r.ok) { $("copy-msg").textContent = "⚠️ " + out.error; $("copy-msg").className = "small down"; return; }
  SNAP = out; render(); $("copy-msg").textContent = e.target.agent.value ? "✅ Guardado: vais receber os avisos." : "Avisos desligados.";
});
function renderFollows() {
  renderCopy();
  const f = SNAP.follows || [];
  $("follows").innerHTML = f.length ? table(["Ação", "Preço", "Hoje", "Notícias (36 h)", "Proposta hoje", ""], f.map((x) => ['<b>' + esc(x.symbol) + '</b> <span class="muted">' + esc(x.name) + '</span>',
    x.price != null ? nf(x.price) + " " + esc(x.currency) : "—", '<span class="' + cls(x.change) + '">' + pct(x.change) + '</span>',
    x.news == null ? "—" : '<span class="' + cls(x.news) + '">' + (x.news > 0 ? "+" : "") + x.news + '</span>', x.proposed ? "🧠 sim" : "—",
    '<button class="del" data-unfollow="' + esc(x.symbol) + '">deixar</button>']), [1, 2, 3]) : '<p class="muted">Ainda não segues nenhuma ação.</p>';
}
async function followAction(action, symbol) {
  const r = await fetch(BASE + "/data", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, symbol }) });
  const out = await r.json();
  if (!r.ok) { $("follow-msg").textContent = "⚠️ " + out.error; $("follow-msg").className = "small down"; return; }
  $("follow-msg").textContent = ""; SNAP = out; render();
}
$("follow-form").addEventListener("submit", (e) => { e.preventDefault(); followAction("follow_add", e.target.symbol.value); e.target.reset(); });
$("follows").addEventListener("click", (e) => { const b = e.target.closest("[data-unfollow]"); if (b) followAction("follow_remove", b.dataset.unfollow); });

function verdictCell(v) {
  if (!v) return "—";
  const icon = { Vender: "🔴", Reduzir: "🟠", Manter: "🟢" }[v.verdict] || "❔";
  return '<span title="' + esc(v.reasons.join(" · ")) + '">' + icon + " <b>" + esc(v.verdict) + '</b></span><div class="muted small" style="white-space:normal;max-width:260px">' +
    esc(v.reasons.join(" · ")) + '</div>';
}

function renderMine() {
  const s = SNAP, P = s.proposals, st = s.settings || { capital: 10000, risk: 1 };
  const f = $("settings");
  if (document.activeElement?.form !== f) { f.capital.value = st.capital; f.risk.value = st.risk; }
  if (!P || !P.buys.length) { $("mine").innerHTML = '<p class="muted">Hoje não há propostas de compra dos agentes. Aparecem depois da liga dos dias úteis (07:30).</p>'; return; }
  $("mine").innerHTML = '<p class="muted small">Fecho de ' + esc(P.as_of) + ' · zona ' + esc(P.zone) + ' · para estudar e decidir tu, não é recomendação.</p><div class="cards">' + P.buys.map((b) => {
    const pl = b.plan, fx = pl.fx_eur, per = (pl.entry_max - pl.stop) * fx;
    let qty = per > 0 ? st.capital * st.risk / 100 / per : 0;
    const capQty = st.capital * 0.2 / (pl.entry_max * fx), capped = qty > capQty;
    qty = Math.floor(Math.min(qty, capQty) * 1000) / 1000;
    const held = SNAP.tx.some((t) => t.symbol === b.symbol);
    return '<div class="opp"><div class="top"><span class="sym">' + esc(b.symbol) + '</span><span class="muted">' + esc(b.name || "") + '</span>' +
      (b.safety != null ? '<span class="score ' + esc((b.level || "").split(" ")[0]) + '">🛡️ ' + b.safety + '/100</span>' : "") + '</div>' +
      '<p class="small">Confiança dos agentes ' + nf(b.confidence, 0) + '% · ' + b.agents.map((a) => esc(a.emoji + " " + a.name)).join(", ") + '</p>' +
      '<p>🧮 <b>' + nf(qty, 3) + ' ações</b> ≈ ' + eur(qty * pl.entry_max * fx) + ' · arriscas ' + eur(qty * per) + (capped ? ' (limitado a 20% do capital)' : '') + '</p>' +
      '<p class="small">Entrada até ' + nf(pl.entry_max) + ' · stop ' + nf(pl.stop) + ' (−' + nf(pl.risk_pct, 1) + '%) · alvo ' + nf(pl.target) + ' (+' + nf(pl.gross_pct, 1) + '%) · líquido no alvo ' +
      (pl.net_target_pct > 0 ? "+" : "") + nf(pl.net_target_pct, 1) + '% · ' + esc(pl.currency) + '</p>' +
      (held ? '<p class="small">💼 Já tens esta ação na carteira.</p>' : '') +
      '<button class="btn alt use" data-sym="' + esc(b.symbol) + '" data-qty="' + qty + '" data-price="' + pl.entry_max + '">PREENCHER COMPRA</button></div>';
  }).join("") + '</div>';
}

$("mine").addEventListener("click", (e) => {
  const b = e.target.closest(".use"); if (!b) return;
  const f = $("form"); resetForm();
  f.side.value = "buy"; f.symbol.value = b.dataset.sym; f.qty.value = b.dataset.qty; f.price.value = b.dataset.price;
  openTab("adicionar"); $("form-msg").textContent = "Confirma o preço a que compraste e a data, e guarda.";
});

$("settings").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const r = await fetch(BASE + "/data", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "settings", capital: +f.capital.value, risk: +f.risk.value }) });
  const out = await r.json();
  if (!r.ok) return alert(out.error);
  SNAP = out; render();
});

function resetForm() {
  const f = $("form");
  f.reset(); f.edit_id.value = ""; f.date.value = new Date().toISOString().slice(0, 10);
  $("form-title").textContent = "➕ Registar uma compra ou venda"; $("form-cancel").hidden = true;
}

function fillForm(t, editing) {
  const f = $("form");
  resetForm();
  f.side.value = t.side; f.symbol.value = t.symbol; f.qty.value = t.qty; f.price.value = t.price; f.fee.value = t.fee; f.note.value = t.note || "";
  f.date.value = editing ? t.date : new Date().toISOString().slice(0, 10);
  if (editing) { f.edit_id.value = t.id; $("form-title").textContent = "✏️ Editar operação de " + t.date; $("form-cancel").hidden = false; }
  else $("form-title").textContent = "⧉ Duplicar: muda a data (e o que precisares) e guarda";
  $("form-msg").textContent = ""; openTab("adicionar");
  (editing ? f.qty : f.date).focus();
}
$("form-cancel").addEventListener("click", resetForm);

// Average-cost bookkeeping per symbol, in the symbol's currency.
function book(tx) {
  const pos = {};
  for (const t of tx.slice().sort((a, b) => a.date.localeCompare(b.date) || a.seq - b.seq)) {
    const p = pos[t.symbol] ||= { symbol: t.symbol, name: t.name, currency: t.currency, qty: 0, cost: 0, realized: 0, fees: 0, bought: 0, sold: 0, first: t.date, last: t.date };
    p.last = t.date; p.fees += t.fee;
    if (t.side === "buy") { p.qty += t.qty; p.cost += t.qty * t.price + t.fee; p.bought += t.qty * t.price + t.fee; }
    else { const avg = p.qty ? p.cost / p.qty : 0; p.realized += t.qty * t.price - t.fee - avg * t.qty; p.cost -= avg * t.qty; p.qty -= t.qty; p.sold += t.qty * t.price - t.fee; }
  }
  return Object.values(pos);
}

function render() {
  const s = SNAP, all = book(s.tx);
  const open = all.filter((p) => p.qty > 1e-9), closed = all.filter((p) => p.qty <= 1e-9);
  let value = 0, cost = 0, day = 0, realized = 0, stale = 0;
  const rows = open.map((p) => {
    const q = s.quotes[p.symbol], fx = s.fx[p.currency] ?? null;
    const price = q?.price, val = price != null ? p.qty * price : null;
    const gain = val != null ? val - p.cost : null, dgain = q?.prev != null && price != null ? p.qty * (price - q.prev) : null;
    if (val != null && fx != null) { value += val * fx; cost += p.cost * fx; day += (dgain || 0) * fx; } else stale++;
    return { p, q, fx, price, val, gain, dgain };
  });
  for (const p of all) realized += p.realized * (s.fx[p.currency] ?? 0);
  const total = value - cost;
  const accounts = Object.entries(s.accounts || {});
  const cashEur = accounts.reduce((sum, [, a]) => sum + a.cash * (s.fx[a.currency] ?? (a.currency === "EUR" ? 1 : 0)), 0);
  $("totals").innerHTML = (accounts.length ? tile("Património total", eur(value + cashEur), "posições + dinheiro") +
    tile("Dinheiro disponível", eur(cashEur), accounts.length + (accounts.length === 1 ? " conta" : " contas")) : "") +
    ((s.dividends || []).length ? tile("Dividendos (12 meses)", eur(s.dividends.filter((d) => Date.parse(d.date) > Date.now() - 365 * 864e5)
      .reduce((t, d) => t + d.amount * (s.fx[d.currency] ?? 1), 0)), s.dividends.length + " pagamentos importados") : "") +
    tile("Valor atual", eur(value), open.length + " posições abertas") +
    tile("Ganho não realizado", eur(total), pct(cost ? total / cost : null), cls(total)) +
    tile("Hoje", eur(day), pct(value - day ? day / (value - day) : null), cls(day)) +
    tile("Ganho realizado (vendas)", eur(realized), closed.length + " posições fechadas", cls(realized)) +
    tile("Ganho total", eur(total + realized), "não realizado + realizado", cls(total + realized)) +
    tile("Investido nas abertas", eur(cost), "custo médio com comissões") +
    tile("Atualizado", new Date(s.at).toLocaleTimeString("pt-PT", { hour: "2-digit", minute: "2-digit", second: "2-digit" }), stale ? "⚠️ " + stale + " sem preço" : "próxima em 60 s");
  const weightOf = (r) => r.val != null && r.fx != null && value ? r.val * r.fx / value : null;
  $("positions").innerHTML = '<h2>💼 Posições abertas</h2>' + table(
    ["Ativo", "Veredito", "Nome", "Qtd.", "Custo médio", "Preço", "Hoje", "Valor", "Ganho", "Ganho %", "Peso", "Valor €", "Stop sugerido", "Desde"],
    rows.sort((a, b) => (weightOf(b) || 0) - (weightOf(a) || 0)).map((r) => ['<b>' + esc(r.p.symbol) + '</b>', verdictCell(s.verdicts?.[r.p.symbol]),
      esc(r.q?.name || r.p.name || ""), nf(r.p.qty, 4),
      nf(r.p.cost / r.p.qty) + " " + esc(r.p.currency), r.price != null ? nf(r.price) + " " + esc(r.p.currency) : "—",
      '<span class="' + cls(r.dgain) + '">' + (r.q?.prev ? pct(r.price / r.q.prev - 1) : "—") + '</span>',
      r.val != null ? nf(r.val) + " " + esc(r.p.currency) : "—", '<span class="' + cls(r.gain) + '">' + nf(r.gain) + " " + esc(r.p.currency) + '</span>',
      '<span class="' + cls(r.gain) + '">' + pct(r.gain != null ? r.gain / r.p.cost : null) + '</span>', weightOf(r) != null ? nf(weightOf(r) * 100, 1) + "%" : "—",
      r.val != null && r.fx != null ? eur(r.val * r.fx) : "—", s.verdicts?.[r.p.symbol]?.stop ? nf(s.verdicts[r.p.symbol].stop) + " " + esc(r.p.currency) : "—",
      esc(r.p.first)]), [3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
  $("verdict-rules").innerHTML = s.verdicts?.rules ? '<details class="panel"><summary><b>Como se decide Manter, Reduzir ou Vender</b></summary><ul class="small">' +
    s.verdicts.rules.map((x) => '<li>' + esc(x) + '</li>').join("") + '</ul><p class="muted small">Sinais técnicos para te ajudar a pensar, não ordens: a decisão é tua.</p></details>' : "";
  renderMine();
  renderFollows();
  renderAccounts();
  renderProfile();
  $("closed").innerHTML = table(["Ativo", "Comprado", "Vendido", "Resultado", "Resultado %", "Comissões", "De", "Até"],
    closed.map((p) => ['<b>' + esc(p.symbol) + '</b>', nf(p.bought) + " " + esc(p.currency), nf(p.sold) + " " + esc(p.currency),
      '<span class="' + cls(p.realized) + '">' + nf(p.realized) + " " + esc(p.currency) + '</span>', '<span class="' + cls(p.realized) + '">' + pct(p.bought ? p.realized / p.bought : null) + '</span>',
      nf(p.fees) + " " + esc(p.currency), esc(p.first), esc(p.last)]), [1, 2, 3, 4, 5]);
  const fp = (t) => [t.side, t.symbol, t.date, Number(t.qty).toFixed(6), Number(t.price).toFixed(4)].join("|");
  const dupes = s.tx.length - new Set(s.tx.map(fp)).size;
  $("txs").innerHTML = '<h2>🧾 Todas as operações (' + s.tx.length + ')</h2>' +
    (dupes ? '<p class="panel">⚠️ Há <b>' + dupes + '</b> operações repetidas (mesma ação, data, quantidade e preço). <button class="btn" id="dedupe">🧹 REMOVER DUPLICADOS</button></p>' : '') + table(["Data", "Tipo", "Ativo", "Qtd.", "Preço", "Comissão", "Total", "Nota", ""],
    s.tx.slice().sort((a, b) => b.date.localeCompare(a.date) || b.seq - a.seq).map((t) => [esc(t.date), t.side === "buy" ? "🟢 compra" : "🔴 venda", '<b>' + esc(t.symbol) + '</b>',
      nf(t.qty, 4), nf(t.price) + " " + esc(t.currency), nf(t.fee), nf(t.qty * t.price + (t.side === "buy" ? t.fee : -t.fee)) + " " + esc(t.currency), esc(t.note || ""),
      '<button class="act" data-act="edit" data-id="' + esc(t.id) + '">✏️ editar</button> <button class="act" data-act="dup" data-id="' + esc(t.id) +
      '">⧉ duplicar</button> <button class="del" data-id="' + esc(t.id) + '">apagar</button>']), [3, 4, 5, 6]);
}

async function load() {
  try {
    const r = await fetch(BASE + "/data", { cache: "no-store" });
    if (r.ok) { SNAP = await r.json(); render(); }
  } catch (e) {}
}

let SCALE = "dia";
try { SCALE = localStorage.getItem("ogj-escala") === "hora" ? "hora" : "dia"; } catch (e) {}
document.querySelectorAll(".scale-pick [data-scale]").forEach((b) => {
  b.classList.toggle("on", b.dataset.scale === SCALE);
  b.addEventListener("click", () => {
    SCALE = b.dataset.scale;
    try { localStorage.setItem("ogj-escala", SCALE); } catch (e) {}
    document.querySelectorAll(".scale-pick [data-scale]").forEach((x) => x.classList.toggle("on", x === b));
    drawHistory();
  });
});
async function drawHistory() {
  if (!window.Plotly || !SNAP || !SNAP.tx.length) { $("chart").innerHTML = '<p class="muted" style="padding:16px">Adiciona a primeira compra para ver a evolução.</p>'; return; }
  const hourly = SCALE === "hora";
  $("chart-note").textContent = hourly
    ? "Valor da carteira em euros vs. dinheiro investido, hora a hora no último mês (hora de Lisboa; noites e fins de semana escondidos)."
    : "Valor da carteira em euros vs. dinheiro investido (compras menos vendas), dia a dia desde a primeira compra.";
  const h = await (await fetch(BASE + "/history" + (hourly ? "?escala=hora" : ""), { cache: "no-store" })).json();
  const days = [...new Set(Object.values(h.series).flatMap((s) => s.t))].filter((d) => d >= SNAP.tx.reduce((m, t) => t.date < m ? t.date : m, "9999")).sort();
  const lastBefore = (s, d) => { if (!s) return null; let v = null; for (let i = 0; i < s.t.length && s.t[i] <= d; i++) if (s.c[i] != null) v = s.c[i]; return v; };
  const fxOn = (cur, d, table = h.fx) => cur === "EUR" ? 1 : (() => { const r = lastBefore(table[cur], d); return r ? (cur === "GBp" || cur === "GBX" ? 0.01 : 1) / r : null; })();
  const tx = SNAP.tx.slice().sort((a, b) => a.date.localeCompare(b.date) || a.seq - b.seq);
  const value = [], invested = [];
  for (const d of days) {
    const held = {}; let inv = 0;
    for (const t of tx) { if (t.date > d) break; held[t.symbol] = (held[t.symbol] || 0) + (t.side === "buy" ? t.qty : -t.qty);
      const fx = fxOn(t.currency, t.date, h.fx_dia || h.fx) ?? fxOn(t.currency, d) ?? 1; inv += (t.side === "buy" ? t.qty * t.price + t.fee : -(t.qty * t.price - t.fee)) * fx; }
    let v = 0;
    for (const [sym, q] of Object.entries(held)) { if (q <= 1e-9) continue; const t = tx.find((x) => x.symbol === sym);
      const px = lastBefore(h.series[sym], d), fx = fxOn(t.currency, d); if (px != null && fx != null) v += q * px * fx; }
    value.push(v); invested.push(inv);
  }
  Plotly.newPlot("chart", [
    { x: days, y: value, name: "Valor da carteira", type: "scatter", mode: "lines", line: { color: "#00e5ff", width: 2 }, hovertemplate: "%{y:,.2f} €<extra>Valor</extra>" },
    { x: days, y: invested, name: "Dinheiro investido", type: "scatter", mode: "lines", line: { color: "#ffe14d", width: 1.5, dash: "dash" }, hovertemplate: "%{y:,.2f} €<extra>Investido</extra>" },
  ], { paper_bgcolor: "#17123a", plot_bgcolor: "#17123a", font: { color: "#e8e6ff" }, margin: { l: 60, r: 12, t: 12, b: 40 }, hovermode: "x unified",
       xaxis: { gridcolor: "#2e2763", ...(hourly ? { rangebreaks: [{ bounds: ["sat", "mon"] }, { pattern: "hour", bounds: [21.5, 7.5] }], hoverformat: "%d/%m %H:%M" } : {}) },
       yaxis: { gridcolor: "#2e2763", ticksuffix: " €" }, legend: { orientation: "h", y: -0.15 } },
     { responsive: true, displaylogo: false });
}

$("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target), msg = $("form-msg");
  msg.textContent = "A guardar…"; msg.className = "small";
  const tx = Object.fromEntries(f.entries());
  const id = tx.edit_id; delete tx.edit_id;
  const r = await fetch(BASE + "/data", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(id ? { action: "edit", id, tx } : { action: "add", tx }) });
  const out = await r.json();
  if (!r.ok) { msg.textContent = "⚠️ " + out.error; msg.className = "small down"; return; }
  SNAP = out; render(); drawHistory();
  msg.textContent = "✅ " + (id ? "Alterada" : "Guardada") + ": " + (tx.side === "buy" ? "compra" : "venda") + " de " + tx.qty + " " + tx.symbol.toUpperCase() + " a " + tx.date; msg.className = "small up";
  resetForm();
});

$("txs").addEventListener("click", async (e) => {
  if (e.target.closest("#dedupe")) {
    try { SNAP = await post({ action: "dedupe" }); const n = SNAP.deduped; render(); drawHistory(); alert(n + " operações repetidas removidas."); } catch (err) { alert(err.message); }
    return;
  }
  const a = e.target.closest(".act");
  if (a) { const t = SNAP.tx.find((x) => x.id === a.dataset.id); if (t) fillForm(t, a.dataset.act === "edit"); return; }
  const b = e.target.closest(".del"); if (!b || !confirm("Apagar esta operação?")) return;
  const r = await fetch(BASE + "/data", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "delete", id: b.dataset.id }) });
  const out = await r.json();
  if (!r.ok) return alert(out.error);
  SNAP = out; render(); drawHistory();
});

load().then(drawHistory);
setInterval(load, 60000);
`;

const CLIENT_PATH = staticAsset("carteira", CLIENT, "js");
