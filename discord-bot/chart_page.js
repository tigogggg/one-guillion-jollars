// Interactive chart pages, served by the Worker at /g/<id>.
//
// scripts/interactive.py stores each chart's data in KV under chart:<id> (and
// the newest id of each kind under chart-latest:<kind>). The page draws it with
// Plotly (hover values, zoom, agents on and off in the legend), shows one
// detailed card per agent (holdings, orders, trades, stats, strategy) and counts
// down to the next scheduled runs, from the same schedule /horario edits.

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
  const [news, market, proposals, calendar, geo, evolution] = await Promise.all(["news", "market", "proposals", "calendar", "geo", "evolution"].map((k) => env.CONFIG.get(`portal:${k}`, "json")));
  const ptId = await env.CONFIG.get("chart-latest:pretreino", "json");
  const pretrain = ptId && ptId !== latest ? await env.CONFIG.get(`chart:${ptId}`, "json") : null;
  const user = session ? { name: session.data.name || "membro", wallet: `/c/${session.tok}` } : null;
  const version = await portalVersion(env);
  return html(chartPage(data, nextRuns(schedule, Date.now()), null, { portal: true, news, market, proposals, calendar, pretrain, user, geo, evolution, version,
                                                                       manifest: token === null ? "/manifest.webmanifest" : `/p/${token}/manifest.webmanifest` }));
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

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// JSON inside <script>: no "</script>" or line separators can break out.
export function safeJson(value) {
  return JSON.stringify(value).replace(/</g, "\\u003c").replace(/\u2028/g, "\\u2028").replace(/\u2029/g, "\\u2029");
}

// Background music on every page: a YouTube video, muted by default, with a 🔇/🔊 button.
// The choice is remembered on this device; browsers only allow sound after a click, so a
// remembered "on" starts at the first tap on the page.
const MUSIC_VIDEO = "ypKD0MieAI4";
const MUSIC_START = 7;
const MUSIC = `<button id="music" type="button" aria-label="Ligar a música" title="Música">🔇</button>
<div id="music-player" aria-hidden="true"></div>
<style>
#music { position:fixed; top:10px; right:10px; z-index:60; width:40px; height:40px; border-radius:50%; border:2px solid var(--accent, #ff2e88);
  background:rgba(11,8,32,.85); color:#fff; font-size:18px; line-height:1; cursor:pointer; box-shadow:0 0 12px rgba(255,46,136,.45); }
#music:hover { transform:scale(1.08); } #music.on { border-color:#38e8ff; box-shadow:0 0 14px rgba(56,232,255,.6); }
#music-player { position:fixed; left:-10000px; top:0; width:200px; height:200px; pointer-events:none; opacity:0; }
</style>
<script>
(function () {
  var btn = document.getElementById("music"), player = null, ready = false, wantOn = false;
  try { wantOn = localStorage.getItem("musica") === "on"; } catch (e) {}
  function show(on) { btn.textContent = on ? "🔊" : "🔇"; btn.classList.toggle("on", on); btn.setAttribute("aria-label", on ? "Silenciar a música" : "Ligar a música"); }
  function save(on) { try { localStorage.setItem("musica", on ? "on" : "off"); } catch (e) {} }
  function apply(on) {
    show(on);
    if (!ready) return;
    if (on) { player.unMute(); player.setVolume(35); player.playVideo(); } else { player.mute(); }
  }
  show(wantOn);
  btn.addEventListener("click", function (e) { e.stopPropagation(); wantOn = !wantOn; save(wantOn); apply(wantOn); });
  if (wantOn) document.addEventListener("pointerdown", function first() { document.removeEventListener("pointerdown", first, true); apply(wantOn); }, true);
  var prev = window.onYouTubeIframeAPIReady;
  window.onYouTubeIframeAPIReady = function () {
    if (prev) prev();
    player = new YT.Player("music-player", { width: 200, height: 200, videoId: "${MUSIC_VIDEO}",
      playerVars: { autoplay: 1, mute: 1, start: ${MUSIC_START}, loop: 1, playlist: "${MUSIC_VIDEO}", controls: 0, playsinline: 1, disablekb: 1, rel: 0 },
      events: {
        onReady: function () { ready = true; player.mute(); player.playVideo(); },
        onStateChange: function (e) { if (e.data === 0) { player.seekTo(${MUSIC_START}); player.playVideo(); } },
        onError: function () { btn.hidden = true; },  // the video cannot be embedded or was removed
      } });
  };
  var tag = document.createElement("script"); tag.src = "https://www.youtube.com/iframe_api"; tag.async = true;
  document.head.appendChild(tag);
})();
</script>`;

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
<link rel="icon" type="image/png" href="/favicon.png?v=1gj">
<link rel="stylesheet" href="${STATIC_PATHS.css}">
${head}
</head>
<body${opts.embed ? ' class="embed"' : ""}><main>${body}</main>${opts.embed ? "" : MUSIC}</body>
</html>`;
}

export function chartPage(data, upcoming, newer, extras = {}) {
  const banner = newer
    ? `<a class="banner" href="/g/${esc(newer)}">🔄 Há uma versão mais recente deste gráfico · abrir</a>`
    : "";
  const portal = !!extras.portal;
  const tabs = (portal ? [["hoje", "🏠 Hoje"]] : []).concat([["corrida", "🏁 Corrida"]])
    .concat(portal && extras.pretrain ? [["pretreino", "⏪ Pré-treino"]] : []).concat([["agentes", "🧾 Agentes"]])
    .concat(portal ? [["propostas", "🧠 Propostas"], ["noticias", "📰 Notícias"], ["oportunidades", "🎯 Oportunidades"]] : [])
    .concat([["agenda", "⏱️ Agenda"]])
    .concat(portal ? [["carteira", "💼 Carteira"], ["ajuda", "❓ Glossário"], ["estado", "🩺 Estado"]] : []);
  const body = `
${banner}
<header>
  ${portal ? `<div class="userbar">${extras.user
    ? `<span>👤 <b>${esc(extras.user.name)}</b></span><a class="btn alt" href="#carteira" data-go="carteira">💼 A MINHA CARTEIRA</a><a class="btn alt" href="/sair">SAIR</a>`
    : `<a class="btn" href="/entrar">🔑 ENTRAR</a>`}</div>` : ""}
  <h1>${portal ? '<img class="logo" src="/icon-192.png?v=1gj" alt="" width="48" height="48">One Guillion Jollars' : esc(data.title)}</h1>
  <p class="muted">${esc(portal ? data.title + " · " + (data.subtitle || "") : data.subtitle || "")}</p>
  <p class="muted small">Dados da liga de <span id="updated"></span> (hora de Lisboa) · simulação com dinheiro fictício e preços reais · ferramenta educativa, não é recomendação de compra</p>
</header>
${portal ? `<form id="search" class="search" role="search" autocomplete="off">
  <input id="q" type="search" list="q-list" placeholder="🔎 Procurar um ticker: notícias, score, agentes e a tua posição (ex.: NVDA, VUSA)" aria-label="Procurar ticker">
  <datalist id="q-list"></datalist><button class="btn" type="submit">PROCURAR</button>
  <button class="btn alt" type="button" id="guide-open" title="Guia rápido">📖 GUIA</button>
</form>` : ""}
<nav class="tabs" id="tabs">${tabs.map(([k, label], i) => `<button class="tab${i ? "" : " on"}" data-tab="${k}">${label}</button>`).join("")}</nav>
${portal ? `<section data-tab="hoje"><div id="today"></div></section>` : ""}
<section class="race" id="race" data-tab="corrida"${portal ? " hidden" : ""}>
  <div class="marquee"><span>🏁 Grande Prémio dos Agentes 🏁</span></div>
  <div class="hud">
    <div>Dia <b id="race-date">—</b></div>
    <div>Líder <b id="race-leader">—</b></div>
    <div class="controls"><button id="race-play" class="btn">▶ VER A CORRIDA</button><button id="race-speed" class="btn alt">1×</button></div>
  </div>
  <div id="track" class="track"></div>
  <input type="range" id="race-scrub" min="0" value="0" aria-label="Dia da corrida">
  <p class="muted small">Cada corredor avança com a rentabilidade do agente; a meta é o melhor resultado da corrida. Toca num corredor para abrir a ficha dele.</p>
  <h2>🏆 Placar</h2>
  <ol id="board" class="board"></ol>
  <h2>📈 Gráfico</h2>
  <div id="chart" class="chart"></div>
  <p class="muted small">Toca numa linha da legenda para a esconder ou mostrar, toca duas vezes para ver só essa. Arrasta para fazer zoom; duplo toque volta ao início.</p>
