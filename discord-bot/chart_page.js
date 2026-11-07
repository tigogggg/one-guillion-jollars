// Interactive chart pages, served by the Worker at /g/<id>.
//
// scripts/interactive.py stores each chart's data in KV under chart:<id> (and
// the newest id of each kind under chart-latest:<kind>). The page draws it with
// Plotly (hover values, zoom, agents on and off in the legend), shows one
// detailed card per agent (holdings, orders, trades, stats, strategy) and counts
// down to the next scheduled runs, from the same schedule /horario edits.

import { I18N_JS } from "./i18n.js";
import { ADMIN_CLIENT, ADMIN_CSS, isAdmin } from "./admin.js";

const TIMEZONE = "Europe/Lisbon";
const PLOTLY = "https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js";
const DAYS = ["dom", "seg", "ter", "qua", "qui", "sex", "sab"];

// Each scheduled job: label and how to find its next times from the schedule.
const JOBS = [
  { key: "dicas", label: "📊 Dicas do dia e rankings", weekdays: true },
  { key: "noticias", label: "📰 Leitura de notícias", weekdays: true },
  { key: "agentes", label: "🤖 Liga dos agentes", weekdays: true },
  { key: "ronda", label: "⏱️ Ronda dos agentes", weekdays: true },
  { key: "alertas", label: "🚨 Alertas de rompimento", weekdays: true },
  { key: "acompanhamento", label: "🔁 Acompanhamento dos alertas", weekdays: true },
  { key: "estado", label: "🔧 Estado do bot", weekdays: true },
  { key: "semanal", label: "🗓️ Resumo semanal" },
  { key: "treino", label: "🧠 Re-treino dos agentes (20 anos)" },
];

// 🩺 Estado: the last runs of the automatic workflows, read from GitHub. Kept 2 minutes in the isolate's memory (no KV
// writes; the portal polls often). The repo is public: run titles, times and results are public anyway.
const STATUS_WORKFLOWS = ["agents.yml", "news.yml", "pretrain.yml", "discord-daily.yml", "opportunity-alerts.yml", "alert-followups.yml",
  "health.yml", "weekly-summary.yml", "intraday-lab.yml", "context-data.yml", "backup.yml", "arquivo-mensal.yml", "deploy-worker.yml", "tests.yml"];
