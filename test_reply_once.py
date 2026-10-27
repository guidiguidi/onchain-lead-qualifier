#!/usr/bin/env python3
"""
One-off test reply to tweet (Selenium). Do not commit cookies.

  python test_reply_once.py [tweet_id]

Default: https://x.com/escitam/status/2042347633922977942
First line in accounts.txt: ct0:auth_token[:proxy]

Environment variables:
  REPLY_TEST_TEXT — reply body (defaults to short test text)
  SELENIUM_HEADLESS=1 — headless mode
  UC_CHROME_VERSION_MAIN=146 — override if Chrome version autodetection fails
  TWITTER_LOG_LEVEL=max|normal|quiet
  SELENIUM_REPLY_VERIFY_SEND=1 — fail with error after Send click if dialog modal
    did not close (verifies delivery in UI)

Delivery verification: log step 7 indicates field became stale; check thread on X
to find REPLY_TEST_TEXT. Under VERIFY_SEND=1 script fails if composer
remains visible on screen.
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

import selenium_reply
from debug_logger import logger
from reply_bot import load_accounts


def main() -> None:
    tweet_id = (
        sys.argv[1].strip()
        if len(sys.argv) > 1
        else "2042347633922977942"
    )
    text = os.getenv("REPLY_TEST_TEXT", "test reply (script)").strip() or "test"
    accounts = load_accounts("accounts.txt")
    acc = accounts[0]
    logger.section("Test reply once", subtitle=f"tweet_id={tweet_id}")
    logger.panel(
        "Account",
        {
            "account": "#0 in accounts.txt",
            "proxy": "yes" if acc.proxy else "no",
            "TWITTER_LOG_LEVEL": os.getenv("TWITTER_LOG_LEVEL", "max"),
        },
    )
    logger.info("Starting reply_to_tweet...")
    selenium_reply.reply_to_tweet(
        cookie=acc.cookie,
        tweet_id=tweet_id,
        reply_text=text,
        proxy=acc.proxy,
        timeout_seconds=45,
    )
    logger.success("Script finished without exceptions — verify thread on X manually.")


if __name__ == "__main__":
    main()
