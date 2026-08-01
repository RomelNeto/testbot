"""
Teste offline com dados de exemplo (mock), so para validar a LOGICA do bot
sem depender de acesso real a rede do Polymarket (bloqueada neste sandbox).

Isto NAO faz parte do bot em si -- e soh um script de verificacao.
Pode ser apagado depois de conferir que tudo funciona.
"""
import os
import random
from unittest import mock

import config
import wallet_ranking
import wallet_discovery
import polymarket_client as pm
from simulator import SimulationState, maybe_copy_trade, close_position

WALLET_A = "0xAAA0000000000000000000000000000000AAA0"
WALLET_B = "0xBBB0000000000000000000000000000000BBB0"
WALLET_C = "0xCCC0000000000000000000000000000000CCC0"  # aparece pouco, nao deve qualificar como candidato


def fake_trades_for_user(wallet_address, limit=500, offset=0):
    if offset > 0:
        return []
    random.seed(hash(wallet_address) % 1000)
    trades = []
    for i in range(120):
        trades.append({
            "id": f"{wallet_address}-trade-{i}",
            "conditionId": f"market-{i % 15}",
            "outcome": "Yes" if i % 2 == 0 else "No",
            "side": "BUY",
            "price": round(random.uniform(0.2, 0.8), 2),
            "size": round(random.uniform(50, 500), 2),
            "title": f"Mercado de teste {i % 15}",
        })
    return trades


def fake_positions_for_user(wallet_address):
    random.seed(hash(wallet_address) % 1000)
    win_bias = 0.65 if wallet_address == WALLET_A else 0.3
    positions = []
    for i in range(110):
        won = random.random() < win_bias
        positions.append({
            "market": f"market-{i % 15}",
            "redeemable": True,
            "realizedPnl": round(random.uniform(5, 50), 2) if won else -round(random.uniform(5, 50), 2),
        })
    return positions


def fake_global_trades_feed(limit=500, offset=0, min_cash_amount=None):
    """Simula o feed global: WALLET_A aparece muitas vezes com trades
    grandes, WALLET_C aparece so 1 vez (nao deveria virar candidata)."""
    if offset > 0:
        return []
    feed = []
    for i in range(20):
        feed.append({"proxyWallet": WALLET_A, "size": 1000, "price": 0.5})
    for i in range(8):
        feed.append({"proxyWallet": WALLET_B, "size": 800, "price": 0.4})
    feed.append({"proxyWallet": WALLET_C, "size": 2000, "price": 0.5})  # so 1x
    return feed


def test_discovery():
    print("=== Teste: descoberta automatica de carteiras ===")
    with mock.patch.object(pm, "get_recent_trades_feed", side_effect=fake_global_trades_feed):
        wallets = wallet_discovery.discover_and_save_watchlist(min_appearances=3, top_n=10)

    print("Carteiras descobertas:", wallets)
    assert WALLET_A in wallets, "Carteira A (muitos trades grandes) deveria ser descoberta"
    assert WALLET_B in wallets, "Carteira B (trades grandes recorrentes) deveria ser descoberta"
    assert WALLET_C not in wallets, "Carteira C (apareceu 1x) NAO deveria ser descoberta"
    assert wallets[0] == WALLET_A, "Carteira A tem mais volume, deveria vir primeiro"
    print("OK: descoberta automatica funcionando.\n")


def test_ranking_and_cycle():
    print("=== Teste: ranking + ciclo de simulacao ===")
    with mock.patch.object(pm, "get_all_trades_for_user", side_effect=lambda w, **kw: fake_trades_for_user(w)), \
         mock.patch.object(pm, "get_positions_for_user", side_effect=fake_positions_for_user):
        results = wallet_ranking.build_qualified_wallet_list()

    for r in results:
        print(f"{r.wallet_address}: win_rate={r.win_rate:.0%} qualifies={r.qualifies}")

    qualified = wallet_ranking.load_qualified_wallets()
    assert WALLET_A in qualified
    print("OK: ranking funcionando.\n")

    print("=== Teste: main.run_single_cycle (modo cron/single-pass) ===")
    import main as bot_main

    state = SimulationState()
    with mock.patch.object(pm, "get_trades_for_user", side_effect=lambda w, **kw: fake_trades_for_user(w)[:5]), \
         mock.patch.object(pm, "get_all_markets", return_value=[]):
        bot_main.run_single_cycle(state, [WALLET_A])

    print(f"Posicoes abertas apos 1 ciclo: {len(state.open_positions)}")
    assert len(state.open_positions) > 0, "O ciclo unico deveria ter copiado ao menos 1 trade"
    state.save()
    print("OK: modo cycle (single-pass, usado pelo GitHub Actions) funcionando.\n")


if __name__ == "__main__":
    os.makedirs(config.DATA_DIR, exist_ok=True)
    test_discovery()
    test_ranking_and_cycle()
    print("Todos os testes offline passaram.")
