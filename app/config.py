"""
Sanitized configuration for PayGate USDT Open-Source Non-Custodial Gateway.
Zero custody: uses ONLY public receiving wallet addresses configured via environment variables.
"""
from __future__ import annotations

import os


class StudioConfig:
    APP_NAME: str = "PayGate USDT Gateway"
    VERSION: str = "2.0.0"

    # Configure your own public receiving wallets via environment variables (NEVER private keys)
    PHANTOM_EVM_ADDRESS: str = os.getenv(
        "PAYGATE_EVM_ADDRESS",
        os.getenv("PHANTOM_EVM_ADDRESS", "0x0000000000000000000000000000000000000000"),
    )
    PHANTOM_SOL_ADDRESS: str = os.getenv(
        "PAYGATE_SOL_ADDRESS",
        os.getenv("PHANTOM_SOL_ADDRESS", "11111111111111111111111111111111"),
    )

    # Public JSON-RPC endpoints for direct on-chain verification (no paid indexer key required)
    EVM_RPCS: dict[str, str] = {
        "ethereum": os.getenv("ETHEREUM_RPC_URL", "https://ethereum-rpc.publicnode.com"),
        "base": os.getenv("BASE_RPC_URL", "https://mainnet.base.org"),
        "arbitrum": os.getenv("ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc"),
        "polygon": os.getenv("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com"),
        "bsc": os.getenv("BSC_RPC_URL", "https://bsc-dataseed.binance.org"),
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

    # Official USDC / USDT ERC-20 contracts (6 decimals on ETH/Base/Arbitrum/Polygon, 18 on BSC)
    EVM_STABLECOINS: dict[str, dict[str, tuple[str, int]]] = {
        "ethereum": {
            "USDC": ("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 6),
            "USDT": ("0xdac17f958d2ee523a2206206994597c13d831ec7", 6),
        },
        "base": {
            "USDC": ("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", 6),
            "USDT": ("0xfde4c96c8593536e31f229ea8f37b2ada2699bb2", 6),
            "USDbC": ("0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca", 6),
        },
        "arbitrum": {
            "USDC": ("0xaf88d065e77c8cc2239327c5edb3a432268e5831", 6),
            "USDT": ("0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9", 6),
        },
        "polygon": {
            "USDC": ("0x3c499c542cef5e3811e1192ce70d8cc03d5c3359", 6),
            "USDT": ("0xc2132d05d31c914a87c6611c10748aeb04b58e8f", 6),
        },
        "bsc": {
            "USDC": ("0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d", 18),
            "USDT": ("0x55d398326f99059ff775485246999027b3197955", 18),
        },
    }

    # Solana SPL stablecoin mints (6 decimals)
    SOLANA_STABLECOINS: dict[str, tuple[str, int]] = {
        "USDC": ("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 6),
        "USDT": ("Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", 6),
    }

    DB_PATH: str = os.getenv(
        "STUDIO_DB_PATH",
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "studio_data.db"),
    )
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
