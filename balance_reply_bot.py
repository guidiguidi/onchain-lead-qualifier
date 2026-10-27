import json
import os
import time
import random
import re
from typing import List, Dict, Any, Optional

from Twitter import Twitter
from blockchain_api import BlockchainValidator
from main import (
    _env_truthy,
    extract_wallets,
    fetch_replies_for_tweet,
    load_cookie_from_accounts,
    load_tweet_ids,
)
from debug_logger import logger
from reply_sender import send_reply

def main():
    logger.section("Twitter Balance-Based Reply Bot")

    # 1. Load configuration
    etherscan_key = os.getenv("ETHERSCAN_API_KEY")
    alchemy_key = os.getenv("ALCHEMY_SOLANA_KEY")  # None if not provided in .env
    
    try:
        cookie = load_cookie_from_accounts("accounts.txt")
        # Extract target post ID from tweets.txt
        tweet_ids = load_tweet_ids("tweets.txt")
        logger.success(f"Loaded {len(tweet_ids)} target tweets from tweets.txt")
    except Exception as e:
        logger.critical(f"Error loading config: {e}")
        return

    twitter = Twitter(cookie)
    validator = BlockchainValidator(etherscan_key, alchemy_key)

    for tweet_id in tweet_ids:
        logger.section(f"Analyzing Tweet ID: {tweet_id}")
        
        # 2. Collect replies under target post
        replies = fetch_replies_for_tweet(twitter, tweet_id)
        logger.info(f"Total replies found: {len(replies)}")

        for reply in replies:
            username = reply.get("username")
            reply_id = reply.get("tweet_id")
            full_text = reply.get("full_text", "")
            
            # Extract crypto wallet addresses from reply body
            wallets = extract_wallets(full_text)
            evm_list = wallets.get("evm", [])
            sol_list = wallets.get("solana", [])

            if not evm_list and not sol_list:
                continue

            print("\n" + "="*50)
            logger.info(f"FOUND WALLET from @{username}")
            logger.debug(f"User wrote: {full_text[:100]}...")

            found_balance_info = None

            # 3. Query EVM wallet balances
            if evm_list and validator.etherscan:
                logger.info(f"Checking EVM balance for: {evm_list[0]}...")
                try:
                    res = validator.etherscan.validate_address(evm_list[0])
                    # Extract numeric value from string (e.g. "1.25 ETH")
                    balance_val = float(res.balance.split()[0])
                    if balance_val > 0:
                        found_balance_info = f"{res.balance} (EVM)"
                except Exception as e:
                    logger.error(f"EVM Check error: {e}")

            # 4. Query Solana balance if EVM query was empty
            if not found_balance_info and sol_list and validator.solscan:
                logger.info(f"Checking Solana balance for: {sol_list[0]}...")
                try:
                    res = validator.solscan.validate_address(sol_list[0])
                    balance_val = float(res.balance.split()[0])
                    if balance_val > 0:
                        found_balance_info = f"{res.balance} (Solana)"
                except Exception as e:
                    logger.error(f"Solana Check error: {e}")

            # 5. Format qualified lead reply if balance exceeds threshold
            if found_balance_info:
                # Reply text variants
                templates = [
                    "Nice! I see you have {bal} on your wallet. 🚀",
                    "Wow, {bal} detected. Great balance! 🔥",
                    "Checking... found {bal}. You're ready for the moon! 🌕",
                    "Verified. Balance: {bal}. Good luck! 💎"
                ]
                
                base_text = random.choice(templates).format(bal=found_balance_info)
                # Final reply payload tagged with target handle
                final_reply = f"@{username} {base_text}"

                print("-" * 30)
                print(f"!!! MATCH FOUND !!!")
                print(f"USER: @{username}")
                print(f"BALANCE: {found_balance_info}")
                print(f"DRAFT MESSAGE: {final_reply}")
                print("-" * 30)

                # Logging pause / manual confirmation checkpoint
                # Uncomment / comment out input prompt for full automation
                # confirm = input("Send this reply? (y/n/skip): ")
                # if confirm.lower() == 'y':
                
                send_target_id = reply_id
                if username and _env_truthy("REPLY_ON_TARGET_LATEST_POST", True):
                    try:
                        latest_post = twitter.get_latest_normal_tweet(username)
                        if latest_post and latest_post.get("tweet_id"):
                            send_target_id = latest_post["tweet_id"]
                            if send_target_id != reply_id:
                                logger.info(
                                    f"Reply target: latest own post {send_target_id} (not thread reply {reply_id})"
                                )
                    except Exception as e:
                        logger.warning(
                            f"get_latest_normal_tweet(@{username}) failed ({e}); using thread reply {reply_id}"
                        )

                logger.info(f"SENDING REPLY to @{username} (under tweet {send_target_id})...")
                try:
                    result = send_reply(
                        cookie=twitter.cookie,
                        tweet_id=send_target_id,
                        text=final_reply,
                        proxy=getattr(twitter, "proxy", None)
                    )
                    if "data" in result:
                        logger.success(f"REPLY SENT! ID: {result['data'].get('create_tweet', {}).get('rest_id')}")
                    else:
                        logger.error(f"Failed to send. Response: {json.dumps(result)}")
                except Exception as e:
                    logger.error(f"Error during reply: {e}")

                # 20-40s randomized rate-limit delay between replies to prevent spam flagging
                wait_time = random.randint(20, 40)
                logger.info(f"Waiting {wait_time}s before next check...")
                time.sleep(wait_time)
            else:
                logger.debug(f"No balance for @{username}, skipping.")

    logger.success("Finishing work.")

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    main()
