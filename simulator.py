"""
Motor de simulacao (copy trading em modo dry-run).

FIXES v2:
- Impede copiar os dois lados do mesmo mercado (bug dos dois outcomes)
- Fecho de posicoes agora usa a Gamma API por market_id directo (mais fiavel)
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone

import config


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def _save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


class SimulationState:
    """Carrega e persiste o estado da simulacao entre execucoes do bot."""

    def __init__(self):
        os.makedirs(config.DATA_DIR, exist_ok=True)
        self.open_positions: dict = _load_json(config.OPEN_POSITIONS_FILE, {})
        self.seen_trade_ids: list = _load_json(config.SEEN_TRADES_FILE, [])
        self.consecutive_losses: dict = _load_json(
            os.path.join(config.DATA_DIR, "consecutive_losses.json"), {}
        )
        self.cash_balance = self._recompute_cash_balance()

    def _recompute_cash_balance(self) -> float:
        balance = config.SIMULATED_CAPITAL_USD
        if not os.path.exists(config.TRADE_LOG_FILE):
            return balance
        with open(config.TRADE_LOG_FILE, newline="") as f:
            for row in csv.DictReader(f):
                action = row.get("action")
                if action == "OPEN":
                    balance -= float(row.get("size_usd", 0) or 0)
                elif action == "CLOSE":
                    balance += float(row.get("proceeds_usd", 0) or 0)
        return balance

    def save(self) -> None:
        _save_json(config.OPEN_POSITIONS_FILE, self.open_positions)
        _save_json(config.SEEN_TRADES_FILE, self.seen_trade_ids)
        _save_json(os.path.join(config.DATA_DIR, "consecutive_losses.json"),
                   self.consecutive_losses)

    def is_wallet_paused(self, wallet_address: str) -> bool:
        return self.consecutive_losses.get(wallet_address, 0) >= config.MAX_CONSECUTIVE_LOSSES_PER_WALLET

    def already_has_position_for_market(self, market_id: str) -> bool:
        """FIX: impede abrir posicao em mercado onde ja existe uma posicao aberta
        (qualquer outcome) — evita o bug de apostar nos dois lados."""
        for pos in self.open_positions.values():
            if pos.get("market_id") == market_id:
                return True
        return False


def _append_trade_log(row: dict) -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    file_exists = os.path.exists(config.TRADE_LOG_FILE)
    fieldnames = [
        "timestamp", "action", "source_wallet", "market_id", "market_question",
        "outcome", "side", "entry_price", "size_usd", "proceeds_usd",
        "pnl_usd", "reason",
    ]
    with open(config.TRADE_LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in fieldnames})


def maybe_copy_trade(state: SimulationState, source_wallet: str, source_trade: dict) -> bool:
    """
    Avalia um trade novo e decide se copia. Retorna True se copiou.
    """
    trade_id = source_trade.get("id") or source_trade.get("transactionHash")
    if trade_id and trade_id in state.seen_trade_ids:
        return False

    if state.is_wallet_paused(source_wallet):
        return False

    if len(state.open_positions) >= config.MAX_OPEN_POSITIONS:
        return False

    market_id = source_trade.get("conditionId") or source_trade.get("market")
    outcome = source_trade.get("outcome")
    side = source_trade.get("side", "BUY")
    entry_price = float(source_trade.get("price", 0) or 0)

    if entry_price <= 0 or entry_price >= 1:
        return False

    # FIX: nao abrir segundo lado do mesmo mercado
    if market_id and state.already_has_position_for_market(market_id):
        print(f"  [skip] mercado {market_id[:16]}... ja tem posicao aberta — ignorando segundo lado")
        if trade_id:
            state.seen_trade_ids.append(trade_id)
        return False

    size_usd = round(config.SIMULATED_CAPITAL_USD * config.POSITION_SIZE_PCT, 2)
    if size_usd > state.cash_balance:
        return False

    position_key = f"{market_id}:{outcome}:{trade_id}"
    state.open_positions[position_key] = {
        "source_wallet": source_wallet,
        "market_id": market_id,
        "market_question": source_trade.get("title") or source_trade.get("question", ""),
        "outcome": outcome,
        "side": side,
        "entry_price": entry_price,
        "size_usd": size_usd,
        "opened_at": _now_iso(),
    }
    if trade_id:
        state.seen_trade_ids.append(trade_id)
    state.cash_balance -= size_usd

    _append_trade_log({
        "timestamp": _now_iso(),
        "action": "OPEN",
        "source_wallet": source_wallet,
        "market_id": market_id,
        "market_question": state.open_positions[position_key]["market_question"],
        "outcome": outcome,
        "side": side,
        "entry_price": entry_price,
        "size_usd": size_usd,
        "reason": "copiado da carteira qualificada",
    })
    return True


def close_position(state: SimulationState, position_key: str, resolution_price: float,
                    reason: str = "mercado resolvido") -> None:
    """Fecha uma posicao simulada com o preco de resolucao."""
    pos = state.open_positions.pop(position_key, None)
    if pos is None:
        return

    shares = pos["size_usd"] / pos["entry_price"] if pos["entry_price"] else 0
    proceeds_usd = round(shares * resolution_price, 2)
    pnl_usd = round(proceeds_usd - pos["size_usd"], 2)

    state.cash_balance += proceeds_usd

    wallet = pos["source_wallet"]
    if pnl_usd < 0:
        state.consecutive_losses[wallet] = state.consecutive_losses.get(wallet, 0) + 1
    else:
        state.consecutive_losses[wallet] = 0

    _append_trade_log({
        "timestamp": _now_iso(),
        "action": "CLOSE",
        "source_wallet": wallet,
        "market_id": pos["market_id"],
        "market_question": pos["market_question"],
        "outcome": pos["outcome"],
        "side": pos["side"],
        "entry_price": pos["entry_price"],
        "size_usd": pos["size_usd"],
        "proceeds_usd": proceeds_usd,
        "pnl_usd": pnl_usd,
        "reason": reason,
    })
