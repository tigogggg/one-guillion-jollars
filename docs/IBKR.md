# Interactive Brokers (scripts/ibkr.py)

Um agente seguido por uma conta da IBKR, primeiro em papel. O mesmo código simula, para os agentes em teste, uma
conta da IBKR com as regras de lá, para se ver o que se perde a passar do agente para uma conta real.

## Como segue

Não copia operação a operação: a cada ciclo põe a conta nos **pesos** da carteira do agente (posições + ordens para a
abertura seguinte), à escala do valor da conta. Um ciclo que falhe é compensado no seguinte. Diferenças abaixo de
`max(min_ordem_eur, banda × conta)` ficam como estão; uma ação que o agente já não tem é vendida toda. Posições
compradas à mão (que o agente nunca teve) não são tocadas.

## Conta simulada (já a correr)

Nas rondas e no diário, os agentes de `ibkr.agentes` (por omissão os candidatos congelados) têm cada um uma conta
virtual da IBKR com `capital_real` euros: ações inteiras (ou frações com `fracoes`), ordem mínima, spread, câmbio e
comissões da IBKR. Fica em `agents/lab/ibkr/virtual/<agente>.json`; o resultado aparece no portal, separador 🎯 Decisão,
coluna **Na IBKR (simulada)**.

## Conta de papel (no teu PC)

1. Conta IBKR **Pro**, aberta e com dinheiro: a IBKR só dá acesso à API (incluindo papel) a contas abertas e
   financiadas. Moeda base **EUR**.
2. Instala Java e descarrega o **Client Portal Gateway** (página da Web API da IBKR). Corre-o
   (`bin/run.sh root/conf.yaml`, ou `bin\run.bat root\conf.yaml` no Windows).
3. Abre https://localhost:5000 e entra com o utilizador **da conta de papel**. As credenciais ficam só no gateway:
   nem o script nem o repositório as veem. Uma sessão por utilizador: entrar no TWS ou na app com o mesmo utilizador
   desliga a do gateway.
4. `uv run python scripts/ibkr.py estado` → tem de dizer `Sessão: sim` e uma conta `papel DU…`.
5. `uv run python scripts/ibkr.py plano --agente <id>` → as ordens que mandaria agora (nada é enviado).
6. `uv run python scripts/ibkr.py seguir --agente <id> --ciclo 600 --push` → manda as ordens de 10 em 10 minutos,
   enquanto o PC estiver ligado e a sessão aberta. Quando a sessão cai, o script avisa e espera que voltes a entrar.

## Regras (config.json, chave `ibkr`, no repositório privado)

| chave | omissão | o que faz |
|---|---|---|
| `agentes` | candidatos congelados | quem tem conta simulada; o primeiro é o que `seguir` usa sem `--agente` |
| `capital_eur` | `capital_real` | dinheiro da conta simulada |
| `fracoes` | false | frações de ações (a IBKR tem para ações e ETFs dos EUA e da Europa elegíveis) |
| `min_ordem_eur` | 100 | ordem mínima (a comissão mínima europeia é 1,25 €) |
| `banda` | 0.01 | diferença de peso que não justifica ordem |
| `max_ordem_eur` | 2500 | teto por ordem |
| `max_ordens` | 10 | teto de ordens por ciclo |
| `max_giro` | 0.6 | teto do valor negociado por dia, em fração da conta |
| `limite_pct` | 0.005 | ordens limitadas a 0,5% do preço de referência (nunca a mercado) |
| `real` | false | conta real: só com isto a true **e** `IBKR_REAL=1` no PC; as perguntas da IBKR só são respondidas com `--confirmar` |

Travão: criar o ficheiro `agents/lab/ibkr/PARAR` pára todas as ordens. Tudo o que é planeado e respondido fica em
`agents/lab/ibkr/registo-<AAAA-MM>.jsonl`. Ordens só com a bolsa aberta (sessão normal), DAY.

## Fora do âmbito

Negociar pelos clientes exige o registo de terceiros da IBKR e autorização do regulador (CMVM): para eles há os avisos
por mensagem privada do agente que escolhem no perfil (`scripts/copy_alerts.py`), e executam eles.
