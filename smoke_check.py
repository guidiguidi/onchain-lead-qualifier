#!/usr/bin/env python3
"""
Verifies dependencies, environment setup and X API connectivity.

  ./venv/bin/python smoke_check.py              # module imports + accounts + API (headless/no browser)
  ./venv/bin/python smoke_check.py --selenium   # executes live test reply via browser engine

TWITTER_LOG_LEVEL=max|normal|quiet — logger verbosity (defaults to max).

Verification checklist:
  1) pip install -r requirements.txt
  2) accounts.txt: ct0:auth_token[:socks5]
  3) smoke_check.py — session cookies and GraphQL API
  4) test_reply_once.py — standalone browser reply verification
  5) reply_bot.py — dry run mode followed by manual confirmation prompt
  6) main.py — full end-to-end data pipeline

Environment variables: REPLY_TEST_TEXT, SELENIUM_HEADLESS, USE_SELENIUM_REPLY, UC_CHROME_VERSION_MAIN,
  SELENIUM_REPLY_VERIFY_SEND=1 — strict verification of composer modal closure after send
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from debug_logger import logger


def main() -> int:
    logger.section("Smoke check", subtitle="imports · accounts · API")
    logger.subsection("Module Imports")
    try:
        import selenium_reply
        from reply_bot import load_accounts
        from Twitter import Twitter
    except Exception as e:
        logger.error(f"FAIL: {e}")
        return 1
    logger.success("All modules loaded successfully")

    logger.subsection("accounts.txt")
    try:
        accs = load_accounts("accounts.txt")
    except Exception as e:
        logger.error(f"FAIL: {e}")
        return 1
    logger.success(
        f"{len(accs)} account(s); primary: proxy={'yes' if accs[0].proxy else 'no'}"
    )

    acc = accs[0]
    tw = Twitter(acc.cookie, proxy=acc.proxy)

    logger.subsection("X API (GraphQL, Cookies)")
    ud = tw.get_user_detail("escitam")
    if ud.get("errors"):
        logger.error(f"UserByScreenName: {ud['errors'][:2]}")
        return 1
    logger.success("UserByScreenName @escitam")

    td = tw.get_tweet_detail("2042347633922977942")
    if td.get("errors"):
        logger.warning(f"TweetDetail: {td['errors'][:2]}")
    else:
        logger.success("TweetDetail (test ID query OK)")

    mode = "enabled" if selenium_reply.use_selenium_reply() else "disabled"
    logger.panel(
        "Reply Engine Mode",
        {"USE_SELENIUM_REPLY": f"Selenium {mode}", "TWITTER_LOG_LEVEL": os.getenv("TWITTER_LOG_LEVEL", "max")},
    )

    if "--selenium" in sys.argv:
        logger.section("Browser Engine", subtitle="test reply dispatch on 2042347633922977942")
        text = os.getenv("REPLY_TEST_TEXT", "smoke_check ok").strip() or "test"
        try:
            selenium_reply.reply_to_tweet(
                cookie=acc.cookie,
                tweet_id="2042347633922977942",
                reply_text=text,
                proxy=acc.proxy,
                timeout_seconds=45,
            )
        except Exception as e:
            logger.error(selenium_reply.format_reply_error(e))
            return 1
        logger.success("No exceptions thrown — verify thread on X")

    logger.divider()
    logger.success("Smoke check completed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
