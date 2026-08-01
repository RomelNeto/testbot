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
