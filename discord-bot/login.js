// Sign in to your portfolio with Discord or Google, besides the private /carteira link.
//
// Discord (scopes identify + guilds): only members of the server get in; the
// first sign-in creates the portfolio, like /carteira.
// Google (scopes openid + email): a member links a Google account from their
// portfolio page first ("Associar conta Google"); that Google account then
// opens the same portfolio. Google alone never creates one, so only members
// get portfolios.
//
// After signing in, a session cookie (HttpOnly, 30 days) lets /entrar open the
// portfolio straight away, e.g. from the portal's Carteira tab.
//
// KV: oauth-state:<state> (10 min), session:<id> -> {owner} (30 days),
// login-google:<sub> -> discord id, discord:ids -> {app, guild} (remembered from
// the interactions). Worker secrets: DISCORD_CLIENT_SECRET, GOOGLE_CLIENT_ID,
// GOOGLE_CLIENT_SECRET (optional DISCORD_CLIENT_ID and DISCORD_GUILD_ID).

import { esc, html, page } from "./chart_page.js";
import { ensurePortfolio } from "./portfolio.js";
import { loadPortfolio, savePortfolio } from "./store.js";
import { HEX24, SESSION_DAYS, cookie, cookies, currentSession, loginActive, newSession } from "./session.js";

const STATE_SECONDS = 600;

