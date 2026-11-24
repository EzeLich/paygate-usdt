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
