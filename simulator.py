"""
Motor de simulacao (copy trading em modo dry-run).

Responsabilidades:
- Manter o saldo fictício e as posicoes abertas.
- Quando uma carteira qualificada abre um trade novo, decidir se copia
  (respeitando limites de exposicao e o corte de perdas consecutivas).
- Registrar cada acao no log de trades (CSV), que depois alimenta o
  relatorio semanal.

Nada aqui envia ordens reais -- e apenas contabilidade em arquivo local.
Isso e o que muda quando (e se) voce migrar para dinheiro real: a funcao
`_record_fill()` abaixo e o unico lugar que, no modo real, chamaria a API
de execucao em vez de so escrever no CSV.
"""
from __future__ import annotations

import csv
import json
import os
import time
from datetime import datetime, timezone

import config
import polymarket_client as pm


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trade_age_hours(source_trade: dict) -> float:
    """
    Idade do trade em horas (0.0 se desconhecida/invalida). Aceita timestamp
    unix em segundos (10 digitos), milissegundos (13 digitos) ou string ISO.
    Usado para o filtro de idade e para o slippage por atraso (FIX v10).
    """
    ts = source_trade.get("timestamp")
    if ts is None:
        return 0.0
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return 0.0
    if ts > 1e12:  # milissegundos -> segundos
        ts /= 1000.0
    age_s = max(0.0, time.time() - ts)
    return age_s / 3600.0


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def _save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


class SimulationState:
    """Carrega e persiste o estado da simulacao entre execucoes do bot."""

    def __init__(self):
        os.makedirs(config.DATA_DIR, exist_ok=True)
        self.open_positions: dict = _load_json(config.OPEN_POSITIONS_FILE, {})
        self.seen_trade_ids: list = _load_json(config.SEEN_TRADES_FILE, [])
        self.consecutive_losses: dict = _load_json(
            os.path.join(config.DATA_DIR, "consecutive_losses.json"), {}
        )
        # Saldo fictício = capital inicial - valor travado em posicoes abertas.
        # Recalculado a partir do log de trades para evitar drift.
        self.cash_balance = self._recompute_cash_balance()

    def _recompute_cash_balance(self) -> float:
        balance = config.SIMULATED_CAPITAL_USD
        if not os.path.exists(config.TRADE_LOG_FILE):
            return balance
        with open(config.TRADE_LOG_FILE, newline="") as f:
            for row in csv.DictReader(f):
                action = row.get("action")
                if action == "OPEN":
                    balance -= float(row.get("size_usd", 0) or 0)
                elif action == "CLOSE":
                    balance += float(row.get("proceeds_usd", 0) or 0)
        return balance

    def save(self) -> None:
        _save_json(config.OPEN_POSITIONS_FILE, self.open_positions)
        _save_json(config.SEEN_TRADES_FILE, self.seen_trade_ids)
        _save_json(os.path.join(config.DATA_DIR, "consecutive_losses.json"),
                   self.consecutive_losses)

    def is_wallet_paused(self, wallet_address: str) -> bool:
        return self.consecutive_losses.get(wallet_address, 0) >= config.MAX_CONSECUTIVE_LOSSES_PER_WALLET

    def count_open_positions_for_market(self, market_id: str) -> int:
        return sum(1 for p in self.open_positions.values() if p.get("market_id") == market_id)

    def count_open_positions_for_event(self, event_key: str) -> int:
        return sum(1 for p in self.open_positions.values() if p.get("event_key") == event_key)


def _append_trade_log(row: dict) -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    file_exists = os.path.exists(config.TRADE_LOG_FILE)
    # IMPORTANTE: novas colunas sempre vao no FINAL desta lista, nunca no
    # meio. O arquivo trade_log.csv ja existente no seu repositorio tem um
    # cabecalho fixo escrito na primeira execucao -- inserir uma coluna no
    # meio desalinharia todas as linhas novas contra esse cabecalho antigo.
    # Colunas adicionadas no final continuam compativeis (linhas antigas
    # simplesmente nao tem valor nela).
    fieldnames = [
        "timestamp", "action", "source_wallet", "market_id", "market_question",
        "outcome", "side", "entry_price", "size_usd", "fee_usd", "proceeds_usd",
        "pnl_usd", "reason", "estimated_close_date",
    ]

    # FIX (bug real detetado): o cabecalho gravado na PRIMEIRA execucao do
    # bot ficava congelado para sempre -- mesmo apos o codigo evoluir e
    # passar a escrever mais colunas (taxa, depois data estimada), o
    # cabecalho no ficheiro continuava com a versao antiga. Resultado: as
    # colunas do dashboard ficavam desalinhadas com os dados reais (ex.: o
    # P&L de uma posicao fechada aparecia como "+$0.00" porque o dashboard
    # estava a ler o valor de outra coluna). Aqui corrigimos o cabecalho
    # automaticamente sempre que ele nao bate com as colunas atuais, antes
    # de escrever a nova linha.
    if file_exists:
        with open(config.TRADE_LOG_FILE, newline="") as f:
            first_line = f.readline().rstrip("\r\n")
        current_header = ",".join(fieldnames)
        if first_line != current_header:
            with open(config.TRADE_LOG_FILE, newline="") as f:
                lines = f.readlines()
            if lines:
                lines[0] = current_header + "\n"
                with open(config.TRADE_LOG_FILE, "w", newline="") as f:
                    f.writelines(lines)

    with open(config.TRADE_LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in fieldnames})


