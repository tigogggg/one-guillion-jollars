"""One-off Discord setup: register the bot's slash commands and post the welcome card.

    --register  PUT the commands below (needs DISCORD_APPLICATION_ID and DISCORD_BOT_TOKEN)
    --welcome   post the welcome card to DISCORD_WEBHOOK_URL, to pin in the channel
    --canais    create the 📈 FINANÇAS category with read-only channels for the bot's
                posts (needs the bot with Manage Channels and Manage Roles) and
                write their ids to discord/channels.json
    --canal-testes  create the read-only 🧪-testes channel in that category; posts
                go there instead of their usual channel when DISCORD_TESTES is set
    --canal-estado  create the read-only 🔧-estado channel for the daily health check
    --portal    create the read-only 🏟️-portal channel at the top of the category and
                pin the portal message there: banner, what is where, and buttons to
                open the portal, get one's private /carteira link and the help
    --apagar-canais IDS  delete these channels (comma-separated ids; categories last),
                e.g. the old ones after moving to another server; refuses any
                channel of the server in discord/channels.json
    --espelho ID    create the same category and channels (plus #billions) in a
                second server and write them to discord/mirror.json; every bot
                post is then copied there

Run from the "Configurar o bot do Discord" workflow. Keep the command list in
step with discord-bot/worker.js.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import requests

from discord_post import BLUE, CHANNELS_PATH, MIRROR_PATH, REPO, embed, post_message, send

SUB_COMMAND, STRING, BOOLEAN, NUMBER = 1, 3, 5, 10
MANAGE_SERVER = "32"  # permission bit: only server admins see /horario
# Mirrors agents.STRATEGIES (not imported: that module pulls in pandas and yfinance).
STRATEGY_CHOICES = [
    ("indexador", "compra e mantém o S&P 500"),
    ("guardiao", "ETF mundial com peso pela zona do mercado"),
    ("rotador", "roda entre os melhores ETFs UCITS"),
    ("foguetao", "top ações por score de tendência"),
    ("joalheiro", "melhores do ranking com qualidade alta"),
    ("cacador", "rompimentos com volume"),
    ("sniper", "contração de volatilidade (VCP)"),
    ("campeao", "melhores ações dos setores mais fortes"),
    ("pescador", "compra quedas em ações em tendência"),
    ("jornalista", "negoceia notícias analisadas pelo Claude"),
    ("deriva", "segue saltos com volume (resultados, notícias)"),
    ("catalisador", "notícias positivas confirmadas pelo preço"),
    ("cetico", "más notícias que o mercado ignorou"),
    ("conselho", "compra o que vários agentes apoiam"),
    ("maestro", "copia os agentes com melhores resultados"),
    ("kamikaze", "tudo nas maiores subidas a 3 meses"),
]
WEEKDAYS = [("segunda", "seg"), ("terça", "ter"), ("quarta", "qua"), ("quinta", "qui"),
            ("sexta", "sex"), ("sábado", "sab"), ("domingo", "dom")]

COMMANDS = [
    {
        "name": "acao",
        "description": "Tendência, qualidade, pivot e stop de uma ação dos EUA",
        "options": [{"type": STRING, "name": "ticker", "description": "Ex.: NVDA, AAPL, KDP", "required": True}],
    },
    {
        "name": "etf",
        "description": "Tendência de um ETF UCITS (comprável na Europa)",
        "options": [{"type": STRING, "name": "ticker", "description": "Ex.: VWCE, SXR8, VVSM", "required": True}],
    },
    {"name": "mercado", "description": "Leitura do mercado agora: breadth, exposição sugerida e setores"},
    {"name": "portal", "description": "Link do portal: corrida dos agentes, notícias, oportunidades e agenda"},
    {
        "name": "carteira",
        "description": "A tua carteira pessoal: regista compras e vendas e acompanha o desempenho (link privado)",
        "options": [{"type": BOOLEAN, "name": "novo_link", "description": "Trocar o link (o antigo deixa de funcionar)"}],
    },
    {"name": "limites", "description": "Estado da leitura de notícias e das fontes de dados das ações, e limites gastos"},
    {
        "name": "posicao",
        "description": "Quantas ações comprar para o risco que escolheres",
        "options": [
            {"type": NUMBER, "name": "capital", "description": "Valor da conta", "required": True, "min_value": 1},
            {"type": NUMBER, "name": "entrada", "description": "Preço de entrada", "required": True, "min_value": 0.01},
            {"type": NUMBER, "name": "stop", "description": "Preço do stop-loss", "required": True, "min_value": 0.01},
            {"type": NUMBER, "name": "risco", "description": "% da conta a arriscar (por defeito 1)", "min_value": 0.1, "max_value": 5},
            {"type": NUMBER, "name": "max_posicao", "description": "Máximo % da conta numa posição (por defeito 15)", "min_value": 1, "max_value": 100},
        ],
    },
    {
        "name": "horario",
        "description": "Ver ou mudar as horas das análises (hora de Lisboa)",
        "default_member_permissions": MANAGE_SERVER,
        "options": [
            {"type": SUB_COMMAND, "name": "ver", "description": "Mostrar os horários atuais"},
            {
                "type": SUB_COMMAND,
                "name": "dicas",
                "description": "Hora das dicas do dia e dos rankings (dias úteis)",
                "options": [{"type": STRING, "name": "hora", "description": "HH:MM, ex.: 07:00", "required": True}],
            },
            {
                "type": SUB_COMMAND,
                "name": "agentes",
                "description": "Hora da liga dos agentes (dias úteis, antes da abertura europeia)",
                "options": [{"type": STRING, "name": "hora", "description": "HH:MM, ex.: 07:30", "required": True}],
            },
            {
                "type": SUB_COMMAND,
                "name": "ronda",
                "description": "Rondas dos agentes: decidem e negociam ao preço do momento (dias úteis)",
                "options": [{"type": STRING, "name": "horas", "description": "De-até/minutos, ex.: 08:05-20:55/10, ou horas separadas por vírgulas", "required": True}],
            },
            {
                "type": SUB_COMMAND,
                "name": "alertas",
                "description": "Horas da verificação de alertas (dias úteis)",
                "options": [{"type": STRING, "name": "horas", "description": "Separadas por vírgulas, ex.: 15:40,17:40,19:40", "required": True}],
            },
            {
                "type": SUB_COMMAND,
                "name": "acompanhamento",
                "description": "Hora do acompanhamento dos alertas (dias úteis, depois do fecho dos EUA)",
                "options": [{"type": STRING, "name": "hora", "description": "HH:MM, ex.: 22:30", "required": True}],
            },
            {
                "type": SUB_COMMAND,
                "name": "estado",
                "description": "Hora da verificação do estado do bot (dias úteis, no fim do dia)",
                "options": [{"type": STRING, "name": "hora", "description": "HH:MM, ex.: 23:00", "required": True}],
            },
            {
                "type": SUB_COMMAND,
                "name": "noticias",
                "description": "Horas da análise de notícias (dias úteis)",
                "options": [{"type": STRING, "name": "horas", "description": "Separadas por vírgulas, ex.: 12:00,16:00,20:30", "required": True}],
            },
            {
                "type": SUB_COMMAND,
                "name": "semanal",
                "description": "Dia e hora do resumo semanal",
                "options": [
                    {"type": STRING, "name": "dia", "description": "Dia da semana", "required": True,
                     "choices": [{"name": n, "value": v} for n, v in WEEKDAYS]},
                    {"type": STRING, "name": "hora", "description": "HH:MM, ex.: 10:00", "required": True},
                ],
            },
            {"type": SUB_COMMAND, "name": "repor", "description": "Voltar aos horários de origem"},
        ],
    },
    {
        "name": "agentes",
        "description": "Liga dos agentes simulados, ou o detalhe de um agente",
        "options": [{"type": STRING, "name": "nome", "description": "Nome do agente (vazio = liga completa)"}],
    },
    {
        "name": "estrategia",
        "description": "Como pensa cada agente: regras, parâmetros, pré-treino e resultados",
        "options": [{"type": STRING, "name": "nome", "description": "Nome do agente (vazio = lista de todos)"}],
    },
    {"name": "prontidao", "description": "Que agentes já merecem confiança para ideias com dinheiro real (5 critérios)"},
    {
        "name": "agente",
        "description": "Gerir os agentes: criar, dar dinheiro, desativar",
        "default_member_permissions": MANAGE_SERVER,
        "options": [
            {
                "type": SUB_COMMAND,
                "name": "criar",
                "description": "Criar um agente novo",
                "options": [
                    {"type": STRING, "name": "nome", "description": "Nome do agente", "required": True},
                    {"type": STRING, "name": "estrategia", "description": "Estratégia", "required": True,
                     "choices": [{"name": f"{k}: {label}"[:100], "value": k} for k, label in STRATEGY_CHOICES]},
                    {"type": NUMBER, "name": "capital", "description": "Dinheiro inicial em € (por defeito 1000)", "min_value": 50},
                    {"type": STRING, "name": "emoji", "description": "Emoji do agente, ex.: 🦊"},
                ],
            },
            {
                "type": SUB_COMMAND,
                "name": "depositar",
                "description": "Dar mais dinheiro a um agente",
                "options": [
                    {"type": STRING, "name": "nome", "description": "Nome do agente", "required": True},
                    {"type": NUMBER, "name": "valor", "description": "Valor em €", "required": True, "min_value": 1},
                ],
            },
            {
                "type": SUB_COMMAND,
                "name": "remover",
                "description": "Desativar um agente (o histórico fica guardado)",
                "options": [{"type": STRING, "name": "nome", "description": "Nome do agente", "required": True}],
            },
        ],
    },
    {
        "name": "seguir",
        "description": "A tua lista pessoal de ações: avisos por mensagem privada",
        "options": [
            {"type": SUB_COMMAND, "name": "adicionar", "description": "Seguir uma ação",
             "options": [{"type": STRING, "name": "ticker", "description": "Ex.: NVDA", "required": True}]},
            {"type": SUB_COMMAND, "name": "remover", "description": "Deixar de seguir uma ação",
             "options": [{"type": STRING, "name": "ticker", "description": "Ex.: NVDA", "required": True}]},
            {"type": SUB_COMMAND, "name": "lista", "description": "Ver a tua lista"},
        ],
    },
    {"name": "ajuda", "description": "Como usar este canal e o bot"},
]


def register() -> int:
    app_id = os.environ.get("DISCORD_APPLICATION_ID")
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not app_id or not token:
        print("Faltam os secrets DISCORD_APPLICATION_ID e/ou DISCORD_BOT_TOKEN.", file=sys.stderr)
        return 1
    resp = requests.put(
        f"https://discord.com/api/v10/applications/{app_id}/commands",
        headers={"Authorization": f"Bot {token}"},
        json=COMMANDS,
        timeout=30,
    )
    if resp.status_code >= 300:
        print(f"Discord respondeu {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
        return 1
    print(f"Registados {len(resp.json())} comandos: " + ", ".join("/" + c["name"] for c in resp.json()))
    return 0


# Channels of the 📈 FINANÇAS category: (key used by discord_post.route, name, topic).
CATEGORY = "📈 FINANÇAS"
CHANNELS = [
    ("mercado", "📊-mercado", "Dicas do dia, rankings de ações e ETFs e resumo semanal. Só o bot publica; comenta em tópicos ou no #billions."),
    ("alertas", "🚨-alertas-e-notícias", "Rompimentos com volume (vota 👍/👎) e notícias que importam. Só o bot publica."),
    ("agentes", "🤖-agentes", "Liga dos agentes simulados: resultados diários, relatório mensal e pré-treino. Só o bot publica."),
]
VIEW, SEND, EMBED, ATTACH, REACT, HISTORY = 1 << 10, 1 << 11, 1 << 14, 1 << 15, 1 << 6, 1 << 16
PUBLIC_THREADS, SEND_IN_THREADS, PIN = 1 << 35, 1 << 38, 1 << 51
BOT_ALLOW = VIEW | SEND | EMBED | ATTACH | REACT | HISTORY | PUBLIC_THREADS | SEND_IN_THREADS
MEMBERS_ALLOW = REACT | PUBLIC_THREADS | SEND_IN_THREADS


def discord_api(method: str, path: str, **kwargs) -> dict | list:
    resp = requests.request(method, f"https://discord.com/api/v10{path}", timeout=30,
                            headers={"Authorization": f"Bot {os.environ['DISCORD_BOT_TOKEN']}"}, **kwargs)
    if resp.status_code >= 300:
        raise RuntimeError(f"{method} {path}: {resp.status_code} {resp.text[:300]}")
    return resp.json() if resp.content else {}


def create_channels() -> int:
    """Create (or reuse) the 📈 FINANÇAS category and its read-only channels; move the webhook's channel in."""
    if not os.environ.get("DISCORD_BOT_TOKEN") or not os.environ.get("DISCORD_WEBHOOK_URL"):
        print("Faltam DISCORD_BOT_TOKEN e/ou DISCORD_WEBHOOK_URL.", file=sys.stderr)
        return 1
    hook = requests.get(os.environ["DISCORD_WEBHOOK_URL"], timeout=30).json()
    guild, chat = hook["guild_id"], hook["channel_id"]
    bot = discord_api("GET", "/users/@me")["id"]
    channels = discord_api("GET", f"/guilds/{guild}/channels")
    category = next((c for c in channels if c["type"] == 4 and c["name"].lower() == CATEGORY.lower()), None) \
        or discord_api("POST", f"/guilds/{guild}/channels", json={"name": CATEGORY, "type": 4})

    def find(name: str, key: str) -> dict | None:
        """An existing text channel with this name, or with the key word inside the category (renamed by Discord)."""
        return next((c for c in channels if c["type"] == 0 and (c["name"] == name or (
            c.get("parent_id") == category["id"] and key in c["name"]))), None)

    overwrites = [
        {"id": guild, "type": 0, "deny": str(SEND), "allow": str(MEMBERS_ALLOW)},  # @everyone: read, react, threads
        {"id": bot, "type": 1, "allow": str(BOT_ALLOW | PIN), "deny": "0"},
    ]
    ids = {}
    for key, name, topic in CHANNELS:
        channel = find(name, key)
        if channel is None:
            channel = discord_api("POST", f"/guilds/{guild}/channels", json={
                "name": name, "type": 0, "parent_id": category["id"], "topic": topic, "permission_overwrites": overwrites})
            print(f"::notice title=Canais::Criado #{name}")
        else:  # reapply parent, topic and permissions (they may have been synced with the category)
            try:
                discord_api("PATCH", f"/channels/{channel['id']}", json={
                    "parent_id": category["id"], "topic": topic, "permission_overwrites": overwrites})
                print(f"::notice title=Canais::Permissões repostas em #{channel['name']}")
            except RuntimeError as e:
                print(f"::warning title=Canais::Não consegui atualizar #{channel['name']}: {e}")
        ids[key] = channel["id"]
    try:
        discord_api("PATCH", f"/channels/{chat}", json={"parent_id": category["id"], "lock_permissions": False})
        print("::notice title=Canais::Canal de conversa movido para a categoria")
    except RuntimeError as e:  # e.g. the bot cannot see that channel; moving it by hand works too
        print(f"::warning title=Canais::Não consegui mover o canal de conversa ({e}); arrasta-o para a categoria à mão.")

    # Keep extra channels (🧪-testes, 🔧-estado) of the same server; a new server starts without them.
    saved = json.loads(CHANNELS_PATH.read_text()) if CHANNELS_PATH.exists() else {}
    extra = saved.get("channels", {}) if saved.get("guild_id") == guild else {}
    CHANNELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHANNELS_PATH.write_text(json.dumps({"guild_id": guild, "category_id": category["id"],
                                         "channels": {**extra, **ids, "discussao": chat}}, indent=2) + "\n")
    failed = 0
    for key, name, topic in CHANNELS:
        try:
            message = discord_api("POST", f"/channels/{ids[key]}/messages",
                                  json={"embeds": [embed(f"Bem-vindo a #{name}", topic, color=BLUE, footer="")]})
        except RuntimeError as e:  # the bot must be able to post here, or that kind of post would be lost
            print(f"::error title=Canais::O bot não consegue publicar em #{name}: {e}")
            failed += 1
            continue
        print(f"::notice title=Canais::O bot publica em #{name}")
        try:
            discord_api("PUT", f"/channels/{ids[key]}/messages/pins/{message['id']}")
        except RuntimeError as e:
            print(f"::warning title=Canais::Não consegui fixar a mensagem em #{name}: {e}")
    if failed:  # keep posting to the webhook channel until every channel works
        CHANNELS_PATH.unlink()
        return 1
    return 0