</section>
${portal && extras.pretrain ? `<section class="race" id="pt-race" data-tab="pretreino" hidden>
  <div class="marquee"><span>⏪ Pré-treino · 20 anos com os parâmetros escolhidos · custos e impostos incluídos ⏪</span></div>
  <p class="muted small">${esc(extras.pretrain.subtitle || "")}. É uma simulação do passado: mostra como cada estratégia se teria portado, não o que vai fazer.</p>
  <div class="hud">
    <div>Dia <b id="pt-race-date">—</b></div>
    <div>Líder <b id="pt-race-leader">—</b></div>
    <div class="controls"><button id="pt-race-play" class="btn">▶ VER A CORRIDA</button><button id="pt-race-speed" class="btn alt">1×</button></div>
  </div>
  <div id="pt-track" class="track"></div>
  <input type="range" id="pt-race-scrub" min="0" value="0" aria-label="Dia da corrida do pré-treino">
  <h2>🏆 Placar</h2>
  <ol id="pt-board" class="board"></ol>
  <h2>📈 Gráfico</h2>
  <div id="pt-chart" class="chart"></div>
  <h2>📊 Desempenho por período</h2>
  <div id="pt-perf"></div>
  <h2>📈 Evolução entre execuções do treino</h2>
  <div id="evo"></div>
  <h2>🔎 Resultados do treino por agente</h2>
  <div id="pt-chips" class="chips"></div>
  <div id="pt-detail"></div>
</section>` : ""}
<section id="agents-section" data-tab="agentes" hidden>
  <h2>📊 Desempenho por período</h2>
  <div id="perf"></div>
  <h2>🧾 Todos os agentes</h2>
  <div id="overview"></div>
  <details class="panel" id="compare-box"><summary><b>⚖️ Comparar dois agentes</b></summary><div class="filters"><select id="cmp-a"></select><select id="cmp-b"></select></div><div id="cmp"></div></details>
  <h2>🔎 Ficha do agente</h2>
  <div id="chips" class="chips"></div>
  <div id="detail"></div>
</section>
${portal ? `<section data-tab="propostas" hidden><div id="props"></div></section>
<section data-tab="noticias" hidden><div id="news"></div></section>
<section data-tab="oportunidades" hidden><div id="opps"></div></section>` : ""}
<section data-tab="agenda" hidden>
  <h2>⏱️ Próximas atualizações</h2>
  <div id="countdowns" class="grid"></div>
  <p class="muted small">Horários de Lisboa, os mesmos do /horario no Discord. Cada tarefa demora alguns minutos a chegar.</p>
