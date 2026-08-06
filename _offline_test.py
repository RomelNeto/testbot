"""
Teste offline com dados de exemplo (mock), so para validar a LOGICA do bot
sem depender de acesso real a rede do Polymarket (bloqueada neste sandbox).

Isto NAO faz parte do bot em si -- e soh um script de verificacao.
Pode ser apagado depois de conferir que tudo funciona.
"""
import os
import time
from unittest import mock

import config
import wallet_ranking
import wallet_discovery
import polymarket_client as pm
from simulator import SimulationState, maybe_copy_trade, close_position

WALLET_A = "0xAAA0000000000000000000000000000000AAA0"
WALLET_B = "0xBBB0000000000000000000000000000000BBB0"
WALLET_C = "0xCCC0000000000000000000000000000000CCC0"  # aparece pouco, nao deve qualificar como candidato


def fake_market_winner(market_id):
    """Vencedor determinístico por mercado: 'Yes' se o número final for par."""
    try:
        n = int(str(market_id).split("-")[-1])
    except Exception:
        n = 0
    return "Yes" if n % 2 == 0 else "No"


def fake_trades_for_user(wallet_address, limit=500, offset=0):
    if offset > 0:
        return []
    # Deterministico (NAO usar hash(): e aleatorio entre execucoes do Python
    # e deixa o teste flaky). WALLET_A acerta 70% dos mercados, demais 30%.
    hit_rate = 7 if wallet_address == WALLET_A else 3
    now = int(time.time())
    trades = []
    for i in range(120):
        mid = f"market-{i % 15}"
        winner = fake_market_winner(mid)
        match = (i % 10) < hit_rate
        outcome = winner if match else ("No" if winner == "Yes" else "Yes")
        trades.append({
            "id": f"{wallet_address}-trade-{i}",
            "conditionId": mid,
            "outcome": outcome,
            "side": "BUY",
            "price": round(0.2 + (i % 60) / 100.0, 2),
            "size": 100.0 + (i % 5) * 50,
            "timestamp": now - i * 60,  # do mais novo ao mais antigo (como a API)
            "title": f"Mercado de teste {i % 15}",
        })
    return trades


def fake_resolved_market(condition_id, limit=25, offset=0):
    """Resolucao fake de um mercado (para o ranking): vencedor a preco 1.0."""
    return [{"outcome": fake_market_winner(condition_id), "price": 1.0, "outcomeIndex": 0}]


def fake_active_market(condition_id, limit=25, offset=0):
    """Mercado AINDA ATIVO (para a copia): precos intermediarios."""
    return [{"outcome": "Yes", "price": 0.5, "outcomeIndex": 0},
            {"outcome": "No", "price": 0.5, "outcomeIndex": 1}]


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
         mock.patch.object(pm, "get_trades_for_market", side_effect=lambda cid, **kw: fake_resolved_market(cid)):
        results = wallet_ranking.build_qualified_wallet_list()

    for r in results:
        print(f"{r.wallet_address}: win_rate={r.win_rate:.0%} qualifies={r.qualifies}")

    qualified = wallet_ranking.load_qualified_wallets()
    assert WALLET_A in qualified, "WALLET_A (70% de acerto) deveria qualificar"
    assert WALLET_B not in qualified, "WALLET_B (30% de acerto) NAO deveria qualificar"
    print("OK: ranking funcionando.\n")

    print("=== Teste: main.run_single_cycle (modo cron/single-pass) ===")
    import main as bot_main

    # Estado limpo em memoria (nao usar os dados reais do disco)
    state = SimulationState()
    state.open_positions = {}
    state.seen_trade_ids = []
    state.consecutive_losses = {}
    state.cash_balance = config.SIMULATED_CAPITAL_USD

    with mock.patch.object(pm, "get_trades_for_user", side_effect=lambda w, **kw: fake_trades_for_user(w)[:5]), \
         mock.patch.object(pm, "get_trades_for_market", side_effect=lambda cid, **kw: fake_active_market(cid)), \
         mock.patch.object(pm, "get_position_for_market", return_value={}):
        bot_main.run_single_cycle(state, [WALLET_A])

    print(f"Posicoes abertas apos 1 ciclo: {len(state.open_positions)}")
    assert len(state.open_positions) > 0, "O ciclo unico deveria ter copiado ao menos 1 trade"
    print("OK: modo cycle (single-pass, usado pelo GitHub Actions) funcionando.\n")


if __name__ == "__main__":
    os.makedirs(config.DATA_DIR, exist_ok=True)
    test_discovery()
    test_ranking_and_cycle()
    print("Todos os testes offline passaram.")
