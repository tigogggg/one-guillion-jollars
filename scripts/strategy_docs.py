"""Plain-Portuguese explanations of each agent strategy and parameter, for /estrategia.

Texts use {param} placeholders filled with the agent's current parameters, so the
explanation always matches what the agent actually does.
"""

from __future__ import annotations

PARAMS = {
    "n": "número máximo de posições",
    "cada": "de quantas em quantas sessões revê a carteira",
    "stop_pct": "stop-loss: vende se cair esta % abaixo da entrada",
    "risco_pct": "% do património que aceita perder por operação (até ao stop)",
    "alvo_r": "alvo de lucro em múltiplos do risco (R)",
    "max_pos": "peso máximo de uma posição no património",
    "qualidade_min": "score de qualidade mínimo (0-100)",
    "aperto_pct": "amplitude máxima dos últimos 10 dias (contração)",
    "setores": "número de setores mais fortes que segue",
    "por_setor": "ações escolhidas em cada setor",
    "queda_pct": "queda semanal mínima para comprar",
    "alvo_pct": "lucro com que vende",
    "max_dias": "número máximo de sessões que mantém uma posição",
    "max_exposicao": "exposição máxima ao ETF mundial",
    "folga": "desvio de peso tolerado antes de reajustar",
    "salto_pct": "salto mínimo na abertura (face ao fecho anterior)",
    "limiar": "força mínima das notícias (soma de sentimento × importância)",
    "min_votos": "número mínimo de agentes que têm de concordar",
    "lideres": "quantos agentes líderes copia",
    "janela": "sessões de resultados recentes, usadas se ainda não houver registo na zona",
    "ignora_resultados": "não evita resultados trimestrais próximos",
    "ignora_risco": "não aplica as regras de risco comuns",
    "ignora_noticias": "não evita ações com notícias negativas",
    "stop_atr": "stop inicial a este múltiplo da variação diária média (ATR) abaixo da compra (0 = desligado)",
    "trail_atr": "stop que sobe com o preço: fecho menos este múltiplo do ATR (0 = desligado)",
    "tempo_max": "vende posições a perder que não andaram ao fim destas sessões (0 = desligado)",
    "min_tecnico": "score técnico mínimo para comprar: tendência, volatilidade e distância ao máximo, 0-60 (0 = desligado)",
    "max_ext": "não compra se o preço estiver mais do que isto acima da média de 50 dias (0 = desligado)",
    "gap_max": "cancela a compra se a ação abrir mais do que isto acima ou abaixo do fecho em que decidiu (0 = desligado)",
    "peso_noticias": "não compra se o score das notícias estiver neste valor negativo ou pior",
    "vix_max": "não compra quando o VIX (medo no mercado) está acima deste valor (0 = desligado)",
    "vix_meio": "compra metade do tamanho quando o VIX está acima deste valor (0 = desligado)",
    "epu_max": "não compra quando a incerteza política está acima deste percentil dos últimos 3 anos (0 = desligado)",
    "credito_max": "não compra quando o crédito de alto risco caiu mais do que isto face ao Tesouro dos EUA num ano (stress financeiro; 0 = desligado)",
    "juros_max": "não compra quando o juro a 10 anos subiu mais do que isto (p.p.) em 3 meses (0 = desligado)",
    "usd_max": "não compra ações dos EUA quando o dólar caiu mais do que isto face ao euro em 3 meses (0 = desligado)",
    "filtro_spy": "não compra quando o S&P 500 está abaixo da média de 200 dias (1 = ligado)",
    "dias_resultados": "não compra ações com resultados trimestrais nos próximos N dias",
    "evita_macro": "não compra na véspera de eventos macro (Fed, inflação, emprego) (1 = ligado)",
    "gpr_max": "não compra quando o risco geopolítico (guerras e tensões nas notícias) está acima deste percentil dos últimos 3 anos (0 = desligado)",
    "petroleo_max": "não compra quando o petróleo subiu mais do que isto em 3 meses (choque petrolífero; 0 = desligado)",
    "ouro_max": "não compra quando o ouro subiu mais do que isto em 3 meses (fuga para ativos de refúgio; 0 = desligado)",
    "filtro_setor": "só compra se o setor da ação estiver acima da média de 200 dias (1) e também mais forte do que o S&P 500 em 3 meses (2) (0 = desligado)",
    "ema_filtro": "só compra com o preço acima das médias exponenciais de 21 e 50 dias, e a de 21 acima da de 50 (1 = ligado)",
    "stop_suporte": "põe o stop abaixo do último suporte (mínimo confirmado) menos este múltiplo do ATR, se estiver a menos de 15% (0 = desligado)",
    "qualidade_fund_min": "só compra empresas com qualidade dos fundamentos (margens, crescimento, cash flow, liquidez, dívida) de pelo menos isto, 0-100 (0 = desligado)",
    "valor_fund_min": "só compra empresas com score de valor (P/E, cash flow livre, margem de segurança) de pelo menos isto, 0-100 (0 = desligado)",
    "evita_geo": "não compra quando o Claude classifica o risco geopolítico do dia como alto (1 = ligado)",
    "valor_min": "score de valor mínimo (0-100): rendimento dos lucros, do cash flow livre e margem de segurança",
    "peso_qualidade": "peso da qualidade dos fundamentos na ordenação das ações",
    "peso_valor": "peso do valor (preço face aos lucros, cash flow e valor intrínseco) na ordenação",
    "peso_tendencia": "peso da tendência do preço na ordenação",
    "peso_momentum": "peso do momentum (subida a 3 e 6 meses face às outras ações)",
    "peso_baixa_vol": "peso da baixa volatilidade (ações calmas sobem a ordenação)",
    "so_tendencia": "só considera ações em tendência de alta (Stage 2) (1 = sim)",
    "queda_min": "queda mínima face ao máximo de 52 semanas para considerar a compra",
    "limiar_mom": "momentum mínimo do melhor ETF de ações para ficar investido em ações",
}

