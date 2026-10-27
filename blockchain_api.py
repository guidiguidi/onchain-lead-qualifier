import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional
from dataclasses import dataclass


@dataclass
class WalletInfo:
    address: str
    chain: str  # "ethereum" or "solana"
    exists: bool = None
    balance: Optional[str] = None
    transaction_count: Optional[int] = None
    token_count: Optional[int] = None
    error: Optional[str] = None


class EtherscanAPI:
    def __init__(self, api_key: str, base_url: str = "https://api.etherscan.io/api"):
        self.api_key = api_key
        self.base_url = base_url
        self.session = requests.Session()
        try:
            self.is_available = self._check_availability()
        except Exception:
            self.is_available = False

    def _check_availability(self) -> bool:
        """Check if Etherscan API is reachable"""
        try:
            response = self.session.head("https://api.etherscan.io", timeout=5)
            return response.status_code < 500
        except Exception:
            return False

    def validate_address(
        self,
        address: str,
        *,
        fetch_transaction_count: bool = False,
    ) -> WalletInfo:
        """
        Validate Ethereum address and get balance; optional internal tx count (extra request).
        """
        if not address or not address.startswith("0x"):
            return WalletInfo(
                address=address,
                chain="ethereum",
                exists=False,
                error="Invalid address format"
            )

        try:
            params = {
                "module": "account",
                "action": "balance",
                "address": address,
                "tag": "latest",
                "apikey": self.api_key
            }
            
            response = self.session.get(self.base_url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "0":
                return WalletInfo(
                    address=address,
                    chain="ethereum",
                    exists=False,
                    error=data.get("message", "Address not found")
                )

            balance = data.get("result", "0")
            balance_eth = float(balance) / 1e18

            tx_count: Optional[int] = None
            if fetch_transaction_count:
                params_tx = {
                    "module": "account",
                    "action": "txlistinternal",
                    "address": address,
                    "startblock": 0,
                    "endblock": 99999999,
                    "sort": "asc",
                    "apikey": self.api_key
                }
                
                response_tx = self.session.get(self.base_url, params=params_tx, timeout=10)
                response_tx.raise_for_status()
                data_tx = response_tx.json()
                
                tx_count = len(data_tx.get("result", [])) if data_tx.get("status") == "1" else 0

            return WalletInfo(
                address=address,
                chain="ethereum",
                exists=True,
                balance=f"{balance_eth:.4f} ETH",
                transaction_count=tx_count
            )

        except requests.RequestException as e:
            return WalletInfo(
                address=address,
                chain="ethereum",
                exists=False,
                error=f"API error: {str(e)}"
            )

    def get_multiple_wallets(
        self,
        addresses: List[str],
        *,
        fetch_transaction_count: bool = False,
        max_workers: int = 1,
        sequential_delay_sec: float = 0.25,
    ) -> List[WalletInfo]:
        """
        Get info for multiple Ethereum addresses with rate limiting.
        If max_workers > 1, requests run in parallel (higher 429 risk); sequential_delay_sec is ignored.
        """
        if not self.is_available:
            return [
                WalletInfo(
                    address=addr,
                    chain="ethereum",
                    exists=None,
                    error="Etherscan API unavailable - skipping validation"
                )
                for addr in addresses
            ]

        def one(addr: str) -> WalletInfo:
            try:
                return self.validate_address(addr, fetch_transaction_count=fetch_transaction_count)
            except Exception as e:
                return WalletInfo(
                    address=addr,
                    chain="ethereum",
                    exists=None,
                    error=f"Etherscan API unavailable: {type(e).__name__}"
                )

        if max_workers <= 1:
            results: List[WalletInfo] = []
            for address in addresses:
                results.append(one(address))
                time.sleep(sequential_delay_sec)
            return results

        results: List[Optional[WalletInfo]] = [None] * len(addresses)

        def task(idx: int, addr: str) -> tuple:
            return idx, one(addr)

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(task, i, a) for i, a in enumerate(addresses)]
            for fut in as_completed(futures):
                idx, info = fut.result()
                results[idx] = info
        return [r for r in results]  # type: ignore[list-item]


