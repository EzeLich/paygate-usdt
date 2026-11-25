# PayGate USDT v2.6 — Non-Custodial Stablecoin Payment Gateway, Python & TypeScript/Node.js SDKs, `@mcp_tool_paywall` & 8-Tool MCP Server

[![CI](https://github.com/EzequielLech/paygate-usdt/actions/workflows/ci.yml/badge.svg)](https://github.com/EzequielLech/paygate-usdt/actions/workflows/ci.yml)
[![Official MCP Registry](https://img.shields.io/badge/MCP%20Registry-io.github.EzequielLech%2Fpaygate--usdt-2563eb.svg)](https://registry.modelcontextprotocol.io/v0/servers?search=EzequielLech)
[![PayGate USDT MCP server](https://glama.ai/mcp/servers/EzeLich/paygate-usdt/badges/score.svg)](https://glama.ai/mcp/servers/EzeLich/paygate-usdt)
[![PayGate USDT MCP connector](https://glama.ai/mcp/connectors/io.github.EzeLich/paygate-usdt/badges/score.svg)](https://glama.ai/mcp/connectors/io.github.EzeLich/paygate-usdt)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB.svg)](https://python.org)
[![Node.js 18+ TypeScript](https://img.shields.io/badge/Node.js-18%2B%20TypeScript%20SDK-339933.svg)](sdk-js/index.d.ts)
[![Protocol: x402 v2](https://img.shields.io/badge/Protocol-x402%20v2-ff6600.svg)](https://chainguard-studio-production.up.railway.app/.well-known/x402.json)
[![MCP Native](https://img.shields.io/badge/MCP-Streamable%20HTTP%202025--06--18-38bdf8.svg)](https://chainguard-studio-production.up.railway.app/paygate/mcp)
[![Custody: Zero](https://img.shields.io/badge/Custody-0%25%20Non--Custodial-10b981.svg)](https://chainguard-studio-production.up.railway.app/paygate)

**Monetize any Python or TypeScript/Node.js MCP Server (`@mcp_tool_paywall` / `withMcpToolPaywall`), FastAPI / Express Route (`@require_usdt_payment` / `paygateExpressMiddleware`), SaaS Subscription, or Telegram VIP Group in USDT & USDC across 6 blockchains straight to your own self-custody wallet.**

No custodial processor holds your funds. No Stripe / Whop / bank account requirements. No platform percentage cut. Includes **1-Click Multi-Chain Switcher** (`Base`, `Polygon`, `Arbitrum`, `BSC`, `Ethereum`, `Solana`), **Zero-Tx-Hash On-Chain Payment Auto-Detection**, and a **Self-Serve Merchant Revenue Dashboard (`/paygate/merchant`)**.

- **Live Cloud Sandbox & Demo:** [https://chainguard-studio-production.up.railway.app/paygate](https://chainguard-studio-production.up.railway.app/paygate)
- **Self-Serve Merchant Revenue Dashboard:** [https://chainguard-studio-production.up.railway.app/paygate/merchant](https://chainguard-studio-production.up.railway.app/paygate/merchant)
- **Live Telegram VIP Bot Demo:** [@ChainGuardVaultBot (`?start=vip`)](https://t.me/ChainGuardVaultBot?start=vip)
- **Live Dedicated MCP Server Endpoint:** `POST https://chainguard-studio-production.up.railway.app/paygate/mcp` (`create_usdt_invoice`, `get_usdt_invoice`, `list_usdt_invoices`, `verify_usdt_invoice`, `expire_usdt_invoice`, `check_usdt_subscription`, `verify_onchain_transfer`, `build_x402_paywall_challenge`)
- **Sister 15-Tool Web3 Security & CI Gate (`EzequielLech/chainguard-mcp`):** [https://github.com/EzequielLech/chainguard-mcp](https://github.com/EzequielLech/chainguard-mcp)

---

## Why Builders Choose PayGate USDT Over Custodial Processors

| Feature | PayGate USDT v2.6 | Custodial Gateways / Whop / InviteMember |
|---|---|---|
| **Settlement Destination** | **100% direct buyer -> your wallet** (Phantom, MetaMask, Ledger, Safe, Exchange) | Held by intermediary; payout delays / freezes |
| **Transaction Fees** | **$0% cut** on settlements (self-hosted MIT) | 1%–10% + withdrawal minimums |
| **SDK Runtimes** | **Python + Zero-Dependency TypeScript/Node.js (`sdk-js/`)** | Often single-language or heavy SDKs |
| **Networks Supported** | **Polygon, BSC (BEP-20), Base, Arbitrum, Ethereum, Solana SPL** | Limited or custodian-locked |
| **Hosted Invoice Pages + Webhooks** | Included (`/paygate/checkout/{id}` + `X-PayGate-Signature` `HMAC-SHA256`) | Often requires merchant KYB/KYC |
| **Non-Custodial SaaS Subscriptions** | Included (`customer_ref` + `billing_days` + `/api/v1/paygate/subscriptions/check`) | Requires cards or infinite token approvals |
| **HTTP 402 (`x402 v2`) Paywalls** | Included (FastAPI `@require_usdt_payment` + Express `paygateExpressMiddleware`) | Rare / separate SDK |
| **Telegram Paid VIP Automation** | Included (60-second bot onboarding, 1-time invite links, auto-kick on expiry) | High % revenue share |
| **MCP Server for AI Agents** | Included (Streamable HTTP `2025-03-26`, `ping`, `resources`, `prompts` + Stdio MCP) | Not supported |
| **Security & Reproducibility** | **100% auditable Python & ESM Node.js**, full `pytest` + `node --test` + CI | Black-box binaries / closed SaaS |

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

### 0. Monetize Any Python MCP Tool in 1 Line (`@mcp_tool_paywall`)

Wrap any `FastMCP` / `mcp` SDK tool or Python function with `@mcp_tool_paywall`. Callers get `free_calls` free invocations (with live `paygate_billing` telemetry), and automatically unlock unlimited access when they pay your non-custodial USDT/USDC invoice or recurring subscription:

```python
from paygate_sdk import mcp_tool_paywall

@mcp_tool_paywall(
    price_usd=9.0,
    merchant_wallet="0xYourEvmOrSolanaWalletAddress",
    network="base",       # base | polygon | arbitrum | bsc | ethereum | solana
    free_calls=3,         # free trial invocations per customer_ref
    billing_days=30,      # recurring 30-day subscription entitlement
    title="Pro AI Agent MCP Pass",
)
def analyze_market_alpha(symbol: str, customer_ref: str = "", paygate_invoice_id: str = "") -> dict:
    return {"symbol": symbol, "verdict": "STRONG_BUY"}
```

### 0b. TypeScript / Node.js Quickstart (`sdk-js/`, Zero npm Dependencies)

Monetize any TypeScript `@modelcontextprotocol/sdk` MCP tool or Express / Next.js API route in 1 wrapper with native Node.js 18+ `fetch` and `node:crypto`:

```ts
import {
  withMcpToolPaywall,
  paygateExpressMiddleware,
  verifyWebhookSignature,
} from "./sdk-js/index.mjs";

// 1. Wrap any TypeScript MCP tool with 3 free calls + $9/30d non-custodial unlock
export const proSignalTool = withMcpToolPaywall(
  {
    priceUsd: 9.0,
    merchantWallet: "0xYourEvmOrSolanaWalletAddress",
    network: "base",
    freeCalls: 3,
    billingDays: 30,
    title: "Pro MCP Tool Pass",
  },
  async ({ symbol }) => ({ symbol, verdict: "STRONG_BUY" })
);

// 2. Protect any Express / Connect HTTP route with x402 v2 + X-PayGate-Invoice
app.get(
  "/api/pro-signal",
  paygateExpressMiddleware({
    priceUsd: 5.0,
    merchantWallet: "0xYourEvmWalletAddress",
    network: "base",
    description: "Institutional Trading Signal",
  }),
  (req, res) => res.json({ ok: true, signal: "ETH/USDC", paygate: req.paygate })
);
```

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

### 6. Non-Custodial Recurring SaaS Subscriptions (No Infinite Approvals)

Accept recurring 30-day SaaS/API memberships directly in USDT/USDC without requesting `MAX_UINT256` token allowances. Renewals paid early automatically stack remaining days:

```python
from paygate_sdk import PayGateClient

client = PayGateClient("https://chainguard-studio-production.up.railway.app")

# 1. Check active entitlement & get a 1-Click pre-filled renewal link
sub = client.check_subscription(
    merchant_wallet="0xYourEvmWalletAddress",
    customer_ref="user_42",
    amount_usd=19.0,
    network="base",
    billing_days=30,
)
if not sub["active"]:
    print("Renew here:", sub["renewal_url"])
```

- [`examples/fastapi_x402_demo.py`](examples/fastapi_x402_demo.py) — FastAPI paywall route protected by `@require_usdt_payment`
- [`examples/webhook_receiver_demo.py`](examples/webhook_receiver_demo.py) — FastAPI receiver verifying `X-PayGate-Signature` (`HMAC-SHA256`)

```bash
docker compose up --build
```

---

## Run Tests Locally

```bash
git clone https://github.com/EzequielLech/paygate-usdt.git
cd paygate-usdt
pip install -r requirements.txt
pytest -v
node --test sdk-js/test.mjs
```

Zero external API keys or private wallet keys are required to run the test suite.

---

## Hosted Cloud, 1-Click Passes & Done-For-You B2B Deployment

Want this running for your business without touching servers, RPC failovers, or webhook glue? Click any instant non-custodial checkout badge below (supports **1-Click MetaMask / Rabby / Phantom Browser Pay** + automatic SHA-256 Receipt):

| Option | Price | Instant 1-Click Checkout / Action |
|---|---|---|
| **Open-Source Self-Hosted (`MIT`)** | **$0** | Clone this repo, run with FastAPI/uvicorn + `.env`, 0% fees |
| **24-Hour Impulse Micro-Unlock Pass (50 Calls)** | **$0.50 USDC/USDT** | [Pay $0.50 Base/Polygon USDC/USDT — Instant `cg_live_` Key](https://chainguard-studio-production.up.railway.app/paygate/pay?merchant=0x4Ed4D0750453C027FA8398067d5af980Bcc9B6eD&amount=0.5&network=base&title=ChainGuard+24h+Micro+Unlock+Pass&issue_pass=1&billing_days=1) |
| **7-Day Starter MCP & Pro API Pass (250 Calls)** | **$2 USDC/USDT** | [Pay $2 Base/Polygon USDC/USDT — Auto-Mints `cg_live_` Key](https://chainguard-studio-production.up.railway.app/paygate/pay?merchant=0x4Ed4D0750453C027FA8398067d5af980Bcc9B6eD&amount=2&network=base&title=ChainGuard+7-Day+Starter+MCP+Pass&issue_pass=1&billing_days=7) |
| **30-Day Unlimited MCP & Pro API Pass** | **$9 USDC/USDT** | [Pay $9 Base USDC/USDT — Auto-Mints `cg_live_` Key](https://chainguard-studio-production.up.railway.app/paygate/pay?merchant=0x4Ed4D0750453C027FA8398067d5af980Bcc9B6eD&amount=9&network=base&title=30-Day+Unlimited+MCP+%26+Pro+API+Pass&issue_pass=1&billing_days=30) |
| **Managed Cloud VIP Bot** | **$29 USDT/mo** (7-day free trial) | [Launch Bot Free in 60s](https://chainguard-studio-production.up.railway.app/paygate#setup) |
| **Lifetime White-Label Bot License** | **$79 USDT** one-time | [Pay $79 Polygon/Base USDT/USDC — Instant Master License Key](https://chainguard-studio-production.up.railway.app/paygate/pay?merchant=0x4Ed4D0750453C027FA8398067d5af980Bcc9B6eD&amount=79&network=polygon&title=PayGate+Lifetime+White-Label+License&issue_pass=1&billing_days=3650) |
| **Turnkey B2B VIP Gate Deployment (24h)** | **$199 USDT** (`$99` deposit) | [Pay $99 Kickoff Deposit (Polygon)](https://chainguard-studio-production.up.railway.app/paygate/pay?merchant=0x4Ed4D0750453C027FA8398067d5af980Bcc9B6eD&amount=99&network=polygon&title=Turnkey+VIP+Gate+Deployment+50pct+Deposit&issue_pass=1) · [Book B2B Scope](https://chainguard-studio-production.up.railway.app/paygate#b2b) |
| **Custom Crypto Bot + API / Exchange Automation** | **$499 USDT** (`$249` deposit) | [Pay $249 Kickoff Deposit (Polygon)](https://chainguard-studio-production.up.railway.app/paygate/pay?merchant=0x4Ed4D0750453C027FA8398067d5af980Bcc9B6eD&amount=249&network=polygon&title=Custom+Bot+%2B+Exchange+API+50pct+Deposit&issue_pass=1) · [Submit GitHub Inquiry](https://github.com/EzequielLech/paygate-usdt/issues/new?template=b2b_custom_deployment.yml) |

- **Book Directly with On-Chain Checkout:** [https://chainguard-studio-production.up.railway.app/paygate#b2b](https://chainguard-studio-production.up.railway.app/paygate#b2b)
- **Self-Serve Merchant Dashboard:** [https://chainguard-studio-production.up.railway.app/paygate/merchant](https://chainguard-studio-production.up.railway.app/paygate/merchant)
- **Submit a Custom Scope via GitHub Issues:** [Open a B2B Custom Integration Request](https://github.com/EzequielLech/paygate-usdt/issues/new?template=b2b_custom_deployment.yml)
