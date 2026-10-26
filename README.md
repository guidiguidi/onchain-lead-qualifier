# On-Chain Lead Qualifier & Social Intelligence Engine (Camoufox & Web3)

An enterprise data enrichment pipeline combining **Web3 Blockchain Analytics** (Ethereum & Solana) with **Camoufox Anti-Detect Browser Automation** and Twitter GraphQL scraping.

## 🚀 Key Capabilities

- **On-Chain Address Sniffing:** Detects and parses Ethereum (`0x...`) and Solana (Base58) addresses from real-time social streams.
- **Multi-Chain Wallet Qualification:**
  - Queries **Etherscan** and **Solscan / RPC** APIs to evaluate wallet balances, token counts, and on-chain transaction activity.
  - Fetches live ETH/USD and SOL/USD spot rates via CoinGecko API (`usd_prices.py`) with monotonic TTL caching.
  - Filters out bots, dust wallets, and inactive accounts based on customizable USD portfolio thresholds.
- **Next-Gen Dispatching Layer (Camoufox Stealth):**
  - **Camoufox Engine (`reply_sender/camoufox_sender.py`):** Uses C++-modified anti-detect Firefox to bypass Cloudflare Turnstile, Kasada, and bot detection natively.
  - **Fast GraphQL API Mode (`reply_sender/api_sender.py`):** Direct HTTP execution for high-speed dispatching.
  - **Failover / Fallback Pattern:** Seamless failover between Camoufox, API, and legacy Selenium engines.
- **Thread-Safe Architecture:** Worker pools using Python `queue.Queue`, `threading.Lock`, and `ThreadPoolExecutor`.

## ⚙️ Configuration

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Configure your parameters:
- `ETHERSCAN_API_KEY`: Etherscan API key (free tier works).
- `SOLSCAN_API_KEY`: Solscan / Solana RPC token.
- `MIN_WALLET_VALUE_USD`: Minimum portfolio USD balance required to qualify.
- `SENDER_BACKEND`: `camoufox` (default, recommended), `api`, or `selenium`.

## 📦 Quickstart

```bash
pip install -r requirements.txt
python -m camoufox fetch
python main.py
```
