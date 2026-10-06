# Blockchain API Setup Guide

## Overview

The project now integrates with **Etherscan** and **Solscan** APIs to validate extracted wallet addresses.

## Files Added

- **`blockchain_api.py`** - Contains `EtherscanAPI`, `SolscanAPI`, and `BlockchainValidator` classes
- **`config.json`** - Configuration file (optional, for future use)
- **`.env.example`** - Template for API keys

## Getting API Keys

### Etherscan API Key

1. Go to https://etherscan.io/apis
2. Click "Create New API Key" (requires registration)
3. Name your app and create it
4. Copy the API key

### Solscan API Key

1. Go to https://solscan.io (or use API without key for basic requests)
2. Some endpoints require API key from https://solscan.io/api-key
3. Copy your API key (optional, but recommended for higher rate limits)

## Configuration

### Step 1: Copy `.env.example` to `.env`

```bash
cp .env.example .env
```

### Step 2: Add Your API Keys

Edit `.env` file:

```
ETHERSCAN_API_KEY=your_etherscan_api_key_here
SOLSCAN_API_KEY=your_solscan_api_key_here
```

## Usage

### Basic Usage (with wallet validation)

```bash
python main.py
```

The script will:
1. Extract wallets from tweet replies
2. Validate them on Etherscan (Ethereum/EVM)
3. Validate them on Solscan (Solana)
4. Save results to `validated_wallets.json`

### Output Format

The `validated_wallets.json` file contains replies with validated wallet info:

```json
[
  {
    "in_reply_to_tweet_id": "2038352438206730",
    "tweet_id": "...",
    "username": "...",
    "full_text": "...",
    "created_at": "...",
    "url": "...",
    "wallets": {
      "evm": ["0x...", "0x..."],
      "solana": ["address1", "address2"]
    },
    "validated_wallets": {
      "ethereum": [
        {
          "address": "0x...",
          "exists": true,
          "balance": "1.2345 ETH",
          "transaction_count": 42,
          "error": null
        }
      ],
      "solana": [
        {
          "address": "...",
          "exists": true,
          "balance": "5.6789 SOL",
          "token_count": 15,
          "error": null
        }
      ]
    }
  }
]
```

## API Classes

### `EtherscanAPI`

Validates Ethereum and EVM-compatible addresses:

```python
from blockchain_api import EtherscanAPI

api = EtherscanAPI("your_api_key")
info = api.validate_address("0x...")
print(f"Balance: {info.balance}")
print(f"Transactions: {info.transaction_count}")
```

### `SolscanAPI`

Validates Solana addresses:

```python
from blockchain_api import SolscanAPI

api = SolscanAPI("your_api_key")
info = api.validate_address("...")
print(f"Balance: {info.balance}")
print(f"Token accounts: {info.token_count}")
```

### `BlockchainValidator`

Unified validator for both chains:

```python
from blockchain_api import BlockchainValidator

validator = BlockchainValidator(
    etherscan_key="your_etherscan_key",
    solscan_key="your_solscan_key"
)

# Validate single wallet
info = validator.validate_wallet("0x...", "ethereum")

# Validate multiple wallets
wallets = {"evm": ["0x...", "0x..."], "solana": ["...", "..."]}
results = validator.validate_multiple_wallets(wallets)
```

## Rate Limiting

- **Etherscan**: Free tier: 5 calls/second, 100,000 calls/day
- **Solscan**: Rate limited, 0.2s delay between requests

The code automatically adds delays between requests.

## Without API Keys

If you don't have API keys configured:
- The script will still extract wallets
- Validation will be skipped
- A warning message will be displayed

## Troubleshooting

### "API key not configured"
- Make sure `.env` file exists and has the correct format
- Check that API keys are not commented out (no `#` prefix)

### "Address not found"
- The address might be invalid
- It might not exist on that blockchain
- Check Etherscan/Solscan directly to verify

### Rate limit exceeded
- Reduce `batch_size` in `config.json`
- Increase delay in code if processing many addresses
- Consider upgrading API key tier

## Optional: Using config.json

For future enhancements, `config.json` can be used:

```json
{
  "apis": {
    "etherscan": {
      "api_key": "YOUR_KEY",
      "enabled": true
    },
    "solscan": {
      "api_key": "YOUR_KEY",
      "enabled": true
    }
  }
}
```

Currently the code uses `.env` file, but you can refactor to use `config.json` if needed.