</section>
${portal ? `<section data-tab="ajuda" hidden><div id="glossary"></div></section>` : ""}
${portal ? `<section data-tab="pesquisa" hidden><div id="q-out"></div></section>
<section data-tab="estado" hidden><div id="status"></div></section>` : ""}
${portal ? `<section data-tab="carteira" hidden>
  ${extras.user ? `<iframe id="wallet-frame" class="wallet-frame" title="A minha carteira" data-src="${esc(extras.user.wallet)}?embed=1" loading="lazy"></iframe>
  <p class="muted small">A tua carteira também abre sozinha em <a href="${esc(extras.user.wallet)}">${esc(extras.user.wallet.slice(0, 9))}…</a> (para guardar no ecrã principal do telemóvel).</p>`
  : `<h2>💼 A tua carteira</h2>
  <div class="panel prose">
    <p>Cada membro regista as suas compras e vendas reais (ou importa da XTB) e acompanha o desempenho com preços quase em tempo real, aqui mesmo no portal.</p>
    <p><a class="btn" style="display:inline-block;text-decoration:none;font-size:.75rem;padding:12px 16px" href="/entrar">🔑 ENTRAR COM O DISCORD OU O GOOGLE</a></p>
    <p class="muted small">Discord: só membros do servidor (a primeira entrada cria a carteira). Google: depois de associares a conta. A sessão fica aberta 30 dias neste aparelho.
    Também podes usar <code>/carteira</code> no Discord, que te dá o link privado.</p>
  </div>`}
</section>` : ""}
<script>
const DATA = ${safeJson(data)};
const PORTAL_VERSION = ${safeJson(extras.version || null)};
const UPCOMING = ${safeJson(upcoming)};
const NEWS = ${safeJson(extras.news || null)};
const MARKET = ${safeJson(extras.market || null)};
const PROPOSALS = ${safeJson(extras.proposals || null)};
const CALENDAR = ${safeJson(extras.calendar || null)};
const PRETRAIN = ${safeJson(extras.pretrain || null)};
const GEO = ${safeJson(extras.geo || null)};
const EVOLUTION = ${safeJson(extras.evolution || null)};
</script>
<script src="${STATIC_PATHS.portal}"></script>
${portal ? `<script src="${STATIC_PATHS.extra}"></script>` : ""}`;
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
h1 .logo { width:2.4em; height:2.4em; vertical-align:middle; margin-right:.5em; border-radius:8px; }
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
.board { list-style:none; padding:0; margin:0; display:grid; gap:6px; counter-reset:pos; }
.board li { display:flex; align-items:center; gap:10px; background:var(--card); border:2px solid var(--line); border-radius:6px; padding:8px 10px; cursor:pointer; }
.board li .pos { font-family:var(--pixel); font-size:.65rem; width:34px; color:var(--gold); }
.board li:nth-child(1) { border-color:var(--gold); box-shadow:0 0 10px rgba(255,225,77,.45); }
.board li:nth-child(2) { border-color:#c0c7d6; } .board li:nth-child(3) { border-color:#d08a4a; }
.tabs { position:sticky; top:0; z-index:5; display:flex; flex-wrap:wrap; gap:6px; padding:8px 0; margin:6px 0 4px; background:linear-gradient(var(--bg) 85%,transparent); }
.tab { flex:none; font-family:var(--pixel); font-size:.55rem; background:var(--card); color:var(--fg); border:2px solid var(--line); border-radius:6px; padding:9px 10px; cursor:pointer; }
.tab.on { border-color:var(--accent); color:#fff; box-shadow:0 0 10px rgba(255,46,136,.5); }
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
.chart { width:100%; height:62vh; min-height:340px; background:var(--card); border:2px solid var(--line); border-radius:8px; }
.chips { display:flex; flex-wrap:wrap; gap:6px; }
.chip { background:var(--card); border:1px solid var(--line); color:var(--fg); border-radius:999px; padding:6px 10px; cursor:pointer; font:inherit; font-size:.88rem; }
.chip.on { border-color:var(--accent); background:#3a0f45; box-shadow:0 0 8px rgba(255,46,136,.5); }
.panel { background:var(--card); border:2px solid var(--neon); box-shadow:0 0 12px rgba(0,229,255,.25); border-radius:10px; padding:14px; margin-top:12px; }
.userbar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; justify-content:flex-end; margin:0 52px 0 0; font-size:.9rem; }
.userbar .btn { text-decoration:none; display:inline-block; }
.wallet-frame { width:100%; min-height:900px; border:0; display:block; background:transparent; }
body.embed .tabs { position:static !important; } body.embed main { padding-top:4px; padding-bottom:12px; } body.embed .embed-hide { display:none !important; }
.search { display:flex; flex-wrap:wrap; gap:6px; margin:6px 0 10px; } .search input { flex:1; min-width:220px; background:var(--card); color:var(--fg); border:2px solid var(--neon); border-radius:6px; padding:9px 11px; font:inherit; }
.dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; vertical-align:middle; } .dot.ok { background:var(--up); box-shadow:0 0 6px var(--up); } .dot.old { background:var(--gold); } .dot.bad { background:var(--down); }
#guide { position:fixed; inset:0; z-index:80; background:rgba(5,3,20,.78); display:flex; align-items:center; justify-content:center; padding:16px; }
#guide[hidden] { display:none; } #guide .panel { max-width:520px; width:100%; } #guide .steps { font-family:var(--pixel); font-size:.55rem; color:var(--muted); }
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
function drawCountdowns() {
  const now = Date.now();
  $("countdowns").innerHTML = UPCOMING.map((j) =>
    '<div class="tile"><div class="k">' + esc(j.label) + '</div><div class="v">' + left(j.at - now) + '</div><div class="s">' +
    lisbon(j.at, { weekday: "long", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) +
    (j.times.length > 1 ? " · " + j.times.length + "× por dia útil: " + esc(j.times.join(" · ")) : "") + '</div></div>').join("");
}
drawCountdowns();
setInterval(drawCountdowns, 1000);

// ---- chart ----
function lineChart(elId, DATA) {
const palette = ["#4cc0a8", "#e2b84a", "#5865f2", "#ed4245", "#57f287", "#eb459e", "#f0b232", "#9b84ee", "#3ba5d8", "#dbdee1"];
const highlight = new Set(DATA.highlight || []);
let colour = 0;
const traces = (DATA.series || []).map((s) => {
  const on = !highlight.size || highlight.has(s.name);
  return { x: s.x, y: s.y, name: s.name, meta: s.name, type: "scatter", mode: s.x.length < 3 ? "lines+markers" : "lines",
    line: { width: on ? 2 : 1, color: on ? palette[colour++ % palette.length] : "#6d6f78" }, opacity: on ? 1 : 0.55,
    hovertemplate: "%{y:+.1f}%<extra>" + esc(s.name) + "</extra>" };
}).concat((DATA.references || []).map((s, i) => ({ x: s.x, y: s.y, name: s.name, meta: s.name, type: "scatter", mode: "lines",
  line: { width: 1.2, dash: i ? "dot" : "dash", color: i ? "#b5bac1" : "#80848e" },
  hovertemplate: "%{y:+.1f}%<extra>" + esc(s.name) + "</extra>" })));
const xs = traces.flatMap((t) => [t.x[0], t.x[t.x.length - 1]]).filter(Boolean).sort();
const spanYears = xs.length ? (Date.parse(xs[xs.length - 1]) - Date.parse(xs[0])) / 3.15e10 : 0;
const narrow = window.innerWidth < 640;
if (!window.Plotly) $(elId).innerHTML = '<p class="muted" style="padding:16px">Não consegui carregar o gráfico (Plotly). As fichas dos agentes estão em baixo.</p>';
else Plotly.newPlot(elId, traces, {
  paper_bgcolor: "#17123a", plot_bgcolor: "#17123a", font: { color: "#e8e6ff", size: 12 },
  margin: { l: 52, r: 12, t: 12, b: narrow ? 10 : 40 }, hovermode: "x unified",
  hoverlabel: { bgcolor: "#0b0820", bordercolor: "#2e2763" },
  xaxis: { gridcolor: "#2e2763", hoverformat: "%d/%m/%Y", tickformat: spanYears > 2 ? "%Y" : spanYears > 0.4 ? "%m/%y" : "%d/%m",
    dtick: spanYears < 0.05 ? 86400000 : undefined, rangeselector: spanYears > 2 ? { bgcolor: "#1e1f22", activecolor: "#5865f2", buttons: [
    { count: 1, label: "1A", step: "year", stepmode: "backward" }, { count: 5, label: "5A", step: "year", stepmode: "backward" },
    { count: 10, label: "10A", step: "year", stepmode: "backward" }, { step: "all", label: "Tudo" }] } : undefined },
  yaxis: { gridcolor: "#2e2763", zerolinecolor: "#80848e", ticksuffix: "%" },
  legend: narrow ? { orientation: "h", y: -0.12, font: { size: 11 } } : { bgcolor: "rgba(0,0,0,0)" },
}, { responsive: true, displaylogo: false, modeBarButtonsToRemove: ["lasso2d", "select2d"] });
if (window.Plotly) intervalLegend(elId, traces);
}

// Legend in the order of the lines (best result on top) with each one's result over the visible interval:
// updated when the interval changes (zoom, drag, 1A/5A/10A/Tudo).
function intervalLegend(elId, traces) {
  const el = $(elId);
  const day = (v) => String(v).slice(0, 10);
  const fmtDay = (d) => d.slice(8, 10) + "/" + d.slice(5, 7) + "/" + d.slice(0, 4);
  let busy = false;
  const apply = (from, to) => {
    const res = traces.map((t) => {
      let i0 = -1, i1 = -1;
      for (let i = 0; i < t.x.length; i++) {
        const d = day(t.x[i]);
        if (t.y[i] == null || (from && d < from) || (to && d > to)) continue;
        if (i0 < 0) i0 = i;
        i1 = i;
      }
      return i0 < 0 || i1 <= i0 ? null : ((1 + t.y[i1] / 100) / (1 + t.y[i0] / 100) - 1) * 100;
    });
    const order = res.map((r, i) => [r == null ? -Infinity : r, i]).sort((a, b) => b[0] - a[0]);
    const rank = []; order.forEach(([, i], k) => { rank[i] = k + 1; });
    const names = traces.map((t, i) => t.meta + (res[i] == null ? "" : " · " + (res[i] > 0 ? "+" : "") + fmtNum(res[i], 1) + "%"));
    const xs = traces.flatMap((t) => t.x.length ? [day(t.x[0]), day(t.x[t.x.length - 1])] : []).sort();
    const a = from && from > xs[0] ? from : xs[0], b = to && to < xs[xs.length - 1] ? to : xs[xs.length - 1];
    const layout = { "legend.title.text": a && b ? "<b>" + fmtDay(a) + " → " + fmtDay(b) + "</b>" : "" };
    if (from || to) {  // fit the vertical axis and the date labels to the visible interval
      let lo = Infinity, hi = -Infinity;
      traces.forEach((t) => t.x.forEach((x, i) => { const d = day(x); if (t.y[i] != null && (!from || d >= from) && (!to || d <= to)) { lo = Math.min(lo, t.y[i]); hi = Math.max(hi, t.y[i]); } }));
      if (isFinite(lo)) { const pad = (hi - lo) * 0.06 || 1; layout["yaxis.range"] = [lo - pad, hi + pad]; }
      const years = (Date.parse(b) - Date.parse(a)) / 3.15e10;
      layout["xaxis.tickformat"] = years > 3 ? "%Y" : years > 0.4 ? "%m/%y" : "%d/%m";
    } else {
      layout["yaxis.autorange"] = true;
    }
    busy = true;
    Plotly.update(el, { name: names, legendrank: rank }, layout)
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
lineChart("chart", DATA);

// ---- agents ----
const agents = DATA.agents || [];
if (!agents.length) $("agents-section").style.display = "none";
const byName = Object.fromEntries(agents.map((a) => [a.name, a]));
const headline = (a) => a.backtest ? a.backtest.return_pct : a.return_pct;
$("chips").innerHTML = agents.map((a, i) =>
  '<button class="chip" data-i="' + i + '">' + esc(a.emoji + " " + a.name) + ' <span class="' + cls(headline(a)) + '">' +
  fmtPct(headline(a)) + '</span></button>').join("");
$("chips").addEventListener("click", (e) => { const b = e.target.closest(".chip"); if (b) show(+b.dataset.i, true); });
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

function strategyHtml(s) {
  return '<h3>🧭 Estratégia: ' + esc(s.label) + '</h3><div class="prose">' +
    (s.description ? '<p>' + md(s.description) + '</p>' : "") +
    '<p>💡 ' + md(s.idea) + '</p><p>🟢 <b>Quando compra:</b> ' + md(s.buys) + '</p><p>🔴 <b>Quando vende:</b> ' + md(s.sells) +
    '</p><p>⚖️ <b>Tamanho das posições:</b> ' + md(s.size) + '</p><p>🛡️ <b>Regras comuns:</b><br>' + md(s.rules) + '</p></div>' +
    '<h3>⚙️ Parâmetros</h3>' + table(["Parâmetro", "Valor", "O que faz"], (s.params || []).map((p) => ['<code>' + esc(p.key) + '</code>', esc(p.value), esc(p.meaning)])) +
    (s.pretrain ? '<h3>🧠 Pré-treino</h3><div class="prose"><p>' + md(s.pretrain) + '</p></div>' : "");
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
    tile("Impostos pagos", fmtEur(a.taxes), esc(a.fees_note)) + '</div>' +
    '<h3>💼 Carteira atual (' + a.positions.length + ' posições ativas)</h3>' +
    table(["Ativo", "Porque comprou", "Nome", "Qtd.", "Custo médio", "Preço atual", "Investido", "Valor", "Ganho", "Ganho %", "Peso", "Stop", "Alvo", "Desde", "Resultados"],
      a.positions.map((p) => ['<b>' + esc(p.symbol) + '</b>', p.why ? '<details><summary>' + esc(p.reason || "ver") + '</summary><p class="small why">' + esc(p.why) + '</p></details>' : esc(p.reason || "—"),
        esc(p.name || p.sector || ""), fmtNum(p.qty, 4),
        fmtNum(p.entry) + " " + p.currency, (p.price == null ? "—" : fmtNum(p.price) + " " + p.currency), fmtEur(p.cost_eur), fmtEur(p.value_eur),
        '<span class="' + cls(p.pnl_eur) + '">' + fmtEur(p.pnl_eur) + '</span>', '<span class="' + cls(p.pnl_pct) + '">' + fmtPct(p.pnl_pct) + '</span>',
        fmtNum(p.weight, 1) + "%", p.stop ? fmtNum(p.stop) : "—", p.tp ? fmtNum(p.tp) : "—", esc(p.entry_date || ""), esc(p.earnings || "—")]),
      [3, 4, 5, 6, 7, 8, 9, 10, 11, 12]) +
    '<p class="muted small">Custo médio e preço na moeda do ativo, com custos incluídos; valores em euros ao câmbio do dia.</p>' +
    '<h3>⏳ Ordens para a próxima abertura</h3>' +
    table(["Tipo", "Ativo", "Valor", "Stop", "Alvo", "Motivo", "Números na decisão", "Criada"],
      (a.pending || []).map((o) => [o.side === "buy" ? "🟢 compra" : "🔴 venda", '<b>' + esc(o.symbol) + '</b>', o.eur ? fmtEur(o.eur) : (o.fraction ? fmtNum(o.fraction * 100, 0) + "% da posição" : "tudo"),
        o.stop ? fmtNum(o.stop) : "—", o.tp ? fmtNum(o.tp) : "—", esc(o.reason || ""), '<span class="small" style="white-space:normal">' + esc((o.why || "").replace(/^Na altura: /, "")) + '</span>', esc(o.created || "")]), [2, 3, 4]) +
    '<h3>📚 Histórico de posições (' + (a.history || []).length + ')</h3><p class="muted small">Cada posição da primeira compra à última venda. Toca para ver as compras, as vendas e a justificação de cada uma segundo a estratégia.</p>' +
    ((a.history || []).map((l) => {
      const icon = l.status === "aberta" ? "🟡" : l.result > 0 ? "✅" : "🔴";
      const last = l.sells.length ? l.sells[l.sells.length - 1].reason : "";
      return '<details class="lot"><summary>' + icon + ' <b>' + esc(l.symbol) + '</b> · ' + esc(l.opened) + ' → ' + esc(l.closed || "aberta") +
        (l.status === "fechada" ? ' · <span class="' + cls(l.result) + '">' + fmtEur(l.result) + (l.result_pct != null ? " (" + fmtPct(l.result_pct) + ")" : "") + '</span>' + (l.days != null ? ' · ' + l.days + ' dias' : '') + (last ? ' · ' + esc(last) : '') : ' · ' + fmtEur(l.invested) + ' investidos') +
        '</summary>' + l.buys.concat(l.sells).sort((x, y) => x.date.localeCompare(y.date)).map((t) =>
          '<div class="trade ' + (t.side === "compra" ? "b" : "s") + '"><b>' + (t.side === "compra" ? "🟢 Compra" : "🔴 Venda") + '</b> · ' + esc(t.date) + ' · ' + fmtNum(t.qty, 4) + ' a ' + fmtNum(t.price) +
          ' · ' + fmtEur(t.eur) + (t.side === "venda" ? ' · <span class="' + cls(t.pnl_eur) + '">' + fmtEur(t.pnl_eur) + '</span>' + (t.held != null ? ' em ' + t.held + ' dias' : '') : '') +
          '<p class="small why">' + esc(t.why || t.reason || "") + '</p></div>').join("") + '</details>';
    }).join("") || '<p class="muted">Ainda sem operações.</p>') +
    '<details><summary>Todas as operações em lista (' + a.trades.length + ')</summary>' +
    table(["Data", "Tipo", "Ativo", "Qtd.", "Preço", "Valor", "Resultado", "Motivo"],
      a.trades.map((t) => [esc(t.date), t.side === "compra" ? "🟢 compra" : t.side === "venda" ? "🔴 venda" : "🧾 " + esc(t.side), '<b>' + esc(t.symbol) + '</b>',
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
  return '<div class="grid">' +
    tile("Rentabilidade no período", fmtPct(b.return_pct, 0), b.bankrupt ? "💀 faliu em " + esc(b.bankrupt) : "sem falência", cls(b.return_pct)) +
    tile("Queda máxima", fmtPct(-b.max_dd_pct, 0), "", "down") +
    tile("Sharpe", fmtNum(b.sharpe), (b.trades ?? "—") + (b.trades === 1 ? " operação" : " operações")) +
    tile("Parâmetros", b.adopted ? "novos adotados" : "mantidos", Object.entries(b.chosen || {}).map(([k, v]) => esc(k) + " " + esc(v)).join(" · ")) + '</div>' +
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

function show(i, scroll) {
  const a = agents[i];
  if (!a) return;
  document.querySelectorAll(".chip").forEach((c) => c.classList.toggle("on", +c.dataset.i === i));
  $("detail").innerHTML = '<div class="panel"><h2>' + esc(a.emoji + " " + a.name) + '</h2>' +
    (a.backtest ? backtestHtml(a) : liveHtml(a)) + strategyHtml(a.strategy) + '</div>';
  if (scroll) { openTab("agentes"); $("detail").scrollIntoView({ behavior: "smooth", block: "start" }); }
}
if (agents.length) show(0, false);

// ---- tabs ----
function openTab(name) {
  document.querySelectorAll("#tabs .tab").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  document.querySelectorAll("section[data-tab]").forEach((sec) => { sec.hidden = sec.dataset.tab !== name; });
  if (name === "corrida" && window.Plotly) Plotly.Plots.resize("chart");
  if (name === "pretreino" && window.Plotly && $("pt-chart")) Plotly.Plots.resize("pt-chart");
  try { history.replaceState(null, "", "#" + name); } catch (e) {}
}
$("tabs").addEventListener("click", (e) => { const b = e.target.closest(".tab"); if (b) openTab(b.dataset.tab); });

// ---- automatic refresh: the portal checks every minute (while visible) whether anything new was published ----
(function autoRefresh() {
  if (!PORTAL_VERSION) return;
  try {  // back where the person was after a refresh
    const y = sessionStorage.getItem("portal-scroll");
    if (y !== null) { sessionStorage.removeItem("portal-scroll"); setTimeout(() => window.scrollTo(0, +y), 300); }
  } catch (e) {}
  const busy = () => {
    const a = document.activeElement;
    const tab = (document.querySelector("#tabs .tab.on") || {}).dataset?.tab;
    return (a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) || tab === "carteira";
  };
  const refresh = () => {
    try { sessionStorage.setItem("portal-scroll", String(window.scrollY)); } catch (e) {}
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
openTab(location.hash && document.querySelector('#tabs .tab[data-tab="' + location.hash.slice(1) + '"]') ? location.hash.slice(1)
  : document.querySelector("#tabs .tab").dataset.tab);
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
  $("glossary").innerHTML = '<h2>❓ Glossário</h2><p class="muted small">Os termos que aparecem no portal, por ordem alfabética. Nas caixas, os termos sublinhados abrem esta explicação ao tocar.</p>' +
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
const PERIODS = [["1D", 1], ["1S", 7], ["1M", 30], ["3M", 91], ["6M", 182], ["1A", 365]];
function perfTable(boxId, data, people) {
  const box = $(boxId);
  if (!box) return;
  const series = (data.series || []).filter((s) => s.x.length);
  if (!series.length) { box.innerHTML = '<p class="muted">Ainda sem histórico.</p>'; return; }
  const byName = Object.fromEntries((people || []).map((a) => [a.name, a]));
  const last = series.reduce((m, s) => (s.x[s.x.length - 1] > m ? s.x[s.x.length - 1] : m), "");
  const first = series.reduce((m, s) => (s.x[0] < m ? s.x[0] : m), "9999");
  const shift = (day, days) => new Date(Date.parse(day + "T12:00:00Z") - days * 864e5).toISOString().slice(0, 10);
  // the value (% since the agent started) on the last point on or before a date; null before it started
  const at = (s, day) => { let v = null; for (let i = 0; i < s.x.length && s.x[i] <= day; i++) v = s.y[i]; return v; };
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
  const tail = series[0].x.slice(-21);
  const gaps = tail.slice(1).map((d, i) => (Date.parse(d) - Date.parse(tail[i])) / 864e5).sort((a, b) => a - b);
  const step = gaps.length ? gaps[Math.floor(gaps.length / 2)] : 1;
  let sortCol = "all";
  function draw() {
    const cols = PERIODS.filter(([, d]) => d >= step).map(([k, d]) => ({ key: k, label: k, from: shift(last, d), to: last }))
      .concat([{ key: "custom", label: "Personalizado", from: custom.from, to: custom.to }, { key: "all", label: "Desde o início", from: "0000-01-01", to: last }]);
    const rows = series.map((s) => ({ s, a: byName[s.name], cells: Object.fromEntries(cols.map((c) => [c.key, between(s, c.from, c.to, c.key === "all")])) }));
    const best = Object.fromEntries(cols.map((c) => [c.key, Math.max(...rows.map((r) => r.cells[c.key].v ?? -Infinity))]));
    rows.sort((x, y) => (y.cells[sortCol].v ?? -Infinity) - (x.cells[sortCol].v ?? -Infinity));
    box.innerHTML = '<div class="filters"><label class="small">Personalizado: de <input type="date" class="pf-from" value="' + custom.from + '" min="' + first + '" max="' + last + '"></label>' +
      '<label class="small">a <input type="date" class="pf-to" value="' + custom.to + '" min="' + first + '" max="' + last + '"></label>' +
      '<span class="muted small">Toca no título de uma coluna para ordenar. Dados até ' + esc(last) + '.</span></div>' +
      '<div class="scroll"><table><tr><th>Agente</th>' + cols.map((c) => '<th class="num"><a href="#" class="pf-sort" data-k="' + c.key + '">' + esc(c.label) + (sortCol === c.key ? " ▼" : "") + '</a></th>').join("") + '</tr>' +
      rows.map((r) => '<tr><td>' + esc((r.a ? r.a.emoji + " " : "") + r.s.name) + '</td>' + cols.map((c) => {
        const x = r.cells[c.key];
        if (x.v == null) return '<td class="num muted">—</td>';
        return '<td class="num"><span class="' + cls(x.v) + '">' + (x.v === best[c.key] && rows.length > 1 ? "🏆 " : "") + fmtPct(x.v) + (x.late ? "*" : "") + '</span></td>';
      }).join("") + '</tr>').join("") + '</table></div>' +
      '<p class="muted small">Rentabilidade em cada período, com custos e impostos. * o agente começou depois do início do período: conta desde o primeiro dia dele.</p>';
    box.querySelectorAll(".pf-sort").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); sortCol = a.dataset.k; draw(); }));
    box.querySelector(".pf-from").addEventListener("change", (e) => { custom.from = e.target.value || custom.from; sortCol = "custom"; draw(); });
    box.querySelector(".pf-to").addEventListener("change", (e) => { custom.to = e.target.value || custom.to; sortCol = "custom"; draw(); });
  }
  draw();
}
perfTable("perf", DATA, agents);
if (PRETRAIN) perfTable("pt-perf", PRETRAIN, PRETRAIN.agents || []);

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
(function today() {
  if (!$("today")) return;
  const M = MARKET || {}, P = PROPOSALS, c = M.context || {};
  const zone = M.zone || P?.zone || "—", colour = { Alta: "var(--up)", Neutra: "var(--gold)", Cautelosa: "#ff9f43", Queda: "var(--down)" }[zone] || "var(--muted)";
  const ctx = [c.vix != null ? ["VIX", fmtNum(c.vix, 0), c.vix > 30 ? "down" : c.vix < 18 ? "up" : ""] : null,
    c.epu_pct != null ? ["Incerteza política", "percentil " + fmtNum(c.epu_pct * 100, 0), c.epu_pct > 0.8 ? "down" : ""] : null,
    c.credit_dd != null ? ["Crédito de alto risco", fmtNum(c.credit_dd * 100, 1) + "% abaixo do melhor do ano", c.credit_dd > 0.05 ? "down" : c.credit_dd < 0.01 ? "up" : ""] : null,
    c.hy != null ? ["Spread de crédito", fmtNum(c.hy, 1) + "%", c.hy > 5 ? "down" : ""] : null,
    c.usd_chg != null ? ["Dólar vs euro (3 meses)", fmtPct(-c.usd_chg * 100), c.usd_chg > 0.03 ? "down" : ""] : null,
    c.spy_trend != null ? ["S&P 500 vs média 200d", fmtPct(c.spy_trend * 100), cls(c.spy_trend)] : null,
    c.gpr_pct != null ? ["Risco geopolítico (índice)", "percentil " + fmtNum(c.gpr_pct * 100, 0), c.gpr_pct > 0.85 ? "down" : ""] : null,
    c.oil_chg != null ? ["Petróleo (3 meses)", fmtPct(c.oil_chg * 100), c.oil_chg > 0.25 ? "down" : ""] : null,
    c.gold_chg != null ? ["Ouro (3 meses)", fmtPct(c.gold_chg * 100), c.gold_chg > 0.12 ? "down" : ""] : null].filter(Boolean);
  const G = GEO;
  const geoHtml = G && G.risk != null ? '<div class="panel"><div class="top" style="display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center">' +
    '<span class="pixel" style="font-size:.75rem;color:' + (G.risk >= 4 ? "var(--down)" : G.risk >= 3 ? "var(--gold)" : "var(--up)") + '">🌍 RISCO GEOPOLÍTICO E MACRO: ' + G.risk + '/5</span>' +
    '<span class="muted small">lido pelo Claude às ' + esc(lisbon(Date.parse(G.at), { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })) + '</span></div>' +
    '<p>' + esc(G.summary || "") + '</p>' + ((G.themes || []).length ? '<div class="cards">' + G.themes.map((t) => '<div class="opp"><b>' + esc(t.theme) + '</b><p class="small">' + esc(t.impact) + '</p>' +
      ((t.winners || []).length ? '<p class="small up">▲ ' + t.winners.map(esc).join(", ") + '</p>' : '') + ((t.losers || []).length ? '<p class="small down">▼ ' + t.losers.map(esc).join(", ") + '</p>' : '') + '</div>').join("") + '</div>' : '') +
    '</div>' : '';
  const leaders = agents.slice().sort((a, b) => headline(b) - headline(a)).slice(0, 3);
  const news = ((NEWS || {}).items || []).filter((i) => (i.importancia || 0) >= 2).slice(0, 3);
  $("today").innerHTML = '<h2>🏠 Hoje</h2>' +
    '<div class="panel"><div class="top" style="display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center"><span class="pixel" style="color:' + colour + ';font-size:.8rem">MERCADO: ' + esc(zone.toUpperCase()) + '</span>' +
    (M.stage2_pct != null ? '<span class="muted small">' + fmtNum(M.stage2_pct, 0) + '% das ações do S&P 500 em <abbr class="g" title="' + esc(GLOSS["Stage 2"]) + '">Stage 2</abbr></span>' : '') + '</div>' +
    (M.stage2_pct != null ? '<div class="bar" style="margin:8px 0"><i style="width:' + M.stage2_pct + '%;background:' + colour + '"></i></div>' : '') +
    (ctx.length ? '<div class="grid">' + ctx.map(([k, v, cl]) => tile(k, v, "", cl)).join("") + '</div>' : '') + '</div>' +
    geoHtml +
    '<h2>🧠 Melhores propostas</h2>' + (P && P.buys.some((b) => !b.conflict) ? '<div class="cards">' + P.buys.filter((b) => !b.conflict).slice(0, 3).map((b) => '<div class="opp"><div class="top"><span class="sym">' + esc(b.symbol) +
      '</span><span class="muted">' + esc(b.name || "") + '</span>' + (b.safety != null ? '<span class="score ' + esc((b.level || "").split(" ")[0]) + '">🛡️ ' + b.safety + '</span>' : '') + '</div><p class="small">Confiança ' +
      fmtNum(b.confidence, 0) + '% · entrada até ' + fmtNum(b.plan.entry_max) + ' · stop ' + fmtNum(b.plan.stop) + ' · alvo ' + fmtNum(b.plan.target) + ' (' + fmtNum(b.plan.rr, 1) + 'R)</p></div>').join("") +
      '</div><p><a href="#propostas" class="go" data-t="propostas">Ver todas as propostas →</a></p>' : '<p class="muted">Sem propostas hoje.</p>') +
    '<h2>💼 A tua carteira</h2><div id="today-wallet"><p class="muted">Abre a tua carteira uma vez neste aparelho (<a href="/entrar">entrar com o Discord ou Google</a>, botão "A minha carteira" no Discord ou <code>/carteira</code>) e o resumo aparece aqui.</p></div>' +
    '<h2>📰 Notícias do dia</h2><div class="news">' + (news.map((i) => '<article class="' + (i.sentimento > 0 ? "pos" : i.sentimento < 0 ? "neg" : "") + '"><b>' + esc(i.symbol) + '</b> ' +
      esc(i.resumo || i.title) + '</article>').join("") || '<p class="muted">Sem notícias importantes.</p>') + '</div><p><a href="#noticias" class="go" data-t="noticias">Ver as notícias →</a></p>' +
    '<h2>🏁 Liga</h2><div class="grid">' + leaders.map((a, i) => tile(["🥇", "🥈", "🥉"][i] + " " + esc(a.emoji + " " + a.name), fmtPct(headline(a)), esc(a.strategy.label), cls(headline(a)))).join("") + '</div>' +
    '<h2>⏱️ A seguir</h2><div class="grid">' + UPCOMING.slice(0, 3).map((j) => tile(esc(j.label), lisbon(j.at, { weekday: "short", hour: "2-digit", minute: "2-digit" }))).join("") + '</div>';
  $("today").addEventListener("click", (e) => { const a = e.target.closest(".go"); if (a) { e.preventDefault(); openTab(a.dataset.t); window.scrollTo(0, 0); } });
  const path = store.get("carteira", null);
  if (path && /^\/c\/[0-9a-f]{24}$/.test(path)) fetch(path + "/data", { cache: "no-store" }).then((r) => r.ok ? r.json() : null).then((w) => {
    if (!w) return;
    let value = 0, cost = 0, day = 0;
    const pos = {};
    for (const t of w.tx.slice().sort((a, b) => a.date.localeCompare(b.date) || a.seq - b.seq)) {
      const p = pos[t.symbol] ||= { qty: 0, cost: 0, cur: t.currency };
      if (t.side === "buy") { p.qty += t.qty; p.cost += t.qty * t.price + t.fee; } else { const avg = p.qty ? p.cost / p.qty : 0; p.cost -= avg * t.qty; p.qty -= t.qty; }
    }
    for (const [sym, p] of Object.entries(pos)) { const q = w.quotes[sym], fx = w.fx[p.cur]; if (!q || fx == null || p.qty <= 1e-9) continue;
      value += p.qty * q.price * fx; cost += p.cost * fx; day += p.qty * (q.price - (q.prev ?? q.price)) * fx; }
    const sells = Object.entries(w.verdicts || {}).filter(([k, v]) => k !== "rules" && v.verdict === "Vender").map(([k]) => k);
    const follows = (w.follows || []).map((f) => esc(f.symbol) + (f.change != null ? ' <span class="' + cls(f.change) + '">' + fmtPct(f.change * 100) + '</span>' : '')).join(" · ");
    $("today-wallet").innerHTML = '<div class="grid">' + tile("Valor", fmtEur(value)) + tile("Ganho", fmtEur(value - cost), fmtPct(cost ? (value / cost - 1) * 100 : null), cls(value - cost)) +
      tile("Hoje", fmtEur(day), "", cls(day)) + tile("Sinais de venda", sells.length ? esc(sells.join(", ")) : "nenhum", "veredito da carteira", sells.length ? "down" : "up") + '</div>' +
      (follows ? '<p class="small">👀 A seguir: ' + follows + '</p>' : '') + '<p><a href="' + esc(path) + '">Abrir a minha carteira →</a></p>';
  }).catch(() => {});
})();


// ---- overview of all agents ----
(function overview() {
  if (!agents.length) return;
  const live = !agents[0].backtest;
  const rows = agents.slice().sort((a, b) => headline(b) - headline(a));
  $("overview").innerHTML = live
    ? table(["#", "Agente", "Património", "Rentab.", "Hoje", "Pontos", "Queda máx.", "Sharpe", "Posições", "Investido", "Compras/vendas", "Estratégia"],
        rows.map((a, k) => [k + 1, '<a href="#" data-i="' + agents.indexOf(a) + '">' + esc(a.emoji + " " + a.name) + '</a>', fmtEur(a.equity),
          '<span class="' + cls(a.return_pct) + '">' + fmtPct(a.return_pct) + '</span>', '<span class="' + cls(a.day_pct) + '">' + fmtPct(a.day_pct) + '</span>',
          fmtNum(a.points, 0), fmtPct(-a.max_dd_pct), fmtNum(a.sharpe), a.positions.length, fmtNum(a.invested_pct, 0) + "%", a.buys + "/" + a.sells,
          esc(a.strategy.label)]), [2, 3, 4, 5, 6, 7, 8, 9])
    : table(["#", "Agente", "Rentab. no período", "Queda máx.", "Sharpe", "Operações", "Parâmetros", "Estratégia"],
        rows.map((a, k) => [k + 1, '<a href="#" data-i="' + agents.indexOf(a) + '">' + esc(a.emoji + " " + a.name) + '</a>',
          '<span class="' + cls(a.backtest.return_pct) + '">' + fmtPct(a.backtest.return_pct, 0) + '</span>', fmtPct(-a.backtest.max_dd_pct, 0),
          fmtNum(a.backtest.sharpe), a.backtest.trades ?? "—", a.backtest.adopted ? "novos" : "mantidos", esc(a.strategy.label)]), [2, 3, 4, 5]);
  $("overview").addEventListener("click", (e) => { const l = e.target.closest("a[data-i]"); if (l) { e.preventDefault(); show(+l.dataset.i, true); } });
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
  $("news").innerHTML = '<h2>📰 Notícias do mercado</h2><p class="muted small">' + (N.updated ? 'Lidas às ' + lisbon(Date.parse(N.updated), { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) + ' · ' : '') +
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

// ---- proposals ----
(function props() {
  if (!$("props")) return;
  const P = PROPOSALS;
  if (!P) { $("props").innerHTML = '<p class="muted">As propostas aparecem depois da próxima liga dos agentes (dias úteis às 07:30).</p>'; return; }
  const sb = (P.scoreboard || {}).all || {};
  const pctOrDash = (x) => x == null ? "—" : fmtNum(x * 100, 0) + "%";
  const sizing = (b, capital, riskPct) => {
    const pl = b.plan, perShare = (pl.entry_max - pl.stop) * pl.fx_eur;
    if (!(perShare > 0) || !(capital > 0)) return null;
    let qty = Math.floor(capital * riskPct / 100 / perShare * 1000) / 1000;
    const cap = capital * 0.2 / (pl.entry_max * pl.fx_eur);
    const capped = qty > cap; if (capped) qty = Math.floor(cap * 1000) / 1000;
    return { qty, value: qty * pl.entry_max * pl.fx_eur, risk: qty * perShare, capped };
  };
  const card = (b) => {
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
  const byBucket = (P.scoreboard || {}).by_bucket || {};
  $("props").innerHTML = '<h2>🧠 Propostas dos agentes para a abertura</h2><p class="muted small">Ordens que os agentes acabaram de decidir no fecho de ' + esc(P.as_of) +
    ', juntas e pesadas pelo historial de cada um (zona de mercado: <b>' + esc(P.zone) + '</b>). Para estudar e decidir tu: não é recomendação.</p>' +
    '<div class="panel filters"><label class="small">O teu capital (€) <input id="pp-cap" type="number" min="0" step="100"></label>' +
    '<label class="small">Risco por operação (%) <input id="pp-risk" type="number" min="0.1" max="5" step="0.1"></label>' +
    '<span class="muted small">Quantas ações: risco em euros ÷ (entrada − stop), no máximo 20% do capital numa posição. Fica guardado neste aparelho.</span></div>' +
    '<h2>🟢 Comprar (' + P.buys.length + ')</h2><div class="cards">' + (P.buys.map(card).join("") || '<p class="muted">Hoje nenhum agente quer comprar.</p>') + '</div>' +
    '<h2>🔴 Os agentes estão a vender (' + P.sells.length + ')</h2>' + table(["Ativo", "Quem vende", "Porquê", "Peso", "Score"], P.sells.map((x) =>
      ['<b>' + esc(x.symbol) + '</b> <span class="muted">' + esc(x.name || "") + '</span>', x.agents.map((a) => esc(a.emoji + " " + a.name)).join(", "),
       esc([...new Set(x.agents.map((a) => a.reason).filter(Boolean))].join(" · ")), fmtNum(x.support, 1), x.safety != null ? x.safety + "/100" : "—"]), [3]) +
    '<h2>📊 Placar das propostas</h2><p class="muted small">Cada proposta de compra é seguida: conta se a ação abriu até ao preço de entrada nas ' + P.method.fill_window +
    ' sessões seguintes, e depois se tocou primeiro no stop ou no alvo; ao fim de ' + P.method.horizon + ' sessões fecha ao preço do dia.</p>' +
    '<div class="grid">' + tile("Propostas", sb.proposals ?? 0, (sb.executed ?? 0) + " executadas · " + (sb.open ?? 0) + " abertas") +
    tile("Taxa de acerto", pctOrDash(sb.win_rate), (sb.targets ?? 0) + " alvos · " + (sb.stops ?? 0) + " stops", cls((sb.win_rate || 0) - 0.5)) +
    tile("R médio", sb.avg_r == null ? "—" : fmtNum(sb.avg_r, 2) + " R", "ganho por unidade de risco", cls(sb.avg_r)) +
    tile("Retorno médio", sb.avg_ret == null ? "—" : fmtPct(sb.avg_ret), "por proposta fechada", cls(sb.avg_ret)) + '</div>' +
    table(["Confiança", "Propostas", "Fechadas", "Acerto", "R médio"], ["alta", "média", "baixa"].map((k) => { const x = byBucket[k] || {};
      return [k + (k === "alta" ? " (≥40%)" : k === "média" ? " (20-40%)" : " (<20%)"), x.proposals ?? 0, x.closed ?? 0, pctOrDash(x.win_rate), x.avg_r == null ? "—" : fmtNum(x.avg_r, 2)]; }), [1, 2, 3, 4]) +
    '<details><summary>Últimas propostas e o que lhes aconteceu</summary>' + table(["Data", "Ativo", "Confiança", "Estado", "Compra", "Saída", "Resultado"],
      ((P.scoreboard || {}).recent || []).map((e) => [esc(e.date), '<b>' + esc(e.symbol) + '</b>', fmtNum(e.confidence, 0) + "%", esc(e.status),
        e.fill != null ? fmtNum(e.fill) : "—", e.exit != null ? fmtNum(e.exit) : (e.last != null ? fmtNum(e.last) + " (aberta)" : "—"),
        '<span class="' + cls(e.ret ?? e.ret_open) + '">' + (e.ret != null ? fmtPct(e.ret * 100) + (e.r != null ? " · " + fmtNum(e.r, 1) + " R" : "") : e.ret_open != null ? fmtPct(e.ret_open * 100) : "—") + '</span>']), [2, 4, 5, 6]) + '</details>' +
    '<h2>⚖️ Peso de cada agente hoje</h2>' + table(["Agente", "Peso", "Porquê", "Historial"], P.weights.map((w) => [esc(w.emoji + " " + w.name), fmtNum(w.weight, 2),
      esc(w.why.join(" · ") || "—"), w.stats && w.stats.trades ? pctOrDash(w.stats.win_rate) + " acerto em " + w.stats.trades + " vendas" : "—"]), [1]) +
    '<details class="panel"><summary><b>Como se calcula o peso</b></summary><ul class="small">' + P.rules.map((r) => '<li>' + esc(r) + '</li>').join("") + '</ul></details>';
  const cap = $("pp-cap"), risk = $("pp-risk");
  cap.value = store.get("capital", 10000); risk.value = store.get("risk", 1);
  const drawSizes = () => {
    store.set("capital", +cap.value); store.set("risk", +risk.value);
    document.querySelectorAll(".sizing").forEach((el) => {
      const b = P.buys.find((x) => x.symbol === el.dataset.sym), z = sizing(b, +cap.value, +risk.value);
      el.innerHTML = z ? '🧮 Para ti: <b>' + fmtNum(z.qty, 3) + ' ações</b> ≈ ' + fmtEur(z.value) + ', arriscando ' + fmtEur(z.risk) + (z.capped ? ' (limitado a 20% do capital)' : '') : "";
    });
  };
  [cap, risk].forEach((el) => el.addEventListener("input", drawSizes));
  drawSizes();
})();

// ---- opportunities ----
(function opps() {
  if (!$("opps")) return;
  if (!MARKET) { $("opps").innerHTML = '<p class="muted">As oportunidades aparecem depois da próxima liga dos agentes (dias úteis).</p>'; return; }
  const lv = (s) => (MARKET.levels.find((l) => s >= l.from) || { name: "" }).name;
  const badge = (r) => '<span class="score ' + esc(r.level.split(" ")[0]) + '">🛡️ ' + r.safety + '/100 · ' + esc(r.level) + '</span>';
  const W = MARKET.watch || [], R = MARKET.rules || {};
  const signals = (r) => '<span class="score ' + (r.severity >= 3 ? "Baixa" : "Média") + '">⚠️ ' + r.warnings.length + ' sina' + (r.warnings.length === 1 ? "l" : "is") + '</span>';
  const card = (r, kind) => '<div class="opp' + (kind === "sell" ? " sellcard" : kind === "watch" ? " watchcard" : "") + '"><div class="top"><span class="sym">' + esc(r.symbol) + '</span><span class="muted">' + esc(r.name || "") +
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
  $("opps").innerHTML = '<h2>🎯 Oportunidades de compra e venda</h2><p class="muted small">Fecho de ' + esc(MARKET.as_of) + ' · mercado em zona <b>' + esc(MARKET.zone) + '</b> (' +
    fmtNum(MARKET.stage2_pct, 0) + '% das ações do S&P 500 em tendência de alta) · ideias para estudar, não recomendações: confirma sempre antes de decidir. ' +
    'Cada ação aparece numa só lista.</p>' +
    '<details class="panel"><summary><b>🛡️ Como se calcula o score de segurança (0-100)</b></summary><p class="small">Mede quão ordenada e suportada está a ação agora: tendência, volatilidade, distância ao máximo, liquidez, qualidade da empresa, resultados, notícias e mercado. ' +
    'Não diz quanto vai subir. Níveis: ' + MARKET.levels.map((l) => esc(l.name) + " ≥ " + l.from).join(" · ") + '.</p>' +
    table(["Critério", "Máx.", "Regra"], MARKET.criteria.map((c) => [esc(c.label), c.max, esc(c.rule)]), [1]) + '</details>' +
    ((MARKET.sectors || []).length ? '<details class="panel"><summary><b>🏭 Saúde dos setores</b> <span class="muted small">(ETF de cada setor)</span></summary>' +
      table(["Setor", "ETF", "Face à média de 200 dias", "Força vs S&P 500 (3 meses)", "Ações em tendência de alta"], MARKET.sectors.map((r) => [esc(r.sector), esc(r.etf),
        r.trend == null ? "—" : '<span class="' + cls(r.trend) + '">' + fmtPct(r.trend) + '</span>', r.rs == null ? "—" : '<span class="' + cls(r.rs) + '">' + fmtPct(r.rs) + '</span>',
        r.stage2 == null ? "—" : fmtNum(r.stage2, 0) + "%"]), [2, 3, 4]) +
      '<p class="muted small">Só se propõem compras em setores acima da média de 200 dias. Os agentes podem aprender a exigir também força face ao S&P 500.</p></details>' : '') +
    '<h2>🟢 Comprar (' + MARKET.buy.length + ')</h2><p class="muted small">Só as que passam em todos os filtros: tendência de alta, um sinal de entrada, não esticadas, sem resultados nem notícias negativas por perto, stop curto e score alto. Poucas e boas; há dias sem nenhuma.</p>' +
    rules(R.buy) + '<div id="buy" class="cards"></div>' +
    '<h2>🔴 Vender (' + MARKET.sell.length + ')</h2><p class="muted small">Ações dos agentes ou do ranking cuja tendência quebrou. Ordenadas pelo score, do mais alto para o mais baixo.</p>' + rules(R.sell) +
    '<div class="cards">' + (MARKET.sell.map((r) => card(r, "sell")).join("") || '<p class="muted">Nenhuma com a tendência quebrada.</p>') + '</div>' +
    '<h2>🟡 Atenção: não comprar agora (' + W.length + ')</h2><p class="muted small">Ações fortes com um motivo para esperar: resultados à porta, notícias negativas, perderam a média de 50 dias ou estão esticadas. Não são sinais de venda.</p>' + rules(R.watch) +
    '<div class="cards">' + (W.map((r) => card(r, "watch")).join("") || '<p class="muted">Nenhuma.</p>') + '</div>';
  const drawBuy = () => { $("buy").innerHTML = MARKET.buy.map((r) => card(r, "buy")).join("") ||
    '<p class="muted">Hoje nenhuma ação passa em todos os filtros. É normal: as boas oportunidades são raras e esperar também é uma decisão.</p>'; };
  drawBuy();
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
  const showPt = (a, scroll) => {
    document.querySelectorAll("#pt-chips .chip").forEach((c) => c.classList.toggle("on", c.dataset.n === a.name));
    $("pt-detail").innerHTML = '<div class="panel"><h2>' + esc(a.emoji + " " + a.name) + '</h2>' + (a.backtest ? backtestHtml(a) : "") + strategyHtml(a.strategy) + '</div>';
    if (scroll) $("pt-detail").scrollIntoView({ behavior: "smooth", block: "start" });
  };
  $("pt-chips").innerHTML = ptAgents.slice().sort((x, y) => (y.backtest?.return_pct ?? 0) - (x.backtest?.return_pct ?? 0)).map((a) =>
    '<button class="chip" data-n="' + esc(a.name) + '">' + esc(a.emoji + " " + a.name) + ' <span class="' + cls(a.backtest?.return_pct) + '">' + fmtPct(a.backtest?.return_pct, 0) + '</span></button>').join("");
  $("pt-chips").addEventListener("click", (e) => { const b = e.target.closest(".chip"); const a = b && ptAgents.find((x) => x.name === b.dataset.n); if (a) showPt(a, true); });
  race("pt-", PRETRAIN, ptAgents, (a) => showPt(a, true));
  lineChart("pt-chart", PRETRAIN);
  if (ptAgents.length) showPt(ptAgents.slice().sort((x, y) => (y.backtest?.return_pct ?? 0) - (x.backtest?.return_pct ?? 0))[0], false);
}
`;

