// Discord interactions endpoint for the CoutoTrading bot (Cloudflare Worker).
//
// /posicao and /ajuda are answered here, instantly. /acao, /etf, /mercado and /limites
// need market data: the Worker replies "a analisar…" and starts the
// discord-bot.yml GitHub Actions workflow, which edits that reply with the
// result (1-2 minutes).
//
// It is also the scheduler: a cron trigger runs scheduled() every 5 minutes,
// which starts the daily tips, the alerts and the weekly summary at the times
// kept in KV (Lisbon time, changed with /horario).
//
// Worker settings (Settings → Variables and Secrets):
//   DISCORD_PUBLIC_KEY  Discord app → General Information → Public Key
//   GITHUB_TOKEN        (secret) fine-grained token, this repo only, Actions: read and write
//   GITHUB_REPO         tigogggg/one-guillion-jollars
// Bindings: KV namespace as CONFIG. Triggers: cron "*/5 * * * *".

import { loginRoute, rememberDiscord } from "./login.js";
import { currentSession } from "./session.js";
import { chartResponse, portalResponse, staticResponse } from "./chart_page.js";
import { portfolioCommand, portfolioRoute } from "./portfolio.js";
import { ICONS } from "./icons.js";
import { dailyBackup } from "./store.js";

// Installable as an app: a service worker that only passes requests through (the pages need live data).
const SERVICE_WORKER = `self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});`;

const PING = 1;
const APPLICATION_COMMAND = 2;
const MESSAGE_COMPONENT = 3;
const PONG = 1;
const CHANNEL_MESSAGE = 4;
const DEFERRED_CHANNEL_MESSAGE = 5;
const EPHEMERAL = 64;

const WORKFLOW_COMMANDS = new Set(["acao", "etf", "mercado", "limites"]);

const TIMEZONE = "Europe/Lisbon";
const DEFAULT_SCHEDULE = {
  dicas: "11:40",
  agentes: "12:15",
  alertas: ["15:40", "17:40", "19:40"],
  acompanhamento: "22:30", // after the US close: how each alert did 5 and 20 sessions later
  estado: "23:00", // health check, after every other weekday job
  noticias: ["12:00", "16:00", "20:30"],
  semanal: { dia: "sab", hora: "10:00" },
  treino: "07:30", // first weekday of each month: re-train the agents on the last 5 years
};
const DAYS = ["dom", "seg", "ter", "qua", "qui", "sex", "sab"];
const DAY_NAMES = { dom: "domingo", seg: "segunda", ter: "terça", qua: "quarta", qui: "quinta", sex: "sexta", sab: "sábado" };
const WORKFLOWS = {
  dicas: "discord-daily.yml",
  agentes: "agents.yml",
  alertas: "opportunity-alerts.yml",
  acompanhamento: "alert-followups.yml",
  estado: "health.yml",
  noticias: "news.yml",
  semanal: "weekly-summary.yml",
  treino: "pretrain.yml",
};
const LATE_LIMIT_MIN = 20; // still start a job this many minutes after its time (missed cron ticks)
const MAX_ALERT_TIMES = 6;
const MAX_FOLLOWS = 20;

const HELP = [
  "👋 **Bem-vindo! Este canal recebe análise de mercado automática.**",
  "",
  "**O que aparece aqui** (hora de Lisboa; `/horario ver` mostra os horários atuais):",
  "• Dias úteis de manhã · 📊 Dicas do dia, 🏆 top 3 ações por setor e 📦 top 10 ETFs UCITS",
  "• Durante a sessão dos EUA · 🚨 alertas, só quando uma ação da lista rompe com volume",
  "• Depois do fecho dos EUA · 🔁 como correu cada alerta 5 e 20 sessões depois (resposta ao alerta)",
  "• Dias úteis · 🤖 liga dos agentes: 16 estratégias com dinheiro fictício e preços reais",
  "• Dias úteis, 3 vezes · 📰 notícias que importam, lidas pelo Claude",
  "• Ao fim de semana · 🗓️ resumo semanal com o desempenho dos alertas",
  "",
  "**Comandos** (escreve `/` na caixa de mensagem):",
  "• `/acao NVDA` · tendência, qualidade, pivot e stop de uma ação dos EUA",
  "• `/etf VWCE` · tendência de um ETF UCITS",
  "• `/mercado` · leitura do mercado agora",
  "• `/limites` · estado da leitura de notícias e das fontes de dados, e limites gastos (Claude, FMP, GitHub)",
  "• `/posicao capital entrada stop` · quantas ações comprar (ex.: `/posicao 10000 35.84 34.14`)",
  "• `/agentes` · liga dos agentes simulados (ou `/agentes nome` para ver um agente)",
  "• `/estrategia nome` · como pensa um agente: regras, parâmetros, pré-treino e resultados",
  "• `/prontidao` · que agentes já merecem confiança (5 critérios)",
  "• `/portal` · página com a corrida dos agentes, notícias, oportunidades com score de segurança e agenda",
  "• `/carteira` · a tua carteira pessoal: regista compras e vendas e acompanha o desempenho (link privado, só para ti)",
  "• `/seguir adicionar NVDA` · a tua lista pessoal: avisos por mensagem privada (`/seguir lista`, `/seguir remover`)",
  "• `/horario` e `/agente` · horários e gestão dos agentes (só administradores)",
  "• `/ajuda` · esta mensagem",
  "",
  "`/acao`, `/etf`, `/mercado` e `/limites` demoram 1-2 minutos: o bot mostra \"a pensar…\" e depois responde.",
  "",
  "_Ferramenta educativa. Nada aqui é recomendação de compra; as decisões são tuas._",
].join("\n");