def maybe_copy_trade(state: SimulationState, source_wallet: str, source_trade: dict) -> bool:
    """
    Avalia um trade novo detectado numa carteira qualificada e decide se o
    bot deve "copiar" (simular) essa entrada. Retorna True se copiou.
    """
    trade_id = source_trade.get("id") or source_trade.get("transactionHash")
    if trade_id and trade_id in state.seen_trade_ids:
        return False  # ja processado

    if state.is_wallet_paused(source_wallet):
        return False  # carteira suspensa por sequencia de perdas

    if len(state.open_positions) >= config.MAX_OPEN_POSITIONS:
        return False  # limite de exposicao simultanea atingido

    market_id = source_trade.get("conditionId") or source_trade.get("market")
    outcome = source_trade.get("outcome")
    outcome_index = source_trade.get("outcomeIndex")
    side = source_trade.get("side", "BUY")
    entry_price = float(source_trade.get("price", 0) or 0)
    if entry_price <= 0 or entry_price >= 1:
        return False  # preco invalido para um mercado binario (0-1)

    # NOVO: filtro de preco maximo. Copiar a 0.95-0.99 e quase sempre EV
    # negativo -- com fee + slippage, uma entrada acima de ~0.98 perde
    # dinheiro mesmo vencendo, e o upside e minusculo para o risco de
    # perder tudo. So copiamos quando o trade tem odds minimas.
    if entry_price > config.MAX_ENTRY_PRICE:
        return False

    # NOVO (FIX v12): exclui mercados ultra-rapidos por palavra-chave no
    # titulo (ex.: "Up or Down" = janelas de 15 min de cripto). Esses
    # mercados resolvem antes de o bot agir, mesmo com polling rapido --
    # copiar e prejuizo quase certo (confirmado com dados reais).
    trade_title = (source_trade.get("title") or source_trade.get("question") or "").lower()
    for kw in config.EXCLUDED_MARKET_KEYWORDS:
        if kw in trade_title:
            return False

    # NOVO (FIX v10): filtro de idade -- ignora trades antigos. O bot ve os
    # trades da carteira com atraso (polling de 30 min + agendamento do
    # GitHub); um trade com muitas horas provavelmente e de um mercado que
    # ja resolveu, entao copia-lo seria irreal (em real, a ordem nem
    # preencheria). 0 desliga o filtro.
    age_hours = _trade_age_hours(source_trade)
    if config.MAX_TRADE_AGE_MINUTES > 0 and age_hours * 60 > config.MAX_TRADE_AGE_MINUTES:
        return False

    # NOVO: limite de exposicao por mercado -- impede que varias compras da
    # mesma carteira no mesmo mercado (ex.: escalando uma posicao aos poucos)
    # sejam todas copiadas, concentrando capital demais num unico mercado.
    if market_id and state.count_open_positions_for_market(market_id) >= config.MAX_POSITIONS_PER_MARKET:
        return False

    # NOVO: limite de exposicao por EVENTO real (eventSlug/eventId). Um mesmo
    # jogo/eleicao pode ter varios mercados diferentes (ex.: "BO3 winner" e
    # "Game 2 winner" da mesma partida) -- copiar todos e exposicao
    # correlacionada disfarcada de diversificacao, entao limitamos por evento,
    # nao so por mercado individual.
    event_key = source_trade.get("eventSlug") or source_trade.get("eventId") or source_trade.get("eventID")
    if event_key and state.count_open_positions_for_event(event_key) >= config.MAX_POSITIONS_PER_EVENT:
        return False

    size_usd = round(config.SIMULATED_CAPITAL_USD * config.POSITION_SIZE_PCT, 2)
    if size_usd > state.cash_balance:
        return False  # sem saldo fictício suficiente

    # NOVO (FIX v10 + v12): checagem de mercado ativo -- so depois de passar
    # pelos filtros baratos. Se QUALQUER lado do mercado ja esta num preco
    # "decidido" (>= MARKET_DECIDED_PRICE, padrao 0.95), o resultado e quase
    # certo e nao devemos copiar (em real, seria ordem em mercado preste a
    # fechar / lado morto). FIX v12: antes era >=0.99 e deixava passar
    # mercados decididos a 0.90-0.97, copiando o lado perdedor a 0.03-0.30.
    # 1 chamada extra, apenas para trades que seriam copiados.
    if market_id:
        try:
            recent = pm.get_trades_for_market(market_id, limit=25)
        except Exception:
            recent = []
        max_px = 0.0
        for t in recent:
            p = t.get("price")
            try:
                p = float(p)
            except (TypeError, ValueError):
                continue
            max_px = max(max_px, p)
        if max_px >= config.MARKET_DECIDED_PRICE:
            return False  # mercado ja "decidido" (algum lado ~certo)

    # NOVO (FIX v10): custos de execucao (taxa + slippage), com slippage POR
    # ATRASO. Quanto mais velho o trade, pior o preco que conseguiriamos de
    # verdade (base SLIPPAGE_PCT + SLIPPAGE_PER_AGE_HOUR por hora, com teto
    # MAX_SLIPPAGE_PCT). Taxa: reduz quanto do valor investido vira acoes de
    # verdade -- o resto e custo de execucao, nao gera retorno.
    slippage_pct = min(config.SLIPPAGE_PCT + age_hours * config.SLIPPAGE_PER_AGE_HOUR,
                       config.MAX_SLIPPAGE_PCT)
    effective_entry_price = round(min(entry_price * (1 + slippage_pct), 0.99), 4)
    fee_usd = round(size_usd * config.TAKER_FEE_PCT, 2)
    net_invested_usd = round(size_usd - fee_usd, 2)

    # NOVO: tenta capturar a data prevista de resolucao do mercado (endDate),
    # olhando a posicao da PROPRIA carteira de origem nesse mercado (ela
    # acabou de negociar, entao a posicao dela ja deve existir). E so uma
    # ESTIMATIVA -- o evento real pode demorar mais para resolver de verdade
    # (ex.: disputas, atrasos). Se nao conseguir, fica sem estimativa em vez
    # de quebrar a copia do trade.
    estimated_close_date = None
    try:
        source_position = pm.get_position_for_market(source_wallet, market_id)
        if source_position:
            estimated_close_date = source_position.get("endDate") or source_position.get("end_date")
    except Exception:
        pass  # estimativa e best-effort, nunca deve impedir a copia do trade

    position_key = f"{market_id}:{outcome}:{trade_id}"
    state.open_positions[position_key] = {
        "source_wallet": source_wallet,
        "market_id": market_id,
        "event_key": event_key,
        "market_question": source_trade.get("title") or source_trade.get("question", ""),
        "outcome": outcome,
        "outcome_index": outcome_index,
        "side": side,
        "entry_price": effective_entry_price,
        "size_usd": size_usd,
        "fee_usd": fee_usd,
        "net_invested_usd": net_invested_usd,
        "estimated_close_date": estimated_close_date,
        "opened_at": _now_iso(),
    }
    if trade_id:
        state.seen_trade_ids.append(trade_id)
    state.cash_balance -= size_usd

    _append_trade_log({
        "timestamp": _now_iso(),
        "action": "OPEN",
        "source_wallet": source_wallet,
        "market_id": market_id,
        "market_question": state.open_positions[position_key]["market_question"],
        "outcome": outcome,
        "side": side,
        "entry_price": effective_entry_price,
        "size_usd": size_usd,
        "fee_usd": fee_usd,
        "reason": "copiado da carteira qualificada (preco original "
                  f"{entry_price}, com slippage {slippage_pct:.0%} "
                  f"[{age_hours:.1f}h de atraso] e taxa "
                  f"{config.TAKER_FEE_PCT:.0%})",
        "estimated_close_date": estimated_close_date or "",
    })
    return True


