"""
Ponto de entrada do bot de simulação.

Uso:
    python main.py discover
    python main.py rank
    python main.py cycle
    python main.py run
    python main.py reset   ← NOVO: limpa posições e log, reinicia simulação

FIXES v3:
- Mecanismo de fecho com diagnóstico detalhado (imprime o que a API retorna)
- Posições com mais de MAX_POSITION_AGE_DAYS dias fechadas automaticamente
- Comando 'reset' para reiniciar a simulação do zero
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import config
import polymarket_client as pm
import wallet_ranking
import wallet_discovery
from simulator import SimulationState, maybe_copy_trade, close_position

MAX_POSITION_AGE_DAYS = 3  # jogos de LoL resolvem em horas — 3 dias é muito conservador


def cmd_discover() -> None:
    print("Escaneando feed global de trades recentes...")
    wallets = wallet_discovery.discover_and_save_watchlist()
    if not wallets:
        print("Nenhuma carteira encontrada. Tente novamente mais tarde.")
        return
    print(f"{len(wallets)} carteira(s) escrita(s) em {config.WATCHLIST_FILE}")
    for w in wallets:
        print(f"  {w}")


def cmd_rank() -> None:
    print("Avaliando carteiras candidatas...")
    results = wallet_ranking.build_qualified_wallet_list()
    if not results:
        print("Nenhuma carteira candidata. Rode 'discover' primeiro.")
        return
    qualified = [r for r in results if r.qualifies]
    print(f"{len(results)} avaliadas, {len(qualified)} qualificadas.")
    for r in results:
        status = "QUALIFICADA" if r.qualifies else f"reprovada ({r.reason})"
        print(f"  {r.wallet_address[:12]}... wr={r.win_rate:.0%} "
              f"({r.wins}W/{r.losses}L) resolved={r.resolved_trades} -> {status}")


def cmd_reset() -> None:
    """
    Reinicia a simulação do zero:
    - Apaga posições abertas
    - Apaga log de trades
    - Apaga IDs de trades já vistos (para poder re-copiar)
    - Apaga contador de perdas consecutivas
    - Mantém watchlist e qualified_wallets (não precisam ser refeitos)
    """
    files_to_reset = [
        config.OPEN_POSITIONS_FILE,
        config.TRADE_LOG_FILE,
        config.SEEN_TRADES_FILE,
        os.path.join(config.DATA_DIR, "consecutive_losses.json"),
    ]
    print("A reiniciar simulação...")
    for path in files_to_reset:
        if os.path.exists(path):
            os.remove(path)
            print(f"  removido: {path}")
    print(f"Simulação reiniciada. Capital fictício reposto a ${config.SIMULATED_CAPITAL_USD:.2f}")
    print("O próximo ciclo começa do zero.")


def _check_open_position_resolutions(state: SimulationState) -> None:
    """
    FIX v3: diagnóstico detalhado + timeout agressivo para jogos de LoL.
    LoL resolve em horas — qualquer posição com +3 dias é claramente fantasma.
    """
    now = datetime.now(timezone.utc)
    cutoff_age = now - timedelta(days=MAX_POSITION_AGE_DAYS)

    for position_key in list(state.open_positions.keys()):
        pos = state.open_positions[position_key]
        market_id = pos["market_id"]

        # Timeout: fecha posições muito antigas como perdidas
        opened_at_str = pos.get("opened_at", "")
        if opened_at_str:
            try:
                opened_at = datetime.fromisoformat(opened_at_str)
                if opened_at < cutoff_age:
                    print(f"  [timeout] {pos['market_question'][:55]}... "
                          f"aberta há +{MAX_POSITION_AGE_DAYS}d → fechando como perdida")
                    close_position(state, position_key, 0.0,
                                   reason=f"timeout {MAX_POSITION_AGE_DAYS}d")
                    continue
            except ValueError:
                pass

        # Busca o mercado com 4 estratégias de fallback
        try:
            market = pm.get_market_by_condition_id(market_id)
        except Exception as exc:
            print(f"  [erro] ao consultar {market_id[:16]}...: {exc}")
            continue

        if not market:
            print(f"  [aberto] {pos['market_question'][:55]}... → não encontrado na API")
            continue

        # Diagnóstico: o que a API está a devolver
        is_closed = (
            market.get("closed") is True
            or market.get("active") is False
            or str(market.get("closed", "")).lower() == "true"
        )

        if not is_closed:
            print(f"  [aberto] {pos['market_question'][:55]}... → API diz activo")
            continue

        # Mercado fechado — extrai o preço de resolução
        outcomes = market.get("outcomes") or []
        outcome_prices = market.get("outcomePrices") or []

        # outcomePrices pode vir como string JSON '["1","0"]'
        if isinstance(outcome_prices, str):
            try:
                outcome_prices = json.loads(outcome_prices)
            except Exception:
                outcome_prices = []

        resolution_price = None
        outcome_name = pos.get("outcome", "")

        if outcome_name in outcomes:
            idx = outcomes.index(outcome_name)
            if idx < len(outcome_prices):
                try:
                    resolution_price = float(outcome_prices[idx])
                except (TypeError, ValueError):
                    pass

        if resolution_price is not None:
            resultado = "GANHOU ✓" if resolution_price >= 0.99 else "PERDEU ✗"
            print(f"  [{resultado}] {pos['market_question'][:55]}... "
                  f"→ {outcome_name} @ {resolution_price}")
            close_position(state, position_key, resolution_price)
        else:
            # Mercado fechado mas sem preço claro → fecha como perdida por segurança
            print(f"  [fechado s/preço] {pos['market_question'][:55]}... "
                  f"outcomes={outcomes} prices={outcome_prices} → marcando perdida")
            close_position(state, position_key, 0.0,
                           reason="mercado fechado sem preço de resolução claro")


def run_single_cycle(state: SimulationState,
                     qualified_wallets: list[str]) -> None:
    print(f"\n{'─'*60}")
    print(f"[{datetime.now(timezone.utc):%H:%M:%S UTC}] Ciclo iniciado")
    print(f"Saldo: ${state.cash_balance:.2f} | "
          f"Posições abertas: {len(state.open_positions)}")

    # 1. Copiar trades novos das carteiras qualificadas
    trades_copiados = 0
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
            if maybe_copy_trade(state, wallet, trade):
                trades_copiados += 1
                print(f"  [copiado] {wallet[:12]}... → "
                      f"{trade.get('title', trade.get('conditionId', '?'))[:55]}")

    if trades_copiados == 0:
        print("  Sem trades novos para copiar.")

    # 2. Verificar resoluções de posições abertas
    if state.open_positions:
        print(f"\nA verificar {len(state.open_positions)} posição(ões) aberta(s)...")
        _check_open_position_resolutions(state)
    
    state.save()
    print(f"\nSaldo final: ${state.cash_balance:.2f} | "
          f"Posições abertas: {len(state.open_positions)}")


def cmd_cycle() -> None:
    # Bootstrap automático se não há carteiras qualificadas
    if not os.path.exists(config.QUALIFIED_WALLETS_FILE):
        print("qualified_wallets.json não existe → a fazer bootstrap...")
        os.makedirs(config.DATA_DIR, exist_ok=True)
        cmd_discover()
        cmd_rank()
    else:
        with open(config.QUALIFIED_WALLETS_FILE) as f:
            data = json.load(f)
        qualified_count = sum(1 for r in data if r.get("qualifies"))
        if qualified_count == 0:
            print("Nenhuma carteira qualificada → a fazer bootstrap...")
            cmd_discover()
            cmd_rank()

    qualified_wallets = wallet_ranking.load_qualified_wallets()
    if not qualified_wallets:
        print("Mesmo após bootstrap, nenhuma carteira qualificou. "
              "O feed pode estar sem carteiras com os critérios actuais.")
        return

    print(f"Monitorando {len(qualified_wallets)} carteira(s).")
    state = SimulationState()
    run_single_cycle(state, qualified_wallets)


def cmd_run() -> None:
    qualified_wallets = wallet_ranking.load_qualified_wallets()
    if not qualified_wallets:
        print("Nenhuma carteira qualificada. Rode 'discover' e 'rank' primeiro.")
        return
    print(f"Loop contínuo | {len(qualified_wallets)} carteira(s) | "
          f"Capital: ${config.SIMULATED_CAPITAL_USD:.2f}")
    state = SimulationState()
    try:
        while True:
            run_single_cycle(state, qualified_wallets)
            time.sleep(config.POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\nInterrompido. Estado salvo.")
        state.save()


def main() -> None:
    valid_commands = {"discover", "rank", "cycle", "run", "reset"}
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
    elif command == "reset":
        cmd_reset()
    else:
        cmd_run()


if __name__ == "__main__":
    main()
