import json
from typing import Optional, Dict, Any
from Twitter import Twitter
from debug_logger import logger

def send_api_reply(cookie: str, tweet_id: str, text: str, proxy: Optional[str] = None) -> Dict[str, Any]:
    """
    Sends a reply using the Twitter API (GraphQL).
    """
    try:
        twitter = Twitter(cookie, proxy=proxy)
        result = twitter.reply_to_tweet(text=text, in_reply_to_tweet_id=tweet_id)
        return result
    except Exception as e:
        logger.error(f"API Reply Error: {e}")
        return {"errors": [{"message": str(e)}]}