PCT_PARAMS = {"stop_pct", "risco_pct", "max_pos", "aperto_pct", "queda_pct", "alvo_pct", "max_exposicao", "folga", "salto_pct", "max_ext", "gap_max",
              "epu_max", "usd_max", "credito_max", "gpr_max", "petroleo_max", "ouro_max", "queda_min", "limiar_mom"}

# strategy: (idea, buys when, sells when, position size)
DOCS = {
    "indexador": (
        "A referência da liga: comprar o S&P 500 e não fazer mais nada. Quem não o bate, não acrescenta valor.",
        "Investe todo o dinheiro disponível no ETF SXR8 (S&P 500 em euros).",
        "Nunca vende, por isso nunca paga imposto sobre mais-valias.",
        "Tudo num só ETF.",
    ),
    "guardiao": (
        "Conservador: só o ETF mundial VWCE, com mais ou menos peso conforme o estado do mercado.",
        "Sobe o peso do VWCE até {max_exposicao} do património × exposição da zona (100% em alta, 70% neutra, 40% cautelosa, 10% em queda), revisto semanalmente.",
        "Reduz o peso quando a zona do mercado piora e o desvio passa de {folga}.",
        "Um só ETF; o resto fica em dinheiro.",
    ),
    "rotador": (
        "Moderado: roda entre os ETFs UCITS com melhor tendência.",
        "A cada {cada} sessões compra os {n} ETFs com melhor score de tendência (acima da média de 200 dias).",
        "Vende os que deixam de estar no topo (com uma posição de folga).",
        "Partes iguais, ajustadas à zona do mercado.",
    ),
    "foguetao": (
        "Momentum: as ações do S&P 500 com a tendência mais forte.",
        "A cada {cada} sessões compra as {n} ações com maior score de tendência em Stage 2 (preço > MM50 > MM200, MM200 a subir).",
        "Vende as que saem do top {n}×2, ou no stop de {stop_pct}.",
        "Partes iguais, ajustadas à zona do mercado.",
    ),
    "joalheiro": (
        "Qualidade: as melhores do ranking diário com lucros, margens e crescimento sólidos.",
        "A cada {cada} sessões compra as {n} melhores do ranking com qualidade ≥ {qualidade_min}.",
        "Vende as que saem da lista, ou no stop de {stop_pct}.",
        "Partes iguais, ajustadas à zona do mercado.",
    ),
    "analista": (
        "Fundamentos: empresas com bons números (margens, crescimento, cash flow, pouca dívida) a preço razoável e em tendência de alta.",
        "A cada {cada} sessões compra as {n} ações em Stage 2 com qualidade ≥ {qualidade_min} e valor ≥ {valor_min} (relatórios da SEC já publicados nesse dia), ordenadas por {peso_qualidade}×qualidade + {peso_valor}×valor + {peso_tendencia}×tendência.",
        "Vende as que saem do top {n}×2, ou no stop de {stop_pct}.",
        "Partes iguais, ajustadas à zona do mercado.",
    ),
    "quant": (
        "Multi-fator: junta cinco fatores conhecidos (momentum, tendência, qualidade, valor e baixa volatilidade) e o treino descobre quanto vale cada um.",
        "A cada {cada} sessões ordena as ações do S&P 500 por {peso_momentum}×momentum + {peso_tendencia}×tendência + {peso_qualidade}×qualidade + {peso_valor}×valor + {peso_baixa_vol}×baixa volatilidade (cada fator em percentil) e compra as {n} primeiras.",
        "Vende as que saem do top {n}×2, ou no stop de {stop_pct}.",
        "Partes iguais, ajustadas à zona do mercado.",
    ),
    "monge": (
        "Calma: as ações menos voláteis do índice tendem a dar retornos parecidos com menos sustos (anomalia da baixa volatilidade).",
        "A cada {cada} sessões compra as {n} ações com menor volatilidade acima da média de 200 dias, com qualidade dos fundamentos ≥ {qualidade_min} quando conhecida.",
        "Vende as que deixam de estar entre as {n}×3 mais calmas, ou no stop de {stop_pct}.",
        "Partes iguais, ajustadas à zona do mercado.",
    ),
    "abutre": (
        "Contrário com rede: compra empresas sólidas e baratas que caíram muito e começam a recuperar.",
        "Compra ações pelo menos {queda_min} abaixo do máximo de 52 semanas, já acima da média exponencial de 21 dias, com qualidade ≥ {qualidade_min} e valor ≥ {valor_min}.",
        "Vende quando volta a 5% do máximo, no stop de {stop_pct} ou ao fim de {max_dias} sessões.",
        "Partes iguais, até {n} posições.",
    ),
    "pendulo": (
        "Momentum duplo: está em ações enquanto sobem e muda para obrigações ou ouro quando deixam de subir.",
        "A cada {cada} sessões, se o melhor ETF de ações tiver momentum acima de {limiar_mom}, compra os {n} ETFs de ações mais fortes; senão o mais forte entre obrigações e ouro.",
        "Vende o que deixa de ser escolhido na revisão.",
        "Todo o dinheiro, em partes iguais.",
    ),
    "cacador": (
        "Rompimentos: ações que passam o máximo das últimas 20 sessões com volume forte.",
        "Compra quando o preço fecha até 3% acima do máximo de 20 sessões, com volume ≥ 1,5× a média, em Stage 2.",
        "Stop no mínimo dos últimos 10 dias; alvo de lucro em {alvo_r}R.",
        "Arrisca {risco_pct} do património até ao stop, no máximo {max_pos} por posição, até {n} posições.",
    ),
    "sniper": (
        "Seletivo: padrões de contração de volatilidade (VCP) perto do ponto de compra.",
        "Compra setups VCP prontos, ou ações em Stage 2 com amplitude de 10 dias ≤ {aperto_pct} junto ao máximo e score ≥ 80.",
        "Stop no mínimo dos últimos 10 dias; vende se o preço perder a média de 50 dias.",
        "Arrisca {risco_pct} do património até ao stop, no máximo {max_pos} por posição, até {n} posições.",
    ),
    "campeao": (
        "Rotação setorial: as melhores ações dos setores mais fortes.",
        "A cada {cada} sessões escolhe os {setores} setores com mais ações em tendência de alta e compra as {por_setor} melhores de cada.",
        "Vende as que saem da seleção, ou no stop de {stop_pct}.",
        "Partes iguais, ajustadas à zona do mercado.",
    ),
    "pescador": (
        "Contrário: compra quedas fortes em ações que continuam em tendência de alta.",
        "Compra ações acima da MM200 (e MM50 > MM200) que caíram ≥ {queda_pct} na semana.",
        "Vende com {alvo_pct} de lucro, no stop de {stop_pct}, ou ao fim de {max_dias} sessões.",
        "1/{n} do património por posição.",
    ),
    "deriva": (
        "Momentum de eventos: depois de resultados ou notícias fortes, as ações tendem a continuar a subir durante semanas.",
        "Compra ações que abriram ≥ {salto_pct} acima do fecho anterior com volume ≥ 2× a média nos últimos 4 dias, e que se mantêm acima do mínimo desse dia e da MM50.",
        "Stop abaixo do mínimo do dia do salto; vende ao fim de {max_dias} sessões.",
        "1/{n} do património por posição.",
    ),
    "jornalista": (
        "Notícias puras: mede quanto valem as notícias sozinhas (lidas pelo Claude).",
        "Compra ações com notícias importantes e positivas (força ≥ {limiar} nas últimas 36 h).",
        "Vende com notícias negativas, no stop de {stop_pct}, ou ao fim de {max_dias} sessões.",
        "1/{n} do património por posição.",
    ),
    "catalisador": (
        "Notícias + execução: só acredita numa notícia boa se o mercado também acreditar.",
        "Compra notícias positivas e importantes (força ≥ {limiar}) só quando o preço confirma: sobe no dia com volume ≥ 1,5× ou abre em salto, acima da MM50.",
        "Vende com notícias negativas, no stop (mínimo de 10 dias ou {stop_pct}), ou ao fim de {max_dias} sessões.",
        "1/{n} do património por posição.",
    ),
    "cetico": (
        "Adversarial: quando sai uma má notícia e a ação não cai, o mercado já a ignorou.",
        "Compra ações em Stage 2 com notícias negativas e importantes (força ≤ -{limiar}) que fecharam o dia sem cair.",
        "Vende com {alvo_pct} de lucro, no stop de {stop_pct}, ou ao fim de {max_dias} sessões.",
        "1/{n} do património por posição.",
    ),
    "conselho": (
        "Inteligência coletiva: quando vários agentes independentes escolhem a mesma ação, o sinal é mais forte.",
        "Compra ações que pelo menos {min_votos} agentes têm ou vão comprar, sem notícias negativas.",
        "Vende quando nenhum agente a apoia, com notícias negativas, ou no stop de {stop_pct}.",
        "1/{n} do património por posição.",
    ),
    "maestro": (
        "Coordenador central por regime: em cada tipo de mercado segue os agentes que melhor se portam nele.",
        "A cada {cada} sessões copia a carteira combinada dos {lideres} agentes que mais batem o S&P 500 na zona atual do mercado (alta, neutra, cautelosa ou queda): pelo registo ao vivo nessa zona (15+ dias) ou, até lá, pelo pré-treino.",
        "Vende o que sai das carteiras dos líderes; fica em dinheiro se nenhum agente bater o S&P 500 nesta zona.",
        "Os pesos das posições dos líderes, até {n} ações.",
    ),
    "kamikaze": (
        "Muito agressivo: tudo nas maiores subidas recentes, sem travões.",
        "A cada {cada} sessões compra as {n} ações com maior subida a 3 meses (acima da MM50).",
        "Vende as que saem do top, ou no stop de {stop_pct}.",
        "Partes iguais de todo o património, sem olhar à zona do mercado.",
    ),
}