TEST_CHANNEL = ("testes", "🧪-testes", "Testes de funcionalidades novas antes de irem para os outros canais. Só o bot publica.")
STATUS_CHANNEL = ("estado", "🔧-estado", "Verificação diária do bot: as tarefas do dia correram e os dados estão em dia? Só o bot publica.")


UPDATES_CATEGORY = "📢 UPDATES"  # old home of 📢-novidades, removed once empty
UPDATES_CHANNEL = ("updates", "📢-novidades", "Novidades e correções da plataforma One Guillion Jollars (portal, carteira, agentes e bot), "
                   "resumidas sempre que há uma atualização. Só o bot publica; comenta em tópicos.")


PORTAL_CHANNEL = ("portal", "🏟️-portal", "A entrada: abre o portal, pede a tua carteira e vê onde aparece cada coisa. Só o bot publica.")
BANNER = REPO / "discord" / "portal-banner.png"  # drawn from discord/banner/portal-banner.html
PINK = 0xFF2E88


def round_text(spec) -> str:
    """The agents' rounds for people: "das 08:05 às 20:55, de 10 em 10 min" or the list of times."""
    if isinstance(spec, list):
        return "às " + " · ".join(spec)
    try:
        span, every = str(spec).split("/")
        start, end = span.split("-")
        every = max(int(every), 5)
        return f"das {start} às {end}, de {every} em {every} min"
    except ValueError:
        return str(spec or "")