// Security headers on every answer. Scripts and styles inline (the pages are self-contained),
// plus the few CDNs and the YouTube player; nothing can frame the site.
const CSP = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://www.youtube.com https://s.ytimg.com",
  "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
  "font-src 'self' data: https://fonts.gstatic.com",
  "img-src 'self' data: blob: https:",
  "connect-src 'self'",
  "frame-src 'self' https://www.youtube.com https://www.youtube-nocookie.com",
  "media-src 'self' https:",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");
const SECURITY_HEADERS = {
  "Content-Security-Policy": CSP,
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "strict-origin",  // other sites never see the private /c/<token> path
  "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
  "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
  "Cross-Origin-Opener-Policy": "same-origin",
};

function secure(response) {
  const out = new Response(response.body, response);
  if (out.headers.get("X-Embed") === "1") {  // the portfolio inside the portal: same site only
    out.headers.delete("X-Embed");
    out.headers.set("Content-Security-Policy", CSP.replace("frame-ancestors 'none'", "frame-ancestors 'self'"));
    out.headers.set("X-Frame-Options", "SAMEORIGIN");
  }
  for (const [k, v] of Object.entries(SECURITY_HEADERS)) if (!out.headers.has(k)) out.headers.set(k, v);
  return out;
}

// Requests per minute and address (Workers rate limiting bindings; skipped when not bound).
async function tooMany(env, request, path) {
  const ip = request.headers.get("CF-Connecting-IP") || "?";
  const [binding, key] = path === "/entrar" || path.startsWith("/entrar/") || path === "/sair" ? [env.RL_AUTH, "auth"]
    : request.method === "POST" && path.startsWith("/c/") ? [env.RL_WRITE, "write"]
    : request.method === "GET" && /^\/(c|p|g)\//.test(path) ? [env.RL_READ, "read"] : [null, ""];
  if (!binding?.limit) return false;
  try {
    const { success } = await binding.limit({ key: `${key}:${ip}` });
    return !success;
  } catch {
    return false;
  }
}

export default {
  async fetch(request, env, ctx) {
    const path = new URL(request.url).pathname;
    if (await tooMany(env, request, path)) {
      return secure(new Response("Demasiados pedidos seguidos. Espera um minuto e tenta outra vez.", {
        status: 429, headers: { "Content-Type": "text/plain; charset=utf-8", "Retry-After": "60" } }));
    }
    return secure(await route(request, env, ctx));
  },

  // Cron trigger, every 5 minutes: start whatever is due and not started yet today.
  async scheduled(event, env, ctx) {
    if (!env.CONFIG) return;
    const now = lisbonNow(new Date(event.scheduledTime));
    if (env.DB && now.minutes >= 3 * 60 + 30) ctx.waitUntil(dailyBackup(env, now.date).catch((e) => console.log("backup", e)));
    await runSchedule(env, ctx, now);
  },
};

