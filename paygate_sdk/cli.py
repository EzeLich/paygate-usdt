"""
PayGate USDT CLI & Local Stdio MCP Server (`python -m paygate_sdk`).
Supports Claude Desktop / Cursor / OpenCode stdio MCP plus direct invoice & tx verification commands.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from app.onchain_verifier import OnChainPaymentVerifier
from . import PayGateClient

DEFAULT_BASE_URL = "https://chainguard-studio-production.up.railway.app"


def _mcp_tools_schema() -> list[dict[str, Any]]:
    return [
        {
            "name": "create_usdt_invoice",
            "description": "Create a non-custodial multi-chain USDT/USDC hosted checkout invoice settling 100% directly to merchant_wallet.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "amount_usd": {"type": "number"},
                    "network": {"type": "string", "default": "polygon"},
                    "merchant_wallet": {"type": "string"},
                    "webhook_url": {"type": "string", "default": ""},
                    "ttl_minutes": {"type": "integer", "default": 60},
                },
                "required": ["title", "amount_usd", "network", "merchant_wallet"],
            },
        },
        {
            "name": "get_usdt_invoice",
            "description": "Fetch status and payment details of an existing PayGate invoice ID.",
            "inputSchema": {
                "type": "object",
                "properties": {"invoice_id": {"type": "string"}},
                "required": ["invoice_id"],
            },
        },
        {
            "name": "verify_usdt_invoice",
            "description": "Verify an on-chain USDT/USDC tx_hash against a PayGate invoice ID and settle it.",
            "inputSchema": {
                "type": "object",
                "properties": {"invoice_id": {"type": "string"}, "tx_hash": {"type": "string"}},
                "required": ["invoice_id", "tx_hash"],
            },
        },
        {
            "name": "verify_onchain_tx",
            "description": "Directly verify an ERC-20 or SPL USDT/USDC transfer via live JSON-RPC (Polygon, BSC, Base, Arbitrum, Ethereum, Solana).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "network": {"type": "string"},
                    "tx_hash": {"type": "string"},
                    "min_amount_usd": {"type": "number"},
                    "expected_recipient": {"type": "string"},
                },
                "required": ["network", "tx_hash", "min_amount_usd", "expected_recipient"],
            },
        },
    ]


def handle_mcp_jsonrpc(msg: dict[str, Any], base_url: str = DEFAULT_BASE_URL) -> dict[str, Any] | None:
    """Process one JSON-RPC 2.0 MCP message cleanly (pure function, stdio-ready)."""
    method = str(msg.get("method") or "")
    rpc_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "paygate-usdt", "version": "2.1.0"},
            },
        }
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
            )
        elif name == "get_usdt_invoice":
            out = client.get_invoice(str(args.get("invoice_id") or ""))
        elif name == "verify_usdt_invoice":
            out = client.verify_invoice(str(args.get("invoice_id") or ""), str(args.get("tx_hash") or ""))
        elif name == "verify_onchain_tx":
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