def _rpc_slot_ok(result: Any) -> bool:
    if isinstance(result, int):
        return True
    if isinstance(result, str) and result.isdigit():
        return True
    return False


class SolscanAPI:
    def __init__(
        self,
        api_key: Optional[str] = None,
        rpc_url: Optional[str] = None,
        bearer_token: Optional[str] = None,
    ):
        self.last_availability_error: Optional[str] = None
        self.api_key = (api_key or "").strip().strip('"').strip("'")
        self.session = requests.Session()
        self.headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0"
        }
        bt = (bearer_token or "").strip().strip('"').strip("'")
        if bt:
            self.headers["Authorization"] = f"Bearer {bt}"
        ru = (rpc_url or "").strip().strip('"').strip("'").rstrip("/")
        if ru:
            self.base_url = ru
        elif self.api_key:
            self.base_url = f"https://solana-mainnet.g.alchemy.com/v2/{self.api_key}"
        else:
            self.base_url = ""
            self.is_available = False
            self.last_availability_error = "no_rpc_url_or_api_key"
            return
        try:
            self.is_available = self._check_availability()
        except Exception as e:
            self.last_availability_error = f"{type(e).__name__}:{str(e)[:160]}"
            self.is_available = False

    def _check_availability(self) -> bool:
        """JSON-RPC probe: getSlot, then getLatestBlockhash (different RPC providers handle slot queries differently)."""
        probes: List[tuple] = [
            ("getSlot", []),
            ("getLatestBlockhash", [{"commitment": "finalized"}]),
        ]
        last_err: Optional[str] = None
        for method, params in probes:
            try:
                payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
                response = self.session.post(
                    self.base_url, json=payload, headers=self.headers, timeout=15
                )
                if response.status_code != 200:
                    last_err = f"http_{response.status_code}"
                    continue
                data = response.json()
                err = data.get("error")
                if err:
                    last_err = str(err)[:200]
                    continue
                r = data.get("result")
                if method == "getSlot" and _rpc_slot_ok(r):
                    self.last_availability_error = None
                    return True
                if method == "getLatestBlockhash" and isinstance(r, dict) and r.get("blockhash"):
                    self.last_availability_error = None
                    return True
            except Exception as e:
                last_err = f"{type(e).__name__}:{str(e)[:120]}"
                continue
        self.last_availability_error = last_err or "probe_failed"
        return False

    def validate_address(
        self,
        address: str,
        *,
        fetch_token_accounts: bool = False,
    ) -> WalletInfo:
        """
        Validate Solana address via Alchemy JSON-RPC.
        fetch_token_accounts adds a second RPC call (SPL token list).
        """
        if not address or len(address) < 32:
            return WalletInfo(
                address=address,
                chain="solana",
                exists=False,
                error="Invalid Solana address format"
            )

        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getAccountInfo",
                "params": [address, {"encoding": "base64"}]
            }
            
            response = self.session.post(self.base_url, json=payload, headers=self.headers, timeout=10)
            response.raise_for_status()
            data = response.json()

            if "error" in data or data.get("result") is None:
                return WalletInfo(
                    address=address,
                    chain="solana",
                    exists=False,
                    error="Address not found on Solana"
                )

            account_info = data.get("result", {}).get("value", {})
            if account_info is None:
                return WalletInfo(
                    address=address,
                    chain="solana",
                    exists=False,
                    error="Account not found"
                )

            lamports = account_info.get("lamports", 0)
            balance_sol = lamports / 1e9

            token_count: Optional[int] = None
            if fetch_token_accounts:
                token_payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [address, {"programId": "TokenkegQfeZyiNwAJsyFbPVwwQQkYk5SLw1DkNvzJ"}]
                }
                
                token_response = self.session.post(self.base_url, json=token_payload, headers=self.headers, timeout=10)
                token_data = token_response.json()
                token_count = len(token_data.get("result", {}).get("value", [])) if "result" in token_data else 0

            return WalletInfo(
                address=address,
                chain="solana",
                exists=True,
                balance=f"{balance_sol:.4f} SOL",
                token_count=token_count
            )

        except requests.RequestException as e:
            return WalletInfo(
                address=address,
                chain="solana",
                exists=False,
                error=f"API error: {str(e)}"
            )

    def get_multiple_wallets(
        self,
        addresses: List[str],
        *,
        fetch_token_accounts: bool = False,
        max_workers: int = 1,
        sequential_delay_sec: float = 0.3,
    ) -> List[WalletInfo]:
        """
        Get info for multiple Solana addresses with rate limiting.
        If max_workers > 1, requests run in parallel (higher rate-limit risk); sequential_delay_sec is ignored.
        """
        if not self.is_available:
            return [
                WalletInfo(
                    address=addr,
                    chain="solana",
                    exists=None,
                    error="Solana API unavailable - skipping validation"
                )
                for addr in addresses
            ]

        def one(addr: str) -> WalletInfo:
            try:
                return self.validate_address(addr, fetch_token_accounts=fetch_token_accounts)
            except Exception as e:
                return WalletInfo(
                    address=addr,
                    chain="solana",
                    exists=None,
                    error=f"Solana API unavailable: {type(e).__name__}"
                )

        if max_workers <= 1:
            results: List[WalletInfo] = []
            for address in addresses:
                results.append(one(address))
                time.sleep(sequential_delay_sec)
            return results

        results: List[Optional[WalletInfo]] = [None] * len(addresses)

        def task(idx: int, addr: str) -> tuple:
            return idx, one(addr)

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(task, i, a) for i, a in enumerate(addresses)]
            for fut in as_completed(futures):
                idx, info = fut.result()
                results[idx] = info
        return [r for r in results]  # type: ignore[list-item]


