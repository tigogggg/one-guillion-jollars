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
      if (job.weekdays && weekday) times = [].concat(schedule[job.key] || []);
      if (job.key === "semanal" && DAYS[dow] === schedule.semanal?.dia) times = [schedule.semanal.hora];
      if (job.key === "treino" && weekday && (d === 1 || (dow === 1 && d <= 3))) times = [schedule.treino];
      for (const t of times.slice().sort()) {
        const [h, mi] = String(t).split(":").map(Number);
        const at = lisbonToEpoch(y, m, d, h, mi);
        if (at > now) { next = at; break; }
      }
    }
    const all = job.weekdays ? [].concat(schedule[job.key] || []) : [];
    out.push({ key: job.key, label: job.label, at: next, times: all });
  }
  return out.filter((j) => j.at).sort((a, b) => a.at - b.at);
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

function html(body, status = 200) {
  return new Response(body, {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "X-Robots-Tag": "noindex" },
  });
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// JSON inside <script>: no "</script>" or line separators can break out.
function safeJson(value) {
  return JSON.stringify(value).replace(/</g, "\\u003c").replace(/\u2028/g, "\\u2028").replace(/\u2029/g, "\\u2029");
}

function page(title, body, head = "") {
  return `<!doctype html>
<html lang="pt-PT">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>${esc(title)}</title>
<style>${CSS}</style>
${head}
</head>
<body><main>${body}</main></body>
</html>`;
}

function chartPage(data, upcoming, newer) {
  const banner = newer
    ? `<a class="banner" href="/g/${esc(newer)}">🔄 Há uma versão mais recente deste gráfico · abrir</a>`
    : "";
  const body = `
${banner}
<header>
  <h1>${esc(data.title)}</h1>
  <p class="muted">${esc(data.subtitle || "")}</p>
  <p class="muted small">Dados de <span id="updated"></span> (hora de Lisboa) · simulação com dinheiro fictício e preços reais · não é recomendação de compra</p>
</header>
<section class="race" id="race">
  <div class="marquee"><span>🏁 GRANDE PRÉMIO DOS AGENTES 🏁</span></div>
  <div class="hud">
    <div>DIA <b id="race-date">—</b></div>
    <div>LÍDER <b id="race-leader">—</b></div>
    <div class="controls"><button id="race-play" class="btn">▶ VER A CORRIDA</button><button id="race-speed" class="btn alt">1×</button></div>
  </div>
  <div id="track" class="track"></div>
  <input type="range" id="race-scrub" min="0" value="0" aria-label="Dia da corrida">
  <p class="muted small">Cada corredor avança com a rentabilidade do agente; a meta é o melhor resultado da corrida. Toca num corredor para abrir a ficha dele.</p>
  <h2 class="pixel">🏆 Placar</h2>
  <ol id="board" class="board"></ol>
</section>
<section>
  <h2>⏱️ Próximas atualizações</h2>
  <div id="countdowns" class="grid"></div>
</section>
<section>
  <div id="chart" class="chart"></div>
  <p class="muted small">Toca numa linha da legenda para a esconder ou mostrar, toca duas vezes para ver só essa. Arrasta para fazer zoom; duplo toque no gráfico volta ao início. Toca numa linha para abrir a ficha do agente.</p>
</section>
<section id="agents-section">
  <h2>🧾 Agentes</h2>
  <div id="chips" class="chips"></div>
  <div id="detail"></div>
</section>
<script>
const DATA = ${safeJson(data)};
const UPCOMING = ${safeJson(upcoming)};
${CLIENT}
</script>`;
  return page(data.title, body, `<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Press+Start+2P&display=swap" rel="stylesheet">
<script src="${PLOTLY}"></script>`);
}

