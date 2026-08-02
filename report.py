"""
Gerador de relatório diário/semanal a partir do log de trades simulados.

Uso:
    python report.py            # relatório dos últimos 7 dias
    python report.py --days 1   # relatório do dia de hoje
    python report.py --days 30  # relatório do mês inteiro
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
    opens  = [r for r in period_rows if r["action"] == "OPEN"]

    total_pnl = sum(float(r["pnl_usd"] or 0) for r in closes)
    wins      = [r for r in closes if float(r["pnl_usd"] or 0) > 0]
    losses    = [r for r in closes if float(r["pnl_usd"] or 0) <= 0]
    win_rate  = (len(wins) / len(closes)) if closes else 0.0

    best_trade  = max(closes, key=lambda r: float(r["pnl_usd"] or 0), default=None)
    worst_trade = min(closes, key=lambda r: float(r["pnl_usd"] or 0), default=None)

    # Recalcula saldo e drawdown a partir de TODO o histórico
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
            dd = (peak - balance) / peak
            max_drawdown_pct = max(max_drawdown_pct, dd)

    per_wallet: dict = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "wins": 0})
    for r in closes:
        w = r["source_wallet"]
        per_wallet[w]["trades"] += 1
        per_wallet[w]["pnl"] += float(r["pnl_usd"] or 0)
        if float(r["pnl_usd"] or 0) > 0:
            per_wallet[w]["wins"] += 1

    suggestions = []
    for wallet, stats in per_wallet.items():
        if stats["trades"] >= 3 and stats["pnl"] < 0:
            suggestions.append(
                f"- ⚠️ Carteira {wallet[:10]}... teve {stats['trades']} trades "
                f"com PnL negativo (${stats['pnl']:.2f}). Considere pausar."
            )
    if not opens and not closes:
        suggestions.append(
            "- ℹ️ Nenhum trade no período. Verifique se qualified_wallets.json "
            "existe e se as carteiras ainda estão ativas."
        )
    if not suggestions:
        suggestions.append("- ✅ Nenhum alerta neste período.")

    # Determina o label do período
    if days == 1:
        periodo_label = f"hoje ({datetime.now(timezone.utc):%Y-%m-%d})"
        periodo_dias  = "1 dia"
    else:
        periodo_label = f"últimos {days} dias"
        periodo_dias  = f"{days} dias"

    lines = []
    lines.append(f"# Relatório do bot — Polymarket Simulação")
    lines.append(f"**Período:** {periodo_label} · gerado às {datetime.now(timezone.utc):%H:%M UTC}")
    lines.append("")
    lines.append("## 💰 Resumo financeiro")
    lines.append(f"| Métrica | Valor |")
    lines.append(f"|---|---|")
    lines.append(f"| Capital inicial (fictício) | ${config.SIMULATED_CAPITAL_USD:.2f} |")
    lines.append(f"| Saldo atual (fictício) | ${balance:.2f} |")
    lines.append(f"| P&L do período | ${total_pnl:+.2f} |")
    lines.append(f"| Win rate | {win_rate:.0%} ({len(wins)}W / {len(losses)}L) |")
    lines.append(f"| Drawdown máximo (histórico) | {max_drawdown_pct:.1%} |")
    lines.append(f"| Trades abertos no período | {len(opens)} |")
    lines.append(f"| Trades fechados no período | {len(closes)} |")
    lines.append("")

    if best_trade:
        lines.append(f"🟢 **Melhor trade:** {best_trade['market_question'][:70]} "
                     f"→ PnL ${float(best_trade['pnl_usd']):+.2f}")
    if worst_trade:
        lines.append(f"🔴 **Pior trade:** {worst_trade['market_question'][:70]} "
                     f"→ PnL ${float(worst_trade['pnl_usd']):+.2f}")
    lines.append("")

    lines.append("## 🔍 Desempenho por carteira copiada")
    if per_wallet:
        lines.append("| Carteira | Trades | Wins | PnL |")
        lines.append("|---|---|---|---|")
        for wallet, stats in sorted(per_wallet.items(),
                                     key=lambda x: x[1]["pnl"], reverse=True):
            wr = f"{stats['wins']}/{stats['trades']}"
            lines.append(f"| `{wallet[:12]}...` | {stats['trades']} | {wr} | ${stats['pnl']:+.2f} |")
    else:
        lines.append("Sem trades fechados neste período.")
    lines.append("")

    lines.append("## 🔧 Sugestões de ajuste")
    lines.extend(suggestions)

    report_text = "\n".join(lines)

    os.makedirs("reports", exist_ok=True)
    # Nome único por data E período para não sobrescrever
    suffix = "diario" if days == 1 else f"{days}d"
    filename = (f"reports/relatorio_"
                f"{datetime.now(timezone.utc):%Y-%m-%d}_{suffix}.md")
    with open(filename, "w") as f:
        f.write(report_text)

    print(f"Relatório salvo em {filename}\n")
    print(report_text)
    return filename


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    generate_report(days=args.days)
