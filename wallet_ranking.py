"""
Filtro e ranking de carteiras para copy trading.

Nao existe um endpoint publico e documentado de "leaderboard" oficial e
estavel do Polymarket, entao a abordagem aqui e:

1. Voce fornece uma lista de carteiras candidatas em data/watchlist.csv
   (uma coluna "wallet_address" com o endereco publico de cada carteira
   que quer avaliar). Essa lista pode vir do leaderboard visivel em
   polymarket.com/leaderboard, de trackers como Polymarket Analytics /
   WalletMaster, ou de qualquer carteira que voce queira testar.

2. O bot busca o historico real de trades de cada carteira (Data API) e
   calcula metricas objetivas.

3. So entram na "watchlist qualificada" as carteiras que passam nos
   filtros minimos definidos em config.py (numero de trades, win rate).
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, asdict
from typing import Optional

import config
import polymarket_client as pm


@dataclass
class WalletMetrics:
    wallet_address: str
    total_trades: int
    resolved_trades: int
    wins: int
    losses: int
    win_rate: float
    total_volume_usd: float
    qualifies: bool
    reason: str


def _load_candidate_wallets(path: str = config.WATCHLIST_FILE) -> list[str]:
    if not os.path.exists(path):
        return []
    wallets = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            addr = (row.get("wallet_address") or "").strip()
            if addr:
                wallets.append(addr)
    return wallets


def _first_present(d: dict, keys: list[str], default=None):
    """Tenta varios nomes de campo possiveis, ja que a Data API pode nomear
    os campos de formas ligeiramente diferentes conforme a versao/endpoint."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _compute_metrics(wallet_address: str, trades: list[dict],
                      positions: list[dict]) -> WalletMetrics:
    """
    Calcula metricas combinando dois dados:

    - trades: historico bruto (usado para volume e contagem total de trades).
    - positions: posicoes da carteira, que e onde o Polymarket normalmente
      expoe o PnL ja calculado por posicao (ex.: campo "realizedPnl" ou
      "cashPnl"). Usamos isso para decidir ganho/perda por posicao
      RESOLVIDA -- e mais confiavel do que tentar recalcular resultado
      trade a trade.

    AVISO: os nomes exatos de campo abaixo foram definidos com base na
    documentacao publica disponivel, mas nao pude testar contra a API ao
    vivo (o ambiente onde este bot foi escrito bloqueia acesso direto ao
    Polymarket). Na primeira execucao real, confira o `raw sample` que o
    bot salva em data/_debug_sample_position.json e ajuste os nomes de
    campo em _first_present() se necessario.
    """
    total_volume = 0.0
    for t in trades:
        try:
            size = float(_first_present(t, ["size", "amount"], 0) or 0)
            price = float(_first_present(t, ["price"], 0) or 0)
            total_volume += size * price
        except (TypeError, ValueError):
            pass

    resolved = 0
    wins = 0
    losses = 0

    if positions:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        debug_path = os.path.join(config.DATA_DIR, "_debug_sample_position.json")
        if not os.path.exists(debug_path):
            with open(debug_path, "w") as f:
                json.dump(positions[0], f, indent=2)

    for p in positions:
        is_resolved = bool(_first_present(p, ["redeemable", "resolved", "closed"], False))
        if not is_resolved:
            continue
        pnl = _first_present(p, ["realizedPnl", "cashPnl", "pnl"], None)
        if pnl is None:
            continue
        try:
            pnl = float(pnl)
        except (TypeError, ValueError):
            continue
        resolved += 1
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

    win_rate = (wins / resolved) if resolved > 0 else 0.0
    total_trades = len(trades)

    qualifies = True
    reasons = []
    if total_trades < config.MIN_TRADES_HISTORY:
        qualifies = False
        reasons.append(f"apenas {total_trades} trades (minimo {config.MIN_TRADES_HISTORY})")
    if resolved > 0 and win_rate < config.MIN_WIN_RATE:
        qualifies = False
        reasons.append(f"win rate {win_rate:.0%} abaixo do minimo {config.MIN_WIN_RATE:.0%}")
    if resolved == 0:
        # Sem dados de resultado, nao da pra confirmar performance --
        # tratamos como nao qualificada ate haver dado suficiente.
        qualifies = False
        reasons.append("sem trades resolvidos suficientes para validar performance")

    reason = "OK" if qualifies else "; ".join(reasons)

    return WalletMetrics(
        wallet_address=wallet_address,
        total_trades=total_trades,
        resolved_trades=resolved,
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        total_volume_usd=round(total_volume, 2),
        qualifies=qualifies,
        reason=reason,
    )


def build_qualified_wallet_list(save: bool = True) -> list[WalletMetrics]:
    """Avalia todas as carteiras candidatas e retorna as metricas de cada uma."""
    candidates = _load_candidate_wallets()
    results: list[WalletMetrics] = []

    for wallet in candidates:
        trades = pm.get_all_trades_for_user(wallet)
        positions = pm.get_positions_for_user(wallet)
        metrics = _compute_metrics(wallet, trades, positions)
        results.append(metrics)

    if save:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(config.QUALIFIED_WALLETS_FILE, "w") as f:
            json.dump([asdict(m) for m in results], f, indent=2)

    return results


def load_qualified_wallets() -> list[str]:
    """Le o resultado ja salvo e retorna so os enderecos que passaram no filtro."""
    if not os.path.exists(config.QUALIFIED_WALLETS_FILE):
        return []
    with open(config.QUALIFIED_WALLETS_FILE) as f:
        data = json.load(f)
    return [row["wallet_address"] for row in data if row.get("qualifies")]
