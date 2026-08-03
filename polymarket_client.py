"""
Cliente para as APIs públicas do Polymarket.

FIXES v3:
- get_market_by_condition_id: tenta 3 estratégias em cascata para garantir
  que mercados resolvidos são sempre encontrados, mesmo que um endpoint falhe.

FIX v4: esta funcao agora e usada so como FALLBACK secundario em main.py
(a estrategia primaria de fechamento usa /positions da carteira de origem,
que ja esta comprovadamente funcionando). Mantida aqui sem alteracoes,
porque ainda serve de reforco quando a carteira de origem ja resgatou a
posicao dela e ela some do /positions.
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
            resp = requests.get(url, params=params,
                                timeout=config.REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last_err = exc
            time.sleep(1.5 * (attempt + 1))
    raise PolymarketClientError(f"Falha ao chamar {url}: {last_err}")


def get_markets(active: bool = True, closed: bool = False, limit: int = 500,
                offset: int = 0, category: Optional[str] = None) -> list[dict]:
    params = {
        "active": str(active).lower(),
        "closed": str(closed).lower(),
        "limit": limit,
        "offset": offset,
    }
    if category:
        params["tag"] = category
    result = _get(f"{config.GAMMA_API_BASE}/markets", params=params)
    return result if isinstance(result, list) else []


def get_all_markets(active: bool = True, closed: bool = False,
                    category: Optional[str] = None, max_pages: int = 20) -> list[dict]:
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


def get_market_by_condition_id(condition_id: str) -> Optional[dict]:
    """
    Busca um mercado pelo conditionId com 3 estratégias em cascata.

    FIX v3: o parâmetro 'condition_ids' nem sempre funciona na Gamma API
    — pode retornar vazio mesmo com um ID válido. Por isso tentamos
    3 abordagens antes de desistir:

    1. Parâmetro 'conditionId' (singular, sem 's') — alguns endpoints aceitam
    2. Parâmetro 'condition_ids' (plural) — documentado mas inconsistente
    3. Buscar mercados fechados recentes e procurar pelo ID manualmente
       (fallback mais lento mas mais fiável)
    """
    # Estratégia 1: conditionId singular
    try:
        result = _get(f"{config.GAMMA_API_BASE}/markets",
                      params={"conditionId": condition_id, "limit": 5})
        if isinstance(result, list) and result:
            return result[0]
        elif isinstance(result, dict) and result:
            return result
    except Exception:
        pass

    # Estratégia 2: condition_ids plural
    try:
        result = _get(f"{config.GAMMA_API_BASE}/markets",
                      params={"condition_ids": condition_id, "limit": 5})
        if isinstance(result, list) and result:
            return result[0]
    except Exception:
        pass

    # Estratégia 3: busca directa pelo ID no endpoint /markets/{id}
    try:
        result = _get(f"{config.GAMMA_API_BASE}/markets/{condition_id}")
        if isinstance(result, dict) and result:
            return result
    except Exception:
        pass

    # Estratégia 4: paginar mercados fechados recentes (até 3 páginas)
    try:
        for offset in range(0, 1500, 500):
            page = get_markets(active=False, closed=True, limit=500,
                               offset=offset)
            if not page:
                break
            match = next(
                (m for m in page
                 if m.get("conditionId") == condition_id
                 or m.get("id") == condition_id),
                None,
            )
            if match:
                return match
    except Exception:
        pass

    return None


def get_trades_for_user(wallet_address: str, limit: int = 500,
                        offset: int = 0) -> list[dict]:
    params = {"user": wallet_address, "limit": limit, "offset": offset}
    result = _get(f"{config.DATA_API_BASE}/trades", params=params)
    return result if isinstance(result, list) else []


def get_all_trades_for_user(wallet_address: str,
                             max_pages: int = 10) -> list[dict]:
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
    params = {"user": wallet_address}
    result = _get(f"{config.DATA_API_BASE}/positions", params=params)
    return result if isinstance(result, list) else []


def get_recent_trades_feed(limit: int = 500, offset: int = 0,
                            min_cash_amount: Optional[float] = None) -> list[dict]:
    params: dict = {"limit": limit, "offset": offset, "takerOnly": "true"}
    if min_cash_amount is not None:
        params["filterType"] = "CASH"
        params["filterAmount"] = min_cash_amount
    result = _get(f"{config.DATA_API_BASE}/trades", params=params)
    return result if isinstance(result, list) else []