async function route(request, env, ctx) {
  {
    const path = new URL(request.url).pathname;
    if (request.method === "GET" && ICONS[path.slice(1)]) {
      const bytes = Uint8Array.from(atob(ICONS[path.slice(1)]), (c) => c.charCodeAt(0));
      return new Response(bytes, { headers: { "Content-Type": "image/png", "Cache-Control": "public, max-age=604800" } });
    }
    if (request.method === "GET" && path.startsWith("/static/")) {
      return staticResponse(path) || new Response("Não encontrado.", { status: 404 });
    }
    if (request.method === "GET" && path === "/sw.js") {
      return new Response(SERVICE_WORKER, { headers: { "Content-Type": "text/javascript", "Cache-Control": "no-cache" } });
    }
    if (path === "/entrar" || path.startsWith("/entrar/") || path === "/sair") {
      const out = await loginRoute(request, env);
      if (out) return out;
    }
    const chart = request.method === "GET" && path.match(/^\/g\/([0-9a-f]{24})$/);
    if (chart && env.CONFIG) {
      return chartResponse(env, chart[1], await getSchedule(env));
    }
    const portal = request.method === "GET" && path.match(/^\/p\/([0-9a-f]{24})(\/manifest\.webmanifest)?$/);
    if (portal && env.CONFIG) {
      return portalResponse(env, portal[1], await getSchedule(env), !!portal[2], await currentSession(env, request));
    }
    if (request.method === "GET" && (path === "/" || path === "/manifest.webmanifest") && env.CONFIG) {  // the site's home: the portal for members
      const session = await currentSession(env, request);
      if (!session) return new Response(null, { status: 302, headers: { Location: "/entrar", "Cache-Control": "no-store" } });
      return portalResponse(env, null, await getSchedule(env), path !== "/", session);
    }
    const wallet = path.match(/^\/c\/([0-9a-f]{24})(\/data|\/history|\/analysis|\/versions|\/export|\/manifest\.webmanifest)?$/);
    if (wallet && env.CONFIG) {
      return portfolioRoute(request, env, wallet[1], wallet[2] || "", (workflow, inputs) => dispatchWorkflow(env, workflow, inputs));
    }
    if (request.method !== "POST") {
      return new Response("One Guillion Jollars: endpoint de interações do Discord.", { status: 200 });
    }
    const body = await request.text();
    if (!(await isValidSignature(request, body, env.DISCORD_PUBLIC_KEY))) {
      return new Response("invalid request signature", { status: 401 });
    }
    const interaction = JSON.parse(body);

    if (interaction.type === PING) {
      return json({ type: PONG });
    }
    ctx.waitUntil(rememberDiscord(env, interaction).catch(() => {}));  // for "Entrar com o Discord"
    // Buttons of the pinned portal message: answered only to whoever pressed them.
    if (interaction.type === MESSAGE_COMPONENT) {
      const id = interaction.data?.custom_id;
      let content = "Botão desconhecido.";
      if (id === "carteira") content = env.CONFIG ? await portfolioCommand(env, interaction, new URL(request.url).origin) : "⚠️ As carteiras ainda não estão ativas.";
      if (id === "ajuda") content = HELP;
      return json({ type: CHANNEL_MESSAGE, data: { content, flags: EPHEMERAL } });
    }
    if (interaction.type !== APPLICATION_COMMAND) {
      return json({ type: CHANNEL_MESSAGE, data: { content: "Interação não suportada.", flags: EPHEMERAL } });
    }

    const name = interaction.data.name;
    const options = Object.fromEntries((interaction.data.options || []).map((o) => [o.name, o.value]));

    if (name === "ajuda") {
      return json({ type: CHANNEL_MESSAGE, data: { content: HELP, flags: EPHEMERAL } });
    }
    if (name === "posicao") {
      return json({ type: CHANNEL_MESSAGE, data: { content: positionSize(options), allowed_mentions: { parse: [] } } });
    }
    if (name === "agentes" || name === "agente" || name === "estrategia" || name === "prontidao") {
      const sub = name === "agente" ? interaction.data.options?.[0] : null;
      const args = sub
        ? { acao: sub.name, ...Object.fromEntries((sub.options || []).map((o) => [o.name, o.value])) }
        : { acao: name === "agentes" ? "ver" : name, nome: String(options.nome || "") };
      ctx.waitUntil(startInteractionWorkflow(env, interaction, "agents.yml", { mode: "config", args: JSON.stringify(args) }));
      return json({ type: DEFERRED_CHANNEL_MESSAGE });
    }
    if (name === "carteira") {
      const content = env.CONFIG ? await portfolioCommand(env, interaction, new URL(request.url).origin)
        : "⚠️ As carteiras ainda não estão ativas.";
      return json({ type: CHANNEL_MESSAGE, data: { content, flags: EPHEMERAL } });
    }
    if (name === "portal") {
      let token = env.CONFIG ? await env.CONFIG.get("portal:token", "json") : null;
      if (env.CONFIG && !token) {  // first use: create the server's portal link
        token = [...crypto.getRandomValues(new Uint8Array(12))].map((b) => b.toString(16).padStart(2, "0")).join("");
        await env.CONFIG.put("portal:token", JSON.stringify(token));
      }
      const content = token
        ? `🏟️ **One Guillion Jollars**: corrida e fichas dos agentes, notícias, oportunidades com score de segurança e agenda.\n${new URL(request.url).origin}/p/${token}\n🔑 Ou entra em ${new URL(request.url).origin}/ com o Discord ou o Google: o portal e a tua carteira no mesmo sítio.\n-# Link do servidor: partilha só com os membros.`
        : "⚠️ O portal aparece depois da próxima liga dos agentes (dias úteis às 12:15).";
      return json({ type: CHANNEL_MESSAGE, data: { content, flags: EPHEMERAL } });
    }
    if (name === "seguir") {
      const user = interaction.member?.user?.id || interaction.user?.id;
      const content = await followCommand(env, user, interaction.data.options?.[0]);
      return json({ type: CHANNEL_MESSAGE, data: { content, flags: EPHEMERAL } });
    }
    if (name === "horario") {
      return json({ type: CHANNEL_MESSAGE, data: { content: await scheduleCommand(env, interaction.data.options?.[0]) } });
    }
    if (WORKFLOW_COMMANDS.has(name)) {
      ctx.waitUntil(startWorkflow(env, interaction, name, String(options.ticker || "")));
      return json({ type: DEFERRED_CHANNEL_MESSAGE });
    }
    return json({ type: CHANNEL_MESSAGE, data: { content: `Comando desconhecido: /${name}`, flags: EPHEMERAL } });
  }
}

