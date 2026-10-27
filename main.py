import json
import os
import re
import sys
import time
import random
from datetime import datetime, timezone
from urllib.parse import quote
from dataclasses import dataclass
from queue import Queue, Empty
from threading import Lock, Thread
from typing import Dict, List, Any, Optional, Tuple
from dotenv import load_dotenv

load_dotenv()

from Twitter import Twitter
from blockchain_api import BlockchainValidator
from debug_logger import logger
from reply_sender import send_reply
from reply_sender.selenium_sender import use_selenium_reply, format_reply_error
from usd_prices import get_eth_sol_usd

_REPLY_AUDIT_LOCK = Lock()


def append_reply_audit(entry: Dict[str, Any]) -> None:
    """Writes reply audit log to JSONL + human-readable .txt (thread-safe)."""
    if not _env_truthy("REPLY_AUDIT", True):
        return
    jsonl_path = (os.getenv("REPLY_AUDIT_LOG") or "reply_audit.jsonl").strip()
    txt_path = (os.getenv("REPLY_AUDIT_TXT") or "reply_audit.txt").strip()
    if not jsonl_path:
        return
    at = datetime.now(timezone.utc).isoformat()
    row = {**entry, "at": at}
    line = json.dumps(row, ensure_ascii=False) + "\n"
    target = entry.get("replied_to_url") or entry.get("reply_to_tweet_id") or "?"
    body = (
        f"\n{'=' * 60}\n"
        f"{at}\n"
    body = f"Target tweet: {target}
"
    body += f"Status: {entry.get('status')}
"
    )
    if entry.get("chosen_chain") or entry.get("chosen_address") or entry.get("balance_summary"):
        body += (
            f"Wallet: {entry.get('chosen_chain')} {entry.get('chosen_address')} | "
            f"balance: {entry.get('balance_summary')}
"
        )
        body += f"Reply body:
{entry.get('reply_text', '')}
"
    if entry.get("error"):
        body += f"Error: {entry['error']}
"
    try:
        with _REPLY_AUDIT_LOCK:
            with open(jsonl_path, "a", encoding="utf-8") as f:
                f.write(line)
            if txt_path:
                with open(txt_path, "a", encoding="utf-8") as f:
                    f.write(body)
    except OSError as e:
        logger.warning(f"Failed to write reply audit log: {e}")


# #region agent log
_AGENT_DEBUG_LOG = "/Users/guidi/Desktop/twitter/.cursor/debug-7d39ee.log"
_AGENT_SESSION = "7d39ee"