const CSS = `
:root { color-scheme: dark; --bg:#0b0820; --card:#17123a; --line:#2e2763; --fg:#e8e6ff; --muted:#9b96c9; --up:#39ff88; --down:#ff4d6d; --accent:#ff2e88; --neon:#00e5ff; --gold:#ffe14d; --pixel:"Press Start 2P",ui-monospace,monospace; }
* { box-sizing: border-box; }
body { margin:0; background:radial-gradient(circle at 50% -20%,#2a1a6b 0,var(--bg) 60%) fixed; color:var(--fg); font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
body::after { content:""; position:fixed; inset:0; pointer-events:none; background:repeating-linear-gradient(0deg,rgba(0,0,0,.18) 0 1px,transparent 1px 3px); }
h1, h2, .pixel { font-family:var(--pixel); letter-spacing:.5px; } h1 { color:var(--gold); text-shadow:0 0 8px rgba(255,225,77,.6); line-height:1.5; }
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
#race-scrub { width:100%; margin:10px 0 0; accent-color:var(--accent); }
.board { list-style:none; padding:0; margin:0; display:grid; gap:6px; counter-reset:pos; }
.board li { display:flex; align-items:center; gap:10px; background:var(--card); border:2px solid var(--line); border-radius:6px; padding:8px 10px; cursor:pointer; }
.board li .pos { font-family:var(--pixel); font-size:.65rem; width:34px; color:var(--gold); }
.board li:nth-child(1) { border-color:var(--gold); box-shadow:0 0 10px rgba(255,225,77,.45); }
.board li:nth-child(2) { border-color:#c0c7d6; } .board li:nth-child(3) { border-color:#d08a4a; }
.board li .nm { flex:1; font-weight:600; } .board li .val { font-family:var(--pixel); font-size:.62rem; }
main { max-width:1100px; margin:0 auto; padding:16px; }
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
const palette = ["#4cc0a8", "#e2b84a", "#5865f2", "#ed4245", "#57f287", "#eb459e", "#f0b232", "#9b84ee", "#3ba5d8", "#dbdee1"];
const highlight = new Set(DATA.highlight || []);
let colour = 0;
const traces = (DATA.series || []).map((s) => {
  const on = !highlight.size || highlight.has(s.name);
  return { x: s.x, y: s.y, name: s.name, type: "scatter", mode: s.x.length < 3 ? "lines+markers" : "lines",
    line: { width: on ? 2 : 1, color: on ? palette[colour++ % palette.length] : "#6d6f78" }, opacity: on ? 1 : 0.55,
    hovertemplate: "%{y:+.1f}%<extra>" + esc(s.name) + "</extra>" };
}).concat((DATA.references || []).map((s, i) => ({ x: s.x, y: s.y, name: s.name, type: "scatter", mode: "lines",
  line: { width: 1.2, dash: i ? "dot" : "dash", color: i ? "#b5bac1" : "#80848e" },
  hovertemplate: "%{y:+.1f}%<extra>" + esc(s.name) + "</extra>" })));
const xs = traces.flatMap((t) => [t.x[0], t.x[t.x.length - 1]]).filter(Boolean).sort();
const spanYears = xs.length ? (Date.parse(xs[xs.length - 1]) - Date.parse(xs[0])) / 3.15e10 : 0;
const narrow = window.innerWidth < 640;
if (!window.Plotly) $("chart").innerHTML = '<p class="muted" style="padding:16px">Não consegui carregar o gráfico (Plotly). As fichas dos agentes estão em baixo.</p>';
else Plotly.newPlot("chart", traces, {
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

// ---- agents ----
const agents = DATA.agents || [];
if (!agents.length) $("agents-section").style.display = "none";
const byName = Object.fromEntries(agents.map((a) => [a.name, a]));
const headline = (a) => a.backtest ? a.backtest.return_pct : a.return_pct;
$("chips").innerHTML = agents.map((a, i) =>
  '<button class="chip" data-i="' + i + '">' + esc(a.emoji + " " + a.name) + ' <span class="' + cls(headline(a)) + '">' +
  fmtPct(headline(a)) + '</span></button>').join("");
$("chips").addEventListener("click", (e) => { const b = e.target.closest(".chip"); if (b) show(+b.dataset.i, true); });
if (window.Plotly) $("chart").on("plotly_click", (ev) => { const a = byName[ev.points[0].data.name]; if (a) show(agents.indexOf(a), true); });

const tile = (k, v, s = "", c = "") => '<div class="tile"><div class="k">' + k + '</div><div class="v ' + c + '">' + v + '</div>' + (s ? '<div class="s">' + s + '</div>' : "") + '</div>';
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
    '<h3>💼 Carteira (' + a.positions.length + ' posições)</h3>' +
    table(["Ativo", "Nome", "Qtd.", "Custo médio", "Preço atual", "Investido", "Valor", "Ganho", "Ganho %", "Peso", "Stop", "Alvo", "Desde", "Resultados"],
      a.positions.map((p) => ['<b>' + esc(p.symbol) + '</b>', esc(p.name || p.sector || ""), fmtNum(p.qty, 4),
        fmtNum(p.entry) + " " + p.currency, (p.price == null ? "—" : fmtNum(p.price) + " " + p.currency), fmtEur(p.cost_eur), fmtEur(p.value_eur),
        '<span class="' + cls(p.pnl_eur) + '">' + fmtEur(p.pnl_eur) + '</span>', '<span class="' + cls(p.pnl_pct) + '">' + fmtPct(p.pnl_pct) + '</span>',
        fmtNum(p.weight, 1) + "%", p.stop ? fmtNum(p.stop) : "—", p.tp ? fmtNum(p.tp) : "—", esc(p.entry_date || ""), esc(p.earnings || "—")]),
      [2, 3, 4, 5, 6, 7, 8, 9, 10, 11]) +
    '<p class="muted small">Custo médio e preço na moeda do ativo, com custos incluídos; valores em euros ao câmbio do dia.</p>' +
    '<h3>⏳ Ordens para a próxima abertura</h3>' +
    table(["Tipo", "Ativo", "Valor", "Stop", "Alvo", "Motivo", "Criada"],
      (a.pending || []).map((o) => [o.side === "buy" ? "🟢 compra" : "🔴 venda", '<b>' + esc(o.symbol) + '</b>', o.eur ? fmtEur(o.eur) : (o.fraction ? fmtNum(o.fraction * 100, 0) + "% da posição" : "tudo"),
        o.stop ? fmtNum(o.stop) : "—", o.tp ? fmtNum(o.tp) : "—", esc(o.reason || ""), esc(o.created || "")]), [2, 3, 4]) +
    '<h3>📜 Histórico de operações (' + a.trades.length + ')</h3>' +
    table(["Data", "Tipo", "Ativo", "Qtd.", "Preço", "Valor", "Resultado", "Motivo"],
      a.trades.map((t) => [esc(t.date), t.side === "compra" ? "🟢 compra" : t.side === "venda" ? "🔴 venda" : "🧾 " + esc(t.side), '<b>' + esc(t.symbol) + '</b>',
        t.qty ? fmtNum(t.qty, 4) : "—", t.price ? fmtNum(t.price) : "—", fmtEur(t.eur),
        t.side === "compra" ? "—" : '<span class="' + cls(t.pnl_eur) + '">' + fmtEur(t.pnl_eur) + '</span>', esc(t.reason || "")]), [3, 4, 5, 6]) +
    (realized.length ? '<h3>🧾 Mais-valias realizadas por ano</h3>' + table(["Ano", "Mais-valias"], realized.map(([y, v]) => [esc(y), '<span class="' + cls(v) + '">' + fmtEur(v) + '</span>']), [1]) : "") +
    ((a.evolution || []).length ? '<h3>🧬 Treinos e mutações</h3>' + table(["Data", "O que mudou"], a.evolution.map((e) => [esc(e.date), esc(e.note)])) : "");
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
  if (scroll) $("detail").scrollIntoView({ behavior: "smooth", block: "start" });
}
if (agents.length) show(0, false);

// ---- race ----
(function race() {
  const runners = (DATA.series || []).filter((s) => s.x.length);
  if (!runners.length) { $("race").style.display = "none"; return; }
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
  const agentOf = (name) => byName[name];
  $("track").innerHTML = runners.map((s, i) => {
    const a = agentOf(s.name);
    return '<div class="lane"><div class="no">' + (i + 1) + '</div><div class="run"><div class="racer" id="racer-' + i + '" style="--c:' +
      colours[i % colours.length] + '" title="' + esc(s.name) + '"><span class="avatar">' + esc(a ? a.emoji : "🏇") + '</span><span class="bib">' +
      (i + 1) + '</span><span class="tag"></span></div></div><div class="finish"></div></div>';
  }).join("");
  runners.forEach((s, i) => $("racer-" + i).addEventListener("click", () => { const a = agentOf(s.name); if (a) show(agents.indexOf(a), true); }));
  const scrub = $("race-scrub");
  scrub.max = frames.length - 1;
  const fmtDay = (d) => d.slice(8, 10) + "/" + d.slice(5, 7) + "/" + d.slice(0, 4);

  function draw(f) {
    const now = runners.map((s, i) => ({ s, i, v: values[i][f] })).filter((r) => r.v != null).sort((a, b) => b.v - a.v);
    runners.forEach((s, i) => {
      const v = values[i][f], el = $("racer-" + i), x = v == null ? 0 : (v - lo) / span;
      el.style.left = (3 + 92 * x) + "%";
      el.classList.toggle("left-tag", x > 0.7);
      el.classList.toggle("dead", v == null);
      const place = now.findIndex((r) => r.i === i);
      el.querySelector(".tag").textContent = v == null ? "" : (place + 1) + "º " + fmtPct(v);
      const crown = el.querySelector(".crown");
      if (place === 0 && !crown) el.insertAdjacentHTML("afterbegin", '<span class="crown">👑</span>');
      if (place !== 0 && crown) crown.remove();
    });
    $("race-date").textContent = fmtDay(frames[f]);
    $("race-leader").textContent = now.length ? (agentOf(now[0].s.name)?.emoji || "") + " " + now[0].s.name.toUpperCase() : "—";
    $("board").innerHTML = now.map((r, k) => {
      const a = agentOf(r.s.name);
      return '<li data-name="' + esc(r.s.name) + '"><span class="pos">' + (k + 1) + 'º</span><span>' + esc(a ? a.emoji : "🏇") + '</span><span class="nm">' +
        esc(r.s.name) + '</span>' + (a && a.points != null ? '<span class="muted small">⭐ ' + fmtNum(a.points, 0) + '</span>' : "") +
        '<span class="val ' + cls(r.v) + '">' + fmtPct(r.v) + '</span></li>';
    }).join("");
    scrub.value = f;
  }
  $("board").addEventListener("click", (e) => { const li = e.target.closest("li"); const a = li && agentOf(li.dataset.name); if (a) show(agents.indexOf(a), true); });

  let timer = null, speed = 1, frame = frames.length - 1;
  const stop = () => { clearInterval(timer); timer = null; $("track").classList.remove("running"); $("race-play").textContent = "▶ VER A CORRIDA"; };
  const tick = () => { if (frame >= frames.length - 1) return stop(); frame++; draw(frame); };
  $("race-play").addEventListener("click", () => {
    if (timer) return stop();
    if (frame >= frames.length - 1) frame = 0;
    draw(frame);
    $("track").classList.add("running");
    $("race-play").textContent = "⏸ PAUSA";
    timer = setInterval(tick, Math.max(40, 12000 / frames.length) / speed);
  });
  $("race-speed").addEventListener("click", () => {
    speed = speed >= 4 ? 1 : speed * 2;
    $("race-speed").textContent = speed + "×";
    if (timer) { clearInterval(timer); timer = setInterval(tick, Math.max(40, 12000 / frames.length) / speed); }
  });
  scrub.addEventListener("input", () => { stop(); frame = +scrub.value; draw(frame); });
  draw(frame);
})();
`;
