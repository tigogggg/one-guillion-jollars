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
  if (rest === "/data" && request.method === "GET") return json(await snapshot(env, data));
  if (rest === "/history" && request.method === "GET") return json(await history(data));
  if (rest === "/manifest.webmanifest") return manifest(data, tok);
  if (rest === "/data" && request.method === "POST") {
    let body;
    try { body = await request.json(); } catch { return json({ error: "Pedido inválido." }, 400); }
    if (body.action === "follow_add" || body.action === "follow_remove") {
      const error = await follow(env, data, body);
      if (error) return json({ error }, 400);
      return json(await snapshot(env, data));
    }
    const error = await change(data, body);
    if (error) return json({ error }, 400);
    await env.CONFIG.put(`portfolio:${tok}`, JSON.stringify(data));
    return json(await snapshot(env, data));
  }
  return json({ error: "Não encontrado." }, 404);
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

function manifest(data, tok) {
  return new Response(JSON.stringify({
    name: `Carteira de ${data.name || "membro"}`, short_name: "Carteira", start_url: `/c/${tok}`, scope: `/c/${tok}`,
    display: "standalone", background_color: "#0b0820", theme_color: "#0b0820",
    icons: [{ src: "/icon-192.png", sizes: "192x192", type: "image/png" }, { src: "/icon-512.png", sizes: "512x512", type: "image/png" }],
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
  if (body.action === "settings") {
    const capital = Number(body.capital), risk = Number(body.risk);
    if (!(capital >= 0 && capital <= 1e9) || !(risk > 0 && risk <= 5)) return "Capital ou risco inválido (risco entre 0,1% e 5%).";
    data.settings = { capital, risk };
    return null;
  }
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

async function snapshot(env, data) {
  const symbols = [...new Set(data.tx.map((t) => t.symbol))];
  const [quotes, proposals, news] = await Promise.all([
    Promise.all(symbols.map(async (s) => [s, await yahooQuote(s)])).then(Object.fromEntries),
    env.CONFIG.get("portal:proposals", "json"),
    env.CONFIG.get("portal:news", "json"),
  ]);
  const currencies = [...new Set(data.tx.map((t) => t.currency).concat(Object.values(quotes).filter(Boolean).map((q) => q.currency))
    .concat((proposals?.buys || []).map((b) => b.plan.currency)))];
  const fx = Object.fromEntries(await Promise.all(currencies.map(async (c) => [c, await fxToEur(c)])));
  const verdicts = await judge(data, quotes, fx, proposals, news);
  const followList = data.owner ? ((await env.CONFIG.get(`follows:${data.owner}`, "json")) || []) : [];
  const follows = await Promise.all(followList.map(async (t) => {
    const q = quotes[t] || (await yahooQuote(t));
    return { symbol: t, name: q?.name || "", price: q?.price ?? null, currency: q?.currency || "", change: q?.prev ? q.price / q.prev - 1 : null,
             news: news?.scores?.[t] ?? null, proposed: (proposals?.buys || []).some((b) => b.symbol === t) };
  }));
  return { name: data.name, tx: data.tx, quotes, fx, verdicts, follows, settings: data.settings || { capital: 10000, risk: 1 },
           proposals: proposals ? { as_of: proposals.as_of, zone: proposals.zone, buys: proposals.buys.slice(0, 12) } : null, at: Date.now() };
}

// ---------------------------------------------------------------------------
// Keep, reduce or sell: one verdict per open position, with its reasons
// ---------------------------------------------------------------------------

const VERDICT_RULES = [
  "🔴 Vender: o preço está abaixo da média de 200 dias e a de 50 abaixo da de 200 (tendência de fundo perdida); ou perde 15% ou mais face ao custo médio; ou agentes que pesam 25% ou mais da liga estão a vender esta ação; ou notícias claramente negativas com o preço abaixo da média de 50 dias.",
  "🟠 Reduzir: abaixo da média de 50 dias (mas acima da de 200); ou mais de 25% da carteira numa só posição; ou ganho de 30%+ com o preço 15%+ acima da média de 50 dias (esticada: realizar parte).",
  "🟢 Manter: nenhum dos sinais acima.",
  "Stop sugerido: o mais alto entre 2,5× a variação diária média abaixo do preço e a média de 200 dias.",
];

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
    const r = await yahoo(sym, "range=1y&interval=1d");
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
    const stop = Math.max(atr ? price - 2.5 * atr : 0, sma200 || 0);
    out[sym] = {
      verdict: sell.length ? "Vender" : reduce.length ? "Reduzir" : "Manter",
      reasons: (sell.length ? sell : reduce.length ? reduce : good.length ? good : ["sem sinais de alerta"])
        .concat(sellers && share < 0.25 ? [`nota: ${sellers} vendeu (${(share * 100).toFixed(0)}% do peso, abaixo dos 25%)`] : []),
      sma50, sma200, atr, stop: stop > 0 && stop < price ? stop : null, gain, weight,
    };
  }));
  return out;
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
  <button class="tab" data-tab="propostas">🧠 Propostas para ti</button>
  <button class="tab" data-tab="seguir">👀 Seguir</button>
</nav>
<section data-tab="resumo">
  <div id="totals" class="grid"></div>
  <h2>📈 Evolução</h2>
  <div id="chart" class="chart"></div>
  <p class="muted small" id="chart-note">Valor da carteira em euros vs. dinheiro investido (compras menos vendas), dia a dia desde a primeira compra.</p>
</section>
<section data-tab="posicoes" hidden><div id="positions"></div><div id="verdict-rules"></div><h2>✅ Posições fechadas</h2><div id="closed"></div></section>
<section data-tab="seguir" hidden>
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
<script>
const TOKEN = ${safeJson(tok)};
${CLIENT}
</script>`;
  return page(`Carteira de ${data.name || "membro"}`, body, `<link rel="manifest" href="/c/${tok}/manifest.webmanifest"><meta name="theme-color" content="#0b0820"><link rel="apple-touch-icon" href="/icon-192.png">
<link href="https://fonts.googleapis.com/css2?family=Press+Start+2P&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js"></script>
<style>.form { display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:10px; align-items:end; }
.form label { display:grid; gap:4px; font-size:.85rem; color:var(--muted); }
.form input, .form select { background:#0b0820; color:var(--fg); border:1px solid var(--line); border-radius:6px; padding:8px; font:inherit; }
.del { background:none; border:1px solid var(--down); color:var(--down); border-radius:4px; cursor:pointer; padding:2px 6px; }
.act { background:none; border:1px solid var(--neon); color:var(--neon); border-radius:4px; cursor:pointer; padding:2px 6px; }</style>`);
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
try { localStorage.setItem("carteira", JSON.stringify(location.pathname.replace(/\/$/, ""))); } catch (e) {}  // the portal's "Hoje" shows it
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

function renderFollows() {
  const f = SNAP.follows || [];
  $("follows").innerHTML = f.length ? table(["Ação", "Preço", "Hoje", "Notícias (36 h)", "Proposta hoje", ""], f.map((x) => ['<b>' + esc(x.symbol) + '</b> <span class="muted">' + esc(x.name) + '</span>',
    x.price != null ? nf(x.price) + " " + esc(x.currency) : "—", '<span class="' + cls(x.change) + '">' + pct(x.change) + '</span>',
    x.news == null ? "—" : '<span class="' + cls(x.news) + '">' + (x.news > 0 ? "+" : "") + x.news + '</span>', x.proposed ? "🧠 sim" : "—",
    '<button class="del" data-unfollow="' + esc(x.symbol) + '">deixar</button>']), [1, 2, 3]) : '<p class="muted">Ainda não segues nenhuma ação.</p>';
}
async function followAction(action, symbol) {
  const r = await fetch(location.pathname.replace(/\/$/, "") + "/data", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, symbol }) });
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
  if (!P || !P.buys.length) { $("mine").innerHTML = '<p class="muted">Hoje não há propostas de compra dos agentes. Aparecem depois da liga dos dias úteis (12:15).</p>'; return; }
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
  const r = await fetch(location.pathname.replace(/\/$/, "") + "/data", { method: "POST", headers: { "Content-Type": "application/json" },
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
  $("closed").innerHTML = table(["Ativo", "Comprado", "Vendido", "Resultado", "Resultado %", "Comissões", "De", "Até"],
    closed.map((p) => ['<b>' + esc(p.symbol) + '</b>', nf(p.bought) + " " + esc(p.currency), nf(p.sold) + " " + esc(p.currency),
      '<span class="' + cls(p.realized) + '">' + nf(p.realized) + " " + esc(p.currency) + '</span>', '<span class="' + cls(p.realized) + '">' + pct(p.bought ? p.realized / p.bought : null) + '</span>',
      nf(p.fees) + " " + esc(p.currency), esc(p.first), esc(p.last)]), [1, 2, 3, 4, 5]);
  $("txs").innerHTML = '<h2>🧾 Todas as operações (' + s.tx.length + ')</h2>' + table(["Data", "Tipo", "Ativo", "Qtd.", "Preço", "Comissão", "Total", "Nota", ""],
    s.tx.slice().sort((a, b) => b.date.localeCompare(a.date) || b.seq - a.seq).map((t) => [esc(t.date), t.side === "buy" ? "🟢 compra" : "🔴 venda", '<b>' + esc(t.symbol) + '</b>',
      nf(t.qty, 4), nf(t.price) + " " + esc(t.currency), nf(t.fee), nf(t.qty * t.price + (t.side === "buy" ? t.fee : -t.fee)) + " " + esc(t.currency), esc(t.note || ""),
      '<button class="act" data-act="edit" data-id="' + esc(t.id) + '">✏️ editar</button> <button class="act" data-act="dup" data-id="' + esc(t.id) +
      '">⧉ duplicar</button> <button class="del" data-id="' + esc(t.id) + '">apagar</button>']), [3, 4, 5, 6]);
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
  const id = tx.edit_id; delete tx.edit_id;
  const r = await fetch(location.pathname.replace(/\/$/, "") + "/data", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(id ? { action: "edit", id, tx } : { action: "add", tx }) });
  const out = await r.json();
  if (!r.ok) { msg.textContent = "⚠️ " + out.error; msg.className = "small down"; return; }
  SNAP = out; render(); drawHistory();
  msg.textContent = "✅ " + (id ? "Alterada" : "Guardada") + ": " + (tx.side === "buy" ? "compra" : "venda") + " de " + tx.qty + " " + tx.symbol.toUpperCase() + " a " + tx.date; msg.className = "small up";
  resetForm();
});

$("txs").addEventListener("click", async (e) => {
  const a = e.target.closest(".act");
  if (a) { const t = SNAP.tx.find((x) => x.id === a.dataset.id); if (t) fillForm(t, a.dataset.act === "edit"); return; }
  const b = e.target.closest(".del"); if (!b || !confirm("Apagar esta operação?")) return;
  const r = await fetch(location.pathname.replace(/\/$/, "") + "/data", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "delete", id: b.dataset.id }) });
  const out = await r.json();
  if (!r.ok) return alert(out.error);
  SNAP = out; render(); drawHistory();
});

load().then(drawHistory);
setInterval(load, 60000);
`;