STATIC_PATHS.css = staticAsset("style", CSS, "css");
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
  const fmtRun = (r, i) => (i + 1) + "ª · " + new Date(r.at).toLocaleDateString("pt-PT", { day: "2-digit", month: "2-digit" }) + (r.source === "local" ? " 💻" : "");
  box.innerHTML = '<p class="muted small">Cada ponto é uma execução do pré-treino (💻 = no PC). Uma execução só troca os parâmetros de um agente quando encontra melhores no treino e no exame em conjunto.</p>' +
    '<div class="filters"><select id="evo-m">' + metrics.map(([k, l]) => '<option value="' + k + '">' + esc(l) + '</option>').join("") + '</select></div>' +
    '<div id="evo-chart" class="chart" style="height:420px"></div><h3>🔧 O que mudou em cada agente</h3><div id="evo-changes"></div>';
  const x = E.runs.map(fmtRun);
  const draw = () => {
    const m = $("evo-m").value;
    const traces = Object.entries(E.series).filter(([s]) => s !== "indexador" || m !== "exam").map(([s, rows]) => ({
      x, y: rows.map((r) => r ? r[m] : null), name: label(s), type: "scatter", mode: "lines+markers", connectgaps: true,
      hovertemplate: "%{y}<extra>" + esc(label(s)) + "</extra>" }));
    if (!window.Plotly) { $("evo-chart").innerHTML = '<p class="muted">Gráfico indisponível.</p>'; return; }
    Plotly.newPlot("evo-chart", traces, { paper_bgcolor: "#17123a", plot_bgcolor: "#17123a", font: { color: "#e8e6ff", size: 11 },
      margin: { l: 48, r: 10, t: 10, b: 60 }, xaxis: { gridcolor: "#2e2763", tickangle: -30 }, yaxis: { gridcolor: "#2e2763", zerolinecolor: "#80848e" },
      legend: window.innerWidth < 640 ? { orientation: "h", y: -0.35 } : { bgcolor: "rgba(0,0,0,0)" } }, { responsive: true, displaylogo: false });
  };
  $("evo-m").addEventListener("input", draw);
  draw();
  const rows = Object.entries(E.changes || {}).flatMap(([s, list]) => list.slice(-3).map((c) => [c.run, s, c]))
    .sort((a, b) => b[0] - a[0]).slice(0, 40);
  $("evo-changes").innerHTML = rows.length ? table(["Execução", "Agente", "Ligou", "Desligou", "Ajustou"], rows.map(([run, s, c]) => [
    esc(fmtRun(E.runs[run], run)), esc(label(s)), c.on.map((k) => '<code>' + esc(k) + '</code>').join(" ") || "—",
    c.off.map((k) => '<code>' + esc(k) + '</code>').join(" ") || "—", c.moved.map((k) => '<code>' + esc(k) + '</code>').join(" ") || "—"])) :
    '<p class="muted">Ainda sem mudanças registadas.</p>';
})();