let RUNS_CACHE = { at: 0, runs: null };
export async function workflowRuns(env) {
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) return null;
  if (RUNS_CACHE.runs && Date.now() - RUNS_CACHE.at < 120000) return RUNS_CACHE.runs;
  const headers = { Authorization: `Bearer ${env.GITHUB_TOKEN}`, Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "coutotrading-discord-bot" };
  const runs = {};
  await Promise.all(STATUS_WORKFLOWS.map(async (f) => {
    try {
      const r = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${f}/runs?per_page=${f === "agents.yml" ? 30 : 5}&exclude_pull_requests=true`, { headers });
      if (!r.ok) return;
      runs[f] = ((await r.json()).workflow_runs || []).map((x) => ({ title: x.display_title, status: x.status, conclusion: x.conclusion,
        started: x.run_started_at || x.created_at, updated: x.updated_at, url: x.html_url }));
    } catch (e) { /* one workflow unreadable: the others still show */ }
  }));
  RUNS_CACHE = { at: Date.now(), runs };
  return runs;
}

// The pre-training of each investor profile in one view: every trained agent (the Indexador once), each with its
// profile, so any of them can be compared with the others and become the main one.
const PROFILE_LABELS = { equilibrado: "Equilibrado", conservador: "Conservador", arrojado: "Arrojado" };
export function mergeProfiles(main, others) {
  const out = { ...main, series: [], agents: [], highlight: [] };
  const seen = new Set();
  const add = (data, profile) => {
    const label = PROFILE_LABELS[profile];
    const rename = (n) => (profile === "equilibrado" || n === "Indexador" || n.endsWith(" " + label) ? n : n + " " + label);
    for (const s of data.series || []) {
      const name = rename(s.name);
      if (seen.has(name)) continue;
      seen.add(name);
      out.series.push({ ...s, name });
    }
    for (const a of data.agents || []) {
      const name = rename(a.name);
      if (out.agents.some((b) => b.name === name)) continue;
      const id = profile === "equilibrado" || !a.id || a.id === "indexador" || a.id.endsWith("-" + profile) ? a.id : a.id + "-" + profile;
      out.agents.push({ ...a, name, id, perfil: profile });
    }
  };
  add(main, "equilibrado");
  for (const [p, data] of others) if (data) add(data, p);
  // in colour: the 5 that end highest, and the Indexador (the page follows the interval in view)
  const last = out.series.filter((s) => s.y.length).map((s) => [s.name, s.y[s.y.length - 1]]).sort((a, b) => b[1] - a[1]);
  out.highlight = [...new Set(last.filter(([n]) => n !== "Indexador").slice(0, 5).map(([n]) => n)
    .concat(last.some(([n]) => n === "Indexador") ? ["Indexador"] : []))];
  return out;
}

export async function chartResponse(env, id, schedule) {
  const data = await env.CONFIG.get(`chart:${id}`, "json");
  if (!data) {
    return html(page("Gráfico indisponível", "<p class='muted'>Este gráfico já expirou ou o link está errado. Usa o botão da publicação mais recente no Discord.</p>"), 404);
  }
  const latest = data.kind ? await env.CONFIG.get(`chart-latest:${data.kind}`, "json") : null;
  const upcoming = nextRuns(schedule, Date.now());
  return html(chartPage(data, upcoming, latest && latest !== id ? latest : null));
}

// The portal: /p/<token> (the server's shared link) or / for a signed-in member.
// session (session.js currentSession) shows the member's name and their portfolio inside the portal.
// What the open portal pages compare to know there is something new: the latest league and pre-training charts and
// the time any portal section was last published (scripts/interactive.py).
export async function portalVersion(env) {
  const keys = ["chart-latest:liga", "chart-latest:pretreino", "portal:version"];
  return (await Promise.all(keys.map((k) => env.CONFIG.get(k)))).map((v) => v || "").join("|");
}

export async function portalResponse(env, token, schedule, wantManifest = false, session = null) {
  if (token !== null) {
    const saved = await env.CONFIG.get("portal:token", "json");
    if (!saved || saved !== token) return html(page("Link inválido", "<p class='muted'>Este link do portal não é válido. Entra em <a href='/'>One Guillion Jollars</a> ou pede o link atual com /portal no Discord.</p>"), 404);
  }
  const home = token === null ? "/" : `/p/${token}`;
  if (wantManifest) {
    return new Response(JSON.stringify({
      name: "One Guillion Jollars", short_name: "OGJ", start_url: home, scope: home, display: "standalone",
      background_color: "#0b0820", theme_color: "#0b0820",
      icons: [{ src: "/icon-192.png?v=1gj", sizes: "192x192", type: "image/png" }, { src: "/icon-512.png?v=1gj", sizes: "512x512", type: "image/png" }],
    }), { headers: { "Content-Type": "application/manifest+json" } });
  }
  // the latest league page; until there is one, the latest pre-training race
  const latest = (await env.CONFIG.get("chart-latest:liga", "json")) || (await env.CONFIG.get("chart-latest:pretreino", "json"));
  const data = (latest && (await env.CONFIG.get(`chart:${latest}`, "json"))) ||
    { title: "Liga dos agentes", subtitle: "ainda sem dados", updated: new Date().toISOString(), series: [], agents: [] };
  const [news, market, proposals, calendar, geo, evolution, decision] = await Promise.all(["news", "market", "proposals", "calendar", "geo", "evolution", "decisao"].map((k) => env.CONFIG.get(`portal:${k}`, "json")));
  const ptId = await env.CONFIG.get("chart-latest:pretreino", "json");
  let pretrain = ptId && ptId !== latest ? await env.CONFIG.get(`chart:${ptId}`, "json") : null;
  if (pretrain) {  // the other investor profiles' latest pre-trainings, on the same page
    const others = await Promise.all(Object.keys(PROFILE_LABELS).filter((p) => p !== "equilibrado").map(async (p) => {
      const id = await env.CONFIG.get(`chart-latest:pretreino-${p}`, "json");
      return [p, id ? await env.CONFIG.get(`chart:${id}`, "json") : null];
    }));
    pretrain = mergeProfiles(pretrain, others);
  }
  const user = session ? { name: session.data.display_name || session.data.name || "membro", discord: session.data.name || "",
                           avatar: avatarUrl(session.owner, session.data.avatar), settings: session.data.settings || null,
                           display: session.data.display_name || "", since: session.data.created || null, google: !!session.data.google,
                           wallet: `/c/${session.tok}` } : null;
  const version = await portalVersion(env);
  const [runs, quotes] = await Promise.all([workflowRuns(env), dashboardQuotes(session, market)]);
  const admin = isAdmin(env, session);  // 🔒 Admin: only the members in the ADMIN_IDS secret
  return html(chartPage(data, nextRuns(schedule, Date.now()), null, { portal: true, admin, news, market, proposals, calendar, pretrain, user, geo, evolution, decision, version, runs, home, quotes,
                                                                       manifest: token === null ? "/manifest.webmanifest" : `/p/${token}/manifest.webmanifest` }));
}

// 📊 Dashboard: the S&P 500 (SPY) and Brent crude today, and, for whoever has no portfolio yet, one stock picked at
// random (a proposal of the day, else a large company). Yahoo's chart API, cached a minute at Cloudflare's edge.
const DASH_STOCKS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM", "V", "KO", "COST", "JNJ"];
async function quoteNow(symbol) {
  try {
    const resp = await fetch(`https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}?range=5d&interval=1d`,
                             { headers: { "User-Agent": "Mozilla/5.0 (OGJ portal)" }, cf: { cacheTtl: 60, cacheEverything: true } });
    if (!resp.ok) return null;
    const r = (await resp.json())?.chart?.result?.[0];
    const m = r?.meta;
    if (!m || m.regularMarketPrice == null) return null;
    const closes = (r.indicators?.quote?.[0]?.close || []).filter((x) => x != null);  // the day before the last close
    const prev = closes.length >= 2 ? closes[closes.length - 2] : m.chartPreviousClose ?? null;
    return { symbol, price: m.regularMarketPrice, prev, currency: m.currency || "USD", name: m.shortName || m.longName || "",
             time: (m.regularMarketTime || 0) * 1000 };
  } catch {
    return null;
  }
}

async function dashboardQuotes(session, market) {
  const hasWallet = !!(session && (session.data.tx || []).length);
  // without a portfolio: the stock that moved most today (up or down), among the 4 biggest movers of the last
  // daily run and the large companies; at most 12 live quotes (each cached a minute at the edge)
  const rows = [].concat(market?.buy || [], market?.watch || [], market?.sell || []).filter((r) => r && r.symbol && r.ret_1d != null && !r.etf);
  const movers = rows.sort((a, b) => Math.abs(b.ret_1d) - Math.abs(a.ret_1d)).slice(0, 4).map((r) => r.symbol);
  const pool = hasWallet ? [] : [...new Set(movers.concat(DASH_STOCKS))].slice(0, 12);
  const [spy, oil, ...others] = await Promise.all([quoteNow("SPY"), quoteNow("BZ=F"), ...pool.map(quoteNow)]);
  const move = (q) => (q && q.prev ? Math.abs(q.price / q.prev - 1) : -1);
  const stock = others.filter(Boolean).sort((a, b) => move(b) - move(a))[0] || null;
  return { spy, oil, stock, wallet: hasWallet };
}

// ---------------------------------------------------------------------------
// Schedule: next run of each job, as epoch milliseconds
// ---------------------------------------------------------------------------

function lisbonParts(ms) {
  const p = Object.fromEntries(
    new Intl.DateTimeFormat("en-GB", {
      timeZone: TIMEZONE, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).formatToParts(new Date(ms)).map((x) => [x.type, x.value]),
  );
  return { y: +p.year, m: +p.month, d: +p.day, h: +p.hour, mi: +p.minute };
}

// The instant when Lisbon's wall clock reads y-m-d h:mi.
function lisbonToEpoch(y, m, d, h, mi) {
  const guess = Date.UTC(y, m - 1, d, h, mi);
  const p = lisbonParts(guess);
  const offset = Date.UTC(p.y, p.m - 1, p.d, p.h, p.mi) - guess;
  return guess - offset;
}

// The agents' rounds: "HH:MM-HH:MM/N" (every N minutes from the first time to the last) or a list of times.
function roundTimes(spec) {
  if (Array.isArray(spec)) return spec;
  const m = /^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})\/(\d{1,3})$/.exec(String(spec || ""));
  if (!m) return [];
  const [from, to, every] = [+m[1] * 60 + +m[2], +m[3] * 60 + +m[4], Math.max(+m[5], 5)];
  const out = [];
  for (let t = from; t <= to && out.length < 200; t += every) out.push(`${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`);
  return out;
}

export function nextRuns(schedule, now) {
  const today = lisbonParts(now);
  const out = [];
  for (const job of JOBS) {
    let next = null;
    for (let offset = 0; offset < 40 && next === null; offset++) {
      const day = new Date(Date.UTC(today.y, today.m - 1, today.d + offset));
      const [y, m, d, dow] = [day.getUTCFullYear(), day.getUTCMonth() + 1, day.getUTCDate(), day.getUTCDay()];
      const weekday = dow >= 1 && dow <= 5;
      let times = [];
      if (job.weekdays && weekday) times = job.key === "ronda" ? roundTimes(schedule.ronda) : [].concat(schedule[job.key] || []);
      if (job.key === "semanal" && DAYS[dow] === schedule.semanal?.dia) times = [schedule.semanal.hora];
      if (job.key === "treino" && DAYS[dow] === "dom") times = [schedule.treino];
      for (const t of times.slice().sort()) {
        const [h, mi] = String(t).split(":").map(Number);
        const at = lisbonToEpoch(y, m, d, h, mi);
        if (at > now) { next = at; break; }
      }
    }
    const all = job.weekdays ? (job.key === "ronda" ? roundTimes(schedule.ronda) : [].concat(schedule[job.key] || [])) : [];
    out.push({ key: job.key, label: job.label, at: next, times: all });
  }
  return out.filter((j) => j.at).sort((a, b) => a.at - b.at);
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function html(body, status = 200) {
  return new Response(body, {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "X-Robots-Tag": "noindex" },
  });
}

// The member's Discord avatar (128 px), or null: the portal then shows their initial.
export function avatarUrl(owner, hash) {
  return /^\d{5,25}$/.test(String(owner || "")) && /^(a_)?[0-9a-f]{32}$/.test(hash || "")
    ? `https://cdn.discordapp.com/avatars/${owner}/${hash}.png?size=128` : null;
}

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// A menu button's label "📊 Dashboard": the icon in its own span (a bit bigger, centred with the text).
export function tabLabel(label) {
  const m = String(label).match(/^(\S+)\s+(.+)$/);
  return m && !/[A-Za-zÀ-ú0-9]/.test(m[1]) ? `<span class="ti" aria-hidden="true">${m[1]}</span><span>${m[2]}</span>` : label;
}

// JSON inside <script>: no "</script>" or line separators can break out.
export function safeJson(value) {
  return JSON.stringify(value).replace(/</g, "\\u003c").replace(/\u2028/g, "\\u2028").replace(/\u2029/g, "\\u2029");
}

// Scripts and styles that never change between requests are served once from /static/<name>-<hash>
// and kept by the browser for a year (a new deploy changes the hash, so nobody gets a stale copy).
export const STATIC = {};
const STATIC_PATHS = {};
function fnv(text) {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) { h ^= text.charCodeAt(i); h = Math.imul(h, 0x01000193); }
  return (h >>> 0).toString(16).padStart(8, "0");
}
export function staticAsset(name, body, kind) {
  const path = `/static/${name}-${fnv(body)}.${kind}`;
  STATIC[path] = { body, type: kind === "css" ? "text/css; charset=utf-8" : "text/javascript; charset=utf-8" };
  return path;
}
export function staticResponse(path) {
  const a = STATIC[path];
  return a ? new Response(a.body, { headers: { "Content-Type": a.type, "Cache-Control": "public, max-age=31536000, immutable" } }) : null;
}

export function page(title, body, head = "", opts = {}) {
  return `<!doctype html>
<html lang="pt-PT">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>${esc(title)}</title>
<link rel="icon" type="image/png" href="/favicon.png?v=2">
<link rel="stylesheet" href="${STATIC_PATHS.css}">
<script src="${STATIC_PATHS.i18n}"></script>
${head}
</head>
<body${opts.embed ? ' class="embed"' : ""}><main>${body}</main></body>
</html>`;
}

// The member's Discord avatar, or their name's initial (me: {name, avatar}).
export function memberFace(me, big = false) {
  if (!me) return "";
  return me.avatar
    ? `<img class="initial${big ? " big" : ""}" src="${esc(me.avatar)}" alt="" width="${big ? 72 : 30}" height="${big ? 72 : 30}" referrerpolicy="no-referrer">`
    : `<span class="initial${big ? " big" : ""}" aria-hidden="true">${esc([...me.name][0] || "?").toUpperCase()}</span>`;
}

// The site's top bar, on the portal and the profile: the logo (with the name) goes home on the left, the portal's search
// in the middle, the member's name (opens /perfil) on the right.
export function siteTopbar(me, home = "/", middle = "") {
  return `<div class="topbar">
  <h1 class="brand"><a class="home logo-link" href="${esc(home)}" title="Página principal"><img class="logo" src="/logo-full.png?v=1" alt="One Guillion Jollars" width="280" height="260"></a></h1>
  <div class="topbar-mid">${middle}</div>
  <div class="userbar">${me
    ? `<a class="me" href="/perfil" title="O meu perfil">${memberFace(me)}<b class="me-name">${esc(me.name)}</b></a><a class="btn alt" href="/sair">SAIR</a>`
    : `<a class="btn" href="/entrar">🔑 ENTRAR</a>`}</div>
  </div>`;
}

export function chartPage(data, upcoming, newer, extras = {}) {
  const banner = newer
    ? `<a class="banner" href="/g/${esc(newer)}">🔄 Há uma versão mais recente deste gráfico · abrir</a>`
    : "";
  const portal = !!extras.portal;
  const tabs = (portal ? [["dashboard", "📊 Dashboard"]] : []).concat([["corrida", "🏁 Corrida"]])
    .concat(portal && extras.pretrain ? [["pretreino", "⏪ Treino"]] : []).concat([["agentes", "🧾 Agentes"]])
    .concat(portal ? [["decisao", "⚖️ Decisão"], ["propostas", "🧠 Propostas"], ["noticias", "📰 Notícias"]] : [])
    .concat(portal ? [] : [["agenda", "⏱️ Agenda"]])  // in the portal the schedule is part of 🩺 Estado
    .concat(portal ? [["ajuda", "❓ Glossário"], ["estado", "🩺 Estado"]] : [])  // the portfolio is in the profile (the name, top right)
    .concat(portal && extras.admin ? [["admin", "🔒 Admin"]] : []);
  const me = extras.user;
  const body = `
${banner}
<header>
  ${portal ? siteTopbar(me, extras.home, `<form id="search" class="search" role="search" autocomplete="off">
    <input id="q" type="search" list="q-list" placeholder="Procurar um ticker (ex.: NVDA, VUSA)" aria-label="Procurar um ticker: notícias, score, agentes e a tua posição" title="Notícias, score, agentes e a tua posição">
    <datalist id="q-list"></datalist><button class="search-go" type="submit" aria-label="Procurar" title="Procurar">🔎</button>
  </form>`) : `<div class="topbar"><h1>${esc(data.title)}</h1></div>
  <p class="muted">${esc(data.subtitle || "")}</p>`}
</header>
<nav class="tabs" id="tabs">${tabs.map(([k, label], i) => `<button class="tab${i ? "" : " on"}" data-tab="${k}">${tabLabel(label)}</button>`).join("")}</nav>
${portal ? `<section data-tab="dashboard"><div id="dash" class="dash"></div></section>` : ""}
<section class="race" id="race" data-tab="corrida"${portal ? " hidden" : ""}>
  <div class="marquee"><span>🏁 Grande Prémio dos Agentes 🏁</span></div>
  <div class="hud">
    <div>Dia <b id="race-date">—</b></div>
    <div>Líder <b id="race-leader">—</b></div>
    <div class="controls"><button id="race-play" class="btn">▶ VER A CORRIDA</button><button id="race-speed" class="btn alt">1×</button></div>
  </div>
  <input type="range" id="race-scrub" min="0" value="0" aria-label="Dia da corrida">
  <p class="muted small">Cada corredor avança com a rentabilidade do agente; a meta é o melhor resultado da corrida. Toca num corredor para abrir a ficha dele.</p>
  <div class="race-split"><div class="race-board"><h2>🏆 Placar</h2><ol id="board" class="board"></ol></div>
    <div class="race-track"><h2>🏁 Corrida</h2><div id="track" class="track"></div></div></div>
  <h2>📈 Gráfico</h2>
  <div id="chart" class="chart"></div>
  <p class="muted small">Toca numa linha da legenda para a esconder ou mostrar, toca duas vezes para ver só essa. Arrasta para fazer zoom; duplo toque volta ao início.</p>
</section>
${portal && extras.pretrain ? `<section class="race" id="pt-race" data-tab="pretreino" hidden>
  <div class="marquee"><span>⏪ Treino · 20 anos com os parâmetros escolhidos · custos e impostos incluídos ⏪</span></div>
  <p class="muted small">${esc(extras.pretrain.subtitle || "")}. É uma simulação do passado: mostra como cada estratégia se teria portado, não o que vai fazer.</p>
  <div class="hud">
    <div>Dia <b id="pt-race-date">—</b></div>
    <div>Líder <b id="pt-race-leader">—</b></div>
    <div class="controls"><button id="pt-race-play" class="btn">▶ VER A CORRIDA</button><button id="pt-race-speed" class="btn alt">1×</button></div>
  </div>
  <input type="range" id="pt-race-scrub" min="0" value="0" aria-label="Dia da corrida do pré-treino">
  <div class="race-split"><div class="race-board"><h2>🏆 Placar</h2><ol id="pt-board" class="board"></ol></div>
    <div class="race-track"><h2>🏁 Corrida</h2><div id="pt-track" class="track"></div></div></div>
  <h2>📈 Gráfico</h2>
  <div id="pt-chart" class="chart"></div>
  <h2>📊 Desempenho</h2>
  <div id="pt-perf"></div>
  <h2>📈 Evolução entre execuções do treino</h2>
  <div id="evo"></div>
  <h2>🔎 Resultados do treino por agente</h2>
  <div class="agent-pick"><nav id="pt-chips" class="agent-list"></nav><div id="pt-detail"></div></div>
</section>` : ""}
<section id="agents-section" data-tab="agentes" hidden>
  <h2>📊 Desempenho</h2>
  <div id="perf"></div>
  <details class="panel" id="compare-box"><summary><b>⚖️ Comparar dois agentes</b></summary><div class="filters"><select id="cmp-a"></select><select id="cmp-b"></select></div><div id="cmp"></div></details>
  <details class="panel" id="holdings-box" open><summary><b>🗂️ Posições de todos os agentes</b> <span class="muted small">quem tem o quê, agora</span></summary>
    <div class="filters"><span class="chips"><button type="button" class="chip on" data-hv="agente">Por agente</button><button type="button" class="chip" data-hv="ativo">Por ativo</button></span>
    <select id="hold-agent"><option value="">Todos os agentes</option></select><input id="hold-q" type="search" placeholder="Ticker ou nome, ex.: NVDA"></div>
    <div id="holdings"></div></details>
  <h2>🔎 Ficha do agente</h2>
  <div class="agent-pick"><nav id="chips" class="agent-list"></nav><div id="detail"></div></div>
</section>
${portal ? `<section data-tab="decisao" hidden><div id="decision"></div></section>
<section data-tab="propostas" hidden><div id="props"></div></section>
<section data-tab="noticias" hidden><div id="news"></div></section>` : ""}
<section data-tab="${portal ? "estado" : "agenda"}" hidden>
  ${portal ? '<div id="status"></div><p class="muted small">Horários de Lisboa (salvo indicação), os mesmos do /horario no Discord. Cada tarefa demora alguns minutos a chegar.</p>'
    : '<h2>⏱️ Próximas atualizações</h2><div id="countdowns" class="grid"></div><p class="muted small">Horários de Lisboa, os mesmos do /horario no Discord. Cada tarefa demora alguns minutos a chegar.</p>'}
</section>
${portal ? `<section data-tab="ajuda" hidden><div id="glossary"></div></section>` : ""}
${portal && extras.admin ? `<section data-tab="admin" hidden><div id="admin"></div></section>` : ""}
${portal ? `<section data-tab="pesquisa" hidden><div id="q-out"></div></section>` : ""}
<footer class="site-foot">
  <p>📊 Dados da liga de <span id="updated"></span> (hora de Lisboa)</p>
  <p class="muted">Simulação com dinheiro fictício e preços reais · ferramenta educativa, não é recomendação de compra</p>
  ${portal ? '<p class="muted small">One Guillion Jollars</p>' : ""}
</footer>
<script>
const DATA = ${safeJson(data)};
const PORTAL_VERSION = ${safeJson(extras.version || null)};
const PREFS = ${safeJson(me ? me.settings || {} : null)};  // the member's settings, kept in the account
const WALLET = ${safeJson(me ? me.wallet : null)};
if (PREFS) try {  // the account wins over this browser's copies (capital and risk for the proposals)
  if (PREFS.capital !== undefined) localStorage.setItem("capital", JSON.stringify(PREFS.capital));
  if (PREFS.risk !== undefined) localStorage.setItem("risk", JSON.stringify(PREFS.risk));
} catch (e) {}
const UPCOMING = ${safeJson(upcoming)};
const NEWS = ${safeJson(extras.news || null)};
const MARKET = ${safeJson(extras.market || null)};
const PROPOSALS = ${safeJson(extras.proposals || null)};
const DECISION = ${safeJson(extras.decision || null)};
const CALENDAR = ${safeJson(extras.calendar || null)};
const PRETRAIN = ${safeJson(extras.pretrain || null)};
const RUNS = ${safeJson(extras.runs || null)};
const GEO = ${safeJson(extras.geo || null)};
const EVOLUTION = ${safeJson(extras.evolution || null)};
const QUOTES = ${safeJson(extras.quotes || null)};
</script>
<script src="${STATIC_PATHS.portal}"></script>
${portal ? `<script src="${STATIC_PATHS.extra}"></script>` : ""}
${portal && extras.admin ? `<script src="${STATIC_PATHS.admin}"></script>` : ""}`;
  return page(portal ? "One Guillion Jollars" : data.title, body, `${extras.manifest ? `<link rel="manifest" href="${esc(extras.manifest)}"><meta name="theme-color" content="#0b0820"><link rel="apple-touch-icon" href="/icon-192.png?v=1gj">` : ""}
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Press+Start+2P&display=swap" rel="stylesheet">
<script src="${PLOTLY}"></script>`);
}

const CSS = `
:root { color-scheme: dark; --bg:#0b0820; --card:#17123a; --line:#2e2763; --fg:#e8e6ff; --muted:#9b96c9; --up:#39ff88; --down:#ff4d6d; --accent:#ff2e88; --neon:#00e5ff; --gold:#ffe14d; --pixel:"Press Start 2P",ui-monospace,monospace; }
* { box-sizing: border-box; }
body { margin:0; background:radial-gradient(circle at 50% -20%,#2a1a6b 0,var(--bg) 60%) fixed; color:var(--fg); font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
body::after { content:""; position:fixed; inset:0; pointer-events:none; background:repeating-linear-gradient(0deg,rgba(0,0,0,.18) 0 1px,transparent 1px 3px); }
h1, h2, .pixel { font-family:var(--pixel); letter-spacing:.5px; } h1 { color:var(--gold); text-shadow:0 0 8px rgba(255,225,77,.6); line-height:1.5; }
.topbar { display:grid; grid-template-columns:1fr minmax(0,520px) 1fr; align-items:center; gap:8px 24px; }
.topbar h1 { margin:0; line-height:0; }
.topbar .brand { justify-self:start; }
.topbar .logo { height:100px; width:auto; display:block; }
.topbar-mid { display:flex; justify-content:center; min-width:0; }
a.home { color:inherit; text-decoration:none; }
.me { display:inline-flex; align-items:center; gap:8px; padding:4px 12px 4px 4px; border-radius:999px; border:1px solid var(--line); color:var(--fg); text-decoration:none; transition:border-color .15s, box-shadow .15s; }
.me:hover, .me:focus-visible { border-color:var(--neon); box-shadow:0 0 10px rgba(0,229,255,.35); }
.initial { display:inline-grid; place-items:center; width:30px; height:30px; border-radius:50%; background:linear-gradient(135deg,var(--accent),#7b2ff7); color:#fff; font-weight:700; flex:none; }
.initial.big { width:72px; height:72px; font-size:2rem; }
.profile-card { display:flex; align-items:center; gap:16px; }
.profile-card h2 { margin:0 0 4px; font-size:1rem !important; }
.initial { object-fit:cover; } .profile-form select { padding:8px 10px; border-radius:6px; border:1px solid var(--line); background:#0f0b2b; color:var(--fg); font:inherit; margin-top:4px; }
.profile-form input:not([type=checkbox]) { flex:1; min-width:180px; padding:10px 12px; border-radius:6px; border:1px solid var(--line); background:#0f0b2b; color:var(--fg); font:inherit; }
.profile-form .row-btns { display:flex; gap:8px; flex-wrap:wrap; margin:8px 0 4px; }
.profile-layout { display:grid; grid-template-columns:230px minmax(0,1fr); gap:20px; align-items:start; margin-top:14px; }
.side { position:sticky; top:12px; display:flex; flex-direction:column; gap:6px; padding:12px; border:1px solid var(--line); border-radius:10px; background:var(--card); }
.side .tab { text-align:left; width:100%; line-height:1.6; justify-content:flex-start; }
.side-h { margin:6px 2px 0; font-size:.72rem; color:var(--muted); text-transform:uppercase; letter-spacing:.06em; }
.side-h:first-child { margin-top:0; }
.side-main > section > h2:first-child, .side-main > section > .grid:first-child { margin-top:0; }
.resumo-layout { display:grid; grid-template-columns:minmax(0,1fr) 230px; gap:16px; align-items:start; }
.resumo-main > h2:first-child { margin-top:0; }
.totals-col { display:flex; flex-direction:column; gap:8px; }
.totals-col .tile { margin:0; }
@media (max-width:760px) {
  .profile-layout { grid-template-columns:1fr; gap:10px; }
  .side { position:static; flex-direction:row; overflow-x:auto; padding:8px; scrollbar-width:none; }
  .side::-webkit-scrollbar { display:none; }
  .side .tab { width:auto; white-space:nowrap; } .side-h { display:none; }
  .resumo-layout { grid-template-columns:1fr; }
  .totals-col { display:grid; grid-template-columns:1fr 1fr; order:-1; }
}
.topbar .userbar { justify-self:end; }
@media (max-width:760px) { .topbar { grid-template-columns:auto 1fr; } .topbar .logo { height:84px; }
  .topbar-mid { grid-column:1 / -1; grid-row:2; } .topbar-mid:empty { display:none; } }
h2 { color:var(--neon); text-shadow:0 0 6px rgba(0,229,255,.55); font-size:.8rem !important; line-height:1.6; }
.marquee { overflow:hidden; white-space:nowrap; border:2px solid var(--accent); border-radius:6px; background:#12002a; box-shadow:0 0 12px rgba(255,46,136,.5); margin:8px 0; }
.marquee span { display:inline-block; padding:8px 0 8px 100%; font-family:var(--pixel); font-size:.75rem; color:var(--gold); animation:scroll 14s linear infinite; }
@keyframes scroll { to { transform:translateX(-100%); } }
.hud { display:flex; flex-wrap:wrap; gap:10px 18px; align-items:center; font-family:var(--pixel); font-size:.62rem; color:var(--muted); margin:10px 0; }
.hud b { color:var(--gold); font-weight:400; } .controls { margin-left:auto; display:flex; gap:6px; }
.btn { font-family:var(--pixel); font-size:.62rem; background:var(--accent); color:#fff; border:0; border-bottom:3px solid #a3004f; border-radius:4px; padding:9px 12px; cursor:pointer; }
.btn:active { transform:translateY(2px); border-bottom-width:1px; } .btn.alt { background:#3a2f86; border-bottom-color:#1d1650; }
.track { background:linear-gradient(#1f6b3a,#195a31); border:3px solid #0d2e19; border-radius:10px; padding:6px 0; box-shadow:inset 0 0 18px rgba(0,0,0,.6),0 0 14px rgba(57,255,136,.25); }
.lane { position:relative; height:46px; border-bottom:2px dashed rgba(255,255,255,.22); display:flex; align-items:center; }
.lane:last-child { border-bottom:0; }
.lane .no { width:30px; flex:none; text-align:center; font-family:var(--pixel); font-size:.55rem; color:rgba(255,255,255,.6); }
.lane .run { position:relative; flex:1; height:100%; margin-right:26px; }
.lane .finish { position:absolute; right:0; top:0; bottom:0; width:18px; background:repeating-conic-gradient(#fff 0 25%,#111 0 50%) 0 0/9px 9px; opacity:.85; }
.racer { position:absolute; top:50%; transform:translate(-50%,-50%); transition:left .25s linear; cursor:pointer; display:flex; align-items:center; }
.racer .avatar { width:36px; height:36px; display:grid; place-items:center; font-size:20px; border-radius:50%; border:3px solid var(--c); background:radial-gradient(circle at 35% 30%,#fff3,transparent 60%),#0b0820; box-shadow:0 0 10px var(--c); }
.racer .bib { position:absolute; bottom:-4px; right:-6px; font-family:var(--pixel); font-size:.45rem; background:var(--c); color:#0b0820; border-radius:3px; padding:2px 3px; }
.racer .tag { position:absolute; left:42px; white-space:nowrap; font-family:var(--pixel); font-size:.5rem; color:#fff; text-shadow:0 1px 0 #000,0 0 4px #000; }
.racer.left-tag .tag { left:auto; right:42px; }
.racer .crown { position:absolute; top:-14px; left:9px; font-size:14px; }
.racer.dead .avatar { filter:grayscale(1); opacity:.5; }
.running .racer .avatar { animation:bob .28s ease-in-out infinite alternate; }
.running .racer::before { content:"💨"; position:absolute; left:-22px; font-size:14px; opacity:.8; }
@keyframes bob { to { transform:translateY(-4px) rotate(-6deg); } }
#race-scrub, #pt-race-scrub { width:100%; margin:10px 0 0; accent-color:var(--accent); }
table.perf th, table.perf td { padding:6px 5px; font-size:.86rem; } table.perf th { white-space:normal; line-height:1.2; }
ul.tree { list-style:none; padding-left:0; } ul.tree ul { list-style:none; padding-left:18px; border-left:2px solid #3a2f86; margin-left:6px; }
ul.tree li { margin:6px 0; } ul.tree details summary { cursor:pointer; }
table.perf td:first-child { white-space:normal; min-width:120px; } table.perf tr.benched td { opacity:.45; }
.race-split { display:grid; grid-template-columns:minmax(260px,2fr) 3fr; gap:14px; align-items:start; }
.race-split > div { min-width:0; } .race-split h2 { margin-top:6px; }
@media (max-width:820px) { .race-split { grid-template-columns:1fr; } .race-track { order:-1; } }
.board { list-style:none; padding:0; margin:0; display:grid; gap:6px; counter-reset:pos; }
.board li { display:flex; align-items:center; gap:10px; background:var(--card); border:2px solid var(--line); border-radius:6px; padding:8px 10px; cursor:pointer; }
.board li .pos { font-family:var(--pixel); font-size:.65rem; width:34px; color:var(--gold); }
.board li:nth-child(1) { border-color:var(--gold); box-shadow:0 0 10px rgba(255,225,77,.45); }
.board li:nth-child(2) { border-color:#c0c7d6; } .board li:nth-child(3) { border-color:#d08a4a; }
.tabs { position:relative; z-index:5; display:flex; flex-wrap:wrap; gap:6px; padding:8px 0; margin:18px 0 4px; }
.tab { flex:none; font-family:var(--pixel); font-size:.55rem; background:var(--card); color:var(--fg); border:2px solid var(--line); border-radius:6px; padding:7px 10px; cursor:pointer;
       display:inline-flex; align-items:center; justify-content:center; gap:7px; line-height:1.2; }
.tab .ti { font-family:system-ui, "Segoe UI Emoji", "Apple Color Emoji", sans-serif; font-size:1.05rem; line-height:1; transform:translateY(-1.5px); }  /* the pixel font's ink sits high: lift the emoji to its centre */
.tab.on { border-color:var(--accent); color:#fff; box-shadow:0 0 10px rgba(255,46,136,.5); }
@media (min-width:641px) { .tabs .tab { flex:1 1 auto; text-align:center; white-space:nowrap; } }
.news { display:grid; gap:8px; } .news article { background:var(--card); border:1px solid var(--line); border-left:4px solid var(--line); border-radius:8px; padding:10px 12px; }
.news article.pos { border-left-color:var(--up); } .news article.neg { border-left-color:var(--down); }
.news .meta { color:var(--muted); font-size:.8rem; } .news a { color:var(--neon); }
.filters { display:flex; flex-wrap:wrap; gap:6px; margin:8px 0; } .filters input, .filters select { background:var(--card); color:var(--fg); border:1px solid var(--line); border-radius:6px; padding:7px 9px; font:inherit; }
.opp { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px; }
.opp .top { display:flex; flex-wrap:wrap; gap:6px 12px; align-items:baseline; } .opp .sym { font-weight:700; font-size:1.05rem; }
.score { font-family:var(--pixel); font-size:.62rem; padding:5px 7px; border-radius:4px; color:#0b0820; }
.score.Alta { background:var(--up); } .score.Média { background:var(--gold); } .score.Baixa { background:#ff9f43; } .score.Muito { background:var(--down); }
.bar { height:6px; background:#0b0820; border-radius:3px; overflow:hidden; } .bar i { display:block; height:100%; background:var(--neon); }
details summary { cursor:pointer; color:var(--neon); margin-top:6px; }
abbr.g { text-decoration:underline dotted; text-underline-offset:3px; cursor:help; }
#gpop { position:fixed; left:12px; right:12px; bottom:84px; z-index:30; background:#0b0820; color:var(--fg); border:2px solid var(--neon); border-radius:8px; padding:10px 12px; box-shadow:0 0 14px rgba(0,229,255,.4); max-width:560px; margin:0 auto; }
@media (max-width:640px) {
  .tabs { position:fixed; top:auto; bottom:0; left:0; right:0; z-index:20; margin:0; padding:6px 8px calc(6px + env(safe-area-inset-bottom));
          background:rgba(11,8,32,.96); border-top:2px solid var(--accent);
          flex-wrap:nowrap; overflow-x:auto; scrollbar-width:none; -webkit-overflow-scrolling:touch; }
  .tabs::-webkit-scrollbar { display:none; }  /* swipe sideways, no bar */
  main { padding-bottom:84px; }
}
.lot-filters .chips { display:flex; flex-wrap:wrap; gap:6px; margin:4px 0; } .lot-filters .chip { padding:3px 10px; font-size:.8rem; }
.lot-sum { margin:8px 0; } .lot-day { margin:10px 0 4px; } .lot-day-head { border-bottom:1px solid var(--line); padding:2px 0 4px; margin-bottom:4px; }
.lot-line { display:inline-flex; flex-wrap:wrap; gap:4px 10px; align-items:baseline; }
#holdings-box .filters { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:8px 0; } #holdings-box .chips { display:flex; gap:6px; }
.lot { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:8px 12px; margin:6px 0; }
.lot summary { color:var(--fg); } .lot .trade { border-left:3px solid var(--line); padding:4px 10px; margin:8px 0 0 4px; }
.lot .trade.b { border-left-color:var(--up); } .lot .trade.s { border-left-color:var(--down); } .why { color:var(--muted); margin:.3rem 0; white-space:normal; }
.cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:10px; }
@media (max-width:640px) { .cards { grid-template-columns:1fr; } }
.board li .nm { flex:1; font-weight:600; } .board li .val { font-family:var(--pixel); font-size:.62rem; }
main { max-width:1440px; margin:0 auto; padding:16px clamp(12px, 3vw, 40px); }
@media (min-width:1600px) { body { font-size:16px; } .chart { height:66vh; } }
h1 { font-size:1.35rem; margin:.2rem 0; } h2 { font-size:1.05rem; margin:1.4rem 0 .6rem; } h3 { font-size:1rem; margin:1.1rem 0 .4rem; }
.muted { color:var(--muted); } .small { font-size:.82rem; } .up { color:var(--up); } .down { color:var(--down); }
.banner { display:block; background:var(--accent); color:#fff; padding:10px 14px; border-radius:8px; text-decoration:none; margin-bottom:12px; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(170px,1fr)); gap:8px; }
@media (max-width:640px) { .grid { grid-template-columns:1fr 1fr; } .tile { padding:8px 10px; } .tile .v { font-size:1rem; } main { padding:12px; } }
.tile { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:10px 12px; }
.tile .v { color:#fff; }
.tile .k { color:var(--muted); font-size:.8rem; } .tile .v { font-size:1.15rem; font-weight:600; font-variant-numeric:tabular-nums; }
.tile .s { color:var(--muted); font-size:.78rem; }
.dash-top { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin-top:12px; }
.dash-top .tile .v { font-size:1.45rem; } .dash .tile .v.up { color:var(--up); } .dash .tile .v.down { color:var(--down); } .dash .tile .v.gold { color:var(--gold); }
.dash-row { display:grid; grid-template-columns:minmax(0,1.6fr) minmax(0,1fr); gap:12px; margin-top:12px; align-items:start; }
.dash-col { display:grid; gap:12px; min-width:0; }
.dash-row-a { grid-template-columns:minmax(0,1fr) minmax(0,1.6fr); align-items:stretch; }
.dash-league { display:flex; flex-direction:column; } .dash-league-list { flex:1 1 0; min-height:0; overflow:hidden; }
.dash-league-list .drow:first-child { border-top:0; }
.dcard { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px 14px; min-width:0; }
.dcard .dh { display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:8px; }
.dcard .dh h3 { margin:0; } .dcard .dh a { font-size:.82rem; }
.drow { display:flex; justify-content:space-between; align-items:center; gap:10px; padding:6px 0; border-top:1px solid var(--line); font-size:.9rem; }
.dh + .drow, .chips + .dash-chart + .drow { border-top:0; } .drow .nm { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.dash .grid { margin-bottom:8px; } .dash .chips { margin-bottom:6px; } .dash .chips .chip { padding:3px 10px; font-size:.8rem; }
.dash-chart { height:260px; } .dash-bars { height:260px; margin-top:4px; }
.wtiles { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:8px; margin-bottom:10px; } .wtiles .tile { margin:0; }
.qhelp { display:inline-grid; place-items:center; width:15px; height:15px; margin-left:3px; border:1px solid var(--muted); border-radius:50%; font-size:.68rem; line-height:1; cursor:help; vertical-align:1px; }
.qhelp:hover, .qhelp:focus { border-color:var(--neon); color:var(--neon); outline:none; }
.qhelp { position:relative; } .qhelp::after { content:attr(data-tip); position:absolute; right:-10px; top:calc(100% + 8px); width:min(260px, 70vw); padding:8px 10px; border:1px solid var(--neon);
  border-radius:8px; background:#17123a; color:var(--fg); font-size:.78rem; line-height:1.35; text-align:left; white-space:normal; z-index:20; box-shadow:0 6px 18px rgba(0,0,0,.5);
  opacity:0; visibility:hidden; transition:opacity .12s; pointer-events:none; }
.qhelp:hover::after, .qhelp:focus::after { opacity:1; visibility:visible; }
@media (max-width:900px) { .wtiles { grid-template-columns:repeat(2,minmax(0,1fr)); } .wtiles .tile:first-child { grid-column:1 / -1; } }
.wbar { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:6px 12px; margin-bottom:6px; }
.dash .wbar .chips { margin:0; } .wleft { display:flex; flex-wrap:wrap; align-items:center; gap:6px 12px; }
.wchk { display:inline-flex; align-items:center; gap:5px; font-size:.8rem; color:var(--muted); cursor:pointer; user-select:none; }
.wchk input { accent-color:#3d7bff; margin:0; cursor:pointer; } .wper { margin:8px 0 2px; text-align:right; }
.wrange { display:flex; flex-wrap:wrap; align-items:center; gap:6px; font-size:.8rem; color:var(--muted); }
.wrange input { background:var(--card); color:var(--fg); border:1px solid var(--line); border-radius:6px; padding:3px 6px; font:inherit; color-scheme:dark; }
.wadv { margin-left:auto; margin-right:14px; white-space:nowrap; cursor:help; } .wadv.gold { color:var(--gold); }
.wsel { cursor:pointer; border-radius:6px; padding-left:6px; padding-right:6px; transition:background .15s; }
.drow .wnm { display:flex; flex-direction:column; gap:1px; white-space:normal; }
.wnm .wl1, .wnm .wl2 { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.wnm .wname { color:var(--fg); opacity:.75; font-size:.85rem; }
.wsel:hover { background:rgba(0,229,255,.06); } .wsel.on { background:rgba(26,86,196,.28); box-shadow:inset 3px 0 0 #3d7bff; }
@media (max-width:900px) { .dash-top { grid-template-columns:1fr 1fr; } .dash-row, .dash-row-a { grid-template-columns:1fr; } .dash-league-list { flex:none; } }
.chart { width:100%; height:62vh; min-height:340px; background:var(--card); border:2px solid var(--line); border-radius:8px; }
.range-custom { display:flex; flex-wrap:wrap; align-items:center; gap:6px; margin:0 0 6px; font-size:12px; color:var(--muted, #b5bac1); }
.range-custom input { background:#1e1f22; color:inherit; border:1px solid var(--line); border-radius:4px; padding:2px 6px; font:inherit; color-scheme:dark; }
.chips { display:flex; flex-wrap:wrap; gap:6px; }
.chip { background:var(--card); border:1px solid var(--line); color:var(--fg); border-radius:999px; padding:6px 10px; cursor:pointer; font:inherit; font-size:.88rem; }
.chip.on { border-color:var(--accent); background:#3a0f45; box-shadow:0 0 8px rgba(255,46,136,.5); }
.agent-pick { display:grid; grid-template-columns:minmax(230px,300px) 1fr; gap:14px; align-items:start; }
.agent-pick > div { min-width:0; }
.agent-list { position:sticky; top:58px; max-height:calc(100vh - 70px); overflow:auto; background:var(--card); border:1px solid var(--line); border-radius:8px; padding:8px; }
.agent-find { width:100%; box-sizing:border-box; margin-bottom:6px; background:#1e1f22; color:var(--fg); border:1px solid var(--line); border-radius:6px; padding:6px 8px; font:inherit; }
.agent-group > summary { cursor:pointer; padding:6px 4px; font-weight:600; color:var(--neon); }
.agent-item { display:flex; justify-content:space-between; gap:8px; width:100%; text-align:left; background:none; border:1px solid transparent; border-radius:6px; color:var(--fg); padding:5px 8px; cursor:pointer; font:inherit; font-size:.88rem; }
.agent-item:hover { background:#241c55; }
.agent-item.on { border-color:var(--accent); background:#3a0f45; box-shadow:0 0 8px rgba(255,46,136,.4); }
.chap-tools { display:flex; gap:6px; justify-content:flex-end; margin:-4px 0 6px; }
.sheet-head { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:6px 12px; margin:1.4rem 0 .6rem; }
.sheet-head h2, .sheet-head .chap-tools { margin:0; }
details.params summary { cursor:pointer; color:var(--neon); }
.tile .v.adopted { color:var(--up); }
details.chap { border-top:1px solid var(--line); padding:2px 0; }
details.chap > summary { cursor:pointer; padding:8px 2px; font-weight:700; font-size:1.02rem; }
details.chap[open] > summary { color:var(--neon); }
@media (max-width:820px) { .agent-pick { grid-template-columns:1fr; } .agent-list { position:static; max-height:42vh; } }
.panel { background:var(--card); border:2px solid var(--neon); box-shadow:0 0 12px rgba(0,229,255,.25); border-radius:10px; padding:14px; margin-top:12px; }
.lang-pick { display:inline-flex; border:1px solid var(--line, #2e2763); border-radius:6px; overflow:hidden; }
.lang-pick button { background:transparent; color:inherit; border:0; padding:5px 8px; font:inherit; font-size:.75rem; cursor:pointer; opacity:.65; }
.lang-pick button.on { background:var(--accent, #ff2e88); color:#fff; opacity:1; }
.lang-pick.floating { position:fixed; top:10px; right:10px; z-index:60; background:#17123a; }
.userbar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; justify-content:flex-end; font-size:.9rem; }
.userbar .btn { text-decoration:none; display:inline-block; }
body.embed .tabs { position:static !important; } body.embed main { padding-top:4px; padding-bottom:12px; } body.embed .embed-hide { display:none !important; }
.search { position:relative; display:flex; width:100%; margin:0; }
.search input { flex:1; min-width:0; background:var(--card); color:var(--fg); border:1px solid var(--line); border-radius:999px; padding:9px 44px 9px 16px; font:inherit; font-size:.92rem; transition:border-color .15s, box-shadow .15s; }
.search input:focus { outline:none; border-color:var(--neon); box-shadow:0 0 8px rgba(0,229,255,.45); }
.search input::-webkit-search-cancel-button { display:none; }
.search-go { position:absolute; right:4px; top:50%; transform:translateY(-50%); width:30px; height:30px; border:0; border-radius:50%; background:transparent; color:var(--fg); cursor:pointer; font-size:.95rem; }
.search-go:hover { background:var(--line); }
.site-foot { margin:40px 0 0; padding:18px 0 6px; border-top:1px solid var(--line); text-align:center; font-size:.85rem; }
.site-foot p { margin:4px 0; }
.dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; vertical-align:middle; } .dot.ok { background:var(--up); box-shadow:0 0 6px var(--up); } .dot.old { background:var(--gold); } .dot.bad { background:var(--down); } .dot.run { background:var(--neon); box-shadow:0 0 6px var(--neon); }
td .wrap { display:block; white-space:normal; min-width:280px; max-width:560px; line-height:1.35; }
.alert { border-radius:6px; padding:6px 10px; font-size:.85rem; margin:6px 0; } .alert.down { background:rgba(255,77,109,.14); border:1px solid var(--down); } .alert.warn { background:rgba(255,225,77,.10); border:1px solid var(--gold); }
.opp.sellcard { border-color:rgba(255,77,109,.55); } .opp.watchcard { border-color:rgba(255,225,77,.45); }
.row-btns { display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
@media (max-width:640px) { .lane { height:40px; } .racer .avatar { width:30px; height:30px; font-size:16px; } .racer .tag { display:none; } .controls { margin-left:0; } }
.scroll { overflow-x:auto; -webkit-overflow-scrolling:touch; }
table { border-collapse:collapse; width:100%; font-size:.86rem; font-variant-numeric:tabular-nums; }
th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
th { color:var(--muted); font-weight:500; } td.num, th.num { text-align:right; }
code { background:#1e1f22; padding:1px 4px; border-radius:4px; }
.prose p { margin:.35rem 0; } .tag { display:inline-block; background:#1e1f22; border-radius:4px; padding:1px 6px; margin:0 4px 4px 0; font-size:.8rem; }
`;

// Runs in the browser. DATA and UPCOMING are defined just before.
const CLIENT = String.raw`
const $ = (id) => document.getElementById(id);
const store = { get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
                set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} } };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
// Discord-style markdown from the bot's texts: **bold**, *italic*, _italic_, \`code\`, new lines.
const md = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\x60(.+?)\x60/g, "<code>$1</code>")
  .replace(/(^|[\s(])_(.+?)_(?=[\s).,]|$)/g, "$1<i>$2</i>").replace(/\n/g, "<br>");
const fmtEur = (x) => x == null ? "—" : new Intl.NumberFormat("pt-PT", { style: "currency", currency: "EUR" }).format(x);
const fmtNum = (x, d = 2) => x == null ? "—" : new Intl.NumberFormat("pt-PT", { maximumFractionDigits: d, minimumFractionDigits: d }).format(x);
const fmtPct = (x, d = 1) => x == null ? "—" : (x > 0 ? "+" : "") + fmtNum(x, d) + "%";
const cls = (x) => x > 0 ? "up" : x < 0 ? "down" : "";
const lisbon = (ms, opts) => new Intl.DateTimeFormat("pt-PT", { timeZone: "Europe/Lisbon", ...opts }).format(new Date(ms));

$("updated").textContent = lisbon(Date.parse(DATA.updated), { dateStyle: "medium", timeStyle: "short" });

// ---- countdowns ----
function left(ms) {
  if (ms <= 0) return "a correr agora";
  const s = Math.floor(ms / 1000), d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
  return (d ? d + " d " : "") + (d || h ? h + " h " : "") + m + " min " + String(sec).padStart(2, "0") + " s";
}
// A job's times of day: evenly spaced ones as "de 10 em 10 min, 08:05–21:15", a few as a list.
function timesText(times) {
  const mins = times.map((t) => +t.slice(0, 2) * 60 + +t.slice(3, 5));
  const step = mins[1] - mins[0];
  if (times.length > 4 && step > 0 && mins.every((m, i) => !i || m - mins[i - 1] === step))
    return times.length + "× por dia útil, de " + (step % 60 ? step + " em " + step + " min" : step / 60 + " em " + step / 60 + " h") +
      ": " + times[0] + "–" + times[times.length - 1];
  return times.length + "× por dia útil: " + times.join(" · ");
}
function drawCountdowns() {
  if (!$("countdowns")) return;  // the portal shows the schedule in 🩺 Estado's table
  const now = Date.now();
  $("countdowns").innerHTML = UPCOMING.map((j) =>
    '<div class="tile"><div class="k">' + esc(j.label) + '</div><div class="v">' + left(j.at - now) + '</div><div class="s">' +
    lisbon(j.at, { weekday: "long", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) +
    (j.times.length > 1 ? " · " + esc(timesText(j.times)) : "") + '</div></div>').join("");
}
drawCountdowns();
setInterval(drawCountdowns, 1000);

// ---- chart ----
function lineChart(elId, DATA) {
const palette = ["#4cc0a8", "#e2b84a", "#5865f2", "#ed4245", "#57f287", "#eb459e", "#f0b232", "#9b84ee", "#3ba5d8", "#dbdee1",
  "#ff8c42", "#00e5ff", "#c3e88d", "#ff2e88", "#a7c7e7", "#d4a373"];
// In colour: the leaders of the interval in view (DATA.highlight: the n best since the start plus the Indexador) —
// each agent with its own colour, so a line keeps it when it enters or leaves the leaders.
const highlight = new Set(DATA.highlight || []);
const always = new Set([...highlight].filter((n) => n === "Indexador"));
const own = (DATA.series || []).map((s, i) => (s.name === "Indexador" ? "#5865f2" : palette[i % palette.length]));
const pick = highlight.size ? { n: highlight.size - always.size, always, own, agents: (DATA.series || []).length } : null;
const traces = (DATA.series || []).map((s, i) => {
  const on = !highlight.size || highlight.has(s.name);
  return { x: s.x, y: s.y, name: s.name, meta: s.name, type: "scatter", mode: s.x.length < 3 ? "lines+markers" : "lines",
    line: { width: on ? 2 : 1, color: on ? own[i] : "#6d6f78" }, opacity: on ? 1 : 0.55,
    hovertemplate: "%{y:+.1f}%<extra>" + esc(s.name) + "</extra>" };
}).concat((DATA.references || []).map((s, i) => ({ x: s.x, y: s.y, name: s.name, meta: s.name, type: "scatter", mode: "lines",
  line: { width: 1.2, dash: i ? "dot" : "dash", color: i ? "#b5bac1" : "#80848e" },
  hovertemplate: "%{y:+.1f}%<extra>" + esc(s.name) + "</extra>" })));
const xs = traces.flatMap((t) => [t.x[0], t.x[t.x.length - 1]]).filter(Boolean).sort();
const spanYears = xs.length ? (Date.parse(xs[xs.length - 1]) - Date.parse(xs[0])) / 3.15e10 : 0;
const narrow = window.innerWidth < 640;
// the spacing of the latest points (weekly data, like the pre-training's, cannot show one day: no 1D there)
const dataStep = (() => {
  const x = ((DATA.series || [])[0] || { x: [] }).x.slice(-21);
  const gaps = x.slice(1).map((d, i) => (Date.parse(d) - Date.parse(x[i])) / 864e5).sort((a, b) => a - b);
  return gaps.length >= 5 ? gaps[Math.floor(gaps.length / 2)] : gaps.length ? gaps[0] : 1;
})();
// every period (each counted back from the last day; one longer than the history shows all of it), then Tudo and Custom
const periods = [[1, "1D", "day", 1], [7, "1W", "day", 7], [1, "1M", "month", 30], [3, "3M", "month", 91], [6, "6M", "month", 182],
  [1, "1A", "year", 365], [2, "2A", "year", 730], [3, "3A", "year", 1095], [4, "4A", "year", 1461], [5, "5A", "year", 1826], [10, "10A", "year", 3652]]
  .filter((p) => spanYears > 0 && p[3] >= dataStep).map(([count, label, step]) => ({ count, label, step, stepmode: "backward" }));
if (!window.Plotly) $(elId).innerHTML = '<p class="muted" style="padding:16px">Não consegui carregar o gráfico (Plotly). As fichas dos agentes estão em baixo.</p>';
else Plotly.newPlot(elId, traces, {
  paper_bgcolor: "#17123a", plot_bgcolor: "#17123a", font: { color: "#e8e6ff", size: 12 },
  margin: { l: 52, r: 12, t: 12, b: narrow ? 10 : 40 }, hovermode: "x unified",
  hoverlabel: { bgcolor: "#0b0820", bordercolor: "#2e2763" },
  xaxis: { gridcolor: "#2e2763", hoverformat: "%d/%m/%Y", tickformat: spanYears > 2 ? "%Y" : spanYears > 0.4 ? "%m/%y" : "%d/%m",
    dtick: spanYears < 0.05 ? 86400000 : undefined, rangeselector: periods.length ? { bgcolor: "#1e1f22", activecolor: "#5865f2",
    buttons: periods.concat([{ step: "all", label: "Tudo" }]) } : undefined },
  yaxis: { gridcolor: "#2e2763", zerolinecolor: "#80848e", ticksuffix: "%" },
  legend: narrow ? { orientation: "h", y: -0.12, font: { size: 11 } } : { bgcolor: "rgba(0,0,0,0)" },
}, { responsive: true, displaylogo: false, modeBarButtonsToRemove: ["lasso2d", "select2d"] });
if (window.Plotly) intervalLegend(elId, traces, pick);
if (window.Plotly && periods.length) customRange(elId, xs[0], xs[xs.length - 1]);
}

// Custom: any interval, from one date to another (the same as zooming: the legend and the lines follow it).
function customRange(elId, first, last) {
  const el = $(elId);
  const day = (v) => String(v).slice(0, 10);
  const bar = document.createElement("div");
  bar.className = "range-custom";
  bar.innerHTML = '<span>Custom</span><input type="date" aria-label="De"> <span>→</span> <input type="date" aria-label="Até">';
  el.parentNode.insertBefore(bar, el);
  const [a, b] = bar.querySelectorAll("input");
  for (const inp of [a, b]) { inp.min = day(first); inp.max = day(last); }
  a.value = day(first); b.value = day(last);
  const go = () => {
    if (!a.value || !b.value) return;
    const [from, to] = a.value <= b.value ? [a.value, b.value] : [b.value, a.value];
    Plotly.relayout(el, { "xaxis.range": [from, to + " 23:59"] });
  };
  a.addEventListener("change", go);
  b.addEventListener("change", go);
  el.on("plotly_relayout", (ev) => {  // the dates show the interval in view, however it was chosen
    if (!ev) return;
    if (ev["xaxis.autorange"]) { a.value = day(first); b.value = day(last); return; }
    const r = ev["xaxis.range"] || (ev["xaxis.range[0]"] !== undefined ? [ev["xaxis.range[0]"], ev["xaxis.range[1]"]] : null);
    if (r) { a.value = day(r[0]) < day(first) ? day(first) : day(r[0]); b.value = day(r[1]) > day(last) ? day(last) : day(r[1]); }
  });
}

// Legend in the order of the lines (best result on top) with each one's result over the visible interval:
// updated when the interval changes (zoom, drag, 1A/5A/10A/Tudo). The lines (and their hover) then start at 0% on the
// interval's first day, so the chart, the hover and the legend all tell the result since then.
function intervalLegend(elId, traces, pick) {
  const el = $(elId);
  const day = (v) => String(v).slice(0, 10);
  const fmtDay = (d) => d.slice(8, 10) + "/" + d.slice(5, 7) + "/" + d.slice(0, 4);
  const orig = traces.map((t) => t.y.slice());  // Plotly.update replaces the traces' y: keep the originals
  let busy = false;
  const apply = (from, to) => {
    const bounds = traces.map((t, k) => {
      let i0 = -1, i1 = -1;
      for (let i = 0; i < t.x.length; i++) {
        const d = day(t.x[i]);
        if (orig[k][i] == null || (from && d < from) || (to && d > to)) continue;
        if (i0 < 0) i0 = i;
        i1 = i;
      }
      return [i0, i1];
    });
    // each line from its first visible point (the whole history: as it is)
    const ys = orig.map((y, k) => {
      const i0 = bounds[k][0];
      if (!(from || to) || i0 < 0) return y;
      const base = 1 + y[i0] / 100;
      return y.map((v) => (v == null ? null : Math.round(((1 + v / 100) / base - 1) * 10000) / 100));
    });
    const res = bounds.map(([i0, i1], k) =>
      (i0 < 0 || i1 <= i0 ? null : ((1 + orig[k][i1] / 100) / (1 + orig[k][i0] / 100) - 1) * 100));
    const order = res.map((r, i) => [r == null ? -Infinity : r, i]).sort((a, b) => b[0] - a[0]);
    const rank = []; order.forEach(([, i], k) => { rank[i] = k + 1; });
    const names = traces.map((t, i) => t.meta + (res[i] == null ? "" : " · " + (res[i] > 0 ? "+" : "") + fmtNum(res[i], 1) + "%"));
    const xs = traces.flatMap((t) => t.x.length ? [day(t.x[0]), day(t.x[t.x.length - 1])] : []).sort();
    const a = from && from > xs[0] ? from : xs[0], b = to && to < xs[xs.length - 1] ? to : xs[xs.length - 1];
    const layout = { "legend.title.text": a && b ? "<b>" + fmtDay(a) + " → " + fmtDay(b) + "</b>" : "" };
    if (from || to) {  // fit the vertical axis and the date labels to the visible interval
      let lo = Infinity, hi = -Infinity;
      traces.forEach((t, k) => t.x.forEach((x, i) => { const d = day(x); const v = ys[k][i]; if (v != null && (!from || d >= from) && (!to || d <= to)) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }));
      if (isFinite(lo)) { const pad = (hi - lo) * 0.06 || 1; layout["yaxis.range"] = [lo - pad, hi + pad]; }
      const years = (Date.parse(b) - Date.parse(a)) / 3.15e10;
      layout["xaxis.tickformat"] = years > 3 ? "%Y" : years > 0.4 ? "%m/%y" : "%d/%m";
    } else {
      layout["yaxis.autorange"] = true;
    }
    const style = { name: names, legendrank: rank, y: ys };
    if (pick) {  // the leaders of this interval in colour, the rest in grey (the reference lines as they are)
      const best = new Set(order.filter(([r, i]) => i < pick.agents && r > -Infinity).slice(0, pick.n).map(([, i]) => i));
      const on = traces.map((t, i) => i < pick.agents && (best.has(i) || pick.always.has(t.meta)));
      style["line.color"] = traces.map((t, i) => (i < pick.agents ? (on[i] ? pick.own[i] : "#6d6f78") : t.line.color));
      style["line.width"] = traces.map((t, i) => (i < pick.agents ? (on[i] ? 2 : 1) : t.line.width));
      style.opacity = traces.map((t, i) => (i < pick.agents ? (on[i] ? 1 : 0.55) : 1));
    }
    busy = true;
    Plotly.update(el, style, layout)
      .finally(() => { busy = false; });
  };
  apply(null, null);
  el.on("plotly_relayout", (ev) => {
    if (busy || !ev) return;
    if (ev["xaxis.autorange"]) return apply(null, null);
    const r = ev["xaxis.range"] || (ev["xaxis.range[0]"] !== undefined ? [ev["xaxis.range[0]"], ev["xaxis.range[1]"]] : null);
    if (r) apply(day(r[0]), day(r[1]));
  });
}
// The league's curves end at the last close; each live agent's return of this moment (the round's prices) is added as
// one more point, so the chart, the Desempenho table and the sheets all tell the same result and the same order.
(function liveNow() {
  const live = Object.fromEntries((DATA.agents || []).filter((a) => !a.backtest && a.return_pct != null).map((a) => [a.name, a.return_pct]));
  const now = String(DATA.updated || "");
  (DATA.series || []).forEach((s) => {
    if (!(s.name in live) || !now || !s.x.length || now.slice(0, 16) <= String(s.x[s.x.length - 1]).slice(0, 16)) return;
    s.x = s.x.concat([now]); s.y = s.y.concat([live[s.name]]);
  });
})();
lineChart("chart", DATA);

// ---- agents ----
const agents = DATA.agents || [];
if (!agents.length) $("agents-section").style.display = "none";
const byName = Object.fromEntries(agents.map((a) => [a.name, a]));
const headline = (a) => a.backtest ? a.backtest.return_pct : a.return_pct;
// Agent picker: the agents in a column (expandable groups, best first, a search box) on the left of the sheet.
function agentPicker(listId, items, ret, pick, groupOf) {
  const box = $(listId);
  const groups = [], byGroup = {};
  items.forEach((a, i) => { const g = groupOf(a); if (!byGroup[g]) { byGroup[g] = []; groups.push(g); } byGroup[g].push(i); });
  box.innerHTML = '<input type="search" class="agent-find" placeholder="🔎 Procurar agente…" aria-label="Procurar agente">' +
    groups.map((g, k) => '<details class="agent-group"' + (k === 0 ? " open" : "") + '><summary>' + esc(g) + ' <span class="muted small">(' + byGroup[g].length + ')</span></summary>' +
      byGroup[g].slice().sort((x, y) => (ret(items[y]) ?? -1e9) - (ret(items[x]) ?? -1e9)).map((i) => {
        const a = items[i];
        return '<button class="agent-item" data-i="' + i + '"><span>' + esc(a.emoji + " " + a.name) + '</span><span class="' + cls(ret(a)) + '">' + fmtPct(ret(a)) + '</span></button>';
      }).join("") + '</details>').join("");
  box.addEventListener("click", (e) => { const b = e.target.closest(".agent-item"); if (b) pick(+b.dataset.i); });
  const find = box.querySelector(".agent-find");
  find.addEventListener("input", () => {
    const q = find.value.trim().toLowerCase();
    box.querySelectorAll(".agent-group").forEach((g) => {
      let n = 0;
      g.querySelectorAll(".agent-item").forEach((b) => { const hit = !q || b.textContent.toLowerCase().includes(q); b.hidden = !hit; n += hit; });
      g.hidden = !n;
      if (q && n) g.open = true;
    });
  });
  return (i) => {  // mark the chosen one (and open its group)
    box.querySelectorAll(".agent-item").forEach((b) => b.classList.toggle("on", +b.dataset.i === i));
    const b = box.querySelector('.agent-item[data-i="' + i + '"]');
    if (b) b.closest("details").open = true;
  };
}
const agentGroup = (a) => a.modo === "diario" || / · diário$/.test(a.name) ? "📅 Diários (decidem às 07:30)"
  : a.perfil === "conservador" ? "🛡️ Perfil conservador" : a.perfil === "arrojado" ? "🔥 Perfil arrojado" : "⚡ Reativos · perfil equilibrado";
const markAgent = agentPicker("chips", agents, headline, (i) => show(i, true), agentGroup);

// A pre-training run as "45ª · 03/11" (💻 on the PC; 🛡️/🔥 the conservative or aggressive profile's run).
const fmtRun = (r, i) => (i + 1) + "ª · " + new Date(r.at).toLocaleDateString("pt-PT", { day: "2-digit", month: "2-digit" }) +
  (r.source === "local" ? " 💻" : "") + ({ conservador: " 🛡️", arrojado: " 🔥" }[r.perfil] || "");
// One agent's changes between its pre-training runs (EVOLUTION.changes), newest first, as a chapter of its sheet.
function changesHtml(a) {
  const E = typeof EVOLUTION === "undefined" ? null : EVOLUTION;
  if (!E || !a.id) return "";
  const base = a.id.replace(/-(conservador|arrojado)$/, ""), profile = a.perfil || "equilibrado";
  const key = profile === "equilibrado" || base === "indexador" ? base : base + "-" + profile;
  const list = ((E.changes || {})[key] || []).slice().reverse();
  const code = (ks) => ks.map((k) => '<code>' + esc(k) + '</code>').join(" ") || "—";
  return '<h3>🔧 Histórico de mudanças (' + list.length + ')</h3>' + (list.length
    ? table(["Execução", "Ligou", "Desligou", "Ajustou"], list.map((c) => [esc(fmtRun(E.runs[c.run], c.run)), code(c.on), code(c.off), code(c.moved)]))
    : '<p class="muted">Sem mudanças registadas entre execuções.</p>');
}

// The sheet in expandable chapters (each <h3> and what follows it). The open ones are remembered as the person opens
// and closes them, for every agent and the next visits.
const CHAP_KEY = "agent-chapters";
const OPEN_CHAPS = (() => { try { const v = JSON.parse(localStorage.getItem(CHAP_KEY)); if (Array.isArray(v)) return new Set(v); } catch (e) {} return new Set(["💼 Carteira atual", "⏳ Ordens para a próxima abertura", "📝 Exame"]); })();
const chapKey = (t) => t.split(":")[0].replace(/\(.*?\)/g, "").replace(/\d+/g, "").trim();
function chapters(panel) {
  let cur = null;
  [...panel.children].forEach((el) => {
    if (el.tagName === "H3") {
      const d = document.createElement("details"), sum = document.createElement("summary"), key = chapKey(el.textContent);
      d.className = "chap"; sum.innerHTML = el.innerHTML; d.appendChild(sum); d.open = OPEN_CHAPS.has(key);
      d.addEventListener("toggle", () => {
        if (d.open) OPEN_CHAPS.add(key); else OPEN_CHAPS.delete(key);
        try { localStorage.setItem(CHAP_KEY, JSON.stringify([...OPEN_CHAPS])); } catch (e) {}
      });
      el.replaceWith(d); cur = d;
    } else if (cur) cur.appendChild(el);
  });
  const all = panel.querySelectorAll("details.chap");
  if (!all.length) return;
  const tools = document.createElement("div");
  tools.className = "chap-tools";
  tools.innerHTML = '<button class="chip" data-chap="1">Abrir tudo</button><button class="chip" data-chap="0">Fechar tudo</button>';
  tools.addEventListener("click", (e) => { const b = e.target.closest("[data-chap]"); if (b) all.forEach((d) => { d.open = b.dataset.chap === "1"; }); });
  const title = panel.querySelector("h2"), head = document.createElement("div");  // the name and the buttons on one line
  head.className = "sheet-head";
  title.before(head);
  head.append(title, tools);
}
if (window.Plotly) $("chart").on("plotly_click", (ev) => { const a = byName[ev.points[0].data.meta || ev.points[0].data.name]; if (a) show(agents.indexOf(a), true); });

const GLOSS = {
  "Stage 2": "Fase de tendência de alta: preço acima da média de 50 dias, esta acima da de 200, e a de 200 a subir.",
  "VCP": "Contração de volatilidade (Minervini): a ação sobe, corrige cada vez menos e fica apertada perto de um máximo (o pivot).",
  "Pivot": "O preço que, se for ultrapassado com volume, confirma o rompimento de um padrão.",
  "Stop": "Preço de saída de proteção: se a ação lá chegar, vende-se para limitar a perda.",
  "Alvo": "Preço de saída com lucro definido na compra.",
  "R": "Uma unidade de risco: a distância entre a entrada e o stop. Ganhar 2R é ganhar o dobro do que se arriscou.",
  "Risco/retorno": "Quanto se pode ganhar até ao alvo por cada euro arriscado até ao stop.",
  "ATR": "Variação diária média do preço (Average True Range, 14 sessões): mede quanto a ação costuma mexer num dia.",
  "Sharpe": "Retorno por unidade de risco (volatilidade), anualizado. Acima de 1 é bom; o S&P 500 anda perto de 0,6-0,8 a longo prazo.",
  "Queda máxima": "A maior descida desde um máximo do património até ao fundo seguinte (drawdown).",
  "Score de segurança": "0-100: tendência, volatilidade, distância ao máximo, liquidez, qualidade, resultados, notícias e mercado. Mede quão ordenada e suportada está a ação, não quanto vai subir.",
  "Score técnico": "0-60: a parte técnica do score de segurança (tendência, volatilidade e distância ao máximo).",
  "Score de tendência": "0-100: 70 pela estrutura da tendência e 30 pela força relativa face às outras ações.",
  "Zona de mercado": "Leitura da breadth: Alta, Neutra, Cautelosa ou Queda, pela % de ações do S&P 500 em Stage 2.",
  "Breadth": "Amplitude do mercado: quantas ações estão a subir. Um mercado que sobe só com poucas ações é frágil.",
  "Monte Carlo": "Milhares de anos alternativos montados com blocos aleatórios dos retornos da própria estratégia, para ver o pior caso plausível.",
  "Exame": "O último ano do pré-treino, que a estratégia não viu ao escolher os parâmetros: é o teste justo.",
  "Fantasma": "Cópia em papel de um agente com parâmetros ligeiramente diferentes; se for claramente melhor durante 20+ sessões, passa a ser o agente.",
  "Peso": "Quanto vale o voto de um agente nas propostas, pelo seu historial (exame, zona de mercado, resultados ao vivo, risco de falência).",
  "Confiança": "A parte do peso de todos os agentes que está a propor esta compra.",
  "VIX": "Índice do medo: a volatilidade que o mercado de opções espera para o S&P 500. Acima de 30 é stress.",
  "Incerteza política": "Índice de incerteza da política económica dos EUA (EPU), baseado nas notícias, em percentil dos últimos 3 anos.",
  "Spread de crédito": "Juro extra que as empresas de maior risco pagam: sobe em stress financeiro.",
  "Crédito de alto risco": "Quanto as obrigações de empresas de maior risco caíram face às do Tesouro dos EUA desde o melhor nível do último ano. Sobe em stress financeiro.",
  "UCITS": "ETFs domiciliados na Europa, que um investidor em Portugal pode comprar.",
  "Rompimento": "Quando o preço ultrapassa um máximo recente com volume forte.",
  "Mais-valias": "Ganho realizado numa venda; em Portugal paga 28% de imposto.",
  "Rentabilidade": "Ganho ou perda face ao dinheiro investido, em %.",
};
const tile = (k, v, s = "", c = "") => '<div class="tile"><div class="k">' + (GLOSS[k] ? '<abbr class="g" title="' + esc(GLOSS[k]) + '">' + k + '</abbr>' : k) + '</div><div class="v ' + c + '">' + v + '</div>' + (s ? '<div class="s">' + s + '</div>' : "") + '</div>';
const table = (head, rows, numeric = []) => rows.length
  ? '<div class="scroll"><table><tr>' + head.map((h, i) => '<th class="' + (numeric.includes(i) ? "num" : "") + '">' + h + '</th>').join("") + '</tr>' +
    rows.map((r) => '<tr>' + r.map((c, i) => '<td class="' + (numeric.includes(i) ? "num" : "") + '">' + c + '</td>').join("") + '</tr>').join("") + '</table></div>'
  : '<p class="muted">Nada.</p>';

// a fusion's family tree (scripts/jury.py): the members it came from, their parameters and where these came from, and
// the jury's analysis that justified it
function genealogyHtml(a) {
  const g = a.genealogia;
  if (!g || !(g.membros || []).length) return "";
  const jur = { rendimento: "🎯", risco: "🛡️", robustez: "🧪", eficiencia: "💶", diversificacao: "🧩", regimes: "🌦️" };
  const params = (p) => Object.entries(p || {}).filter(([, v]) => typeof v !== "object" && v !== 0 && v !== false)
    .map(([k, v]) => k + " " + v).join(", ");
  const an = g.analise || {}, bt = an.backtest;
  return '<h3>🌳 Árvore genealógica</h3><div class="prose"><p>' + esc(a.name) + ' nasceu a ' + esc(g.data) +
    ' do júri de domingo: junta as carteiras do top 5 de então, com pesos ' + esc(g.pesos || "") + '. Os membros e os pesos ficam fixos.</p></div>' +
    '<ul class="tree"><li><b>' + esc(a.emoji + " " + a.name) + '</b> · parâmetros: ' + esc(params(g.parametros)) + '<ul>' +
    g.membros.map((m) => '<li><details><summary><b>' + m.lugar + '.º ' + esc(m.emoji + " " + m.name) + '</b> · ' +
      fmtNum((m.peso || 0) * 100, 0) + '% · nota ' + fmtNum(m.nota, 0) + ' · ' + esc(m.strategy_label || m.strategy) + ' · perfil ' + esc(m.perfil) +
      (m.linhagem ? ' · descende de ' + esc(m.linhagem) : "") + '</summary>' +
      '<p class="small">Notas dos jurados: ' + Object.entries(m.notas || {}).map(([j, v]) => (jur[j] || j) + " " + fmtNum(v, 0)).join(" · ") + '</p>' +
      (Object.keys(m.ajustes_claude || {}).length ? '<p class="small">🤖 Ajustes do Claude: ' + Object.entries(m.ajustes_claude).map(([j, x]) =>
        (jur[j] || j) + " " + (x.ajuste > 0 ? "+" : "") + x.ajuste + " (" + esc(x.porque) + ")").join("; ") + '</p>' : "") +
      '<p class="small"><b>Parâmetros com que entrou:</b> <code>' + esc(params(m.parametros)) + '</code></p>' +
      ((m.origem_parametros || []).length ? '<p class="small"><b>De onde vêm:</b></p>' + table(["Data", "O que mudou"],
        m.origem_parametros.map((e) => [esc(e.date), esc(e.note)])) : '<p class="small muted">Parâmetros originais da estratégia (sem mudanças registadas).</p>') +
      '</details></li>').join("") + '</ul></li></ul>' +
    '<h3>⚖️ A análise que justificou a criação</h3><div class="prose">' +
    (an.parecer ? '<p>🧑‍⚖️ ' + md(an.parecer) + '</p>' : '<p class="muted">Sem parecer do presidente nesse dia.</p>') +
    (bt ? '<p>📼 <b>Backtest de 20 anos da fusão:</b> veredito ' + fmtNum(bt.overall, 0) + ' · exame ' + fmtNum(bt.exam, 0) + ' · ' +
      bt.years_beating_index + '/' + bt.years_total + ' anos acima do S&P 500 · ' + fmtPct((bt.return_after_tax || 0) * 100, 0) +
      ' depois de impostos (S&P 500 ' + fmtPct((an.indice_depois_impostos || 0) * 100, 0) + ') · queda máxima ' + fmtPct(-(bt.max_drawdown || 0) * 100, 0) + '</p>' : "") +
    (an.jurados ? '<p class="small">Pesos dos jurados: ' + Object.entries(an.jurados).map(([j, w]) => esc(j) + " " + fmtNum(w * 100, 0) + "%").join(" · ") + '</p>' : "") +
    ((an.ranking || []).length ? '<p class="small">Ranking do júri nesse dia: ' + an.ranking.map((r) => r.lugar + ".º " + esc(r.name) + " (" + fmtNum(r.nota, 0) + ")").join(" · ") + '</p>' : "") +
    '</div>';
}

function strategyHtml(s) {
  return '<h3>🧭 Estratégia: ' + esc(s.label) + '</h3><div class="prose">' +
    (s.description ? '<p>' + md(s.description) + '</p>' : "") +
    '<p>💡 ' + md(s.idea) + '</p><p>🟢 <b>Quando compra:</b> ' + md(s.buys) + '</p><p>🔴 <b>Quando vende:</b> ' + md(s.sells) +
    '</p><p>⚖️ <b>Tamanho das posições:</b> ' + md(s.size) + '</p><p>🛡️ <b>Regras comuns:</b><br>' + md(s.rules) + '</p></div>' +
    '<h3>⚙️ Parâmetros</h3>' + table(["Parâmetro", "Valor", "O que faz"], (s.params || []).map((p) => ['<code>' + esc(p.key) + '</code>', esc(p.value), esc(p.meaning)])) +
    (s.pretrain ? '<h3>🧠 Treino</h3><div class="prose"><p>' + md(s.pretrain) + '</p></div>' : "");
}

function liveHtml(a) {
  const realized = Object.entries(a.realized || {});
  return '<div class="grid">' +
    tile("Património", fmtEur(a.equity), "depositado " + fmtEur(a.deposits)) +
    tile("Rentabilidade", fmtPct(a.return_pct), "hoje " + fmtPct(a.day_pct), cls(a.return_pct)) +
    tile("Pontos", fmtNum(a.points, 0), "geração " + a.generation + (a.lineage ? " · filho de " + esc(a.lineage) : "")) +
    tile("Queda máxima", fmtPct(-a.max_dd_pct), "desde o máximo do património", a.max_dd_pct ? "down" : "") +
    tile("Sharpe", fmtNum(a.sharpe), a.sessions + " sessões desde " + esc(a.started)) +
    tile("Dinheiro", fmtEur(a.cash), fmtNum(a.invested_pct, 0) + "% investido") +
    tile("Operações", a.buys + " compras · " + a.sells + " vendas", a.sells ? a.wins + " vendas com lucro (" + fmtNum(a.wins / a.sells * 100, 0) + "%)" : "ainda sem vendas") +
    tile("Impostos pagos", fmtEur(a.taxes), esc(a.fees_note)) +
    tile("Dividendos", fmtEur((a.dividendos || {}).liquido_eur || 0), (a.dividendos || {}).bruto_eur
      ? fmtEur(a.dividendos.bruto_eur) + " brutos · " + fmtEur(a.dividendos.imposto_eur) + " de imposto (28%) · " +
        (a.dividendos.ultimos || []).length + " pagamentos"
      : "recebidos desde 5/11/2026, líquidos de imposto") + '</div>' +
    '<h3>💼 Carteira atual (' + a.positions.length + ' posições ativas)</h3>' +
    table(["Ativo", "Porque comprou", "Nome", "Qtd.", "Custo médio", "Preço atual", "Investido", "Valor", "Ganho", "Ganho %", "Peso", "Stop", "Alvo", "Desde", "Resultados"],
      a.positions.map((p) => ['<b>' + esc(p.symbol) + '</b>', p.why ? '<details><summary>' + esc(p.reason || "ver") + '</summary><p class="small why">' + esc(p.why) + '</p></details>' : esc(p.reason || "—"),
        esc(p.name || p.sector || ""), fmtNum(p.qty, 4),
        fmtNum(p.entry) + " " + p.currency, (p.price == null ? "—" : fmtNum(p.price) + " " + p.currency), fmtEur(p.cost_eur), fmtEur(p.value_eur),
        '<span class="' + cls(p.pnl_eur) + '">' + fmtEur(p.pnl_eur) + '</span>', '<span class="' + cls(p.pnl_pct) + '">' + fmtPct(p.pnl_pct) + '</span>',
        fmtNum(p.weight, 1) + "%", p.stop ? fmtNum(p.stop) : "—", p.tp ? fmtNum(p.tp) : "—", esc(p.since || p.entry_date || ""), esc(p.earnings || "—")]),
      [3, 4, 5, 6, 7, 8, 9, 10, 11, 12]) +
    '<p class="muted small">Custo médio e preço na moeda do ativo, com custos incluídos; valores em euros ao câmbio do dia.</p>' +
    '<h3>⏳ Ordens para a próxima abertura</h3>' +
    table(["Tipo", "Ativo", "Valor", "Stop", "Alvo", "Motivo", "Números na decisão", "Criada"],
      (a.pending || []).map((o) => [o.side === "buy" ? "🟢 compra" : "🔴 venda", '<b>' + esc(o.symbol) + '</b>', o.eur ? fmtEur(o.eur) : (o.fraction ? fmtNum(o.fraction * 100, 0) + "% da posição" : "tudo"),
        o.stop ? fmtNum(o.stop) : "—", o.tp ? fmtNum(o.tp) : "—", esc(o.reason || ""), '<span class="small" style="white-space:normal">' + esc((o.why || "").replace(/^Na altura: /, "")) + '</span>', esc(o.created || "")]), [2, 3, 4]) +
    '<h3>📚 Histórico de posições (' + (a.history || []).length + ')</h3><p class="muted small">Cada posição da primeira compra à última venda, agrupadas pelo dia em que abriram. Filtra pelo que procuras; toca numa posição para ver as compras, as vendas e a justificação de cada uma segundo a estratégia.</p>' +
    '<div class="lots-box"></div>' +
    '<details><summary>Todas as operações em lista (' + a.trades.length + ')</summary>' +
    table(["Data", "Tipo", "Ativo", "Qtd.", "Preço", "Valor", "Resultado", "Motivo"],
      a.trades.map((t) => [esc(t.date + (t.hora ? " " + t.hora : "")), t.side === "compra" ? "🟢 compra" : t.side === "venda" ? "🔴 venda" : "🧾 " + esc(t.side), '<b>' + esc(t.symbol) + '</b>',
        t.qty ? fmtNum(t.qty, 4) : "—", t.price ? fmtNum(t.price) : "—", fmtEur(t.eur),
        t.side === "compra" ? "—" : '<span class="' + cls(t.pnl_eur) + '">' + fmtEur(t.pnl_eur) + '</span>', esc(t.reason || "")]), [3, 4, 5, 6]) + '</details>' +
    (realized.length ? '<h3>🧾 Mais-valias realizadas por ano</h3>' + table(["Ano", "Mais-valias"], realized.map(([y, v]) => [esc(y), '<span class="' + cls(v) + '">' + fmtEur(v) + '</span>']), [1]) : "") +
    ((a.evolution || []).length ? '<h3>🧬 Treinos e mutações</h3>' + table(["Data", "O que mudou"], a.evolution.map((e) => [esc(e.date), esc(e.note)])) : "") +
    ((a.shadows || []).length ? '<h3>👻 Cópias-fantasma a testar</h3><p class="muted small">Cópias em papel com parâmetros ligeiramente diferentes. Ao fim de 20 sessões, uma que faça 2 pontos percentuais mais do que o agente (e mais pontos) passa a ser o agente.</p>' +
      table(["Alterações", "Desde", "Sessões", "Cópia", "Agente no mesmo período"], a.shadows.map((x) => [esc(x.changes.join(", ")), esc(x.born), x.sessions,
        '<span class="' + cls(x.ret_pct) + '">' + fmtPct(x.ret_pct) + '</span>', '<span class="' + cls(x.parent_ret_pct) + '">' + fmtPct(x.parent_ret_pct) + '</span>']), [2, 3, 4]) : "") +
    (Object.keys(a.por_zona || {}).length ? '<h3>🧭 Parâmetros por zona de mercado</h3>' + table(["Zona", "Muda"], Object.entries(a.por_zona).map(([z, d]) =>
      [esc(z), esc(Object.entries(d).map(([k, v]) => k + " " + v).join(", "))])) : "");
}

function backtestHtml(a) {
  const b = a.backtest, mc = b.monte_carlo || {};
  const exam = Object.entries(b.exam || {});
  // Parameters: how many changed (older reports have no "default"), the full list folded away, the changed ones in bold.
  const params = Object.entries(b.chosen || {}).filter(([, v]) => v === null || typeof v !== "object");
  const before = b.default && Object.keys(b.default).length ? b.default : null;
  const isNew = ([k, v]) => before && before[k] !== v;
  const changed = before ? params.filter(isNew).length : null;
  const paramsTile = tile("Parâmetros", b.adopted ? (changed != null ? changed + (changed === 1 ? " novo adotado" : " novos adotados") : "novos adotados") : "mantidos",
    params.length ? '<details class="params"><summary>ver os ' + params.length + '</summary>' +
      params.map((p) => (isNew(p) ? "<b>" + esc(p[0]) + " " + esc(p[1]) + "</b>" : esc(p[0]) + " " + esc(p[1]))).join(" · ") + '</details>' : "",
    b.adopted ? "adopted" : "");
  return '<div class="grid">' +
    tile("Rentabilidade no período", fmtPct(b.return_pct, 0), b.bankrupt ? "💀 faliu em " + esc(b.bankrupt) : "sem falência", cls(b.return_pct)) +
    tile("Queda máxima", fmtPct(-b.max_dd_pct, 0), "", "down") +
    tile("Sharpe", fmtNum(b.sharpe), (b.trades ?? "—") + (b.trades === 1 ? " operação" : " operações")) +
    paramsTile + '</div>' +
    (exam.length ? '<h3>📝 Exame (último ano, fora do treino)</h3>' + table(["Parâmetros", "Pontos", "Rentabilidade", "Queda máx.", "Sharpe", "Operações"],
      exam.map(([k, x]) => [k === "best" ? "escolhidos" : "originais", fmtNum(x.points, 0), fmtPct(x.return_pct), fmtPct(-x.max_dd_pct), fmtNum(x.sharpe), x.trades ?? "—"]), [1, 2, 3, 4, 5]) : "") +
    (Object.keys(b.crises || {}).length ? '<h3>💥 Crises</h3>' + table(["Crise", "Rentabilidade", "Queda máx.", "Falência"],
      Object.entries(b.crises).map(([k, c]) => [esc(k), '<span class="' + cls(c.return_pct) + '">' + fmtPct(c.return_pct) + '</span>', fmtPct(-c.max_dd_pct), c.bankrupt ? "💀 " + esc(c.bankrupt) : "—"]), [1, 2]) : "") +
    (mc.median != null ? '<h3>🎲 Monte Carlo (anos alternativos)</h3><div class="grid">' +
      tile("Ano mediano", fmtPct(mc.median * 100)) + tile("Pior 5%", fmtPct(mc.p5 * 100), "", "down") +
      tile("Probabilidade de perder", fmtNum(mc.p_loss * 100, 0) + "%") + tile("Queda de 30% ou mais", fmtNum((mc.p_dd30 || 0) * 100, 0) + "%") +
      tile("Falência", fmtNum(mc.p_ruin * 100, 0) + "%") + '</div>' : "") +
    ((b.greedy || []).length ? '<h3>🎛️ Fase final: regras ligadas uma a uma</h3><p class="muted small">Cada regra desligada foi testada em três níveis; fica ligada só se acrescentar 2% aos pontos de treino sem piorar o exame nem as crises.</p>' +
      table(["Regra", "Nível", "Treino", "Exame", "Ficou?"], b.greedy.map((t) => ['<code>' + esc(t.param) + '</code>', esc(t.value),
        '<span class="' + cls(t.train_gain) + '">' + (t.train_gain > 0 ? "+" : "") + fmtNum(t.train_gain, 0) + '</span>',
        t.exam_gain == null ? "—" : '<span class="' + cls(t.exam_gain) + '">' + (t.exam_gain > 0 ? "+" : "") + fmtNum(t.exam_gain, 0) + '</span>', t.kept ? "✅" : "—"]), [2, 3]) : "") +
    (b.review && b.review.parecer ? '<h3>🤖 Revisão do Claude' + (b.review.date ? ' <span class="muted small">' + esc(b.review.date) + '</span>' : '') + '</h3><p>' + esc(b.review.parecer) + '</p>' +
      ((b.review.fortes || []).length ? '<p><b>Pontos fortes:</b> ' + b.review.fortes.map(esc).join(" · ") + '</p>' : "") +
      ((b.review.fracos || []).length ? '<p><b>Pontos fracos:</b> ' + b.review.fracos.map(esc).join(" · ") + '</p>' : "") +
      (b.review.manter === false ? '<p class="down">⚠️ O Claude sugere repensar esta estratégia.</p>' : "") +
      ((b.review.regras_novas || []).length ? '<p><b>Regras novas sugeridas:</b> ' + b.review.regras_novas.map(esc).join(" · ") + '</p>' : "") : "") +
    ((b.claude || []).length ? '<h3>🧪 Propostas do Claude no treino</h3><p class="muted small">O Claude propõe; o backtest decide. Cada proposta entrou como candidato e passou pelos mesmos testes: bater os originais em 65% dos anos, robustez, crises e exame.</p>' +
      table(["Alterações", "Hipótese", "Anos ganhos", "Pontuação", "Resultado"], b.claude.map((c) => [
        Object.entries(c.changes || {}).map(([k, v]) => '<code>' + esc(k) + ' ' + esc(v) + '</code>').join(" "), esc(c.hipotese || ""),
        esc(c.beats_years) + "/" + esc(c.years), '<span class="' + cls(c.score - c.score_original) + '">' + fmtNum(c.score, 0) + '</span> vs ' + fmtNum(c.score_original, 0),
        c.adopted ? "✅ adotada" : c.picked ? "🟡 escolhida, falhou o exame" : c.consistent ? "🟡 consistente, não foi a melhor" : "❌ falhou os anos"]), [2, 3]) : "") +
    (Object.keys(b.importance || {}).length ? '<h3>🔬 O que cada parâmetro vale</h3><p class="muted small">Cada parâmetro ligado/desligado com o resto igual: pontos que acrescenta (ou acrescentaria, se estiver desligado) nos anos de treino e no exame.</p>' +
      table(["Parâmetro", "Estado", "Valor", "Treino", "Exame"], Object.entries(b.importance).sort((x, y) => y[1].train - x[1].train).map(([k, v]) =>
        ['<code>' + esc(k) + '</code>', v.used ? "em uso" : "testado (desligado)", esc(v.value), '<span class="' + cls(v.train) + '">' + (v.train > 0 ? "+" : "") + fmtNum(v.train, 0) + '</span>',
         '<span class="' + cls(v.exam) + '">' + (v.exam > 0 ? "+" : "") + fmtNum(v.exam, 0) + '</span>']), [3, 4]) : "") +
    ((b.years || []).length ? '<h3>📅 Pontos da liga em cada ano de treino</h3><p>' + b.years.map((p, i) => '<span class="tag ' + cls(p) + '">' +
      (b.train_start ? (b.train_start + i) + "/" + String((b.train_start + i + 1) % 100).padStart(2, "0") : (i + 1) + "º") + ' · ' + fmtNum(p, 0) + '</span>').join("") +
      '</p><p class="muted small">Pontos face ao S&P 500 com penalização das quedas; cada ano conta de outubro a outubro.</p>' : "");
}

let SHOWN = null;  // the agent on the sheet (kept across the portal's refreshes)
// ---- an agent's position history: filters, a summary of what is filtered, grouped by the day they opened ----
const LOT_PAGE = 20;
let LOTF = { estado: "todas", res: "todos", motivo: "", periodo: "tudo", q: "", n: LOT_PAGE };
const lotExit = (l) => l.status === "aberta" ? "" : ((l.sells[l.sells.length - 1] || {}).reason || "—");
function lotHtml(l) {
  const icon = l.status === "aberta" ? "🟡" : l.result > 0 ? "✅" : "🔴";
  const last = lotExit(l), hm = (x) => (x || "").slice(11) || "";
  const span = (l.opened_at || l.opened) + (l.status === "aberta" ? " → aberta" : " → " + (l.closed === l.opened ? hm(l.closed_at) || l.closed : (l.closed_at || l.closed)));
  return '<details class="lot"><summary><span class="lot-line">' + icon + ' <b>' + esc(l.symbol) + '</b><span class="muted small">' + esc(span.replace(l.opened + " ", "")) + '</span>' +
    (l.status === "fechada" ? '<span class="' + cls(l.result) + '">' + fmtEur(l.result) + (l.result_pct != null ? " (" + fmtPct(l.result_pct) + ")" : "") + '</span>' +
      '<span class="muted small">' + (l.days ? l.days + (l.days === 1 ? " dia" : " dias") + " · " : "") + esc(last) + '</span>' : '<span class="muted small">' + fmtEur(l.invested) + ' investidos</span>') +
    '</span></summary>' + l.buys.concat(l.sells).sort((x, y) => (x.date + (x.hora || "")).localeCompare(y.date + (y.hora || ""))).map((t) =>
      '<div class="trade ' + (t.side === "compra" ? "b" : "s") + '"><b>' + (t.side === "compra" ? "🟢 Compra" : "🔴 Venda") + '</b> · ' + esc(t.date + (t.hora ? " " + t.hora : "")) + ' · ' + fmtNum(t.qty, 4) + ' a ' + fmtNum(t.price) +
      ' · ' + fmtEur(t.eur) + (t.side === "venda" ? ' · <span class="' + cls(t.pnl_eur) + '">' + fmtEur(t.pnl_eur) + '</span>' + (t.held != null ? ' em ' + t.held + ' dias' : '') : '') +
      '<p class="small why">' + esc(t.why || t.reason || "") + '</p></div>').join("") + '</details>';
}
function drawLots(a, box) {
  const all = a.history || [];
  if (!all.length) { box.innerHTML = '<p class="muted">Ainda sem operações.</p>'; return; }
  const today = new Date().toISOString().slice(0, 10);
  const since = { hoje: today, "7d": new Date(Date.now() - 7 * 864e5).toISOString().slice(0, 10), "30d": new Date(Date.now() - 30 * 864e5).toISOString().slice(0, 10),
    "90d": new Date(Date.now() - 90 * 864e5).toISOString().slice(0, 10) }[LOTF.periodo];
  const motivos = [...new Set(all.map(lotExit).filter(Boolean))].sort();
  if (LOTF.motivo && !motivos.includes(LOTF.motivo)) LOTF.motivo = "";
  const q = LOTF.q.trim().toUpperCase();
  const shown = all.filter((l) => (LOTF.estado === "todas" || (LOTF.estado === "abertas") === (l.status === "aberta")) &&
    (LOTF.res === "todos" || (l.status === "fechada" && (LOTF.res === "ganho" ? l.result > 0 : l.result <= 0))) &&
    (!LOTF.motivo || lotExit(l) === LOTF.motivo) && (!since || (l.closed || l.opened) >= since) && (!q || l.symbol.toUpperCase().includes(q)));
  const closed = shown.filter((l) => l.status === "fechada"), wins = closed.filter((l) => l.result > 0);
  const total = closed.reduce((s, l) => s + l.result, 0), inv = closed.reduce((s, l) => s + l.invested, 0);
  const chip = (k, v, label) => '<button type="button" class="chip' + (LOTF[k] === v ? " on" : "") + '" data-f="' + k + '" data-v="' + v + '">' + label + '</button>';
  const days = {};
  shown.slice(0, LOTF.n).forEach((l) => (days[l.opened] = days[l.opened] || []).push(l));
  const dayName = (d) => d === today ? "Hoje" : d === new Date(Date.now() - 864e5).toISOString().slice(0, 10) ? "Ontem"
    : new Date(d + "T12:00:00Z").toLocaleDateString("pt-PT", { weekday: "short", day: "2-digit", month: "2-digit", year: d.slice(0, 4) === today.slice(0, 4) ? undefined : "numeric" });
  box.innerHTML = '<div class="lot-filters">' +
    '<div class="chips">' + chip("estado", "todas", "Todas") + chip("estado", "abertas", "🟡 Abertas") + chip("estado", "fechadas", "Fechadas") + '</div>' +
    '<div class="chips">' + chip("res", "todos", "Qualquer resultado") + chip("res", "ganho", "✅ Ganho") + chip("res", "perda", "🔴 Perda") + '</div>' +
    '<div class="chips">' + chip("periodo", "tudo", "Tudo") + chip("periodo", "hoje", "Hoje") + chip("periodo", "7d", "7 dias") + chip("periodo", "30d", "30 dias") + chip("periodo", "90d", "90 dias") + '</div>' +
    '<div class="filters"><select data-f="motivo"><option value="">Qualquer motivo de saída</option>' + motivos.map((m) => '<option' + (m === LOTF.motivo ? " selected" : "") + '>' + esc(m) + '</option>').join("") + '</select>' +
    '<input type="search" data-f="q" placeholder="Ticker, ex.: NVDA" value="' + esc(LOTF.q) + '"></div></div>' +
    '<p class="small lot-sum"><b>' + shown.length + '</b> de ' + all.length + ' posições' + (closed.length ? ' · ' + wins.length + ' com ganho, ' + (closed.length - wins.length) + ' com perda (' +
      fmtNum(wins.length / closed.length * 100, 0) + '% de acerto) · resultado <span class="' + cls(total) + '">' + fmtEur(total) + (inv ? ' (' + fmtPct(total / inv * 100) + ')' : '') + '</span>' : '') +
      (shown.length - closed.length ? ' · ' + (shown.length - closed.length) + ' aberta' + (shown.length - closed.length === 1 ? '' : 's') : '') + '</p>' +
    (shown.length ? Object.entries(days).map(([d, ls]) => {
      const t = ls.filter((l) => l.status === "fechada").reduce((s, l) => s + l.result, 0);
      return '<div class="lot-day"><div class="lot-day-head"><b>' + esc(dayName(d)) + '</b> <span class="muted small">' + (/^(Hoje|Ontem)$/.test(dayName(d)) ? d + ' · ' : '') + ls.length + (ls.length === 1 ? ' posição' : ' posições') + '</span>' +
        (ls.some((l) => l.status === "fechada") ? ' <span class="small ' + cls(t) + '">' + fmtEur(t) + '</span>' : '') + '</div>' + ls.map(lotHtml).join("") + '</div>';
    }).join("") : '<p class="muted">Nenhuma posição com estes filtros.</p>') +
    (shown.length > LOTF.n ? '<p><button type="button" class="chip" data-more="1">Mostrar mais ' + Math.min(LOT_PAGE, shown.length - LOTF.n) + ' (faltam ' + (shown.length - LOTF.n) + ')</button></p>' : '');
  box.onclick = (e) => {
    const b = e.target.closest("[data-f][data-v]"), m = e.target.closest("[data-more]");
    if (b) { LOTF[b.dataset.f] = b.dataset.v; LOTF.n = LOT_PAGE; drawLots(a, box); }
    else if (m) { LOTF.n += LOT_PAGE; drawLots(a, box); }
  };
  box.querySelector('select[data-f="motivo"]').onchange = (e) => { LOTF.motivo = e.target.value; LOTF.n = LOT_PAGE; drawLots(a, box); };
  const qi = box.querySelector('input[data-f="q"]');
  qi.oninput = () => { LOTF.q = qi.value; LOTF.n = LOT_PAGE; const pos = qi.selectionStart; drawLots(a, box); const n = box.querySelector('input[data-f="q"]'); n.focus(); n.setSelectionRange(pos, pos); };
}

function show(i, scroll) {
  const a = agents[i];
  if (!a) return;
  if (SHOWN !== a.name) LOTF = { estado: "todas", res: "todos", motivo: "", periodo: "tudo", q: "", n: LOT_PAGE };
  SHOWN = a.name;
  markAgent(i);
  const byReturn = (list) => list.slice().sort((x, y) => (headline(y) ?? -1e9) - (headline(x) ?? -1e9));
  const rank = byReturn(agents).indexOf(a) + 1, group = agents.filter((b) => agentGroup(b) === agentGroup(a));
  $("detail").innerHTML = '<div class="panel"><h2>' + esc(a.emoji + " " + a.name) + '</h2>' +
    '<p class="muted">🏅 ' + rank + '.º de ' + agents.length + ' na liga toda · ' + (byReturn(group).indexOf(a) + 1) + '.º de ' + group.length +
    ' em ' + esc(agentGroup(a).replace(/^\S+\s/, "")) + ' · ' + esc(a.strategy.label) + '</p>' +
    (a.backtest ? backtestHtml(a) : liveHtml(a)) + genealogyHtml(a) + strategyHtml(a.strategy) + '</div>';
  chapters($("detail").firstElementChild);
  const lotsBox = $("detail").querySelector(".lots-box");
  if (lotsBox) drawLots(a, lotsBox);
  if (scroll) { openTab("agentes"); $("detail").scrollIntoView({ behavior: "smooth", block: "start" }); }
}
if (agents.length) show(0, false);

// ---- 🗂️ every live agent's open positions: by agent (what each one holds) or by stock (who holds it) ----
(function holdings() {
  if (!$("holdings")) return;
  const live = agents.filter((a) => !a.backtest && (a.positions || []).length);
  $("hold-agent").innerHTML += live.map((a) => '<option value="' + esc(a.name) + '">' + esc(a.emoji + " " + a.name) + ' (' + a.positions.length + ')</option>').join("");
  let view = "agente";
  const draw = () => {
    const who = $("hold-agent").value, q = $("hold-q").value.trim().toUpperCase();
    const rows = [];
    live.forEach((a) => { if (!who || a.name === who) a.positions.forEach((p) => { if (!q || p.symbol.toUpperCase().includes(q) || (p.name || "").toUpperCase().includes(q)) rows.push({ a, p }); }); });
    const agentLink = (a) => '<a href="#" class="hold-ag" data-n="' + esc(a.name) + '">' + esc(a.emoji + " " + a.name) + '</a>';
    if (view === "agente") {
      $("holdings").innerHTML = rows.length ? table(["Agente", "Ativo", "Nome", "Desde", "Investido", "Valor", "Ganho %", "Peso"], rows.map(({ a, p }) => [agentLink(a), '<b>' + esc(p.symbol) + '</b>',
        esc(p.name || p.sector || ""), esc(p.since || p.entry_date || ""), fmtEur(p.cost_eur), fmtEur(p.value_eur), '<span class="' + cls(p.pnl_pct) + '">' + fmtPct(p.pnl_pct) + '</span>',
        fmtNum(p.weight, 1) + "%"]), [4, 5, 6, 7]) + '<p class="muted small">' + rows.length + ' posições em ' + new Set(rows.map((r) => r.a.name)).size + ' agentes.</p>'
        : '<p class="muted">Nenhuma posição com estes filtros.</p>';
    } else {
      const by = {};
      rows.forEach(({ a, p }) => { const x = by[p.symbol] = by[p.symbol] || { p, agents: [], cost: 0, value: 0 }; x.agents.push(a); x.cost += p.cost_eur; x.value += p.value_eur; });
      const list = Object.values(by).sort((x, y) => y.agents.length - x.agents.length || y.value - x.value);
      $("holdings").innerHTML = list.length ? table(["Ativo", "Nome", "Agentes", "Quem tem", "Investido", "Valor", "Ganho %"], list.map((x) => ['<b>' + esc(x.p.symbol) + '</b>',
        esc(x.p.name || x.p.sector || ""), x.agents.length, '<span class="small" style="white-space:normal">' + x.agents.map(agentLink).join(", ") + '</span>', fmtEur(x.cost), fmtEur(x.value),
        '<span class="' + cls(x.value - x.cost) + '">' + fmtPct(x.cost ? (x.value / x.cost - 1) * 100 : 0) + '</span>']), [2, 4, 5, 6]) +
        '<p class="muted small">' + list.length + ' ativos; os detidos por mais agentes primeiro.</p>' : '<p class="muted">Nenhuma posição com estes filtros.</p>';
    }
  };
  $("holdings-box").addEventListener("click", (e) => {
    const v = e.target.closest("[data-hv]"), ag = e.target.closest(".hold-ag");
    if (v) { view = v.dataset.hv; $("holdings-box").querySelectorAll("[data-hv]").forEach((b) => b.classList.toggle("on", b === v)); draw(); }
    if (ag) { e.preventDefault(); const i = agents.findIndex((x) => x.name === ag.dataset.n); if (i >= 0) show(i, true); }
  });
  $("hold-agent").addEventListener("change", draw);
  $("hold-q").addEventListener("input", draw);
  draw();
})();

// ---- tabs ----
function openTab(name) {
  document.querySelectorAll("#tabs .tab").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  document.querySelectorAll("section[data-tab]").forEach((sec) => { sec.hidden = sec.dataset.tab !== name; });
  if (name === "corrida" && window.Plotly) Plotly.Plots.resize("chart");
  if (name === "pretreino" && window.Plotly && $("pt-chart")) Plotly.Plots.resize("pt-chart");
  if (name === "dashboard" && window.Plotly && $("dash-pt-chart") && $("dash-pt-chart").data) Plotly.Plots.resize("dash-pt-chart");
  if (name === "dashboard" && window.dashFit) window.dashFit();
  if (name === "dashboard" && window.Plotly && $("dash-wallet-chart") && $("dash-wallet-chart").data) Plotly.Plots.resize("dash-wallet-chart");
  try { history.replaceState(null, "", "#" + name); } catch (e) {}
}
$("tabs").addEventListener("click", (e) => { const b = e.target.closest(".tab"); if (b) openTab(b.dataset.tab); });

// ---- automatic refresh: the portal checks every minute (while visible) whether anything new was published ----
// What the person is looking at survives the refresh: each chart's shown and hidden lines and its zoom, the open
// sections, the choices in the lists and boxes, and where the page and its tables were scrolled to.
const VIEW_KEY = "portal-view";
function detailsKey(d) {
  const sum = d.querySelector("summary");
  return d.id || ("s:" + (sum ? sum.textContent.trim().slice(0, 80) : ""));
}
function snapshotView() {
  const v = { scroll: window.scrollY, charts: {}, details: [], fields: {}, scrolls: {}, agent: SHOWN };
  document.querySelectorAll(".js-plotly-plot").forEach((el) => {
    if (!el.id || !el.data) return;
    const vis = {};
    el.data.forEach((t) => { const k = t.meta || t.name; if (k) vis[k] = t.visible === undefined ? true : t.visible; });  // meta: the name before the legend adds results
    const ranges = {};
    ["xaxis", "yaxis"].forEach((ax) => {
      const a = (el.layout || {})[ax];
      if (a && a.autorange === false && a.range) ranges[ax + ".range"] = a.range.slice();
    });
    v.charts[el.id] = { vis: vis, ranges: ranges };
  });
  const seen = {};
  document.querySelectorAll("details").forEach((d) => {
    const k = detailsKey(d); seen[k] = (seen[k] || 0) + 1;
    if (d.open) v.details.push(k + "#" + seen[k]);
  });
  document.querySelectorAll("select[id], input[id]").forEach((el) => {
    if (/^(password|file|hidden|button|submit)$/.test(el.type)) return;
    v.fields[el.id] = el.type === "checkbox" || el.type === "radio" ? el.checked : el.value;
  });
  document.querySelectorAll(".scroll").forEach((el, i) => { if (el.scrollLeft) v.scrolls[i] = el.scrollLeft; });
  return v;
}
function restoreView(v) {
  const doneCharts = new WeakSet(), doneFields = {}, doneDetails = {};
  let agentDone = false;
  const step = () => {
    if (!agentDone && v.agent) { agentDone = true; const i = agents.findIndex((a) => a.name === v.agent); if (i >= 0 && v.agent !== SHOWN) show(i, false); }
    Object.keys(v.fields).forEach((id) => {  // first the choices (they may redraw a chart or a list)
      const el = $(id);
      if (!el || doneFields[id]) return;
      doneFields[id] = true;
      const want = v.fields[id];
      if (el.type === "checkbox" || el.type === "radio") { if (el.checked === want) return; el.checked = want; }
      else { if (el.value === want || (el.tagName === "SELECT" && ![...el.options].some((o) => o.value === want))) return; el.value = want; }
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    });
    const seen = {};
    document.querySelectorAll("details").forEach((d) => {
      const k = detailsKey(d); seen[k] = (seen[k] || 0) + 1;
      const key = k + "#" + seen[k];
      if (!doneDetails[key] && v.details.indexOf(key) >= 0) { d.open = true; doneDetails[key] = true; }
    });
    Object.keys(v.charts).forEach((id) => {
      const el = $(id);
      if (!el || !el.data || !window.Plotly || doneCharts.has(el.data)) return;
      doneCharts.add(el.data);
      const c = v.charts[id], idx = [], vis = [];
      el.data.forEach((t, i) => { const k = t.meta || t.name; if (k && k in c.vis) { idx.push(i); vis.push(c.vis[k]); } });
      if (idx.length) Plotly.restyle(el, { visible: vis }, idx);
      if (Object.keys(c.ranges).length) Plotly.relayout(el, c.ranges);
    });
  };
  let n = 0;
  const timer = setInterval(() => { step(); if (++n >= 40) clearInterval(timer); }, 250);  // charts and lists appear as they load
  setTimeout(() => {
    window.scrollTo(0, v.scroll || 0);
    document.querySelectorAll(".scroll").forEach((el, i) => { if (v.scrolls[i]) el.scrollLeft = v.scrolls[i]; });
  }, 600);
}

(function autoRefresh() {
  if (!PORTAL_VERSION) return;
  try {  // back to what the person was looking at, after a refresh
    const saved = sessionStorage.getItem(VIEW_KEY);
    if (saved !== null) { sessionStorage.removeItem(VIEW_KEY); restoreView(JSON.parse(saved)); }
  } catch (e) {}
  const busy = () => {
    const a = document.activeElement;
    return a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName);
  };
  const refresh = () => {
    try { sessionStorage.setItem(VIEW_KEY, JSON.stringify(snapshotView())); } catch (e) {}
    location.reload();
  };
  const check = async () => {
    if (document.visibilityState !== "visible") return;
    try {
      const r = await fetch("/versao", { cache: "no-store" });
      if (!r.ok) return;
      const { v } = await r.json();
      if (!v || v === PORTAL_VERSION) return;
      if (!busy()) return refresh();
      let bar = $("newdata");  // editing something: offer it instead of losing what was typed
      if (!bar) {
        bar = document.body.appendChild(Object.assign(document.createElement("button"), { id: "newdata", textContent: "🔄 Há dados novos · atualizar" }));
        bar.style.cssText = "position:fixed;bottom:16px;left:50%;transform:translateX(-50%);z-index:50;padding:10px 16px;border-radius:999px;border:0;cursor:pointer;background:#3b82f6;color:#fff;font:inherit;box-shadow:0 4px 16px rgba(0,0,0,.3)";
        bar.addEventListener("click", refresh);
      }
    } catch (e) {}
  };
  setInterval(check, 60000);
  document.addEventListener("visibilitychange", check);
})();
{
  const hash = location.hash.slice(1);  // old #agenda links; #perfil and #carteira moved to /perfil
  if ((hash === "perfil" || hash === "carteira") && WALLET) location.replace("/perfil");
  // Oportunidades joined Propostas; Hoje gave way to the Dashboard (the start page)
  const asked = hash === "agenda" && $("status") ? "estado" : hash === "oportunidades" ? "propostas" : hash === "hoje" ? "dashboard" : hash;
  const known = asked && document.querySelector('#tabs .tab[data-tab="' + asked + '"]');
  const start = PREFS && PREFS.start && document.querySelector('#tabs .tab[data-tab="' + PREFS.start + '"]') ? PREFS.start : null;  // ⚙️ Definições
  openTab(known ? asked : start || document.querySelector("#tabs .tab").dataset.tab);
}
document.addEventListener("click", (e) => {  // glossary terms: tap to read (phones have no hover)
  const g = e.target.closest("abbr.g"); if (!g) return;
  const box = $("gpop") || document.body.appendChild(Object.assign(document.createElement("div"), { id: "gpop" }));
  box.textContent = g.title; box.hidden = false; clearTimeout(box.t); box.t = setTimeout(() => { box.hidden = true; }, 6000);
});
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

// ---- glossary ----
(function glossary() {
  if (!$("glossary")) return;
  const terms = Object.entries(GLOSS).sort(([a], [b]) => a.localeCompare(b, "pt", { sensitivity: "base" }));
  const fold = (t) => t.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  $("glossary").innerHTML = '<p class="muted small">Os termos que aparecem no portal, por ordem alfabética. Nas caixas, os termos sublinhados abrem esta explicação ao tocar.</p>' +
    '<div class="filters"><input id="gl-q" type="search" placeholder="Procurar um termo ou palavra (ex.: stop, risco, VIX)" style="flex:1;min-width:240px"></div>' +
    '<p class="muted small" id="gl-n"></p><div class="cards" id="gl-list"></div>';
  const draw = () => {
    const q = fold($("gl-q").value.trim());
    const list = terms.filter(([k, v]) => !q || fold(k + " " + v).includes(q));
    $("gl-n").textContent = list.length + " de " + terms.length + " termos";
    $("gl-list").innerHTML = list.map(([k, v]) => '<div class="opp"><b>' + esc(k) + '</b><p class="small">' + esc(v) + '</p></div>').join("") ||
      '<p class="muted">Nenhum termo com essa palavra.</p>';
  };
  $("gl-q").addEventListener("input", draw);
  draw();
})();

// ---- performance by period ----
const PERIODS = [["1D", 1], ["1W", 7], ["1M", 30], ["3M", 91], ["6M", 182], ["1A", 365], ["2A", 730], ["3A", 1095], ["4A", 1461],
  ["5A", 1826], ["10A", 3652]];
function perfTable(boxId, data, people, pick) {
  const box = $(boxId);
  if (!box) return;
  // agents on the bench (out of the league: not beating the S&P 500 in the pre-training) are listed too, greyed,
  // with their values up to the day they left
  const series = (data.series || []).filter((s) => s.x.length)
    .concat((data.bench || []).filter((s) => s.x.length).map((s) => ({ ...s, bench: true })));
  if (!series.length) { box.innerHTML = '<p class="muted">Ainda sem histórico.</p>'; return; }
  const byName = Object.fromEntries((people || []).map((a) => [a.name, a]));
  const dayOf = (v) => String(v).slice(0, 10);  // the live point has a time; the table counts in days
  const lastX = series.reduce((m, s) => (s.x[s.x.length - 1] > m ? s.x[s.x.length - 1] : m), "");
  const last = dayOf(lastX);
  const first = dayOf(series.reduce((m, s) => (s.x[0] < m ? s.x[0] : m), "9999"));
  const shift = (day, days) => new Date(Date.parse(day + "T12:00:00Z") - days * 864e5).toISOString().slice(0, 10);
  // the value (% since the agent started) on the last point on or before a date; null before it started
  const at = (s, day) => { let v = null; for (let i = 0; i < s.x.length && dayOf(s.x[i]) <= day; i++) v = s.y[i]; return v; };
  // return between two dates from the "since start" curve: (1 + end) / (1 + start) - 1
  const between = (s, from, to, whole) => {
    const end = at(s, to);
    if (end == null) return { v: null };
    const start = whole ? null : at(s, from);
    const late = !whole && start == null;  // started after the period began: counted from its first day
    const base = late ? 0 : start;
    return { v: ((1 + end / 100) / (1 + base / 100) - 1) * 100, late };
  };
  let custom = { from: shift(last, 30), to: last };
  // weekly data cannot show one day: periods shorter than the spacing of the latest points are left out
  // (the median spacing of the latest points; with only a few, the smallest: a weekend is not the spacing)
  const tail = series[0].x.slice(-21);
  const gaps = tail.slice(1).map((d, i) => (Date.parse(d) - Date.parse(tail[i])) / 864e5).sort((a, b) => a - b);
  const step = gaps.length >= 5 ? gaps[Math.floor(gaps.length / 2)] : gaps.length ? gaps[0] : 1;
  let sortCol = "all", showAll = false;
  const LIMIT = 20;  // the first 20 by the chosen column; a button shows the rest
  function draw() {
    const cols = PERIODS.filter(([, d]) => d >= step).map(([k, d]) => ({ key: k, label: k, from: shift(last, d), to: last }))
      .concat([{ key: "custom", label: "Personalizado", from: custom.from, to: custom.to }, { key: "all", label: "Desde o início", from: "0000-01-01", to: last }]);
    const rows = series.map((s) => ({ s, a: s.bench ? null : byName[s.name], cells: Object.fromEntries(cols.map((c) => {
      const end = s.bench && dayOf(s.x[s.x.length - 1]) < c.to ? dayOf(s.x[s.x.length - 1]) : c.to;  // a benched one: up to its exit
      return [c.key, between(s, c.from, end, c.key === "all")];
    })) }));
    const best = Object.fromEntries(cols.map((c) => [c.key, Math.max(...rows.filter((r) => !r.s.bench).map((r) => r.cells[c.key].v ?? -Infinity))]));
    const benchDay = (s) => { const d = dayOf(s.since || s.x[s.x.length - 1]); return d.slice(8, 10) + "/" + d.slice(5, 7); };
    rows.sort((x, y) => (y.cells[sortCol].v ?? -Infinity) - (x.cells[sortCol].v ?? -Infinity));
    const total = rows.length;
    if (!showAll) rows.splice(LIMIT);
    box.innerHTML = '<div class="filters"><label class="small">Personalizado: de <input type="date" class="pf-from" value="' + custom.from + '" min="' + first + '" max="' + last + '"></label>' +
      '<label class="small">a <input type="date" class="pf-to" value="' + custom.to + '" min="' + first + '" max="' + last + '"></label>' +
      '<span class="muted small">Toca no título de uma coluna para ordenar. Dados até ' + esc(lastX.length > 10 ? lisbon(Date.parse(lastX), { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : last) + '.</span></div>' +
      '<div class="scroll"><table class="perf"><tr><th>Agente</th>' + cols.map((c) => '<th class="num"><a href="#" class="pf-sort" data-k="' + c.key + '">' + esc(c.label) + (sortCol === c.key ? " ▼" : "") + '</a></th>').join("") + '</tr>' +
      rows.map((r) => '<tr' + (r.s.bench ? ' class="benched" title="No banco desde ' + benchDay(r.s) + ': não bate o S&P 500 no pré-treino deste perfil. Continua a treinar e volta quando o bater. Valores até à saída."' : "") + '><td>' +
        (r.a && pick ? '<a href="#" class="pf-agent" data-n="' + esc(r.s.name) + '" title="Abrir a ficha do agente">' : "") +
        esc((r.s.bench ? "🪑 " + (r.s.emoji ? r.s.emoji + " " : "") : r.a ? r.a.emoji + " " : "") + r.s.name) + (r.a && pick ? "</a>" : "") +
        (r.s.bench ? ' <span class="small">(banco desde ' + benchDay(r.s) + ')</span>' : "") + '</td>' + cols.map((c) => {
        const x = r.cells[c.key];
        if (x.v == null) return '<td class="num muted">—</td>';
        return '<td class="num"><span class="' + cls(x.v) + '">' + (!r.s.bench && x.v === best[c.key] && rows.length > 1 ? "🏆 " : "") + fmtPct(x.v) + (x.late ? "*" : "") + '</span></td>';
      }).join("") + '</tr>').join("") + '</table></div>' +
      (total > LIMIT ? '<p><button class="chip pf-more">' + (showAll ? "Mostrar só os " + LIMIT + " primeiros" : "Mostrar todos (" + total + ")") + '</button></p>' : "") +
      '<p class="muted small">Rentabilidade em cada período, com custos e impostos. Toca no nome de um agente para abrir a ficha. * o agente começou depois do início do período: conta desde o primeiro dia dele.' +
        (series.some((s) => s.bench) ? ' 🪑 a cinzento: no banco (fora da liga por não bater o S&P 500 no pré-treino do seu perfil), com os valores até ao dia em que saiu.' : "") + '</p>';
    const more = box.querySelector(".pf-more");
    if (more) more.addEventListener("click", () => { showAll = !showAll; draw(); });
    box.querySelectorAll(".pf-sort").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); sortCol = a.dataset.k; draw(); }));
    box.querySelectorAll(".pf-agent").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); if (byName[a.dataset.n]) pick(byName[a.dataset.n]); }));
    box.querySelector(".pf-from").addEventListener("change", (e) => { custom.from = e.target.value || custom.from; sortCol = "custom"; draw(); });
    box.querySelector(".pf-to").addEventListener("change", (e) => { custom.to = e.target.value || custom.to; sortCol = "custom"; draw(); });
  }
  draw();
}
perfTable("perf", DATA, agents, (a) => show(agents.indexOf(a), true));
if (PRETRAIN) perfTable("pt-perf", PRETRAIN, PRETRAIN.agents || [], (a) => {  // through its button in the list
  const b = [...document.querySelectorAll("#pt-chips .agent-item")].find((x) => (PRETRAIN.agents || [])[+x.dataset.i] === a);
  if (b) b.click();
});

// ---- compare two agents ----
(function compare() {
  if (!$("cmp-a") || agents.length < 2) { if ($("compare-box")) $("compare-box").hidden = true; return; }
  const opts = agents.map((a, i) => '<option value="' + i + '">' + esc(a.emoji + " " + a.name) + '</option>').join("");
  $("cmp-a").innerHTML = opts; $("cmp-b").innerHTML = opts; $("cmp-b").value = 1;
  const live = !agents[0].backtest;
  const rows = live
    ? [["Rentabilidade", (a) => fmtPct(a.return_pct), (a) => a.return_pct], ["Hoje", (a) => fmtPct(a.day_pct), (a) => a.day_pct], ["Pontos", (a) => fmtNum(a.points, 0), (a) => a.points],
       ["Queda máxima", (a) => fmtPct(-a.max_dd_pct), (a) => -a.max_dd_pct], ["Sharpe", (a) => fmtNum(a.sharpe), (a) => a.sharpe], ["Posições", (a) => a.positions.length, null],
       ["Investido", (a) => fmtNum(a.invested_pct, 0) + "%", null], ["Vendas com lucro", (a) => a.sells ? fmtNum(a.wins / a.sells * 100, 0) + "%" : "—", (a) => a.sells ? a.wins / a.sells : null],
       ["Estratégia", (a) => esc(a.strategy.label), null]]
    : [["Rentabilidade", (a) => fmtPct(a.backtest.return_pct, 0), (a) => a.backtest.return_pct], ["Queda máxima", (a) => fmtPct(-a.backtest.max_dd_pct, 0), (a) => -a.backtest.max_dd_pct],
       ["Sharpe", (a) => fmtNum(a.backtest.sharpe), (a) => a.backtest.sharpe], ["Operações", (a) => a.backtest.trades ?? "—", null],
       ["Pior 5% (Monte Carlo)", (a) => a.backtest.monte_carlo?.p5 != null ? fmtPct(a.backtest.monte_carlo.p5 * 100) : "—", (a) => a.backtest.monte_carlo?.p5], ["Estratégia", (a) => esc(a.strategy.label), null]];
  function draw() {
    const A = agents[+$("cmp-a").value], B = agents[+$("cmp-b").value];
    $("cmp").innerHTML = table(["", esc(A.emoji + " " + A.name), esc(B.emoji + " " + B.name)], rows.map(([label, f, v]) => {
      const va = v ? v(A) : null, vb = v ? v(B) : null;
      const win = va != null && vb != null && va !== vb ? (va > vb ? 1 : 2) : 0;
      return [label, (win === 1 ? "🏆 " : "") + f(A), (win === 2 ? "🏆 " : "") + f(B)];
    })) + '<div id="cmp-chart" style="height:280px"></div>';
    const sa = (DATA.series || []).find((x) => x.name === A.name), sb = (DATA.series || []).find((x) => x.name === B.name);
    if (window.Plotly && sa && sb) Plotly.newPlot("cmp-chart", [sa, sb].map((x, i) => ({ x: x.x, y: x.y, name: x.name, type: "scatter", mode: "lines", line: { color: i ? "#ffe14d" : "#00e5ff", width: 2 } })),
      { paper_bgcolor: "#17123a", plot_bgcolor: "#17123a", font: { color: "#e8e6ff" }, margin: { l: 50, r: 10, t: 10, b: 30 }, hovermode: "x unified",
        yaxis: { ticksuffix: "%", gridcolor: "#2e2763" }, xaxis: { gridcolor: "#2e2763" }, legend: { orientation: "h", y: -0.2 } }, { responsive: true, displaylogo: false });
  }
  ["cmp-a", "cmp-b"].forEach((id) => $(id).addEventListener("input", draw));
  $("compare-box").addEventListener("toggle", draw, { once: true });
})();

// ---- today ----
// ---- 📊 dashboard: the market, the member's portfolio, the league, the pre-training, the news and the proposals ----
(function dashboard() {
  if (!$("dash")) return;
  const Q = QUOTES || {}, M = MARKET || {}, c = M.context || {}, P = PROPOSALS;
  const dayPct = (q) => q && q.prev ? (q.price / q.prev - 1) * 100 : null;
  const money = (x, cur) => fmtNum(x) + " " + (({ EUR: "€", USD: "$", GBp: "p" })[cur] || cur || "");
  const go = (t, label) => '<a href="#' + t + '" class="go" data-t="' + t + '">' + label + ' →</a>';
  const zone = M.zone || (P && P.zone) || "—";
  const zoneCls = { Alta: "up", Neutra: "gold", Cautelosa: "gold", Queda: "down" }[zone] || "";
  const vixWord = c.vix == null ? "" : c.vix > 30 ? "mercado com medo" : c.vix > 20 ? "mercado nervoso" : "mercado calmo";
  const spy = Q.spy, oil = Q.oil, stock = Q.stock;
  const walletOn = !!(Q.wallet && WALLET);
  const stockTile = () => stock ? tile("🔥 " + esc(stock.symbol) + " · destaque do dia", fmtPct(dayPct(stock)), esc(stock.name) + " · " + money(stock.price, stock.currency), cls(dayPct(stock)))
    : tile("🔥 Destaque do dia", "—", "sem cotação agora");
  const top = '<div class="dash-top">' +
    tile("📈 S&P 500 (SPY) hoje", fmtPct(dayPct(spy)), spy ? money(spy.price, spy.currency) + (c.spy_trend != null ? " · " + fmtPct(c.spy_trend * 100) + " vs média 200d" : "") : "sem cotação agora", cls(dayPct(spy))) +
    '<div id="dash-wallet-tile">' + (walletOn ? tile("💼 A tua carteira hoje", "…", "a carregar") : stockTile()) + '</div>' +
    tile("🧭 Sentimento do mercado", esc(zone), (c.vix != null ? "VIX " + fmtNum(c.vix, 0) + " · " + vixWord : "") + (M.stage2_pct != null ? " · " + fmtNum(M.stage2_pct, 0) + "% em Stage 2" : ""), zoneCls) +
    tile("🛢️ Petróleo (Brent) hoje", fmtPct(dayPct(oil)), (oil ? money(oil.price, oil.currency) : "sem cotação agora") + (c.oil_chg != null ? " · 3 meses " + fmtPct(c.oil_chg * 100) : ""), cls(dayPct(oil))) + '</div>';
  const wallet = '<div class="dcard"><div class="dh"><h3>💼 A tua carteira</h3>' + (WALLET ? '<a href="/perfil#resumo">Abrir no perfil →</a>' : '') + '</div><div id="dash-wallet">' +
    (walletOn ? '<p class="muted small">A carregar…</p>' : WALLET ? '<p class="muted">Ainda não tens movimentos. Importa o extrato da corretora ou adiciona uma compra no <a href="/perfil#importar">perfil</a> e o resumo aparece aqui.</p>'
      : '<p class="muted"><a href="/entrar">Entra com o Discord ou o Google</a> para veres aqui a tua carteira.</p>') + '</div></div>';
  const leaders = agents.slice().sort((a, b) => headline(b) - headline(a));
  const league = '<div class="dcard dash-league"><div class="dh"><h3>🏁 Liga dos agentes</h3>' + go("corrida", "Corrida") + '</div><div class="dash-league-list">' +
    (leaders.map((a, i) => '<div class="drow"><span class="nm">' + (i + 1) + ' · <a href="#" class="dash-agent" data-n="' + esc(a.name) + '">' + esc(a.emoji + " " + a.name) + '</a></span><span class="' + cls(headline(a)) + '">' + fmtPct(headline(a)) + '</span></div>').join("") ||
      '<p class="muted">Ainda sem agentes.</p>') + '</div>' +
    (leaders.length ? '<p class="muted small">' + leaders.length + ' agentes na liga · dados de ' + esc(lisbon(Date.parse(DATA.updated), { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })) + '</p>' : '') + '</div>';
  const pretrain = '<div class="dcard"><div class="dh"><h3>⏪ Treino</h3>' + (PRETRAIN ? go("pretreino", "Treino") : '') + '</div><div id="dash-pt">' +
    (PRETRAIN ? '' : '<p class="muted">O pré-treino aparece aqui depois da próxima execução.</p>') + '</div></div>';
  const items = ((NEWS || {}).items || []).filter((i) => (i.importancia || 0) >= 2).slice(0, 5);
  const news = '<div class="dcard"><div class="dh"><h3>📰 Notícias</h3>' + go("noticias", "Todas") + '</div>' +
    (items.map((i) => '<div class="drow"><span class="nm" title="' + esc(i.resumo || i.title) + '"><b class="' + (i.sentimento > 0 ? "up" : i.sentimento < 0 ? "down" : "") + '">' + esc(i.symbol) + '</b> ' + esc(i.resumo || i.title) + '</span></div>').join("") ||
      '<p class="muted">Sem notícias importantes.</p>') + '</div>';
  const buys = P ? (P.buys || []).filter((b) => !b.conflict).slice(0, 4) : [], sells = P ? (P.sells || []).slice(0, 2) : [];
  const props = '<div class="dcard"><div class="dh"><h3>🧠 Propostas de hoje</h3>' + go("propostas", "Ver") + '</div>' +
    (buys.map((b) => '<div class="drow"><span class="nm"><b>' + esc(b.symbol) + '</b> <span class="muted small">' + esc(b.name || "") + '</span></span><span><span class="up">comprar</span>' + (b.safety != null ? ' <span class="muted small">🛡️ ' + b.safety + '</span>' : '') + '</span></div>').join("") +
     sells.map((x) => '<div class="drow"><span class="nm"><b>' + esc(x.symbol) + '</b></span><span class="down">vender</span></div>').join("") || '<p class="muted">Sem propostas hoje.</p>') + '</div>';
  $("dash").innerHTML = top + '<div class="dash-row dash-row-a">' + league + pretrain + '</div>' +
    '<div class="dash-row">' + wallet + '<div class="dash-col">' + news + props + '</div></div>';
  $("dash").addEventListener("click", (e) => {
    const a = e.target.closest(".go");
    if (a) { e.preventDefault(); openTab(a.dataset.t); window.scrollTo(0, 0); return; }
    const ag = e.target.closest(".dash-agent");
    if (ag) { e.preventDefault(); const i = agents.findIndex((x) => x.name === ag.dataset.n); if (i >= 0) { openTab("agentes"); show(i, true); } }
  });

  // the pre-training over the chosen years (20 by default), every line from 0% at the start of the interval
  if (PRETRAIN) {
    const series = (PRETRAIN.series || []).filter((s) => s.x.length);
    const first = series.reduce((m, s) => (s.x[0] < m ? s.x[0] : m), "9999"), last = series.reduce((m, s) => (s.x[s.x.length - 1] > m ? s.x[s.x.length - 1] : m), "");
    const span = last ? (Date.parse(last) - Date.parse(first)) / 3.156e10 : 0;
    const spans = [20, 15, 10, 5, 3, 1].filter((y) => y <= Math.round(span));
    if (!spans.length) spans.push(Math.max(1, Math.round(span)));
    let years = store.get("dash-pt-years", spans[0]);
    if (spans.indexOf(years) < 0) years = spans[0];
    const ptByName = Object.fromEntries((PRETRAIN.agents || []).map((a) => [a.name, a]));
    const colours = ["#39ff88", "#00e5ff", "#ffe14d", "#ff2e88", "#9b84ee"];
    const draw = () => {
      const from = new Date(Date.parse(last) - years * 3.156e10).toISOString().slice(0, 10);
      const cut = series.map((s) => {
        let i = 0;
        while (i < s.x.length - 1 && String(s.x[i]).slice(0, 10) < from) i++;
        const base = 1 + s.y[i] / 100;
        const y = s.y.slice(i).map((v) => ((1 + v / 100) / base - 1) * 100);
        return { name: s.name, x: s.x.slice(i), y, ret: y[y.length - 1] };
      });
      const bench = cut.find((s) => s.name === "Indexador");
      const best = cut.filter((s) => s.name !== "Indexador").sort((a, b) => b.ret - a.ret).slice(0, 5);
      $("dash-pt").innerHTML = '<div class="chips">' + spans.map((y) => '<button class="chip' + (y === years ? ' on' : '') + '" data-y="' + y + '">' + y + (y === 1 ? ' ano' : ' anos') + '</button>').join("") + '</div>' +
        '<div id="dash-pt-chart" class="dash-chart"></div>' +
        best.map((s, i) => { const a = ptByName[s.name]; return '<div class="drow"><span class="nm"><span style="color:' + colours[i] + '">━</span> ' + esc((a ? a.emoji + " " : "") + s.name) + '</span><span class="' + cls(s.ret) + '">' + fmtPct(s.ret, 0) + '</span></div>'; }).join("") +
        (bench ? '<div class="drow"><span class="nm muted">┅ 🐢 Indexador (S&P 500)</span><span class="' + cls(bench.ret) + '">' + fmtPct(bench.ret, 0) + '</span></div>' : '') +
        '<p class="muted small">Os 5 melhores ' + (years === 1 ? 'no último ano' : 'nos últimos ' + years + ' anos') + ' da simulação, com custos e impostos' +
        (PRETRAIN.updated ? ' · treino de ' + esc(lisbon(Date.parse(PRETRAIN.updated), { day: "2-digit", month: "2-digit", year: "numeric" })) : '') + '.</p>';
      if (window.Plotly) Plotly.newPlot("dash-pt-chart", best.map((s, i) => ({ x: s.x, y: s.y, name: s.name, type: "scatter", mode: "lines", line: { color: colours[i], width: 2 } }))
        .concat(bench ? [{ x: bench.x, y: bench.y, name: "Indexador", type: "scatter", mode: "lines", line: { color: "#9b96c9", width: 1.5, dash: "dot" } }] : []),
        { paper_bgcolor: "#17123a", plot_bgcolor: "#17123a", font: { color: "#e8e6ff", size: 11 }, margin: { l: 48, r: 8, t: 8, b: 26 }, hovermode: "x unified", showlegend: false,
          yaxis: { ticksuffix: "%", gridcolor: "#2e2763" }, xaxis: { gridcolor: "#2e2763" } }, { responsive: true, displaylogo: false, displayModeBar: false });
      else $("dash-pt-chart").style.display = "none";
    };
    $("dash-pt").addEventListener("click", (e) => {
      const b = e.target.closest("[data-y]");
      if (!b) return;
      years = +b.dataset.y;
      store.set("dash-pt-years", years);
      draw();
      fitLeague();
    });
    draw();
  }
  function fitLeague() {
    const list = document.querySelector(".dash-league-list");
    if (!list || !list.offsetParent) return;  // the tab is hidden: measured when it opens
    const rows = [...list.children];
    rows.forEach((r) => { r.style.display = ""; });
    if (window.matchMedia("(max-width:900px)").matches) { rows.forEach((r, i) => { r.style.display = i >= 10 ? "none" : ""; }); return; }
    const bottom = list.getBoundingClientRect().bottom;
    rows.forEach((r) => { if (r.getBoundingClientRect().bottom > bottom + 1) r.style.display = "none"; });
  }
  window.dashFit = fitLeague;
  window.addEventListener("resize", fitLeague);
  fitLeague();

  // the member's portfolio: the same data as the profile (/c/<token>/data)
  if (walletOn) fetch(WALLET + "/data", { cache: "no-store" }).then((r) => r.ok ? r.json() : null).then((w) => {
    if (!w || !(w.tx || []).length) { $("dash-wallet-tile").innerHTML = stockTile(); $("dash-wallet").innerHTML = '<p class="muted">Ainda não tens movimentos.</p>'; return; }
    let value = 0, cost = 0, day = 0;
    const pos = {};
    for (const t of w.tx.slice().sort((a, b) => a.date.localeCompare(b.date) || a.seq - b.seq)) {
      const p = pos[t.symbol] ||= { qty: 0, cost: 0, cur: t.currency, name: t.name || "" };
      if (t.side === "buy") { p.qty += t.qty; p.cost += t.qty * t.price + t.fee; } else { const avg = p.qty ? p.cost / p.qty : 0; p.cost -= avg * t.qty; p.qty -= t.qty; }
    }
    const rows = [];
    for (const [sym, p] of Object.entries(pos)) {
      const q = w.quotes[sym], fx = w.fx[p.cur];
      if (!q || fx == null || p.qty <= 1e-9) continue;
      const v = p.qty * q.price * fx, d = p.qty * (q.price - (q.prev ?? q.price)) * fx;
      value += v; cost += p.cost * fx; day += d;
      rows.push({ sym, name: q.name || p.name || "", qty: p.qty, v, c: p.cost * fx, d, pct: q.prev ? (q.price / q.prev - 1) * 100 : null });
    }
    const dayP = value - day ? day / (value - day) * 100 : null;
    $("dash-wallet-tile").innerHTML = tile("💼 A tua carteira hoje", fmtPct(dayP), fmtEur(day) + " · valor total " + fmtEur(value), cls(day));
    const sells = Object.entries(w.verdicts || {}).filter(([k, v]) => k !== "rules" && v.verdict === "Vender").map(([k]) => k);
    rows.sort((a, b) => b.v - a.v);
    const SCALES = [["dias", "Dias", "dias"], ["semanas", "Semanas", "semanas"], ["meses", "Meses", "meses"], ["anos", "Anos", "anos"]];
    let scale = store.get("dash-wallet-scale", "meses");
    if (!SCALES.some(([k]) => k === scale)) scale = "meses";
    let pick = "";  // "" = the whole portfolio, else a symbol
    let showInv = store.get("dash-wallet-inv", true) !== false;  // off: only the gain or loss, on its own scale
    // each position's advice: the portfolio's verdict (Manter / Reduzir / Vender, rules in the profile), reasons on hover
    const advice = (k) => {
      const v = k && (w.verdicts || {})[k];
      if (!v || !v.verdict || v.verdict === "?") return k ? '<span class="wadv muted small">—</span>' : '<span class="wadv"></span>';
      const icon = { Vender: "🔴", Reduzir: "🟠", Manter: "🟢" }[v.verdict] || "";
      const why = (v.reasons || []).join(" · ") + (v.stop ? " · stop sugerido " + fmtNum(v.stop) : "");
      return '<span class="wadv small ' + (v.verdict === "Vender" ? "down" : v.verdict === "Reduzir" ? "gold" : "up") + '" title="' + esc(why) + '" data-tip="' + esc(why) + '" tabindex="0">' + icon + ' ' + esc(v.verdict) + '</span>';
    };
    // the company's name, short: "BANCO COMERCIAL PORTUGUES, S.A." -> "Banco Comercial Portugues" (the full one on hover)
    const SUFFIX = /[\s,]+(s\.?\s?a\.?|sgps|n\.?\s?v\.?|se|ag|plc|inc\.?|corp\.?|corporation|ltd\.?|limited|s\.?p\.?a\.?|asa|ab|oyj|co\.?|company|group|holdings?)\.?$/i;
    const nice = (s) => {
      let n = String(s || "").trim(), o;
      do { o = n; n = n.replace(SUFFIX, "").trim(); } while (n !== o && n);
      if (!n) n = String(s || "").trim();
      if (n === n.toUpperCase()) n = n.toLowerCase().replace(/(^|[\s\-&.'(/])(\p{L})/gu, (m, a, b) => a + b.toUpperCase());
      return n;
    };
    const line = (k, label, sub, v, pct, name) => '<div class="drow wsel' + (k === pick ? ' on' : '') + '" data-k="' + esc(k) + '" data-p="' + (pct ?? "") + '" role="button" tabindex="0"><span class="nm wnm"><span class="wl1"><b>' + label + '</b>' +
      (name ? ' <span class="wname" data-noi18n title="' + esc(name) + '">' + esc(nice(name)) + '</span>' : '') + '</span><span class="wl2 muted small">' + sub + '</span></span>' + advice(k) + '<span class="wp ' + cls(pct) + '">' + fmtPct(pct) + '</span></div>';
    // the tiles follow the chosen row: the whole portfolio or one position
    const TAX = 0.28;  // Portugal: 28% on the gain (capital gains), only when there is a gain
    const tiles = () => {
      const r = pick ? rows.find((x) => x.sym === pick) : { v: value, c: cost, d: day, pct: dayP };
      const verdict = pick ? ((w.verdicts || {})[pick] || {}).verdict : null;
      const gross = r.v - r.c, net = gross > 0 ? gross * (1 - TAX) : gross;
      const pc = (g) => (r.c ? fmtPct(g / r.c * 100, 2) : "—");
      const help = gross > 0 ? "Depois de 28% de imposto sobre o lucro (" + fmtEur(gross * TAX) + "), se vendesses hoje. Em Portugal as mais-valias pagam 28% (taxa liberatória); quem opta pelo englobamento pode pagar outra taxa."
        : "Sem imposto: não há lucro. Em Portugal o imposto de 28% só incide sobre o lucro, quando vendes.";
      return tile("Valor" + (pick ? " · " + esc(pick) : ""), fmtEur(r.v)) +
        tile("Ganho bruto", fmtEur(gross), pc(gross), cls(gross)) +
        '<div class="tile"><div class="k">Ganho líquido <span class="qhelp" tabindex="0" data-tip="' + esc(help) + '" aria-label="' + esc(help) + '">?</span></div><div class="v ' + cls(net) + '">' + fmtEur(net) + '</div><div class="s">' + pc(net) + '</div></div>' +
        tile("Hoje", fmtEur(r.d), fmtPct(r.pct, 2), cls(r.d)) +
        (pick ? tile("Veredito", esc(verdict || "—"), "da carteira para " + esc(pick), verdict === "Vender" ? "down" : verdict ? "up" : "")
          : tile("Sinais de venda", sells.length ? esc(sells.join(", ")) : "nenhum", "veredito da carteira", sells.length ? "down" : "up"));
    };
    const first = w.tx.reduce((m, t) => (t.date < m ? t.date : m), "9999"), todayIso = new Date().toISOString().slice(0, 10);
    let from = first, to = todayIso;
    $("dash-wallet").innerHTML = '<div class="wtiles" id="dash-wallet-tiles">' + tiles() + '</div>' +
      '<div class="wbar"><div class="wleft"><div class="chips" id="dash-wallet-scale">' + SCALES.map(([k, t]) => '<button class="chip' + (k === scale ? ' on' : '') + '" data-s="' + k + '">' + t + '</button>').join("") + '</div>' +
      '<label class="wchk"><input type="checkbox" id="dash-wallet-inv"' + (showInv ? ' checked' : '') + '> <span>Investimento</span></label></div>' +
      '<div class="wrange"><label>De <input type="date" id="dash-wallet-from" value="' + first + '" min="' + first + '" max="' + todayIso + '"></label>' +
      '<label>a <input type="date" id="dash-wallet-to" value="' + todayIso + '" min="' + first + '" max="' + todayIso + '"></label>' +
      '<button class="chip" id="dash-wallet-all" title="Desde a primeira compra até hoje">Tudo</button></div></div>' +
      '<div id="dash-wallet-chart" class="dash-chart dash-bars"><p class="muted small">A carregar o histórico…</p></div>' +
      '<p class="muted small"><span id="dash-wallet-leg-inv"' + (showInv ? '' : ' hidden') + '><span style="color:#1a56c4">■</span> <span>investido</span> · </span><span style="color:#39ff88">■</span> <span>lucro</span> · <span style="color:#ff4d6d">■</span> <span>prejuízo</span> · <span id="dash-wallet-what">Total</span></p>' +
      '<p class="muted small wper" id="dash-wallet-per">% de hoje</p>' +
      '<div id="dash-wallet-rows">' + line("", "Total", fmtEur(value), value, dayP) +
      rows.slice(0, 6).map((r) => line(r.sym, esc(r.sym), fmtNum(r.qty, r.qty % 1 ? 2 : 0) + ' · ' + fmtEur(r.v), r.v, r.pct, r.name)).join("") + '</div>' +
      (rows.length > 6 ? '<p class="muted small">+ ' + (rows.length - 6) + ' posições no perfil</p>' : '');

    // day by day since the first purchase, each position's money in (average cost, in euros at each purchase's rate) and
    // its value at that day's close; the last day is today's live value, the same as the tiles above
    let H = null;
    const build = (h) => {
      const tx = w.tx.slice().sort((a, b) => a.date.localeCompare(b.date) || a.seq - b.seq);
      const first = tx[0].date, today = new Date().toISOString().slice(0, 10);
      const days = [...new Set(Object.values(h.series || {}).flatMap((x) => x.t))].filter((d) => d >= first && d < today).sort();
      days.push(today);
      const cursor = (x) => { let i = 0, v = null; return (d) => { if (!x) return null; while (i < x.t.length && x.t[i] <= d) { if (x.c[i] != null) v = x.c[i]; i++; } return v; }; };
      const rate = (cur, close) => (close ? (cur === "GBp" || cur === "GBX" ? 0.01 : 1) / close : null);
      const fxAt = (cur, d) => { if (!cur || cur === "EUR") return 1; const x = (h.fx || {})[cur]; let v = null; if (x) for (let i = 0; i < x.t.length && x.t[i] <= d; i++) if (x.c[i] != null) v = x.c[i]; return rate(cur, v) ?? w.fx[cur] ?? 1; };
      const px = {}, fxc = {}, st = {}, out = { "": { inv: [], val: [], rg: [] } };
      for (const t of tx) { px[t.symbol] ||= cursor((h.series || {})[t.symbol]); if (t.currency && t.currency !== "EUR") fxc[t.currency] ||= cursor((h.fx || {})[t.currency]); }
      let ti = 0;
      for (const d of days) {
        for (; ti < tx.length && tx[ti].date <= d; ti++) {
          const t = tx[ti], q = st[t.symbol] ||= { qty: 0, inv: 0, rg: 0, cur: t.currency };
          if (t.side === "buy") { q.qty += t.qty; q.inv += (t.qty * t.price + t.fee) * fxAt(t.currency, t.date); }
          else { const avg = q.qty ? q.inv / q.qty : 0; q.rg += (t.qty * t.price - t.fee) * fxAt(t.currency, t.date) - avg * t.qty; q.inv -= avg * t.qty; q.qty -= t.qty; }
        }
        let ti2 = 0, tv = 0, tr = 0;
        for (const [sym, q] of Object.entries(st)) {
          const o = out[sym] ||= { inv: new Array(out[""].inv.length).fill(0), val: new Array(out[""].inv.length).fill(0), rg: new Array(out[""].inv.length).fill(0) };
          const close = px[sym](d), fx = !q.cur || q.cur === "EUR" ? 1 : rate(q.cur, fxc[q.cur](d)) ?? w.fx[q.cur] ?? 1;
          const live = d === today ? rows.find((r) => r.sym === sym) : null;
          const held = q.qty > 1e-9, inv = held ? (live ? live.c : q.inv) : 0;
          const val = held ? (live ? live.v : close != null ? q.qty * close * fx : inv) : 0;
          o.inv.push(inv); o.val.push(val); o.rg.push(q.rg); ti2 += inv; tv += val; tr += q.rg;
        }
        out[""].inv.push(ti2); out[""].val.push(tv); out[""].rg.push(tr);
      }
      return { days, out };
    };
    const monday = (d) => { const x = new Date(d + "T00:00:00Z"); x.setUTCDate(x.getUTCDate() - (x.getUTCDay() + 6) % 7); return x.toISOString().slice(0, 10); };
    const keyOf = { dias: (d) => d, semanas: monday, meses: (d) => d.slice(0, 7), anos: (d) => d.slice(0, 4) };
    const labelOf = { dias: (k) => k.slice(8, 10) + "/" + k.slice(5, 7), semanas: (k) => k.slice(8, 10) + "/" + k.slice(5, 7), meses: (k) => k.slice(5, 7) + "/" + k.slice(0, 4), anos: (k) => k };
    const PER = { dias: "do dia", semanas: "da semana", meses: "do mês", anos: "do ano" };
    const dmy = (d) => d.split("-").reverse().join("/");
    const period = () => {
      if (!H || !H.days.length) return;
      let e = -1;
      H.days.forEach((d, i) => { if (d <= to) e = i; });
      if (e < 0) return;
      const k = keyOf[scale](H.days[e]);
      let b = e;
      while (b > 0 && keyOf[scale](H.days[b - 1]) === k) b--;
      const s0 = b - 1;  // the close before the period starts (-1: nothing held yet)
      const live = scale === "dias" && e === H.days.length - 1;  // today: the live change, as in the tile
      if (live) {
        $("dash-wallet-rows").querySelectorAll(".wsel").forEach((r) => { const v = r.dataset.p === "" ? null : +r.dataset.p, x = r.querySelector(".wp"); x.className = "wp " + cls(v); x.textContent = fmtPct(v); });
        $("dash-wallet-per").textContent = "% de hoje";
        return;
      }
      const pctOf = (o) => {
        if (!o) return null;
        const g = (i) => (i < 0 ? 0 : o.val[i] - o.inv[i] + o.rg[i]), inv0 = s0 < 0 ? 0 : o.inv[s0];
        const base = (s0 < 0 ? 0 : o.val[s0]) + Math.max(0, o.inv[e] - inv0);
        return base ? (g(e) - g(s0)) / base * 100 : null;
      };
      $("dash-wallet-rows").querySelectorAll(".wsel").forEach((r) => {
        const v = pctOf(H.out[r.dataset.k]), x = r.querySelector(".wp");
        x.className = "wp " + cls(v); x.textContent = fmtPct(v);
      });
      $("dash-wallet-per").textContent = "% " + PER[scale] + " · fecho " + dmy(H.days[Math.max(0, s0)]) + " → " + dmy(H.days[e]);
    };
    // bars: the money in (dark blue), the gain stacked on top (green) or the loss below zero (red), at the end of each period
    const bars = () => {
      $("dash-wallet-what").textContent = pick || "Total";
      if (!window.Plotly || !H) return;
      period();
      const o = H.out[pick] || H.out[""], last = {};
      H.days.forEach((d, i) => { if (d >= from && d <= to) last[keyOf[scale](d)] = i; });
      let keys = Object.keys(last).sort().filter((k) => o.inv[last[k]] || o.val[last[k]]);
      const room = Math.max(10, Math.floor(($("dash-wallet-chart").clientWidth || 600) / 12));  // at least ~12px per bar
      const n = Math.max(1, Math.ceil(keys.length / room));
      const span = {};  // each bar's first period, when it holds several
      if (n > 1) {
        const kept = [];
        for (let i = keys.length; i > 0; i -= n) { kept.unshift(keys[i - 1]); span[keys[i - 1]] = keys[Math.max(0, i - n)]; }
        keys = kept;
      }
      const unit = SCALES.find(([k]) => k === scale)[2];
      if (keys.length) $("dash-wallet-what").textContent = (pick || "Total") + " · " + H.days[last[span[keys[0]] || keys[0]]].split("-").reverse().join("/") + " → " +
        H.days[last[keys[keys.length - 1]]].split("-").reverse().join("/") + (n > 1 ? " · cada barra junta " + n + " " + unit : "");
      if (!keys.length) { $("dash-wallet-chart").innerHTML = '<p class="muted small">Sem histórico nestas datas.</p>'; return; }
      // dd/mm repeats across years: then the year goes in too (Plotly would stack equal labels into one bar)
      const years = new Set(keys.map((k) => k.slice(0, 4))).size > 1 && (scale === "dias" || scale === "semanas");
      const x = keys.map((k) => labelOf[scale](k) + (years ? "/" + k.slice(2, 4) : "")), inv = keys.map((k) => o.inv[last[k]]), val = keys.map((k) => o.val[last[k]]);
      const gain = val.map((v, i) => v - inv[i]);
      const when = keys.map((k) => (span[k] && span[k] !== k ? labelOf[scale](span[k]) + " a " : scale === "semanas" ? "semana de " : "") + labelOf[scale](k) +
        (scale === "dias" ? "" : " (fecho " + H.days[last[k]].slice(8, 10) + "/" + H.days[last[k]].slice(5, 7) + ")"));
      const info = gain.map((g, i) => when[i] + "<br>investido " + fmtEur(inv[i]) + "<br>" + (g >= 0 ? "lucro " : "prejuízo ") + fmtEur(g) + (inv[i] ? " (" + fmtPct((val[i] / inv[i] - 1) * 100, 2) + ")" : "") + "<br>vale " + fmtEur(val[i]));
      $("dash-wallet-chart").innerHTML = "";
      Plotly.newPlot("dash-wallet-chart", showInv ? [
        { x, y: inv.map((v, i) => (gain[i] < 0 ? val[i] : v)), name: "Investido", type: "bar", marker: { color: "#1a56c4", line: { color: "#3d7bff", width: 1 } }, hovertext: info, hoverinfo: "text" },
        { x, y: gain.map((g) => (g > 0 ? g : 0)), name: "Lucro", type: "bar", marker: { color: "#39ff88" }, hoverinfo: "skip" },
        { x, y: gain.map((g) => (g < 0 ? -g : 0)), name: "Prejuízo", type: "bar", marker: { color: "#ff4d6d" }, hoverinfo: "skip" },
      ] : [
        { x, y: gain, name: "Lucro", type: "bar", marker: { color: gain.map((g) => (g < 0 ? "#ff4d6d" : "#39ff88")) }, hovertext: info, hoverinfo: "text" },
      ], { barmode: "relative", bargap: keys.length < 4 ? 0.6 : 0.25, paper_bgcolor: "#17123a", plot_bgcolor: "#17123a", font: { color: "#e8e6ff", size: 11 },
        margin: { l: 56, r: 8, t: 8, b: keys.length > 12 ? 56 : 30 }, showlegend: false, hovermode: "x",
        yaxis: { ticksuffix: " €", gridcolor: "#2e2763", zerolinecolor: "#9b96c9" }, xaxis: { type: "category", tickangle: keys.length > 12 ? -45 : 0, nticks: 14 },
        hoverlabel: { bgcolor: "#17123a", bordercolor: "#3d7bff" } },
        { responsive: true, displaylogo: false, displayModeBar: false });
    };
    $("dash-wallet-scale").addEventListener("click", (e) => {
      const b = e.target.closest("[data-s]");
      if (!b) return;
      scale = b.dataset.s;
      store.set("dash-wallet-scale", scale);
      $("dash-wallet-scale").querySelectorAll(".chip").forEach((c) => c.classList.toggle("on", c.dataset.s === scale));
      bars();
    });
    const choose = (e) => {
      const r = e.target.closest(".wsel");
      if (!r || (e.type === "keydown" && e.key !== "Enter" && e.key !== " ")) return;
      e.preventDefault();
      pick = r.dataset.k;
      $("dash-wallet-tiles").innerHTML = tiles();
      $("dash-wallet-rows").querySelectorAll(".wsel").forEach((x) => x.classList.toggle("on", x.dataset.k === pick));
      bars();
    };
    const range = () => {
      const f = $("dash-wallet-from").value || first, t = $("dash-wallet-to").value || todayIso;
      from = f <= t ? f : t; to = f <= t ? t : f;
      bars();
    };
    $("dash-wallet-inv").addEventListener("change", (e) => {
      showInv = e.target.checked;
      store.set("dash-wallet-inv", showInv);
      $("dash-wallet-leg-inv").hidden = !showInv;
      bars();
    });
    $("dash-wallet-from").addEventListener("change", range);
    $("dash-wallet-to").addEventListener("change", range);
    $("dash-wallet-all").addEventListener("click", () => { $("dash-wallet-from").value = first; $("dash-wallet-to").value = todayIso; range(); });
    let redraw = 0;
    window.addEventListener("resize", () => { clearTimeout(redraw); redraw = setTimeout(bars, 250); });
    $("dash-wallet-rows").addEventListener("click", choose);
    $("dash-wallet-rows").addEventListener("keydown", choose);
    fetch(WALLET + "/history", { cache: "no-store" }).then((r) => r.ok ? r.json() : null).then((h) => {
      H = build(h || { series: {}, fx: {} });
      bars();
    }).catch(() => { H = build({ series: {}, fx: {} }); bars(); });
  }).catch(() => { $("dash-wallet").innerHTML = '<p class="muted">Não consegui ler a carteira agora.</p>'; });
})();

// ---- news ----
(function news() {
  if (!$("news")) return;
  const N = NEWS || {};
  const sentiment = { 2: "🟢 muito positiva", 1: "🟢 positiva", 0: "⚪ neutra", "-1": "🔴 negativa", "-2": "🔴 muito negativa" };
  const ago = (iso) => { const h = (Date.now() - Date.parse(iso)) / 3.6e6; return h < 1 ? Math.max(1, Math.round(h * 60)) + " min" : h < 48 ? Math.round(h) + " h" : Math.round(h / 24) + " dias"; };
  const stars = (n) => "★".repeat(n || 0) + "☆".repeat(3 - (n || 0));
  const react = (i) => (i.r1 != null ? ' · reação: <span class="' + cls(i.r1) + '">' + fmtPct(i.r1) + '</span> no dia seguinte' + (i.r5 != null ? ', <span class="' + cls(i.r5) + '">' + fmtPct(i.r5) + '</span> em 5 sessões' : '') + ' (vs S&P 500)' : '');
  const article = (i) => '<article class="' + (i.sentimento > 0 ? "pos" : i.sentimento < 0 ? "neg" : "") + '"><div><b>' + esc(i.symbol) + '</b>' +
    (i.company ? ' <span class="muted">' + esc(i.company) + '</span>' : "") + (i.sector ? ' <span class="tag">' + esc(i.sector) + '</span>' : '') + ' · ' + (sentiment[i.sentimento] || "⚪") + ' · ' + stars(i.importancia) +
    (i.surpresa ? ' · ⚡ surpresa' : "") + '</div><div>' + esc(i.resumo || i.title) + '</div><div class="meta">' +
    (i.url ? '<a href="' + esc(i.url) + '" target="_blank" rel="noopener noreferrer">' + esc(i.title) + '</a>' : esc(i.title)) +
    ' · ' + esc(i.publisher || "") + ' · há ' + ago(i.published) + ' · lida por ' + esc(i.fonte || "?") + react(i) + '</div></article>';
  const subs = [["agora", "Agora"], ["setores", "Por setor"], ["mudancas", "O que mudou"], ["acao", "Por ação"], ["calendario", "Calendário"], ["precisao", "Precisão"]];
  $("news").innerHTML = '<p class="muted small">' + (N.updated ? 'Lidas às ' + lisbon(Date.parse(N.updated), { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) + ' · ' : '') +
    'ações do ranking, das carteiras dos agentes, do /seguir e as que mais mexeram · arquivo de ' + (N.archive_days || 30) + ' dias</p>' +
    '<div class="chips" id="nsub">' + subs.map(([k, l], i) => '<button class="chip' + (i ? '' : ' on') + '" data-k="' + k + '">' + l + '</button>').join("") + '</div><div id="nbody"></div>';
  const views = {
    agora() {
      if (!(N.items || []).length) return '<p class="muted">Ainda não há notícias lidas.</p>';
      const scores = Object.entries(N.scores || {}).sort((a, b) => b[1] - a[1]);
      setTimeout(() => { ["nf-q", "nf-s", "nf-i"].forEach((id) => $(id).addEventListener("input", drawList)); drawList(); });
      return (scores.length ? '<div class="grid">' + tile("Mais positivas", scores.slice(0, 3).map(([k, v]) => esc(k) + " +" + v).join(" · ") || "—", "score = sentimento × importância (36 h)", "up") +
        tile("Mais negativas", scores.slice(-3).reverse().filter(([, v]) => v < 0).map(([k, v]) => esc(k) + " " + v).join(" · ") || "—", "", "down") + '</div>' : "") +
        '<div class="filters"><input id="nf-q" placeholder="Procurar ação, empresa ou palavra"><select id="nf-s"><option value="">Todos os sentimentos</option><option value="pos">Positivas</option><option value="neg">Negativas</option><option value="neu">Neutras</option></select>' +
        '<select id="nf-i"><option value="0">Toda a importância</option><option value="2" selected>Importância 2+</option><option value="3">Só as materiais (3)</option></select></div><div id="news-list" class="news"></div>';
    },
    setores() {
      return table(["Setor", "Notícias", "Importantes", "Sentimento médio", "Ações"], (N.sectors || []).map((r) => [esc(r.sector), r.count, r.important,
        '<span class="' + cls(r.sentiment) + '">' + (r.sentiment > 0 ? "+" : "") + fmtNum(r.sentiment, 2) + '</span>',
        r.symbols.map((x) => '<a href="#" class="nsym" data-s="' + esc(x) + '">' + esc(x) + '</a>').join(" ")]), [1, 2, 3]) +
        '<p class="muted small">Sentimento de -2 (muito negativo) a +2 (muito positivo), nas últimas 36 horas.</p>';
    },
    mudancas() {
      const c = N.changes || {};
      return '<h3>🔄 Score das notícias: últimas 24 h vs as 24 h anteriores</h3>' + table(["Ação", "Agora", "Antes", "Mudança"], (c.moves || []).map((m) =>
        ['<a href="#" class="nsym" data-s="' + esc(m.symbol) + '">' + esc(m.symbol) + '</a> <span class="muted">' + esc(m.company) + '</span>', m.now, m.before,
         '<span class="' + cls(m.delta) + '">' + (m.delta > 0 ? "+" : "") + m.delta + '</span>']), [1, 2, 3]) +
        '<h3>🆕 Notícias importantes das últimas 24 h</h3><div class="news">' + ((c.new_important || []).map(article).join("") || '<p class="muted">Nenhuma.</p>') + '</div>';
    },
    acao() {
      const syms = Object.keys(N.timeline || {});
      setTimeout(() => { $("ns-sel").addEventListener("input", drawTimeline); drawTimeline(); });
      return '<div class="filters"><select id="ns-sel">' + syms.map((x) => '<option value="' + esc(x) + '"' + (x === pickSym ? ' selected' : '') + '>' + esc(x) + ' (' + N.timeline[x].length + ')</option>').join("") +
        '</select><span class="muted small">Notícias dos últimos 30 dias e como o preço reagiu (descontado o S&P 500).</span></div><div id="ns-stats"></div><div id="ns-chart" style="height:260px"></div><div id="ns-list" class="news"></div>' +
        '<h3>📈 Que ações reagem às notícias</h3><p class="muted small">Só notícias importantes (2+). Movimento médio no dia seguinte e em 5 sessões, em valor absoluto, e quantas vezes foi na direção que o sentimento indicava.</p>' +
        table(["Ação", "Notícias", "Move no dia seguinte", "Move em 5 sessões", "Direção certa (1 dia)", "Direção certa (5)"], (N.reaction || []).map((r) =>
          ['<a href="#" class="nsym" data-s="' + esc(r.symbol) + '">' + esc(r.symbol) + '</a> <span class="muted">' + esc(r.company) + '</span>', r.news, fmtNum(r.avg_abs_r1, 1) + "%",
           r.avg_abs_r5 == null ? "—" : fmtNum(r.avg_abs_r5, 1) + "%", r.hit_r1 == null ? "—" : fmtNum(r.hit_r1 * 100, 0) + "%", r.hit_r5 == null ? "—" : fmtNum(r.hit_r5 * 100, 0) + "%"]), [1, 2, 3, 4, 5]);
    },
    calendario() {
      const C = CALENDAR;
      if (!C) return '<p class="muted">O calendário aparece depois da próxima liga dos agentes.</p>';
      const dayName = (d) => lisbon(Date.parse(d + "T12:00:00Z"), { weekday: "short", day: "2-digit", month: "2-digit" });
      return '<h3>🏛️ Agenda macro (14 dias)</h3><p class="muted small">Fonte: ' + esc(C.source) + '. Os agentes com <code>evita_macro</code> ligado não compram na véspera.</p>' +
        table(["Dia", "Evento"], C.macro.map((e) => [esc(dayName(e.date)), esc(e.name)])) +
        '<h3>📅 Resultados trimestrais (21 dias)</h3><p class="muted small">Das ações do ranking, das carteiras dos agentes e das propostas. Por defeito os agentes não compram ' + C.avoid_days + ' dias antes.</p>' +
        table(["Dia", "Ação", "Na carteira de", "No ranking"], C.earnings.map((e) => [esc(dayName(e.date)), '<b>' + esc(e.symbol) + '</b> <span class="muted">' + esc(e.name) + '</span>',
          esc(e.held_by.join(", ") || "—"), e.ranking ? "🏆" : ""]));
    },
    precisao() {
      const A = N.accuracy || {};
      return '<div class="grid">' + tile("Notícias importantes avaliadas", A.news ?? 0, "nos últimos 30 dias") +
        tile("Direção certa no dia seguinte", A.hit_r1 == null ? "—" : fmtNum(A.hit_r1 * 100, 0) + "%", "50% seria ao acaso", cls((A.hit_r1 || 0.5) - 0.5)) +
        tile("Direção certa em 5 sessões", A.hit_r5 == null ? "—" : fmtNum(A.hit_r5 * 100, 0) + "%", "", cls((A.hit_r5 || 0.5) - 0.5)) +
        tile("Claude vs FinBERT", (A.by_source?.Claude == null ? "—" : fmtNum(A.by_source.Claude * 100, 0) + "%") + " vs " + (A.by_source?.FinBERT == null ? "—" : fmtNum(A.by_source.FinBERT * 100, 0) + "%"), "direção certa no dia seguinte") + '</div>' +
        '<p class="muted small">Uma notícia acerta quando o preço, descontado o S&P 500, se move no sentido do sentimento (positiva → sobe). Mede se vale a pena seguir a leitura das notícias.</p>';
    },
  };
  let pickSym = Object.keys(N.timeline || {})[0];
  function drawList() {
    const q = $("nf-q").value.trim().toLowerCase(), sv = $("nf-s").value, imin = +$("nf-i").value;
    const list = (N.items || []).filter((i) => (i.importancia || 0) >= imin && (!sv || (sv === "pos" ? i.sentimento > 0 : sv === "neg" ? i.sentimento < 0 : !i.sentimento)) &&
      (!q || [i.symbol, i.company, i.title, i.resumo, i.sector].join(" ").toLowerCase().includes(q)));
    $("news-list").innerHTML = list.map(article).join("") || '<p class="muted">Nenhuma notícia com estes filtros.</p>';
  }
  function drawTimeline() {
    pickSym = $("ns-sel").value;
    const xs = (N.timeline || {})[pickSym] || [];
    const withR = xs.filter((i) => i.r1 != null);
    $("ns-stats").innerHTML = '<div class="grid">' + tile("Notícias (30 dias)", xs.length, esc(xs[0]?.company || "")) +
      tile("Sentimento médio", fmtNum(xs.reduce((a, i) => a + (i.sentimento || 0), 0) / Math.max(1, xs.length), 2), "", cls(xs.reduce((a, i) => a + (i.sentimento || 0), 0))) +
      tile("Move médio no dia seguinte", withR.length ? fmtNum(withR.reduce((a, i) => a + Math.abs(i.r1), 0) / withR.length, 1) + "%" : "—", "valor absoluto, vs S&P 500") + '</div>';
    $("ns-list").innerHTML = xs.map(article).join("");
    if (window.Plotly && withR.length) Plotly.newPlot("ns-chart", [{ x: withR.map((i) => i.published.slice(0, 10)), y: withR.map((i) => i.r1), type: "bar",
      marker: { color: withR.map((i) => i.sentimento > 0 ? "#39ff88" : i.sentimento < 0 ? "#ff4d6d" : "#9b96c9") }, text: withR.map((i) => i.resumo || i.title), textposition: "none",
      hovertemplate: "%{text}<br>%{y:+.1f}% no dia seguinte<extra></extra>" }], { paper_bgcolor: "#17123a", plot_bgcolor: "#17123a", font: { color: "#e8e6ff" },
      margin: { l: 44, r: 10, t: 10, b: 30 }, barmode: "group", yaxis: { ticksuffix: "%", gridcolor: "#2e2763" }, xaxis: { gridcolor: "#2e2763" } }, { responsive: true, displaylogo: false });
    else $("ns-chart").innerHTML = '<p class="muted small" style="padding:8px">Ainda sem reações medidas (precisam de pelo menos uma sessão).</p>';
  }
  function show(k) {
    document.querySelectorAll("#nsub .chip").forEach((b) => b.classList.toggle("on", b.dataset.k === k));
    $("nbody").innerHTML = views[k]();
  }
  $("nsub").addEventListener("click", (e) => { const b = e.target.closest(".chip"); if (b) show(b.dataset.k); });
  $("nbody").addEventListener("click", (e) => { const a = e.target.closest(".nsym"); if (a) { e.preventDefault(); pickSym = a.dataset.s; show("acao"); } });
  show("agora");
})();

// ---- the decision: the frozen candidates against the S&P 500 after tax, before any real money ----
(function decision() {
  if (!$("decision")) return;
  const D = DECISION;
  if (!D) { $("decision").innerHTML = '<p class="muted">Aparece depois da próxima liga dos agentes (dias úteis às 07:30).</p>'; return; }
  const pctOrDash = (x) => x == null ? "—" : fmtPct(x * 100);
  const rows = (D.candidatos || []).map((c) => [
    '<b>' + esc(c.emoji + " " + c.name) + '</b><br><span class="muted small">desde ' + esc(c.desde) + ' até ' + esc(c.ate) + '</span>',
    pctOrDash(c.liquido), pctOrDash(c.indice_liquido),
    '<span class="' + cls(c.diferenca_liquida || 0) + '">' + pctOrDash(c.diferenca_liquida) + '</span>',
    fmtPct(-(c.queda_max || 0) * 100) + ' <span class="muted small">(índice ' + fmtPct(-(c.indice_queda_max || 0) * 100) + ')</span>',
    c.ibkr ? '<span class="' + cls(c.ibkr.retorno) + '">' + pctOrDash(c.ibkr.retorno) + '</span><br><span class="muted small">' + c.ibkr.ordens + ' ordens · ' + fmtEur(c.ibkr.custos_eur) + ' de custos</span>' : "—",
    esc(c.veredicto)]);
  const orders = (D.ordens || []).map((o) => [esc(o.emoji + " " + o.agent), '<b>' + esc(o.symbol) + '</b>',
    o.side === "buy" ? "comprar" : "vender" + (o.fraction < 0.999 ? " " + fmtNum(o.fraction * 100, 0) + "%" : ""),
    o.eur != null ? fmtEur(o.eur) : "—", esc(o.reason || "")]);
  $("decision").innerHTML = '<h2>⚖️ Vale a pena pôr dinheiro real?</h2><p class="muted small">Três candidatos congelados durante um ano ' +
    '(parâmetros fixos, sem treino nem júri), medidos ao vivo contra o S&P 500 como numa conta real: o que sobra depois de vender tudo hoje ' +
    'e pagar o imposto. Dados de ' + esc(D.as_of) + '.</p><div class="panel"><p><b>Regra:</b> ' + esc(D.regra) + '</p></div>' +
    table(["Candidato", "Depois de impostos", "S&P 500 (idem)", "Diferença", "Queda máxima", "Na IBKR (simulada)", "Veredicto"], rows, [1, 2, 3]) +
    '<p class="muted small">Na IBKR (simulada): o mesmo candidato numa conta de ' + fmtEur(D.capital_real) + ' na Interactive Brokers, com as regras de lá ' +
    '(ações inteiras, ordem mínima, comissões e câmbio da IBKR), antes de impostos. A diferença para o agente é o que se perde a passar para uma conta real.</p>' +
    '<h2>📝 Ordens de hoje, à escala da tua conta (' + fmtEur(D.capital_real) + ')</h2><p class="muted small">O que cada candidato vai fazer ' +
    'na abertura, se o seguisses com a conta real. Só para replicar à mão se um dia algum cumprir a regra: nada é enviado a nenhuma corretora.</p>' +
    table(["Candidato", "Ativo", "Ordem", "Valor", "Porquê"], orders, [3]);
})();

// ---- proposals: the agents' orders and the buy rules (ex-Oportunidades) in one place ----
(function props() {
  if (!$("props")) return;
  const P = PROPOSALS, M = MARKET;
  if (!P && !M) { $("props").innerHTML = '<p class="muted">As propostas aparecem depois da próxima liga dos agentes (dias úteis às 07:30).</p>'; return; }
  const pctOrDash = (x) => x == null ? "—" : fmtNum(x * 100, 0) + "%";
  const sizing = (b, capital, riskPct) => {
    const pl = b.plan, perShare = (pl.entry_max - pl.stop) * pl.fx_eur;
    if (!(perShare > 0) || !(capital > 0)) return null;
    let qty = Math.floor(capital * riskPct / 100 / perShare * 1000) / 1000;
    const cap = capital * 0.2 / (pl.entry_max * pl.fx_eur);
    const capped = qty > cap; if (capped) qty = Math.floor(cap * 1000) / 1000;
    return { qty, value: qty * pl.entry_max * pl.fx_eur, risk: qty * perShare, capped };
  };
  const propCard = (b) => {
    const pl = b.plan, rec = b.record || {}, cur = ({ EUR: "€", USD: "$", GBp: "p", CHF: "CHF", SEK: "kr", DKK: "kr", NOK: "kr" })[pl.currency] || pl.currency || "";
    return '<div class="opp"><div class="top"><span class="sym">' + esc(b.symbol) + '</span><span class="muted">' + esc(b.name || "") + (b.sector ? " · " + esc(b.sector) : "") + '</span>' +
      (b.safety != null ? '<span class="score ' + esc((b.level || "").split(" ")[0]) + '">🛡️ ' + b.safety + '/100 · ' + esc(b.level) + '</span>' : "") + '</div>' +
      ((b.alerts || []).length ? '<p class="alert ' + (b.conflict === "venda" ? "down" : "warn") + '">' + (b.conflict === "venda" ? "🔴 Contra os sinais de venda" : "🟡 Atenção antes de comprar") + ': ' +
        b.alerts.map(esc).join(" · ") + '. Os agentes seguem a sua estratégia, mas estes sinais pedem cuidado: confirma antes de decidir.</p>' : '') +
      '<p class="small">Confiança dos agentes <b>' + fmtNum(b.confidence, 0) + '%</b> (peso ' + fmtNum(b.support, 1) + ' de ' + fmtNum(P.total_weight, 1) + ')</p><div class="bar"><i style="width:' + Math.min(100, b.confidence) + '%"></i></div>' +
      '<p>' + b.agents.map((a) => '<span class="tag">' + esc(a.emoji + " " + a.name) + ' · peso ' + fmtNum(a.weight, 1) + (a.reason ? ' · ' + esc(a.reason) : '') + '</span>').join("") + '</p>' +
      '<div class="grid">' + tile("Entrada até", fmtNum(pl.entry_max) + " " + cur, "fecho " + fmtNum(pl.price) + " " + cur) +
      tile("Stop", fmtNum(pl.stop) + " " + cur, "-" + fmtNum(pl.risk_pct, 1) + "% · 2× a variação diária média", "down") +
      tile("Alvo", fmtNum(pl.target) + " " + cur, "+" + fmtNum(pl.gross_pct, 1) + "% · risco/retorno " + fmtNum(pl.rr, 1), "up") +
      tile("Líquido no alvo", fmtPct(pl.net_target_pct), "depois de custos e 28% de imposto", "up") +
      tile("Líquido no stop", fmtPct(pl.net_stop_pct), "com custos", "down") +
      (b.odds && b.odds.needed != null ? tile("Para empatar", pctOrDash(b.odds.needed) + " de acerto",
        b.odds.verdict ? "historial dos agentes " + pctOrDash(b.odds.record) + ": " + esc(b.odds.verdict) : "acima disto, o plano ganha dinheiro em média") : "") +
      tile("Historial dos agentes", rec.trades ? pctOrDash(rec.win_rate) + " de acerto" : "ainda sem vendas", rec.trades ? "ganho médio " + fmtPct(rec.avg_win * 100) + " · perda média " + fmtPct(rec.avg_loss * 100) + (rec.median_days ? " · ~" + rec.median_days + " dias" : "") : "") +
      '</div><p class="small sizing" data-sym="' + esc(b.symbol) + '"></p>' +
      (b.earnings ? '<p class="small">📅 Resultados a ' + esc(b.earnings) + '</p>' : '') + (b.news ? '<p class="small muted">📰 ' + esc(b.news) + '</p>' : '') +
      ((b.parts || []).length ? '<details><summary>Score de segurança: como chegou a ' + b.safety + '</summary>' + table(["Critério", "Pontos", "Porquê"], b.parts.map((x) => [esc(x.label), fmtNum(x.points, 1) + " / " + x.max, esc(x.why)]), [1]) + '</details>' : '') + '</div>';
  };
  const badge = (r) => '<span class="score ' + esc(r.level.split(" ")[0]) + '">🛡️ ' + r.safety + '/100 · ' + esc(r.level) + '</span>';
  const signals = (r) => '<span class="score ' + (r.severity >= 3 ? "Baixa" : "Média") + '">⚠️ ' + r.warnings.length + ' sina' + (r.warnings.length === 1 ? "l" : "is") + '</span>';
  const oppCard = (r, kind) => '<div class="opp' + (kind === "sell" ? " sellcard" : kind === "watch" ? " watchcard" : "") + '"><div class="top"><span class="sym">' + esc(r.symbol) + '</span><span class="muted">' + esc(r.name || "") +
    (r.sector ? " · " + esc(r.sector) : "") + '</span>' + (kind === "buy" ? badge(r) : signals(r)) + '</div>' + (kind === "buy" ? '<div class="bar"><i style="width:' + r.safety + '%"></i></div>' : '') +
    '<p class="small">' + fmtNum(r.price) + " " + (({ EUR: "€", USD: "$", GBp: "p", CHF: "CHF", SEK: "kr", DKK: "kr", NOK: "kr" })[r.currency || (r.etf ? "EUR" : "USD")] || r.currency) + ' · dia <span class="' + cls(r.ret_1d) + '">' + fmtPct(r.ret_1d) + '</span> · semana <span class="' + cls(r.ret_1w) + '">' +
    fmtPct(r.ret_1w) + '</span> · 3 meses <span class="' + cls(r.ret_3m) + '">' + fmtPct(r.ret_3m) + '</span> · tendência ' + r.trend_score + '/100</p>' +
    '<p>' + (kind === "buy" ? r.reasons : r.warnings).map((x) => '<span class="tag">' + esc(x) + '</span>').join("") + '</p>' +
    (r.fund && r.fund.fund_q != null ? '<p class="small">💰 ' + [r.fund.pe ? "P/E " + fmtNum(r.fund.pe, 0) : null,
      r.fund.fcf_yield != null ? "FCF yield " + fmtPct(r.fund.fcf_yield * 100) : null, r.fund.mos != null ? "margem de segurança " + fmtPct(r.fund.mos * 100, 0) : null,
      r.fund.rev_growth != null ? "receitas " + fmtPct(r.fund.rev_growth * 100, 0) : null, r.fund.op_margin != null ? "margem operacional " + fmtPct(r.fund.op_margin * 100, 0) : null,
      "qualidade " + fmtNum(r.fund.fund_q, 0) + "/100", r.fund.fund_v != null ? "valor " + fmtNum(r.fund.fund_v, 0) + "/100" : null].filter(Boolean).join(" · ") + '</p>' : '') +
    (kind === "buy" && (r.checks || []).length ? '<details><summary>Porque passou nos filtros</summary><p class="small">' + r.checks.map(esc).join("<br>") + '</p></details>' : '') +
    '<p class="small">' + (kind === "buy" ? '🛑 Stop sugerido ' + fmtNum(r.stop) + ' (risco ' + fmtNum(r.risk_pct, 1) + '%)' : kind === "sell" ? '🔴 Quem tem: considerar vender ou apertar o stop.' : '🟡 Não comprar agora; quem tem pode manter com stop.') +
    (r.earnings ? ' · 📅 resultados ' + esc(r.earnings) : '') + (r.held_by.length ? ' · 🤖 na carteira de ' + esc(r.held_by.join(", ")) : '') + '</p>' +
    (r.news ? '<p class="small muted">📰 ' + esc(r.news) + '</p>' : '') +
    (kind === "buy" ? '<details><summary>Como chegou a ' + r.safety + ' pontos</summary>' +
      table(["Critério", "Pontos", "Porquê"], r.parts.map((x) => [esc(x.label), fmtNum(x.points, 1) + " / " + x.max, esc(x.why)]), [1]) + '</details>' : '') + '</div>';
  const rules = (list) => list && list.length ? '<details><summary>Regras desta lista</summary><ul class="small">' + list.map((x) => '<li>' + esc(x) + '</li>').join("") + '</ul></details>' : '';
  const buys = P ? P.buys : [], mBuy = M ? M.buy : [], R = (M && M.rules) || {};
  const passes = (b) => b.regras != null ? b.regras : mBuy.some((r) => r.symbol === b.symbol);  // before the flag existed
  const both = buys.filter(passes), agentsOnly = buys.filter((b) => !passes(b));
  const rulesOnly = mBuy.filter((r) => !buys.some((b) => b.symbol === r.symbol));
  const failNote = (b) => {
    const why = (b.falhas && b.falhas.length ? b.falhas : b.alerts || []);
    return '<p class="alert warn">❌ Não passa nas regras' + (why.length ? ': ' + why.map(esc).join(" · ") : '') + '</p>';
  };
  const sb = P ? (P.scoreboard || {}) : {}, all = sb.all || {}, byBucket = sb.by_bucket || {}, byRules = sb.by_rules || {};
  const sbRow = (label, x) => [label, x.proposals ?? 0, x.closed ?? 0, pctOrDash(x.win_rate), x.avg_r == null ? "—" : fmtNum(x.avg_r, 2), x.avg_ret == null ? "—" : fmtPct(x.avg_ret)];
  const asOf = (P && P.as_of) || (M && M.as_of) || "", zone = (P && P.zone) || (M && M.zone) || "";
  $("props").innerHTML = '<p class="muted small">Fecho de ' + esc(asOf) + ' · mercado em zona <b>' + esc(zone) + '</b>' +
    (M ? ' (' + fmtNum(M.stage2_pct, 0) + '% das ações do S&P 500 em tendência de alta)' : '') +
    '. Duas leituras de cada ação, lado a lado: o que os <b>agentes</b> decidiram para a abertura (pesados pelo historial de cada um) e se passa nas <b>regras de compra</b> ' +
    '(tendência, sinal de entrada, resultados, notícias, stop e score de segurança). Para estudar e decidir tu: não é recomendação.</p>' +
    '<div class="panel filters"><label class="small">O teu capital (€) <input id="pp-cap" type="number" min="0" step="100"></label>' +
    '<label class="small">Risco por operação (%) <input id="pp-risk" type="number" min="0.1" max="5" step="0.1"></label>' +
    '<span class="muted small">Quantas ações: risco em euros ÷ (entrada − stop), no máximo 20% do capital numa posição. Fica guardado neste aparelho.</span></div>' +
    '<h2>🟢 Comprar</h2>' +
    '<h3>✅ Agentes e regras de acordo (' + both.length + ')</h3><div class="cards">' + (both.map(propCard).join("") || '<p class="muted">Hoje nenhuma.</p>') + '</div>' +
    '<h3>🤖 Só os agentes (' + agentsOnly.length + ')</h3><p class="muted small">Os agentes querem comprar, mas a ação falha alguma regra: o aviso diz qual. Confirma antes de decidir.</p>' +
    '<div class="cards">' + (agentsOnly.map((b) => propCard(b).replace('<div class="grid">', failNote(b) + '<div class="grid">')).join("") || '<p class="muted">Nenhuma.</p>') + '</div>' +
    (M ? '<h3>🛡️ Só as regras (' + rulesOnly.length + ')</h3><p class="muted small">Passam em todas as regras de compra, mas nenhum agente as quer hoje. Poucas e boas; há dias sem nenhuma.</p>' + rules(R.buy) +
      '<div class="cards">' + (rulesOnly.map((r) => oppCard(r, "buy")).join("") || '<p class="muted">Nenhuma.</p>') + '</div>' : '') +
    '<h2>🔴 A evitar / a vender</h2>' +
    (P ? '<h3>Os agentes estão a vender (' + P.sells.length + ')</h3>' + table(["Ativo", "Quem vende", "Porquê", "Peso", "Score"], P.sells.map((x) =>
      ['<b>' + esc(x.symbol) + '</b> <span class="muted">' + esc(x.name || "") + '</span>', x.agents.map((a) => esc(a.emoji + " " + a.name)).join(", "),
       esc([...new Set(x.agents.map((a) => a.reason).filter(Boolean))].join(" · ")), fmtNum(x.support, 1), x.safety != null ? x.safety + "/100" : "—"]), [3]) : '') +
    (M ? '<h3>Tendência quebrada (' + M.sell.length + ')</h3><p class="muted small">Ações dos agentes ou do ranking cuja tendência quebrou: quem tem, considerar vender ou apertar o stop.</p>' + rules(R.sell) +
      '<div class="cards">' + (M.sell.map((r) => oppCard(r, "sell")).join("") || '<p class="muted">Nenhuma.</p>') + '</div>' +
      '<h3>🟡 Atenção: não comprar agora (' + (M.watch || []).length + ')</h3><p class="muted small">Ações fortes com um motivo para esperar: resultados à porta, notícias negativas, perderam a média de 50 dias ou estão esticadas. Não são sinais de venda.</p>' + rules(R.watch) +
      '<div class="cards">' + ((M.watch || []).map((r) => oppCard(r, "watch")).join("") || '<p class="muted">Nenhuma.</p>') + '</div>' : '') +
    (P ? '<h2>📊 Placar das propostas</h2><p class="muted small">Cada proposta de compra é seguida: conta se a ação abriu até ao preço de entrada nas ' + P.method.fill_window +
      ' sessões seguintes, e depois se tocou primeiro no stop ou no alvo; ao fim de ' + P.method.horizon + ' sessões fecha ao preço do dia.</p>' +
      '<div class="grid">' + tile("Propostas", all.proposals ?? 0, (all.executed ?? 0) + " executadas · " + (all.open ?? 0) + " abertas") +
      tile("Taxa de acerto", pctOrDash(all.win_rate), (all.targets ?? 0) + " alvos · " + (all.stops ?? 0) + " stops", cls((all.win_rate || 0) - 0.5)) +
      tile("R médio", all.avg_r == null ? "—" : fmtNum(all.avg_r, 2) + " R", "ganho por unidade de risco", cls(all.avg_r)) +
      tile("Retorno médio", all.avg_ret == null ? "—" : fmtPct(all.avg_ret), "por proposta fechada", cls(all.avg_ret)) + '</div>' +
      '<p class="muted small"><b>As regras acrescentam alguma coisa?</b> As propostas que também passavam nas regras contra as que não passavam (contadas desde 06/11/2026).</p>' +
      table(["Propostas", "Total", "Fechadas", "Acerto", "R médio", "Retorno médio"], [sbRow("✅ passavam nas regras", byRules.passa || {}), sbRow("🤖 só os agentes", byRules.falha || {})], [1, 2, 3, 4, 5]) +
      table(["Confiança", "Total", "Fechadas", "Acerto", "R médio", "Retorno médio"], ["alta", "média", "baixa"].map((k) =>
        sbRow(k + (k === "alta" ? " (≥40%)" : k === "média" ? " (20-40%)" : " (<20%)"), byBucket[k] || {})), [1, 2, 3, 4, 5]) +
      '<details><summary>Últimas propostas e o que lhes aconteceu</summary>' + table(["Data", "Ativo", "Confiança", "Regras", "Estado", "Compra", "Saída", "Resultado"],
        (sb.recent || []).map((e) => [esc(e.date), '<b>' + esc(e.symbol) + '</b>', fmtNum(e.confidence, 0) + "%", e.regras == null ? "—" : e.regras ? "✅" : "❌", esc(e.status),
          e.fill != null ? fmtNum(e.fill) : "—", e.exit != null ? fmtNum(e.exit) : (e.last != null ? fmtNum(e.last) + " (aberta)" : "—"),
          '<span class="' + cls(e.ret ?? e.ret_open) + '">' + (e.ret != null ? fmtPct(e.ret * 100) + (e.r != null ? " · " + fmtNum(e.r, 1) + " R" : "") : e.ret_open != null ? fmtPct(e.ret_open * 100) : "—") + '</span>']), [2, 5, 6, 7]) + '</details>' +
      '<h2>⚖️ Peso de cada agente hoje</h2>' + table(["Agente", "Peso", "Porquê", "Historial"], P.weights.map((w) => [esc(w.emoji + " " + w.name), fmtNum(w.weight, 2),
        esc(w.why.join(" · ") || "—"), w.stats && w.stats.trades ? pctOrDash(w.stats.win_rate) + " acerto em " + w.stats.trades + " vendas" : "—"]), [1]) +
      '<details class="panel"><summary><b>Como se calcula o peso</b></summary><ul class="small">' + P.rules.map((r) => '<li>' + esc(r) + '</li>').join("") + '</ul></details>' : '') +
    (M ? '<details class="panel"><summary><b>🛡️ Como se calcula o score de segurança (0-100)</b></summary><p class="small">Mede quão ordenada e suportada está a ação agora: tendência, volatilidade, distância ao máximo, liquidez, qualidade da empresa, resultados, notícias e mercado. ' +
      'Não diz quanto vai subir. Níveis: ' + M.levels.map((l) => esc(l.name) + " ≥ " + l.from).join(" · ") + '.</p>' +
      table(["Critério", "Máx.", "Regra"], M.criteria.map((c) => [esc(c.label), c.max, esc(c.rule)]), [1]) + '</details>' +
      ((M.sectors || []).length ? '<details class="panel"><summary><b>🏭 Saúde dos setores</b> <span class="muted small">(ETF de cada setor)</span></summary>' +
        table(["Setor", "ETF", "Face à média de 200 dias", "Força vs S&P 500 (3 meses)", "Ações em tendência de alta"], M.sectors.map((r) => [esc(r.sector), esc(r.etf),
          r.trend == null ? "—" : '<span class="' + cls(r.trend) + '">' + fmtPct(r.trend) + '</span>', r.rs == null ? "—" : '<span class="' + cls(r.rs) + '">' + fmtPct(r.rs) + '</span>',
          r.stage2 == null ? "—" : fmtNum(r.stage2, 0) + "%"]), [2, 3, 4]) +
        '<p class="muted small">Só se propõem compras em setores acima da média de 200 dias.</p></details>' : '') : '');
  const cap = $("pp-cap"), risk = $("pp-risk");
  cap.value = store.get("capital", 10000); risk.value = store.get("risk", 1);
  const drawSizes = () => {
    store.set("capital", +cap.value); store.set("risk", +risk.value);
    document.querySelectorAll(".sizing").forEach((el) => {
      const b = buys.find((x) => x.symbol === el.dataset.sym), z = b && sizing(b, +cap.value, +risk.value);
      el.innerHTML = z ? '🧮 Para ti: <b>' + fmtNum(z.qty, 3) + ' ações</b> ≈ ' + fmtEur(z.value) + ', arriscando ' + fmtEur(z.risk) + (z.capped ? ' (limitado a 20% do capital)' : '') : "";
    });
  };
  [cap, risk].forEach((el) => el.addEventListener("input", drawSizes));
  [cap, risk].forEach((el) => el.addEventListener("change", () => {  // signed in: the account keeps them too
    if (+cap.value >= 0 && +risk.value > 0 && +risk.value <= 5) savePrefs({ capital: +cap.value, risk: +risk.value }).catch(() => {});
  }));
  drawSizes();
})();

// ---- race ----
function race(P, DATA, people, pick) {
  const id = (x) => $(P + x);
  const runners = (DATA.series || []).filter((s) => s.x.length);
  if (!runners.length) { id("race").style.display = "none"; return; }
  const dates = [...new Set(runners.flatMap((s) => s.x))].sort();
  const step = Math.max(1, Math.ceil(dates.length / 240));  // at most ~240 frames
  const frames = dates.filter((_, i) => i % step === 0 || i === dates.length - 1);
  // value of each runner on each frame: its last value on or before that date, null before it started
  const values = runners.map((s) => {
    let j = -1;
    return frames.map((d) => { while (j + 1 < s.x.length && s.x[j + 1] <= d) j++; return j < 0 ? null : s.y[j]; });
  });
  const all = values.flat().filter((v) => v != null);
  const lo = Math.min(0, ...all), hi = Math.max(...all), span = hi - lo || 1;
  const colours = ["#00e5ff", "#ffe14d", "#ff2e88", "#39ff88", "#b388ff", "#ff9f43", "#4dd0e1", "#f06292", "#aed581", "#ffd54f",
                   "#64b5f6", "#e57373", "#81c784", "#ba68c8", "#4db6ac", "#fff176"];
  const byPerson = Object.fromEntries(people.map((a) => [a.name, a]));
  const agentOf = (name) => byPerson[name];
  id("track").innerHTML = runners.map((s, i) => {
    const a = agentOf(s.name);
    return '<div class="lane"><div class="no">' + (i + 1) + '</div><div class="run"><div class="racer" id="' + P + 'racer-' + i + '" style="--c:' +
      colours[i % colours.length] + '" title="' + esc(s.name) + '"><span class="avatar">' + esc(a ? a.emoji : "🏇") + '</span><span class="bib">' +
      (i + 1) + '</span><span class="tag"></span></div></div><div class="finish"></div></div>';
  }).join("");
  runners.forEach((s, i) => id("racer-" + i).addEventListener("click", () => { const a = agentOf(s.name); if (a) show(agents.indexOf(a), true); }));
  const scrub = id("race-scrub");
  scrub.max = frames.length - 1;
  const fmtDay = (d) => d.slice(8, 10) + "/" + d.slice(5, 7) + "/" + d.slice(0, 4);

  function draw(f) {
    const now = runners.map((s, i) => ({ s, i, v: values[i][f] })).filter((r) => r.v != null).sort((a, b) => b.v - a.v);
    runners.forEach((s, i) => {
      const v = values[i][f], el = id("racer-" + i), x = v == null ? 0 : (v - lo) / span;
      el.style.left = (3 + 92 * x) + "%";
      el.classList.toggle("left-tag", x > 0.7);
      el.classList.toggle("dead", v == null);
      const place = now.findIndex((r) => r.i === i);
      el.querySelector(".tag").textContent = v == null ? "" : (place + 1) + "º " + fmtPct(v);
      const crown = el.querySelector(".crown");
      if (place === 0 && !crown) el.insertAdjacentHTML("afterbegin", '<span class="crown">👑</span>');
      if (place !== 0 && crown) crown.remove();
    });
    id("race-date").textContent = fmtDay(frames[f]);
    id("race-leader").textContent = now.length ? (agentOf(now[0].s.name)?.emoji || "") + " " + now[0].s.name : "—";
    id("board").innerHTML = now.map((r, k) => {
      const a = agentOf(r.s.name);
      return '<li data-name="' + esc(r.s.name) + '"><span class="pos">' + (k + 1) + 'º</span><span>' + esc(a ? a.emoji : "🏇") + '</span><span class="nm">' +
        esc(r.s.name) + '</span>' + (a && a.points != null ? '<span class="muted small">⭐ ' + fmtNum(a.points, 0) + '</span>' : "") +
        '<span class="val ' + cls(r.v) + '">' + fmtPct(r.v) + '</span></li>';
    }).join("");
    scrub.value = f;
  }
  id("board").addEventListener("click", (e) => { const li = e.target.closest("li"); const a = li && agentOf(li.dataset.name); if (a) pick(a); });

  let timer = null, speed = 1, frame = frames.length - 1;
  const stop = () => { clearInterval(timer); timer = null; id("track").classList.remove("running"); id("race-play").textContent = "▶ VER A CORRIDA"; };
  const tick = () => { if (frame >= frames.length - 1) return stop(); frame++; draw(frame); };
  id("race-play").addEventListener("click", () => {
    if (timer) return stop();
    if (frame >= frames.length - 1) frame = 0;
    draw(frame);
    id("track").classList.add("running");
    id("race-play").textContent = "⏸ PAUSA";
    timer = setInterval(tick, Math.max(40, 12000 / frames.length) / speed);
  });
  id("race-speed").addEventListener("click", () => {
    speed = speed >= 4 ? 1 : speed * 2;
    id("race-speed").textContent = speed + "×";
    if (timer) { clearInterval(timer); timer = setInterval(tick, Math.max(40, 12000 / frames.length) / speed); }
  });
  scrub.addEventListener("input", () => { stop(); frame = +scrub.value; draw(frame); });
  draw(frame);
}
race("", DATA, agents, (a) => show(agents.indexOf(a), true));

// ---- pre-training race ----
if (PRETRAIN && $("pt-race")) {
  const ptAgents = PRETRAIN.agents || [];
  let markPt = () => {};
  const showPt = (a, scroll) => {
    markPt(ptAgents.indexOf(a));
    $("pt-detail").innerHTML = '<div class="panel"><h2>' + esc(a.emoji + " " + a.name) + '</h2>' + (a.backtest ? backtestHtml(a) : "") + changesHtml(a) + strategyHtml(a.strategy) + '</div>';
    chapters($("pt-detail").firstElementChild);
    if (scroll) $("pt-detail").scrollIntoView({ behavior: "smooth", block: "start" });
  };
  markPt = agentPicker("pt-chips", ptAgents, (a) => a.backtest?.return_pct, (i) => showPt(ptAgents[i], true),
    (a) => ({ conservador: "🛡️ Perfil conservador", arrojado: "🔥 Perfil arrojado" })[a.perfil] || "⚖️ Perfil equilibrado");
  race("pt-", PRETRAIN, ptAgents, (a) => showPt(a, true));
  lineChart("pt-chart", PRETRAIN);
  if (ptAgents.length) showPt(ptAgents.slice().sort((x, y) => (y.backtest?.return_pct ?? 0) - (x.backtest?.return_pct ?? 0))[0], false);
}
`;

STATIC_PATHS.css = staticAsset("style", CSS + ADMIN_CSS, "css");
STATIC_PATHS.admin = staticAsset("admin", ADMIN_CLIENT, "js");
STATIC_PATHS.i18n = staticAsset("i18n", I18N_JS, "js");
STATIC_PATHS.portal = staticAsset("portal", CLIENT, "js");

// Search, status and guide: a separate script, so they work even if a section above fails on odd data.
const EXTRA = String.raw`// ---- evolution of the agents between pre-training runs ----
(function evolutionChart() {
  const box = $("evo"), E = EVOLUTION;
  if (!box) return;
  if (!E || !(E.runs || []).length) { box.innerHTML = '<p class="muted">O histórico aparece depois da próxima publicação do pré-treino.</p>'; return; }
  const people = {};
  ((PRETRAIN && PRETRAIN.agents) || []).concat(agents || []).forEach((a) => { if (a && a.id) people[a.id] = a; });
  const label = (s) => people[s] ? people[s].emoji + " " + people[s].name : s;
  const metrics = [["exam", "Pontos no exame (último ano, fora do treino)"], ["ret", "Rentabilidade em 20 anos (%)"], ["dd", "Queda máxima em 20 anos (%)"], ["sharpe", "Sharpe em 20 anos"]];
  const profiles = [["", "Todos os perfis"], ["equilibrado", "⚖️ Equilibrado"], ["conservador", "🛡️ Conservador"], ["arrojado", "🔥 Arrojado"]];
  const profileOf = (s) => (/-(conservador|arrojado)$/.exec(s) || [, "equilibrado"])[1];
  box.innerHTML = '<p class="muted small">Cada ponto é uma execução do pré-treino de um perfil (💻 = no PC; 🛡️ conservador, 🔥 arrojado). Cada perfil é um agente próprio. Uma execução só troca os parâmetros de um agente quando encontra melhores no treino e no exame em conjunto; o que mudou está na ficha de cada agente, em baixo.</p>' +
    '<div class="filters"><select id="evo-m">' + metrics.map(([k, l]) => '<option value="' + k + '">' + esc(l) + '</option>').join("") + '</select>' +
    '<select id="evo-p">' + profiles.map(([k, l]) => '<option value="' + k + '">' + esc(l) + '</option>').join("") + '</select>' +
    '<select id="evo-n"><option value="30">Últimas 30 execuções</option><option value="60">Últimas 60 execuções</option><option value="0">Todas as execuções</option></select></div>' +
    '<div id="evo-chart" class="chart" style="height:420px"></div>';
  const draw = () => {
    const m = $("evo-m").value, p = $("evo-p").value, n = +$("evo-n").value;
    // the runs on the axis: those of the chosen profile (all of them for "Todos"), the latest n
    let idx = E.runs.map((r, i) => i).filter((i) => !p || (E.runs[i].perfil || "equilibrado") === p);
    if (n) idx = idx.slice(-n);
    const x = idx.map((i) => fmtRun(E.runs[i], i));
    const traces = Object.entries(E.series).filter(([s]) => (s !== "indexador" || m !== "exam") && (!p || s === "indexador" || profileOf(s) === p))
      .map(([s, rows]) => [s, idx.map((i) => (rows[i] ? rows[i][m] : null))]).filter(([, y]) => y.some((v) => v != null)).map(([s, y]) => ({
      x, y, name: label(s), type: "scatter", mode: "lines+markers", connectgaps: true,
      hovertemplate: "%{y}<extra>" + esc(label(s)) + "</extra>" }));
    if (!window.Plotly) { $("evo-chart").innerHTML = '<p class="muted">Gráfico indisponível.</p>'; return; }
    Plotly.newPlot("evo-chart", traces, { paper_bgcolor: "#17123a", plot_bgcolor: "#17123a", font: { color: "#e8e6ff", size: 11 },
      margin: { l: 48, r: 10, t: 10, b: 60 }, xaxis: { gridcolor: "#2e2763", tickangle: -30 }, yaxis: { gridcolor: "#2e2763", zerolinecolor: "#80848e" },
      legend: window.innerWidth < 640 ? { orientation: "h", y: -0.35 } : { bgcolor: "rgba(0,0,0,0)" } }, { responsive: true, displaylogo: false });
  };
  $("evo-m").addEventListener("input", draw);
  $("evo-p").addEventListener("input", draw);
  $("evo-n").addEventListener("input", draw);
  draw();
})();

// ---- the logo and the title go to the main page (the Dashboard), without reloading when already on it ----
document.addEventListener("click", (e) => {
  const a = e.target.closest("a.home");
  if (!a || a.getAttribute("href") !== location.pathname) return;
  e.preventDefault();
  openTab(document.querySelector("#tabs .tab").dataset.tab);
  window.scrollTo(0, 0);
});

// ---- settings kept in the account (edited on /perfil): the proposals' capital and risk ----
async function savePrefs(fields) {
  if (!WALLET) return null;
  const r = await fetch(WALLET + "/data", { method: "POST", headers: { "Content-Type": "application/json" },
                                           body: JSON.stringify({ action: "preferences", ...fields }) });
  const out = await r.json().catch(() => ({}));
  if (!r.ok || out.error) throw new Error(out.error || "Não consegui guardar. Tenta outra vez.");
  return out.settings;
}

// ---- ticker search: everything the portal knows about one ticker, and the member's own position ----
(function search() {
  if (!$("search")) return;
  const N = NEWS || {}, M = MARKET || {}, P = PROPOSALS || {}, C = CALENDAR || {};
  const base = (x) => String(x || "").toUpperCase().split(".")[0];
  const names = {};
  const add = (sym, name) => { if (sym && !names[sym]) names[sym] = name || ""; };
  (M.buy || []).concat(M.sell || [], M.watch || []).forEach((r) => add(r.symbol, r.name));
  (P.buys || []).forEach((b) => add(b.symbol, b.name)); (P.sells || []).forEach((x) => add(x.symbol, ""));
  Object.keys(N.timeline || {}).forEach((k) => add(k, ((N.timeline[k] || [])[0] || {}).company));
  agents.forEach((a) => (a.positions || []).forEach((p) => add(p.symbol, p.name)));
  (C.earnings || []).forEach((e) => add(e.symbol, e.name));
  $("q-list").innerHTML = Object.keys(names).sort().map((k) => '<option value="' + esc(k) + '">' + esc(names[k] || "") + '</option>').join("");
  let wallet = null;
  const walletPath = store.get("carteira", null);
  const loadWallet = () => wallet || (walletPath && /^\/c\/[0-9a-f]{24}$/.test(walletPath)
    ? (wallet = fetch(walletPath + "/data", { cache: "no-store" }).then((r) => r.ok ? r.json() : null).catch(() => null)) : Promise.resolve(null));
  const same = (a, b) => a && b && (a.toUpperCase() === b.toUpperCase() || base(a) === base(b));
  async function run(raw) {
    const q = String(raw || "").trim().toUpperCase();
    if (!q) return;
    const out = $("q-out");
    const mk = (M.buy || []).map((r) => [r, "buy"]).concat((M.sell || []).map((r) => [r, "sell"]), (M.watch || []).map((r) => [r, "watch"])).filter(([r]) => same(r.symbol, q));
    const pb = (P.buys || []).filter((b) => same(b.symbol, q)), ps = (P.sells || []).filter((x) => same(x.symbol, q));
    const held = agents.flatMap((a) => (a.positions || []).filter((p) => same(p.symbol, q)).map((p) => [a, p]));
    const newsKey = Object.keys(N.timeline || {}).find((k) => same(k, q));
    const items = newsKey ? N.timeline[newsKey] : [];
    const earn = (C.earnings || []).filter((e) => same(e.symbol, q));
    const name = names[Object.keys(names).find((k) => same(k, q))] || "";
    let html = '<h2>🔎 ' + esc(q) + (name ? ' <span class="muted small">' + esc(name) + '</span>' : '') + '</h2>';
    const w = await loadWallet();
    if (w && w.tx) {
      const mine = w.tx.filter((t) => same(t.symbol, q));
      if (mine.length) {
        let qty = 0, cost = 0;
        mine.slice().sort((a, b) => a.date.localeCompare(b.date)).forEach((t) => {
          if (t.side === "buy") { qty += t.qty; cost += t.qty * t.price + (t.fee || 0); } else { const avg = qty ? cost / qty : 0; cost -= avg * t.qty; qty -= t.qty; } });
        const sym = mine[0].symbol, quote = (w.quotes || {})[sym] || {}, v = (w.verdicts || {})[sym] || {};
        html += '<div class="panel"><h3>💼 A tua posição</h3><div class="grid">' + tile("Quantidade", fmtNum(qty, 4)) +
          tile("Custo médio", qty > 0 ? fmtNum(cost / qty) : "—") + tile("Preço", fmtNum(quote.price), quote.currency || "") +
          (qty > 0 && quote.price ? tile("Ganho", fmtPct((quote.price / (cost / qty) - 1) * 100), "", cls(quote.price - cost / qty)) : "") +
          (v.verdict ? tile("Veredito", esc(v.verdict), (v.reasons || []).slice(0, 2).map(esc).join(" · ")) : "") + '</div>' +
          '<p class="small"><a href="' + esc(walletPath) + '">Abrir a minha carteira →</a></p></div>';
      }
    } else if (!walletPath) {
      html += '<p class="muted small">Para ver aqui a tua posição, abre a tua carteira uma vez neste aparelho (separador 💼 Carteira).</p>';
    }
    if (mk.length) html += '<div class="panel"><h3>🛡️ Score de segurança</h3>' + mk.map(([r, kind]) =>
      '<p><span class="score ' + esc((r.level || "").split(" ")[0]) + '">🛡️ ' + r.safety + '/100 · ' + esc(r.level) + '</span> ' + (kind === "buy" ? "🟢 oportunidade de compra" : kind === "sell" ? "🔴 sinais de venda" : "🟡 atenção: não comprar agora") +
      ' · ' + fmtNum(r.price) + ' · 3 meses <span class="' + cls(r.ret_3m) + '">' + fmtPct(r.ret_3m) + '</span></p><p>' + (kind === "buy" ? r.reasons : r.warnings).map((x) => '<span class="tag">' + esc(x) + '</span>').join("") + '</p>' +
      (kind === "buy" && r.stop ? '<p class="small">🛑 Stop sugerido ' + fmtNum(r.stop) + '</p>' : '')).join("") + '<p class="small"><a href="#propostas" data-go="propostas">Ver em Propostas →</a></p></div>';
    if (pb.length || ps.length) html += '<div class="panel"><h3>🧠 Propostas dos agentes</h3>' + pb.map((b) => '<p>🟢 <b>Comprar</b> · confiança ' + fmtNum(b.confidence, 0) + '% · entrada até ' + fmtNum(b.plan.entry_max) +
      ' · stop ' + fmtNum(b.plan.stop) + ' · alvo ' + fmtNum(b.plan.target) + '</p><p>' + b.agents.map((a) => '<span class="tag">' + esc(a.emoji + " " + a.name) + '</span>').join("") + '</p>').join("") +
      ps.map((x) => '<p>🔴 <b>A vender</b>: ' + x.agents.map((a) => esc(a.emoji + " " + a.name)).join(", ") + '</p>').join("") + '<p class="small"><a href="#propostas" data-go="propostas">Ver em Propostas →</a></p></div>';
    if (held.length) html += '<div class="panel"><h3>🤖 Nas carteiras dos agentes</h3>' + table(["Agente", "Porquê", "Desde"], held.map(([a, p]) =>
      [esc(a.emoji + " " + a.name), esc(p.reason || "—"), esc(p.since || p.date || "—")])) + '</div>';
    if (earn.length) html += '<div class="panel"><h3>📅 Resultados</h3><p>' + earn.map((e) => esc(e.date)).join(" · ") + '</p></div>';
    if (items.length) html += '<div class="panel"><h3>📰 Notícias (' + items.length + ')' + (N.scores && N.scores[newsKey] != null ? ' · score ' + N.scores[newsKey] : '') + '</h3>' +
      items.slice(0, 10).map((i) => '<p class="small">' + (i.sentimento > 0 ? "🟢" : i.sentimento < 0 ? "🔴" : "⚪") + ' ' + esc((i.published || "").slice(0, 10)) + ' · ' +
        (i.url ? '<a href="' + esc(i.url) + '" target="_blank" rel="noopener noreferrer">' + esc(i.resumo || i.title) + '</a>' : esc(i.resumo || i.title)) + '</p>').join("") + '</div>';
    if (!mk.length && !pb.length && !ps.length && !held.length && !earn.length && !items.length && !(w && w.tx && w.tx.some((t) => same(t.symbol, q)))) {
      html += '<p class="muted">Nada sobre ' + esc(q) + ' nos dados de hoje. O portal só conhece as ações do ranking, das carteiras dos agentes, do /seguir, das notícias lidas e das propostas. Para seguir uma ação, usa /seguir no Discord.</p>';
    }
    out.innerHTML = html;
    openTab("pesquisa");
  }
  $("search").addEventListener("submit", (e) => { e.preventDefault(); run($("q").value); });
  $("q").addEventListener("change", (e) => { if (names[e.target.value.toUpperCase()] !== undefined) run(e.target.value); });
  document.addEventListener("click", (e) => { const a = e.target.closest("[data-go]"); if (a) { e.preventDefault(); openTab(a.dataset.go); } });
})();

// ---- status: every part and automatic job, its state, last and next update and what makes it run ----
(function status() {
  if (!$("status")) return;
  const when = (x) => x && (x.updated || x.as_of || x.date || x.created);
  const ms = (iso) => iso ? Date.parse(iso.length === 10 ? iso + "T18:00:00Z" : iso) : null;
  const weekendSlack = [0, 1].includes(new Date().getDay()) ? 48 : 0;  // Sunday and Monday: Friday's data is fine
  const runsOf = (wf, title) => ((RUNS || {})[wf] || []).filter((r) => !title || title.test(r.title || ""));
  // next run of a GitHub cron (UTC): daily at h:m, weekly on a weekday, monthly on a day
  const cron = (h, m, { dow = null, dom = null } = {}) => () => {
    const now = new Date();
    for (let i = 0; i < 40; i++) {
      const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + i, h, m));
      if (d > now && (dow === null || d.getUTCDay() === dow) && (dom === null || d.getUTCDate() === dom)) return d.getTime();
    }
    return null;
  };
  const sched = (...keys) => () => { const at = Math.min(...UPCOMING.filter((j) => keys.includes(j.key)).map((j) => j.at)); return isFinite(at) ? at : null; };
  const d = "dias úteis (seg–sex, hora de Lisboa)", D = "Dias úteis (seg–sex, hora de Lisboa)";
  const roundJob = UPCOMING.find((j) => j.key === "ronda"), rt = (roundJob && roundJob.times) || [];
  const every = rt.length > 1 ? (+rt[1].slice(0, 2) * 60 + +rt[1].slice(3)) - (+rt[0].slice(0, 2) * 60 + +rt[0].slice(3)) : null;
  const roundsText = rt.length ? (every ? "de " + every + " em " + every + " min das " + rt[0] + " às " + rt[rt.length - 1] : "às " + rt.join(" · ")) : "de 10 em 10 min";
  // part, its data's last update (null: the job's last run), hours before late, workflow (+ run title), next run, condition
  const rows = [
    { k: "📈 Preços do mercado", iso: DATA.updated, max: 30 + weekendSlack, wf: "agents.yml", title: /stops/, next: sched("ronda"),
      when: "Preços novos " + roundsText + " (" + d + "), do Yahoo Finance: Europa 08:00–16:30 e EUA 14:30–21:00 (as rondas logo a seguir ao fecho guardam o preço final do dia). " +
        "Algumas bolsas chegam com até 15 min de atraso. Fora destas horas e ao fim de semana ficam os últimos fechos." },
    { k: "🏁 Liga dos agentes", iso: DATA.updated, max: 30 + weekendSlack, wf: "agents.yml", next: sched("ronda", "agentes"),
      when: "Em cada ronda, " + d + ", " + roundsText + ": cada agente decide com os preços do momento nas bolsas abertas (Europa 08:00–16:30, EUA 14:30–21:00) e a ronda-sombra do motor Rust repete-a. Às 07:30 o fecho diário com as sessões completas da véspera. Ao fim de semana não muda." },
    { k: "📰 Notícias", iso: NEWS && NEWS.updated, max: 8 + weekendSlack, wf: "news.yml", next: sched("noticias"),
      when: D + " às 12:00, 16:00 e 20:30: o Claude lê as notícias das ações seguidas, os 8-K da SEC e o risco geopolítico; os agentes usam-nas para vetar compras." },
    { k: "🛡️ Regras de compra e contexto", iso: when(MARKET), max: 30 + weekendSlack, wf: "agents.yml", title: /daily/, next: sched("agentes"),
      when: "Na liga diária (" + d + ", 07:30), com os fechos da véspera: VIX, crédito, juros, dólar, petróleo, ouro, setores, VCP e rankings." },
    { k: "🧠 Propostas", iso: when(PROPOSALS), max: 30 + weekendSlack, wf: "agents.yml", title: /daily/, next: sched("agentes"),
      when: "Na liga diária (07:30): o que os agentes querem comprar ou vender na abertura, com entrada, stop, alvo e score." },
    { k: "📅 Agenda", iso: when(CALENDAR), max: 30 + weekendSlack, wf: "agents.yml", title: /daily/, next: sched("agentes"),
      when: "Na liga diária (07:30): resultados das empresas e calendário macro dos 14 dias seguintes." },
    { k: "⏪ Treino", iso: PRETRAIN && PRETRAIN.updated, max: 24 * 8, wf: "pretrain.yml", title: /Pré-treino/, next: sched("treino"),
      when: "Ao domingo às 06:00 (depois dos dados da semana) e quando pedido: cada perfil treina em série até a escolha estabilizar; a seguir conservador e arrojado, e voltas aos 3 perfis enquanto alguma escolha mudar." },
    { k: "⚡ Treino da reatividade", wf: "intraday-lab.yml", max: 24 * 8, next: null,
      when: "Quando acaba o último perfil do pré-treino: barras de 30 min da Alpaca (EUA, guardadas e só as novas descarregadas) e a reatividade de cada estratégia (abertura, confirmação ou tempo real)." },
    { k: "📊 Dicas do dia e rankings", wf: "discord-daily.yml", max: 30 + weekendSlack, next: sched("dicas"),
      when: D + " às 07:00, antes da abertura europeia: ideias do dia e rankings no Discord." },
    { k: "🚨 Alertas de rompimento", wf: "opportunity-alerts.yml", max: 30 + weekendSlack, next: sched("alertas"),
      when: D + " às 15:40, 17:40 e 19:40, com a bolsa americana aberta: ações que rompem com volume ou mexem 5% ou mais; mensagens privadas a quem as segue (/seguir)." },
    { k: "🔁 Acompanhamento dos alertas", wf: "alert-followups.yml", max: 30 + weekendSlack, next: sched("acompanhamento"),
      when: D + " às 22:30, depois do fecho dos EUA: como correu cada alerta 5 e 20 sessões depois." },
    { k: "🔧 Estado do bot", wf: "health.yml", max: 30 + weekendSlack, next: sched("estado"),
      when: D + " às 23:00, depois de todas as tarefas do dia: confirma que correram e avisa no canal 🔧-estado." },
    { k: "🗓️ Resumo semanal", wf: "weekly-summary.yml", max: 24 * 8, next: sched("semanal"),
      when: "Ao sábado às 10:00: a semana da liga e do mercado no Discord." },
    { k: "🗂️ Dados de contexto e fundamentos", wf: "context-data.yml", max: 24 * 8, next: cron(3, 17, { dow: 0 }),
      when: "Ao domingo às 03:17 UTC, antes do pré-treino: VIX, incerteza, crédito, juros, dólar, risco geopolítico, petróleo, ouro, ETFs de setor e fundamentos da SEC." },
    { k: "💾 Cópia de segurança", wf: "backup.yml", max: 30, next: cron(3, 17),
      when: "Todos os dias às 03:17 UTC: carteiras e contas dos membros, cifradas, guardadas 30 dias (e 14 cópias diárias na própria base de dados)." },
    { k: "📦 Arquivo mensal", wf: "arquivo-mensal.yml", max: 24 * 32, next: cron(2, 23, { dom: 1 }),
      when: "No dia 1 de cada mês às 02:23 UTC: os preços de cada ronda do mês e as barras de 5 min das ações europeias (o Yahoo só as guarda 60 dias), cifrados num ficheiro só." },
    { k: "🌐 Site (Cloudflare)", wf: "deploy-worker.yml", max: null, next: null,
      when: "A cada alteração do código do site: publica o portal e o bot do Discord." },
    { k: "✅ Testes", wf: "tests.yml", max: null, next: null,
      when: "A cada alteração do código: os testes do Python e do motor Rust têm de passar." },
  ];
  const fmt = (t) => esc(lisbon(t, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }));
  const ago = (t) => { if (t == null) return "—"; const h = (Date.now() - t) / 3.6e6; return h < 1 ? Math.max(1, Math.round(h * 60)) + " min" : h < 48 ? Math.round(h) + " h" : Math.round(h / 24) + " dias"; };
  const dot = (cls, text) => '<span class="dot ' + cls + '"></span>' + text;
  const state = (r) => {
    const runs = r.wf ? runsOf(r.wf, r.title) : [];
    const last = runs[0], done = runs.find((x) => x.status === "completed");
    const data = r.iso !== undefined ? ms(r.iso) : (done && done.conclusion === "success" ? ms(done.updated) : null);
    const lastOk = r.iso !== undefined ? data : (runs.find((x) => x.conclusion === "success") || {}).updated;
    let st;
    if (last && last.status !== "completed") st = dot("run", "a correr");
    else if (done && done.conclusion === "failure") st = dot("bad", "falhou");
    else if (r.iso !== undefined || r.max) {
      const t = r.iso !== undefined ? data : ms(lastOk), h = t == null ? null : (Date.now() - t) / 3.6e6;
      st = h == null ? dot("bad", RUNS ? "sem dados" : "—") : !r.max || h <= r.max ? dot("ok", "em dia") : h <= r.max * 2 ? dot("old", "atrasado") : dot("bad", "parado");
    } else st = done ? dot(done.conclusion === "success" ? "ok" : "old", done.conclusion === "success" ? "ok" : esc(done.conclusion || "")) : "—";
    const t = r.iso !== undefined ? data : (done ? ms(done.updated) : null);
    const link = (done || last) ? ' <a class="muted" href="' + esc((done || last).url) + '" target="_blank" rel="noopener" title="Ver no GitHub">↗</a>' : "";
    return [st, t ? fmt(t) + link : "—", ago(t)];
  };
  const nextCell = (r) => { const at = r.next && r.next(); return at ? fmt(at) + '<br><span class="muted small" data-at="' + at + '"></span>' : '<span class="muted">' + (r.next === null && r.wf === "intraday-lab.yml" ? "depois do pré-treino" : "a cada alteração") + "</span>"; };
  const tick = () => document.querySelectorAll("#status [data-at]").forEach((e) => { e.textContent = "daqui a " + left(+e.dataset.at - Date.now()); });
  const draw = () => {
    $("status").innerHTML = '<p class="muted small">Cada parte do portal e cada tarefa automática: como está, quando correu e quando volta a correr, e o que a faz correr. ' +
      (RUNS ? "As execuções vêm do GitHub Actions (↗ abre a última)." : "Sem acesso às execuções do GitHub agora: o estado vem só da data dos dados.") + "</p>" +
      table(["Parte", "Estado", "Última atualização", "Há", "Próxima", "Condição de atualização"], rows.map((r) => [r.k, ...state(r), nextCell(r), '<span class="small wrap">' + esc(r.when) + "</span>"])) +
      '<p class="muted small">🟢 em dia · 🔵 a correr · 🟡 atrasado (mais do que o normal) · 🔴 falhou, parado ou sem dados. Se algo ficar parado, avisa no Discord: o canal 🔧-estado mostra os erros.</p>';
    tick();
  };
  draw();
  setInterval(() => { if (!$("status").closest("section").hidden) tick(); }, 1000);
  setInterval(() => { if (!$("status").closest("section").hidden) draw(); }, 60000);
})();
`;
STATIC_PATHS.extra = staticAsset("portal-extra", EXTRA, "js");
