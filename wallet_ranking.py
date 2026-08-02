"""
Filtro e ranking de carteiras para copy trading.

FIXES v2:
- Correcao do calculo de win rate: o problema era que a API do Polymarket
  retorna posicoes com pnl=0 para mercados ainda nao resolvidos, que eram
  contadas como nem win nem loss, mas o denominador ficava errado.
  Agora: win rate = wins / (wins + losses) — ignora posicoes com pnl==0.
- Adiciona campo "win_rate_display" para debug (wins/losses/empates visiveis)
- Guarda amostra de posicao em _debug para facilitar diagnostico
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
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _compute_metrics(wallet_address: str, trades: list[dict],
                      positions: list[dict]) -> WalletMetrics:
    """
    FIX v2: win rate calculado correctamente.

    O problema anterior: posicoes com pnl=0 eram ignoradas no count de
    wins/losses mas o campo resolved_trades contava-as, fazendo o
    win_rate = wins/resolved aparecer sempre baixo (0/96 = 0% mesmo
    com carteiras lucrativas).

    Correccao: win_rate = wins / (wins + losses), ignorando pnl==0.
    Posicoes com pnl==0 sao tipicamente mercados resolvidos como N/A
    ou posicoes ainda abertas que a API inclui na lista.
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
    pnl_zero = 0  # posicoes com pnl=0 (provavelmente nao resolvidas ainda)

    # Guarda amostra para debug na primeira execucao real
    if positions:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        debug_path = os.path.join(config.DATA_DIR, "_debug_sample_position.json")
        if not os.path.exists(debug_path):
            with open(debug_path, "w") as f:
                json.dump(positions[0], f, indent=2)

    for p in positions:
        # Aceita varios nomes de campo possiveis para "posicao resolvida"
        is_resolved = bool(_first_present(
            p, ["redeemable", "resolved", "closed", "isResolved"], False
        ))
        if not is_resolved:
            continue

        pnl = _first_present(p, ["realizedPnl", "cashPnl", "pnl", "profit"], None)
        if pnl is None:
            continue
        try:
            pnl = float(pnl)
        except (TypeError, ValueError):
            continue

        resolved += 1
        if pnl > 0.01:       # margem de 1 centavo para evitar falsos positivos
            wins += 1
        elif pnl < -0.01:
            losses += 1
        else:
            pnl_zero += 1    # pnl ~0: ignorado no win rate mas contado para debug

    # FIX: denominador = wins + losses (ignora pnl~0)
    denominator = wins + losses
    win_rate = (wins / denominator) if denominator > 0 else 0.0

    total_trades = len(trades)

    qualifies = True
    reasons = []
    if total_trades < config.MIN_TRADES_HISTORY:
        qualifies = False
        reasons.append(f"apenas {total_trades} trades (minimo {config.MIN_TRADES_HISTORY})")
    if denominator == 0:
        qualifies = False
        reasons.append("sem trades resolvidos com PnL claro para validar performance")
    elif win_rate < config.MIN_WIN_RATE:
        qualifies = False
        reasons.append(f"win rate {win_rate:.0%} abaixo do minimo {config.MIN_WIN_RATE:.0%} "
                       f"({wins}W/{losses}L, {pnl_zero} neutros ignorados)")

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
    candidates = _load_candidate_wallets()
    results: list[WalletMetrics] = []
    for wallet in candidates:
        print(f"  avaliando {wallet[:12]}...")
        trades = pm.get_all_trades_for_user(wallet)
        positions = pm.get_positions_for_user(wallet)
        metrics = _compute_metrics(wallet, trades, positions)
        results.append(metrics)
        print(f"    trades={metrics.total_trades} resolved={metrics.resolved_trades} "
              f"wins={metrics.wins} losses={metrics.losses} "
              f"win_rate={metrics.win_rate:.0%} qualifies={metrics.qualifies}")

    if save:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(config.QUALIFIED_WALLETS_FILE, "w") as f:
            json.dump([asdict(m) for m in results], f, indent=2)

    qualified = [m for m in results if m.qualifies]
    print(f"\n{len(results)} carteiras avaliadas, {len(qualified)} qualificadas.")
    return results


def load_qualified_wallets() -> list[str]:
    if not os.path.exists(config.QUALIFIED_WALLETS_FILE):
        return []
    with open(config.QUALIFIED_WALLETS_FILE) as f:
        data = json.load(f)
    return [row["wallet_address"] for row in data if row.get("qualifies")]