def fmt_param(key: str, value) -> str:
    if isinstance(value, bool):
        return "sim" if value else "não"
    if key in PCT_PARAMS and isinstance(value, (int, float)):
        return f"{value * 100:.1f}%".replace(".0%", "%")
    return f"{value:g}" if isinstance(value, float) else str(value)


def describe(strategy: str, params: dict) -> tuple[str, str, str, str]:
    """(idea, buys, sells, size) with this agent's parameter values filled in."""
    shown = {k: fmt_param(k, v) for k, v in params.items()}
    idea, buys, sells, size = DOCS[strategy]
    return tuple(text.format_map(_Missing(shown)) for text in (idea, buys, sells, size))


class _Missing(dict):
    def __missing__(self, key):  # a parameter the agent does not have: leave the placeholder readable
        return f"«{key}»"


def params_lines(params: dict) -> list[str]:
    return [f"`{k}` = **{fmt_param(k, v)}** · {PARAMS.get(k, '')}" for k, v in params.items()]


# --------------------------------------------------------------------------
# Why an agent bought or sold: the strategy's rule plus the numbers at the time
# --------------------------------------------------------------------------

SELL_REASONS = [  # (start of the order's reason, plain explanation)
    ("stop", "o preço tocou no stop: a regra corta a perda (ou protege o ganho) sem esperar"),
    ("alvo", "o preço chegou ao alvo de lucro definido na compra"),
    ("saiu da lista", "na revisão periódica deixou de estar entre as escolhidas da estratégia"),
    ("perdeu a média 50d", "o preço fechou abaixo da média de 50 dias: a tendência que justificou a compra quebrou"),
    ("notícias negativas", "saíram notícias claramente negativas sobre a empresa"),
    ("não andou", "a posição estava a perder e não andou no tempo máximo definido"),
    ("falência", "o agente perdeu metade do dinheiro e foi liquidado"),
    ("zona", "a zona do mercado mudou e o peso alvo do ETF desceu"),
    ("o conselho deixou de a apoiar", "menos agentes do que o mínimo continuam a ter ou a querer esta ação"),
    ("saiu das carteiras dos líderes", "os agentes que o Maestro segue já não a têm"),
    ("nenhum agente bate", "nesta zona de mercado nenhum agente bate o S&P 500, por isso o Maestro fica de fora"),
]


