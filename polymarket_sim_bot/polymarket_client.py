"""
Cliente fino para as APIs publicas do Polymarket.

Usa dois servicos:
- Gamma API  (gamma-api.polymarket.com): metadados de mercados (titulo,
  categoria, se esta ativo/fechado, precos correntes das outcomes).
- Data API   (data-api.polymarket.com): trades e posicoes on-chain por
  carteira (endereco publico).

Nenhum dos dois exige autenticacao para estas leituras. Nao ha envio de
ordens aqui de proposito -- este projeto e apenas o bot de SIMULACAO.

IMPORTANTE: estas chamadas fazem requisicoes HTTP reais. Elas precisam
rodar num ambiente com acesso a internet ao Polymarket (o sandbox usado
para escrever este codigo tem a rede restrita a uma allowlist e bloqueia
estes dominios -- por isso os testes aqui foram feitos com dados de
exemplo, nao com chamadas reais). Rode no seu computador ou num VPS para
testar de verdade.
"""
from __future__ import annotations

import time
from typing import Any, Optional

import requests

import config


class PolymarketClientError(Exception):
    pass


def _get(url: str, params: Optional[dict] = None, retries: int = 3) -> Any:
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=config.REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last_err = exc
            time.sleep(1.5 * (attempt + 1))
    raise PolymarketClientError(f"Falha ao chamar {url}: {last_err}")


def get_markets(active: bool = True, closed: bool = False, limit: int = 500,
                 offset: int = 0, category: Optional[str] = None) -> list[dict]:
    """Busca mercados via Gamma API, com paginacao manual (limit max 500)."""
    params = {"active": str(active).lower(), "closed": str(closed).lower(),
              "limit": limit, "offset": offset}
    if category:
        params["tag"] = category
    return _get(f"{config.GAMMA_API_BASE}/markets", params=params)


def get_all_markets(active: bool = True, closed: bool = False,
                     category: Optional[str] = None, max_pages: int = 20) -> list[dict]:
    """Pagina o endpoint /markets ate acabar ou atingir max_pages."""
    all_markets: list[dict] = []
    offset = 0
    for _ in range(max_pages):
        page = get_markets(active=active, closed=closed, limit=500,
                            offset=offset, category=category)
        if not page:
            break
        all_markets.extend(page)
        if len(page) < 500:
            break
        offset += 500
    return all_markets


def get_trades_for_user(wallet_address: str, limit: int = 500,
                         offset: int = 0) -> list[dict]:
    """Historico de trades de uma carteira especifica (mais recente primeiro)."""
    params = {"user": wallet_address, "limit": limit, "offset": offset}
    return _get(f"{config.DATA_API_BASE}/trades", params=params)


def get_all_trades_for_user(wallet_address: str, max_pages: int = 10) -> list[dict]:
    all_trades: list[dict] = []
    offset = 0
    for _ in range(max_pages):
        page = get_trades_for_user(wallet_address, limit=500, offset=offset)
        if not page:
            break
        all_trades.extend(page)
        if len(page) < 500:
            break
        offset += 500
    return all_trades


def get_positions_for_user(wallet_address: str) -> list[dict]:
    """Posicoes abertas/fechadas de uma carteira (usado para checar resolucao)."""
    params = {"user": wallet_address}
    return _get(f"{config.DATA_API_BASE}/positions", params=params)


def get_recent_trades_feed(limit: int = 500, offset: int = 0,
                            min_cash_amount: Optional[float] = None) -> list[dict]:
    """
    Feed GLOBAL de trades recentes (sem filtrar por usuario) -- usado para
    DESCOBRIR carteiras automaticamente, em vez de exigir que voce ja saiba
    enderecos de antemao.

    Se min_cash_amount for informado, usa os parametros documentados
    filterType=CASH & filterAmount=<valor> para trazer so trades acima
    desse valor em dolares (um proxy simples de "trade feito por alguem
    com capital relevante").
    """
    params: dict = {"limit": limit, "offset": offset, "takerOnly": "true"}
    if min_cash_amount is not None:
        params["filterType"] = "CASH"
        params["filterAmount"] = min_cash_amount
    return _get(f"{config.DATA_API_BASE}/trades", params=params)
