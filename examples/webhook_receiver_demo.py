"""Minimal FastAPI receiver verifying X-PayGate-Signature (HMAC-SHA256)."""
from __future__ import annotations

import json
import os
from fastapi import FastAPI, HTTPException, Request
from paygate_sdk import verify_webhook_signature

WEBHOOK_SECRET = os.getenv("PAYGATE_WEBHOOK_SECRET", "whsec_demo")

app = FastAPI(title="PayGate Webhook Receiver Demo")


@app.post("/webhooks/paygate")
async def handle_paygate_webhook(request: Request):
    raw_body = await request.body()
    sig = request.headers.get("X-PayGate-Signature", "")
    if not verify_webhook_signature(raw_body, sig, WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid X-PayGate-Signature")
    data = json.loads(raw_body.decode("utf-8"))
    return {"ok": True, "event": data.get("event"), "invoice_id": data.get("invoice_id")}
