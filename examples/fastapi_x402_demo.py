"""Minimal FastAPI server protected by PayGate USDT HTTP 402 (x402 v2)."""
from __future__ import annotations

import os
from fastapi import FastAPI, Request
from paygate_sdk import PayGateClient, require_usdt_payment

MERCHANT_WALLET = os.getenv("PAYGATE_EVM_ADDRESS", "0x0000000000000000000000000000000000000000")

app = FastAPI(title="PayGate USDT x402 Demo")


@app.get("/")
def root():
    return {
        "service": "PayGate USDT x402 Demo",
        "repo": "https://github.com/EzeLich/paygate-usdt",
        "protected_endpoint": "/api/pro-signal",
    }


@app.get("/api/pro-signal")
@require_usdt_payment(
    price_usd=2.0,
    recipient_wallet=MERCHANT_WALLET,
    networks=("polygon", "base", "bsc"),
    description="Pro crypto market signal unlocked via non-custodial USDT/USDC transfer.",
)
async def pro_signal(request: Request):
    return {"ok": True, "signal": "BTC/USDT momentum trigger verified"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
