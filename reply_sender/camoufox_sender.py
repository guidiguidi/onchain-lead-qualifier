import time
import random
import logging
from typing import Optional, Dict
from urllib.parse import urlparse

from camoufox.sync_api import Camoufox
from debug_logger import logger


def parse_proxy(proxy_str: str) -> Optional[Dict[str, str]]:
    if not proxy_str or not proxy_str.strip():
        return None
    p = proxy_str.strip()
    if not p.startswith(("http://", "https://", "socks5://")):
        p = "http://" + p
    parsed = urlparse(p)
    server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
    res = {"server": server}
    if parsed.username:
        res["username"] = parsed.username
    if parsed.password:
        res["password"] = parsed.password
    return res


def reply_to_tweet_camoufox(
    cookie_str: str,
    tweet_id: str,
    reply_text: str,
    proxy: Optional[str] = None,
    headless: bool = True
) -> bool:
    """
    Sends a reply to a tweet using Camoufox (anti-detect Playwright Firefox).
    Bypasses Cloudflare Turnstile and Twitter behavioral heuristics natively.
    """
    logger.info(f"[Camoufox] Preparing stealth reply to tweet {tweet_id}...")

    proxy_cfg = parse_proxy(proxy) if proxy else None

    # Parse cookies from string (ct0 and auth_token)
    cookies_to_add = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies_to_add.append({
                "name": k.strip(),
                "value": v.strip(),
                "domain": ".x.com",
                "path": "/"
            })

    with Camoufox(headless=headless, proxy=proxy_cfg, geoip=True if proxy_cfg else False) as browser:
        context = browser.new_context()
        if cookies_to_add:
            context.add_cookies(cookies_to_add)

        page = context.new_page()
        tweet_url = f"https://x.com/i/status/{tweet_id}"
        logger.info(f"[Camoufox] Navigating to {tweet_url}...")
        page.goto(tweet_url, timeout=45000)
        time.sleep(random.uniform(2.0, 3.5))

        # Focus reply composer
        composer_selector = 'div[data-testid="tweetTextarea_0"][contenteditable="true"]'
        page.wait_for_selector(composer_selector, timeout=15000)
        composer = page.locator(composer_selector)
        composer.focus()

        # Type reply with human cadence
        for char in reply_text:
            composer.type(char, delay=int(random.uniform(30, 90)))

        time.sleep(random.uniform(0.8, 1.6))

        # Click reply button
        reply_btn_selector = 'button[data-testid="tweetButtonInline"]'
        page.wait_for_selector(reply_btn_selector, timeout=10000)
        page.locator(reply_btn_selector).click()
        time.sleep(random.uniform(2.0, 3.5))

        logger.success(f"[Camoufox] Reply sent successfully to tweet {tweet_id}!")
        return True
