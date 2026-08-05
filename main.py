"""
Ponto de entrada do bot de simulação.

Uso:
    python main.py discover
    python main.py rank
    python main.py cycle
    python main.py run
    python main.py reset   ← limpa posições e log, reinicia simulação

FIXES v4:
- _check_open_position_resolutions agora usa PRIMEIRO os dados de
  /positions da própria carteira copiada (redeemable + sinal do
  realizedPnl) -- essa fonte já está comprovadamente funcionando (é o
  que alimenta o win rate real no wallet_ranking.py). A consulta à
  Gamma API por conditionId (que nunca encontrava o mercado) vira
  fallback secundário, e o timeout por idade continua como rede de
  segurança final.
- maybe_copy_trade (em simulator.py) agora respeita
  config.MAX_POSITIONS_PER_MARKET, evitando concentrar capital quando a
  carteira copiada faz várias compras seguidas no mesmo mercado.
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


def _first_present(d: dict, keys: list[str], default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


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


def _try_close_via_source_wallet_positions(state: SimulationState, position_key: str,
                                            pos: dict, positions_cache: dict,
                                            verbose: bool = False) -> bool:
    """
    FIX v4 -- estratégia PRIMÁRIA de fechamento.

    Em vez de perguntar à Gamma API "esse mercado já resolveu?" (que nunca
    encontrava o mercado nas tentativas anteriores), perguntamos à API de
    posições (/positions) da PRÓPRIA carteira que copiamos. Essa fonte já
    está comprovadamente funcionando -- é dela que vêm os win rates reais
    no ranking de carteiras (ex.: 77%, 85%, 80%).

    FIX v6 -- correção importante confirmada com dados reais: o campo
    "realizedPnl" fica em 0 para posições PERDEDORAS (o resgate on-chain só
    acontece quando há algo a resgatar; uma posição perdedora não precisa de
    "redeem", então nunca sai de 0, mesmo já resolvida havia semanas). Usar
    o sinal de realizedPnl como fizemos antes deixava essas perdas presas
    para sempre em "aberto".

    O sinal correto e confiável é "curPrice" (o preço atual/final da
    outcome): perto de 1.0 significa que a outcome VENCEU, perto de 0.0
    significa que PERDEU. Isso é verdade tanto para vitórias quanto
    derrotas, resgatadas ou não. Mantemos o sinal de PnL (cashPnl/
    realizedPnl) como reforço secundário só para o caso raro de curPrice
    vir ausente ou não numérico.

    Retorna True se a posição foi fechada.
    """
    source_wallet = pos["source_wallet"]
    market_id = pos["market_id"]

    # FIX v5, tentativa 1: lookup direto por condition_id -- mais confiavel
    # para carteiras muito ativas, onde a lista completa de posicoes (sem
    # filtro) pode nao trazer a posicao especifica que procuramos.
    source_position = None
    try:
        source_position = pm.get_position_for_market(source_wallet, market_id)
    except Exception as exc:
        print(f"  [erro] lookup direto de posicao falhou para "
              f"{source_wallet[:12]}.../{market_id[:16]}...: {exc}")

    if source_position is None and source_wallet not in positions_cache:
        try:
            raw_positions = pm.get_positions_for_user(source_wallet)
        except Exception as exc:
            print(f"  [erro] ao buscar posicoes de {source_wallet[:12]}...: {exc}")
            raw_positions = []
        lookup = {}
        for p in raw_positions:
            key = _first_present(p, ["conditionId", "condition_id", "market", "asset", "marketId"])
            if key:
                lookup[key] = p
        positions_cache[source_wallet] = lookup

    if source_position is None:
        source_position = positions_cache.get(source_wallet, {}).get(market_id)

    if not source_position:
        if verbose:
            print(f"    [posicoes] carteira {source_wallet[:12]}... nao tem (ou nao "
                  f"achamos) a posicao no mercado {market_id[:16]}... -- pode ja ter "
                  f"resgatado/vendido, ou nunca teve")
        return False  # a carteira de origem nao tem (ou nao achamos) essa posicao

    resolution_price = None
    resultado = None

    # FIX v8 (bug real corrigido): o sinal "redeemable/resolved/closed" NAO
    # serve como portao para detectar resolucao. Posicoes PERDEDORAS nunca
    # ficam redeemable=True (nao ha o que resgatar on-chain), entao exigir
    # esse flag antes de olhar o resultado bloqueava perdas reais de fechar
    # -- ficavam presas para sempre, so saindo pelo timeout de 3 dias.
    # O sinal confiavel e o curPrice (preco atual/final da outcome): ~1.0 =
    # venceu, ~0.0 = perdeu, valido tanto para vitorias quanto derrotas,
    # resgatadas ou nao (como o FIX v6 ja documentava). Por isso o curPrice
    # passa a ser consultado PRIMEIRO, sem depender do flag de resolved.
    # O flag continua apenas como pista auxiliar para o fallback de PnL.

    # Sinal PRIMARIO: curPrice (preco atual/final da outcome). Posicoes
    # ainda ABERTAS normalmente tem um preco intermediario (nao bate nos
    # limites abaixo), entao nao fecham aqui.
    cur_price = _first_present(source_position, ["curPrice", "currentPrice", "price"], None)
    if cur_price is not None:
        try:
            cur_price = float(cur_price)
            if cur_price >= 0.99:
                resolution_price, resultado = 1.0, "GANHOU ✓"
            elif cur_price <= 0.01:
                resolution_price, resultado = 0.0, "PERDEU ✗"
        except (TypeError, ValueError):
            pass

    # Fallback secundario: sinal do PnL. So usado se curPrice nao foi
    # conclusivo (ausente ou intermediario) E a posicao vier marcada como
    # resolvida pela API (redeemable/resolved/closed) -- ai sim o PnL e um
    # reforco legitimo.
    if resolution_price is None:
        is_resolved = bool(_first_present(
            source_position, ["redeemable", "resolved", "closed", "isResolved"], False
        ))
        if is_resolved:
            pnl = _first_present(source_position, ["cashPnl", "realizedPnl", "pnl", "profit"], None)
            if pnl is not None:
                try:
                    pnl = float(pnl)
                    if pnl > 0.01:
                        resolution_price, resultado = 1.0, "GANHOU ✓ (via PnL)"
                    elif pnl < -0.01:
                        resolution_price, resultado = 0.0, "PERDEU ✗ (via PnL)"
                except (TypeError, ValueError):
                    pass

    if resolution_price is None:
        if verbose:
            print(f"    [posicoes] posicao em {market_id[:16]}... marcada como resolvida "
                  f"mas sem curPrice/PnL conclusivo (curPrice={source_position.get('curPrice')})")
        return False  # marcado como resolvido mas sem sinal conclusivo -- deixa outras estrategias tentarem

    print(f"  [{resultado} via /positions da carteira de origem] "
          f"{pos['market_question'][:55]}...")
    close_position(state, position_key, resolution_price,
                   reason="resolvido (via posicoes da carteira de origem)")
    return True


def _try_close_via_market_trades(state: SimulationState, position_key: str,
                                  pos: dict, verbose: bool = False) -> bool:
    """
    FIX v9 -- estrategia de fechamento via trades do MERCADO (nova fonte
    confiavel, validada com dados reais em 2026-08-05).

    Contexto real (verificado):
      - A carteira de origem normalmente resgata/vende a posicao assim que o
        mercado resolve, entao ela SOME do /positions da carteira
        (confirmado: 0/16 dos mercados abertos estavam nas posicoes atuais
        da carteira, mesmo paginando 1000 posicoes).
      - A Gamma API ignora TODOS os filtros por identificador (conditionId,
        condition_ids, slug, title, id) e devolve sempre o mesmo mercado
        'padrao' de maior volume (confirmado) -- por isso
        get_market_by_condition_id nunca encontra o mercado certo.
      - A fonte que FUNCIONA: /trades?market=<conditionId> filtra de verdade
        os trades daquele mercado. Num mercado ja resolvido, o preco do
        ultimo trade de cada outcome reflete o resultado final: o vencedor
        negocia a ~1.0 e o perdedor a ~0.0. Mercados ainda abertos tem
        precos intermediarios (nao chegam a 0.99), entao nao fecham por
        engano.

    Retorna True se a posicao foi fechada.
    """
    market_id = pos["market_id"]
    our_outcome = pos.get("outcome")
    our_index = pos.get("outcome_index")
    try:
        trades = pm.get_trades_for_market(market_id, limit=25)
    except Exception as exc:
        if verbose:
            print(f"    [trades] erro ao buscar trades de {market_id[:16]}...: {exc}")
        return False

    if not trades:
        if verbose:
            print(f"    [trades] sem trades para {market_id[:16]}... -- mercado "
                  f"sem atividade ou nao encontrado")
        return False

    # O endpoint devolve os trades mais recentes primeiro. Procuramos o
    # outcome cujo preco ja chegou ao valor final (~1.0 = venceu).
    winner = None
    winner_index = None
    for t in trades:
        price = t.get("price")
        if price is None:
            continue
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        if price >= 0.99:
            winner = t.get("outcome") or winner
            idx = t.get("outcomeIndex")
            if idx is not None:
                try:
                    winner_index = int(idx)
                except (TypeError, ValueError):
                    pass
            break

    if winner is None:
        if verbose:
            print(f"    [trades] {market_id[:16]}... sem outcome com preco >= 0.99 "
                  f"-- mercado provavelmente ainda nao resolveu")
        return False  # ainda nao resolvido / sem sinal conclusivo

    # Decide o resultado da NOSSA aposta: por nome do outcome (ex.: "No",
    # "Gabriela Ruse", "Over"), com fallback pelo indice.
    our_won = False
    if our_outcome:
        our_won = str(our_outcome).strip().lower() == str(winner).strip().lower()
    elif our_index is not None and winner_index is not None:
        our_won = our_index == winner_index
    else:
        if verbose:
            print(f"    [trades] {market_id[:16]}... sem como comparar o nosso "
                  f"outcome ({our_outcome!r}) com o vencedor ({winner!r})")
        return False

    resolution_price = 1.0 if our_won else 0.0
    resultado = "GANHOU ✓" if our_won else "PERDEU ✗"
    print(f"  [{resultado} via trades do mercado] {pos['market_question'][:55]}...")
    close_position(state, position_key, resolution_price,
                   reason="resolvido (via trades do mercado)")
    return True


def _try_close_via_gamma_api(state: SimulationState, position_key: str, pos: dict,
                              verbose: bool = False) -> bool:
    """
    NOVO: promovida de fallback secundario para fonte CO-IGUAL/preferencial.

    Por que a mudanca: a posicao da carteira de origem em /positions pode
    simplesmente DESAPARECER se ela ja resgatou (redeem) o resultado --
    nesse caso a estrategia via /positions nunca vai ter dados para
    trabalhar, para sempre. Ja a Gamma API descreve o MERCADO em si (nao a
    carteira): uma vez resolvido, o campo "closed" e o "outcomePrices"
    final ficam registados permanentemente, independente do que qualquer
    carteira especifica faca com a posicao dela. E a fonte mais duravel
    disponivel -- por isso tentamos ela primeiro agora.
    """
    market_id = pos["market_id"]
    try:
        market = pm.get_market_by_condition_id(market_id)
    except Exception as exc:
        if verbose:
            print(f"    [gamma] erro ao consultar {market_id[:16]}...: {exc}")
        return False

    if not market:
        if verbose:
            print(f"    [gamma] mercado {market_id[:16]}... nao encontrado na Gamma API")
        return False

    is_closed = (
        market.get("closed") is True
        or market.get("active") is False
        or str(market.get("closed", "")).lower() == "true"
    )
    if not is_closed:
        if verbose:
            print(f"    [gamma] mercado {market_id[:16]}... encontrado mas ainda "
                  f"active={market.get('active')} closed={market.get('closed')}")
        return False

    outcomes = market.get("outcomes") or []
    outcome_prices = market.get("outcomePrices") or []
    if isinstance(outcome_prices, str):
        try:
            outcome_prices = json.loads(outcome_prices)
        except Exception:
            outcome_prices = []

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

    if resolution_price is None:
        if verbose:
            print(f"    [gamma] mercado {market_id[:16]}... fechado mas sem "
                  f"outcomePrices conclusivo (outcomes={outcomes})")
        return False

    resultado = "GANHOU ✓" if resolution_price >= 0.99 else "PERDEU ✗"
    print(f"  [{resultado} via Gamma API] {pos['market_question'][:55]}...")
    close_position(state, position_key, resolution_price,
                   reason="resolvido (via Gamma API)")
    return True


# NOVO: se uma posicao ja esta aberta ha mais tempo que isto sem nenhuma
# estrategia conseguir resolve-la, passamos a imprimir diagnostico detalhado
# de cada tentativa -- para uma posicao recem-aberta isso so faria ruido nos
# logs (o mercado provavelmente ainda nem terminou de verdade), mas para uma
# que ja devia ter resolvido ha muito, o diagnostico ajuda a perceber
# exatamente em qual fonte/campo a informacao esta a faltar.
VERBOSE_DIAGNOSTIC_AGE_HOURS = 2


def _check_open_position_resolutions(state: SimulationState) -> None:
    """
    Para cada posição aberta, tenta fechar em 3 estratégias.

    FIX v7 -- BUG REAL CORRIGIDO (ciclos deixaram de fechar QUALQUER posição
    depois da v6): promover a Gamma API para "primeira tentativa sempre"
    multiplicou por ~25x (MAX_OPEN_POSITIONS) o volume de chamadas a Gamma
    a cada ciclo -- cada posição passou a fazer ate 4 estrategias em
    cascata (get_market_by_condition_id) mesmo quando a via rapida
    (/positions da carteira) teria resolvido na hora. Isso e lento o
    suficiente para o ciclo não terminar dentro do intervalo entre
    execuções agendadas (POLL_INTERVAL/cron), o que faz o próximo cron
    disparar por cima do anterior (não há "concurrency:" no workflow) --
    de fora, isso aparenta "nada fecha nunca", porque execuções se
    sobrepõem e nenhuma chega a terminar e fazer commit/push do resultado.

    A ordem agora é ADAPTATIVA por idade da posição:
      - Posição jovem (< VERBOSE_DIAGNOSTIC_AGE_HOURS): tenta primeiro
        /positions da carteira de origem (rápido, e na maioria dos casos
        ainda tem os dados, porque a carteira normalmente só resgata a
        posição depois de um tempo). Só cai para a Gamma API se isso
        falhar.
      - Posição já "presa" há tempo (>= VERBOSE_DIAGNOSTIC_AGE_HOURS): aí
        sim vale a pena pagar o custo extra da Gamma primeiro, porque é
        exatamente o caso em que a carteira de origem já deve ter
        resgatado e sumido do /positions.
      3. Timeout por idade (rede de segurança final, marca como perdida)
    """
    now = datetime.now(timezone.utc)
    cutoff_age = now - timedelta(days=MAX_POSITION_AGE_DAYS)
    positions_cache: dict = {}  # wallet -> {market_id: position}

    for position_key in list(state.open_positions.keys()):
        pos = state.open_positions[position_key]

        age_hours = None
        opened_at_str_check = pos.get("opened_at", "")
        if opened_at_str_check:
            try:
                age_hours = (now - datetime.fromisoformat(opened_at_str_check)).total_seconds() / 3600
            except ValueError:
                pass
        is_stuck = age_hours is not None and age_hours >= VERBOSE_DIAGNOSTIC_AGE_HOURS
        verbose = is_stuck
        if verbose:
            print(f"  [diagnostico] {pos['market_question'][:55]}... aberta há "
                  f"{age_hours:.1f}h sem resolver -- a detalhar cada tentativa:")

        if is_stuck:
            # posição já presa há tempo -- a carteira de origem provavelmente
            # já resgatou/vendeu (some do /positions), então a fonte mais
            # confiável é a dos trades do MERCADO (válida sempre, pois o
            # preço final do mercado resolve). Gamma ficou como último
            # recurso (confirmado que ela ignora filtros por id).
            if _try_close_via_market_trades(state, position_key, pos, verbose=verbose):
                continue
            if _try_close_via_source_wallet_positions(state, position_key, pos, positions_cache,
                                                        verbose=verbose):
                continue
            if _try_close_via_gamma_api(state, position_key, pos, verbose=verbose):
                continue
        else:
            # posição jovem -- via rápida primeiro (/positions da carteira,
            # que ainda deve ter os dados), depois trades do mercado (1
            # chamada só), e só então a Gamma.
            if _try_close_via_source_wallet_positions(state, position_key, pos, positions_cache,
                                                        verbose=verbose):
                continue
            if _try_close_via_market_trades(state, position_key, pos, verbose=verbose):
                continue
            if _try_close_via_gamma_api(state, position_key, pos, verbose=verbose):
                continue

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

        print(f"  [aberto] {pos['market_question'][:55]}... → ainda não resolvido em nenhuma fonte")


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