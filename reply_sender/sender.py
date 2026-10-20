import os
from typing import Optional, Dict, Any
from .api_sender import send_api_reply
from .camoufox_sender import reply_to_tweet_camoufox
from .selenium_sender import reply_to_tweet as send_selenium_reply, use_selenium_reply, format_reply_error
from debug_logger import logger


def send_reply(cookie: str, tweet_id: str, text: str, proxy: Optional[str] = None) -> Dict[str, Any]:
    """
    Main entry point for dispatching replies.
    Supports 3 backends:
      1. 'camoufox' (Default stealth mode: Playwright + C++ anti-detect Firefox)
      2. 'api' (Direct Twitter GraphQL API)
      3. 'selenium' (Legacy Selenium fallback)
    """
    backend = os.getenv("SENDER_BACKEND", "camoufox").lower()

    if backend == "camoufox":
        try:
            headless = os.getenv("HEADLESS", "true").lower() in ("1", "true", "yes")
            reply_to_tweet_camoufox(cookie, tweet_id, text, proxy=proxy, headless=headless)
            return {"data": {"create_tweet": {"rest_id": "camoufox_success"}}}
        except Exception as e:
            logger.warning(f"[Sender] Camoufox dispatch encountered error: {e}. Falling back to GraphQL API...")
            return send_api_reply(cookie, tweet_id, text, proxy)

    elif backend == "selenium" or use_selenium_reply():
        try:
            send_selenium_reply(cookie=cookie, proxy=proxy, tweet_id=tweet_id, reply_text=text)
            return {"data": {"create_tweet": {"rest_id": "selenium_success"}}}
        except Exception as sel_err:
            logger.warning(f"[Sender] Selenium failed, falling back to API: {format_reply_error(sel_err)}")
            return send_api_reply(cookie, tweet_id, text, proxy)

    else:
        return send_api_reply(cookie, tweet_id, text, proxy)