def _agent_debug_log(
    hypothesis_id: str,
    location: str,
    message: str,
    data: Dict[str, Any],
    run_id: str = "pre-fix",
) -> None:
    try:
        with open(_AGENT_DEBUG_LOG, "a", encoding="utf-8") as _f:
            _f.write(
                json.dumps(
                    {
                        "sessionId": _AGENT_SESSION,
                        "timestamp": int(time.time() * 1000),
                        "hypothesisId": hypothesis_id,
                        "location": location,
                        "message": message,
                        "data": data,
                        "runId": run_id,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass


# #endregion

def _validation_workers() -> int:
    raw = os.getenv("WALLET_VALIDATE_WORKERS", "1").strip()
    try:
        n = int(raw)
    except ValueError:
        return 1
    return max(1, min(n, 32))


def _env_truthy(name: str, default: bool = False) -> bool:
    v = os.getenv(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _solana_alchemy_key() -> Optional[str]:
    """Alchemy path segment after /v2/ ; ALCHEMY_SOLANA_KEY preferred, SOLSCAN_API_KEY as legacy alias."""
    for name in ("ALCHEMY_SOLANA_KEY", "SOLSCAN_API_KEY"):
        v = os.getenv(name, "").strip().strip('"').strip("'")
        if v:
            return v
    return None


def _solana_rpc_url() -> Optional[str]:
    """
    Full HTTPS JSON-RPC URL. Quotes in .env are stripped.
    For Helius: provide full URL with ?api-key=..., or base URL + SOLANA_RPC_API_KEY (appends query param).
    """
    raw = os.getenv("SOLANA_RPC_URL", "").strip().strip('"').strip("'")
    if not raw:
        return None
    extra = os.getenv("SOLANA_RPC_API_KEY", "").strip().strip('"').strip("'")
    low = raw.lower()
    has_key_param = "api-key=" in low or "apikey=" in low
    if extra and not has_key_param and "helius-rpc.com" in low:
        sep = "&" if "?" in raw else "?"
        raw = f"{raw}{sep}api-key={quote(extra, safe='')}"
    return raw.rstrip("/")


def _solana_rpc_bearer() -> Optional[str]:
    v = os.getenv("SOLANA_RPC_BEARER", "").strip().strip('"').strip("'")
    return v or None


EVM_PATTERN = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
SOL_PATTERN = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
# x.com/user/status/123, twitter.com/.../status/123, optional query string
STATUS_TWEET_ID_RE = re.compile(r"/status/(\d+)", re.IGNORECASE)


def parse_tweet_id_line(line: str) -> Optional[str]:
    s = line.strip()
    if not s:
        return None
    m = STATUS_TWEET_ID_RE.search(s)
    if m:
        return m.group(1)
    if re.fullmatch(r"\d+", s):
        return s
    return None


def min_reply_balance_usd_threshold() -> Optional[float]:
    """
    If None: send only when native balance > 0 (any positive).
    If set: send when estimated USD value (native × spot) >= this amount.
    """
    raw = os.getenv("MIN_REPLY_BALANCE_USD")
    if raw is None or not str(raw).strip():
        return None
    try:
        v = float(str(raw).strip())
    except ValueError:
        logger.warning("MIN_REPLY_BALANCE_USD is not a valid number; using any positive balance")
        return None
    if v <= 0:
        return None
    return v

def load_reply_texts(file_path: str = "texts.txt") -> List[str]:
    if not os.path.exists(file_path):
        return []
    with open(file_path, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]


def send_replies_to_validated_users(
    twitter: Twitter,
    validated_results: List[Dict[str, Any]],
    *,
    account_cookie: Optional[str] = None,
    account_proxy: Optional[str] = None,
):
    """
    Iterates through qualified wallet leads and dispatches automated replies when balance threshold is met.
    Threshold: MIN_REPLY_BALANCE_USD in .env (evaluated in USD via CoinGecko spot rates) — any balance > 0.
    """
    logger.section("Starting Auto-Reply Phase")

    min_usd = min_reply_balance_usd_threshold()
    eth_px = 0.0
    sol_px = 0.0
    if min_usd is None:
        logger.info("MIN_REPLY_BALANCE_USD not set — replying when balance > 0")
    else:
        px = get_eth_sol_usd()
        if not px:
            logger.error(
                "MIN_REPLY_BALANCE_USD is set but ETH/SOL USD prices are unavailable — skipping auto-replies"
            )
            return False
        eth_px, sol_px = px
        logger.info(
            f"MIN_REPLY_BALANCE_USD=${min_usd:.2f} (spot: ETH ${eth_px:,.2f}, SOL ${sol_px:,.2f})"
        )

    text_lines = load_reply_texts("texts.txt")
    fallback_with_bal = [
        "Nice! I see you have {bal} on your wallet. 🚀",
        "Wow, {bal} detected. Great balance! 🔥",
        "Checking... found {bal}. You're ready for the moon! 🌕",
        "Verified. Balance: {bal}. Good luck! 💎",
    ]
    if text_lines:
        logger.info(f"Reply texts: {len(text_lines)} line(s) from texts.txt")
    else:
        logger.warning("texts.txt missing or empty — using built-in templates with {{bal}}")

    def eth_qualifies(native: float) -> bool:
        if min_usd is None:
            return native > 0
        return native * eth_px >= min_usd

    def sol_qualifies(native: float) -> bool:
        if min_usd is None:
            return native > 0
        return native * sol_px >= min_usd

    sent_count = 0
    sent_to: List[Dict[str, Any]] = []

    for record in validated_results:
        username = record.get("username")
        reply_id = record.get("tweet_id")
        v_wallets = record.get("validated_wallets", {})

        found_balance_text: Optional[str] = None
        chosen_chain: Optional[str] = None
        chosen_address: Optional[str] = None

        # 1. Check Ethereum balances
        for eth in v_wallets.get("ethereum", []):
            if eth.get("exists") and eth.get("balance"):
                try:
                    val = float(eth["balance"].split()[0])
                    if eth_qualifies(val):
                        found_balance_text = f"{eth['balance']} (ETH)"
                        chosen_chain = "ethereum"
                        chosen_address = eth.get("address")
                        break
                except (ValueError, IndexError):
                    continue

        # 2. Check Solana balances if no ETH match
        if not found_balance_text:
            for sol in v_wallets.get("solana", []):
                if sol.get("exists") and sol.get("balance"):
                    try:
                        val = float(sol["balance"].split()[0])
                        if sol_qualifies(val):
                            found_balance_text = f"{sol['balance']} (SOL)"
                            chosen_chain = "solana"
                            chosen_address = sol.get("address")
                            break
                    except (ValueError, IndexError):
                        continue

        # 3. Dispatch reply if balance threshold met
        if found_balance_text:
            post_url: Optional[str] = record.get("url")
            if not post_url and username and reply_id:
                post_url = f"https://x.com/{username}/status/{reply_id}"

            wallet_reply_tweet_id = reply_id
            send_reply_to_tweet_id = reply_id
            if username and _env_truthy("REPLY_ON_TARGET_LATEST_POST", True):
                try:
                    latest_post = twitter.get_latest_normal_tweet(username)
                except Exception as e:
                    logger.warning(
                        f"Failed to fetch latest post for @{username} ({e}); "
                        f"reply will target thread comment ({wallet_reply_tweet_id})."
                    )
                    latest_post = None
                if latest_post and latest_post.get("tweet_id"):
                    send_reply_to_tweet_id = latest_post["tweet_id"]
                    if send_reply_to_tweet_id != wallet_reply_tweet_id:
                        logger.info(
            logger.info(f"Replying under latest organic tweet of @{username} (id={send_reply_to_tweet_id})")
                            f"excluding retweets/quotes/replies; wallets originated in reply id={wallet_reply_tweet_id}"
                        )
                elif send_reply_to_tweet_id == wallet_reply_tweet_id:
                    logger.info(
                        f"@{username}: targeting thread reply (id={wallet_reply_tweet_id}) — "
            logger.info(f"@{username}: targeting thread reply (id={wallet_reply_tweet_id}) — no standalone tweet found")
                    )

            if text_lines:
                body = random.choice(text_lines)
                if "{bal}" in body:
                    body = body.replace("{bal}", found_balance_text)
                if post_url:
                    body = body.replace("{url}", post_url).replace("{link}", post_url)
            else:
                body = random.choice(fallback_with_bal).format(bal=found_balance_text)
                if post_url:
                    body = body.replace("{url}", post_url).replace("{link}", post_url)

            if post_url and post_url not in body and _env_truthy("REPLY_APPEND_SOURCE_URL", True):
                body = f"{body.rstrip()}\n{post_url}"

            reply_text = f"@{username} {body}"

            thread_root = record.get("in_reply_to_tweet_id")
            usd_note = ""
            if min_usd is not None:
                usd_note = f" | MIN_REPLY_BALANCE_USD=${min_usd:.2f}"
            logger.info(
                f"Preparing reply for @{username}: chain={chosen_chain} | address={chosen_address} | "
                f"balance={found_balance_text}{usd_note}"
            )
            send_target_url: Optional[str] = None
            if username and send_reply_to_tweet_id:
                send_target_url = f"https://x.com/{username}/status/{send_reply_to_tweet_id}"
            logger.info(
    draft_banner = (
        f"Reply draft preview ↓
"
        f"  Thread root: {thread_root}
"
        f"  Target tweet: {send_target_url or send_reply_to_tweet_id}
"
        f"  Originating reply: {post_url or wallet_reply_tweet_id}
"
                f"  Text payload:
{reply_text}"
            )

            try:
                result = send_reply(
                    cookie=account_cookie,
                    tweet_id=send_reply_to_tweet_id,
                    text=reply_text,
                    proxy=account_proxy
                )
                
                # Try to determine if it was selenium or api for audit purposes
                # This is a bit of a hack since send_reply abstracts this, 
                # but we can check if use_selenium_reply() was true.
                send_via = "selenium" if use_selenium_reply() else "api"
                
                if isinstance(result, dict) and result.get("errors"):
                    for err in result.get("errors", []):
                        try:
                            code = int(err.get("code"))
                        except (TypeError, ValueError):
                            continue
                        if code == 344:
                            logger.error("Daily limit hit (344). Stopping sends for this account.")
                            _log_auto_reply_recipients_summary(sent_to, sent_count)
                            return True
                        if code == 226:
                            logger.error(
                                "X rejected create_tweet due to automated activity limit (226). "
            "Subsequent automated replies from this session may be restricted by platform rate limits."
                            )
                            _log_auto_reply_recipients_summary(sent_to, sent_count)
                            return True
                if "data" in result and "create_tweet" in result["data"]:
                    logger.success(f"Successfully replied to @{username}")
                    sent_count += 1
                    sent_to.append(
                        {
                            "username": username,
                            "url": send_target_url or post_url,
                            "reply_to_tweet_id": send_reply_to_tweet_id,
                            "wallet_reply_tweet_id": wallet_reply_tweet_id,
                        }
                    )
                    append_reply_audit(
                        {
                            "username": username,
                            "reply_to_tweet_id": send_reply_to_tweet_id,
                            "wallet_reply_tweet_id": wallet_reply_tweet_id,
                            "replied_to_url": send_target_url or post_url,
                            "wallet_source_url": post_url,
                            "in_reply_to_tweet_id": thread_root,
                            "reply_text": reply_text,
                            "status": "sent",
                            **({"send_via": send_via} if send_via else {}),
                            "chosen_chain": chosen_chain,
                            "chosen_address": chosen_address,
                            "balance_summary": found_balance_text,
                        }
                    )
                else:
                    err_msg = json.dumps(result)
                    logger.error(f"Failed to reply to @{username}: {err_msg}")
                    append_reply_audit(
                        {
                            "username": username,
                            "reply_to_tweet_id": send_reply_to_tweet_id,
                            "wallet_reply_tweet_id": wallet_reply_tweet_id,
                            "replied_to_url": send_target_url or post_url,
                            "wallet_source_url": post_url,
                            "in_reply_to_tweet_id": thread_root,
                            "reply_text": reply_text,
                            "status": "failed",
                            "error": err_msg[:2000],
                            **({"send_via": send_via} if send_via else {}),
                            "chosen_chain": chosen_chain,
                            "chosen_address": chosen_address,
                            "balance_summary": found_balance_text,
                        }
                    )
            except Exception as e:
                err_human = format_reply_error(e)
                logger.error(f"Failed to send reply to @{username}:
{err_human}")
                append_reply_audit(
                    {
                        "username": username,
                        "reply_to_tweet_id": send_reply_to_tweet_id,
                        "wallet_reply_tweet_id": wallet_reply_tweet_id,
                        "replied_to_url": send_target_url or post_url,
                        "wallet_source_url": post_url,
                        "in_reply_to_tweet_id": thread_root,
                        "reply_text": reply_text,
                        "status": "failed",
                        "error": (err_human + "\n---\n" + str(e))[:2000],
                        "chosen_chain": chosen_chain,
                        "chosen_address": chosen_address,
                        "balance_summary": found_balance_text,
                    }
                )

            # Rate-limit delay between replies for account safety
            wait = random.randint(25, 45)
            logger.info(f"Anti-spam delay: {wait}s...")
            time.sleep(wait)

    _log_auto_reply_recipients_summary(sent_to, sent_count)
    logger.success(f"Finished replying. Total sent: {sent_count}")
    return False


def _log_auto_reply_recipients_summary(sent_to: List[Dict[str, Any]], sent_count: int) -> None:
    """Summary list of recipients dispatched during reply phase."""
    if sent_count <= 0:
        logger.info("Automated replies: 0 dispatches sent.")
        return
    logger.subsection(f"Dispatched Replies ({sent_count})")
    for i, row in enumerate(sent_to, 1):
        u = row.get("username") or "?"
        link = row.get("url") or row.get("reply_to_tweet_id")
        logger.info(f"  {i}. @{u} → {link}")


def extract_wallets(text: str) -> Dict[str, List[str]]:
    if not text:
        return {"evm": [], "solana": []}

    evm = EVM_PATTERN.findall(text)
    sol = SOL_PATTERN.findall(text)

    evm_unique = list(dict.fromkeys(evm))
    sol_unique = list(dict.fromkeys(sol))

    return {"evm": evm_unique, "solana": sol_unique}

@dataclass(frozen=True)
class Account:
    cookie: str
    proxy: Optional[str] = None

def load_accounts(file_path: str = "accounts.txt") -> List[Account]:
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"accounts.txt not found at path: {file_path}")

    accounts: List[Account] = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Only split first two ":" so proxy can be socks5://host:port:user:pass
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
        raise ValueError("accounts.txt does not contain any valid ct0:auth[:socks5] line")
    return accounts


def load_tweet_ids(file_path: str = "tweets.txt") -> List[str]:
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"tweets.txt not found at path: {file_path}")

    tweet_ids: List[str] = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            tid = parse_tweet_id_line(raw)
            if tid:
                tweet_ids.append(tid)
            else:
                logger.warning(f"Skip tweets.txt line (not a URL with /status/ID nor a numeric ID): {raw[:80]!r}")

    if not tweet_ids:
        raise ValueError(
            "tweets.txt has no valid entries — use a numeric tweet id or a link like https://x.com/user/status/123"
        )

    return tweet_ids


def extract_field_from_reply(reply_result: Dict[str, Any]) -> Dict[str, Any]:
    # The result can be nested in 'tweet' if it's TweetWithVisibilityResults
    tweet_obj = reply_result
    if "tweet" in reply_result:
        tweet_obj = reply_result["tweet"]

    legacy = tweet_obj.get("legacy", {})
    reply_id = tweet_obj.get("rest_id") or legacy.get("id_str")

    core = tweet_obj.get("core", {})
    user_results = core.get("user_results", {}).get("result", {})
    user_legacy = user_results.get("legacy", {})

    username = user_legacy.get("screen_name")
    text = legacy.get("full_text")
    created_at = legacy.get("created_at")
    favorite_count = legacy.get("favorite_count")

    url: Optional[str] = None
    if username and reply_id:
        url = f"https://x.com/{username}/status/{reply_id}"

    return {
        "tweet_id": reply_id,
        "username": username,
        "full_text": text,
        "created_at": created_at,
        "favorite_count": favorite_count,
        "url": url,
    }


def fetch_replies_for_tweet(
    twitter: Twitter,
    tweet_id: str,
    sleep_seconds: float = 2.0,
) -> List[Dict[str, Any]]:
    all_replies: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    seen_reply_ids = set()
    page = 0

    logger.push_indent()
    while True:
        page += 1
        logger.debug(f"Fetching page {page}...", cursor=cursor[:20] + "..." if cursor else None)
        
        try:
            response = twitter.get_tweet_detail(tweet_id, cursor=cursor)
        except Exception as e:
            logger.error(f"Failed to fetch page {page}: {e}")
            break

        if not isinstance(response, dict):
            logger.warning("Invalid response format")
            break

        data = response.get("data", {})
        conversation = data.get("threaded_conversation_with_injections_v2", {})
        instructions = conversation.get("instructions", [])

        next_cursor = None
        current_batch_replies = []
        # #region agent log
        _dbg_entry_types: Dict[str, int] = {}
        _dbg_no_result = 0
        _dbg_dup = 0
        _dbg_focal = 0
        _n_timeline_add_entries = 0
        prev_cursor = cursor
        entries_found = 0
        # #endregion

        for instr in instructions:
            if instr.get("type") != "TimelineAddEntries":
                continue
            # #region agent log
            _n_timeline_add_entries += 1
            # #endregion
            
            entries = instr.get("entries", [])
            entries_found = 0

            for entry in entries:
                entry_id = entry.get("entryId", "")
                content = entry.get("content", {})
                item_type = content.get("entryType")
                # #region agent log
                if item_type:
                    _dbg_entry_types[item_type] = _dbg_entry_types.get(item_type, 0) + 1
                # #endregion

                # Parse Tweets
                if item_type == "TimelineTimelineItem":
                    item_content = content.get("itemContent", {})
                    if item_content.get("itemType") == "TimelineTweet":
                        tweet_results = item_content.get("tweet_results", {})
                        result = tweet_results.get("result")
                        # #region agent log
                        if not result:
                            _dbg_no_result += 1
                        elif entry_id == f"tweet-{tweet_id}":
                            _dbg_focal += 1
                        else:
                            reply_data = extract_field_from_reply(result)
                            rid = reply_data["tweet_id"]
                            if rid and rid not in seen_reply_ids:
                                current_batch_replies.append(reply_data)
                                seen_reply_ids.add(rid)
                                entries_found += 1
                            elif rid:
                                _dbg_dup += 1
                        # #endregion

                # Parse Conversation Modules (Threads)
                elif item_type == "TimelineTimelineModule":
                    items = content.get("items", [])
                    for module_item in items:
                        item = module_item.get("item", {})
                        item_content = item.get("itemContent", {})
                        if item_content.get("itemType") == "TimelineTweet":
                            tweet_results = item_content.get("tweet_results", {})
                            result = tweet_results.get("result")
                            # #region agent log
                            if not result:
                                _dbg_no_result += 1
                            else:
                                reply_data = extract_field_from_reply(result)
                                rid = reply_data["tweet_id"]
                                if rid and rid not in seen_reply_ids:
                                    current_batch_replies.append(reply_data)
                                    seen_reply_ids.add(rid)
                                    entries_found += 1
                                elif rid:
                                    _dbg_dup += 1
                            # #endregion

                # Parse Cursor
                elif item_type == "TimelineTimelineCursor":
                    if content.get("cursorType") == "Bottom":
                        next_cursor = content.get("value")

        # #region agent log
        _agent_debug_log(
            "H1-H5",
            "main.py:fetch_replies_for_tweet",
            "page_parse_summary",
            {
                "page": page,
                "tweet_id": str(tweet_id),
                "logged_entries_found_last_instr_only": entries_found,
                "page_new_tweets_in_batch": len(current_batch_replies),
                "dup_rid_skips": _dbg_dup,
                "timeline_tweet_no_result": _dbg_no_result,
                "focal_skips": _dbg_focal,
                "entry_types": _dbg_entry_types,
                "n_timeline_add_entries": _n_timeline_add_entries,
                "instruction_types": [
                    i.get("type") for i in instructions if isinstance(i, dict)
                ],
                "has_next_cursor": bool(next_cursor),
                "next_cursor_prefix": (next_cursor[:24] + "…") if next_cursor else None,
                "prev_cursor_prefix": (prev_cursor[:24] + "…") if prev_cursor else None,
            },
        )
        # #endregion

        page_new_count = len(current_batch_replies)
        logger.debug(f"Page {page}: Found {page_new_count} new replies")

        if not current_batch_replies and not next_cursor:
            logger.debug("No more replies found")
            break

        if not current_batch_replies and next_cursor:
            logger.debug("No tweet entries this page (cursor-only tail); stopping pagination")
            # #region agent log
            _agent_debug_log(
                "FIX-verify",
                "main.py:fetch_replies_for_tweet",
                "stop_cursor_only_tail",
                {"page": page, "tweet_id": str(tweet_id)},
                run_id="post-fix",
            )
            # #endregion
            break

        for base_fields in current_batch_replies:
            wallets = extract_wallets(base_fields.get("full_text") or "")
            if not wallets.get("evm") and not wallets.get("solana"):
                continue
            record = {
                "in_reply_to_tweet_id": tweet_id,
                **base_fields,
                "wallets": wallets,
            }
            all_replies.append(record)

        if not next_cursor or next_cursor == cursor:
            logger.debug("No next cursor available")
            break

        cursor = next_cursor
        time.sleep(sleep_seconds)

    logger.pop_indent()
    return all_replies


def validate_wallets_and_save(
    all_results: List[Dict[str, Any]], output_path: str = "validated_wallets.json"
) -> List[Dict[str, Any]]:
    """
    Validate extracted wallets via Etherscan and Alchemy (unique addresses per run).
    Skips replies without wallet addresses. Optional: FETCH_ETH_TX_COUNT / FETCH_SOL_TOKEN_ACCOUNTS.
    """
    etherscan_key = os.getenv("ETHERSCAN_API_KEY")
    alchemy_key = _solana_alchemy_key()
    sol_rpc_url = _solana_rpc_url()
    sol_bearer = _solana_rpc_bearer()

    if not etherscan_key:
        logger.warning("Etherscan API key not configured. Skipping Ethereum validation.")
    if not sol_rpc_url and not alchemy_key:
        logger.warning(
            "No Solana RPC: set SOLANA_RPC_URL (full HTTPS URL) or ALCHEMY_SOLANA_KEY / SOLSCAN_API_KEY (Alchemy app key only, not Solscan JWT)."
        )

    validator = BlockchainValidator(
        etherscan_key,
        alchemy_key,
        solana_rpc_url=sol_rpc_url,
        solana_rpc_bearer=sol_bearer,
    )

    eth_available = validator.etherscan.is_available if validator.etherscan else False
    sol_available = validator.solscan.is_available if validator.solscan else False

    if not eth_available and not sol_available:
        logger.warning("All blockchain APIs are unavailable. Skipping validation.")
        return []

    if validator.etherscan and not eth_available:
        logger.warning("Etherscan API is unavailable - skipping Ethereum validation")
    if validator.solscan and not sol_available:
        detail = getattr(validator.solscan, "last_availability_error", None) or "unknown"
        hint = (
            "http_401 = invalid/expired API key or missing key parameter. "
            "Alchemy: full URL .../v2/<APP_KEY> or ALCHEMY_SOLANA_KEY. "
            "Helius: URL with ?api-key=... or SOLANA_RPC_URL + SOLANA_RPC_API_KEY. "
            "Other RPC providers: configure SOLANA_RPC_BEARER if required."
        )
        if "401" not in str(detail):
            hint = (
        "Provide SOLANA_RPC_URL (full HTTPS) or ALCHEMY_SOLANA_KEY in .env. "
                "See .env.example."
            )
        logger.warning(
            f"Solana RPC check failed ({detail}) — skipping Solana validation. {hint}"
        )

    workers = _validation_workers()
    fetch_eth_tx = _env_truthy("FETCH_ETH_TX_COUNT", False)
    fetch_sol_tokens = _env_truthy("FETCH_SOL_TOKEN_ACCOUNTS", False)
    if workers > 1:
        logger.info(f"WALLET_VALIDATE_WORKERS={workers} (parallel validation; watch for API rate limits)")

    unique_evm: List[str] = list(
        dict.fromkeys(
            a for r in all_results for a in r.get("wallets", {}).get("evm", [])
        )
    )
    unique_sol: List[str] = list(
        dict.fromkeys(
            a for r in all_results for a in r.get("wallets", {}).get("solana", [])
        )
    )

    eth_by_addr: Dict[str, Dict[str, Any]] = {}
    if unique_evm and eth_available and validator.etherscan:
        logger.info(f"Validating {len(unique_evm)} unique Ethereum address(es)...")
        logger.push_indent()
        eth_results = validator.etherscan.get_multiple_wallets(
            unique_evm,
            fetch_transaction_count=fetch_eth_tx,
            max_workers=workers,
        )
        for w in eth_results:
            logger.wallet_result(w.address, "ethereum", w.exists, w.balance, w.error)
            eth_by_addr[w.address] = {
                "address": w.address,
                "exists": w.exists,
                "balance": w.balance,
                "transaction_count": w.transaction_count,
                "error": w.error,
            }
        logger.pop_indent()

    sol_by_addr: Dict[str, Dict[str, Any]] = {}
    if unique_sol and sol_available and validator.solscan:
        logger.info(f"Validating {len(unique_sol)} unique Solana address(es)...")
        logger.push_indent()
        sol_results = validator.solscan.get_multiple_wallets(
            unique_sol,
            fetch_token_accounts=fetch_sol_tokens,
            max_workers=workers,
        )
        for w in sol_results:
            logger.wallet_result(w.address, "solana", w.exists, w.balance, w.error)
            sol_by_addr[w.address] = {
                "address": w.address,
                "exists": w.exists,
                "balance": w.balance,
                "token_count": w.token_count,
                "error": w.error,
            }
        logger.pop_indent()

    validated_results: List[Dict[str, Any]] = []
    skipped_count = 0

    logger.info("Mapping validation results to replies...")
    logger.push_indent()

    for reply_idx, reply in enumerate(all_results, 1):
        wallets = reply.get("wallets", {})
        evm_count = len(wallets.get("evm", []))
        solana_count = len(wallets.get("solana", []))

        if evm_count + solana_count == 0:
            skipped_count += 1
            logger.info(f"Reply {reply_idx}: no wallet addresses found — skipping")
            continue

        validated_wallets: Dict[str, List[Dict[str, Any]]] = {"ethereum": [], "solana": []}

        if wallets.get("evm") and eth_available:
            for addr in wallets["evm"]:
                row = eth_by_addr.get(addr)
                if row is not None:
                    validated_wallets["ethereum"].append(dict(row))

        if wallets.get("solana") and sol_available:
            for addr in wallets["solana"]:
                row = sol_by_addr.get(addr)
                if row is not None:
                    validated_wallets["solana"].append(dict(row))

        reply["validated_wallets"] = validated_wallets
        validated_results.append(reply)

        un = reply.get("username") or "?"
        tid = reply.get("tweet_id")
        r_url = reply.get("url")
        if not r_url and un != "?" and tid:
            r_url = f"https://x.com/{un}/status/{tid}"
        logger.info(
            f"Reply {reply_idx}: @{un} | detected {evm_count} EVM + {solana_count} Solana addresses"
        )
        detail_lines = [
            f"  Tweet link: {r_url or tid}",
            "  Validation results for this reply:",
        ]
        for row in validated_wallets["ethereum"]:
            detail_lines.append(
                f"    ETH {row.get('address')} | exists={row.get('exists')} | "
                f"balance={row.get('balance')!s} | error={row.get('error')!s}"
            )
        for row in validated_wallets["solana"]:
            detail_lines.append(
                f"    SOL {row.get('address')} | exists={row.get('exists')} | "
                f"balance={row.get('balance')!s} | error={row.get('error')!s}"
            )
        if not validated_wallets["ethereum"] and not validated_wallets["solana"]:
            detail_lines.append("    (no validation lines — chain APIs unreachable or invalid address)")
        logger.info("\n".join(detail_lines))

    logger.pop_indent()

    try:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(validated_results, f, ensure_ascii=False, indent=2)

        valid_eth = sum(
            1
            for r in validated_results
            for w in r.get("validated_wallets", {}).get("ethereum", [])
            if w.get("exists")
        )
        valid_sol = sum(
            1
            for r in validated_results
            for w in r.get("validated_wallets", {}).get("solana", [])
            if w.get("exists")
        )
        stats = {
            "Total replies processed": len(all_results),
            "Replies with wallets": len(validated_results),
            "Replies skipped (no wallets)": skipped_count,
            "Valid Ethereum": valid_eth,
            "Valid Solana": valid_sol,
            "Unique EVM validated": len(eth_by_addr),
            "Unique Solana validated": len(sol_by_addr),
        }
        logger.success(f"Validated wallets saved to {output_path}")
        logger.stats("Validation Summary", stats)
    except Exception as e:
        logger.error(f"Failed to write {output_path}: {e}")

    return validated_results


def try_delete_file(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        logger.warning(f"Failed to delete {path}: {e}")


def scratch_json_paths(tweet_id: str) -> Tuple[str, str]:
    """Per-tweet scratch files so parallel workers do not clobber each other."""
    safe = re.sub(r"[^0-9A-Za-z_-]", "_", tweet_id).strip("_") or "tweet"
    return (
        f"replies_with_wallets_{safe}.json",
        f"validated_wallets_{safe}.json",
    )


def main() -> None:
    logger.panel(
        "Logging",
        {
            "TWITTER_LOG_LEVEL": os.getenv("TWITTER_LOG_LEVEL", "max"),
            "verbose": logger.verbose_enabled,
            "trace": logger.trace_enabled,
        },
    )
    logger.section("Twitter Wallet Scraper & Blockchain Validator")

    try:
        logger.info("Loading Twitter credentials...")
        accounts = load_accounts("accounts.txt")
        logger.success(f"Loaded {len(accounts)} account(s)")
    except (FileNotFoundError, ValueError) as e:
        logger.critical(f"Cookie load error: {e}")
        sys.exit(1)

    try:
        logger.info("Loading tweet IDs...")
        tweet_ids = load_tweet_ids("tweets.txt")
        logger.success(f"Loaded {len(tweet_ids)} tweet ID(s)")
    except (FileNotFoundError, ValueError) as e:
        logger.critical(f"Tweet IDs load error: {e}")
        sys.exit(1)

    logger.section("Fetching Tweet Replies")
    work: "Queue[str]" = Queue()
    for tweet_id in tweet_ids:
        work.put(tweet_id)

    def worker(account: Account) -> None:
        twitter = Twitter(account.cookie, proxy=account.proxy)
        while True:
            try:
                tweet_id = work.get_nowait()
            except Empty:
                return

            output_path, validated_path = scratch_json_paths(tweet_id)

            logger.subsection(f"Tweet {tweet_id}")
            logger.push_indent()
            try_delete_file(output_path)
            try_delete_file(validated_path)
            try:
                replies = fetch_replies_for_tweet(twitter, tweet_id)
                logger.success(f"Fetched {len(replies)} replies", tweet_id=tweet_id)

                wallet_count = sum(
                    len(r["wallets"]["evm"]) + len(r["wallets"]["solana"])
                    for r in replies
                )
                logger.info(f"Found {wallet_count} wallet addresses", indent=True)

                logger.section("Blockchain Wallet Validation")
                try:
                    validated_data = validate_wallets_and_save(replies, validated_path)
                    if validated_data:
                        hit_limit = send_replies_to_validated_users(
                            twitter,
                            validated_data,
                            account_cookie=account.cookie,
                            account_proxy=account.proxy,
                        )
                        if hit_limit:
                            return
                    else:
                        logger.warning("No validated wallets to reply to.")
                except Exception as e:
                    logger.error(f"Error during auto-reply phase: {e}")
            except Exception as e:
                logger.error(f"Failed to process tweet: {e}")
            finally:
                try_delete_file(output_path)
                try_delete_file(validated_path)
                logger.pop_indent()
                work.task_done()

    threads: List[Thread] = []
    for acc in accounts:
        t = Thread(target=worker, args=(acc,), daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

if __name__ == "__main__":
    main()
