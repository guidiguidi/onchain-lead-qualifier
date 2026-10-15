"""Normalize proxy strings from accounts.txt (host:port:user:pass) for requests/Selenium."""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote


def normalize_socks_proxy_url(proxy: Optional[str]) -> Optional[str]:
    if not proxy:
        return None
    p = proxy.strip()
    if not p:
        return None
    pl = p.lower()
    if pl.startswith("http://") or pl.startswith("https://"):
        return p

    body = p
    if pl.startswith("socks5h://"):
        body = p[10:]
    elif pl.startswith("socks5://"):
        body = p[9:]

    # user:pass@host:port (already valid)
    if body and "@" in body:
        return f"socks5h://{body}"

    parts = body.split(":")
    if len(parts) >= 4:
        host, port, user = parts[0], parts[1], parts[2]
        password = ":".join(parts[3:])
        u = quote(user, safe="")
        pw = quote(password, safe="")
        return f"socks5h://{u}:{pw}@{host}:{port}"
    if len(parts) == 2:
        return f"socks5h://{parts[0]}:{parts[1]}"
    if not body:
        return None
    return f"socks5h://{body}"
