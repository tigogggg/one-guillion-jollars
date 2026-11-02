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

// Where to go after signing in: a path on this site only.
export function safeNext(v) {
  const t = String(v || "");
  return /^\/(?![\/\\])[^\s]{0,300}$/.test(t) && !t.startsWith("/entrar") && !t.startsWith("/sair") ? t : "/";
}

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

async function signIn(env, owner, next = "/") {
  const tok = await currentToken(env, owner);
  const cur = tok ? await loadPortfolio(env, tok) : null;
  const sid = await newSession(env, owner, cur?.data.session_epoch);
  return redirect(safeNext(next), [cookie("ogj_s", sid, SESSION_DAYS * 86400), cookie("ogj_state", "", 0), cookie("ogj_gp", "", 0)]);
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
  const saved = await checkState(env, request, "discord");
  if (!saved) return message("Pedido expirado", "<p>Volta a carregar em <b>Entrar com o Discord</b>.</p>", 400);
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
  const tok = await ensurePortfolio(env, user);
  if (saved.google_pending) {  // "Entrar com o Discord e associar esta conta Google"
    const pending = await env.CONFIG.get(`google-pending:${saved.google_pending}`, "json");
    await env.CONFIG.delete(`google-pending:${saved.google_pending}`);
    if (pending?.sub) {
      const error = await linkGoogle(env, tok, pending);
      if (error) return message("Conta Google não associada", `<p>${esc(error)}</p><p><a href="/c/${tok}">Abrir a minha carteira →</a></p>`, 409);
    }
  }
  return signIn(env, user.id, saved.next);
}

// Link a Google account ({sub, email}) to the portfolio behind tok; an error message, or null.
async function linkGoogle(env, tok, info) {
  const key = `login-google:${info.sub}`;
  const cur = await loadPortfolio(env, tok);
  const data = cur?.data;
  if (!data?.owner) return "Carteira não encontrada.";
  const linked = await env.CONFIG.get(key);
  if (linked && linked !== data.owner) return "Esta conta Google já abre outra carteira. Desassocia-a lá primeiro.";
  const old = data.google?.sub;
  data.google = { sub: info.sub, email: info.email || "", linked: new Date().toISOString().slice(0, 10) };
  if ((await savePortfolio(env, tok, data, cur.version, "google_link")) == null) return "A carteira mudou ao mesmo tempo noutro sítio. Tenta outra vez.";
  if (old && old !== info.sub) await env.CONFIG.delete(`login-google:${old}`);
  await env.CONFIG.put(key, data.owner);
  return null;
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
    const error = await linkGoogle(env, saved.link, info);
    if (error) return message("Conta Google não associada", `<p>${esc(error)}</p>`, 409);
    const data = (await loadPortfolio(env, saved.link)).data;
    return signIn(env, data.owner, saved.next);
  }
  if (!linked || !(await currentToken(env, linked))) {
    const on = await loginActive(env);
    const pending = rand();  // kept 10 minutes, tied to this browser by a cookie
    await env.CONFIG.put(`google-pending:${pending}`, JSON.stringify({ sub: info.sub, email: info.email || "" }), { expirationTtl: STATE_SECONDS });
    const resp = message("Conta Google não associada",
      `<p>A conta <b>${esc(info.email || "Google")}</b> ainda não está associada a nenhuma carteira.</p>
       <p>As carteiras são dos membros do servidor, por isso a primeira vez confirmas com o Discord:</p>
       ${on.discord ? `<p><a class="btn" style="display:inline-block;text-decoration:none;font-size:.75rem;padding:12px 16px" href="/entrar/discord?associar=1">🎮 ENTRAR COM O DISCORD E ASSOCIAR ESTA CONTA GOOGLE</a></p>` : ""}
       <p class="muted small">Depois disso, entras só com o Google. Também podes associar na carteira, em 🔒 Conta → Associar conta Google.</p>`, 403);
    resp.headers.append("Set-Cookie", cookie("ogj_gp", pending, STATE_SECONDS));
    return resp;
  }
  return signIn(env, linked, saved.next);
}

const REASONS = {
  login: "🔒 Esta carteira pede login: entra com o Discord ou com a conta Google associada.",
  apagada: "🗑️ A carteira foi apagada.",
};

