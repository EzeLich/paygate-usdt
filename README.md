# PayGate USDT — Zero-Custody Telegram VIP & On-Chain Checkout

Live deployment: https://chainguard-studio-production.up.railway.app/paygate  
Interactive Telegram demo: https://t.me/ChainGuardVaultBot?start=vip

## What It Does
- **Zero-Custody On-Chain Verification:** Directly verifies ERC-20 `Transfer` logs (`eth_getTransactionReceipt`) on **Polygon, BNB Smart Chain, Arbitrum, Base, Ethereum** and SPL token balance deltas (`getTransaction`) on **Solana** via public JSON-RPCs.
- **Multi-Tenant Telegram VIP Gatekeeper:** Creators connect a `@BotFather` token + destination wallet; members pay USDC/USDT directly to the creator's wallet, receive a single-use private invite link (`member_limit=1`), and are automatically rotated/removed when expired.
- **Anti-Replay Protection:** Persists verified `(bot_id, tx_hash)` records in SQLite.

## Stack
- Python 3.10+, FastAPI, SQLite, Telegram Bot HTTP API, EVM & Solana JSON-RPC.
