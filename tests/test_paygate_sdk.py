from __future__ import annotations

import base64
import json
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.ledger import StudioLedger
from app.onchain_verifier import OnChainPaymentVerifier, decode_evm_stablecoin_transfer
from app.paygate import sign_paygate_webhook, verify_and_settle_invoice
from paygate_sdk import (
    PayGateClient,
    build_x402_challenge,
    require_usdt_payment,
    verify_webhook_signature,
)


def test_hmac_webhook_signature_verification():
    body = b'{"event":"invoice.paid","invoice_id":"inv_123","amount_usd":49.0}'
    secret = "whsec_live_super_secret"
    sig = "sha256=" + sign_paygate_webhook(body, secret)

    assert verify_webhook_signature(body, sig, secret) is True
    assert verify_webhook_signature(body, sig, "wrong_secret") is False
    assert verify_webhook_signature(b'{"tampered":true}', sig, secret) is False
    assert verify_webhook_signature(body, "", secret) is False


def test_erc20_transfer_decoder_polygon_and_bsc():
    merchant = "0x3333333333333333333333333333333333333333"
    rec_topic = "0x" + merchant.lower().removeprefix("0x").rjust(64, "0")
    sender_topic = "0x" + "11".rjust(64, "0")

    # Polygon USDC (6 decimals) -> $49.50
    receipt_poly = {
        "transactionHash": "0x" + "aa" * 32,
        "status": "0x1",
        "logs": [
            {
                "address": "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                    sender_topic,
                    rec_topic,
                ],
                "data": hex(49_500_000),
            }
        ],
    }
    res_poly = decode_evm_stablecoin_transfer(receipt_poly, "polygon", merchant, 49.0)
    assert res_poly.verified is True
    assert res_poly.token == "USDC"
    assert res_poly.amount_usd == 49.5

    # BSC BEP20 USDT (18 decimals) -> $199.00
    receipt_bsc = {
        "transactionHash": "0x" + "bb" * 32,
        "status": "0x1",
        "logs": [
            {
                "address": "0x55d398326f99059ff775485246999027b3197955",
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                    sender_topic,
                    rec_topic,
                ],
                "data": hex(199 * 10**18),
            }
        ],
    }
    res_bsc = decode_evm_stablecoin_transfer(receipt_bsc, "bsc", merchant, 199.0)
    assert res_bsc.verified is True
    assert res_bsc.token == "USDT"
    assert res_bsc.amount_usd == 199.0

    # Wrong recipient rejected
    res_wrong = decode_evm_stablecoin_transfer(
        receipt_bsc, "bsc", "0x4444444444444444444444444444444444444444", 199.0
    )
    assert res_wrong.verified is False


def test_fastapi_require_usdt_payment_x402_and_replay_protection(monkeypatch):
    merchant = "0x3333333333333333333333333333333333333333"
    tx_hash = "0x" + "cc" * 32
    rec_topic = "0x" + merchant.lower().removeprefix("0x").rjust(64, "0")
    sender_topic = "0x" + "22" * 32

    def fake_rpc(url: str, payload: dict, timeout_s: float = 10.0):
        return {
            "result": {
                "transactionHash": tx_hash,
                "status": "0x1",
                "logs": [
                    {
                        "address": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                        "topics": [
                            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                            sender_topic,
                            rec_topic,
                        ],
                        "data": hex(5 * 10**6),
                    }
                ],
            }
        }

    monkeypatch.setattr("app.onchain_verifier._rpc_post", fake_rpc)

    api = FastAPI()

    @api.get("/premium")
    @require_usdt_payment(price_usd=5.0, recipient_wallet=merchant, networks=("base", "polygon"))
    async def premium_route(request: Request):
        return {"unlocked": True, "secret_data": "ok"}

    client = TestClient(api)

    # 1. Unpaid -> HTTP 402 + PAYMENT-REQUIRED x402 v2 header
    r402 = client.get("/premium")
    assert r402.status_code == 402
    assert r402.headers.get("X-Payment-Protocol") == "x402"
    decoded = json.loads(base64.b64decode(r402.headers["PAYMENT-REQUIRED"]).decode("utf-8"))
    assert decoded["x402Version"] == 2
    assert decoded["accepts"][0]["payTo"] == merchant

    # 2. Paid -> 200 OK
    r200 = client.get("/premium", headers={"X-Payment-Network": "base", "X-Payment-Tx": tx_hash})
    assert r200.status_code == 200
    assert r200.json()["unlocked"] is True

    # 3. Replay -> blocked 402
    r_rep = client.get("/premium", headers={"X-Payment-Network": "base", "X-Payment-Tx": tx_hash})
    assert r_rep.status_code == 402


