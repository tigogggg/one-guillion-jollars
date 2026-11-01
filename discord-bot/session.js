// Sign-in sessions: an HttpOnly cookie ogj_s=<id>, KV session:<id> -> {owner, epoch}.
// "Terminar sessão em todos os aparelhos" bumps the portfolio's session_epoch, which voids
// every session made before it.

import { loadPortfolio } from "./store.js";

export const SESSION_DAYS = 30;
export const HEX24 = /^[0-9a-f]{24}$/;

export function cookies(request) {
  return Object.fromEntries((request.headers.get("Cookie") || "").split(/;\s*/).filter(Boolean).map((c) => {
    const i = c.indexOf("=");
    return [c.slice(0, i), c.slice(i + 1)];
  }));
}

export function cookie(name, value, seconds) {
  return `${name}=${value}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${seconds}`;
}

// {sid, owner, tok, data} for a valid session, else null.
export async function currentSession(env, request) {
  const sid = cookies(request).ogj_s;
  if (!sid || !HEX24.test(sid)) return null;
  const s = await env.CONFIG.get(`session:${sid}`, "json");
  if (!s?.owner) return null;
  const tok = await env.CONFIG.get(`portfolio-user:${s.owner}`);
  const cur = tok ? await loadPortfolio(env, tok) : null;
  if (!cur || cur.data.owner !== s.owner || (s.epoch || 0) !== (cur.data.session_epoch || 0)) return null;
  return { sid, owner: s.owner, tok, data: cur.data };
}

export async function newSession(env, owner, epoch) {
  const sid = [...crypto.getRandomValues(new Uint8Array(12))].map((b) => b.toString(16).padStart(2, "0")).join("");
  await env.CONFIG.put(`session:${sid}`, JSON.stringify({ owner, epoch: epoch || 0, at: new Date().toISOString() }),
                       { expirationTtl: SESSION_DAYS * 86400 });
  return sid;
}

// Signing in works (Discord or Google configured): only then can a portfolio require it.
export async function loginActive(env) {
  const kv = (await env.CONFIG.get("discord:ids", "json")) || {};
  const discord = !!((env.DISCORD_CLIENT_ID || kv.app) && (env.DISCORD_GUILD_ID || kv.guild) && env.DISCORD_CLIENT_SECRET);
  const google = !!(env.GOOGLE_CLIENT_ID && env.GOOGLE_CLIENT_SECRET);
  return { discord, google, any: discord || google };
}
