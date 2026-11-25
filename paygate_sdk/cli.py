"""
PayGate USDT CLI & Local Stdio MCP Server (`python -m paygate_sdk`).
Supports Claude Desktop / Cursor / Windsurf / OpenCode stdio MCP plus direct invoice & tx verification commands.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from typing import Any

from app.onchain_verifier import OnChainPaymentVerifier
from . import PayGateClient, build_x402_challenge

DEFAULT_BASE_URL = "https://chainguard-studio-production.up.railway.app"


def _mcp_tools_schema() -> list[dict[str, Any]]:
    return [
        {
            "name": "create_usdt_invoice",
            "description": (
                "Create a new non-custodial multi-chain USDT/USDC payment invoice and hosted checkout page on Polygon, BSC, Arbitrum, Base, Ethereum, or Solana settling 100% directly to `merchant_wallet` with 0% platform custody. "
                "State-changing operation creating a single-use `pending` invoice record (unlimited free calls, no authentication required). "
                "Supports recurring SaaS subscriptions via optional `customer_ref` and `billing_days`. "
                "Returns JSON with `ok` (boolean), `checkout_url` (hosted payment page), and `invoice` (`invoice_id`, `status`, `amount_usd`, `network`, `merchant_wallet`, `expires_at`)."
            ),
            "annotations": {
                "title": "Create Non-Custodial USDT/USDC Checkout Invoice",
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": True,
            },
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Human-readable product, order, or subscription plan title displayed on the hosted checkout page (2-140 chars).",
                    },
                    "amount_usd": {
                        "type": "number",
                        "description": "Exact invoice price denominated in USD (settled 1:1 in USDT or USDC on-chain, e.g. `19.0`). Must be >= `0.01`.",
                    },
                    "network": {
                        "type": "string",
                        "default": "polygon",
                        "enum": ["polygon", "bsc", "base", "arbitrum", "ethereum", "solana"],
                        "description": "Settlement blockchain network (`polygon`, `bsc`, `base`, `arbitrum`, `ethereum`, or `solana`). Defaults to `polygon`.",
                    },
                    "merchant_wallet": {
                        "type": "string",
                        "description": "Merchant's self-custody receiving wallet address (`0x...` for EVM chains or base58 public key for `solana`). Funds transfer directly here.",
                    },
                    "webhook_url": {
                        "type": "string",
                        "default": "",
                        "description": "Optional HTTPS URL to receive an HMAC-SHA256 signed `invoice.paid` POST webhook upon on-chain settlement.",
                    },
                    "ttl_minutes": {
                        "type": "integer",
                        "default": 60,
                        "description": "Invoice expiration window in minutes (`5` to `43200`). After `ttl_minutes`, unpaid invoices cannot be settled. Defaults to `60`.",
                    },
                    "customer_ref": {
                        "type": "string",
                        "default": "",
                        "description": "Optional external customer identifier (e.g. email, user ID, or Telegram ID) for tracking recurring subscription status.",
                    },
                    "billing_days": {
                        "type": "integer",
                        "default": 0,
                        "description": "Optional subscription duration in days (e.g. `30` for monthly, `365` for annual). Pass `0` for one-time purchases.",
                    },
                },
                "required": ["title", "amount_usd", "network", "merchant_wallet"],
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "checkout_url": {"type": "string"},
                    "invoice": {"type": "object"},
                },
                "required": ["ok"],
            },
        },
        {
            "name": "get_usdt_invoice",
            "description": (
                "Retrieve current settlement status, payment metadata, expiration timestamp, and cryptographic SHA-256 receipt for an existing PayGate invoice by `invoice_id`. "
                "Read-only, non-destructive lookup (unlimited free calls, no authentication required). "
                "Returns JSON with `ok` (boolean), `invoice` (`invoice_id`, `status` as `pending`/`paid`/`expired`, `amount_usd`, `network`, `merchant_wallet`, `tx_hash`, `paid_at`, `metadata`), and `receipt`."
            ),
            "annotations": {
                "title": "Get PayGate USDT/USDC Invoice & Receipt",
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
            "inputSchema": {
                "type": "object",
                "properties": {
                    "invoice_id": {
                        "type": "string",
                        "description": "Unique PayGate invoice identifier to look up (e.g. `inv_a1b2c3d4e5f6`).",
                    },
                },
                "required": ["invoice_id"],
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "invoice": {"type": "object"},
                    "receipt": {"type": "object"},
                },
                "required": ["ok"],
            },
        },
        {
            "name": "list_usdt_invoices",
            "description": (
                "List and filter PayGate USDT/USDC invoices by `merchant_wallet`, settlement `status` (`pending`, `paid`, `expired`), or `customer_ref`. "
                "Read-only query returning up to `limit` most recent invoices in reverse chronological order (unlimited free calls, no authentication required). "
                "Returns JSON with `ok` (boolean), `count` (integer), and `invoices` array."
            ),
            "annotations": {
                "title": "List & Filter PayGate USDT/USDC Invoices",
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
            "inputSchema": {
                "type": "object",
                "properties": {
                    "merchant_wallet": {
                        "type": "string",
                        "default": "",
                        "description": "Optional merchant receiving wallet address (`0x...` or Solana base58) to filter invoices for a specific merchant.",
                    },
                    "status": {
                        "type": "string",
                        "default": "",
                        "enum": ["", "pending", "paid", "expired"],
                        "description": "Optional invoice lifecycle status filter: `pending`, `paid`, `expired`, or empty string for all statuses.",
                    },
                    "customer_ref": {
                        "type": "string",
                        "default": "",
                        "description": "Optional customer identifier (`customer_ref`) to list payment history for a single subscriber or buyer.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 25,
                        "description": "Maximum number of invoices to return (`1` to `100`). Defaults to `25`.",
                    },
                },
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "count": {"type": "integer"},
                    "invoices": {"type": "array"},
                },
                "required": ["ok", "count", "invoices"],
            },
        },
        {
            "name": "verify_usdt_invoice",
            "description": (
                "Verify and settle a confirmed on-chain USDT/USDC transaction hash (`tx_hash`) against a PayGate `invoice_id` via live JSON-RPC receipt log inspection. "
                "Idempotent state-changing operation (unlimited free calls, anti-replay enforced): confirms recipient address, token contract, and minimum USD amount, transitions the invoice to `paid`, extends recurring `subscription_expires_at` if configured, and dispatches any configured webhook. "
                "Returns JSON with `ok` (boolean), `already_paid` (boolean), and updated `invoice` object."
            ),
            "annotations": {
                "title": "Verify & Settle On-Chain USDT/USDC Invoice Payment",
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": True,
            },
            "inputSchema": {
                "type": "object",
                "properties": {
                    "invoice_id": {
                        "type": "string",
                        "description": "Unique PayGate invoice identifier returned by `create_usdt_invoice` (e.g. `inv_a1b2c3d4e5f6`).",
                    },
                    "tx_hash": {
                        "type": "string",
                        "description": "Confirmed on-chain transaction hash (`0x...` 66-char hex on EVM chains or base58 signature on Solana) transferring USDT/USDC to the invoice's `merchant_wallet`.",
                    },
                },
                "required": ["invoice_id", "tx_hash"],
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "invoice": {"type": "object"},
                    "receipt": {"type": "object"},
                },
                "required": ["ok"],
            },
        },
        {
            "name": "expire_usdt_invoice",
            "description": (
                "Cancel and expire an unpaid `pending` PayGate invoice by `invoice_id` so it can no longer be settled. "
                "Idempotent state-changing operation: transitions `pending` invoices to `expired` while leaving already-`paid` invoices untouched (unlimited free calls). "
                "Returns JSON with `ok` (boolean) and the updated `invoice` object (`status: \"expired\"`)."
            ),
            "annotations": {
                "title": "Cancel & Expire Unpaid PayGate Invoice",
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
            "inputSchema": {
                "type": "object",
                "properties": {
                    "invoice_id": {
                        "type": "string",
                        "description": "Unique PayGate invoice identifier (`inv_...`) to cancel/expire.",
                    },
                    "merchant_wallet": {
                        "type": "string",
                        "default": "",
                        "description": "Optional merchant wallet address (`0x...` or Solana base58) to verify ownership before expiring the invoice.",
                    },
                },
                "required": ["invoice_id"],
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "invoice": {"type": "object"},
                    "receipt": {"type": "object"},
                },
                "required": ["ok"],
            },
        },
        {
            "name": "check_usdt_subscription",
            "description": (
                "Check whether a customer (`customer_ref`) has an active non-custodial recurring USDT/USDC subscription with `merchant_wallet`, including remaining days, expiration timestamp, and a pre-filled 1-click renewal checkout URL. "
                "Read-only lookup (unlimited free calls, early renewals automatically stack remaining days without requiring `approve(MAX_UINT256)`). "
                "Returns JSON with `ok`, `active` (boolean), `days_remaining` (number), `expires_at` (ISO-8601 or null), `last_invoice_id`, and `renewal_url`."
            ),
            "annotations": {
                "title": "Check Recurring USDT/USDC Subscription Status",
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
            "inputSchema": {
                "type": "object",
                "properties": {
                    "merchant_wallet": {
                        "type": "string",
                        "description": "Merchant's receiving wallet address (`0x...` on EVM or base58 on Solana) that collects subscription payments.",
                    },
                    "customer_ref": {
                        "type": "string",
                        "description": "Unique subscriber identifier (e.g. user ID, email, or account handle) passed when creating the subscription invoice.",
                    },
                    "amount_usd": {
                        "type": "number",
                        "default": 9.0,
                        "description": "Subscription renewal price in USD used to pre-populate the returned `renewal_url`. Defaults to `9.0`.",
                    },
                    "network": {
                        "type": "string",
                        "default": "polygon",
                        "enum": ["polygon", "bsc", "base", "arbitrum", "ethereum", "solana"],
                        "description": "Settlement network pre-populated in the returned `renewal_url`. Defaults to `polygon`.",
                    },
                    "billing_days": {
                        "type": "integer",
                        "default": 30,
                        "description": "Billing cycle length in days pre-populated in the returned `renewal_url`. Defaults to `30`.",
                    },
                },
                "required": ["merchant_wallet", "customer_ref"],
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "active": {"type": "boolean"},
                    "days_remaining": {"type": "number"},
                    "renewal_url": {"type": "string"},
                },
                "required": ["ok", "active", "days_remaining", "renewal_url"],
            },
        },
        {
            "name": "verify_onchain_transfer",
            "description": (
                "Statelessly verify any standalone on-chain USDT or USDC transfer transaction hash (`tx_hash`) via live blockchain JSON-RPC without requiring a pre-created PayGate invoice ID. "
                "Read-only RPC receipt inspection across `polygon`, `bsc`, `base`, `arbitrum`, `ethereum`, or `solana` confirming that `expected_recipient` received at least `min_amount_usd` in official USDT/USDC. "
                "Returns JSON with `verified` (boolean), `network`, `token` (`USDT` or `USDC`), `amount_usd`, `sender`, `recipient`, `tx_hash`, and `reason`."
            ),
            "annotations": {
                "title": "Stateless On-Chain USDT/USDC Transfer Verifier",
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": True,
            },
            "inputSchema": {
                "type": "object",
                "properties": {
                    "network": {
                        "type": "string",
                        "enum": ["polygon", "bsc", "base", "arbitrum", "ethereum", "solana"],
                        "description": "Blockchain network where `tx_hash` was confirmed (`polygon`, `bsc`, `base`, `arbitrum`, `ethereum`, or `solana`).",
                    },
                    "tx_hash": {
                        "type": "string",
                        "description": "On-chain transaction hash (`0x...` 66-char hex on EVM or base58 signature on Solana) to verify.",
                    },
                    "min_amount_usd": {
                        "type": "number",
                        "description": "Minimum required stablecoin transfer amount in USD (e.g. `19.0`). Transfers below this threshold return `verified: false`.",
                    },
                    "expected_recipient": {
                        "type": "string",
                        "description": "Expected recipient wallet address (`0x...` on EVM or base58 on Solana) that must have received the USDT/USDC transfer.",
                    },
                },
                "required": ["network", "tx_hash", "min_amount_usd", "expected_recipient"],
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "verified": {"type": "boolean"},
                    "network": {"type": "string"},
                    "token": {"type": "string"},
                    "amount_usd": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["verified", "network", "amount_usd"],
            },
        },
        {
            "name": "build_x402_paywall_challenge",
            "description": (
                "Generate a standards-compliant HTTP 402 (`x402 v2`) payment challenge JSON payload, base64 `PAYMENT-REQUIRED` header, and 1-click PayGate `checkout_url` for monetizing any REST or AI-agent API endpoint in USDT/USDC. "
                "Purely read-only deterministic payload builder with zero network calls or side effects. "
                "Returns JSON with `x402Version` (`2`), `amount_usd`, `checkout_url`, `payment_required_header_b64`, and CAIP-2 `accepts` array."
            ),
            "annotations": {
                "title": "Build HTTP 402 (x402 v2) Paywall Challenge Payload",
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
            "inputSchema": {
                "type": "object",
                "properties": {
                    "resource_url": {
                        "type": "string",
                        "description": "Protected HTTPS endpoint URL being monetized (e.g. `https://api.example.com/v1/pro-signal`).",
                    },
                    "price_usd": {
                        "type": "number",
                        "description": "Required per-call or pass price in USD (e.g. `0.05` or `9.0`).",
                    },
                    "recipient_wallet": {
                        "type": "string",
                        "description": "Merchant's EVM wallet address (`0x...`) to receive on-chain USDC/USDT payments across Base, Polygon, Arbitrum, and BSC.",
                    },
                    "description": {
                        "type": "string",
                        "default": "Paid API endpoint (non-custodial USDT/USDC settlement)",
                        "description": "Human- and agent-readable description of what the paid endpoint delivers upon settlement.",
                    },
                },
                "required": ["resource_url", "price_usd", "recipient_wallet"],
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "x402Version": {"type": "integer"},
                    "amount_usd": {"type": "number"},
                    "checkout_url": {"type": "string"},
                    "payment_required_header_b64": {"type": "string"},
                    "accepts": {"type": "array"},
                },
                "required": ["x402Version", "amount_usd", "checkout_url", "accepts"],
            },
        },
    ]


def handle_mcp_jsonrpc(msg: dict[str, Any], base_url: str = DEFAULT_BASE_URL) -> dict[str, Any] | None:
    """Process one JSON-RPC 2.0 MCP message cleanly (pure function, stdio-ready)."""
    method = str(msg.get("method") or "")
    rpc_id = msg.get("id")
    params = msg.get("params") or {}

    if method.startswith("notifications/"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {}}
    if method == "initialize":
        req_proto = str(params.get("protocolVersion") or "2025-06-18")
        supported = {"2025-06-18", "2025-03-26", "2024-11-05"}
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "protocolVersion": req_proto if req_proto in supported else "2025-06-18",
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": {"name": "paygate-usdt", "version": "2.5.1"},
                "instructions": (
                    f"PayGate USDT v2.5.1 — Non-custodial 6-chain USDT/USDC payment gateway MCP (Polygon, BSC, Arbitrum, Base, Ethereum, Solana). "
                    f"Hosted checkout UI: {base_url}/paygate | Remote Streamable-HTTP MCP: {base_url}/paygate/mcp | B2B Turnkey Setup: {base_url}/paygate#b2b"
                ),
            },
        }
    if method == "resources/list":
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "resources": [
                    {
                        "uri": "paygate://quickstart",
                        "name": "PayGate USDT v2.4 Quickstart",
                        "description": "Non-custodial USDT/USDC invoicing, x402 paywalls, and recurring subscriptions.",
                        "mimeType": "text/markdown",
                    }
                ]
            },
        }
    if method == "resources/read":
        uri = str(params.get("uri") or "")
        if uri == "paygate://quickstart":
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "text/markdown",
                            "text": f"# PayGate USDT v2.4\n\n- Checkout UI: {base_url}/paygate\n- Docs: https://github.com/EzeLich/paygate-usdt\n",
                        }
                    ]
                },
            }
        return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32602, "message": f"Unknown URI: {uri}"}}
    if method == "prompts/list":
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "prompts": [
                    {
                        "name": "create_recurring_usdt_plan",
                        "description": "Create a non-custodial 30-day recurring USDT/USDC subscription invoice.",
                    }
                ]
            },
        }
    if method == "prompts/get":
        p_name = str(params.get("name") or "")
        if p_name == "create_recurring_usdt_plan":
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "description": "Generate a 30-day recurring USDT/USDC checkout link via PayGate.",
                    "messages": [
                        {
                            "role": "user",
                            "content": {
                                "type": "text",
                                "text": "Call create_usdt_invoice with billing_days=30 and customer_ref set to the user ID.",
                            },
                        }
                    ],
                },
            }
        return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32602, "message": f"Unknown prompt: {p_name}"}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": _mcp_tools_schema()}}
    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        client = PayGateClient(base_url=base_url)
        if name == "create_usdt_invoice":
            out = client.create_invoice(
                title=str(args.get("title") or "Digital Service"),
                amount_usd=float(args.get("amount_usd") or 10.0),
                network=str(args.get("network") or "polygon"),
                merchant_wallet=str(args.get("merchant_wallet") or ""),
                webhook_url=str(args.get("webhook_url") or ""),
                ttl_minutes=int(args.get("ttl_minutes") or 60),
                customer_ref=str(args.get("customer_ref") or ""),
                billing_days=int(args.get("billing_days") or 0),
            )
        elif name == "check_usdt_subscription":
            out = client.check_subscription(
                merchant_wallet=str(args.get("merchant_wallet") or ""),
                customer_ref=str(args.get("customer_ref") or ""),
                amount_usd=float(args.get("amount_usd") or 9.0),
                network=str(args.get("network") or "polygon"),
                billing_days=int(args.get("billing_days") or 30),
            )
        elif name == "get_usdt_invoice":
            out = client.get_invoice(str(args.get("invoice_id") or ""))
        elif name == "list_usdt_invoices":
            out = client.list_invoices(
                merchant_wallet=str(args.get("merchant_wallet") or ""),
                status=str(args.get("status") or ""),
                customer_ref=str(args.get("customer_ref") or ""),
                limit=int(args.get("limit") or 25),
            )
        elif name == "verify_usdt_invoice":
            out = client.verify_invoice(str(args.get("invoice_id") or ""), str(args.get("tx_hash") or ""))
        elif name == "expire_usdt_invoice":
            out = client.expire_invoice(
                str(args.get("invoice_id") or ""),
                str(args.get("merchant_wallet") or ""),
            )
        elif name in ("verify_onchain_transfer", "verify_onchain_tx"):
            vr = OnChainPaymentVerifier().verify(
                network=str(args.get("network") or "polygon"),
                tx_hash=str(args.get("tx_hash") or ""),
                min_amount_usd=float(args.get("min_amount_usd") or 0.01),
                expected_recipient=str(args.get("expected_recipient") or ""),
            )
            out = {
                "verified": vr.verified,
                "network": vr.network,
                "token": vr.token,
                "amount_usd": vr.amount_usd,
                "recipient": vr.recipient,
                "sender": vr.sender,
                "tx_hash": vr.tx_hash,
                "reason": vr.reason,
            }
        elif name == "build_x402_paywall_challenge":
            r_url = str(args.get("resource_url") or f"{base_url}/api/v1/pro").strip()
            p_usd = max(0.01, float(args.get("price_usd") or 1.0))
            r_wal = str(args.get("recipient_wallet") or "").strip()
            desc = str(args.get("description") or "Paid API endpoint (non-custodial USDT/USDC settlement)").strip()
            ch = build_x402_challenge(
                resource_url=r_url,
                description=desc,
                price_usd=p_usd,
                evm_recipient=r_wal,
            )
            ch["checkout_url"] = client.build_payment_url(
                merchant_wallet=r_wal,
                amount_usd=p_usd,
                network="base",
                title=desc[:60],
            )
            ch["payment_required_header_b64"] = base64.b64encode(json.dumps(ch).encode("utf-8")).decode("ascii")
            out = ch
        else:
            return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601, "message": f"Unknown tool: {name}"}}
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {"content": [{"type": "text", "text": json.dumps(out, indent=2)}]}}

    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def run_stdio_mcp(base_url: str = DEFAULT_BASE_URL) -> int:
    for line in sys.stdin:
        raw = line.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
            continue
        resp = handle_mcp_jsonrpc(msg, base_url=base_url)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="paygate-usdt", description="Non-custodial USDT/USDC CLI & MCP Server")
    sub = parser.add_subparsers(dest="command", required=True)

    p_mcp = sub.add_parser("mcp", help="Run local stdio MCP server for Claude Desktop / Cursor / OpenCode")
    p_mcp.add_argument("--base-url", default=DEFAULT_BASE_URL)

    p_inv = sub.add_parser("invoice", help="Create a hosted non-custodial USDT/USDC checkout invoice")
    p_inv.add_argument("--title", required=True)
    p_inv.add_argument("--amount", type=float, required=True)
    p_inv.add_argument("--network", default="polygon")
    p_inv.add_argument("--wallet", required=True)
    p_inv.add_argument("--webhook-url", default="")
    p_inv.add_argument("--base-url", default=DEFAULT_BASE_URL)

    p_ver = sub.add_parser("verify-tx", help="Verify an on-chain USDT/USDC transfer directly via RPC")
    p_ver.add_argument("--network", required=True)
    p_ver.add_argument("--tx", required=True)
    p_ver.add_argument("--amount", type=float, required=True)
    p_ver.add_argument("--wallet", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "mcp":
        return run_stdio_mcp(base_url=args.base_url)
    if args.command == "invoice":
        res = PayGateClient(base_url=args.base_url).create_invoice(
            title=args.title,
            amount_usd=args.amount,
            network=args.network,
            merchant_wallet=args.wallet,
            webhook_url=args.webhook_url,
        )
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1
    if args.command == "verify-tx":
        vr = OnChainPaymentVerifier().verify(
            network=args.network,
            tx_hash=args.tx,
            min_amount_usd=args.amount,
            expected_recipient=args.wallet,
        )
        print(json.dumps({"verified": vr.verified, "network": vr.network, "token": vr.token, "amount_usd": vr.amount_usd, "reason": vr.reason}, indent=2))
        return 0 if vr.verified else 1
    return 1