def portal_card(url: str | None, sched: dict, channels: dict[str, str]) -> dict:
    """The pinned portal message; `channels` fills in the channel mentions."""
    from limits import DAY_NAMES

    ch = lambda key: f"<#{channels[key]}>" if channels.get(key) else f"#{key}"  # noqa: E731
    alerts, news = " · ".join(sched["alertas"]), " · ".join(sched["noticias"])
    weekly = sched["semanal"]
    return embed(
        "🏟️ One Guillion Jollars",
        "**O portal é o centro**: tudo o que o bot sabe, numa página.\n"
        "🏁 corrida e fichas completas dos 16 agentes · 📰 notícias que importam · 🎯 oportunidades de compra e venda com "
        "**score de segurança** explicado · ⏱️ contagem para cada atualização · 💼 a tua carteira pessoal\n\n"
        "**O Discord é o hub**: aqui chegam os resumos às horas marcadas e aqui se discute.",
        color=PINK,
        image="portal-banner.png",
        fields=[
            ("📍 Onde aparece cada coisa (hora de Lisboa, dias úteis)",
             f"📊 {ch('mercado')} · dicas do dia e rankings às **{sched['dicas']}**, resumo semanal ao "
             f"{DAY_NAMES.get(weekly['dia'], weekly['dia'])} às **{weekly['hora']}**\n"
             f"🚨 {ch('alertas')} · notícias às **{news}**, alertas às **{alerts}**, acompanhamento às **{sched['acompanhamento']}**\n"
             f"🤖 {ch('agentes')} · liga dos agentes às **{sched['agentes']}**, rondas **{round_text(sched.get('ronda'))}**\n"
             f"🔧 {ch('estado')} · estado do bot às **{sched['estado']}**\n"
             f"💬 {ch('discussao')} · conversa livre"),
            ("🎮 Começar",
             "1. **Abrir o portal** (botão em baixo): guarda-o nos favoritos.\n"
             "2. **A minha carteira**: recebes o teu link privado para registar compras e vendas.\n"
             "3. Escreve `/` em qualquer canal para ver os comandos (`/acao`, `/agentes`, `/limites`…)."),
        ],
        footer="Ferramenta educativa com dinheiro fictício nos agentes. Nada aqui é recomendação de compra.",
    ) if url else embed("🏟️ One Guillion Jollars", "O portal ainda não está ligado (faltam as credenciais da Cloudflare).", color=PINK)


