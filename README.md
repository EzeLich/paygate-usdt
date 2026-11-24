# PayGate USDT v2.0 — Non-Custodial Stablecoin Payment Gateway, x402 Paywall, Telegram VIP Bot & MCP Server

[![CI](https://github.com/EzeLich/paygate-usdt/actions/workflows/ci.yml/badge.svg)](https://github.com/EzeLich/paygate-usdt/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB.svg)](https://python.org)
[![Protocol: x402 v2](https://img.shields.io/badge/Protocol-x402%20v2-ff6600.svg)](https://chainguard-studio-production.up.railway.app/.well-known/x402.json)
[![MCP Native](https://img.shields.io/badge/MCP-Streamable%20HTTP-38bdf8.svg)](https://chainguard-studio-production.up.railway.app/mcp)
[![Custody: Zero](https://img.shields.io/badge/Custody-0%25%20Non--Custodial-10b981.svg)](https://chainguard-studio-production.up.railway.app/paygate)

**Accept USDT & USDC across 6 blockchains straight to your own self-custody wallet — with Hosted Checkout Invoices, HMAC-SHA256 Signed Webhooks, FastAPI `HTTP 402` (`x402 v2`) API Paywalls, Automated Telegram VIP Groups, and an AI-Agent MCP Server.**

No custodial processor holds your funds. No Stripe / Whop / bank account requirements. No platform percentage cut.

- 🌐 **Live Cloud Sandbox & Demo:** [https://chainguard-studio-production.up.railway.app/paygate](https://chainguard-studio-production.up.railway.app/paygate)
- 🤖 **Live Telegram VIP Bot Demo:** [@ChainGuardVaultBot (`?start=vip`)](https://t.me/ChainGuardVaultBot?start=vip)
- 🧠 **Live MCP Server Endpoint:** `POST https://chainguard-studio-production.up.railway.app/mcp` (`create_usdt_invoice`, `verify_usdt_invoice`)

---

## Why Builders Choose PayGate USDT Over Custodial Processors

| Feature | PayGate USDT v2.0 | Custodial Gateways / Whop / InviteMember |
|---|---|---|
| **Settlement Destination** | **100% direct buyer -> your wallet** (Phantom, MetaMask, Ledger, Safe, Exchange) | Held by intermediary; payout delays / freezes |
| **Transaction Fees** | **$0% cut** on settlements (self-hosted MIT) | 1%–10% + withdrawal minimums |
| **Networks Supported** | **Polygon, BSC (BEP-20), Base, Arbitrum, Ethereum, Solana SPL** | Limited or custodian-locked |
| **Hosted Invoice Pages + Webhooks** | ✅ `/paygate/checkout/{id}` + `X-PayGate-Signature` (`HMAC-SHA256`) | Often requires merchant KYB/KYC |
| **FastAPI HTTP 402 (`x402 v2`) Decorator** | ✅ `@require_usdt_payment(...)` with `PAYMENT-REQUIRED` header + anti-replay | Rare / separate SDK |
| **Telegram Paid VIP Automation** | ✅ 60-second bot onboarding, 1-time invite links, auto-kick on expiry | High % revenue share |
| **MCP Server for AI Agents** | ✅ `create_usdt_invoice` & `verify_usdt_invoice` native tools | Not supported |
| **Security & Reproducibility** | **100% pure auditable Python**, full `pytest` + GitHub Actions CI | Black-box binaries / closed SaaS |

---

## Architecture Overview

```text
  Merchant / SaaS / AI Agent
        │
        ├── 1. Invoice API or MCP (`create_usdt_invoice`)
        │        └─> Hosted Checkout URL (/paygate/checkout/{inv_id})
        │
        ├── 2. FastAPI `@require_usdt_payment` Route Guard
        │        └─> Returns HTTP 402 + `PAYMENT-REQUIRED` (x402 v2 JSON)
        │
        └── 3. Multi-Tenant Telegram VIP Bot (`/vip` / `/start`)
                 │
                 ▼
      Buyer transfers USDT / USDC directly to Merchant Wallet
      (Polygon · BSC BEP20 · Base · Arbitrum · Ethereum · Solana)
                 │
                 ▼
      PayGate On-Chain RPC Verifier (live JSON-RPC + SQLite anti-replay)
                 │
                 ├── Signed `invoice.paid` Webhook (`X-PayGate-Signature: sha256=...`)
                 ├── Single-use Telegram Invite Link (`member_limit=1`) + Expiry Kick
                 └── HTTP 200 API Response Unlocked
```

---

## 30-Second Quickstarts

### 1. Protect any FastAPI Endpoint with HTTP 402 (`x402 v2` Direct USDT/USDC)

```python
from fastapi import FastAPI, Request
from paygate_sdk import require_usdt_payment

app = FastAPI()

@app.get("/api/pro-signal")
@require_usdt_payment(
    price_usd=5.0,
    recipient_wallet="0xYourEvmWalletAddress",
    networks=("base", "polygon", "arbitrum", "bsc"),
)
async def pro_signal(request: Request):
    return {"signal": "ETH/USDC breakout verified"}
```

Unpaid callers receive `402 Payment Required` with an x402 v2 `PAYMENT-REQUIRED` header. Callers pass `X-Payment-Network` + `X-Payment-Tx` and PayGate verifies the stablecoin `Transfer` event on-chain with built-in anti-replay protection.

### 2. Create Hosted Checkout Links & Verify HMAC-SHA256 Webhooks

```python
from paygate_sdk import PayGateClient, verify_webhook_signature

client = PayGateClient("https://chainguard-studio-production.up.railway.app")

res = client.create_invoice(
    title="SaaS Pro Seat (30 Days)",
    amount_usd=29.0,
    network="polygon",  # polygon | bsc | base | arbitrum | ethereum | solana
    merchant_wallet="0xYourEvmWalletAddress",
    webhook_url="https://yourapp.com/webhooks/paygate",
    webhook_secret="whsec_your_signing_secret",
)
print("Send buyer to:", res["checkout_url"])

# Inside your webhook receiver:
is_authentic = verify_webhook_signature(raw_body_bytes, request.headers["X-PayGate-Signature"], "whsec_your_signing_secret")
```

### 3. Verify On-Chain Transfers Standalone (`OnChainPaymentVerifier`)

```python
from app.onchain_verifier import OnChainPaymentVerifier

verifier = OnChainPaymentVerifier()
result = verifier.verify(
    network="bsc",
    tx_hash="0x...",
    min_amount_usd=199.0,
    expected_recipient="0xYourBep20WalletAddress",
)
print(result.verified, result.amount_usd, result.token)
```

### 4. CLI & Local Stdio MCP Server (Claude Desktop / Cursor / OpenCode)

Run an instant hosted checkout link directly from your terminal:

```bash
pip install -e .
paygate-usdt invoice --title "VIP Access" --amount 49 --network polygon --wallet 0xYourWallet
paygate-usdt verify-tx --network bsc --tx 0x... --amount 199 --wallet 0xYourWallet
```

Or connect PayGate USDT as a local Stdio MCP server (`mcpServers` config):

```json
{
  "mcpServers": {
    "paygate-usdt": {
      "command": "python",
      "args": ["-m", "paygate_sdk", "mcp"]
    }
  }
}
```

### 5. Runnable Examples, 1-Line Embed Widget & Docker

Add a non-custodial USDT/USDC pay button to **any static site, GitHub README, or Markdown page** in 1 URL or 1 `<script>` tag:

```html
<a data-paygate-merchant="0xYourWallet" data-paygate-amount="29" data-paygate-network="polygon" data-paygate-title="Pro Access">
  Pay $29 USDT
</a>
<script src="https://chainguard-studio-production.up.railway.app/paygate/widget.js"></script>
```

Or generate a stateless 1-click checkout URL directly in Python:

```python
from paygate_sdk import PayGateClient
url = PayGateClient().build_payment_url(
    merchant_wallet="0xYourWallet",
    amount_usd=29.0,
    network="polygon",
    title="Pro Plan (30d)",
)
```

- [`examples/fastapi_x402_demo.py`](examples/fastapi_x402_demo.py) — FastAPI paywall route protected by `@require_usdt_payment`
- [`examples/webhook_receiver_demo.py`](examples/webhook_receiver_demo.py) — FastAPI receiver verifying `X-PayGate-Signature` (`HMAC-SHA256`)

```bash
docker compose up --build
```

---

## Run Tests Locally

```bash
git clone https://github.com/EzeLich/paygate-usdt.git
cd paygate-usdt
pip install -r requirements.txt
pytest -v
```

Zero external API keys or private wallet keys are required to run the test suite.

---

## Hosted Cloud, 1-Click Passes & Done-For-You B2B Deployment

Want this running for your business without touching servers, RPC failovers, or webhook glue? Click any instant non-custodial checkout badge below (supports **1-Click MetaMask / Rabby / Phantom Browser Pay** + automatic SHA-256 Receipt):

| Option | Price | Instant 1-Click Checkout / Action |
|---|---|---|
| **Open-Source Self-Hosted (`MIT`)** | **$0** | Clone this repo, run with FastAPI/uvicorn + `.env`, 0% fees |
| **30-Day Unlimited MCP & Pro API Pass** | **$9 USDC/USDT** | [⚡ Pay $9 Base USDC/USDT — Auto-Mints `cg_live_` Key](https://chainguard-studio-production.up.railway.app/paygate/pay?merchant=0x4Ed4D0750453C027FA8398067d5af980Bcc9B6eD&amount=9&network=base&title=30-Day+Unlimited+MCP+%26+Pro+API+Pass&issue_pass=1) |
| **Managed Cloud VIP Bot** | **$29 USDT/mo** (7-day free trial) | [Launch Bot Free in 60s](https://chainguard-studio-production.up.railway.app/paygate#setup) |
| **Lifetime White-Label Bot License** | **$79 USDT** one-time | [⚡ Pay $79 Polygon USDT/USDC](https://chainguard-studio-production.up.railway.app/paygate/pay?merchant=0x4Ed4D0750453C027FA8398067d5af980Bcc9B6eD&amount=79&network=polygon&title=PayGate+Lifetime+White-Label+License) |
| **Turnkey B2B VIP Gate Deployment (24h)** | **$199 USDT** (`$99` deposit) | [⚡ Pay $99 Kickoff Deposit (Polygon)](https://chainguard-studio-production.up.railway.app/paygate/pay?merchant=0x4Ed4D0750453C027FA8398067d5af980Bcc9B6eD&amount=99&network=polygon&title=Turnkey+VIP+Gate+Deployment+50pct+Deposit) · [Book B2B Scope](https://chainguard-studio-production.up.railway.app/paygate#b2b) |
| **Custom Crypto Bot + API / Exchange Automation** | **$499 USDT** (`$249` deposit) | [⚡ Pay $249 Kickoff Deposit (Polygon)](https://chainguard-studio-production.up.railway.app/paygate/pay?merchant=0x4Ed4D0750453C027FA8398067d5af980Bcc9B6eD&amount=249&network=polygon&title=Custom+Bot+%2B+Exchange+API+50pct+Deposit) · [Submit GitHub Inquiry](https://github.com/EzeLich/paygate-usdt/issues/new?template=b2b_custom_deployment.yml) |

- ⚡ **Book Directly with On-Chain Checkout:** [https://chainguard-studio-production.up.railway.app/paygate#b2b](https://chainguard-studio-production.up.railway.app/paygate#b2b)
- 📝 **Submit a Custom Scope via GitHub Issues:** [Open a B2B Custom Integration Request](https://github.com/EzeLich/paygate-usdt/issues/new?template=b2b_custom_deployment.yml)
