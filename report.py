"""
Gerador de relatorio semanal a partir do log de trades simulados.

Uso:
    python report.py            # relatorio dos ultimos 7 dias
    python report.py --days 30  # relatorio do periodo inteiro (1 mes)

Gera um arquivo markdown em reports/relatorio_<data>.md com:
- retorno acumulado no periodo
- win rate
- maior ganho / maior perda
- drawdown maximo (queda do pico ao vale do saldo)
- trades por carteira copiada (para ver quais valeu a pena seguir)
- sugestoes automaticas simples (ex.: pausar carteira com performance ruim)

O formato de entrega (email, Telegram, etc.) ainda nao foi decidido -- por
enquanto ele so salva o arquivo localmente. Da pra plugar um envio depois
sem mudar a logica de calculo.
"""
from __future__ import annotations

import argparse
import csv
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import config


def _load_trade_log() -> list[dict]:
    if not os.path.exists(config.TRADE_LOG_FILE):
        return []
    with open(config.TRADE_LOG_FILE, newline="") as f:
        return list(csv.DictReader(f))


def _parse_ts(row: dict) -> datetime:
    return datetime.fromisoformat(row["timestamp"])


def generate_report(days: int = 7) -> str:
    rows = _load_trade_log()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    period_rows = [r for r in rows if _parse_ts(r) >= cutoff]

    closes = [r for r in period_rows if r["action"] == "CLOSE"]
    opens = [r for r in period_rows if r["action"] == "OPEN"]

    total_pnl = sum(float(r["pnl_usd"] or 0) for r in closes)
    wins = [r for r in closes if float(r["pnl_usd"] or 0) > 0]
    losses = [r for r in closes if float(r["pnl_usd"] or 0) <= 0]
    win_rate = (len(wins) / len(closes)) if closes else 0.0

    # NOVO: total pago em taxas no periodo (custo de execucao ja embutido no
    # pnl_usd, mas mostrado separado para deixar claro o quanto foi custo).
    total_fees = sum(float(r.get("fee_usd") or 0) for r in opens)

    best_trade = max(closes, key=lambda r: float(r["pnl_usd"] or 0), default=None)
    worst_trade = min(closes, key=lambda r: float(r["pnl_usd"] or 0), default=None)

    # Drawdown: reconstroi a curva de saldo a partir de TODO o historico
    # (nao so do periodo), pra medir queda real do pico ao vale.
    balance = config.SIMULATED_CAPITAL_USD
    peak = balance
    max_drawdown_pct = 0.0
    for r in sorted(rows, key=_parse_ts):
        if r["action"] == "OPEN":
            balance -= float(r["size_usd"] or 0)
        elif r["action"] == "CLOSE":
            balance += float(r["proceeds_usd"] or 0)
        peak = max(peak, balance)
        if peak > 0:
            drawdown_pct = (peak - balance) / peak
            max_drawdown_pct = max(max_drawdown_pct, drawdown_pct)

    per_wallet = defaultdict(lambda: {"trades": 0, "pnl": 0.0})
    for r in closes:
        w = r["source_wallet"]
        per_wallet[w]["trades"] += 1
        per_wallet[w]["pnl"] += float(r["pnl_usd"] or 0)

    suggestions = []
    for wallet, stats in per_wallet.items():
        if stats["trades"] >= 3 and stats["pnl"] < 0:
            suggestions.append(
                f"- Carteira {wallet[:10]}... teve {stats['trades']} trades fechados "
                f"com PnL negativo (${stats['pnl']:.2f}) no periodo. Considere revisar "
                f"ou parar de segui-la."
            )
    if not opens and not closes:
        suggestions.append(
            "- Nenhum trade foi copiado neste periodo. Verifique se as carteiras "
            "qualificadas ainda estao ativas, ou se os filtros em config.py "
            "estao restritivos demais."
        )
    if not suggestions:
        suggestions.append("- Nenhum alerta neste periodo.")

    lines = []
    lines.append(f"# Relatorio semanal -- bot de simulacao Polymarket")
    lines.append(f"Periodo: ultimos {days} dias (ate {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC})")
    lines.append("")
    lines.append("## Resumo")
    lines.append(f"- Capital fictício inicial: ${config.SIMULATED_CAPITAL_USD:.2f}")
    lines.append(f"- Saldo fictício atual: ${balance:.2f}")
    lines.append(f"- Trades abertos no periodo: {len(opens)}")
    lines.append(f"- Trades fechados no periodo: {len(closes)}")
    lines.append(f"- PnL do periodo: ${total_pnl:.2f}")
    lines.append(f"- Taxas pagas no periodo (ja incluidas no PnL acima): ${total_fees:.2f}")
    lines.append(f"- Win rate do periodo: {win_rate:.0%}")
    lines.append(f"- Drawdown maximo (historico completo): {max_drawdown_pct:.1%}")
    lines.append("")
    if best_trade:
        lines.append(f"- Melhor trade: {best_trade['market_question'][:60]} "
                      f"(PnL ${float(best_trade['pnl_usd']):.2f})")
    if worst_trade:
        lines.append(f"- Pior trade: {worst_trade['market_question'][:60]} "
                      f"(PnL ${float(worst_trade['pnl_usd']):.2f})")
    lines.append("")
    lines.append("## Desempenho por carteira copiada")
    if per_wallet:
        for wallet, stats in per_wallet.items():
            lines.append(f"- {wallet[:10]}...: {stats['trades']} trades, "
                          f"PnL ${stats['pnl']:.2f}")
    else:
        lines.append("- Sem trades fechados neste periodo.")
    lines.append("")
    lines.append("## Sugestoes de ajuste")
    lines.extend(suggestions)

    report_text = "\n".join(lines)

    os.makedirs("reports", exist_ok=True)
    filename = f"reports/relatorio_{datetime.now(timezone.utc):%Y-%m-%d}.md"
    with open(filename, "w") as f:
        f.write(report_text)

    print(f"Relatorio salvo em {filename}\n")
    print(report_text)
    return filename


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    generate_report(days=args.days)