def create_portal() -> int:
    """Create (or reuse) 🏟️-portal at the top of the category and pin a fresh portal message there."""
    import interactive
    from limits import schedule

    if not os.environ.get("DISCORD_BOT_TOKEN") or not CHANNELS_PATH.exists():
        print("Faltam DISCORD_BOT_TOKEN e/ou discord/channels.json (corre --canais primeiro).", file=sys.stderr)
        return 1
    status = create_test_channel(PORTAL_CHANNEL)
    if status:
        return status
    saved = json.loads(CHANNELS_PATH.read_text())
    guild, category, channel = saved["guild_id"], saved["category_id"], saved["channels"]["portal"]
    # top of the category: the portal first, the others in their current order
    inside = sorted((c for c in discord_api("GET", f"/guilds/{guild}/channels") if c.get("parent_id") == category),
                    key=lambda c: c["position"])
    order = [channel] + [c["id"] for c in inside if c["id"] != channel]
    discord_api("PATCH", f"/guilds/{guild}/channels", json=[{"id": cid, "position": i} for i, cid in enumerate(order)])
    # replace the bot's earlier messages there (the welcome note included)
    bot = discord_api("GET", "/users/@me")["id"]
    for m in discord_api("GET", f"/channels/{channel}/messages", params={"limit": 50}):
        if m["author"]["id"] == bot:
            discord_api("DELETE", f"/channels/{channel}/messages/{m['id']}")
    url = interactive.portal_url()
    card = portal_card(url, schedule(), saved["channels"])
    buttons = [{"type": 2, "style": 2, "label": "💼 A minha carteira", "custom_id": "carteira"},
               {"type": 2, "style": 2, "label": "❓ Como funciona", "custom_id": "ajuda"}]
    if url:
        buttons.insert(0, {"type": 2, "style": 5, "label": "🏟️ Abrir o portal", "url": url})
    message = post_message([card], {"portal-banner.png": BANNER.read_bytes()} if BANNER.exists() else {},
                           url=f"https://discord.com/api/v10/channels/{channel}/messages",
                           headers={"Authorization": f"Bot {os.environ['DISCORD_BOT_TOKEN']}"},
                           components=[{"type": 1, "components": buttons}])
    if not message:
        print("::error title=Portal::Não consegui publicar a mensagem do portal.")
        return 1
    try:
        discord_api("PUT", f"/channels/{channel}/messages/pins/{message['id']}")
    except RuntimeError as e:
        print(f"::warning title=Portal::Não consegui afixar a mensagem: {e}")
    print(f"::notice title=Portal::Mensagem do portal publicada e afixada em #{PORTAL_CHANNEL[1]}")
    return 0


