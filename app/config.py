"""
Configuration for ChainGuard & TokenDiet Autonomous Micro-SaaS.
Zero custody: uses ONLY public Phantom receiving addresses.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PricingTier:
    tier_id: str
    name: str
    price_usd: float
    description: str
    features: tuple[str, ...]


class StudioConfig:
    APP_NAME: str = "ChainGuard & TokenDiet Studio"
    VERSION: str = "1.0.0"

    # Public Phantom receiving wallets (NEVER private keys)
    PHANTOM_EVM_ADDRESS: str = os.getenv(
        "PHANTOM_EVM_ADDRESS",
        "0x4Ed4D0750453C027FA8398067d5af980Bcc9B6eD",
    )
    PHANTOM_SOL_ADDRESS: str = os.getenv(
        "PHANTOM_SOL_ADDRESS",
        "3fZSMAyCEMhZwWiynbJDjoYNUT97aiV9BLzoUNroEMAz",
    )
    PHANTOM_BTC_ADDRESS: str = os.getenv(
        "PHANTOM_BTC_ADDRESS",
        "bc1p980u6y2m768yrhcy5u3llas49w2wpsnselp7sqetn43vnetzqkdqypjcgk",
    )

    # Public RPC endpoints for real on-chain verification (no API key required)
    EVM_RPCS: dict[str, str] = {
        "ethereum": os.getenv("ETHEREUM_RPC_URL", "https://ethereum-rpc.publicnode.com"),
        "base": os.getenv("BASE_RPC_URL", "https://base-rpc.publicnode.com"),
        "arbitrum": os.getenv("ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc"),
        "polygon": os.getenv("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com"),
        "bsc": os.getenv("BSC_RPC_URL", "https://bsc-rpc.publicnode.com"),
    }
    EVM_RPC_FALLBACKS: dict[str, tuple[str, ...]] = {
        "ethereum": (
            "https://ethereum-rpc.publicnode.com",
            "https://rpc.ankr.com/eth",
        ),
        "base": (
            "https://base-rpc.publicnode.com",
            "https://mainnet.base.org",
            "https://base.llamarpc.com",
        ),
        "arbitrum": (
            "https://arb1.arbitrum.io/rpc",
            "https://arbitrum.llamarpc.com",
        ),
        "polygon": (
            "https://polygon-bor-rpc.publicnode.com",
            "https://polygon-rpc.com",
        ),
        "bsc": (
            "https://bsc-rpc.publicnode.com",
            "https://bsc-dataseed.binance.org",
            "https://bsc-dataseed1.defibit.io",
        ),
    }
    EVM_CHAIN_IDS: dict[str, int] = {
        "ethereum": 1,
        "base": 8453,
        "arbitrum": 42161,
        "polygon": 137,
        "bsc": 56,
    }
    SOLANA_RPC_URL: str = os.getenv(
        "SOLANA_RPC_URL",
        "https://api.mainnet-beta.solana.com",
    )

    # Official USDC / USDT ERC-20 contracts (EIP-55 checksummed; 6 decimals, or 18 on BSC)
    EVM_STABLECOINS: dict[str, dict[str, tuple[str, int]]] = {
        "ethereum": {
            "USDC": ("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6),
            "USDT": ("0xdAC17F958D2ee523a2206206994597C13D831ec7", 6),
        },
        "base": {
            "USDC": ("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6),
            "USDT": ("0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2", 6),
            "USDbC": ("0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA", 6),
        },
        "arbitrum": {
            "USDC": ("0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6),
            "USDT": ("0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", 6),
        },
        "polygon": {
            "USDC": ("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6),
            "USDT": ("0xc2132D05D31c914a87C6611C10748AEb04B58e8F", 6),
        },
        "bsc": {
            "USDC": ("0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", 18),
            "USDT": ("0x55d398326f99059fF775485246999027B3197955", 18),
        },
    }

    # Solana SPL stablecoin mints (6 decimals)
    SOLANA_STABLECOINS: dict[str, tuple[str, int]] = {
        "USDC": ("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 6),
        "USDT": ("Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", 6),
    }

    # Pricing tiers designed to convert micro-usage into SaaS recurring revenue
    TIERS: dict[str, PricingTier] = {
        "batch_scan": PricingTier(
            tier_id="batch_scan",
            name="Batch Portfolio & Token Shield (Up to 25 Targets)",
            price_usd=5.0,
            description="Scan up to 25 EVM wallets (EIP-7702), ERC-20 tokens, or Solana Token-2022 mints in 1 call.",
            features=(
                "Up to 25 addresses/tokens/mints per batch",
                "Consolidated risk summary + export JSON",
                "Zero rate limits",
            ),
        ),
        "pro_pass": PricingTier(
            tier_id="pro_pass",
            name="30-Day Pro API & Unlimited MCP Pass",
            price_usd=9.0,
            description="Instant cg_live_ API key unlocking unlimited /mcp calls + 1,000 REST firewall scans for 30 days.",
            features=(
                "Instant cg_live_ API Key on payment",
                "Unlimited Cursor/Claude /mcp tool calls (bypasses hourly free cap)",
                "1,000 REST calls across EIP-7702, Token-2022, GoPlus & Tx Firewall",
            ),
        ),
        "watchdog_30d": PricingTier(
            tier_id="watchdog_30d",
            name="24/7 Wallet & MCP Rug-Pull Watchdog (30 Days)",
            price_usd=9.0,
            description="Continuous monitoring of any EVM wallet (EIP-7702 hijack alerts) or MCP URL (hash drift) via Webhook.",
            features=(
                "Automated checks every 5 minutes",
                "Instant JSON webhook alert (Discord/Slack/Telegram compatible)",
                "Covers EIP-7702 delegation changes & MCP rug-pull drift",
            ),
        ),
        "frontend_guard_30d": PricingTier(
            tier_id="frontend_guard_30d",
            name="DeFi Frontend Hijack & EIP-1967 Proxy Guard (30 Days)",
            price_usd=29.0,
            description="5-minute automated monitoring of DeFi frontend JS bundles, SRI hashes, embedded contract addresses, and EIP-1967 proxy upgrade slots via webhook.",
            features=(
                "Checks external <script> hashes & embedded EVM addresses every 5 minutes",
                "Monitors EIP-1967 proxy implementation & admin slots on Base/Ethereum/Arbitrum/Polygon",
                "Instant Discord/Slack/Telegram webhook alert on unauthorized frontend/proxy drift",
            ),
        ),
        "frontend_guard_90d": PricingTier(
            tier_id="frontend_guard_90d",
            name="DeFi Frontend Hijack & EIP-1967 Proxy Guard (90 Days)",
            price_usd=79.0,
            description="Quarterly 24/7 frontend hijack, script/address drift, and EIP-1967 proxy upgrade monitoring via webhook.",
            features=(
                "90 days of continuous 5-minute frontend & proxy integrity checks",
                "Covers JS supply-chain injections, SRI tampering, and EIP-1967 slot swaps",
                "Instant Discord/Slack/Telegram JSON alerts",
            ),
        ),
        "pro_audit": PricingTier(
            tier_id="pro_audit",
            name="Pro Deep Audit + Public SHA-256 Certificate",
            price_usd=19.0,
            description="Full line-by-line vulnerability report, public /certificate/{id} trust badge & 100 API calls.",
            features=(
                "All Critical/High/Medium/Low findings unlocked",
                "Public shareable /certificate/{id} trust page",
                "Exact remediation code patches + 100 API calls",
            ),
        ),
        "fix_kit": PricingTier(
            tier_id="fix_kit",
            name="Full Fix PR + Hardened Kit + 500 API Calls",
            price_usd=49.0,
            description="Complete patched Solidity/MCP files, regression tests, public certificate & 500 API calls.",
            features=(
                "Everything in Pro Deep Audit + Public Certificate",
                "Auto-generated hardened source/config & regression tests",
                "cg_live_ API Key with 500 calls included",
            ),
        ),
        "agency_bundle": PricingTier(
            tier_id="agency_bundle",
            name="Agency Security Bundle (3 Audits + 5 Watchdogs + API Pass)",
            price_usd=100.0,
            description="Instant $100 USDT goal-tier agency pack: 3 public certificates, 5 watchdogs & 5,000 API calls.",
            features=(
                "5,000 API calls + unlimited /mcp for 30 days",
                "5 active 24/7 Webhook Watchdogs included",
                "Up to 3 public SHA-256 Trust Certificates",
            ),
        ),
    }

    DB_PATH: str = os.getenv(
        "STUDIO_DB_PATH",
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "studio_data.db"),
    )
    BASE_BUILDER_CODE: str = os.getenv("BASE_BUILDER_CODE", "bc_j2clt9ey")
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    WHOP_API_KEY: str = os.getenv("WHOP_API_KEY", "")
    WHOP_APP_ID: str = os.getenv("WHOP_APP_ID", "")
    WHOP_COMPANY_ID: str = os.getenv("WHOP_COMPANY_ID", "")
    WHOP_WEBHOOK_SECRET: str = os.getenv("WHOP_WEBHOOK_SECRET", "")
    DAILY_GOAL_USDT: float = 100.0
