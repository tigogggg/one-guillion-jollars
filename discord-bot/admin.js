// 🔒 Admin: a portal tab only for the members whose Discord id is in the ADMIN_IDS secret (comma-separated).
//
// It shows the tick-data collector (rjpm1995/arcade-agents-tick): Dukascopy quotes, Euronext & Xetra trades and the
// stock catalog. The collector runs on a PC and in GitHub Actions, out of the Worker's reach, so it sends its data here
// (`uv run python -m tick.portal`): POST /admin/tick with "Authorization: Bearer <TICK_PUSH_KEY>" (a Worker secret, the
// same key on the PC and in that repo's secrets) and a body {onde, partes: {estado?, euronext?, catalogo?}, lista?}.
// Each part is kept as {recebido, onde, dados} in D1 (table tick), or KV tick:<part> without D1. The answer carries the
// download list (catalog keys), which the admins change here: the newer list wins, so the PC picks it up on its next push.
//
//   GET  /admin/tick/dados   every part and the list (admins only; anyone else gets 404)
//   POST /admin/tick/lista   {key, adicionar}: add or remove a catalog stock (admins only, same origin)

import { currentSession } from "./session.js";

const PARTS = ["estado", "euronext", "catalogo"];
const MAX_BODY = 3_000_000;
const MAX_LIST = 500;

export function adminIds(env) {
  return String(env.ADMIN_IDS || "").split(/[\s,;]+/).filter((x) => /^\d{5,25}$/.test(x));
}

export function isAdmin(env, session) {
  return !!session?.owner && adminIds(env).includes(String(session.owner));
}