def remove_empty_category(name: str) -> int:
    """Delete a category of this server that no channel uses any more."""
    guild = json.loads(CHANNELS_PATH.read_text())["guild_id"]
    channels = discord_api("GET", f"/guilds/{guild}/channels")
    for cat in [c for c in channels if c["type"] == 4 and c["name"].lower() == name.lower()]:
        if not any(c.get("parent_id") == cat["id"] for c in channels):
            discord_api("DELETE", f"/channels/{cat['id']}")
            print(f"::notice title=Canais::Categoria {name} apagada (vazia)")
    return 0


def create_test_channel(spec: tuple[str, str, str] = TEST_CHANNEL, category_name: str | None = None) -> int:
    """Create (or reuse) one extra read-only channel (🧪-testes by default) in the category and add it to discord/channels.json.

    Leaves the other channels alone; needs the category from --canais first.
    """
    if not os.environ.get("DISCORD_BOT_TOKEN") or not CHANNELS_PATH.exists():
        print("Faltam DISCORD_BOT_TOKEN e/ou discord/channels.json (corre --canais primeiro).", file=sys.stderr)
        return 1
    saved = json.loads(CHANNELS_PATH.read_text())
    guild, category = saved["guild_id"], saved["category_id"]
    key, name, topic = spec
    bot = discord_api("GET", "/users/@me")["id"]
    if category_name:  # a category of its own (📢 UPDATES), created if missing
        found = next((c for c in discord_api("GET", f"/guilds/{guild}/channels") if c["type"] == 4 and c["name"].lower() == category_name.lower()), None)
        category = (found or discord_api("POST", f"/guilds/{guild}/channels", json={"name": category_name, "type": 4}))["id"]
    channel = next((c for c in discord_api("GET", f"/guilds/{guild}/channels") if c["type"] == 0 and c["name"] == name), None)
    if channel is None:
        channel = discord_api("POST", f"/guilds/{guild}/channels", json={
            "name": name, "type": 0, "parent_id": category, "topic": topic, "permission_overwrites": [
                {"id": guild, "type": 0, "deny": str(SEND), "allow": str(MEMBERS_ALLOW)},
                {"id": bot, "type": 1, "allow": str(BOT_ALLOW | PIN), "deny": "0"},
            ]})
        print(f"::notice title=Canais::Criado #{name}")
        discord_api("POST", f"/channels/{channel['id']}/messages",
                    json={"embeds": [embed(f"Bem-vindo a #{name}", topic, color=BLUE, footer="")]})
    elif channel.get("parent_id") != category:  # moved to this category (e.g. 📢-novidades into 📈 FINANÇAS)
        discord_api("PATCH", f"/channels/{channel['id']}", json={"parent_id": category, "lock_permissions": False})
        print(f"::notice title=Canais::#{name} movido para a categoria")
    saved["channels"][key] = channel["id"]
    CHANNELS_PATH.write_text(json.dumps(saved, indent=2) + "\n")
    return 0


