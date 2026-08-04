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
    resolved_records = []  # (end_date_str_ou_None, outcome_won) -- para o calculo recente

    if positions:
        _save_debug_sample(positions[0])

    for p in positions:
        # Aceita varios nomes de campo possiveis para "posicao resolvida"
        is_resolved = bool(_first_present(
            p, ["redeemable", "resolved", "closed", "isResolved"], False
        ))
        if not is_resolved:
            continue

        # FIX v6 -- correcao importante confirmada com dados reais:
        # "realizedPnl" fica em 0 para posicoes PERDEDORAS (o resgate on-chain
        # so acontece quando ha algo a resgatar; perdas nao precisam de
        # "redeem", entao o campo nunca sai de 0). Usar so o sinal de
        # realizedPnl fazia perdas reais serem contadas como "neutras" em vez
        # de derrota, inflando o win rate. Agora usamos "curPrice" (preco
        # final da outcome: ~1 = venceu, ~0 = perdeu) como sinal primario,
        # com o PnL como reforco so quando curPrice nao for conclusivo.
        outcome_won = None
        cur_price = _first_present(p, ["curPrice", "currentPrice", "price"], None)
        if cur_price is not None:
            try:
                cur_price = float(cur_price)
                if cur_price >= 0.99:
                    outcome_won = True
                elif cur_price <= 0.01:
                    outcome_won = False
            except (TypeError, ValueError):
                pass

        if outcome_won is None:
            pnl = _first_present(p, ["cashPnl", "realizedPnl", "pnl", "profit"], None)
            if pnl is not None:
                try:
                    pnl = float(pnl)
                    if pnl > 0.01:
                        outcome_won = True
                    elif pnl < -0.01:
                        outcome_won = False
                except (TypeError, ValueError):
                    pass

        if outcome_won is None:
            pnl_zero += 1  # sem sinal conclusivo (nem curPrice nem PnL bateram) -- nao entra no calculo
            continue

        resolved += 1
        if outcome_won:
            wins += 1
        else:
            losses += 1

        end_date = _first_present(p, ["endDate", "end_date", "endDateIso"], None)
        resolved_records.append((end_date, outcome_won))

    # FIX: denominador = wins + losses (ignora pnl~0)
    denominator = wins + losses
    win_rate = (wins / denominator) if denominator > 0 else 0.0

    total_trades = len(trades)

    # NOVO (FIX v7): win rate RECENTE -- pega so os N trades resolvidos mais
    # recentes (ordenado por endDate, mais novo primeiro; registros sem data
    # ficam por ultimo, tratados como mais antigos). Uma carteira que "esfriou"
    # aparece aqui antes de o historico completo refletir isso.
    def _sort_key(record):
        end_date = record[0]
        return end_date if end_date else ""  # strings vazias ordenam por ultimo (mais antigas)

    sorted_records = sorted(resolved_records, key=_sort_key, reverse=True)
    recent_records = sorted_records[:config.RECENT_TRADES_WINDOW]
    recent_wins = sum(1 for _, won in recent_records if won)
    recent_losses = sum(1 for _, won in recent_records if not won)
    recent_sample_size = recent_wins + recent_losses
    win_rate_recent = (recent_wins / recent_sample_size) if recent_sample_size > 0 else 0.0

    # So usa o win rate recente para qualificar se houver amostra suficiente
    # (config.MIN_RECENT_SAMPLE) -- caso contrario, uma amostra recente
    # pequena e so ruido, e o historico completo e mais confiavel.
    used_recent_window = recent_sample_size >= config.MIN_RECENT_SAMPLE
    effective_win_rate = win_rate_recent if used_recent_window else win_rate

    # NOVO (FIX v8) -- amostra efetiva usada para decidir a qualificacao:
    # a janela recente se estiver em uso, senao o historico completo.
    eff_wins, eff_n = (recent_wins, recent_sample_size) if used_recent_window else (wins, denominator)
    win_rate_confidence_lower = _wilson_lower_bound(eff_wins, eff_n, z=config.WILSON_CONFIDENCE_Z)

    qualifies = True
    reasons = []
    if total_trades < config.MIN_TRADES_HISTORY:
        qualifies = False
        reasons.append(f"apenas {total_trades} trades no total (minimo {config.MIN_TRADES_HISTORY})")

    janela = f"ultimos {recent_sample_size} trades" if used_recent_window else "historico completo"

    # BUG REAL CORRIGIDO: antes disto, o minimo de amostra so olhava para o
    # total de trades (sempre alto para carteiras ativas), nao para quantos
    # JA RESOLVERAM com resultado claro -- por isso uma carteira com so 8
    # resolvidos (6W/1L) qualificava, e na pratica deu 0% de acerto ao ser
    # copiada de verdade.
    if eff_n < config.MIN_RESOLVED_TRADES:
        qualifies = False
        reasons.append(f"apenas {eff_n} trades resolvidos ({janela}, minimo "
                       f"{config.MIN_RESOLVED_TRADES}) -- amostra pequena demais para "
                       "confiar no win rate")
    elif win_rate_confidence_lower < config.MIN_WIN_RATE:
        qualifies = False
        # NOVO: a decisao agora usa o limite inferior de Wilson, nao o win
        # rate bruto -- por isso mostramos os dois no motivo, para ficar
        # claro por que uma carteira com win rate bruto aparentemente bom
        # (ex.: 85%) pode reprovar por ter amostra pequena demais.
        reasons.append(f"limite de confianca (Wilson) {win_rate_confidence_lower:.0%} "
                       f"abaixo do minimo {config.MIN_WIN_RATE:.0%} ({janela}, win rate "
                       f"bruto {effective_win_rate:.0%}, {eff_wins}W/{eff_n - eff_wins}L, "
                       f"{pnl_zero} neutros ignorados)")

    reason = "OK" if qualifies else "; ".join(reasons)

    return WalletMetrics(
        wallet_address=wallet_address,
        total_trades=total_trades,
        resolved_trades=resolved,
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        win_rate_confidence_lower=round(win_rate_confidence_lower, 4),
        total_volume_usd=round(total_volume, 2),
        qualifies=qualifies,
        reason=reason,
        win_rate_recent=win_rate_recent,
        recent_sample_size=recent_sample_size,
        used_recent_window=used_recent_window,
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
        janela_info = (f"recente({metrics.recent_sample_size})={metrics.win_rate_recent:.0%}"
                       if metrics.used_recent_window else "recente=amostra insuficiente")
        print(f"    trades={metrics.total_trades} resolved={metrics.resolved_trades} "
              f"wins={metrics.wins} losses={metrics.losses} "
              f"win_rate_geral={metrics.win_rate:.0%} {janela_info} "
              f"wilson_lower={metrics.win_rate_confidence_lower:.0%} "
              f"qualifies={metrics.qualifies}")

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