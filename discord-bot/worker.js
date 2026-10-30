// Discord interactions endpoint for the CoutoTrading bot (Cloudflare Worker).
//
// /posicao and /ajuda are answered here, instantly. /acao, /etf and /mercado
// need market data: the Worker replies "a analisar…" and starts the
// discord-bot.yml GitHub Actions workflow, which edits that reply with the
// result (1-2 minutes).
//
// Worker settings (Settings → Variables and Secrets):
//   DISCORD_PUBLIC_KEY  Discord app → General Information → Public Key
//   GITHUB_TOKEN        (secret) fine-grained token, this repo only, Actions: read and write
//   GITHUB_REPO         tigogggg/one-guillion-jollars

const PING = 1;
const APPLICATION_COMMAND = 2;
const PONG = 1;
const CHANNEL_MESSAGE = 4;
const DEFERRED_CHANNEL_MESSAGE = 5;
const EPHEMERAL = 64;

const WORKFLOW_COMMANDS = new Set(["acao", "etf", "mercado"]);

const HELP = [
  "👋 **Bem-vindo! Este canal recebe análise de mercado automática.**",
  "",
  "**O que aparece aqui (dias úteis, hora UTC):**",
  "• 11:37 · 📊 Dicas do dia: saúde do mercado (breadth), exposição sugerida e setups VCP",
  "• 11:37 · 🏆 Top 3 ações por setor e 📦 top 10 ETFs UCITS (compráveis na Europa)",
  "• 15:37, 17:37, 19:37 · 🚨 Alertas, só quando uma ação da lista rompe com volume",
  "",
  "**Comandos** (escreve `/` na caixa de mensagem):",
  "• `/acao NVDA` · tendência, qualidade, pivot e stop de uma ação dos EUA",
  "• `/etf VWCE` · tendência de um ETF UCITS",
  "• `/mercado` · leitura do mercado agora",
  "• `/posicao capital entrada stop` · quantas ações comprar (ex.: `/posicao 10000 35.84 34.14`)",
  "• `/ajuda` · esta mensagem",
  "",
  "`/acao`, `/etf` e `/mercado` demoram 1-2 minutos: o bot responde \"a analisar…\" e depois atualiza.",
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
    if (WORKFLOW_COMMANDS.has(name)) {
      ctx.waitUntil(startWorkflow(env, interaction, name, String(options.ticker || "")));
      return json({ type: DEFERRED_CHANNEL_MESSAGE });
    }
    return json({ type: CHANNEL_MESSAGE, data: { content: `Comando desconhecido: /${name}`, flags: EPHEMERAL } });
  },
};

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

async function startWorkflow(env, interaction, command, arg) {
  const resp = await fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/discord-bot.yml/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "coutotrading-discord-bot",
      },
      body: JSON.stringify({
        ref: "main",
        inputs: { command, arg, application_id: interaction.application_id, token: interaction.token },
      }),
    },
  );
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