MIRROR_CHAT = ("discussao", "billions", "Conversa livre. As análises do bot aparecem nos canais ao lado.")


def create_mirror_channels(guild: str) -> int:
    """Create (or reuse) the same channels in a second server and write them to discord/mirror.json.

    discord_post copies every bot post into the channel of the same kind there.
    The bot must already be in that server (OAuth invite with Manage Channels and Manage Roles).
    """
    if not os.environ.get("DISCORD_BOT_TOKEN"):
        print("Falta DISCORD_BOT_TOKEN.", file=sys.stderr)
        return 1
    bot = discord_api("GET", "/users/@me")["id"]
    channels = discord_api("GET", f"/guilds/{guild}/channels")
    category = next((c for c in channels if c["type"] == 4 and c["name"].lower() == CATEGORY.lower()), None) \
        or discord_api("POST", f"/guilds/{guild}/channels", json={"name": CATEGORY, "type": 4})
    read_only = [
        {"id": guild, "type": 0, "deny": str(SEND), "allow": str(MEMBERS_ALLOW)},
        {"id": bot, "type": 1, "allow": str(BOT_ALLOW | PIN), "deny": "0"},
    ]
    ids = {}
    for key, name, topic in [MIRROR_CHAT, *CHANNELS, TEST_CHANNEL]:
        channel = next((c for c in channels if c["type"] == 0 and c["name"] == name), None)
        if channel is None:
            body = {"name": name, "type": 0, "parent_id": category["id"], "topic": topic}
            if key != MIRROR_CHAT[0]:
                body["permission_overwrites"] = read_only
            channel = discord_api("POST", f"/guilds/{guild}/channels", json=body)
            print(f"::notice title=Espelho::Criado #{name}")
        ids[key] = channel["id"]

    failed = 0
    for key, name, topic in [*CHANNELS, TEST_CHANNEL]:
        try:  # the bot must be able to post in every copy, or that kind of post would only reach one server
            discord_api("POST", f"/channels/{ids[key]}/messages",
                        json={"embeds": [embed(f"Bem-vindo a #{name}", topic, color=BLUE, footer="")]})
        except RuntimeError as e:
            print(f"::error title=Espelho::O bot não consegue publicar em #{name}: {e}")
            failed += 1
    if failed:
        return 1
    MIRROR_PATH.parent.mkdir(parents=True, exist_ok=True)
    MIRROR_PATH.write_text(json.dumps({"guild_id": guild, "category_id": category["id"], "channels": ids}, indent=2) + "\n")
    return 0


