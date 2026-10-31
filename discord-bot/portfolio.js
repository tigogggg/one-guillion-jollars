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

import { esc, html, page, safeJson } from "./chart_page.js";

const MAX_TX = 1000;
const SYMBOL = /^[A-Z0-9][A-Z0-9.\-^=]{0,14}$/;
const DAY = /^\d{4}-\d{2}-\d{2}$/;
const UA = { "User-Agent": "Mozilla/5.0 (compatible; CoutoTradingBot/1.0)" };

function token() {
  const bytes = crypto.getRandomValues(new Uint8Array(12));
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// /carteira: the member's link (created on first use, replaced with novo_link).
export async function portfolioCommand(env, interaction, origin) {
  const member = interaction.member?.user;
  if (!member) return "⚠️ Usa `/carteira` dentro do servidor: as carteiras são só para os membros.";
  const fresh = (interaction.data.options || []).some((o) => o.name === "novo_link" && o.value);
  const userKey = `portfolio-user:${member.id}`;
  let current = await env.CONFIG.get(userKey);
  let data = current ? await env.CONFIG.get(`portfolio:${current}`, "json") : null;
  if (!data) data = { owner: member.id, name: member.global_name || member.username, created: new Date().toISOString(), tx: [] };
  if (!current || fresh) {
    if (current) await env.CONFIG.delete(`portfolio:${current}`);
    current = token();
    await env.CONFIG.put(userKey, current);
  }
  data.name = member.global_name || member.username || data.name;
  await env.CONFIG.put(`portfolio:${current}`, JSON.stringify(data));
  return [
    `💼 **A tua carteira** (só tu vês esta mensagem)`,
    `${origin}/c/${current}`,
    fresh ? "🔄 Link novo: o anterior deixou de funcionar." : "",
    "-# Quem tiver este link vê e edita a tua carteira: não o partilhes. `/carteira novo_link:True` troca-o.",
  ].filter(Boolean).join("\n");
}

// /c/<token>, /c/<token>/data (GET quotes, POST add/delete) and /c/<token>/history
export async function portfolioRoute(request, env, tok, rest) {
  const data = await env.CONFIG.get(`portfolio:${tok}`, "json");
  if (!data) return html(page("Carteira", "<p class='muted'>Link inválido ou substituído. Pede o teu link com /carteira no Discord.</p>"), 404);
  if (rest === "" && request.method === "GET") return html(portfolioPage(data, tok));
  if (rest === "/data" && request.method === "GET") return json(await snapshot(data));
  if (rest === "/history" && request.method === "GET") return json(await history(data));
  if (rest === "/data" && request.method === "POST") {
    let body;
    try { body = await request.json(); } catch { return json({ error: "Pedido inválido." }, 400); }
    const error = await change(data, body);
    if (error) return json({ error }, 400);
    await env.CONFIG.put(`portfolio:${tok}`, JSON.stringify(data));
    return json(await snapshot(data));
  }
  return json({ error: "Não encontrado." }, 404);
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
  if (body.action !== "add") return "Ação desconhecida.";
  if (data.tx.length >= MAX_TX) return `Limite de ${MAX_TX} operações.`;
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
  if (side === "sell" && heldOn(data.tx, symbol, date) < qty - 1e-9) return `Em ${date} não tinhas ${qty} ${symbol} para vender.`;
  const seq = data.tx.reduce((m, x) => Math.max(m, x.seq || 0), 0) + 1;
  data.tx.push({ id: token().slice(0, 12), seq, side, symbol, date, qty, price, fee, currency: quote.currency, name: quote.name,
                 note: String(t.note || "").slice(0, 120) });
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

async function yahooQuote(symbol) {
  const r = await yahoo(symbol, "range=5d&interval=1d");
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

async function snapshot(data) {
  const symbols = [...new Set(data.tx.map((t) => t.symbol))];
  const quotes = Object.fromEntries(await Promise.all(symbols.map(async (s) => [s, await yahooQuote(s)])));
  const currencies = [...new Set(data.tx.map((t) => t.currency).concat(Object.values(quotes).filter(Boolean).map((q) => q.currency)))];
  const fx = Object.fromEntries(await Promise.all(currencies.map(async (c) => [c, await fxToEur(c)])));
  return { name: data.name, tx: data.tx, quotes, fx, at: Date.now() };
}

async function history(data) {
  if (!data.tx.length) return { series: {}, fx: {} };
  const first = data.tx.reduce((m, t) => (t.date < m ? t.date : m), data.tx[0].date);
  const p1 = Math.floor(Date.parse(first + "T00:00:00Z") / 1000) - 7 * 86400;
  const p2 = Math.floor(Date.now() / 1000) + 86400;
  const params = `period1=${p1}&period2=${p2}&interval=1d`;
  const grab = async (sym) => {
    const r = await yahoo(sym, params);
    const ts = r?.timestamp || [], cl = r?.indicators?.quote?.[0]?.close || [];
    return { t: ts.map((x) => new Date(x * 1000).toISOString().slice(0, 10)), c: cl };
  };
  const symbols = [...new Set(data.tx.map((t) => t.symbol))];
  const currencies = [...new Set(data.tx.map((t) => t.currency))].filter((c) => c && c !== "EUR");
  const [series, fxs] = await Promise.all([
    Promise.all(symbols.map(async (s) => [s, await grab(s)])),
    Promise.all(currencies.map(async (c) => [c, await grab(`EUR${c === "GBp" || c === "GBX" ? "GBP" : c}=X`)])),
  ]);
  return { series: Object.fromEntries(series), fx: Object.fromEntries(fxs) };
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

function portfolioPage(data, tok) {
  const body = `
<header>
  <h1>💼 Carteira de ${esc(data.name || "membro")}</h1>
  <p class="muted small">Privada: só quem tem este link a vê. Preços do Yahoo Finance, atualizados a cada minuto (normalmente com ~15 min de atraso face à bolsa). Valores totais em euros ao câmbio atual. Ferramenta educativa, não é aconselhamento.</p>
</header>
<nav class="tabs" id="tabs">
  <button class="tab on" data-tab="resumo">📊 Resumo</button>
  <button class="tab" data-tab="posicoes">💼 Posições</button>
  <button class="tab" data-tab="operacoes">🧾 Operações</button>
  <button class="tab" data-tab="adicionar">➕ Adicionar</button>
</nav>
<section data-tab="resumo">
  <div id="totals" class="grid"></div>
  <h2>📈 Evolução</h2>
  <div id="chart" class="chart"></div>
  <p class="muted small" id="chart-note">Valor da carteira em euros vs. dinheiro investido (compras menos vendas), dia a dia desde a primeira compra.</p>
</section>
<section data-tab="posicoes" hidden><div id="positions"></div><h2>✅ Posições fechadas</h2><div id="closed"></div></section>
<section data-tab="operacoes" hidden><div id="txs"></div></section>
<section data-tab="adicionar" hidden>
  <h2>➕ Registar uma compra ou venda</h2>
  <form id="form" class="panel form">
    <label>Tipo <select name="side"><option value="buy">🟢 Compra</option><option value="sell">🔴 Venda</option></select></label>
    <label>Ticker <input name="symbol" placeholder="AAPL, NVDA, VWCE.DE" required autocapitalize="characters"></label>
    <label>Data <input name="date" type="date" required></label>
    <label>Quantidade <input name="qty" type="number" step="any" min="0" required></label>
    <label>Preço por unidade <input name="price" type="number" step="any" min="0" required><span class="muted small">na moeda da bolsa (USD nos EUA, EUR na Xetra/Euronext)</span></label>
    <label>Comissão <input name="fee" type="number" step="any" min="0" value="0"><span class="muted small">na mesma moeda</span></label>
    <label>Nota <input name="note" maxlength="120" placeholder="opcional"></label>
    <button class="btn" type="submit">GUARDAR</button>
    <p id="form-msg" class="small"></p>
  </form>
  <p class="muted small">Ações europeias: junta o sufixo da bolsa do Yahoo Finance (.DE Xetra, .AS Amesterdão, .PA Paris, .MI Milão, .L Londres, .LS Lisboa).</p>
</section>
<script>
const TOKEN = ${safeJson(tok)};
${CLIENT}
</script>`;
  return page(`Carteira de ${data.name || "membro"}`, body, `<link href="https://fonts.googleapis.com/css2?family=Press+Start+2P&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js"></script>
<style>.form { display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:10px; align-items:end; }
.form label { display:grid; gap:4px; font-size:.85rem; color:var(--muted); }
.form input, .form select { background:#0b0820; color:var(--fg); border:1px solid var(--line); border-radius:6px; padding:8px; font:inherit; }
.del { background:none; border:1px solid var(--down); color:var(--down); border-radius:4px; cursor:pointer; padding:2px 6px; }</style>`);
}

const CLIENT = String.raw`
const $ = (id) => document.getElementById(id);
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
}
$("tabs").addEventListener("click", (e) => { const b = e.target.closest(".tab"); if (b) openTab(b.dataset.tab); });

let SNAP = null;

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
  $("totals").innerHTML =
    tile("Valor atual", eur(value), open.length + " posições abertas") +
    tile("Ganho não realizado", eur(total), pct(cost ? total / cost : null), cls(total)) +
    tile("Hoje", eur(day), pct(value - day ? day / (value - day) : null), cls(day)) +
    tile("Ganho realizado (vendas)", eur(realized), closed.length + " posições fechadas", cls(realized)) +
    tile("Ganho total", eur(total + realized), "não realizado + realizado", cls(total + realized)) +
    tile("Investido nas abertas", eur(cost), "custo médio com comissões") +
    tile("Atualizado", new Date(s.at).toLocaleTimeString("pt-PT", { hour: "2-digit", minute: "2-digit", second: "2-digit" }), stale ? "⚠️ " + stale + " sem preço" : "próxima em 60 s");
  const weightOf = (r) => r.val != null && r.fx != null && value ? r.val * r.fx / value : null;
  $("positions").innerHTML = '<h2>💼 Posições abertas</h2>' + table(
    ["Ativo", "Nome", "Qtd.", "Custo médio", "Preço", "Hoje", "Valor", "Ganho", "Ganho %", "Peso", "Valor €", "Desde"],
    rows.sort((a, b) => (weightOf(b) || 0) - (weightOf(a) || 0)).map((r) => ['<b>' + esc(r.p.symbol) + '</b>', esc(r.q?.name || r.p.name || ""), nf(r.p.qty, 4),
      nf(r.p.cost / r.p.qty) + " " + esc(r.p.currency), r.price != null ? nf(r.price) + " " + esc(r.p.currency) : "—",
      '<span class="' + cls(r.dgain) + '">' + (r.q?.prev ? pct(r.price / r.q.prev - 1) : "—") + '</span>',
      r.val != null ? nf(r.val) + " " + esc(r.p.currency) : "—", '<span class="' + cls(r.gain) + '">' + nf(r.gain) + " " + esc(r.p.currency) + '</span>',
      '<span class="' + cls(r.gain) + '">' + pct(r.gain != null ? r.gain / r.p.cost : null) + '</span>', weightOf(r) != null ? nf(weightOf(r) * 100, 1) + "%" : "—",
      r.val != null && r.fx != null ? eur(r.val * r.fx) : "—", esc(r.p.first)]), [2, 3, 4, 5, 6, 7, 8, 9, 10]);
  $("closed").innerHTML = table(["Ativo", "Comprado", "Vendido", "Resultado", "Resultado %", "Comissões", "De", "Até"],
    closed.map((p) => ['<b>' + esc(p.symbol) + '</b>', nf(p.bought) + " " + esc(p.currency), nf(p.sold) + " " + esc(p.currency),
      '<span class="' + cls(p.realized) + '">' + nf(p.realized) + " " + esc(p.currency) + '</span>', '<span class="' + cls(p.realized) + '">' + pct(p.bought ? p.realized / p.bought : null) + '</span>',
      nf(p.fees) + " " + esc(p.currency), esc(p.first), esc(p.last)]), [1, 2, 3, 4, 5]);
  $("txs").innerHTML = '<h2>🧾 Todas as operações (' + s.tx.length + ')</h2>' + table(["Data", "Tipo", "Ativo", "Qtd.", "Preço", "Comissão", "Total", "Nota", ""],
    s.tx.slice().sort((a, b) => b.date.localeCompare(a.date) || b.seq - a.seq).map((t) => [esc(t.date), t.side === "buy" ? "🟢 compra" : "🔴 venda", '<b>' + esc(t.symbol) + '</b>',
      nf(t.qty, 4), nf(t.price) + " " + esc(t.currency), nf(t.fee), nf(t.qty * t.price + (t.side === "buy" ? t.fee : -t.fee)) + " " + esc(t.currency), esc(t.note || ""),
      '<button class="del" data-id="' + esc(t.id) + '">apagar</button>']), [3, 4, 5, 6]);
}

async function load() {
  try {
    const r = await fetch(location.pathname.replace(/\/$/, "") + "/data", { cache: "no-store" });
    if (r.ok) { SNAP = await r.json(); render(); }
  } catch (e) {}
}

async function drawHistory() {
  if (!window.Plotly || !SNAP || !SNAP.tx.length) { $("chart").innerHTML = '<p class="muted" style="padding:16px">Adiciona a primeira compra para ver a evolução.</p>'; return; }
  const h = await (await fetch(location.pathname.replace(/\/$/, "") + "/history", { cache: "no-store" })).json();
  const days = [...new Set(Object.values(h.series).flatMap((s) => s.t))].filter((d) => d >= SNAP.tx.reduce((m, t) => t.date < m ? t.date : m, "9999")).sort();
  const lastBefore = (s, d) => { if (!s) return null; let v = null; for (let i = 0; i < s.t.length && s.t[i] <= d; i++) if (s.c[i] != null) v = s.c[i]; return v; };
  const fxOn = (cur, d) => cur === "EUR" ? 1 : (() => { const r = lastBefore(h.fx[cur], d); return r ? (cur === "GBp" || cur === "GBX" ? 0.01 : 1) / r : null; })();
  const tx = SNAP.tx.slice().sort((a, b) => a.date.localeCompare(b.date) || a.seq - b.seq);
  const value = [], invested = [];
  for (const d of days) {
    const held = {}; let inv = 0;
    for (const t of tx) { if (t.date > d) break; held[t.symbol] = (held[t.symbol] || 0) + (t.side === "buy" ? t.qty : -t.qty);
      const fx = fxOn(t.currency, t.date) ?? 1; inv += (t.side === "buy" ? t.qty * t.price + t.fee : -(t.qty * t.price - t.fee)) * fx; }
    let v = 0;
    for (const [sym, q] of Object.entries(held)) { if (q <= 1e-9) continue; const t = tx.find((x) => x.symbol === sym);
      const px = lastBefore(h.series[sym], d), fx = fxOn(t.currency, d); if (px != null && fx != null) v += q * px * fx; }
    value.push(v); invested.push(inv);
  }
  Plotly.newPlot("chart", [
    { x: days, y: value, name: "Valor da carteira", type: "scatter", mode: "lines", line: { color: "#00e5ff", width: 2 }, hovertemplate: "%{y:,.2f} €<extra>Valor</extra>" },
    { x: days, y: invested, name: "Dinheiro investido", type: "scatter", mode: "lines", line: { color: "#ffe14d", width: 1.5, dash: "dash" }, hovertemplate: "%{y:,.2f} €<extra>Investido</extra>" },
  ], { paper_bgcolor: "#17123a", plot_bgcolor: "#17123a", font: { color: "#e8e6ff" }, margin: { l: 60, r: 12, t: 12, b: 40 }, hovermode: "x unified",
       xaxis: { gridcolor: "#2e2763" }, yaxis: { gridcolor: "#2e2763", ticksuffix: " €" }, legend: { orientation: "h", y: -0.15 } },
     { responsive: true, displaylogo: false });
}

$("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target), msg = $("form-msg");
  msg.textContent = "A guardar…"; msg.className = "small";
  const tx = Object.fromEntries(f.entries());
  const r = await fetch(location.pathname.replace(/\/$/, "") + "/data", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "add", tx }) });
  const out = await r.json();
  if (!r.ok) { msg.textContent = "⚠️ " + out.error; msg.className = "small down"; return; }
  SNAP = out; render(); drawHistory();
  msg.textContent = "✅ Guardado: " + (tx.side === "buy" ? "compra" : "venda") + " de " + tx.qty + " " + tx.symbol.toUpperCase(); msg.className = "small up";
  e.target.reset(); document.querySelector('input[name=date]').value = new Date().toISOString().slice(0, 10);
});

$("txs").addEventListener("click", async (e) => {
  const b = e.target.closest(".del"); if (!b || !confirm("Apagar esta operação?")) return;
  const r = await fetch(location.pathname.replace(/\/$/, "") + "/data", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "delete", id: b.dataset.id }) });
  const out = await r.json();
  if (!r.ok) return alert(out.error);
  SNAP = out; render(); drawHistory();
});

load().then(drawHistory);
setInterval(load, 60000);
`;
