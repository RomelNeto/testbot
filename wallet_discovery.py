"""
Descoberta automatica de carteiras candidatas -- para quem nao quer (ou nao
sabe) procurar enderecos manualmente no leaderboard do Polymarket.

Como funciona: em vez de exigir uma lista pronta, o bot varre o FEED
GLOBAL de trades recentes (todas as carteiras, nao uma so), filtrando por
trades de valor alto (parametro filterType=CASH / filterAmount, documentado
pela Data API). A ideia: quem faz trades grandes e repetidos tem mais chance
de ser um trader serio do que alguem apostando $2 por diversao.

As carteiras que aparecem com frequencia e volume relevante nesse feed
viram "candidatas" e sao escritas em data/watchlist.csv automaticamente.
Elas AINDA passam pelo filtro de qualidade de wallet_ranking.py (min. 100
trades, win rate minimo) antes de virarem carteiras "qualificadas" de
verdade -- a descoberta so resolve o problema de "de onde tirar os
enderecos", nao substitui a validacao de performance.
"""
from __future__ import annotations

import csv
import os
from collections import defaultdict

import config
import polymarket_client as pm


def _first_present(d: dict, keys: list[str], default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def discover_candidate_wallets(
    min_trade_size_usd: float = 500.0,
    pages_to_scan: int = 10,
    min_appearances: int = 3,
    top_n: int = 25,
) -> list[str]:
    """
    Varre o feed global de trades recentes e retorna os enderecos de
    carteira que aparecem com trades grandes (>= min_trade_size_usd) pelo
    menos `min_appearances` vezes na janela escaneada, ordenados por
    volume total (maior primeiro), limitado a `top_n` carteiras.
    """
    volume_by_wallet: dict[str, float] = defaultdict(float)
    count_by_wallet: dict[str, int] = defaultdict(int)

    offset = 0
    for _ in range(pages_to_scan):
        page = pm.get_recent_trades_feed(limit=500, offset=offset,
                                          min_cash_amount=min_trade_size_usd)
        if not page:
            break
        for t in page:
            wallet = _first_present(t, ["proxyWallet", "user", "maker", "wallet"])
            if not wallet:
                continue
            size = float(_first_present(t, ["size", "amount"], 0) or 0)
            price = float(_first_present(t, ["price"], 0) or 0)
            volume_by_wallet[wallet] += size * price
            count_by_wallet[wallet] += 1
        if len(page) < 500:
            break
        offset += 500

    candidates = [
        w for w, count in count_by_wallet.items() if count >= min_appearances
    ]
    candidates.sort(key=lambda w: volume_by_wallet[w], reverse=True)
    return candidates[:top_n]


def discover_and_save_watchlist(**kwargs) -> list[str]:
    """Descobre carteiras e SOBRESCREVE data/watchlist.csv com o resultado."""
    wallets = discover_candidate_wallets(**kwargs)

    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(config.WATCHLIST_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["wallet_address"])
        for w in wallets:
            writer.writerow([w])

    return wallets
