#!/usr/bin/env python3
"""
Проверка, что всё заведётся и API X отвечает.

  ./venv/bin/python smoke_check.py              # только импорты + accounts + API (без браузера)
  ./venv/bin/python smoke_check.py --selenium   # + реальный ответ на тренировочный твит (см. ниже)

TWITTER_LOG_LEVEL=max|normal|quiet — детализация логов (по умолчанию в debug_logger: max).

Порядок ручной проверки целиком:
  1) pip install -r requirements.txt
  2) accounts.txt: ct0:auth_token[:socks5]
  3) smoke_check.py — куки и GraphQL
  4) test_reply_once.py — только Selenium-ответ
  5) reply_bot.py — сначала dry run, потом подтверждение y
  6) main.py — полный пайплайн (свои tweet_ids / .env)

Переменные: REPLY_TEST_TEXT, SELENIUM_HEADLESS, USE_SELENIUM_REPLY, UC_CHROME_VERSION_MAIN,
  SELENIUM_REPLY_VERIFY_SEND=1 — строгая проверка закрытия модалки после Send (см. selenium_reply)
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from debug_logger import logger


def main() -> int:
    logger.section("Smoke check", subtitle="импорты · accounts · API")
    logger.subsection("Импорты")
    try:
        import selenium_reply
        from reply_bot import load_accounts
        from Twitter import Twitter
    except Exception as e:
        logger.error(f"FAIL: {e}")
        return 1
    logger.success("Модули загружены")

    logger.subsection("accounts.txt")
    try:
        accs = load_accounts("accounts.txt")
    except Exception as e:
        logger.error(f"FAIL: {e}")
        return 1
    logger.success(
        f"{len(accs)} аккаунт(ов); первый: proxy={'да' if accs[0].proxy else 'нет'}"
    )

    acc = accs[0]
    tw = Twitter(acc.cookie, proxy=acc.proxy)

    logger.subsection("API X (GraphQL, куки)")
    ud = tw.get_user_detail("escitam")
    if ud.get("errors"):
        logger.error(f"UserByScreenName: {ud['errors'][:2]}")
        return 1
    logger.success("UserByScreenName @escitam")

    td = tw.get_tweet_detail("2042347633922977942")
    if td.get("errors"):
        logger.warning(f"TweetDetail: {td['errors'][:2]}")
    else:
        logger.success("TweetDetail (тренировочный id)")

    mode = "вкл" if selenium_reply.use_selenium_reply() else "выкл"
    logger.panel(
        "Режим ответов",
        {"USE_SELENIUM_REPLY": f"Selenium {mode}", "TWITTER_LOG_LEVEL": os.getenv("TWITTER_LOG_LEVEL", "max")},
    )

    if "--selenium" in sys.argv:
        logger.section("Selenium", subtitle="реальный ответ на 2042347633922977942")
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
        logger.success("Исключений нет — проверь тред на X")

    logger.divider()
    logger.success("Smoke check завершён")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
