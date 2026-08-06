"""
Filtro e ranking de carteiras para copy trading.

FIXES v2:
- Correcao do calculo de win rate: o problema era que a API do Polymarket
  retorna posicoes com pnl=0 para mercados ainda nao resolvidos, que eram
  contadas como nem win nem loss, mas o denominador ficava errado.
  Agora: win rate = wins / (wins + losses) — ignora posicoes com pnl==0.
- Adiciona campo "win_rate_display" para debug (wins/losses/empates visiveis)
- Guarda amostra de posicao em _debug para facilitar diagnostico

FIX v4:
- O arquivo de debug so era escrito "se nao existir" -- como o projeto ja
  entregava um arquivo placeholder, ele nunca era substituido pela amostra
  real. Agora sobrescreve sempre que encontra uma posicao de verdade (com
  campos alem da nota inicial), garantindo que reflita a API de verdade.
"""
from __future__ import annotations

import csv
import json
import math
import os
import time
from dataclasses import dataclass, asdict
from typing import Optional

import config
import polymarket_client as pm


def _wilson_lower_bound(wins: int, n: int, z: float = 1.96) -> float:
    """
    Limite inferior do intervalo de confianca de Wilson para uma proporcao
    (aqui, o win rate). Ao contrario do win rate bruto, isto penaliza
    automaticamente amostras pequenas: uma carteira com 6W/1L (85.7% bruto,
    so 7 resolvidos) tem um limite inferior bem mais baixo do que uma com
    68W/20L (77.3% bruto, 88 resolvidos) -- reflete que a segunda amostra e
    muito mais confiavel, mesmo tendo um win rate bruto menor.

    z=1.96 corresponde a 95% de confianca (config.WILSON_CONFIDENCE_Z).
    """
    if n <= 0:
        return 0.0
    phat = wins / n
    z2 = z * z
    denom = 1 + z2 / n
    center = phat + z2 / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) / n) + (z2 / (4 * n * n)))
    return max(0.0, (center - margin) / denom)


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
    # NOVO (FIX v7): win rate calculado so com os trades resolvidos mais
    # recentes (config.RECENT_TRADES_WINDOW), para detectar carteiras que
    # "esfriaram" mais rapido do que o historico completo mostraria.
    win_rate_recent: float = 0.0
    recent_sample_size: int = 0
    used_recent_window: bool = False
    # NOVO (FIX v8): limite inferior de confianca de Wilson sobre a amostra
    # efetiva (recente, se usada; senao o historico completo). E este valor
    # -- nao o win_rate/win_rate_recent bruto -- que decide a qualificacao.
    win_rate_confidence_lower: float = 0.0
    # NOVO (FIX v14): copiabilidade + estimativa de EV (valor esperado).
    # copyable_ratio = fracao dos trades recentes que o bot CONSEGUE copiar
    # (outcome valido, sem keyword excluida, preco <= MAX_ENTRY_PRICE).
    # ev_estimate = retorno medio por unidade apostada nos trades resolvidos
    # (positivo = edge real, negativo = prejuizo esperado).
    copyable_ratio: float = 0.0
    ev_estimate: float = 0.0


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


def _save_debug_sample(position: dict) -> None:
    """FIX v4: sempre sobrescreve com uma amostra real (nao so na primeira
    vez), para o arquivo nunca ficar preso no placeholder inicial."""
    os.makedirs(config.DATA_DIR, exist_ok=True)
    debug_path = os.path.join(config.DATA_DIR, "_debug_sample_position.json")
    try:
        with open(debug_path, "w") as f:
            json.dump(position, f, indent=2)
    except Exception:
        pass  # debug e best-effort, nunca deve quebrar o ranking