async function runSchedule(env, ctx, now) {
  {
    const schedule = await getSchedule(env);
    const weekday = now.day !== "sab" && now.day !== "dom";
    const jobs = [];
    if (weekday) {
      jobs.push(["dicas", schedule.dicas]);
      jobs.push(["agentes", schedule.agentes]);
      for (const t of schedule.alertas) jobs.push(["alertas", t]);
      jobs.push(["acompanhamento", schedule.acompanhamento]);
      jobs.push(["estado", schedule.estado]);
      for (const t of schedule.noticias) jobs.push(["noticias", t]);
    }
    if (now.day === schedule.semanal.dia) jobs.push(["semanal", schedule.semanal.hora]);
    const dayOfMonth = Number(now.date.slice(8));
    if (weekday && (dayOfMonth === 1 || (now.day === "seg" && dayOfMonth <= 3))) jobs.push(["treino", schedule.treino]);

    for (const [job, time] of jobs) {
      const late = now.minutes - toMinutes(time);
      if (late < 0 || late >= LATE_LIMIT_MIN) continue;
      const key = `started:${job}:${now.date}:${time}`;
      if (await env.CONFIG.get(key)) continue;
      await env.CONFIG.put(key, "1", { expirationTtl: 3 * 24 * 3600 });
      ctx.waitUntil(dispatchWorkflow(env, WORKFLOWS[job], {}));
    }
  }
}

// /seguir: a personal list of tickers in KV (follows:<user id>); the alerts
// workflow DMs each user about their stocks.
async function followCommand(env, user, sub) {
  if (!env.CONFIG || !user) return "⚠️ As listas pessoais ainda não estão ativas.";
  const key = `follows:${user}`;
  const list = (await env.CONFIG.get(key, "json")) || [];
  const ticker = String(sub?.options?.find((o) => o.name === "ticker")?.value || "").trim().toUpperCase();
  const action = sub?.name || "lista";

  if (action === "adicionar" || action === "remover") {
    if (!/^[A-Z0-9][A-Z0-9.\-]{0,9}$/.test(ticker)) return `⚠️ \`${ticker.slice(0, 12)}\` não parece um ticker (ex.: NVDA).`;
    if (action === "adicionar") {
      if (list.includes(ticker)) return `**${ticker}** já está na tua lista.`;
      if (list.length >= MAX_FOLLOWS) return `⚠️ A lista tem no máximo ${MAX_FOLLOWS} ações. Remove alguma primeiro.`;
      list.push(ticker);
    } else if (!list.includes(ticker)) {
      return `**${ticker}** não está na tua lista.`;
    } else {
      list.splice(list.indexOf(ticker), 1);
    }
    if (list.length) await env.CONFIG.put(key, JSON.stringify(list));
    else await env.CONFIG.delete(key);
  }
  const header = action === "adicionar" ? `✅ **${ticker}** adicionada.` : action === "remover" ? `🗑️ **${ticker}** removida.` : "";
  const body = list.length
    ? `A tua lista (${list.length}/${MAX_FOLLOWS}): ${list.map((t) => `**${t}**`).join(", ")}`
    : "A tua lista está vazia. Usa `/seguir adicionar NVDA`.";
  return [header, body, "-# Recebes uma mensagem privada quando uma destas ações rompe com volume ou mexe 5% ou mais no dia (verificado 3 vezes por dia útil)."]
    .filter(Boolean)
    .join("\n");
}