def sell_reason_text(reason: str) -> str:
    for start, text in SELL_REASONS:
        if reason.startswith(start):
            return text
    if reason.endswith("sessões"):
        return f"atingiu o tempo máximo de {reason} que a estratégia mantém uma posição"
    return reason or "venda pela regra da estratégia"


def context_text(ctx: dict | None) -> str:
    """The numbers at the time of the decision, in one line."""
    if not ctx:
        return ""
    bits = []
    if "trend" in ctx:
        bits.append(f"tendência {ctx['trend']}/100")
    if "ret_3m" in ctx:
        bits.append(f"{ctx['ret_3m']:+.1f}% em 3 meses")
    if "from_high" in ctx:
        bits.append(f"{ctx['from_high']:.0f}% abaixo do máximo de 52 semanas")
    if "vs_sma50" in ctx:
        bits.append(f"{ctx['vs_sma50']:+.1f}% face à média de 50 dias")
    if ctx.get("vol_ratio"):
        bits.append(f"volume {ctx['vol_ratio']:.1f}× a média")
    if "tech" in ctx:
        bits.append(f"score técnico {ctx['tech']}/60")
    if ctx.get("news"):
        bits.append(f"notícias {ctx['news']:+d}")
    if ctx.get("zone"):
        bits.append(f"mercado em zona {ctx['zone']}")
    if ctx.get("vix"):
        bits.append(f"VIX {ctx['vix']:.0f}")
    if ctx.get("epu_pct") is not None and "epu_pct" in ctx:
        bits.append(f"incerteza política no percentil {ctx['epu_pct'] * 100:.0f}")
    if ctx.get("credit_dd"):
        bits.append(f"crédito de alto risco {ctx['credit_dd'] * 100:.0f}% abaixo do melhor do ano")
    if ctx.get("usd_chg") is not None and "usd_chg" in ctx:
        bits.append(f"dólar {-ctx['usd_chg'] * 100:+.1f}% face ao euro em 3 meses")
    if ctx.get("fund_q") is not None:
        bits.append(f"fundamentos: qualidade {ctx['fund_q']:.0f}/100" + (f", valor {ctx['fund_v']:.0f}/100" if ctx.get("fund_v") is not None else "")
                    + (f", P/E {ctx['pe']:.0f}" if ctx.get("pe") else "") + (f", margem de segurança {ctx['mos'] * 100:+.0f}%" if ctx.get("mos") is not None else ""))
    if ctx.get("sector_trend") is not None:
        bits.append(f"setor {ctx['sector_trend']:+.0f}% face à média de 200 dias")
    if ctx.get("gpr_pct") is not None:
        bits.append(f"risco geopolítico no percentil {ctx['gpr_pct'] * 100:.0f}")
    if ctx.get("oil_chg") is not None:
        bits.append(f"petróleo {ctx['oil_chg'] * 100:+.0f}% em 3 meses")
    if ctx.get("geo_risk") is not None:
        bits.append(f"risco geopolítico (Claude) {ctx['geo_risk']}/5")
    if ctx.get("stop") and "low" not in ctx:
        bits.append(f"stop em {ctx['stop']:.2f}")
    if "low" in ctx:
        bits.append(f"stop {ctx['stop']:.2f}, mínimo do dia {ctx['low']:.2f}, entrada {ctx['entry']:.2f}")
    if "high" in ctx:
        bits.append(f"alvo {ctx['tp']:.2f}, máximo do dia {ctx['high']:.2f}, entrada {ctx['entry']:.2f}")
    if "ret" in ctx and "low" not in ctx and "high" not in ctx:
        bits.append(f"{ctx['ret']:+.1f}% desde a compra" + (f" em {ctx['held']} sessões" if ctx.get("held") else ""))
    return "Na altura: " + ", ".join(bits) + "."


def justify(strategy: str, params: dict, trade: dict) -> str:
    """Why this buy or sell, in the strategy's own terms."""
    _, buys, sells, _ = describe(strategy, params)
    if trade["side"] == "compra":
        head = f"Comprou ({trade.get('reason') or 'regra de compra'}). Regra da estratégia: {buys}"
    elif trade["side"] == "venda":
        head = f"Vendeu: {sell_reason_text(trade.get('reason', ''))}. Regra da estratégia: {sells}"
    else:
        return trade.get("reason", "")
    return " ".join(x for x in (head, context_text(trade.get("ctx"))) if x)
