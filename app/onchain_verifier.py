"""
Real On-Chain Payment Verifier (Zero Custody, Zero Payment Processor KYC).
Verifies real ERC-20 USDC/USDT transfers on Base / Arbitrum / Polygon and
SPL USDC/USDT transfers on Solana directly via public JSON-RPC endpoints.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import StudioConfig

# keccak256("Transfer(address,address,uint256)")
ERC20_TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)

NATIVE_ASSET_SPECS: dict[str, tuple[str, str]] = {
    "ethereum": ("ETH", "coingecko:ethereum"),
    "base": ("ETH", "coingecko:ethereum"),
    "arbitrum": ("ETH", "coingecko:ethereum"),
    "bsc": ("BNB", "coingecko:binancecoin"),
    "polygon": ("POL", "coingecko:polygon-ecosystem-token"),
    "solana": ("SOL", "coingecko:solana"),
    "bitcoin": ("BTC", "coingecko:bitcoin"),
}

_SPOT_PRICE_CACHE: dict[str, tuple[float, float]] = {}


def _http_get_json(url: str, timeout_s: float = 5.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "chainguard-studio/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_native_usd_price(network: str) -> float:
    """Fetch cached live USD spot price for the network native gas/L1 asset."""
    net = (network or "").strip().lower()
    spec = NATIVE_ASSET_SPECS.get(net)
    if not spec:
        return 0.0
    sym, llama_id = spec
    now = time.time()
    cached = _SPOT_PRICE_CACHE.get(llama_id)
    if cached and (now - cached[1]) < 60.0 and cached[0] > 0:
        return cached[0]
    try:
        ids = ",".join(sorted({v[1] for v in NATIVE_ASSET_SPECS.values()}))
        data = _http_get_json(f"https://coins.llama.fi/prices/current/{ids}", timeout_s=5.0)
        coins = (data or {}).get("coins") or {}
        for _, v_id in NATIVE_ASSET_SPECS.values():
            px = float((coins.get(v_id) or {}).get("price") or 0.0)
            if px > 0:
                _SPOT_PRICE_CACHE[v_id] = (px, now)
        if llama_id in _SPOT_PRICE_CACHE:
            return _SPOT_PRICE_CACHE[llama_id][0]
    except Exception:
        pass
    try:
        cb = _http_get_json(f"https://api.coinbase.com/v2/prices/{sym}-USD/spot", timeout_s=4.0)
        px = float(((cb or {}).get("data") or {}).get("amount") or 0.0)
        if px > 0:
            _SPOT_PRICE_CACHE[llama_id] = (px, now)
            return px
    except Exception:
        pass
    return 0.0


@dataclass
class VerificationResult:
    verified: bool
    network: str
    token: str
    amount_usd: float
    recipient: str
    sender: str
    tx_hash: str
    reason: str
    raw_proof: dict[str, Any]


def _rpc_post(url: str, payload: dict[str, Any], timeout_s: float = 10.0) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "chainguard-studio/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _evm_rpc_urls(network: str) -> list[str]:
    primary = StudioConfig.EVM_RPCS.get(network, "")
    fallbacks = getattr(StudioConfig, "EVM_RPC_FALLBACKS", {}).get(network, ())
    seen: list[str] = []
    for u in (primary, *fallbacks):
        if u and u not in seen:
            seen.append(u)
    return seen


def _evm_rpc_call(network: str, payload: dict[str, Any], timeout_s: float = 7.0) -> dict[str, Any]:
    urls = _evm_rpc_urls(network)
    last_exc: Exception | None = None
    last_resp: dict[str, Any] = {}
    for url in urls:
        try:
            resp = _rpc_post(url, payload, timeout_s=timeout_s)
            if isinstance(resp, dict) and "error" not in resp:
                return resp
            last_resp = resp if isinstance(resp, dict) else {}
        except Exception as exc:
            last_exc = exc
            continue
    if last_resp:
        return last_resp
    if last_exc is not None:
        raise last_exc
    return {}


def decode_evm_stablecoin_transfer(
    receipt: dict[str, Any],
    network: str,
    expected_recipient: str,
    min_amount_usd: float,
) -> VerificationResult:
    """Pure deterministic parser for eth_getTransactionReceipt logs (unit-testable offline)."""
    tx_hash = str(receipt.get("transactionHash") or "")
    status_hex = str(receipt.get("status") or "0x0").lower()
    if status_hex != "0x1":
        return VerificationResult(
            verified=False,
            network=network,
            token="",
            amount_usd=0.0,
            recipient=expected_recipient,
            sender="",
            tx_hash=tx_hash,
            reason="Transaction failed or not confirmed (status != 0x1).",
            raw_proof={"status": status_hex},
        )

    target_addr = expected_recipient.lower().removeprefix("0x").rjust(64, "0")
    contracts = StudioConfig.EVM_STABLECOINS.get(network, {})
    contract_lookup = {addr.lower(): (sym, dec) for sym, (addr, dec) in contracts.items()}

    for log in receipt.get("logs") or []:
        log_addr = str(log.get("address") or "").lower()
        topics = [str(t).lower() for t in (log.get("topics") or [])]
        if log_addr not in contract_lookup or len(topics) < 3:
            continue
        if topics[0] != ERC20_TRANSFER_TOPIC:
            continue
        to_topic = topics[2].removeprefix("0x").rjust(64, "0")
        if to_topic != target_addr:
            continue
        raw_data = str(log.get("data") or "0x0")
        raw_int = int(raw_data, 16)
        sym, decimals = contract_lookup[log_addr]
        amount_usd = raw_int / (10**decimals)
        sender = "0x" + topics[1].removeprefix("0x")[-40:]
        if amount_usd + 1e-6 >= min_amount_usd:
            return VerificationResult(
                verified=True,
                network=network,
                token=sym,
                amount_usd=round(amount_usd, 6),
                recipient=expected_recipient,
                sender=sender,
                tx_hash=tx_hash,
                reason=f"Verified on-chain {amount_usd:.2f} {sym} transfer on {network}.",
                raw_proof={
                    "blockNumber": receipt.get("blockNumber"),
                    "contract": log_addr,
                    "token": sym,
                    "amount_usd": amount_usd,
                },
            )
        return VerificationResult(
            verified=False,
            network=network,
            token=sym,
            amount_usd=round(amount_usd, 6),
            recipient=expected_recipient,
            sender=sender,
            tx_hash=tx_hash,
            reason=f"Transfer amount ${amount_usd:.2f} {sym} is below required ${min_amount_usd:.2f}.",
            raw_proof={"amount_usd": amount_usd, "required_usd": min_amount_usd},
        )

    return VerificationResult(
        verified=False,
        network=network,
        token="",
        amount_usd=0.0,
        recipient=expected_recipient,
        sender="",
        tx_hash=tx_hash,
        reason=f"No matching USDC/USDT Transfer event to {expected_recipient} found in receipt.",
        raw_proof={"logs_checked": len(receipt.get("logs") or [])},
    )


def decode_solana_stablecoin_transfer(
    tx_result: dict[str, Any],
    expected_recipient: str,
    min_amount_usd: float,
    tx_sig: str,
) -> VerificationResult:
    """Pure deterministic parser for Solana getTransaction SPL token balance deltas."""
    meta = tx_result.get("meta") or {}
    if meta.get("err") is not None:
        return VerificationResult(
            verified=False,
            network="solana",
            token="",
            amount_usd=0.0,
            recipient=expected_recipient,
            sender="",
            tx_hash=tx_sig,
            reason=f"Solana transaction failed: {meta.get('err')}",
            raw_proof={"err": meta.get("err")},
        )

    mint_lookup = {mint: sym for sym, (mint, _) in StudioConfig.SOLANA_STABLECOINS.items()}
    pre_map: dict[tuple[str, str], float] = {}
    for entry in meta.get("preTokenBalances") or []:
        owner = str(entry.get("owner") or "")
        mint = str(entry.get("mint") or "")
        amt = float((entry.get("uiTokenAmount") or {}).get("uiAmount") or 0.0)
        if owner == expected_recipient and mint in mint_lookup:
            pre_map[(owner, mint)] = amt

    for entry in meta.get("postTokenBalances") or []:
        owner = str(entry.get("owner") or "")
        mint = str(entry.get("mint") or "")
        if owner != expected_recipient or mint not in mint_lookup:
            continue
        post_amt = float((entry.get("uiTokenAmount") or {}).get("uiAmount") or 0.0)
        delta = post_amt - pre_map.get((owner, mint), 0.0)
        sym = mint_lookup[mint]
        if delta + 1e-6 >= min_amount_usd:
            return VerificationResult(
                verified=True,
                network="solana",
                token=sym,
                amount_usd=round(delta, 6),
                recipient=expected_recipient,
                sender="solana_payer",
                tx_hash=tx_sig,
                reason=f"Verified on-chain {delta:.2f} {sym} SPL transfer on Solana.",
                raw_proof={"slot": tx_result.get("slot"), "mint": mint, "delta": delta},
            )
        if delta > 0:
            return VerificationResult(
                verified=False,
                network="solana",
                token=sym,
                amount_usd=round(delta, 6),
                recipient=expected_recipient,
                sender="solana_payer",
                tx_hash=tx_sig,
                reason=f"Transfer amount ${delta:.2f} {sym} is below required ${min_amount_usd:.2f}.",
                raw_proof={"delta": delta, "required_usd": min_amount_usd},
            )

    return VerificationResult(
        verified=False,
        network="solana",
        token="",
        amount_usd=0.0,
        recipient=expected_recipient,
        sender="",
        tx_hash=tx_sig,
        reason=f"No matching USDC/USDT SPL credit to {expected_recipient} found in transaction.",
        raw_proof={},
    )


def decode_evm_native_transfer(
    tx_obj: dict[str, Any],
    receipt: dict[str, Any],
    network: str,
    expected_recipient: str,
    min_amount_usd: float,
    spot_price_usd: float,
) -> VerificationResult:
    """Deterministic decoder for native ETH/BNB/POL transfers using confirmed receipt + tx value."""
    tx_hash = str(receipt.get("transactionHash") or tx_obj.get("hash") or "")
    sym = (NATIVE_ASSET_SPECS.get(network) or ("NATIVE", ""))[0]
    if str(receipt.get("status") or "0x0").lower() != "0x1":
        return VerificationResult(
            verified=False,
            network=network,
            token=sym,
            amount_usd=0.0,
            recipient=expected_recipient,
            sender="",
            tx_hash=tx_hash,
            reason="Native transaction failed or not confirmed (status != 0x1).",
            raw_proof={},
        )
    tx_to = str(tx_obj.get("to") or "").strip().lower()
    if tx_to != expected_recipient.strip().lower():
        return VerificationResult(
            verified=False,
            network=network,
            token="",
            amount_usd=0.0,
            recipient=expected_recipient,
            sender=str(tx_obj.get("from") or ""),
            tx_hash=tx_hash,
            reason=f"Native tx recipient {tx_to} does not match {expected_recipient}.",
            raw_proof={},
        )
    wei = int(str(tx_obj.get("value") or "0x0"), 16)
    native_amt = wei / 1e18
    sender = str(tx_obj.get("from") or "")
    if native_amt <= 0 or spot_price_usd <= 0:
        return VerificationResult(
            verified=False,
            network=network,
            token=sym,
            amount_usd=0.0,
            recipient=expected_recipient,
            sender=sender,
            tx_hash=tx_hash,
            reason=f"Zero {sym} value or unavailable live USD quote.",
            raw_proof={},
        )
    amount_usd = native_amt * float(spot_price_usd)
    # 2% tolerance protects genuine buyers from brief spot jitter while clicking MetaMask
    if amount_usd + 1e-6 >= float(min_amount_usd) * 0.98:
        return VerificationResult(
            verified=True,
            network=network,
            token=sym,
            amount_usd=round(max(amount_usd, float(min_amount_usd)) if amount_usd >= float(min_amount_usd) * 0.98 else amount_usd, 4),
            recipient=expected_recipient,
            sender=sender,
            tx_hash=tx_hash,
            reason=f"Verified on-chain native {native_amt:.6f} {sym} (~${amount_usd:.2f}) transfer on {network}.",
            raw_proof={
                "blockNumber": receipt.get("blockNumber"),
                "token": sym,
                "native_amount": native_amt,
                "spot_price_usd": spot_price_usd,
                "amount_usd": amount_usd,
            },
        )
    return VerificationResult(
        verified=False,
        network=network,
        token=sym,
        amount_usd=round(amount_usd, 4),
        recipient=expected_recipient,
        sender=sender,
        tx_hash=tx_hash,
        reason=f"Native transfer value ~${amount_usd:.2f} {sym} is below required ${min_amount_usd:.2f}.",
        raw_proof={"native_amount": native_amt, "amount_usd": amount_usd, "required_usd": min_amount_usd},
    )


def decode_solana_native_transfer(
    tx_result: dict[str, Any],
    expected_recipient: str,
    min_amount_usd: float,
    tx_sig: str,
    spot_price_usd: float,
) -> VerificationResult:
    """Deterministic decoder for native SOL balance delta in Solana getTransaction."""
    meta = tx_result.get("meta") or {}
    if meta.get("err") is not None:
        return VerificationResult(
            verified=False,
            network="solana",
            token="SOL",
            amount_usd=0.0,
            recipient=expected_recipient,
            sender="",
            tx_hash=tx_sig,
            reason=f"Solana transaction failed: {meta.get('err')}",
            raw_proof={"err": meta.get("err")},
        )
    acct_keys = ((tx_result.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    pre_bals = meta.get("preBalances") or []
    post_bals = meta.get("postBalances") or []
    target_idx = -1
    for i, item in enumerate(acct_keys):
        pk = str(item.get("pubkey") if isinstance(item, dict) else item).strip()
        if pk == expected_recipient.strip():
            target_idx = i
            break
    if target_idx < 0 or target_idx >= len(pre_bals) or target_idx >= len(post_bals):
        return VerificationResult(
            verified=False,
            network="solana",
            token="",
            amount_usd=0.0,
            recipient=expected_recipient,
            sender="",
            tx_hash=tx_sig,
            reason=f"No native SOL credit to {expected_recipient} found in transaction.",
            raw_proof={},
        )
    delta_sol = (int(post_bals[target_idx]) - int(pre_bals[target_idx])) / 1e9
    if delta_sol <= 0 or spot_price_usd <= 0:
        return VerificationResult(
            verified=False,
            network="solana",
            token="SOL",
            amount_usd=0.0,
            recipient=expected_recipient,
            sender="",
            tx_hash=tx_sig,
            reason="Zero SOL delta or unavailable live SOL price.",
            raw_proof={},
        )
    amount_usd = delta_sol * float(spot_price_usd)
    if amount_usd + 1e-6 >= float(min_amount_usd) * 0.98:
        return VerificationResult(
            verified=True,
            network="solana",
            token="SOL",
            amount_usd=round(amount_usd, 4),
            recipient=expected_recipient,
            sender="solana_payer",
            tx_hash=tx_sig,
            reason=f"Verified on-chain {delta_sol:.6f} SOL (~${amount_usd:.2f}) transfer on Solana.",
            raw_proof={"slot": tx_result.get("slot"), "native_amount": delta_sol, "spot_price_usd": spot_price_usd},
        )
    return VerificationResult(
        verified=False,
        network="solana",
        token="SOL",
        amount_usd=round(amount_usd, 4),
        recipient=expected_recipient,
        sender="solana_payer",
        tx_hash=tx_sig,
        reason=f"Native SOL amount ~${amount_usd:.2f} is below required ${min_amount_usd:.2f}.",
        raw_proof={"delta_sol": delta_sol, "required_usd": min_amount_usd},
    )


def decode_bitcoin_transfer(
    tx_obj: dict[str, Any],
    expected_recipient: str,
    min_amount_usd: float,
    tx_hash: str,
    spot_price_usd: float,
) -> VerificationResult:
    """Deterministic decoder for confirmed Mempool.space Bitcoin transactions paying expected_recipient."""
    target = expected_recipient.strip()
    total_sats = sum(
        int(v.get("value") or 0)
        for v in (tx_obj.get("vout") or [])
        if str(v.get("scriptpubkey_address") or "").strip() == target
    )
    if total_sats <= 0:
        return VerificationResult(
            verified=False,
            network="bitcoin",
            token="BTC",
            amount_usd=0.0,
            recipient=target,
            sender="",
            tx_hash=tx_hash,
            reason=f"No output paying Bitcoin address {target} found in tx.",
            raw_proof={},
        )
    confirmed = bool((tx_obj.get("status") or {}).get("confirmed"))
    if not confirmed:
        return VerificationResult(
            verified=False,
            network="bitcoin",
            token="BTC",
            amount_usd=0.0,
            recipient=target,
            sender="",
            tx_hash=tx_hash,
            reason="Bitcoin transaction detected in mempool; awaiting 1st block confirmation.",
            raw_proof={"confirmed": False, "sats": total_sats},
        )
    if spot_price_usd <= 0:
        return VerificationResult(
            verified=False,
            network="bitcoin",
            token="BTC",
            amount_usd=0.0,
            recipient=target,
            sender="",
            tx_hash=tx_hash,
            reason="Unable to fetch live BTC/USD price.",
            raw_proof={},
        )
    btc_amt = total_sats / 1e8
    amount_usd = btc_amt * float(spot_price_usd)
    if amount_usd + 1e-6 >= float(min_amount_usd) * 0.98:
        return VerificationResult(
            verified=True,
            network="bitcoin",
            token="BTC",
            amount_usd=round(amount_usd, 4),
            recipient=target,
            sender="bitcoin_payer",
            tx_hash=tx_hash,
            reason=f"Verified confirmed Bitcoin transfer of {btc_amt:.8f} BTC (~${amount_usd:.2f}).",
            raw_proof={"block_height": (tx_obj.get("status") or {}).get("block_height"), "sats": total_sats, "spot_price_usd": spot_price_usd},
        )
    return VerificationResult(
        verified=False,
        network="bitcoin",
        token="BTC",
        amount_usd=round(amount_usd, 4),
        recipient=target,
        sender="bitcoin_payer",
        tx_hash=tx_hash,
        reason=f"Bitcoin amount ~${amount_usd:.2f} BTC is below required ${min_amount_usd:.2f}.",
        raw_proof={"sats": total_sats, "amount_usd": amount_usd, "required_usd": min_amount_usd},
    )


class OnChainPaymentVerifier:
    """Queries live public RPCs to verify real stablecoin transfers to Phantom or creator wallets."""

    def verify(
        self,
        network: str,
        tx_hash: str,
        min_amount_usd: float,
        expected_recipient: str = "",
    ) -> VerificationResult:
        net = network.strip().lower()
        tx = tx_hash.strip()
        if not tx:
            return VerificationResult(
                verified=False,
                network=net,
                token="",
                amount_usd=0.0,
                recipient="",
                sender="",
                tx_hash=tx,
                reason="Empty transaction hash.",
                raw_proof={},
            )

        if net in StudioConfig.EVM_RPCS:
            target = (expected_recipient or StudioConfig.PHANTOM_EVM_ADDRESS).strip()
            return self._verify_evm(net, tx, min_amount_usd, target)
        if net == "solana":
            target = (expected_recipient or StudioConfig.PHANTOM_SOL_ADDRESS).strip()
            return self._verify_solana(tx, min_amount_usd, target)
        if net == "bitcoin":
            target = (expected_recipient or StudioConfig.PHANTOM_BTC_ADDRESS).strip()
            return self._verify_bitcoin(tx, min_amount_usd, target)
        return VerificationResult(
            verified=False,
            network=net,
            token="",
            amount_usd=0.0,
            recipient="",
            sender="",
            tx_hash=tx,
            reason=f"Unsupported network '{net}'. Use base, arbitrum, polygon, bsc, ethereum, solana, or bitcoin.",
            raw_proof={},
        )

    def _verify_evm(
        self,
        network: str,
        tx_hash: str,
        min_amount_usd: float,
        expected_recipient: str = "",
    ) -> VerificationResult:
        target = (expected_recipient or StudioConfig.PHANTOM_EVM_ADDRESS).strip()
        rpc_url = StudioConfig.EVM_RPCS[network]
        try:
            resp = _evm_rpc_call(
                network,
                {"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt", "params": [tx_hash]},
            )
            receipt = resp.get("result")
            if not receipt:
                return VerificationResult(
                    verified=False,
                    network=network,
                    token="",
                    amount_usd=0.0,
                    recipient=target,
                    sender="",
                    tx_hash=tx_hash,
                    reason="Transaction receipt not found yet on RPC (wait a few seconds for confirmation).",
                    raw_proof={"rpc": rpc_url},
                )
            res = decode_evm_stablecoin_transfer(
                receipt=receipt,
                network=network,
                expected_recipient=target,
                min_amount_usd=min_amount_usd,
            )
            if res.verified or "below required" in res.reason:
                return res
            if str(receipt.get("status") or "0x0").lower() == "0x1":
                try:
                    tx_resp = _evm_rpc_call(
                        network,
                        {"jsonrpc": "2.0", "id": 2, "method": "eth_getTransactionByHash", "params": [tx_hash]},
                    )
                    tx_obj = (tx_resp or {}).get("result")
                    if (
                        isinstance(tx_obj, dict)
                        and str(tx_obj.get("to") or "").strip().lower() == target.lower()
                        and int(str(tx_obj.get("value") or "0x0"), 16) > 0
                    ):
                        spot = fetch_native_usd_price(network)
                        return decode_evm_native_transfer(
                            tx_obj=tx_obj,
                            receipt=receipt,
                            network=network,
                            expected_recipient=target,
                            min_amount_usd=min_amount_usd,
                            spot_price_usd=spot,
                        )
                except Exception:
                    pass
            return res
        except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError) as exc:
            return VerificationResult(
                verified=False,
                network=network,
                token="",
                amount_usd=0.0,
                recipient=target,
                sender="",
                tx_hash=tx_hash,
                reason=f"RPC verification error on {network}: {exc}",
                raw_proof={},
            )

    def _verify_solana(
        self,
        tx_sig: str,
        min_amount_usd: float,
        expected_recipient: str = "",
    ) -> VerificationResult:
        target = (expected_recipient or StudioConfig.PHANTOM_SOL_ADDRESS).strip()
        try:
            resp = _rpc_post(
                StudioConfig.SOLANA_RPC_URL,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTransaction",
                    "params": [tx_sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
                },
            )
            tx_result = resp.get("result")
            if not tx_result:
                return VerificationResult(
                    verified=False,
                    network="solana",
                    token="",
                    amount_usd=0.0,
                    recipient=target,
                    sender="",
                    tx_hash=tx_sig,
                    reason="Solana transaction not found yet on RPC.",
                    raw_proof={},
                )
            res = decode_solana_stablecoin_transfer(
                tx_result=tx_result,
                expected_recipient=target,
                min_amount_usd=min_amount_usd,
                tx_sig=tx_sig,
            )
            if res.verified or "below required" in res.reason:
                return res
            spot_sol = fetch_native_usd_price("solana")
            native_res = decode_solana_native_transfer(
                tx_result=tx_result,
                expected_recipient=target,
                min_amount_usd=min_amount_usd,
                tx_sig=tx_sig,
                spot_price_usd=spot_sol,
            )
            if native_res.verified or "below required" in native_res.reason:
                return native_res
            return res
        except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError) as exc:
            return VerificationResult(
                verified=False,
                network="solana",
                token="",
                amount_usd=0.0,
                recipient=target,
                sender="",
                tx_hash=tx_sig,
                reason=f"Solana RPC error: {exc}",
                raw_proof={},
            )

    def _verify_bitcoin(
        self,
        tx_hash: str,
        min_amount_usd: float,
        expected_recipient: str = "",
    ) -> VerificationResult:
        target = (expected_recipient or StudioConfig.PHANTOM_BTC_ADDRESS).strip()
        try:
            tx_obj = _http_get_json(f"https://mempool.space/api/tx/{tx_hash}", timeout_s=8.0)
            if not isinstance(tx_obj, dict):
                return VerificationResult(
                    verified=False,
                    network="bitcoin",
                    token="BTC",
                    amount_usd=0.0,
                    recipient=target,
                    sender="",
                    tx_hash=tx_hash,
                    reason="Bitcoin transaction not found on mempool.space yet.",
                    raw_proof={},
                )
            spot_btc = fetch_native_usd_price("bitcoin")
            return decode_bitcoin_transfer(
                tx_obj=tx_obj,
                expected_recipient=target,
                min_amount_usd=min_amount_usd,
                tx_hash=tx_hash,
                spot_price_usd=spot_btc,
            )
        except Exception as exc:
            return VerificationResult(
                verified=False,
                network="bitcoin",
                token="BTC",
                amount_usd=0.0,
                recipient=target,
                sender="",
                tx_hash=tx_hash,
                reason=f"Bitcoin verification error: {exc}",
                raw_proof={},
            )

    def find_recent_btc_transfers(
        self,
        expected_recipient: str,
        min_amount_usd: float,
    ) -> list[VerificationResult]:
        target = expected_recipient.strip()
        if not target:
            return []
        try:
            txs = _http_get_json(f"https://mempool.space/api/address/{target}/txs", timeout_s=6.0)
            spot_btc = fetch_native_usd_price("bitcoin")
            if not isinstance(txs, list) or spot_btc <= 0:
                return []
            out: list[VerificationResult] = []
            for tx_obj in txs[:10]:
                txid = str(tx_obj.get("txid") or "").strip()
                if not txid:
                    continue
                res = decode_bitcoin_transfer(
                    tx_obj=tx_obj,
                    expected_recipient=target,
                    min_amount_usd=min_amount_usd,
                    tx_hash=txid,
                    spot_price_usd=spot_btc,
                )
                if res.verified:
                    out.append(res)
            return out
        except Exception:
            return []

    def find_recent_solana_transfers(
        self,
        expected_recipient: str,
        min_amount_usd: float,
    ) -> list[VerificationResult]:
        """Scan recent Solana signatures on expected_recipient (and its USDC/USDT token accounts) for zero-tx-hash settlement."""
        target = expected_recipient.strip()
        if not target:
            return []
        try:
            sigs: list[str] = []
            r_main = _rpc_post(
                StudioConfig.SOLANA_RPC_URL,
                {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress", "params": [target, {"limit": 6}]},
                timeout_s=6.0,
            )
            for item in (r_main.get("result") or []):
                sig = str((item or {}).get("signature") or "").strip()
                if sig and not (item or {}).get("err") and sig not in sigs:
                    sigs.append(sig)
            for _, (mint_addr, _) in StudioConfig.SOLANA_STABLECOINS.items():
                ta_resp = _rpc_post(
                    StudioConfig.SOLANA_RPC_URL,
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "getTokenAccountsByOwner",
                        "params": [target, {"mint": mint_addr}, {"encoding": "jsonParsed"}],
                    },
                    timeout_s=5.0,
                )
                for acc in ((ta_resp.get("result") or {}).get("value") or [])[:2]:
                    ata_pk = str((acc or {}).get("pubkey") or "").strip()
                    if not ata_pk:
                        continue
                    r_ata = _rpc_post(
                        StudioConfig.SOLANA_RPC_URL,
                        {"jsonrpc": "2.0", "id": 3, "method": "getSignaturesForAddress", "params": [ata_pk, {"limit": 5}]},
                        timeout_s=5.0,
                    )
                    for item in (r_ata.get("result") or []):
                        sig = str((item or {}).get("signature") or "").strip()
                        if sig and not (item or {}).get("err") and sig not in sigs:
                            sigs.append(sig)
            out: list[VerificationResult] = []
            for sig in sigs[:8]:
                vr = self._verify_solana(sig, min_amount_usd, target)
                if vr.verified:
                    out.append(vr)
            return out
        except Exception:
            return []

    def find_recent_evm_transfers(
        self,
        network: str,
        expected_recipient: str,
        min_amount_usd: float,
        lookback_blocks: int = 900,
    ) -> list[VerificationResult]:
        """Scan recent ERC-20 USDT/USDC Transfer logs to expected_recipient so buyers don't need to copy-paste tx_hash."""
        net = network.strip().lower()
        if net not in StudioConfig.EVM_RPCS:
            return []
        contracts = StudioConfig.EVM_STABLECOINS.get(net, {})
        if not contracts:
            return []
        contract_lookup = {addr.lower(): (sym, dec) for sym, (addr, dec) in contracts.items()}
        target_topic = "0x" + expected_recipient.strip().lower().removeprefix("0x").rjust(64, "0")
        try:
            blk_resp = _evm_rpc_call(net, {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []})
            head_block = int(str(blk_resp.get("result") or "0x0"), 16)
            if head_block <= 0:
                return []
            from_block = hex(max(0, head_block - max(10, int(lookback_blocks))))
            logs_resp = _evm_rpc_call(
                net,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "eth_getLogs",
                    "params": [
                        {
                            "fromBlock": from_block,
                            "toBlock": "latest",
                            "address": [addr for addr, _ in contracts.values()],
                            "topics": [ERC20_TRANSFER_TOPIC, None, target_topic],
                        }
                    ],
                },
            )
            logs = logs_resp.get("result") or []
            if not isinstance(logs, list):
                return []
            matches: list[VerificationResult] = []
            for log in reversed(logs):
                log_addr = str(log.get("address") or "").lower()
                tx_hash = str(log.get("transactionHash") or "").strip()
                topics = [str(t).lower() for t in (log.get("topics") or [])]
                if not tx_hash or log_addr not in contract_lookup or len(topics) < 3:
                    continue
                sym, decimals = contract_lookup[log_addr]
                raw_int = int(str(log.get("data") or "0x0"), 16)
                amount_usd = raw_int / (10**decimals)
                if amount_usd + 1e-6 < float(min_amount_usd):
                    continue
                sender = "0x" + topics[1].removeprefix("0x")[-40:]
                matches.append(
                    VerificationResult(
                        verified=True,
                        network=net,
                        token=sym,
                        amount_usd=round(amount_usd, 6),
                        recipient=expected_recipient,
                        sender=sender,
                        tx_hash=tx_hash,
                        reason=f"Auto-detected on-chain {amount_usd:.2f} {sym} transfer on {net}.",
                        raw_proof={
                            "blockNumber": log.get("blockNumber"),
                            "contract": log_addr,
                            "token": sym,
                            "amount_usd": amount_usd,
                            "auto_detected": True,
                        },
                    )
                )
            return matches
        except Exception:
            return []