def diagnose() -> int:
    """Annotate the bot's server-wide permissions, to explain Missing Access errors."""
    hook = requests.get(os.environ["DISCORD_WEBHOOK_URL"], timeout=30).json()
    guild = hook["guild_id"]
    bot = discord_api("GET", "/users/@me")["id"]
    server = discord_api("GET", f"/guilds/{guild}")["name"]
    chat = discord_api("GET", f"/channels/{hook['channel_id']}")["name"]
    print(f"::notice title=Diagnóstico::O webhook publica em {server} → #{chat}")
    member = discord_api("GET", f"/guilds/{guild}/members/{bot}")
    roles = {r["id"]: r for r in discord_api("GET", f"/guilds/{guild}/roles")}
    names = {VIEW: "Ver canais", SEND: "Enviar mensagens", 1 << 4: "Gerenciar canais", 1 << 28: "Gerenciar cargos",
             1 << 3: "Administrador", EMBED: "Inserir links", ATTACH: "Anexar arquivos", HISTORY: "Ver histórico"}
    mine = [roles[guild]] + [roles[r] for r in member["roles"] if r in roles]
    perms = 0
    for r in mine:
        perms |= int(r["permissions"])
    print(f"::notice title=Diagnóstico::Cargos do bot: {', '.join(r['name'] for r in mine)}")
    print("::notice title=Diagnóstico::Permissões efetivas: "
          + ", ".join(f"{label} {'✅' if perms & bit else '❌'}" for bit, label in names.items()))
    if CHANNELS_PATH.exists():
        for key, channel in json.loads(CHANNELS_PATH.read_text())["channels"].items():
            try:
                name = discord_api("GET", f"/channels/{channel}")["name"]
                print(f"::notice title=Diagnóstico::#{name} ({key}): o bot tem acesso ✅")
            except RuntimeError as e:
                print(f"::warning title=Diagnóstico::canal {key} ({channel}): sem acesso ❌ {e}")
    return 0


def welcome_card() -> dict:
    return embed(
        "👋 Bem-vindo! Este canal recebe análise de mercado automática",
        "Tudo aqui é gerado por scripts a partir de dados públicos, sem ninguém a escolher ações à mão.",
        color=BLUE,
        fields=[
            (
                "📅 O que aparece aqui (hora de Lisboa)",
                "**Dias úteis, 07:00** · 📊 Dicas do dia: saúde do mercado (breadth), exposição sugerida, setores e setups VCP\n"
                "**Dias úteis, 07:00** · 🏆 Top 3 ações por setor e 📦 top 10 ETFs UCITS, com valorização do dia, semana, 3 e 6 meses\n"
                "**Dias úteis, 12:00 · 16:00 · 20:30** · 📰 Notícias que importam, lidas pelo Claude\n"
                "**Dias úteis, 07:30** · 🤖 Liga dos agentes: estratégias com dinheiro fictício e preços reais, ações dos EUA e da Europa\n"
                "**Dias úteis, das 08:05 às 20:55, de 10 em 10 minutos** · ⏱️ Rondas dos agentes: reagem ao mercado ao preço do momento\n"
                "**Dias úteis, 15:40 · 17:40 · 19:40** · 🚨 Alertas, só quando uma ação da lista rompe o pivot com volume\n"
                "**Dias úteis, 22:30** · 🔁 Como correu cada alerta 5 e 20 sessões depois, em resposta ao alerta original\n"
                "**Sábado, 10:00** · 🗓️ Resumo semanal: índices, setores, maiores subidas e quedas, e como correram os alertas\n"
                "Os administradores podem mudar estas horas com `/horario`.",
            ),
            (
                "🤖 Comandos (escreve / na caixa de mensagem)",
                "`/acao NVDA` · tendência, qualidade, pivot, stop e gráfico de uma ação dos EUA\n"
                "`/etf VWCE` · tendência e gráfico de um ETF UCITS\n"
                "`/mercado` · leitura do mercado agora\n"
                "`/limites` · estado das notícias (Claude) e das fontes de dados, e limites gastos\n"
                "`/posicao capital entrada stop` · quantas ações comprar (ex.: 10000, 35.84, 34.14)\n"                "`/agentes` · liga dos agentes, ou `/agentes nome` para ver um agente\n"
                "`/estrategia nome` · como pensa cada agente (regras, parâmetros, pré-treino)\n"
                "`/seguir adicionar NVDA` · lista pessoal com avisos por mensagem privada\n"
                "`/portal` · corrida dos agentes, notícias, oportunidades com score de segurança e agenda\n"
                "`/carteira` · a tua carteira pessoal (compras, vendas e desempenho), com link privado\n"
                "`/ajuda` · resumo destes comandos (só tu vês)\n"
                "`/acao`, `/etf`, `/mercado` e `/limites` demoram 1-2 minutos: o bot mostra \"a pensar…\" e depois responde.",
            ),
            (
                "📏 Como ler os números",
                "**Score 0-100**: 60% tendência (preço acima das médias de 50 e 200 dias, força relativa a 3-6 meses) "
                "+ 40% qualidade (margem, ROE, crescimento, dívida). Nos ETFs é só tendência.\n"
                "**Pivot**: máximo recente que a ação tem de passar. **Stop**: onde a ideia falha.\n"
                "**Zona do mercado**: 🟢 a favor, 🟡 neutra (reduzir tamanho), 🟠/🔴 defensiva.",
            ),
        ],
        footer="Ferramenta educativa. Nada aqui é recomendação de compra; as decisões e o risco são teus.",
    )


