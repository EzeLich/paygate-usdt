---
name: paygate-crypto-checkout
description: Create non-custodial 7-chain crypto checkout invoices (USDT, USDC, ETH, BNB, POL, SOL, BTC), verify on-chain transactions, check recurring subscriptions, and add x402 or MCP paywalls with 0% platform custody.
---

# PayGate Non-Custodial Crypto Checkout & x402 Paywall Skill

Use this skill whenever the user wants to accept crypto or stablecoin payments (`USDT`, `USDC`, `ETH`, `BNB`, `POL`, `SOL`, `BTC`) without Stripe, KYC, or custodial intermediaries, add HTTP `402 Payment Required` (`x402 v2`) paywalls to FastAPI or Express/Node.js APIs, monetize MCP tools, or gate a Telegram VIP community.

## Instructions

1. **Instant Stateless Checkout URL (Zero Code)**
   Construct a direct 7-chain checkout URL settling 100% to the merchant's wallet:
   ```text
   https://chainguard-studio-production.up.railway.app/paygate/pay?merchant=0xYourWallet&amount=29&network=base&title=Pro+Plan
   ```

2. **Remote MCP Server for Autonomous Agent Invoicing**
   ```json
   {
     "mcpServers": {
       "paygate-usdt": {
         "url": "https://chainguard-studio-production.up.railway.app/paygate/mcp"
       }
     }
   }
   ```
   Provides `create_usdt_invoice`, `get_usdt_invoice`, `list_usdt_invoices`, `verify_usdt_invoice`, `expire_usdt_invoice`, `check_usdt_subscription`, `verify_onchain_transfer`, and `build_x402_paywall_challenge`.

3. **Python & TypeScript SDK Decorators**
   - Python MCP paywall: `@mcp_tool_paywall(price_usd=9.0, merchant_wallet="0x...", network="base", free_calls=3, billing_days=30)`
   - FastAPI HTTP 402 route guard: `@require_usdt_payment(price_usd=0.05, merchant_wallet="0x...", network="base")`
   - Express / Node.js middleware: `paygateExpressMiddleware({ priceUsd: 0.05, merchantWallet: "0x...", network: "base" })`

## Examples

- **Input:** Create a $49 non-custodial USDC invoice on Base with an HMAC-SHA256 webhook callback.
  **Action:** `POST https://chainguard-studio-production.up.railway.app/api/v1/paygate/invoices` with `{"title": "Pro License", "amount_usd": 49, "network": "base", "merchant_wallet": "0x...", "webhook_url": "https://..."}` or call `create_usdt_invoice`.
- **Input:** Put a $9/month crypto paywall with 3 free trial calls in front of my Python MCP tool.
  **Action:** Import `mcp_tool_paywall` from `paygate_sdk` and decorate the tool function.