// ---- the member's portfolio inside the portal (same site, so the frame grows to fit) ----
(function walletFrame() {
  const f = $("wallet-frame");
  if (!f) return;
  const load = () => { if (!f.src) f.src = f.dataset.src; };
  const fit = () => { try { const d = f.contentDocument; if (d && d.body) f.style.height = Math.max(600, d.documentElement.scrollHeight) + "px"; } catch (e) {} };
  f.addEventListener("load", () => {
    fit();
    try { new ResizeObserver(fit).observe(f.contentDocument.body); } catch (e) { setInterval(fit, 1000); }
  });
  document.querySelectorAll('#tabs .tab[data-tab="carteira"]').forEach((b) => b.addEventListener("click", load));
  document.addEventListener("click", (e) => { if (e.target.closest('[data-go="carteira"]')) { e.preventDefault(); load(); openTab("carteira"); } });
  if (location.hash === "#carteira") load();
  document.addEventListener("click", (e) => {  // "Abrir a minha carteira" links open the tab instead of another page
    const a = e.target.closest('a[href^="/c/"]');
    if (a && !a.closest(".wallet-frame") && !/\/export/.test(a.getAttribute("href"))) { e.preventDefault(); load(); openTab("carteira"); window.scrollTo(0, 0); }
  });
})();

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
      (kind === "buy" && r.stop ? '<p class="small">🛑 Stop sugerido ' + fmtNum(r.stop) + '</p>' : '')).join("") + '<p class="small"><a href="#oportunidades" data-go="oportunidades">Ver em Oportunidades →</a></p></div>';
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