def delete_channels(ids: list[str]) -> int:
    """Delete channels by id, categories last; never one of the current server (discord/channels.json)."""
    current = json.loads(CHANNELS_PATH.read_text()).get("guild_id") if CHANNELS_PATH.exists() else None
    found = []
    for channel_id in ids:
        channel = discord_api("GET", f"/channels/{channel_id}")
        if channel.get("guild_id") == current:
            print(f"::error title=Apagar canais::#{channel['name']} é do servidor atual; não apago nada.")
            return 1
        found.append(channel)
    failed = 0
    for channel in sorted(found, key=lambda c: c["type"] == 4):  # channels before their category
        try:
            discord_api("DELETE", f"/channels/{channel['id']}")
            print(f"::notice title=Apagar canais::Apagado #{channel['name']}")
        except RuntimeError as e:
            print(f"::error title=Apagar canais::#{channel['name']}: {e}")
            failed += 1
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--register", action="store_true", help="Register the slash commands")
    parser.add_argument("--welcome", action="store_true", help="Post the welcome card")
    parser.add_argument("--canais", action="store_true", help="Create the 📈 FINANÇAS category and channels")
    parser.add_argument("--canal-testes", action="store_true", help="Create the 🧪-testes channel (for DISCORD_TESTES)")
    parser.add_argument("--canal-estado", action="store_true", help="Create the 🔧-estado channel (health check)")
    parser.add_argument("--canal-updates", action="store_true", help="Create (or move) the 📢-novidades channel in the 📈 FINANÇAS category")
    parser.add_argument("--portal", action="store_true", help="Create 🏟️-portal and pin the portal message with its banner")
    parser.add_argument("--apagar-canais", metavar="IDS", help="Delete these channels (comma-separated ids)")
    parser.add_argument("--espelho", metavar="GUILD_ID", help="Create the channels in a second server and copy posts there")
    parser.add_argument("--diagnostico", action="store_true", help="Show the bot's server-wide permissions")
    parser.add_argument("--dry-run", action="store_true", help="Print the welcome card instead of posting it")
    args = parser.parse_args()

    status = register() if args.register else 0
    if args.diagnostico:
        try:
            status = diagnose()
        except (RuntimeError, KeyError, requests.RequestException) as e:
            print(f"::error title=Diagnóstico::{e}")
            status = 1
    if args.canais:
        try:
            status = status or create_channels()
        except (RuntimeError, KeyError, requests.RequestException) as e:
            # An annotation shows on the run page even when the job log is not reachable.
            print(f"::error title=Criar canais::{e}")
            status = 1
    if args.canal_testes:
        try:
            status = status or create_test_channel()
        except (RuntimeError, KeyError, requests.RequestException) as e:
            print(f"::error title=Canal de testes::{e}")
            status = 1
    if args.portal:
        try:
            status = status or create_portal()
        except (RuntimeError, KeyError, requests.RequestException) as e:
            print(f"::error title=Portal::{e}")
            status = 1
    if args.canal_estado:
        try:
            status = status or create_test_channel(STATUS_CHANNEL)
        except (RuntimeError, KeyError, requests.RequestException) as e:
            print(f"::error title=Canal de estado::{e}")
            status = 1
    if args.canal_updates:
        try:
            status = status or create_test_channel(UPDATES_CHANNEL) or remove_empty_category(UPDATES_CATEGORY)
        except (RuntimeError, KeyError, requests.RequestException) as e:
            print(f"::error title=Canal de updates::{e}")
            status = 1
    if args.apagar_canais:
        try:
            status = status or delete_channels([i.strip() for i in args.apagar_canais.split(",") if i.strip()])
        except (RuntimeError, KeyError, requests.RequestException) as e:
            print(f"::error title=Apagar canais::{e}")
            status = 1
    if args.espelho:
        try:
            status = status or create_mirror_channels(args.espelho)
        except (RuntimeError, KeyError, requests.RequestException) as e:
            print(f"::error title=Espelho::{e} (o bot já foi adicionado a esse servidor?)")
            status = 1
    if args.welcome:
        status = status or send([welcome_card()], dry_run=args.dry_run)
    return status


if __name__ == "__main__":
    sys.exit(main())
