// Discord interactions endpoint for the CoutoTrading bot (Cloudflare Worker).
//
// /posicao and /ajuda are answered here, instantly. /acao, /etf and /mercado
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

const PING = 1;
const APPLICATION_COMMAND = 2;
const PONG = 1;
const CHANNEL_MESSAGE = 4;
const DEFERRED_CHANNEL_MESSAGE = 5;
const EPHEMERAL = 64;

const WORKFLOW_COMMANDS = new Set(["acao", "etf", "mercado"]);

const TIMEZONE = "Europe/Lisbon";
const DEFAULT_SCHEDULE = {
  dicas: "11:40",
  agentes: "12:15",
  alertas: ["15:40", "17:40", "19:40"],
  semanal: { dia: "sab", hora: "10:00" },
};
const DAYS = ["dom", "seg", "ter", "qua", "qui", "sex", "sab"];
const DAY_NAMES = { dom: "domingo", seg: "segunda", ter: "terça", qua: "quarta", qui: "quinta", sex: "sexta", sab: "sábado" };
const WORKFLOWS = {
  dicas: "discord-daily.yml",
  agentes: "agents.yml",
  alertas: "opportunity-alerts.yml",
  semanal: "weekly-summary.yml",
};
const LATE_LIMIT_MIN = 20; // still start a job this many minutes after its time (missed cron ticks)
const MAX_ALERT_TIMES = 6;

const HELP = [
  "👋 **Bem-vindo! Este canal recebe análise de mercado automática.**",
  "",
  "**O que aparece aqui** (hora de Lisboa; `/horario ver` mostra os horários atuais):",
  "• Dias úteis de manhã · 📊 Dicas do dia, 🏆 top 3 ações por setor e 📦 top 10 ETFs UCITS",
  "• Durante a sessão dos EUA · 🚨 alertas, só quando uma ação da lista rompe com volume",
  "• Dias úteis · 🤖 liga dos agentes: 10 estratégias com dinheiro fictício e preços reais",
  "• Ao fim de semana · 🗓️ resumo semanal com o desempenho dos alertas",
  "",
  "**Comandos** (escreve `/` na caixa de mensagem):",
  "• `/acao NVDA` · tendência, qualidade, pivot e stop de uma ação dos EUA",
  "• `/etf VWCE` · tendência de um ETF UCITS",
  "• `/mercado` · leitura do mercado agora",
  "• `/posicao capital entrada stop` · quantas ações comprar (ex.: `/posicao 10000 35.84 34.14`)",
  "• `/agentes` · liga dos agentes simulados (ou `/agentes nome` para ver um agente)",
  "• `/horario` e `/agente` · horários e gestão dos agentes (só administradores)",
  "• `/ajuda` · esta mensagem",
  "",
  "`/acao`, `/etf` e `/mercado` demoram 1-2 minutos: o bot mostra \"a pensar…\" e depois responde.",
  "",
  "_Ferramenta educativa. Nada aqui é recomendação de compra; as decisões são tuas._",
].join("\n");

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("CoutoTrading bot: endpoint de interações do Discord.", { status: 200 });
    }
    const body = await request.text();
    if (!(await isValidSignature(request, body, env.DISCORD_PUBLIC_KEY))) {
      return new Response("invalid request signature", { status: 401 });
    }
    const interaction = JSON.parse(body);

    if (interaction.type === PING) {
      return json({ type: PONG });
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
    if (name === "agentes" || name === "agente") {
      const sub = name === "agente" ? interaction.data.options?.[0] : null;
      const args = sub
        ? { acao: sub.name, ...Object.fromEntries((sub.options || []).map((o) => [o.name, o.value])) }
        : { acao: "ver", nome: String(options.nome || "") };
      ctx.waitUntil(startInteractionWorkflow(env, interaction, "agents.yml", { mode: "config", args: JSON.stringify(args) }));
      return json({ type: DEFERRED_CHANNEL_MESSAGE });
    }
    if (name === "horario") {
      return json({ type: CHANNEL_MESSAGE, data: { content: await scheduleCommand(env, interaction.data.options?.[0]) } });
    }
    if (WORKFLOW_COMMANDS.has(name)) {
      ctx.waitUntil(startWorkflow(env, interaction, name, String(options.ticker || "")));
      return json({ type: DEFERRED_CHANNEL_MESSAGE });
    }
    return json({ type: CHANNEL_MESSAGE, data: { content: `Comando desconhecido: /${name}`, flags: EPHEMERAL } });
  },

  // Cron trigger, every 5 minutes: start whatever is due and not started yet today.
  async scheduled(event, env, ctx) {
    if (!env.CONFIG) return;
    const now = lisbonNow(new Date(event.scheduledTime));
    const schedule = await getSchedule(env);
    const weekday = now.day !== "sab" && now.day !== "dom";
    const jobs = [];
    if (weekday) {
      jobs.push(["dicas", schedule.dicas]);
      jobs.push(["agentes", schedule.agentes]);
      for (const t of schedule.alertas) jobs.push(["alertas", t]);
    }
    if (now.day === schedule.semanal.dia) jobs.push(["semanal", schedule.semanal.hora]);

    for (const [job, time] of jobs) {
      const late = now.minutes - toMinutes(time);
      if (late < 0 || late >= LATE_LIMIT_MIN) continue;
      const key = `started:${job}:${now.date}:${time}`;
      if (await env.CONFIG.get(key)) continue;
      await env.CONFIG.put(key, "1", { expirationTtl: 3 * 24 * 3600 });
      ctx.waitUntil(dispatchWorkflow(env, WORKFLOWS[job], {}));
    }
  },
};

async function scheduleCommand(env, sub) {
  if (!env.CONFIG) {
    return "⚠️ Os horários ainda não estão ativos: falta ligar o KV `CONFIG` ao Worker na Cloudflare.";
  }
  const schedule = await getSchedule(env);
  const args = Object.fromEntries((sub?.options || []).map((o) => [o.name, String(o.value).trim()]));
  const action = sub?.name || "ver";

  if (action === "dicas" || action === "agentes") {
    if (!isTime(args.hora)) return badTime(args.hora);
    schedule[action] = normaliseTime(args.hora);
  } else if (action === "alertas") {
    const times = (args.horas || "").split(/[,\s]+/).filter(Boolean);
    if (!times.length || times.length > MAX_ALERT_TIMES || !times.every(isTime)) {
      return `⚠️ Indica entre 1 e ${MAX_ALERT_TIMES} horas separadas por vírgulas, ex.: \`15:30,17:30,20:00\`.`;
    }
    schedule.alertas = [...new Set(times.map(normaliseTime))].sort();
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
    `🗓️ Resumo semanal: **${DAY_NAMES[schedule.semanal.dia]} às ${schedule.semanal.hora}**`,
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
