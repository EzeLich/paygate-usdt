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
        customer_ref: str = "",
        billing_days: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": title,
            "amount_usd": float(amount_usd),
            "network": network,
            "merchant_wallet": merchant_wallet,
            "webhook_url": webhook_url,
            "webhook_secret": webhook_secret,
            "redirect_url": redirect_url,
            "ttl_minutes": int(ttl_minutes),
            "metadata": metadata or {},
        }
        if customer_ref.strip():
            payload["customer_ref"] = customer_ref.strip()
        if int(billing_days) > 0:
            payload["billing_days"] = int(billing_days)
        return self._request("/api/v1/paygate/invoices", method="POST", payload=payload)

    def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        return self._request(f"/api/v1/paygate/invoices/{invoice_id}", method="GET")

    def list_invoices(
        self,
        *,
        merchant_wallet: str = "",
        status: str = "",
        customer_ref: str = "",
        limit: int = 25,
    ) -> dict[str, Any]:
        import urllib.parse

        qs = urllib.parse.urlencode(
            {
                "merchant_wallet": merchant_wallet.strip(),
                "status": status.strip().lower(),
                "customer_ref": customer_ref.strip(),
                "limit": int(limit),
            }
        )
        return self._request(f"/api/v1/paygate/invoices?{qs}", method="GET")

    def expire_invoice(self, invoice_id: str, merchant_wallet: str = "") -> dict[str, Any]:
        import urllib.parse

        qs = f"?{urllib.parse.urlencode({'merchant_wallet': merchant_wallet.strip()})}" if merchant_wallet.strip() else ""
        return self._request(f"/api/v1/paygate/invoices/{invoice_id}/expire{qs}", method="POST", payload={})

    def verify_invoice(self, invoice_id: str, tx_hash: str) -> dict[str, Any]:
        return self._request(
            f"/api/v1/paygate/invoices/{invoice_id}/verify",
            method="POST",
            payload={"tx_hash": tx_hash},
        )

    def auto_detect_invoice(self, invoice_id: str) -> dict[str, Any]:
        return self._request(
            f"/api/v1/paygate/invoices/{invoice_id}/auto-detect",
            method="POST",
            payload={},
        )

    def get_receipt(self, invoice_id: str) -> dict[str, Any]:
        return self._request(f"/api/v1/paygate/invoices/{invoice_id}/receipt", method="GET")

    def check_subscription(
        self,
        *,
        merchant_wallet: str,
        customer_ref: str,
        amount_usd: float = 9.0,
        network: str = "polygon",
        title: str = "Monthly Subscription",
        billing_days: int = 30,
    ) -> dict[str, Any]:
        import urllib.parse

        qs = urllib.parse.urlencode(
            {
                "merchant": merchant_wallet.strip(),
                "customer_ref": customer_ref.strip(),
                "amount": round(float(amount_usd), 2),
                "network": network.strip().lower(),
                "title": title.strip(),
                "billing_days": int(billing_days),
            }
        )
        return self._request(f"/api/v1/paygate/subscriptions/check?{qs}", method="GET")

    def build_payment_url(
        self,
        *,
        merchant_wallet: str,
        amount_usd: float,
        network: str = "polygon",
        title: str = "Instant Checkout",
        redirect_url: str = "",
        issue_pass: bool = False,
        customer_ref: str = "",
        billing_days: int = 0,
    ) -> str:
        import urllib.parse

        params: dict[str, Any] = {
            "merchant": merchant_wallet.strip(),
            "amount": round(float(amount_usd), 2),
            "network": network.strip().lower(),
            "title": title.strip(),
        }
        if redirect_url.strip():
            params["redirect_url"] = redirect_url.strip()
        if issue_pass:
            params["issue_pass"] = 1
        if customer_ref.strip():
            params["customer_ref"] = customer_ref.strip()
        if int(billing_days) > 0:
            params["billing_days"] = int(billing_days)
        return f"{self.base_url}/paygate/pay?{urllib.parse.urlencode(params)}"


from .cli import handle_mcp_jsonrpc