// ---- status: when each part was last updated ----
(function status() {
  if (!$("status")) return;
  const when = (x) => x && (x.updated || x.as_of || x.date || x.created);
  const hours = (iso) => iso ? (Date.now() - Date.parse(iso.length === 10 ? iso + "T18:00:00Z" : iso)) / 3.6e6 : null;
  const weekendSlack = [0, 1].includes(new Date().getDay()) ? 48 : 0;  // Sunday and Monday: Friday's data is fine
  const rows = [
    ["🏁 Liga dos agentes", DATA.updated, 30 + weekendSlack, "dias úteis às 07:30"],
    ["📰 Notícias", NEWS && NEWS.updated, 8 + weekendSlack, "várias vezes por dia"],
    ["🎯 Oportunidades e contexto", when(MARKET), 30 + weekendSlack, "com a liga"],
    ["🧠 Propostas", when(PROPOSALS), 30 + weekendSlack, "com a liga"],
    ["📅 Agenda", when(CALENDAR), 30 + weekendSlack, "com a liga"],
    ["⏪ Pré-treino", PRETRAIN && PRETRAIN.updated, 24 * 8, "ao domingo"],
  ];
  const dot = (h, max) => h == null ? '<span class="dot bad"></span>sem dados' : h <= max ? '<span class="dot ok"></span>em dia' : h <= max * 2 ? '<span class="dot old"></span>atrasado' : '<span class="dot bad"></span>parado';
  const ago = (h) => h == null ? "—" : h < 1 ? Math.max(1, Math.round(h * 60)) + " min" : h < 48 ? Math.round(h) + " h" : Math.round(h / 24) + " dias";
  $("status").innerHTML = '<h2>🩺 Estado do sistema</h2><p class="muted small">Quando cada parte do portal foi atualizada pela última vez. As próximas atualizações estão em ⏱️ Agenda.</p>' +
    table(["Parte", "Estado", "Última atualização", "Há", "Costuma atualizar"], rows.map(([k, iso, max, every]) => { const h = hours(iso);
      return [k, dot(h, max), iso ? esc(lisbon(Date.parse(iso.length === 10 ? iso + "T18:00:00Z" : iso), { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })) : "—", ago(h), esc(every)]; })) +
    '<p class="muted small">🟢 em dia · 🟡 atrasado (mais do que o normal) · 🔴 parado ou sem dados. Se algo ficar parado, avisa no Discord: o canal 🔧-estado mostra os erros.</p>';
})();