def close_position(state: SimulationState, position_key: str, resolution_price: float,
                    reason: str = "mercado resolvido") -> None:
    """
    Fecha uma posicao simulada quando o mercado copiado resolve
    (resolution_price = 1.0 se o outcome apostado venceu, 0.0 se perdeu;
    valores intermediarios cobrem fechamento antecipado por preco de mercado).
    """
    pos = state.open_positions.pop(position_key, None)
    if pos is None:
        return

    # NOVO: usa o valor liquido (ja descontada a taxa de entrada) para
    # calcular quantas "acoes" a posicao realmente comprou. O PnL final
    # continua sendo medido contra o size_usd BRUTO (o que saiu do bolso),
    # entao a taxa aparece corretamente como um custo a mais no resultado.
    net_invested = pos.get("net_invested_usd", pos["size_usd"])
    shares = net_invested / pos["entry_price"] if pos["entry_price"] else 0
    proceeds_usd = round(shares * resolution_price, 2)
    pnl_usd = round(proceeds_usd - pos["size_usd"], 2)

    state.cash_balance += proceeds_usd

    wallet = pos["source_wallet"]
    if pnl_usd < 0:
        state.consecutive_losses[wallet] = state.consecutive_losses.get(wallet, 0) + 1
    else:
        state.consecutive_losses[wallet] = 0

    _append_trade_log({
        "timestamp": _now_iso(),
        "action": "CLOSE",
        "source_wallet": wallet,
        "market_id": pos["market_id"],
        "market_question": pos["market_question"],
        "outcome": pos["outcome"],
        "side": pos["side"],
        "entry_price": pos["entry_price"],
        "size_usd": pos["size_usd"],
        "proceeds_usd": proceeds_usd,
        "pnl_usd": pnl_usd,
        "reason": reason,
    })
