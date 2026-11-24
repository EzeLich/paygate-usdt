"""
PayGate USDT Python SDK v2.0
Non-custodial multi-chain USDT/USDC invoices, HMAC webhook verification,
and FastAPI HTTP 402 (x402 v2) paywall protection.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
import urllib.error
import urllib.request
from functools import wraps
from typing import Any, Callable

from fastapi import Request
from fastapi.responses import JSONResponse

from app.config import StudioConfig
from app.onchain_verifier import OnChainPaymentVerifier

_CAIP2_BY_NET: dict[str, tuple[str, str]] = {
    "base": ("eip155:8453", StudioConfig.EVM_STABLECOINS["base"]["USDC"][0]),
    "polygon": ("eip155:137", StudioConfig.EVM_STABLECOINS["polygon"]["USDC"][0]),
    "arbitrum": ("eip155:42161", StudioConfig.EVM_STABLECOINS["arbitrum"]["USDC"][0]),
    "ethereum": ("eip155:1", StudioConfig.EVM_STABLECOINS["ethereum"]["USDC"][0]),
    "bsc": ("eip155:56", StudioConfig.EVM_STABLECOINS["bsc"]["USDT"][0]),
    "solana": ("solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", StudioConfig.SOLANA_STABLECOINS["USDC"][0]),
}


def verify_webhook_signature(payload_bytes: bytes, signature_header: str, secret: str) -> bool:
    """Verify `X-PayGate-Signature: sha256=<hex>` using constant-time HMAC-SHA256."""
    if not secret or not signature_header:
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.strip())


def build_x402_challenge(
    *,
    resource_url: str,
    description: str,
    price_usd: float,
    evm_recipient: str = "",
    solana_recipient: str = "",
    networks: tuple[str, ...] = ("base", "polygon", "arbitrum", "bsc", "solana"),
) -> dict[str, Any]:
    """Build an x402 v2 compliant HTTP 402 Payment Required JSON payload."""
    atomic_6 = str(int(round(float(price_usd) * 1_000_000)))
    evm_to = (evm_recipient or StudioConfig.PHANTOM_EVM_ADDRESS).strip()
    sol_to = (solana_recipient or StudioConfig.PHANTOM_SOL_ADDRESS).strip()
    accepts: list[dict[str, Any]] = []
    for net in networks:
        n = net.lower().strip()
        if n not in _CAIP2_BY_NET:
            continue
        caip_net, asset = _CAIP2_BY_NET[n]
        pay_to = sol_to if n == "solana" else evm_to
        if not pay_to:
            continue
        accepts.append(
            {
                "scheme": "exact",
                "network": caip_net,
                "amount": atomic_6,
                "maxAmountRequired": atomic_6,
                "resource": resource_url,
                "description": description,
                "mimeType": "application/json",
                "payTo": pay_to,
                "maxTimeoutSeconds": 300,
                "asset": asset,
            }
        )
    return {
        "x402Version": 2,
        "error": "Payment Required",
        "protocol": "x402-direct-onchain",
        "amount_usd": float(price_usd),
        "accepts": accepts,
        "retry_headers": {
            "X-Payment-Network": "base | polygon | arbitrum | bsc | ethereum | solana",
            "X-Payment-Tx": "<confirmed_onchain_tx_hash>",
        },
    }


def require_usdt_payment(
    *,
    price_usd: float,
    recipient_wallet: str,
    description: str = "Paid API endpoint (USDT/USDC direct settlement)",
    solana_wallet: str = "",
    networks: tuple[str, ...] = ("base", "polygon", "arbitrum", "bsc"),
    used_txs: set[str] | None = None,
    verifier: OnChainPaymentVerifier | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    FastAPI route decorator enforcing non-custodial HTTP 402 USDT/USDC payment.
    Requires `request: Request` parameter in the decorated endpoint.
    """
    ver = verifier or OnChainPaymentVerifier()
    replay_guard = used_txs if used_txs is not None else set()

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        def _authorize_or_402(args: tuple[Any, ...], kwargs: dict[str, Any]) -> JSONResponse | None:
            request: Request | None = kwargs.get("request")
            if request is None:
                for a in args:
                    if isinstance(a, Request):
                        request = a
                        break
            if request is None:
                raise RuntimeError("@require_usdt_payment endpoint must accept a FastAPI Request parameter.")

            tx_hash = (request.headers.get("X-Payment-Tx") or "").strip()
            net = (request.headers.get("X-Payment-Network") or "").strip().lower()
            if tx_hash and net:
                key = tx_hash.lower()
                if key in replay_guard:
                    return JSONResponse(status_code=402, content={"error": "Transaction already redeemed (anti-replay)."})
                target = solana_wallet if (net == "solana" and solana_wallet) else recipient_wallet
                res = ver.verify(
                    network=net,
                    tx_hash=tx_hash,
                    min_amount_usd=float(price_usd),
                    expected_recipient=target,
                )
                if not res.verified:
                    return JSONResponse(status_code=402, content={"error": res.reason})
                replay_guard.add(key)
                return None

            challenge = build_x402_challenge(
                resource_url=str(request.url),
                description=description,
                price_usd=float(price_usd),
                evm_recipient=recipient_wallet,
                solana_recipient=solana_wallet,
                networks=networks,
            )
            b64 = base64.b64encode(json.dumps(challenge).encode("utf-8")).decode("ascii")
            return JSONResponse(
                status_code=402,
                headers={"PAYMENT-REQUIRED": b64, "X-Payment-Protocol": "x402"},
                content=challenge,
            )

        if inspect.iscoroutinefunction(func):
            @wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                denied = _authorize_or_402(args, kwargs)
                if denied is not None:
                    return denied
                return await func(*args, **kwargs)

            return async_wrapper

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            denied = _authorize_or_402(args, kwargs)
            if denied is not None:
                return denied
            return func(*args, **kwargs)

        return sync_wrapper

    return decorator


class PayGateClient:
    """Lightweight stdlib client for PayGate USDT hosted invoice & B2B API."""

    def __init__(
        self,
        base_url: str = "https://chainguard-studio-production.up.railway.app",
        timeout_s: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = float(timeout_s)

    def _request(self, path: str, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"User-Agent": "paygate-usdt-sdk/2.0"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                return json.loads(body)
            except Exception:
                return {"ok": False, "error": body, "status_code": exc.code}

    def create_invoice(
        self,
        *,
        title: str,
        amount_usd: float,
        network: str,
        merchant_wallet: str,
        webhook_url: str = "",
        webhook_secret: str = "",
        redirect_url: str = "",
        ttl_minutes: int = 60,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "/api/v1/paygate/invoices",
            method="POST",
            payload={
                "title": title,
                "amount_usd": float(amount_usd),
                "network": network,
                "merchant_wallet": merchant_wallet,
                "webhook_url": webhook_url,
                "webhook_secret": webhook_secret,
                "redirect_url": redirect_url,
                "ttl_minutes": int(ttl_minutes),
                "metadata": metadata or {},
            },
        )

    def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        return self._request(f"/api/v1/paygate/invoices/{invoice_id}", method="GET")

    def verify_invoice(self, invoice_id: str, tx_hash: str) -> dict[str, Any]:
        return self._request(
            f"/api/v1/paygate/invoices/{invoice_id}/verify",
            method="POST",
            payload={"tx_hash": tx_hash},
        )


__all__ = [
    "PayGateClient",
    "build_x402_challenge",
    "require_usdt_payment",
    "verify_webhook_signature",
]
