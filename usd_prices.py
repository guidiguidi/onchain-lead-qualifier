"""
ETH / SOL spot vs USD for reply thresholds. Cached to limit requests.
"""
from __future__ import annotations

import time
from typing import Optional, Tuple

import requests

_CACHE_TTL_SEC = 90.0
_cache: Optional[Tuple[float, float, float]] = None  # eth_usd, sol_usd, monotonic_ts


def get_eth_sol_usd(session: Optional[requests.Session] = None) -> Optional[Tuple[float, float]]:
    """Returns (eth_usd, sol_usd) or None if unavailable."""
    global _cache
    now = time.monotonic()
    if _cache is not None:
        eth_usd, sol_usd, ts = _cache
        if (now - ts) < _CACHE_TTL_SEC:
            return eth_usd, sol_usd

    sess = session or requests.Session()
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": "ethereum,solana", "vs_currencies": "usd"}
    try:
        r = sess.get(
            url,
            params=params,
            timeout=20,
            headers={"Accept": "application/json"},
        )
        r.raise_for_status()
        data = r.json()
        eth = data.get("ethereum", {}).get("usd")
        sol = data.get("solana", {}).get("usd")
        if eth is None or sol is None:
            return None
        eth_f, sol_f = float(eth), float(sol)
        _cache = (eth_f, sol_f, now)
        return eth_f, sol_f
    except (requests.RequestException, ValueError, TypeError, KeyError):
        return None