const json = (data, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" } });
const notFound = () => new Response("Não encontrado.", { status: 404, headers: { "Content-Type": "text/plain; charset=utf-8" } });

let tableReady = null;
function table(env) {
  if (!tableReady) {
    tableReady = env.DB.prepare("CREATE TABLE IF NOT EXISTS tick (part TEXT PRIMARY KEY, value TEXT NOT NULL, at TEXT NOT NULL)").run()
      .catch((e) => { tableReady = null; throw e; });
  }
  return tableReady;
}

export async function getPart(env, part) {
  if (env.DB) {
    await table(env);
    const row = await env.DB.prepare("SELECT value FROM tick WHERE part = ?").bind(part).first();
    return row ? JSON.parse(row.value) : null;
  }
  return env.CONFIG.get(`tick:${part}`, "json");
}

export async function putPart(env, part, value) {
  const text = JSON.stringify(value);
  if (env.DB) {
    await table(env);
    await env.DB.prepare("INSERT INTO tick (part, value, at) VALUES (?, ?, ?) ON CONFLICT(part) DO UPDATE SET value = excluded.value, at = excluded.at")
      .bind(part, text, new Date().toISOString()).run();
  } else {
    await env.CONFIG.put(`tick:${part}`, text);
  }
}

async function sameKey(given, expected) {
  const enc = new TextEncoder();
  const [a, b] = await Promise.all([given, expected].map((s) => crypto.subtle.digest("SHA-256", enc.encode(s))));
  if (crypto.subtle.timingSafeEqual) return crypto.subtle.timingSafeEqual(a, b);
  const x = new Uint8Array(a), y = new Uint8Array(b);
  let d = 0;
  for (let i = 0; i < x.length; i++) d |= x[i] ^ y[i];
  return d === 0;
}

export function cleanList(keys) {
  return [...new Set((Array.isArray(keys) ? keys : []).map(String).filter((k) => k.length <= 80))].slice(0, MAX_LIST);
}

// The collector's push: its parts, and the newer of the two download lists back.
async function push(request, env) {
  if (!env.TICK_PUSH_KEY) return json({ erro: "TICK_PUSH_KEY por definir no Worker" }, 503);
  const auth = request.headers.get("Authorization") || "";
  if (!auth.startsWith("Bearer ") || !(await sameKey(auth.slice(7), env.TICK_PUSH_KEY))) return json({ erro: "chave inválida" }, 401);
  const text = await request.text();
  if (text.length > MAX_BODY) return json({ erro: "demasiado grande" }, 413);
  let body;
  try { body = JSON.parse(text); } catch { return json({ erro: "JSON inválido" }, 400); }
  const now = new Date().toISOString(), onde = String(body.onde || "?").slice(0, 80), saved = [];
  for (const part of PARTS) {
    const dados = body.partes?.[part];
    if (!dados || typeof dados !== "object") continue;
    if (part === "euronext" && !dados.tarefa?.existe) {  // GitHub Actions has no Windows task: keep the PC's last reading
      const old = await getPart(env, part);
      if (old?.dados?.tarefa?.existe) dados.tarefa = { ...old.dados.tarefa, lida: old.recebido };
    }
    await putPart(env, part, { recebido: now, onde, dados });
    saved.push(part);
  }
  let lista = await getPart(env, "lista");
  const sent = body.lista;
  if (sent && Array.isArray(sent.keys) && (!lista || String(sent.alterada || "") > String(lista.alterada || ""))) {
    lista = { keys: cleanList(sent.keys), alterada: String(sent.alterada || now), por: onde };
    await putPart(env, "lista", lista);
  }
  return json({ ok: true, guardado: saved, lista });
}

// An admin adds or removes a stock from the download list (the catalog's rules: euros only, base stocks always in).
async function changeList(request, env) {
  const origin = request.headers.get("Origin");
  if (origin && origin !== new URL(request.url).origin) return json({ erro: "origem não permitida" }, 403);
  let body;
  try { body = await request.json(); } catch { return json({ erro: "JSON inválido" }, 400); }
  const key = String(body?.key || ""), add = !!body?.adicionar;
  const cat = (await getPart(env, "catalogo"))?.dados;
  const row = (cat?.acoes || []).find((r) => r.key === key);
  if (!row) return json({ erro: "ação desconhecida" }, 400);
  if (row.base) return json({ erro: "as ações base são sempre descarregadas" }, 400);
  if (add && cat.moeda && row.currency !== cat.moeda) return json({ erro: `só ações em ${cat.moeda}` }, 400);
  const old = await getPart(env, "lista");
  let keys = cleanList(old?.keys || (cat.acoes || []).filter((r) => r.na_lista && !r.base).map((r) => r.key));
  keys = add ? cleanList(keys.concat([key])) : keys.filter((k) => k !== key);
  const lista = { keys, alterada: new Date().toISOString(), por: "portal" };
  await putPart(env, "lista", lista);
  return json({ ok: true, lista });
}

// null when the path is not one of these (the worker goes on routing).
export async function adminRoute(request, env) {
  const path = new URL(request.url).pathname;
  if (path === "/admin/tick" && request.method === "POST") return push(request, env);
  if (path !== "/admin/tick/dados" && path !== "/admin/tick/lista") return null;
  if (!isAdmin(env, await currentSession(env, request))) return notFound();
  if (path === "/admin/tick/dados" && request.method === "GET") {
    const [estado, euronext, catalogo, lista] = await Promise.all(PARTS.concat(["lista"]).map((p) => getPart(env, p)));
    return json({ estado, euronext, catalogo, lista, agora: new Date().toISOString() });
  }
  if (path === "/admin/tick/lista" && request.method === "POST") return changeList(request, env);
  return notFound();
}

// The tab's script (served from /static, loaded only for admins). No backticks or dollar-braces inside: it is a String.raw.
export const ADMIN_CLIENT = String.raw`
(function () {
  "use strict";
  const sec = document.querySelector('section[data-tab="admin"]'), root = document.getElementById("admin");
  if (!sec || !root) return;
  const $a = (id) => document.getElementById(id);
  const e = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const fmtInt = (n) => (n == null ? "–" : Math.round(n).toLocaleString("pt-PT"));
  const fmtBytes = (b) => {
    if (b == null) return "–";
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return (b >= 100 || i === 0 ? b.toFixed(0) : b.toFixed(1)).replace(".", ",") + " " + u[i];
  };
  const fmtDur = (s) => {
    if (s == null) return "–";
    s = Math.round(s);
    const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    return d ? d + "d " + h + "h" : h ? h + "h " + m + "m" : m ? m + "m" : s + "s";
  };
  const ago = (iso) => {
    if (!iso) return "nunca";
    const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
    return s < 90 ? "há " + Math.round(s) + " s" : s < 5400 ? "há " + Math.round(s / 60) + " min" : s < 172800 ? "há " + Math.round(s / 3600) + " h" : "há " + Math.round(s / 86400) + " dias";
  };
  const when = (iso) => (iso ? new Date(iso).toLocaleString("pt-PT", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "–");
  const SUB = [["dl", "Dukascopy · Quotes"], ["enx", "Euronext & Xetra · Trades"], ["cat", "Catálogo de ações"]];
  let sub = "dl";
  try { sub = localStorage.getItem("admin-sub") || "dl"; } catch (x) {}
  if (!SUB.some((s) => s[0] === sub)) sub = "dl";
  let D = null, busy = false, timer = 0;

  root.innerHTML = '<div class="adm"><div class="adm-top"><div class="chips" id="adm-sub">' +
    SUB.map((s) => '<button class="chip' + (s[0] === sub ? " on" : "") + '" data-s="' + s[0] + '">' + e(s[1]) + "</button>").join("") +
    '</div><span class="pill" id="adm-pill">…</span><span class="muted small" id="adm-fresh"></span></div>' +
    '<div id="adm-dl"><p class="muted">A carregar…</p></div><div id="adm-enx" hidden></div>' +
    '<div id="adm-cat" hidden><div class="adm-card"><h3>Ações em euros na Dukascopy (sem comissão de câmbio)</h3>' +
    '<div class="adm-tools"><input type="search" id="adm-q" placeholder="Procurar empresa ou ticker…" aria-label="Procurar empresa ou ticker">' +
    '<select id="adm-country" aria-label="País"><option value="">Todos os países</option></select>' +
    '<select id="adm-sort" aria-label="Ordenar"><option value="pop">Destaques primeiro</option><option value="name">Empresa A–Z</option><option value="hist">Histórico mais longo</option></select>' +
    '<label class="adm-chk"><input type="checkbox" id="adm-onlyhl"> Só destaques</label><label class="adm-chk"><input type="checkbox" id="adm-onlylist"> Na minha lista</label></div>' +
    '<div class="muted small" id="adm-catinfo"></div><div class="adm-scroll"><table class="adm-t"><thead><tr><th></th><th>Empresa</th><th>Ticker</th><th>País</th><th>Ticks desde</th><th class="num">Meses</th><th class="num">Tamanho est.</th><th></th></tr></thead><tbody id="adm-catrows"></tbody></table></div>' +
    '<div class="adm-note">As ações que juntares são descarregadas depois das que já estão na lista. O PC vai buscar a lista no próximo envio (<code>uv run python -m tick.portal</code>) e a JForex só a lê quando arranca: para a descarga (Ctrl+C) e volta a correr <code>uv run python -m tick.jforex</code>.</div></div></div></div>';

  const fresh = (p, label) => (p ? label + " " + ago(p.recebido) + (p.onde ? " (" + e(p.onde) + ")" : "") : label + " nunca recebido");
  function showSub() {
    for (const s of SUB) $a("adm-" + s[0]).hidden = s[0] !== sub;
    $a("adm-sub").querySelectorAll(".chip").forEach((c) => c.classList.toggle("on", c.dataset.s === sub));
    if (!D) return;
    const p = sub === "enx" ? D.euronext : sub === "cat" ? D.catalogo : D.estado;
    $a("adm-fresh").innerHTML = fresh(p, "recebido");
    pill();
  }
  $a("adm-sub").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-s]"); if (!b) return;
    sub = b.dataset.s;
    try { localStorage.setItem("admin-sub", sub); } catch (x) {}
    showSub();
    if (sub === "cat") drawCatalog();
  });

  function pill() {
    let t = "…", c = "";
    if (sub === "enx") {
      const tk = (D.euronext && D.euronext.dados.tarefa) || {}, last = D.euronext && D.euronext.dados.diario;
      const okRun = last && last.codigo === 0;
      t = D.euronext && D.euronext.dados.a_correr ? "Trades: a recolher" : last ? "Trades: última corrida " + (okRun ? "ok" : "falhou") : tk.existe ? "Trades: agendado" : "Trades";
      c = D.euronext && D.euronext.dados.a_correr ? "run" : last && !okRun ? "err" : "";
    } else if (D.estado) {
      const p = D.estado.dados.pipeline || {}, stale = Date.now() - Date.parse(D.estado.recebido) > 5 * 60000;
      t = "Dukascopy: " + (p.a_correr ? (stale ? "a descarregar? (sem notícias do PC)" : "a descarregar") : p.estado === "erro" ? "erro" : p.estado === "concluído" ? "concluído" : "parado");
      c = p.a_correr && !stale ? "run" : p.estado === "erro" || (p.a_correr && stale) ? "err" : "";
    } else t = "Dukascopy: sem dados";
    $a("adm-pill").textContent = t;
    $a("adm-pill").className = "pill" + (c ? " " + c : "");
  }

  // Google Drive: what is used by whom and what is still to come (on both data tabs)
  function driveCard(kind) {
    const s = D.estado && D.estado.dados, x = D.euronext && D.euronext.dados;
    const d = (s && s.drive && s.drive.total ? s.drive : null) || (x && x.drive) || null;
    let body;
    if (!d || !d.total) body = '<p class="muted small">' + (d && d.erro ? "Não consegui ler a Drive: " + e(d.erro) : "Sem leitura da Drive ainda.") + "</p>";
    else {
      const t = (s && s.totais) || {}, q = { bytes: t.bytes || 0, total: t.bytes_previstos || 0, perMonth: t.bytes_por_mes };
      const tt = (x && x.totais) || {}, nStocks = (x && x.tarefa && x.tarefa.acoes && x.tarefa.acoes.length) || ((x && x.acoes) || []).length;
      const tr = { bytes: tt.bytes || 0, perYear: tt.dias ? tt.bytes / tt.dias * nStocks * 252 : 0 };
      const others = Math.max(0, d.usado - q.bytes - tr.bytes), qCome = Math.max(0, q.total - q.bytes);
      const w = (v) => (100 * Math.max(0, v) / d.total).toFixed(2) + "%";
      const left = d.livre - qCome, years = tr.perYear > 0 ? left / tr.perYear : null;
      const fits = left >= 0 ? "Cabe." : '<span class="down">As quotes não cabem no espaço livre.</span>';
      body = '<div class="adm-drive"><div><div class="adm-big">' + fmtBytes(d.livre) + '</div><div class="muted small">livres de ' + fmtBytes(d.total) + "</div></div>" +
        '<div class="adm-gauge"><div class="adm-bar"><i class="m" style="width:' + w(others) + '"></i><i class="b" style="width:' + w(q.bytes) + '"></i><i class="g" style="width:' + w(qCome) + '"></i>' +
        '<i style="width:' + w(tr.bytes) + '"></i><i class="t" style="width:' + w(tr.perYear) + '"></i></div>' +
        '<div class="adm-legend"><span style="--c:var(--muted)">outros ficheiros</span><span style="--c:#ff2e88">quotes Dukascopy</span><span style="--c:rgba(255,46,136,.35)">quotes por vir</span>' +
        '<span style="--c:var(--neon)">trades Euronext &amp; Xetra</span><span style="--c:rgba(0,229,255,.35)">trades, próximos 12 meses</span></div>' +
        '<div class="adm-note">' + (kind === "dl"
          ? "Quotes: " + fmtBytes(q.bytes) + " guardados; a ~" + fmtBytes(q.perMonth) + " por ação-mês a lista toda precisa de cerca de <b>" + fmtBytes(q.total) + "</b>. " + fits
          : "Trades: " + fmtBytes(tr.bytes) + " guardados, cerca de <b>" + fmtBytes(tr.perYear) + " por ano</b> ao ritmo atual. " +
            (years != null ? "Depois das quotes acabarem, o espaço livre dá para cerca de <b>" + (years >= 1 ? years.toFixed(1).replace(".", ",") + " anos" : Math.round(years * 12) + " meses") + "</b> de trades." : "")) +
        " <span class=\"muted\">Lida " + ago(d.verificado) + ".</span></div></div></div>";
    }
    return '<div class="adm-card span12"><h3>Google Drive</h3>' + body + "</div>";
  }

  function drawQuotes() {
    if (!D.estado) { $a("adm-dl").innerHTML = '<div class="adm-card"><p class="muted">Ainda não chegou nada do PC. Corre lá <code>uv run python -m tick.portal --cada 60</code>.</p></div>'; return; }
    const s = D.estado.dados, p = s.pipeline || {}, t = s.totais || {}, a = p.atual;
    const pct = t.meses_total ? 100 * t.meses_feitos / t.meses_total : 0;
    const done = (r) => (r.meses_total ? r.meses_feitos / r.meses_total : 0);
    const rows = (s.acoes || []).slice().sort((x, y) => done(y) - done(x)).map((r) => {
      const sp = r.meses_total ? 100 * r.meses_feitos / r.meses_total : 0, live = a && a.acao === r.key;
      const extra = r.dias_incompletos ? '<div class="small" style="color:var(--gold)" title="dias com buracos ou começo tardio / fim cedo, marcados no estado.json">' + r.dias_incompletos + " dia" + (r.dias_incompletos > 1 ? "s" : "") + " incompleto" + (r.dias_incompletos > 1 ? "s" : "") + "</div>"
        : r.meses_feitos && r.meses_verificados < r.meses_feitos ? '<div class="muted small">' + (r.meses_feitos - r.meses_verificados) + " meses por verificar</div>" : "";
      return "<tr><td><b>" + e(r.key) + "</b>" + (live ? ' <span class="adm-tag on">agora</span>' : "") + '<div class="muted small">' + e(r.nome) + "</div></td>" +
        '<td><div class="adm-bar thin"><i class="b" style="width:' + sp + '%"></i></div></td><td class="num">' + r.meses_feitos + "/" + r.meses_total + extra + "</td>" +
        '<td class="num">' + fmtInt(r.ticks) + '</td><td class="num">' + fmtBytes(r.bytes) + '</td><td class="num">' + (r.ultimo_mes ? (r.primeiro_mes === r.ultimo_mes ? r.ultimo_mes : r.primeiro_mes + " – " + r.ultimo_mes) : "–") + "</td></tr>";
    }).join("");
    const feed = (s.recentes || []).map((r) => '<div class="' + (r.falhou ? "fail" : "") + '"><span class="muted">' + e(r.hora) + "</span><b>" + e(r.acao) + "</b><span>" + e(r.dia) + "</span>" +
      '<span style="margin-left:auto">' + (r.falhou ? "o servidor falhou, volta a tentar" : fmtInt(r.ticks) + " ticks") + '</span><span class="muted" style="width:60px;text-align:right">' + (r.segundos != null ? r.segundos.toFixed(1).replace(".", ",") + " s" : "") + "</span></div>").join("");
    $a("adm-dl").innerHTML = '<div class="adm-grid">' + driveCard("dl") +
      '<div class="adm-card span8"><h3>Ações</h3><div class="adm-scroll"><table class="adm-t"><thead><tr><th>Ação</th><th style="width:16%">Meses</th><th class="num">Feitos</th><th class="num">Ticks</th><th class="num">Tamanho</th><th class="num">Meses guardados</th></tr></thead><tbody>' + rows + "</tbody></table></div></div>" +
      '<div class="adm-card span4"><h3>Progresso geral</h3><div class="adm-row"><div class="adm-big">' + pct.toFixed(1).replace(".", ",") + '%</div><div class="muted">' + fmtInt(t.meses_feitos) + " de " + fmtInt(t.meses_total) + " ações-mês</div></div>" +
      '<div class="adm-bar" style="margin-top:10px"><i class="b" style="width:' + pct + '%"></i></div><div class="adm-stats">' +
      '<div><b>' + fmtInt(t.ticks) + '</b><span>ticks guardados</span></div><div><b>' + fmtBytes(t.bytes) + '</b><span>na Drive</span></div>' +
      '<div><b>' + (p.segundos_por_mes ? fmtDur(p.segundos_por_mes) : "–") + '</b><span>por mês</span></div><div><b>' + (p.a_correr ? fmtDur(p.eta_segundos) : "–") + '</b><span>falta</span></div></div>' +
      (a ? '<p class="muted small">Agora: ' + e(a.acao) + " " + e(a.mes) + ", " + a.dias_feitos + " de " + a.dias_total + " dias de bolsa" + (a.passagem > 1 ? " (tentativa " + a.passagem + ")" : "") + "</p>" : "") +
      (p.estado === "erro" && p.erro ? '<div class="adm-note down">' + e(p.erro) + "</div>" : "") + "</div>" +
      '<div class="adm-card span12"><h3>Últimos dias descarregados</h3><div class="adm-feed">' + (feed || '<span class="muted">Ainda nada.</span>') + "</div></div></div>";
  }

  const TASK_CODES = { 0: "OK", 1: "erro (código 1, ver o registo)", 267009: "a correr", 267011: "nunca correu", 267014: "parada à mão" };
  function drawTrades() {
    if (!D.euronext) { $a("adm-enx").innerHTML = '<div class="adm-card"><p class="muted">Ainda não chegou nada da recolha diária.</p></div>'; return; }
    const s = D.euronext.dados, t = s.totais || {}, rows = s.acoes || [], task = s.tarefa || {}, last = s.diario, now = s.a_correr;
    const onDrive = rows.reduce((n, r) => n + r.na_drive, 0), latest = rows.map((r) => r.ultimo).filter(Boolean).sort().pop();
    let taskHtml;
    if (task.erro) taskHtml = '<span class="down" style="grid-column:span 2">Não consegui ler a tarefa: ' + e(task.erro) + "</span>";
    else if (task.existe === false) taskHtml = '<span class="muted" style="grid-column:span 2">Sem tarefa do Windows no PC.</span>';
    else if (task.existe) {
      const ok = task.resultado === 0;
      taskHtml = "<span>Tarefa do PC</span><span>" + (task.estado === "Running" ? '<span class="up">a correr</span>' : task.estado === "Disabled" ? '<span class="down">desligada</span>' : "agendada, dias úteis") + "</span>" +
        "<span>Ações</span><span>" + e((task.acoes || []).join(", ") || "–") + "</span>" +
        "<span>Última no PC</span><span>" + when(task.ultima) + ' · <span class="' + (ok ? "up" : task.resultado === 267011 ? "muted" : "down") + '">' + e(TASK_CODES[task.resultado] || "código " + task.resultado) + "</span></span>" +
        "<span>Próxima no PC</span><span>" + when(task.proxima) + "</span>" + (task.lida ? '<span></span><span class="muted small">lida ' + ago(task.lida) + "</span>" : "");
    } else taskHtml = "";
    const runHtml = (now ? "<span>A correr</span><span class=\"up\">em " + e(now.onde) + " desde " + when(now.inicio) + "</span>" : "") +
      (last ? "<span>Última corrida</span><span>" + when(last.inicio) + " · " + e(last.onde) + ' · <span class="' + (last.codigo === 0 ? "up" : "down") + '">' + (last.codigo === 0 ? "ok" : "código " + e(last.codigo)) + "</span></span>" : "");
    const strip = (r) => r.faixa.map((d, i) => {
      const mon = i > 0 && new Date(d.dia + "T12:00:00Z").getUTCDay() === 1;
      const tip = d.dia + ": " + ({ drive: fmtInt(d.negocios) + " trades, na Drive", local: fmtInt(d.negocios) + " trades, só no PC", falta: "em falta", antes: "antes da primeira descarga", hoje: "hoje, guardado depois do fecho" })[d.tipo] +
        (d.incompleto ? " · INCOMPLETO: " + d.incompleto : "") + (d.cotacoes ? " · quotes ✓" : "") + (d.junto ? " · juntos ✓" : "");
      return '<i class="' + d.tipo + (mon ? " mon" : "") + (d.junto ? " j" : "") + (d.incompleto ? " inc" : "") + '" title="' + e(tip) + '"></i>';
    }).join("");
    const table = rows.map((r) => "<tr><td><b>" + e(r.key) + "</b>" + (r.agendada || !task.existe ? "" : ' <span class="adm-tag">não agendada</span>') +
      '<div class="muted small">' + e(r.nome) + " · <code>" + e(r.isin) + "</code> · " + (r.fonte === "xetra" ? "Xetra" : "Euronext " + e(r.local)) + "</div></td>" +
      '<td><div class="adm-strip">' + strip(r) + '</div></td><td class="num">' + r.dias + "</td>" +
      '<td class="num">' + (r.tem_cotacoes ? r.dias_cotacoes : '<span class="muted" title="a Dukascopy não tem esta ação">–</span>') + "</td>" +
      '<td class="num">' + (r.tem_cotacoes ? r.dias_juntos : '<span class="muted">–</span>') + (r.compras != null ? '<div class="muted small">' + Math.round(100 * r.compras) + "% compras</div>" : "") + "</td>" +
      '<td class="num">' + fmtInt(r.negocios) + '</td><td class="num">' + fmtBytes(r.bytes) + '</td><td class="num">' + (r.ultimo || "–") + '<div class="muted small">' + (r.ultimo_negocios != null ? fmtInt(r.ultimo_negocios) + " trades" : "") + "</div></td></tr>").join("");
    $a("adm-enx").innerHTML = '<div class="adm-grid">' + driveCard("enx") +
      '<div class="adm-card span8"><h3>Trades guardados</h3><div class="adm-row"><div class="adm-big">' + fmtInt(t.dias) + '</div><div class="muted">ações-dia · ' + rows.length + " aç" + (rows.length === 1 ? "ão" : "ões") + "</div></div>" +
      '<div class="adm-stats"><div><b>' + fmtInt(t.negocios) + '</b><span>trades</span></div><div><b>' + fmtBytes(t.bytes) + '</b><span>Parquet</span></div><div><b>' + onDrive + "/" + fmtInt(t.dias) + '</b><span>na Drive</span></div><div><b>' + (latest || "–") + '</b><span>último dia</span></div></div></div>' +
      '<div class="adm-card span4"><h3>Recolha diária</h3><div class="adm-kv">' + (runHtml + taskHtml || '<span class="muted" style="grid-column:span 2">Sem informação.</span>') + "</div>" +
      '<div class="adm-note">Corre no GitHub Actions às 19:00 UTC nos dias úteis (e no PC, se lá estiver agendada). A Euronext e a Xetra só guardam cada dia cerca de 24 horas: um dia útil sem recolha perde-se.</div></div>' +
      '<div class="adm-card span12"><h3>Últimos 30 dias de bolsa</h3><div class="adm-scroll"><table class="adm-t"><thead><tr><th>Ação</th><th>Dias</th><th class="num">Dias com trades</th><th class="num" title="dias com quotes da Dukascopy">Quotes</th><th class="num" title="dias com trades juntos às quotes (lado agressor)">Juntos</th><th class="num">Trades</th><th class="num">Tamanho</th><th class="num">Último</th></tr></thead><tbody>' +
      (table || '<tr><td colspan="8" class="muted">Ainda nada guardado.</td></tr>') + "</tbody></table></div>" +
      '<div class="adm-legend"><span style="--c:var(--up)">na Drive</span><span style="--c:var(--gold)">só no PC</span><span style="--c:rgba(255,77,109,.55)">em falta (feriado ou sem recolha)</span><span style="--c:#0b0820">antes da primeira descarga</span><span style="--c:var(--neon)">hoje, ainda não acabou</span><span style="--c:linear-gradient(to top, var(--neon) 45%, var(--up) 45%)">pé azul: junto às quotes</span><span style="--c:repeating-linear-gradient(135deg, var(--up) 0 2px, var(--gold) 2px 4px)">às riscas: dia incompleto (passa o rato)</span></div></div>' +
      '<div class="adm-card span12"><h3>Registo</h3><div class="adm-log">' + ((s.registo || []).map((l) => "<div>" + e(l) + "</div>").join("") || '<span class="muted">Ainda nada.</span>') + "</div>" +
      '<p class="muted small">Fontes: ficheiro de trades com atraso da Euronext e ficheiros post-trade com atraso da Deutsche Börse Xetra (MiFIR art. 13), gratuitos, 15 min de atraso. © Euronext N.V. e Deutsche Börse AG.</p></div></div>';
  }

  let countriesFor = null;
  function inList(r) {
    return r.base || (D.lista ? D.lista.keys.indexOf(r.key) >= 0 : r.na_lista);
  }
  function drawCatalog() {
    if (!D || sub !== "cat") return;
    const cat = D.catalogo && D.catalogo.dados;
    if (!cat) { $a("adm-catinfo").textContent = "O catálogo ainda não chegou do PC."; $a("adm-catrows").innerHTML = ""; return; }
    if (countriesFor !== cat) {
      countriesFor = cat;
      const cur = $a("adm-country").value;
      $a("adm-country").innerHTML = '<option value="">Todos os países</option>' + [...new Set(cat.acoes.map((r) => r.country))].sort().map((c) => "<option" + (c === cur ? " selected" : "") + ">" + e(c) + "</option>").join("");
    }
    const q = $a("adm-q").value.trim().toLowerCase(), c = $a("adm-country").value, hl = $a("adm-onlyhl").checked, mine = $a("adm-onlylist").checked;
    const rows = cat.acoes.filter((r) => (!c || r.country === c) && (!hl || r.destaque) && (!mine || inList(r)) &&
      (!q || String(r.company).toLowerCase().includes(q) || String(r.ticker).toLowerCase().includes(q)));
    const sort = $a("adm-sort").value;
    rows.sort(sort === "name" ? (a, b) => String(a.company).localeCompare(String(b.company))
      : sort === "hist" ? (a, b) => String(a.ticks_since).localeCompare(String(b.ticks_since))
      : (a, b) => (inList(b) - inList(a)) || (b.destaque - a.destaque) || String(a.company).localeCompare(String(b.company)));
    const listed = cat.acoes.filter(inList);
    $a("adm-catinfo").textContent = rows.length + " de " + cat.acoes.length + " ações · " + listed.length + " na tua lista · cerca de " +
      fmtBytes(listed.reduce((n, r) => n + (r.bytes_previstos || 0), 0)) + " para a lista toda · " + fmtInt(cat.outras_moedas) + " noutras moedas escondidas" +
      (D.lista ? " · lista mudada " + ago(D.lista.alterada) + (D.lista.por ? " (" + D.lista.por + ")" : "") : "");
    $a("adm-catrows").innerHTML = rows.slice(0, 400).map((r) => "<tr><td>" + (r.destaque ? '<span class="adm-star" title="destaque">★</span>' : "") + "</td>" +
      "<td>" + e(r.company) + "</td><td><code>" + e(r.ticker) + "</code></td><td>" + e(r.country) + ' <span class="muted small">' + e(r.currency) + "</span></td>" +
      "<td>" + e(r.ticks_since) + '</td><td class="num">' + (r.meses == null ? "–" : r.meses) + '</td><td class="num">' + fmtBytes(r.bytes_previstos) + "</td>" +
      '<td class="num">' + (r.base ? '<span class="adm-tag on">base</span>' : '<button class="chip adm-act' + (inList(r) ? "" : " add") + '" data-k="' + e(r.key) + '" data-on="' + (inList(r) ? 1 : 0) + '">' + (inList(r) ? "Tirar" : "Juntar") + "</button>") + "</td></tr>").join("") +
      (rows.length > 400 ? '<tr><td colspan="8" class="muted">A mostrar 400 de ' + rows.length + "; afina a pesquisa.</td></tr>" : "");
  }
  ["adm-q", "adm-country", "adm-sort", "adm-onlyhl", "adm-onlylist"].forEach((id) => $a(id).addEventListener("input", drawCatalog));
  $a("adm-catrows").addEventListener("click", async (ev) => {
    const b = ev.target.closest("button[data-k]"); if (!b) return;
    b.disabled = true;
    try {
      const r = await fetch("/admin/tick/lista", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key: b.dataset.k, adicionar: b.dataset.on !== "1" }) });
      const out = await r.json();
      if (!r.ok) throw new Error(out.erro || r.status);
      D.lista = out.lista;
    } catch (x) { alert("Não consegui mudar a lista: " + x.message); }
    drawCatalog();
  });

  async function load() {
    if (busy) return;
    busy = true;
    try {
      const r = await fetch("/admin/tick/dados", { cache: "no-store", credentials: "same-origin" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      D = await r.json();
      drawQuotes(); drawTrades(); showSub(); drawCatalog();
    } catch (x) {
      $a("adm-fresh").textContent = "não consegui ler os dados (" + x.message + ")";
    } finally { busy = false; }
  }
  function tick() {
    clearTimeout(timer);
    if (!sec.hidden && !document.hidden) load();
    timer = setTimeout(tick, 60000);  // the PC sends every minute
  }
  new MutationObserver(() => { if (!sec.hidden) tick(); }).observe(sec, { attributes: true, attributeFilter: ["hidden"] });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !sec.hidden) tick(); });
  if (!sec.hidden) tick();
})();
`;

export const ADMIN_CSS = `
.adm-top { display:flex; flex-wrap:wrap; align-items:center; gap:8px 14px; margin-bottom:12px; }
.adm-top .chips { margin:0; }
.adm .pill { padding:4px 10px; border-radius:999px; font-size:.75rem; border:1px solid var(--line); color:var(--muted); }
.adm .pill.run { color:var(--up); border-color:var(--up); box-shadow:0 0 8px rgba(57,255,136,.35); }
.adm .pill.run::before { content:""; display:inline-block; width:7px; height:7px; border-radius:50%; background:var(--up); margin-right:6px; animation:admpulse 1.4s infinite; vertical-align:1px; }
.adm .pill.err { color:var(--down); border-color:var(--down); }
@keyframes admpulse { 50% { opacity:.25; } }
.adm-grid { display:grid; gap:14px; grid-template-columns:repeat(12,minmax(0,1fr)); }
.adm-card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; min-width:0; }
.adm-card h3 { margin:0 0 10px; font-family:var(--pixel); font-size:.62rem; font-weight:400; color:var(--neon); text-shadow:0 0 6px rgba(0,229,255,.5); line-height:1.6; }
.span12 { grid-column:span 12; } .span8 { grid-column:span 8; } .span4 { grid-column:span 4; }
@media (max-width:900px) { .span8, .span4 { grid-column:span 12; } }
.adm-big { font-size:2rem; font-weight:700; color:var(--gold); text-shadow:0 0 10px rgba(255,225,77,.45); font-variant-numeric:tabular-nums; }
.adm-row { display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; }
.adm-stats { display:flex; flex-wrap:wrap; gap:8px 22px; margin-top:10px; }
.adm-stats div { display:flex; flex-direction:column; } .adm-stats b { font-size:1.05rem; font-variant-numeric:tabular-nums; } .adm-stats span { font-size:.75rem; color:var(--muted); }
.adm-bar { height:10px; background:#0b0820; border-radius:99px; overflow:hidden; display:flex; }
.adm-bar.thin { height:6px; min-width:60px; }
.adm-bar > i { display:block; height:100%; background:var(--neon); }
.adm-bar > i.b { background:#ff2e88; } .adm-bar > i.g { background:rgba(255,46,136,.35); } .adm-bar > i.m { background:var(--muted); opacity:.5; } .adm-bar > i.t { background:rgba(0,229,255,.35); }
.adm-drive { display:flex; gap:24px; align-items:center; flex-wrap:wrap; } .adm-gauge { flex:1; min-width:240px; }
.adm-legend { display:flex; gap:6px 14px; flex-wrap:wrap; margin-top:8px; font-size:.75rem; color:var(--muted); }
.adm-legend span::before { content:""; display:inline-block; width:9px; height:9px; border-radius:2px; margin-right:5px; background:var(--c); vertical-align:-1px; }
.adm-note { margin-top:10px; padding:9px 12px; border-radius:8px; background:#0f0b2b; border:1px solid var(--line); color:var(--muted); font-size:.8rem; }
.adm-scroll { overflow-x:auto; }
.adm-t { width:100%; border-collapse:collapse; font-size:.85rem; }
.adm-t th { text-align:left; font-size:.75rem; color:var(--muted); font-weight:500; padding:6px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
.adm-t td { padding:7px 8px; border-bottom:1px solid var(--line); vertical-align:middle; font-variant-numeric:tabular-nums; }
.adm-t tr:last-child td { border-bottom:0; } .adm-t .num { text-align:right; }
.adm-feed { max-height:330px; overflow:auto; padding-right:8px; }
.adm-feed div { display:flex; gap:10px; padding:5px 0; border-bottom:1px dashed var(--line); font-variant-numeric:tabular-nums; font-size:.85rem; }
.adm-feed .fail { color:var(--gold); }
.adm-kv { display:grid; grid-template-columns:auto 1fr; gap:6px 14px; font-size:.85rem; } .adm-kv > span:nth-child(odd) { color:var(--muted); }
.adm-strip { display:flex; gap:3px; }
.adm-strip i { flex:0 0 auto; width:9px; height:16px; border-radius:2px; background:#0b0820; }
.adm-strip i.drive { background:var(--up); } .adm-strip i.local { background:var(--gold); } .adm-strip i.falta { background:rgba(255,77,109,.55); }
.adm-strip i.hoje { background:transparent; outline:1px dashed var(--neon); outline-offset:-1px; } .adm-strip i.mon { margin-left:5px; }
.adm-strip i.j { box-shadow:inset 0 -4px 0 var(--neon); } .adm-strip i.inc { background:repeating-linear-gradient(135deg,var(--up) 0 3px,var(--gold) 3px 6px); }
.adm-log { max-height:300px; overflow:auto; font-family:ui-monospace,Consolas,monospace; font-size:.75rem; }
.adm-log div { padding:3px 0; border-bottom:1px dashed var(--line); white-space:pre-wrap; }
.adm-tools { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:10px; }
.adm-tools input[type=search], .adm-tools select { background:#0f0b2b; color:var(--fg); border:1px solid var(--line); border-radius:6px; padding:7px 10px; font:inherit; font-size:.85rem; }
.adm-tools input[type=search] { flex:1; min-width:180px; }
.adm-chk { display:flex; gap:6px; align-items:center; color:var(--muted); font-size:.85rem; cursor:pointer; } .adm-chk input { accent-color:#3d7bff; }
.adm-tag { font-size:.7rem; padding:1px 7px; border-radius:99px; background:#0f0b2b; color:var(--muted); border:1px solid var(--line); }
.adm-tag.on { color:var(--up); border-color:rgba(57,255,136,.5); }
.adm-star { color:var(--gold); }
.adm-act { margin:0; } .adm-act.add { border-color:var(--up); color:var(--up); }
`;
