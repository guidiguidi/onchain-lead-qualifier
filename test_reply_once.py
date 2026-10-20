#!/usr/bin/env python3
"""
Один пробный ответ на твит (Selenium). Не коммитьте куки.

  python test_reply_once.py [tweet_id]

По умолчанию: https://x.com/escitam/status/2042347633922977942
Первая строка из accounts.txt: ct0:auth_token[:proxy]

Переменные:
  REPLY_TEST_TEXT — текст ответа (по умолчанию короткий тест)
  SELENIUM_HEADLESS=1 — без окна
  UC_CHROME_VERSION_MAIN=146 — если автоопределение версии Chrome не сработало
  TWITTER_LOG_LEVEL=max|normal|quiet
  SELENIUM_REPLY_VERIFY_SEND=1 — после клика Send упасть с ошибкой, если модалка
    не закрылась (проверка, что отправка дошла до UI)

Как понять, что Send сработал: в логах шаг 7 «поле стало stale»; плюс открой тред
на X и найди свой REPLY_TEST_TEXT. При VERIFY_SEND=1 скрипт не завершится «успехом»,
если композер остался на экране.
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
        "Аккаунт",
        {
            "account": "#0 из accounts.txt",
            "proxy": "да" if acc.proxy else "нет",
            "TWITTER_LOG_LEVEL": os.getenv("TWITTER_LOG_LEVEL", "max"),
        },
    )
    logger.info("Запуск reply_to_tweet…")
    selenium_reply.reply_to_tweet(
        cookie=acc.cookie,
        tweet_id=tweet_id,
        reply_text=text,
        proxy=acc.proxy,
        timeout_seconds=45,
    )
    logger.success("Скрипт завершился без исключения — проверь тред на X вручную.")


if __name__ == "__main__":
    main()