def _load_resolutions_cache() -> dict:
    if not os.path.exists(config.MARKET_RESOLUTIONS_FILE):
        return {}
    try:
        with open(config.MARKET_RESOLUTIONS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_resolutions_cache(resolutions: dict) -> None:
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(config.MARKET_RESOLUTIONS_FILE, "w") as f:
            json.dump(resolutions, f, indent=2)
    except Exception:
        pass  # cache e best-effort, nunca deve quebrar o ranking


def _resolve_market_winner(condition_id: str, resolutions: dict,
                            throttle: float = 0.0) -> Optional[str]:
    """
    Descobre o outcome VENCEDOR de um mercado (para saber se cada trade da
    carteira ganhou ou perdeu). Fonte: /trades?market=<conditionId> -- num
    mercado ja resolvido, o preco do ultimo trade do vencedor e ~1.0
    (verificado). Usa cache local para nao refazer chamadas, e uma pequena
    pausa (throttle) para nao estourar o rate limit da API.
    """
    if condition_id in resolutions:
        return resolutions[condition_id]
    winner = None
    try:
        trades = pm.get_trades_for_market(condition_id, limit=25)
        for t in trades:
            p = t.get("price")
            try:
                p = float(p)
            except (TypeError, ValueError):
                continue
            if p >= 0.99:
                winner = t.get("outcome")
                break
    except Exception:
        winner = None
    if winner is not None:
        resolutions[condition_id] = winner
    if throttle > 0:
        time.sleep(throttle)
    return winner


def _is_copyable(trade: dict) -> bool:
    """
    FIX v14: um trade e "copiavel" se o bot CONSEGUE segui-lo de verdade
    (mesmos guards da copia em simulator.maybe_copy_trade). Carteiras que
    qualificam pelo win rate mas so operam mercados que o bot nao consegue
    seguir (ex.: scalpers de "Up or Down" a 0.99) ficam com copyable_ratio
    baixo e sao desqualificadas.
    """
    outcome = trade.get("outcome")
    if not outcome or not str(outcome).strip():
        return False  # parlay/multi-leg sem outcome
    title = (trade.get("title") or trade.get("question") or "").lower()
    for kw in config.EXCLUDED_MARKET_KEYWORDS:
        if kw in title:
            return False
    try:
        price = float(trade.get("price", 0) or 0)
    except (TypeError, ValueError):
        return False
    if price <= 0 or price >= 1 or price > config.MAX_ENTRY_PRICE:
        return False
    return True


def _compute_metrics(wallet_address: str, trades: list[dict],
                      resolutions: dict) -> WalletMetrics:
    """
    FIX v11 -- win rate VERDADEIRO via historico de trades.

    O calculo anterior usava /positions (o que a carteira AINDA tem). Isso e
    viesado: vitorias sao resgatadas e somem do /positions, sobrando quase
    so perdas (verificado com dados reais: 572/572 posicoes atuais da
    carteira principal eram PERDAS, mas o historico real dela tinha ~77% de
    acerto). Por isso o win rate "na foto atual" aparecia como 0%.

    Agora calculamos pelo HISTORICO REAL de trades (/trades?user=): para
    cada mercado em que a carteira negociou, descobrimos o vencedor (via
    /trades?market=, com cache) e marcamos cada trade como GANHOU/PERDEU
    conforme o outcome que ela apostou. Isso inclui as vitorias ja
    resgatadas, dando um win rate fiel ao desempenho real.
    """
    total_volume = 0.0
    for t in trades:
        try:
            size = float(_first_present(t, ["size", "amount"], 0) or 0)
            price = float(_first_present(t, ["price"], 0) or 0)
            total_volume += size * price
        except (TypeError, ValueError):
            pass

    total_trades = len(trades)

    # So usamos os RANK_TRADES_WINDOW trades mais recentes para o win rate
    # (a API devolve do mais novo para o mais antigo) -- mantem o custo de
    # API baixo (resolve poucos mercados) e o win rate "de agora" relevante.
    window_trades = trades[:config.RANK_TRADES_WINDOW]

    # FIX v14: quantos dos trades recentes o bot consegue copiar de verdade
    copyable_count = sum(1 for t in window_trades if _is_copyable(t))

    resolved = 0
    wins = 0
    losses = 0
    pending = 0  # mercados ainda sem resolucao conclusiva
    resolved_records = []  # (timestamp_unix, outcome_won) para a janela recente
    ev_records = []        # (entry_price, outcome_won) para o EV estimado

    # Agrupa por mercado para resolver cada um UMA vez (cache compartilhada).
    by_market: dict = {}
    for t in window_trades:
        cid = t.get("conditionId") or t.get("market")
        if not cid:
            continue
        by_market.setdefault(cid, []).append(t)

    for cid, tlist in by_market.items():
        winner = _resolve_market_winner(cid, resolutions,
                                        throttle=config.RANK_API_DELAY_SECONDS)
        if winner is None:
            pending += 1
            continue
        for t in tlist:
            traded_outcome = t.get("outcome")
            ts = t.get("timestamp")
            if traded_outcome is None:
                continue
            outcome_won = (str(traded_outcome).strip().lower() == str(winner).strip().lower())
            resolved += 1
            if outcome_won:
                wins += 1
            else:
                losses += 1
            resolved_records.append((ts, outcome_won))
            ev_records.append((_first_present(t, ["price"], 0), outcome_won))

    copyable_ratio = (copyable_count / len(window_trades)) if window_trades else 0.0

    # FIX v14: EV estimado por unidade apostada (payoff ~ (1-entry)/entry se
    # ganhou, -1 se perdeu). Positivo = edge real; negativo = prejuizo.
    ev_estimate = 0.0
    if ev_records:
        total = 0.0
        for entry, won in ev_records:
            try:
                entry = float(entry)
            except (TypeError, ValueError):
                entry = 0.0
            if entry <= 0:
                entry = 0.5  # sem preco valido, assume neutro
            total += ((1.0 - entry) / entry) if won else -1.0
        ev_estimate = total / len(ev_records)

    denominator = wins + losses
    win_rate = (wins / denominator) if denominator > 0 else 0.0

    # Janela recente pelos timestamps dos trades (mais novo primeiro).
    sorted_records = sorted(resolved_records, key=lambda r: r[0] or 0, reverse=True)
    recent_records = sorted_records[:config.RECENT_TRADES_WINDOW]
    recent_wins = sum(1 for _, won in recent_records if won)
    recent_losses = sum(1 for _, won in recent_records if not won)
    recent_sample_size = recent_wins + recent_losses
    win_rate_recent = (recent_wins / recent_sample_size) if recent_sample_size > 0 else 0.0

    # So usa o win rate recente se houver amostra suficiente.
    used_recent_window = recent_sample_size >= config.MIN_RECENT_SAMPLE
    effective_win_rate = win_rate_recent if used_recent_window else win_rate

    eff_wins, eff_n = (recent_wins, recent_sample_size) if used_recent_window else (wins, denominator)
    win_rate_confidence_lower = _wilson_lower_bound(eff_wins, eff_n, z=config.WILSON_CONFIDENCE_Z)

    qualifies = True
    reasons = []
    if total_trades < config.MIN_TRADES_HISTORY:
        qualifies = False
        reasons.append(f"apenas {total_trades} trades no total (minimo {config.MIN_TRADES_HISTORY})")

    # FIX v14: se a maioria dos trades recentes nao e copiavel pelo bot, a
    # carteira e um scalper de mercados rapidos -- o win rate dela nao vale
    # para copy trading (o bot nao consegue seguir).
    if copyable_ratio < config.MIN_COPYABLE_RATIO:
        qualifies = False
        reasons.append(f"apenas {copyable_ratio:.0%} dos trades recentes sao "
                       "copiaveis pelo bot (muito scalping/mercados rapidos)")

    janela = f"ultimos {recent_sample_size} trades" if used_recent_window else "historico completo"

    if eff_n < config.MIN_RESOLVED_TRADES:
        qualifies = False
        reasons.append(f"apenas {eff_n} trades resolvidos ({janela}, minimo "
                       f"{config.MIN_RESOLVED_TRADES}) -- amostra pequena demais para "
                       "confiar no win rate")
    elif win_rate_confidence_lower < config.MIN_WIN_RATE:
        qualifies = False
        reasons.append(f"limite de confianca (Wilson) {win_rate_confidence_lower:.0%} "
                       f"abaixo do minimo {config.MIN_WIN_RATE:.0%} ({janela}, win rate "
                       f"bruto {effective_win_rate:.0%}, {eff_wins}W/{eff_n - eff_wins}L, "
                       f"{pending} mercados ainda sem resolucao)")

    reason = "OK" if qualifies else "; ".join(reasons)

    return WalletMetrics(
        wallet_address=wallet_address,
        total_trades=total_trades,
        resolved_trades=resolved,
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        win_rate_confidence_lower=round(win_rate_confidence_lower, 4),
        copyable_ratio=round(copyable_ratio, 4),
        ev_estimate=round(ev_estimate, 4),
        total_volume_usd=round(total_volume, 2),
        qualifies=qualifies,
        reason=reason,
        win_rate_recent=win_rate_recent,
        recent_sample_size=recent_sample_size,
        used_recent_window=used_recent_window,
    )


def build_qualified_wallet_list(save: bool = True) -> list[WalletMetrics]:
    candidates = _load_candidate_wallets()
    resolutions = _load_resolutions_cache()
    results: list[WalletMetrics] = []
    for wallet in candidates:
        print(f"  avaliando {wallet[:12]}...")
        try:
            trades = pm.get_all_trades_for_user(wallet)
        except Exception as exc:
            print(f"    [erro] ao buscar trades de {wallet[:12]}...: {exc}")
            continue
        metrics = _compute_metrics(wallet, trades, resolutions)
        results.append(metrics)
        janela_info = (f"recente({metrics.recent_sample_size})={metrics.win_rate_recent:.0%}"
                       if metrics.used_recent_window else "recente=amostra insuficiente")
        print(f"    trades={metrics.total_trades} resolved={metrics.resolved_trades} "
              f"wins={metrics.wins} losses={metrics.losses} "
              f"win_rate_geral={metrics.win_rate:.0%} {janela_info} "
              f"copiavel={metrics.copyable_ratio:.0%} ev={metrics.ev_estimate:+.2f} "
              f"wilson_lower={metrics.win_rate_confidence_lower:.0%} "
              f"qualifies={metrics.qualifies}")

    if save:
        _save_resolutions_cache(resolutions)
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