// The front door: nothing inside the site is visible without signing in.
async function loginPage(env, request, reason, next = "/") {
  const on = await loginActive(env);
  const origin = new URL(request.url).origin;
  const q = next && next !== "/" ? `?voltar=${encodeURIComponent(next)}` : "";
  const button = (href, label, ok, cls = "") => ok
    ? `<a class="btn big ${cls}" href="${href}">${label}</a>`
    : `<span class="btn big off">${label} · em breve</span>`;
  return html(page("One Guillion Jollars", `<div class="door">
  <img class="door-logo" src="/icon-512.png" alt="1GJ" width="160" height="160">
  <h1 class="door-title">One Guillion Jollars</h1>
  <p class="door-sub">Agentes de investimento treinados em 20 anos de mercado, notícias lidas por IA e a tua carteira, num só sítio.</p>
  ${REASONS[reason] ? `<p class="panel">${esc(REASONS[reason])}</p>` : ""}
  <div class="panel door-box">
    <h2>▶ PRESS START</h2>
    <div class="door-btns">
      ${button(`/entrar/discord${q}`, "🎮 ENTRAR COM O DISCORD", on.discord, "discord")}
      ${button(`/entrar/google${q}`, "🔑 ENTRAR COM O GOOGLE", on.google, "alt")}
    </div>
    <p class="muted small">Só para membros do servidor do Discord. A primeira entrada é com o Discord (cria a tua carteira); depois podes associar uma conta Google em 💼 Carteira → 🔒 Conta.
    A sessão fica aberta 30 dias neste aparelho.</p>
  </div>
  <div class="door-feats">
    <div class="tile"><div class="k">🏁 Liga dos agentes</div><div class="s">Estratégias simuladas a competir todos os dias, com carteiras e justificações.</div></div>
    <div class="tile"><div class="k">🎯 Oportunidades</div><div class="s">Compras e vendas com score de segurança explicado e plano (entrada, stop, alvo).</div></div>
    <div class="tile"><div class="k">💼 A tua carteira</div><div class="s">Importa da XTB, acompanha em tempo quase real e pede uma análise ao Claude.</div></div>
  </div>
  <p class="muted small door-foot">Ferramenta educativa: não é aconselhamento financeiro.</p>
  ${on.discord && on.google ? "" : `<p class="muted small">Endereços de retorno a registar: <code>${esc(origin)}/entrar/discord/callback</code> e <code>${esc(origin)}/entrar/google/callback</code>.</p>`}
</div>`, `<style>
.door { max-width:760px; margin:6vh auto 0; text-align:center; }
.door-logo { display:block; width:clamp(110px,28vw,160px); height:auto; margin:0 auto .4rem; border-radius:18px; box-shadow:0 0 24px rgba(255,180,60,.35); }
.door-title { font-size:clamp(1.3rem,4.5vw,2.2rem); margin:.2em 0; } .door-sub { color:var(--muted); font-size:1.05rem; }
.door-box { text-align:center; padding:22px 16px; } .door-box h2 { margin:0 0 14px; animation:blink 1.2s steps(2) infinite; }
@keyframes blink { 50% { opacity:.35; } }
.door-btns { display:flex; flex-wrap:wrap; gap:12px; justify-content:center; margin-bottom:14px; }
.btn.big { display:inline-block; text-decoration:none; font-size:.8rem; padding:14px 18px; } .btn.big.discord { background:#5865f2; border-bottom-color:#3b44b0; }
.btn.big.off { opacity:.45; cursor:default; }
.door-feats { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:10px; margin-top:16px; text-align:left; }
.door-foot { margin-top:18px; }
</style>`));
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
    const next = safeNext(url.searchParams.get("voltar"));
    if (session && url.searchParams.get("trocar") == null) return redirect(next);
    return loginPage(env, request, url.searchParams.get("motivo"), next);
  }
  const on = await loginActive(env);
  if (path === "/entrar/discord") {
    if (!on.discord) return loginPage(env, request);
    const gp = url.searchParams.get("associar") != null ? cookies(request).ogj_gp : null;
    const next = safeNext(url.searchParams.get("voltar"));
    return startOAuth(env, request, "discord", gp && HEX24.test(gp) ? { google_pending: gp, next } : { next });
  }
  if (path === "/entrar/google") {
    if (!on.google) return loginPage(env, request);
    const link = url.searchParams.get("ligar");
    if (link && !HEX24.test(link)) return message("Link inválido", "<p>Abre a tua carteira outra vez e repete.</p>", 400);
    const next = safeNext(url.searchParams.get("voltar") || (link ? "/#carteira" : "/"));
    return startOAuth(env, request, "google", link ? { link, next } : { next });
  }
  if (path === "/entrar/discord/callback" && on.discord) return discordCallback(env, request);
  if (path === "/entrar/google/callback" && on.google) return googleCallback(env, request);
  return null;
}