async function scheduleCommand(env, sub) {
  if (!env.CONFIG) {
    return "⚠️ Os horários ainda não estão ativos: falta ligar o KV `CONFIG` ao Worker na Cloudflare.";
  }
  const schedule = await getSchedule(env);
  const args = Object.fromEntries((sub?.options || []).map((o) => [o.name, String(o.value).trim()]));
  const action = sub?.name || "ver";

  if (action === "dicas" || action === "agentes" || action === "acompanhamento" || action === "estado") {
    if (!isTime(args.hora)) return badTime(args.hora);
    schedule[action] = normaliseTime(args.hora);
  } else if (action === "alertas" || action === "noticias") {
    const times = (args.horas || "").split(/[,\s]+/).filter(Boolean);
    if (!times.length || times.length > MAX_ALERT_TIMES || !times.every(isTime)) {
      return `⚠️ Indica entre 1 e ${MAX_ALERT_TIMES} horas separadas por vírgulas, ex.: \`15:30,17:30,20:00\`.`;
    }
    schedule[action] = [...new Set(times.map(normaliseTime))].sort();
  } else if (action === "semanal") {
    if (!DAYS.includes(args.dia)) return "⚠️ Dia inválido.";
    if (!isTime(args.hora)) return badTime(args.hora);
    schedule.semanal = { dia: args.dia, hora: normaliseTime(args.hora) };
  } else if (action === "repor") {
    Object.assign(schedule, structuredClone(DEFAULT_SCHEDULE));
  }

  if (action !== "ver") await env.CONFIG.put("schedule", JSON.stringify(schedule));
  return [
    action === "ver" ? "🕒 **Horários atuais** (hora de Lisboa)" : "✅ **Horários atualizados** (hora de Lisboa)",
    `📊 Dicas do dia e rankings: **${schedule.dicas}** (dias úteis)`,
    `🤖 Liga dos agentes: **${schedule.agentes}** (dias úteis, depois das dicas)`,
    `🚨 Verificação de alertas: **${schedule.alertas.join(" · ")}** (dias úteis; a sessão dos EUA é das 14:30 às 21:00)`,
    `🔁 Acompanhamento dos alertas: **${schedule.acompanhamento}** (dias úteis, depois do fecho dos EUA)`,
    `📰 Notícias: **${schedule.noticias.join(" · ")}** (dias úteis)`,
    `🗓️ Resumo semanal: **${DAY_NAMES[schedule.semanal.dia]} às ${schedule.semanal.hora}**`,
    `🔧 Verificação do estado do bot: **${schedule.estado}** (dias úteis, no canal 🔧-estado)`,
    `🧠 Re-treino dos agentes (últimos 5 anos): **1.º dia útil de cada mês às ${schedule.treino}**`,
    "-# As análises arrancam até 5 minutos depois da hora marcada e demoram alguns minutos a chegar.",
  ].join("\n");
}

async function getSchedule(env) {
  const saved = await env.CONFIG.get("schedule", "json");
  return { ...structuredClone(DEFAULT_SCHEDULE), ...(saved || {}) };
}

function lisbonNow(date) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-GB", {
      timeZone: TIMEZONE,
      weekday: "short",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    })
      .formatToParts(date)
      .map((p) => [p.type, p.value]),
  );
  const day = { Sun: "dom", Mon: "seg", Tue: "ter", Wed: "qua", Thu: "qui", Fri: "sex", Sat: "sab" }[parts.weekday];
  return { day, date: `${parts.year}-${parts.month}-${parts.day}`, minutes: Number(parts.hour) * 60 + Number(parts.minute) };
}