def test_ledger_invoice_lifecycle_and_webhook_dispatch(tmp_path, monkeypatch):
    ledger = StudioLedger(tmp_path / "sdk_test.db")
    merchant = "0x3333333333333333333333333333333333333333"
    secret = "sec_123456"

    inv = ledger.create_paygate_invoice(
        title="Annual API Seat",
        amount_usd=25.0,
        network="polygon",
        merchant_wallet=merchant,
        webhook_url="https://hooks.example.com/paygate",
        webhook_secret=secret,
    )
    assert inv["status"] == "pending"

    tx_hash = "0x" + "dd" * 32
    rec_topic = "0x" + merchant.lower().removeprefix("0x").rjust(64, "0")
    sender_topic = "0x" + "77" * 32

    monkeypatch.setattr(
        "app.onchain_verifier._rpc_post",
        lambda u, p, timeout_s=10.0: {
            "result": {
                "transactionHash": tx_hash,
                "status": "0x1",
                "logs": [
                    {
                        "address": "0xc2132d05d31c914a87c6611c10748aeb04b58e8f",
                        "topics": [
                            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                            sender_topic,
                            rec_topic,
                        ],
                        "data": hex(25 * 10**6),
                    }
                ],
            }
        },
    )

    sent: list[tuple[bytes, str]] = []

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        "app.paygate.urllib.request.urlopen",
        lambda req, timeout=8: sent.append((req.data or b"", req.get_header("X-paygate-signature") or "")) or _FakeResp(),
    )

    out = verify_and_settle_invoice(
        invoice_id=inv["invoice_id"],
        tx_hash=tx_hash,
        ledger=ledger,
        verifier=OnChainPaymentVerifier(),
    )
    assert out["ok"] is True
    assert out["invoice"]["status"] == "paid"
    assert len(sent) == 1
    assert verify_webhook_signature(sent[0][0], sent[0][1], secret) is True


def test_paygate_client_create_invoice_mock(monkeypatch):
    def fake_req(self, path, method="GET", payload=None):
        assert path == "/api/v1/paygate/invoices"
        assert method == "POST"
        return {
            "ok": True,
            "checkout_url": "https://example.com/paygate/checkout/inv_demo",
            "invoice": {"invoice_id": "inv_demo", **payload},
        }

    monkeypatch.setattr(PayGateClient, "_request", fake_req)
    client = PayGateClient("https://example.com")
    res = client.create_invoice(
        title="Agent Kit",
        amount_usd=19.0,
        network="base",
        merchant_wallet="0x3333333333333333333333333333333333333333",
    )
    assert res["ok"] is True
    assert res["invoice"]["invoice_id"] == "inv_demo"
    ch = build_x402_challenge(resource_url="https://api.example.com/v1/pro", description="Pro", price_usd=1.0)
    assert ch["x402Version"] == 2