class BlockchainValidator:
    def __init__(
        self,
        etherscan_key: Optional[str] = None,
        solana_api_key: Optional[str] = None,
        solana_rpc_url: Optional[str] = None,
        solana_rpc_bearer: Optional[str] = None,
    ):
        self.etherscan = EtherscanAPI(etherscan_key) if etherscan_key else None
        ru = (solana_rpc_url or "").strip() or None
        sk = (solana_api_key or "").strip() or None
        bearer_token = (solana_rpc_bearer or "").strip() or None
        if ru or sk:
            self.solscan = SolscanAPI(
                api_key=sk,
                rpc_url=ru,
                bearer_token=bearer_token,
            )
        else:
            self.solscan = None

    def validate_wallet(self, address: str, chain: str) -> WalletInfo:
        """
        Validate wallet on specified chain
        """
        if chain == "ethereum" or address.startswith("0x"):
            if not self.etherscan:
                return WalletInfo(
                    address=address,
                    chain="ethereum",
                    exists=False,
                    error="Etherscan API not configured"
                )
            return self.etherscan.validate_address(address, fetch_transaction_count=True)

        elif chain == "solana":
            if not self.solscan:
                return WalletInfo(
                    address=address,
                    chain="solana",
                    exists=False,
                    error="Solscan API not configured"
                )
            return self.solscan.validate_address(address, fetch_token_accounts=True)

        return WalletInfo(
            address=address,
            chain=chain,
            exists=False,
            error=f"Unknown chain: {chain}"
        )

    def validate_multiple_wallets(self, wallets: Dict[str, List[str]]) -> Dict[str, List[WalletInfo]]:
        """
        Validate all wallets from extracted data
        """
        results = {}

        if "evm" in wallets and self.etherscan:
            results["ethereum"] = self.etherscan.get_multiple_wallets(
                wallets["evm"],
                fetch_transaction_count=True,
            )

        if "solana" in wallets and self.solscan:
            results["solana"] = self.solscan.get_multiple_wallets(
                wallets["solana"],
                fetch_token_accounts=True,
            )

        return results
