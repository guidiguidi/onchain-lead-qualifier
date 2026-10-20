import json
import os
import random
import sys
import time
from dataclasses import dataclass
from typing import List, Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

load_dotenv()

from Twitter import Twitter
from debug_logger import logger
from reply_sender import send_reply

@dataclass(frozen=True)
class Account:
    cookie: str
    proxy: Optional[str] = None

def load_accounts(file_path: str = "accounts.txt") -> List[Account]:
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"accounts.txt not found: {file_path}")

    accounts: List[Account] = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":", 2)
            if len(parts) < 2:
                continue
            ct0 = parts[0].strip()
            auth_token = parts[1].strip()
            proxy = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
            if not ct0 or not auth_token:
                continue
            cookie = f"ct0={ct0}; auth_token={auth_token}"
            accounts.append(Account(cookie=cookie, proxy=proxy or None))

    if not accounts:
        raise ValueError("No valid accounts found in accounts.txt (format: ct0:auth[:socks5])")
    return accounts

def load_targets(file_path: str = "targets.txt") -> List[str]:
    if not os.path.exists(file_path):
        # Create a default targets.txt if not exists
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("# List of Twitter usernames to monitor (one per line)\nelonmusk\nvitalikbuterin\n")
        return ["elonmusk", "vitalikbuterin"]

    targets = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip().lstrip("@")
            if not line or line.startswith("#"):
                continue
            targets.append(line)

    if not targets:
        return ["elonmusk", "vitalikbuterin"]

    return targets

def get_random_text(file_path: str = "texts.txt") -> str:
    if not os.path.exists(file_path):
        # Create a default texts.txt if not exists
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("gm\nnice post\nwatching this closely\ninteresting\n")
        return "gm"

    with open(file_path, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    if not lines:
        return "gm"

    return random.choice(lines)

def reply_under_latest_profile_post(twitter: Twitter, username: str, dry_run: bool = False) -> None:
    logger.subsection(f"Checking @{username}")
    
    try:
        latest = twitter.get_latest_normal_tweet(username)
    except Exception as e:
        logger.error(f"Failed to fetch latest tweet for @{username}: {e}")
        return

    if not latest:
        logger.warning(f"No normal tweet found for @{username}")
        return

    text = get_random_text("texts.txt")
    tweet_url = f"https://x.com/{latest.get('username', username)}/status/{latest['tweet_id']}"

    logger.info(f"Latest post ID: {latest['tweet_id']}")
    logger.info(f"Tweet link: {tweet_url}")
    logger.info(f"Text snippet: {latest.get('text', '')[:50]}...")
    logger.info(f"My reply text: {text}")

    if dry_run:
        logger.success(f"[DRY RUN] Would reply to @{username}")
        return

    try:
        result = send_reply(
            cookie=twitter.cookie,
            tweet_id=latest["tweet_id"],
            text=text,
            proxy=getattr(twitter, "proxy", None)
        )
        if "data" in result and "create_tweet" in result["data"]:
            logger.success(f"Successfully replied to @{username}")
        else:
            logger.error(f"Failed to reply to @{username}: {json.dumps(result)}")
    except Exception as e:
        logger.error(f"Failed to reply to @{username}: {e}")

def process_targets_with_account(account: Account, targets: List[str], dry_run: bool) -> None:
    twitter = Twitter(account.cookie, proxy=account.proxy)
    for username in targets:
        reply_under_latest_profile_post(twitter, username, dry_run=dry_run)
        time.sleep(2)

def main() -> None:
    logger.panel(
        "Логи",
        {
            "TWITTER_LOG_LEVEL": os.getenv("TWITTER_LOG_LEVEL", "max"),
            "verbose": logger.verbose_enabled,
            "trace": logger.trace_enabled,
        },
    )
    logger.section("Twitter Auto-Reply Bot")

    try:
        accounts = load_accounts("accounts.txt")
        targets = load_targets("targets.txt")
        logger.success(f"Loaded {len(accounts)} account(s) and {len(targets)} target(s)")
    except Exception as e:
        logger.critical(f"Initialization error: {e}")
        sys.exit(1)

    chunks: List[List[str]] = [[] for _ in range(len(accounts))]
    for i, t in enumerate(targets):
        chunks[i % len(accounts)].append(t)

    logger.info("Starting initial verification (DRY RUN)...")
    with ThreadPoolExecutor(max_workers=len(accounts)) as ex:
        futures = [
            ex.submit(process_targets_with_account, acc, chunk, True)
            for acc, chunk in zip(accounts, chunks)
            if chunk
        ]
        for fut in as_completed(futures):
            _ = fut.result()

    logger.section("Live Execution")
    # Uncomment the following to run for real, or run with a flag
    # For now, I'll keep it as a separate loop that can be enabled
    confirm = input("\nDo you want to proceed with ACTUAL replies? (y/n): ")
    if confirm.lower() == 'y':
        with ThreadPoolExecutor(max_workers=len(accounts)) as ex:
            futures = [
                ex.submit(process_targets_with_account, acc, chunk, False)
                for acc, chunk in zip(accounts, chunks)
                if chunk
            ]
            for fut in as_completed(futures):
                _ = fut.result()
    else:
        logger.info("Live execution cancelled by user.")

if __name__ == "__main__":
    main()
