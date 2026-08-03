"""
Configuracoes centrais do bot de simulacao (copy trading no Polymarket).

Tudo que voce normalmente ajustaria durante o mes de teste fica aqui,
para nao precisar mexer na logica do bot.
"""

# ---------------------------------------------------------------------------
# MODO DE OPERACAO
# ---------------------------------------------------------------------------
# DRY_RUN = True  -> modo simulacao (nenhuma ordem real e enviada).
# DRY_RUN = False -> modo real (NAO IMPLEMENTADO neste projeto de proposito;
#                    veja o README, secao "Migrando para dinheiro real").
DRY_RUN = True

# ---------------------------------------------------------------------------
# CAPITAL
# ---------------------------------------------------------------------------
SIMULATED_CAPITAL_USD = 500.0

# Percentual do capital arriscado em CADA trade copiado (position sizing fixo).
# 5% de $500 = $25 por trade. Isso limita o dano de uma sequencia ruim.
POSITION_SIZE_PCT = 0.05

# Numero maximo de posicoes abertas ao mesmo tempo (protecao de exposicao).
MAX_OPEN_POSITIONS = 8

# NOVO: numero maximo de posicoes copiadas no MESMO mercado (mesmo market_id).
# Evita que o bot concentre todo o capital num unico evento quando a carteira
# de origem faz varias compras seguidas no mesmo mercado (ex.: escalando uma
# posicao aos poucos). 1 = so a primeira compra em cada mercado e copiada.
MAX_POSITIONS_PER_MARKET = 1

# NOVO: numero maximo de posicoes copiadas no mesmo EVENTO real (agrupado por
# eventId/eventSlug). Diferente de MAX_POSITIONS_PER_MARKET: um mesmo jogo
# pode ter varios mercados diferentes (ex.: "BO3 winner" e "Game 2 winner"
# da mesma partida) -- isso e exposicao CORRELACIONADA, nao diversificacao de
# verdade. 1 = so copia UM mercado por evento, mesmo que a carteira de origem
# tenha apostado em varios mercados do mesmo jogo/eleicao/etc.
MAX_POSITIONS_PER_EVENT = 1

# ---------------------------------------------------------------------------
# CUSTOS DE EXECUCAO (taxa + slippage)
# ---------------------------------------------------------------------------
# Taxa aplicada em cada trade copiado, simulando a taxa real do Polymarket
# (que pode chegar a ~1.56% dependendo do lado da operacao). Aplicada so na
# ABERTURA da posicao -- resgatar/reivindicar o resultado no final nao tem
# taxa adicional na Polymarket de verdade.
TAKER_FEE_PCT = 0.02

# Slippage simulado: o preco que voce realmente consegue ao copiar costuma
# ser um pouco pior do que o preco do trade original, porque ha um atraso
# entre a carteira de origem negociar e o bot detectar (o ciclo roda a cada
# alguns minutos, nao em tempo real). 0.01 = 1% pior que o preco visto.
SLIPPAGE_PCT = 0.01

# ---------------------------------------------------------------------------
# FILTRO DE CARTEIRAS (WALLET RANKING)
# ---------------------------------------------------------------------------
# Numero minimo de trades historicos para uma carteira ser considerada
# "validada" (abaixo disso e apenas variancia, nao habilidade comprovada).
MIN_TRADES_HISTORY = 100

# Win rate minimo (0.0 a 1.0) para a carteira entrar na watchlist filtrada.
MIN_WIN_RATE = 0.55

# Categoria de mercado para focar o filtro.
# None = sem restricao (todas as categorias). Ex.: "Sports", "Politics", "Crypto".
CATEGORY_FILTER = None

# NOVO: quantos dos trades resolvidos MAIS RECENTES usar para calcular o win
# rate "de agora" -- uma carteira pode ter tido uma fase otima ha meses e
# estar errando mais ultimamente, e o historico completo demora a refletir
# isso. Se a carteira tiver pelo menos MIN_RECENT_SAMPLE trades resolvidos
# dentro dessa janela, o win rate RECENTE e usado para qualificar (em vez do
# historico completo). Com menos que isso, cai de volta no historico
# completo, ja que uma amostra recente pequena demais e so ruido.
RECENT_TRADES_WINDOW = 30
MIN_RECENT_SAMPLE = 10

# Depois de N perdas SEGUIDAS copiando uma carteira especifica, o bot para
# de segui-la automaticamente ate revisao manual.
MAX_CONSECUTIVE_LOSSES_PER_WALLET = 3

# ---------------------------------------------------------------------------
# POLLING (com que frequencia o bot verifica novos trades das carteiras)
# ---------------------------------------------------------------------------
POLL_INTERVAL_SECONDS = 300  # 5 minutos

# ---------------------------------------------------------------------------
# API (Polymarket - endpoints publicos, sem necessidade de autenticacao
# para leitura de dados de mercado e trades)
# ---------------------------------------------------------------------------
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"

REQUEST_TIMEOUT_SECONDS = 15

# ---------------------------------------------------------------------------
# ARQUIVOS DE ESTADO
# ---------------------------------------------------------------------------
DATA_DIR = "data"
WATCHLIST_FILE = f"{DATA_DIR}/watchlist.csv"          # carteiras candidatas (voce preenche)
QUALIFIED_WALLETS_FILE = f"{DATA_DIR}/qualified_wallets.json"  # carteiras aprovadas pelo filtro
TRADE_LOG_FILE = f"{DATA_DIR}/trade_log.csv"          # historico de trades simulados
OPEN_POSITIONS_FILE = f"{DATA_DIR}/open_positions.json"
SEEN_TRADES_FILE = f"{DATA_DIR}/seen_trade_ids.json"  # evita copiar o mesmo trade 2x