function rand() {
  return [...crypto.getRandomValues(new Uint8Array(12))].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function redirect(location, setCookies = []) {
  const headers = new Headers({ Location: location, "Cache-Control": "no-store" });
  for (const c of setCookies) headers.append("Set-Cookie", c);
  return new Response(null, { status: 302, headers });
}

function message(title, body, status = 200) {
  return html(page(title, `<h1>${esc(title)}</h1><div class="panel prose">${body}</div><p><a href="/entrar">← Entrar</a></p>`), status);
}

// The Discord application and server, remembered from the interactions (or set as Worker variables).
export async function rememberDiscord(env, interaction) {
  if (!env.CONFIG || !interaction.guild_id || !interaction.application_id) return;
  const now = await env.CONFIG.get("discord:ids", "json");
  if (now?.app === interaction.application_id && now?.guild === interaction.guild_id) return;
  await env.CONFIG.put("discord:ids", JSON.stringify({ app: interaction.application_id, guild: interaction.guild_id }));
}

async function discordIds(env) {
  const kv = (await env.CONFIG.get("discord:ids", "json")) || {};
  return { app: env.DISCORD_CLIENT_ID || kv.app, guild: env.DISCORD_GUILD_ID || kv.guild };
}

async function currentToken(env, owner) {
  return owner ? env.CONFIG.get(`portfolio-user:${owner}`) : null;
}

async function signIn(env, owner) {
  const tok = await currentToken(env, owner);
  const cur = tok ? await loadPortfolio(env, tok) : null;
  const sid = await newSession(env, owner, cur?.data.session_epoch);
  return redirect(`/c/${tok}`, [cookie("ogj_s", sid, SESSION_DAYS * 86400), cookie("ogj_state", "", 0)]);
}

async function startOAuth(env, request, provider, extra = {}) {
  const origin = new URL(request.url).origin;
  const state = rand();
  await env.CONFIG.put(`oauth-state:${state}`, JSON.stringify({ provider, ...extra }), { expirationTtl: STATE_SECONDS });
  const redirectUri = `${origin}/entrar/${provider}/callback`;
  let url;
  if (provider === "discord") {
    const { app } = await discordIds(env);
    url = "https://discord.com/oauth2/authorize?" + new URLSearchParams({
      client_id: app, response_type: "code", redirect_uri: redirectUri, scope: "identify guilds", state, prompt: "none" });
  } else {
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + new URLSearchParams({
      client_id: env.GOOGLE_CLIENT_ID, response_type: "code", redirect_uri: redirectUri, scope: "openid email", state, prompt: "select_account" });
  }
  return redirect(url, [cookie("ogj_state", state, STATE_SECONDS)]);
}

// The state must be the one this browser started with (no sign-in planted from another site).
async function checkState(env, request, provider) {
  const url = new URL(request.url);
  const state = url.searchParams.get("state") || "";
  if (!HEX24.test(state) || cookies(request).ogj_state !== state) return null;
  const saved = await env.CONFIG.get(`oauth-state:${state}`, "json");
  await env.CONFIG.delete(`oauth-state:${state}`);
  return saved?.provider === provider ? saved : null;
}

async function exchange(tokenUrl, params) {
  const resp = await fetch(tokenUrl, { method: "POST", body: new URLSearchParams(params),
                                      headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" } });
  const out = await resp.json().catch(() => ({}));
  return resp.ok ? out.access_token : null;
}

async function discordCallback(env, request) {
  const url = new URL(request.url);
  if (!(await checkState(env, request, "discord"))) return message("Pedido expirado", "<p>Volta a carregar em <b>Entrar com o Discord</b>.</p>", 400);
  const code = url.searchParams.get("code");
  if (!code) return message("Entrada cancelada", "<p>O Discord não deu autorização.</p>", 400);
  const { app, guild } = await discordIds(env);
  const access = await exchange("https://discord.com/api/oauth2/token", {
    client_id: app, client_secret: env.DISCORD_CLIENT_SECRET, grant_type: "authorization_code", code,
    redirect_uri: `${url.origin}/entrar/discord/callback` });
  if (!access) return message("Não foi possível entrar", "<p>O Discord recusou o pedido. Tenta outra vez.</p>", 502);
  const auth = { headers: { Authorization: `Bearer ${access}` } };
  const [user, guilds] = await Promise.all([
    fetch("https://discord.com/api/users/@me", auth).then((r) => r.ok ? r.json() : null),
    fetch("https://discord.com/api/users/@me/guilds", auth).then((r) => r.ok ? r.json() : null),
  ]);
  if (!user?.id) return message("Não foi possível entrar", "<p>Não consegui ler a tua conta Discord.</p>", 502);
  if (!Array.isArray(guilds) || !guilds.some((g) => g.id === guild)) {
    return message("Só para membros", "<p>Esta conta Discord não está no servidor <b>One Guillion Jollars</b>. As carteiras são só para os membros.</p>", 403);
  }
  await ensurePortfolio(env, user);
  return signIn(env, user.id);
}

async function googleCallback(env, request) {
  const url = new URL(request.url);
  const saved = await checkState(env, request, "google");
  if (!saved) return message("Pedido expirado", "<p>Volta a carregar em <b>Entrar com o Google</b>.</p>", 400);
  const code = url.searchParams.get("code");
  if (!code) return message("Entrada cancelada", "<p>O Google não deu autorização.</p>", 400);
  const access = await exchange("https://oauth2.googleapis.com/token", {
    client_id: env.GOOGLE_CLIENT_ID, client_secret: env.GOOGLE_CLIENT_SECRET, grant_type: "authorization_code", code,
    redirect_uri: `${url.origin}/entrar/google/callback` });
  const info = access ? await fetch("https://openidconnect.googleapis.com/v1/userinfo", { headers: { Authorization: `Bearer ${access}` } })
    .then((r) => r.ok ? r.json() : null) : null;
  if (!info?.sub) return message("Não foi possível entrar", "<p>O Google recusou o pedido. Tenta outra vez.</p>", 502);
  const key = `login-google:${info.sub}`;
  const linked = await env.CONFIG.get(key);

  if (saved.link) {  // linking from the portfolio page
    const cur = await loadPortfolio(env, saved.link);
    const data = cur?.data;
    if (!data?.owner) return message("Link inválido", "<p>Abre a tua carteira outra vez e repete.</p>", 400);
    if (linked && linked !== data.owner) return message("Conta já associada", "<p>Esta conta Google já abre outra carteira. Desassocia-a lá primeiro.</p>", 409);
    if (data.google?.sub && data.google.sub !== info.sub) await env.CONFIG.delete(`login-google:${data.google.sub}`);
    data.google = { sub: info.sub, email: info.email || "", linked: new Date().toISOString().slice(0, 10) };
    if ((await savePortfolio(env, saved.link, data, cur.version, "google_link")) == null) {
      return message("Tenta outra vez", "<p>A carteira mudou ao mesmo tempo noutro sítio.</p>", 409);
    }
    await env.CONFIG.put(key, data.owner);
    return signIn(env, data.owner);
  }
  if (!linked || !(await currentToken(env, linked))) {
    return message("Conta Google não associada",
      `<p>A conta <b>${esc(info.email || "Google")}</b> ainda não está associada a nenhuma carteira.</p>
       <p>Entra primeiro com o <b>Discord</b> (ou com <code>/carteira</code>) e, na tua carteira, carrega em <b>Associar conta Google</b>.
       A partir daí podes entrar só com o Google.</p>`, 403);
  }
  return signIn(env, linked);
}

const REASONS = {
  login: "🔒 Esta carteira pede login: entra com o Discord ou com a conta Google associada.",
  apagada: "🗑️ A carteira foi apagada.",
};

async function loginPage(env, request, reason) {
  const on = await loginActive(env);
  const origin = new URL(request.url).origin;
  const button = (href, label, ok) => ok ? `<a class="btn" style="display:inline-block;text-decoration:none;font-size:.75rem;padding:12px 16px" href="${href}">${label}</a>` : `<span class="btn" style="display:inline-block;opacity:.45;font-size:.75rem;padding:12px 16px">${label} (ainda não ativo)</span>`;
  return html(page("Entrar", `<h1>💼 Entrar na tua carteira</h1>
  ${REASONS[reason] ? `<p class="panel">${esc(REASONS[reason])}</p>` : ""}
  <div class="panel prose">
    <div style="display:flex;flex-wrap:wrap;gap:12px;margin:6px 0 14px">${button("/entrar/discord", "🎮 Entrar com o Discord", on.discord)}
    ${button("/entrar/google", "🔑 Entrar com o Google", on.google)}</div>
    <p class="muted small">Discord: só membros do servidor; a primeira entrada cria a carteira.
    Google: associa primeiro a conta na tua carteira (separador Resumo, "Contas para entrar").
    Também podes usar <code>/carteira</code> no Discord, como até agora.</p>
  </div>
  ${on.discord && on.google ? "" : `<p class="muted small">Endereços de retorno a registar: <code>${esc(origin)}/entrar/discord/callback</code> (Discord) e
  <code>${esc(origin)}/entrar/google/callback</code> (Google).</p>`}`));
}

// /entrar, /entrar/{discord,google}[/callback], /sair. Returns null for other paths.
export async function loginRoute(request, env) {
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/$/, "");
  if (request.method !== "GET" || !env.CONFIG) return null;
  if (path === "/sair") {
    const sid = cookies(request).ogj_s;
    if (sid && HEX24.test(sid)) await env.CONFIG.delete(`session:${sid}`);
    return redirect("/entrar", [cookie("ogj_s", "", 0)]);
  }
  if (path === "/entrar") {
    const session = await currentSession(env, request);
    if (session && url.searchParams.get("trocar") == null) return redirect(`/c/${session.tok}`);
    return loginPage(env, request, url.searchParams.get("motivo"));
  }
  const on = await loginActive(env);
  if (path === "/entrar/discord") return on.discord ? startOAuth(env, request, "discord") : loginPage(env, request);
  if (path === "/entrar/google") {
    if (!on.google) return loginPage(env, request);
    const link = url.searchParams.get("ligar");
    if (link && !HEX24.test(link)) return message("Link inválido", "<p>Abre a tua carteira outra vez e repete.</p>", 400);
    return startOAuth(env, request, "google", link ? { link } : {});
  }
  if (path === "/entrar/discord/callback" && on.discord) return discordCallback(env, request);
  if (path === "/entrar/google/callback" && on.google) return googleCallback(env, request);
  return null;
}