function isTime(t) {
  return /^([01]?\d|2[0-3])[:h]([0-5]\d)$/.test(t || "");
}

function normaliseTime(t) {
  const [h, m] = t.split(/[:h]/);
  return `${h.padStart(2, "0")}:${m}`;
}

function toMinutes(t) {
  const [h, m] = t.split(":").map(Number);
  return h * 60 + m;
}

function badTime(t) {
  return `⚠️ Hora inválida: \`${t || ""}\`. Usa o formato HH:MM, ex.: \`11:40\`.`;
}

function positionSize({ capital, entrada, stop, risco = 1, max_posicao = 15 }) {
  if (!(capital > 0 && entrada > 0 && stop > 0)) {
    return "Indica capital, entrada e stop maiores que zero.";
  }
  if (stop >= entrada) {
    return "O stop tem de ficar abaixo do preço de entrada (posições compradas).";
  }
  const riskPerShare = entrada - stop;
  const byRisk = Math.floor((capital * risco) / 100 / riskPerShare);
  const byCap = Math.floor((capital * max_posicao) / 100 / entrada);
  const shares = Math.min(byRisk, byCap);
  if (shares < 1) {
    return `Com estes valores não dá para comprar nenhuma ação: o risco de ${fmt(risco)}% são $${fmt(capital * risco / 100)}, menos que o risco de uma ação ($${fmt(riskPerShare)}).`;
  }
  const value = shares * entrada;
  const loss = shares * riskPerShare;
  const limit = byCap < byRisk ? `limitado pelo máximo de ${fmt(max_posicao)}% por posição` : `pela regra de ${fmt(risco)}% de risco`;
  return [
    `📐 **Tamanho da posição**: capital $${fmt(capital)}, entrada $${fmt(entrada)}, stop $${fmt(stop)}`,
    `**${shares} ações** · $${fmt(value)} (${fmt((value / capital) * 100)}% da conta) · ${limit}`,
    `Perda se o stop for atingido: $${fmt(loss)} (${fmt((loss / capital) * 100)}% da conta, ${fmt((riskPerShare / entrada) * 100)}% por ação)`,
    `Alvos: 2R $${fmt(entrada + 2 * riskPerShare)} · 3R $${fmt(entrada + 3 * riskPerShare)}`,
    "-# Em zona Neutra do mercado, considera risco de 0,75% (`risco:0.75`). Não é recomendação de compra.",
  ].join("\n");
}

async function dispatchWorkflow(env, workflow, inputs) {
  const resp = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${workflow}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "coutotrading-discord-bot",
    },
    body: JSON.stringify({ ref: "main", inputs }),
  });
  if (!resp.ok) console.log(`dispatch ${workflow} failed: ${resp.status} ${await resp.clone().text()}`);
  return resp;
}

async function startWorkflow(env, interaction, command, arg) {
  return startInteractionWorkflow(env, interaction, "discord-bot.yml", { command, arg });
}

// Start a workflow that edits this interaction's deferred reply; report failures in the reply.
async function startInteractionWorkflow(env, interaction, workflow, inputs) {
  const resp = await dispatchWorkflow(env, workflow, {
    ...inputs,
    application_id: interaction.application_id,
    token: interaction.token,
  });
  if (!resp.ok) {
    const detail = (await resp.text()).slice(0, 200);
    await fetch(`https://discord.com/api/v10/webhooks/${interaction.application_id}/${interaction.token}/messages/@original`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: `⚠️ Não consegui iniciar a análise (GitHub respondeu ${resp.status}: ${detail}).` }),
    });
  }
}

async function isValidSignature(request, body, publicKey) {
  const signature = request.headers.get("X-Signature-Ed25519");
  const timestamp = request.headers.get("X-Signature-Timestamp");
  if (!signature || !timestamp || !publicKey) return false;
  try {
    const key = await crypto.subtle.importKey("raw", hexToBytes(publicKey), { name: "Ed25519" }, false, ["verify"]);
    return await crypto.subtle.verify("Ed25519", key, hexToBytes(signature), new TextEncoder().encode(timestamp + body));
  } catch {
    return false;
  }
}

function hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
  return bytes;
}

function fmt(n) {
  return Number(n).toLocaleString("pt-PT", { maximumFractionDigits: 2 });
}

function json(data) {
  return new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } });
}