def test_stdio_mcp_jsonrpc_and_cli(monkeypatch, capsys):
    from paygate_sdk import handle_mcp_jsonrpc
    from paygate_sdk.cli import main as cli_main

    init = handle_mcp_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "paygate-usdt"
    assert handle_mcp_jsonrpc({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    tl = handle_mcp_jsonrpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools = tl["result"]["tools"]
    tool_names = [t["name"] for t in tools]
    assert len(tools) == 8
    assert "create_usdt_invoice" in tool_names
    assert "list_usdt_invoices" in tool_names
    assert "expire_usdt_invoice" in tool_names
    assert "verify_onchain_transfer" in tool_names
    assert "build_x402_paywall_challenge" in tool_names
    for t in tools:
        assert "annotations" in t and "outputSchema" in t
        for p_name, p_schema in t["inputSchema"].get("properties", {}).items():
            assert p_schema.get("description"), f"Missing description on {t['name']}.{p_name}"

    monkeypatch.setattr(
        PayGateClient,
        "create_invoice",
        lambda self, **kw: {
            "ok": True,
            "checkout_url": "https://example.com/paygate/checkout/inv_cli1",
            "invoice": {"invoice_id": "inv_cli1", "title": kw["title"]},
        },
    )
    tc = handle_mcp_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "create_usdt_invoice",
                "arguments": {
                    "title": "MCP Tool Invoice",
                    "amount_usd": 12.0,
                    "network": "polygon",
                    "merchant_wallet": "0x3333333333333333333333333333333333333333",
                },
            },
        }
    )
    payload = json.loads(tc["result"]["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["invoice"]["invoice_id"] == "inv_cli1"

    rc = cli_main(
        [
            "invoice",
            "--title",
            "CLI Test Invoice",
            "--amount",
            "15",
            "--network",
            "base",
            "--wallet",
            "0x3333333333333333333333333333333333333333",
        ]
    )
    assert rc == 0
    out_json = json.loads(capsys.readouterr().out)
    assert out_json["checkout_url"].endswith("/inv_cli1")


def test_stateless_payment_url_receipt_and_widget():
    from app.paygate import build_invoice_receipt, render_paygate_widget_js

    client = PayGateClient("https://chainguard-studio-production.up.railway.app")
    url = client.build_payment_url(
        merchant_wallet="0x3333333333333333333333333333333333333333",
        amount_usd=9.0,
        network="base",
        title="Unlimited MCP Pass",
        issue_pass=True,
    )
    assert "/paygate/pay?" in url
    assert "issue_pass=1" in url
    assert "network=base" in url

    rec = build_invoice_receipt(
        {
            "invoice_id": "inv_test99",
            "status": "paid",
            "title": "Unlimited MCP Pass",
            "amount_usd": 9.0,
            "paid_amount_usd": 9.0,
            "token": "USDC",
            "network": "base",
            "merchant_wallet": "0x3333333333333333333333333333333333333333",
            "payer": "0x1111111111111111111111111111111111111111",
            "tx_hash": "0x" + "ee" * 32,
            "paid_at": "2026-11-24T06:00:00+00:00",
        }
    )
    assert len(rec["receipt_sha256"]) == 64
    assert rec["explorer_url"].startswith("https://basescan.org/tx/")
    js = render_paygate_widget_js("https://chainguard-studio-production.up.railway.app")
    assert "data-paygate-merchant" in js


def test_v23_recurring_subscriptions_and_mcp_handshake(tmp_path):
    from paygate_sdk.cli import handle_mcp_jsonrpc

    client = PayGateClient("https://chainguard-studio-production.up.railway.app")
    sub_url = client.build_payment_url(
        merchant_wallet="0x3333333333333333333333333333333333333333",
        amount_usd=19.0,
        network="base",
        title="SaaS Pro",
        customer_ref="cust_99",
        billing_days=30,
    )
    assert "customer_ref=cust_99" in sub_url
    assert "billing_days=30" in sub_url

    ledger = StudioLedger(str(tmp_path / "subs_v23.db"))
    wallet = "0x3333333333333333333333333333333333333333"
    inv = ledger.find_or_create_paygate_invoice(
        title="SaaS Pro",
        amount_usd=19.0,
        network="polygon",
        merchant_wallet=wallet,
        metadata={"customer_ref": "cust_99", "billing_days": 30},
    )
    ledger.mark_paygate_invoice_paid(
        inv["invoice_id"],
        tx_hash="0x" + "77" * 32,
        paid_amount_usd=19.0,
        token="USDC",
        payer="0x1111111111111111111111111111111111111111",
    )
    status = ledger.get_paygate_subscription_status(merchant_wallet=wallet, customer_ref="cust_99")
    assert status["active"] is True
    assert status["days_remaining"] >= 29.0

    # MCP 2025-03-26 handshake methods
    init = handle_mcp_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
    assert init["result"]["protocolVersion"] == "2025-03-26"
    assert handle_mcp_jsonrpc({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert handle_mcp_jsonrpc({"jsonrpc": "2.0", "id": 2, "method": "ping"}) == {"jsonrpc": "2.0", "id": 2, "result": {}}
    assert len(handle_mcp_jsonrpc({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})["result"]["resources"]) >= 1
    assert len(handle_mcp_jsonrpc({"jsonrpc": "2.0", "id": 4, "method": "prompts/list"})["result"]["prompts"]) >= 1


def test_mcp_tool_paywall_decorator_free_quota_and_paid_unlock(monkeypatch):
    from paygate_sdk import PayGateClient, mcp_tool_paywall

    merchant = "0x3333333333333333333333333333333333333333"

    @mcp_tool_paywall(
        price_usd=9.0,
        merchant_wallet=merchant,
        network="base",
        title="Alpha MCP Tool",
        free_calls=2,
        billing_days=30,
    )
    def get_alpha_signal(symbol: str, customer_ref: str = "", paygate_invoice_id: str = "") -> dict:
        return {"symbol": symbol, "signal": "LONG"}

    monkeypatch.setattr(
        PayGateClient,
        "check_subscription",
        lambda self, **kw: {"ok": True, "active": False, "renewal_url": "https://example.com/pay"},
    )

    # 1. First 2 calls succeed on free tier and include paygate_billing
    r1 = get_alpha_signal(symbol="ETH", customer_ref="agent_7")
    assert r1["signal"] == "LONG"
    assert r1["paygate_billing"]["tier"] == "free_tier"
    assert r1["paygate_billing"]["free_calls_remaining"] == 1

    r2 = get_alpha_signal(symbol="SOL", customer_ref="agent_7")
    assert r2["paygate_billing"]["free_calls_remaining"] == 0

    # 2. 3rd call hits paywall when subscription is inactive
    r3 = get_alpha_signal(symbol="BTC", customer_ref="agent_7")
    assert r3["ok"] is False
    assert "Free MCP tool quota reached" in r3["error"]
    assert "/paygate/pay?" in r3["checkout_url"]

    # 3. Passing a paid paygate_invoice_id unlocks immediately
    monkeypatch.setattr(
        PayGateClient,
        "get_invoice",
        lambda self, inv_id: {
            "ok": True,
            "invoice": {
                "invoice_id": inv_id,
                "status": "paid",
                "merchant_wallet": merchant,
                "paid_amount_usd": 9.0,
                "tx_hash": "0x" + "99" * 32,
                "network": "base",
            },
        },
    )
    r4 = get_alpha_signal(symbol="BTC", customer_ref="agent_7", paygate_invoice_id="inv_paid1")
    assert r4["signal"] == "LONG"
    assert r4["paygate_billing"]["tier"] == "paid_invoice"



