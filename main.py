"""
Ponto de entrada do bot de simulacao.

Uso:
    python main.py discover
    python main.py rank
    python main.py cycle
    python main.py run

FIXES v2:
- Resolucao de posicoes: busca mercado por conditionId directamente na Gamma API
  (em vez de paginar todos os mercados fechados — muito mais fiavel e rapido)
- Fallback: se o conditionId nao encontrar, tenta pelo campo 'id'
- Posicoes com mais de MAX_POSITION_AGE_DAYS dias sao fechadas como perdidas
  (evita posicoes fantasma que ficam abertas para sempre)
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone

import config
import polymarket_client as pm
import wallet_ranking
import wallet_discovery
from simulator import SimulationState, maybe_copy_trade, close_position

# Posicoes abertas ha mais de N dias sem resolucao sao fechadas como perdidas
MAX_POSITION_AGE_DAYS = 14


def cmd_discover() -> None:
    print("Escaneando feed global de trades recentes...")
    wallets = wallet_discovery.discover_and_save_watchlist()
    if not wallets:
        print("Nenhuma carteira encontrada. Tente diminuir min_trade_size_usd "
              "em wallet_discovery.py ou rode novamente mais tarde.")
        return
    print(f"{len(wallets)} carteira(s) escrita(s) em {config.WATCHLIST_FILE}")
    for w in wallets:
        print(f"  {w}")
    print("\nProximo passo: python main.py rank")


def cmd_rank() -> None:
    print("Avaliando carteiras candidatas em", config.WATCHLIST_FILE, "...")
    results = wallet_ranking.build_qualified_wallet_list()
    if not results:
        print("Nenhuma carteira candidata. Rode 'python main.py discover' primeiro.")
        return
    qualified = [r for r in results if r.qualifies]
    print(f"{len(results)} avaliadas, {len(qualified)} qualificadas.")
    for r in results:
        status = "QUALIFICADA" if r.qualifies else f"reprovada ({r.reason})"
        print(f"  {r.wallet_address[:12]}... win_rate={r.win_rate:.0%} "
              f"resolved={r.resolved_trades} -> {status}")


def _fetch_market_by_condition_id(condition_id: str) -> dict | None:
    """
    FIX: busca um mercado especifico pelo conditionId directamente,
    em vez de paginar todos os mercados fechados.
    Muito mais rapido e fiavel para detectar resolucoes.
    """
    try:
        # Tenta endpoint directo por conditionId
        markets = pm._get(
            f"{config.GAMMA_API_BASE}/markets",
            params={"conditionId": condition_id, "limit": 5}
        )
        if markets:
            return markets[0]
    except Exception:
        pass

    # Fallback: tenta pelo campo closed=True paginando menos (so 2 paginas)
    try:
        for closed_flag in [True, False]:
            page = pm.get_markets(active=not closed_flag, closed=closed_flag,
                                   limit=500, offset=0)
            match = next(
                (m for m in page
                 if m.get("conditionId") == condition_id or m.get("id") == condition_id),
                None
            )
            if match:
                return match
    except Exception:
        pass

    return None


def _check_open_position_resolutions(state: SimulationState) -> None:
    """
    Verifica se posicoes abertas ja foram resolvidas.

    FIX principal: em vez de paginar TODOS os mercados fechados (lento e
    pouco fiavel), busca cada mercado pelo seu conditionId directamente.

    Tambem fecha posicoes "fantasma" com mais de MAX_POSITION_AGE_DAYS dias.
    """
    now = datetime.now(timezone.utc)
    cutoff_age = now - timedelta(days=MAX_POSITION_AGE_DAYS)

    for position_key in list(state.open_positions.keys()):
        pos = state.open_positions[position_key]
        market_id = pos["market_id"]

        # Fecho por idade maxima (posicao fantasma)
        opened_at_str = pos.get("opened_at", "")
        if opened_at_str:
            try:
                opened_at = datetime.fromisoformat(opened_at_str)
                if opened_at < cutoff_age:
                    print(f"[timeout] Posicao com mais de {MAX_POSITION_AGE_DAYS} dias "
                          f"sem resolucao: {pos['market_question'][:50]} — fechando como perdida")
                    close_position(state, position_key, 0.0,
                                   reason=f"timeout {MAX_POSITION_AGE_DAYS}d sem resolucao")
                    continue
            except ValueError:
                pass

        # Busca o mercado directamente pelo conditionId
        market = _fetch_market_by_condition_id(market_id)
        if not market:
            print(f"  [resolve] mercado {market_id[:16]}... nao encontrado ainda")
            continue

        # Verifica se esta realmente fechado/resolvido
        is_closed = (
            market.get("closed") is True
            or market.get("active") is False
            or market.get("resolved") is True
        )
        if not is_closed:
            continue  # mercado ainda aberto

        # Extrai o preco de resolucao para o outcome desta posicao
        outcomes = market.get("outcomes") or []
        outcome_prices = market.get("outcomePrices") or []

        # outcomePrices pode vir como string JSON "[\"1\",\"0\"]"
        if isinstance(outcome_prices, str):
            import json as _json
            try:
                outcome_prices = _json.loads(outcome_prices)
            except Exception:
                outcome_prices = []

        resolution_price = None
        if pos["outcome"] in outcomes:
            idx = outcomes.index(pos["outcome"])
            if idx < len(outcome_prices):
                try:
                    resolution_price = float(outcome_prices[idx])
                except (TypeError, ValueError):
                    resolution_price = None

        if resolution_price is None:
            # Mercado fechado mas sem preco claro — marca como perdida por seguranca
            print(f"  [resolve] {pos['market_question'][:50]} — fechado sem preco, "
                  f"marcando como perdida")
            close_position(state, position_key, 0.0, reason="mercado fechado sem preco de resolucao")
        else:
            resultado = "GANHOU" if resolution_price >= 0.99 else "PERDEU"
            print(f"  [resolve] {pos['market_question'][:50]} "
                  f"({pos['outcome']}) → {resultado} (preco={resolution_price})")
            close_position(state, position_key, resolution_price)


def run_single_cycle(state: SimulationState, qualified_wallets: list[str]) -> None:
    """Uma unica passada: copia trades novos e fecha posicoes resolvidas."""
    for wallet in qualified_wallets:
        if state.is_wallet_paused(wallet):
            print(f"  [pausada] {wallet[:12]}... (muitas perdas seguidas)")
            continue
        try:
            recent_trades = pm.get_trades_for_user(wallet, limit=50)
        except Exception as exc:
            print(f"  [erro] ao buscar trades de {wallet[:12]}...: {exc}")
            continue

        for trade in recent_trades:
            copied = maybe_copy_trade(state, wallet, trade)
            if copied:
                print(f"  [copiado] {wallet[:12]}... → "
                      f"{trade.get('title', trade.get('conditionId', '?'))[:60]}")

    _check_open_position_resolutions(state)
    state.save()

    print(f"\nSaldo ficticio: ${state.cash_balance:.2f} | "
          f"Posicoes abertas: {len(state.open_positions)}")


def cmd_cycle() -> None:
    qualified_wallets = wallet_ranking.load_qualified_wallets()
    if not qualified_wallets:
        print("Nenhuma carteira qualificada. Rode 'discover' e 'rank' primeiro.")
        return
    print(f"Monitorando {len(qualified_wallets)} carteira(s). "
          f"Capital ficticio: ${config.SIMULATED_CAPITAL_USD:.2f}")
    state = SimulationState()
    run_single_cycle(state, qualified_wallets)


def cmd_run() -> None:
    qualified_wallets = wallet_ranking.load_qualified_wallets()
    if not qualified_wallets:
        print("Nenhuma carteira qualificada. Rode 'discover' e 'rank' primeiro.")
        return
    print(f"Monitorando {len(qualified_wallets)} carteira(s). "
          f"Capital ficticio: ${config.SIMULATED_CAPITAL_USD:.2f} | DRY_RUN={config.DRY_RUN}")
    state = SimulationState()
    try:
        while True:
            run_single_cycle(state, qualified_wallets)
            time.sleep(config.POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\nInterrompido. Estado salvo.")
        state.save()


def main() -> None:
    valid_commands = {"discover", "rank", "cycle", "run"}
    if len(sys.argv) < 2 or sys.argv[1] not in valid_commands:
        print(__doc__)
        sys.exit(1)
    command = sys.argv[1]
    if command == "discover":
        cmd_discover()
    elif command == "rank":
        cmd_rank()
    elif command == "cycle":
        cmd_cycle()
    else:
        cmd_run()


if __name__ == "__main__":
    main()