def mcp_tool_paywall(
    *,
    price_usd: float,
    merchant_wallet: str,
    network: str = "base",
    title: str = "Premium MCP Tool Access",
    free_calls: int = 3,
    billing_days: int = 30,
    base_url: str = "https://chainguard-studio-production.up.railway.app",
    client: PayGateClient | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    1-line non-custodial USDT/USDC paywall & recurring subscription decorator for any Python MCP tool
    (compatible with FastMCP `@mcp.tool()`, Anthropic `mcp` SDK, and plain Python tool functions).

    - Allows `free_calls` invocations per `customer_ref` (default `"anonymous"`), attaching a `paygate_billing`
      telemetry block with remaining free calls and a 1-click multi-chain checkout URL.
    - Once `free_calls` are used, automatically verifies if the caller passed a paid `paygate_invoice_id`
      or has an active recurring subscription (`customer_ref`) with `merchant_wallet`.
    - When unpaid, returns a structured MCP payment prompt with `checkout_url` and x402 challenge.
    """
    pg = client or PayGateClient(base_url=base_url)
    usage_counts: dict[str, int] = {}

    def _check_access(kwargs: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        cust_ref = str(kwargs.get("customer_ref") or "anonymous").strip() or "anonymous"
        inv_id = str(kwargs.get("paygate_invoice_id") or "").strip()

        checkout_url = pg.build_payment_url(
            merchant_wallet=merchant_wallet,
            amount_usd=price_usd,
            network=network,
            title=title,
            customer_ref=cust_ref if cust_ref != "anonymous" else "",
            billing_days=billing_days,
        )

        # 1. Explicit paid invoice ID unlocks immediately
        if inv_id:
            inv_res = pg.get_invoice(inv_id)
            inv_obj = inv_res.get("invoice") if isinstance(inv_res, dict) else None
            if (
                isinstance(inv_obj, dict)
                and inv_obj.get("status") == "paid"
                and str(inv_obj.get("merchant_wallet") or "").lower() == merchant_wallet.strip().lower()
                and float(inv_obj.get("paid_amount_usd") or 0.0) + 1e-6 >= float(price_usd)
            ):
                return True, {
                    "tier": "paid_invoice",
                    "invoice_id": inv_id,
                    "tx_hash": inv_obj.get("tx_hash"),
                    "network": inv_obj.get("network"),
                    "checkout_url": checkout_url,
                }

        # 2. Active recurring subscription for customer_ref unlocks immediately
        if cust_ref != "anonymous" and billing_days > 0:
            sub = pg.check_subscription(
                merchant_wallet=merchant_wallet,
                customer_ref=cust_ref,
                amount_usd=price_usd,
                network=network,
                title=title,
                billing_days=billing_days,
            )
            if isinstance(sub, dict) and sub.get("active"):
                return True, {
                    "tier": "active_subscription",
                    "customer_ref": cust_ref,
                    "days_remaining": sub.get("days_remaining"),
                    "expires_at": sub.get("expires_at"),
                    "renewal_url": sub.get("renewal_url") or checkout_url,
                }

        # 3. Free tier quota check
        used = usage_counts.get(cust_ref, 0)
        if used < max(0, int(free_calls)):
            usage_counts[cust_ref] = used + 1
            remaining = max(0, int(free_calls) - (used + 1))
            return True, {
                "tier": "free_tier",
                "free_calls_remaining": remaining,
                "price_usd": float(price_usd),
                "checkout_url": checkout_url,
            }

        # 4. Quota exhausted -> return non-custodial payment challenge
        ch = build_x402_challenge(
            resource_url=f"{pg.base_url}/paygate",
            description=title,
            price_usd=float(price_usd),
            evm_recipient=merchant_wallet if merchant_wallet.startswith("0x") else "",
            solana_recipient="" if merchant_wallet.startswith("0x") else merchant_wallet,
        )
        return False, {
            "ok": False,
            "error": f"Free MCP tool quota reached ({free_calls} calls). Complete non-custodial USDT/USDC payment to unlock.",
            "price_usd": float(price_usd),
            "merchant_wallet": merchant_wallet,
            "network": network,
            "billing_days": int(billing_days),
            "checkout_url": checkout_url,
            "instructions": (
                f"Open {checkout_url} to pay ${price_usd:.2f} USDT/USDC on Base, Polygon, Arbitrum, BSC, or Solana, "
                "then pass `paygate_invoice_id` or `customer_ref` to unlock."
            ),
            "x402": ch,
        }

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.iscoroutinefunction(func):
            @wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                allowed, billing_info = _check_access(kwargs)
                if not allowed:
                    return billing_info
                res = await func(*args, **kwargs)
                if isinstance(res, dict):
                    return {**res, "paygate_billing": billing_info}
                return res

            return async_wrapper

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            allowed, billing_info = _check_access(kwargs)
            if not allowed:
                return billing_info
            res = func(*args, **kwargs)
            if isinstance(res, dict):
                return {**res, "paygate_billing": billing_info}
            return res

        return sync_wrapper

    return decorator


__all__ = [
    "PayGateClient",
    "build_x402_challenge",
    "handle_mcp_jsonrpc",
    "mcp_tool_paywall",
    "require_usdt_payment",
    "verify_webhook_signature",
]
