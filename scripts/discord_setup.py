"""One-off Discord setup: register the bot's slash commands and post the welcome card.

    --register  PUT the commands below (needs DISCORD_APPLICATION_ID and DISCORD_BOT_TOKEN)
    --welcome   post the welcome card to DISCORD_WEBHOOK_URL, to pin in the channel

Run from the "Configurar o bot do Discord" workflow. Keep the command list in
step with discord-bot/worker.js.
"""

from __future__ import annotations

import argparse
import os
import sys

import requests

from discord_post import BLUE, embed, send

SUB_COMMAND, STRING, NUMBER = 1, 3, 10
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
                "options": [{"type": STRING, "name": "hora", "description": "HH:MM, ex.: 11:40", "required": True}],
            },
            {
                "type": SUB_COMMAND,
                "name": "agentes",
                "description": "Hora da liga dos agentes (dias úteis, depois das dicas)",
                "options": [{"type": STRING, "name": "hora", "description": "HH:MM, ex.: 12:15", "required": True}],
            },
            {
                "type": SUB_COMMAND,
                "name": "alertas",
                "description": "Horas da verificação de alertas (dias úteis)",
                "options": [{"type": STRING, "name": "horas", "description": "Separadas por vírgulas, ex.: 15:40,17:40,19:40", "required": True}],
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


def welcome_card() -> dict:
    return embed(
        "👋 Bem-vindo! Este canal recebe análise de mercado automática",
        "Tudo aqui é gerado por scripts a partir de dados públicos, sem ninguém a escolher ações à mão.",
        color=BLUE,
        fields=[
            (
                "📅 O que aparece aqui (hora de Lisboa)",
                "**Dias úteis, 11:40** · 📊 Dicas do dia: saúde do mercado (breadth), exposição sugerida, setores e setups VCP\n"
                "**Dias úteis, 11:40** · 🏆 Top 3 ações por setor e 📦 top 10 ETFs UCITS, com valorização do dia, semana, 3 e 6 meses\n"
                "**Dias úteis, 12:15** · 🤖 Liga dos agentes: 10 estratégias com dinheiro fictício e preços reais\n"
                "**Dias úteis, 15:40 · 17:40 · 19:40** · 🚨 Alertas, só quando uma ação da lista rompe o pivot com volume\n"
                "**Sábado, 10:00** · 🗓️ Resumo semanal: índices, setores, maiores subidas e quedas, e como correram os alertas\n"
                "Os administradores podem mudar estas horas com `/horario`.",
            ),
            (
                "🤖 Comandos (escreve / na caixa de mensagem)",
                "`/acao NVDA` · tendência, qualidade, pivot, stop e gráfico de uma ação dos EUA\n"
                "`/etf VWCE` · tendência e gráfico de um ETF UCITS\n"
                "`/mercado` · leitura do mercado agora\n"
                "`/posicao capital entrada stop` · quantas ações comprar (ex.: 10000, 35.84, 34.14)\n"                "`/agentes` · liga dos agentes, ou `/agentes nome` para ver um agente\n"
                "`/seguir adicionar NVDA` · lista pessoal com avisos por mensagem privada\n"
                "`/ajuda` · resumo destes comandos (só tu vês)\n"
                "`/acao`, `/etf` e `/mercado` demoram 1-2 minutos: o bot mostra \"a pensar…\" e depois responde.",
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--register", action="store_true", help="Register the slash commands")
    parser.add_argument("--welcome", action="store_true", help="Post the welcome card")
    parser.add_argument("--dry-run", action="store_true", help="Print the welcome card instead of posting it")
    args = parser.parse_args()

    status = register() if args.register else 0
    if args.welcome:
        status = status or send([welcome_card()], dry_run=args.dry_run)
    return status


if __name__ == "__main__":
    sys.exit(main())