// ---- quick guide on the first visit ----
(function guide() {
  if (!$("guide-open")) return;
  const steps = [
    ["🏠 Hoje", "O resumo do dia: o mercado, as melhores propostas dos agentes e a tua carteira. Começa sempre aqui."],
    ["🧠 Propostas e 🎯 Oportunidades", "O que os agentes (estratégias simuladas, treinadas em 20 anos de mercado) querem comprar ou vender, com plano (entrada, stop, alvo) e um score de segurança explicado. São ideias para estudar, não ordens."],
    ["💼 A tua carteira", "Entra com o Discord (ou uma conta Google associada), importa o histórico da XTB e acompanha tudo em tempo quase real. Podes pedir uma análise ao Claude. Usa a 🔎 pesquisa para ver tudo sobre um ticker."],
  ];
  let i = 0;
  const box = document.body.appendChild(Object.assign(document.createElement("div"), { id: "guide", hidden: true }));
  const draw = () => {
    box.innerHTML = '<div class="panel"><p class="steps">PASSO ' + (i + 1) + ' DE ' + steps.length + '</p><h2>' + esc(steps[i][0]) + '</h2><p>' + esc(steps[i][1]) + '</p>' +
      '<p class="row-btns"><button class="btn" id="g-next">' + (i < steps.length - 1 ? "SEGUINTE" : "COMEÇAR") + '</button><button class="btn alt" id="g-skip">SALTAR</button></p></div>';
    $("g-next").onclick = () => { if (i < steps.length - 1) { i++; draw(); } else close(); };
    $("g-skip").onclick = close;
  };
  const close = () => { box.hidden = true; try { localStorage.setItem("guia-visto", "1"); } catch (e) {} };
  const open = () => { i = 0; draw(); box.hidden = false; };
  $("guide-open").onclick = open;
  box.addEventListener("click", (e) => { if (e.target === box) close(); });
  let seen = false;
  try { seen = localStorage.getItem("guia-visto") === "1"; } catch (e) { seen = true; }
  if (!seen) open();
})();
`;
STATIC_PATHS.extra = staticAsset("portal-extra", EXTRA, "js");
