"""
Ponto de entrada do bot de simulacao.

Uso:
    python main.py discover  # descobre carteiras candidatas automaticamente
                              # (feed global de trades grandes) e escreve
                              # em data/watchlist.csv -- use isso se voce
                              # nao sabe/nao quer procurar enderecos manualmente
    python main.py rank      # avalia as carteiras de data/watchlist.csv e
                              # salva a watchlist qualificada (min. trades e
                              # win rate definidos em config.py)
    python main.py cycle     # roda UMA passada (busca trades novos, copia,
                              # checa resolucoes) e encerra -- ideal para
                              # rodar via cron/GitHub Actions (PC nao precisa
                              # ficar ligado)
    python main.py run       # loop continuo local (ctrl+C para parar) --
                              # equivalente a rodar "cycle" repetidamente

Fluxo recomendado para quem esta comecando:
    python main.py discover
    python main.py rank
    python main.py cycle      (ou configurar para rodar via cron/Actions)

Repita "discover" + "rank" semanalmente para manter a lista atualizada.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

import config
import polymarket_client as pm
import wallet_ranking
import wallet_discovery
from simulator import SimulationState, maybe_copy_trade, close_position


def cmd_discover() -> None:
    print("Escaneando feed global de trades recentes por carteiras "
          "com trades grandes e frequentes...")
    wallets = wallet_discovery.discover_and_save_watchlist()
    if not wallets:
        print("Nenhuma carteira encontrada com os criterios atuais. "
              "Tente diminuir min_trade_size_usd em wallet_discovery.py "
              "ou rode novamente mais tarde (o feed muda com o tempo).")
        return
    print(f"{len(wallets)} carteira(s) candidata(s) escrita(s) em "
          f"{config.WATCHLIST_FILE}:")
    for w in wallets:
        print(f"  {w}")
    print("\nProximo passo: python main.py rank")


def cmd_rank() -> None:
    print("Avaliando carteiras candidatas em", config.WATCHLIST_FILE, "...")
    results = wallet_ranking.build_qualified_wallet_list()
    if not results:
        print("Nenhuma carteira candidata encontrada. Rode "
              "'python main.py discover' primeiro (ou preencha "
              f"{config.WATCHLIST_FILE} manualmente).")
        return
    qualified = [r for r in results if r.qualifies]
    print(f"{len(results)} carteiras avaliadas, {len(qualified)} qualificadas.")
    for r in results:
        status = "QUALIFICADA" if r.qualifies else f"reprovada ({r.reason})"
        print(f"  {r.wallet_address[:10]}... -> {status}")


def _check_open_position_resolutions(state: SimulationState) -> None:
    """Para cada posicao aberta, verifica se o mercado ja foi resolvido e,
    se sim, fecha a posicao simulada com o resultado real.

    Busca o mercado especifico direto pelo conditionId (muito mais rapido e
    confiavel do que paginar todos os mercados fechados), e usa o
    outcome_index numerico (quando disponivel) em vez de comparar o texto
    do outcome, que e mais sujeito a nao bater exatamente."""
    for position_key in list(state.open_positions.keys()):
        pos = state.open_positions[position_key]
        market_id = pos["market_id"]

        try:
            match = pm.get_market_by_condition_id(market_id)
        except Exception as exc:
            print(f"[aviso] erro ao consultar mercado {market_id}: {exc}")
            continue

        if not match:
            print(f"[info] mercado {market_id[:12]}... ainda nao encontrado/"
                  f"resolvido na Gamma API.")
            continue

        is_closed = bool(match.get("closed", False))
        if not is_closed:
            continue  # mercado existe mas ainda esta ativo

        outcomes = match.get("outcomes") or []
        outcome_prices = match.get("outcomePrices") or []
        resolution_price = None

        idx = pos.get("outcome_index")
        if idx is not None:
            try:
                idx = int(idx)
                if idx < len(outcome_prices):
                    resolution_price = float(outcome_prices[idx])
            except (TypeError, ValueError, IndexError):
                resolution_price = None

        if resolution_price is None and pos.get("outcome") in outcomes:
            idx = outcomes.index(pos["outcome"])
            if idx < len(outcome_prices):
                try:
                    resolution_price = float(outcome_prices[idx])
                except (TypeError, ValueError):
                    resolution_price = None

        if resolution_price is not None:
            close_position(state, position_key, resolution_price)
            print(f"Posicao fechada: {pos['market_question'][:60]} -> "
                  f"preco final {resolution_price}")
        else:
            print(f"[aviso] mercado {market_id[:12]}... esta fechado mas nao "
                  f"consegui determinar o preco de resolucao da outcome "
                  f"'{pos.get('outcome')}' (outcomes={outcomes}, "
                  f"outcomePrices={outcome_prices}). Posicao mantida aberta "
                  f"para revisao manual.")


def run_single_cycle(state: SimulationState, qualified_wallets: list[str]) -> None:
    """Uma unica passada: busca trades novos das carteiras qualificadas,
    decide o que copiar, checa resolucoes de mercado, e salva o estado.
    Usada tanto pelo loop continuo ('run') quanto pela execucao agendada
    ('cycle', pensada para cron/GitHub Actions)."""
    for wallet in qualified_wallets:
        if state.is_wallet_paused(wallet):
            continue
        try:
            recent_trades = pm.get_trades_for_user(wallet, limit=50)
        except Exception as exc:
            print(f"[aviso] erro ao buscar trades de {wallet}: {exc}")
            continue

        for trade in recent_trades:
            copied = maybe_copy_trade(state, wallet, trade)
            if copied:
                print(f"[{datetime.now(timezone.utc):%H:%M:%S}] "
                      f"Copiado trade de {wallet[:10]}...: "
                      f"{trade.get('title', trade.get('conditionId'))}")

    _check_open_position_resolutions(state)
    state.save()

    print(f"Saldo fictício atual: ${state.cash_balance:.2f} | "
          f"Posicoes abertas: {len(state.open_positions)}")


def cmd_cycle() -> None:
    """Executa uma unica passada e encerra. Pensado para ser chamado por
    um agendador externo (cron, systemd timer, GitHub Actions) -- assim o
    bot nao depende de um processo continuo nem do PC ficar ligado."""
    qualified_wallets = wallet_ranking.load_qualified_wallets()
    if not qualified_wallets:
        print("Nenhuma carteira qualificada. Rode 'python main.py discover' "
              "e depois 'python main.py rank' primeiro.")
        return

    print(f"Monitorando {len(qualified_wallets)} carteira(s) qualificada(s). "
          f"Capital fictício: ${config.SIMULATED_CAPITAL_USD:.2f}")
    state = SimulationState()
    run_single_cycle(state, qualified_wallets)


def cmd_run() -> None:
    """Loop continuo local -- exige o processo (e o PC/VPS) ligado o tempo
    todo. Para rodar sem depender disso, use 'cycle' agendado externamente."""
    qualified_wallets = wallet_ranking.load_qualified_wallets()
    if not qualified_wallets:
        print("Nenhuma carteira qualificada. Rode 'python main.py discover' "
              "e depois 'python main.py rank' primeiro.")
        return

    print(f"Monitorando {len(qualified_wallets)} carteira(s) qualificada(s).")
    print(f"Capital fictício: ${config.SIMULATED_CAPITAL_USD:.2f} | "
          f"DRY_RUN={config.DRY_RUN}")

    state = SimulationState()

    try:
        while True:
            run_single_cycle(state, qualified_wallets)
            time.sleep(config.POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\nInterrompido pelo usuario. Estado salvo.")
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
