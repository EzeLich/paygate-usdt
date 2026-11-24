# Security Policy

## Non-Custodial Architecture
Neither `paygate-usdt` nor `chainguard-mcp` ever requests, stores, or processes private keys, seed phrases, or custodial funds. All on-chain settlements occur directly between the buyer's wallet and the merchant's self-custody wallet via verified ERC-20 (`USDT`/`USDC`) and Solana SPL token contracts.

## Supported Versions

| Version | Supported |
| ------- | --------- |
| `2.4.x` / `1.1.x` | ✅ Active |
| `< 2.0` | ❌ Upgrade recommended |

## Reporting a Vulnerability
If you discover a security issue in our smart-contract parsers, JSON-RPC receipt verifiers, HMAC-SHA256 webhook verification, or MCP endpoints:
- Email: `ezequiellich44@gmail.com`
- Canonical policy: `https://chainguard-studio-production.up.railway.app/.well-known/security.txt`
