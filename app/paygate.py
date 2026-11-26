"""
PayGate USDT — Multi-Tenant Telegram VIP & Digital Product Checkout Engine.
Collects USDT/USDC directly to creator self-custody wallets (Polygon, BSC, Arbitrum, Base, Solana)
without banks, Stripe, Whop, or monotributo, and auto-manages 1-use invites + expiry kicks.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import StudioConfig
from .ledger import StudioLedger
from .onchain_verifier import NATIVE_ASSET_SPECS, OnChainPaymentVerifier, fetch_native_usd_price

FREE_CLOUD_SETTLEMENTS_LIMIT = 3

SUPPORTED_NETWORKS: dict[str, dict[str, str]] = {
    "polygon": {"label": "Polygon (USDT / USDC / POL — ~$0.01 fee)", "tokens": "USDT, USDC, or POL"},
    "bsc": {"label": "BNB Smart Chain BEP20 (USDT / USDC / BNB)", "tokens": "USDT, USDC, or BNB"},
    "arbitrum": {"label": "Arbitrum One (USDT / USDC / ETH)", "tokens": "USDT, USDC, or ETH"},
    "base": {"label": "Base (USDC / USDT / ETH)", "tokens": "USDC, USDT, or ETH"},
    "solana": {"label": "Solana (USDC / USDT / SOL — Phantom)", "tokens": "USDC, USDT, or SOL"},
    "ethereum": {"label": "Ethereum Mainnet (USDC / USDT / ETH)", "tokens": "USDC, USDT, or ETH"},
    "bitcoin": {"label": "Bitcoin (BTC — Taproot / Bech32)", "tokens": "BTC"},
}

PAYGATE_B2B_PACKAGES: dict[str, dict[str, Any]] = {
    "turnkey_199": {
        "package_id": "turnkey_199",
        "name_en": "Turnkey VIP Gate Deployment (24h)",
        "name_es": "Despliegue Llave en Mano VIP Gate (24h)",
        "full_price_usd": 199.0,
        "min_deposit_usd": 99.0,
        "delivery": "24 hours",
        "summary_en": "We create & configure your branded Telegram payment bot, multi-chain USDT/USDC wallet rules, private channel auto-kick permissions, and live test payment end-to-end.",
        "summary_es": "Creamos y configuramos tu bot Telegram con tu marca, cobro USDT/USDC multi-red, permisos de invitación/expulsión en tu canal y prueba en vivo punta a punta.",
    },
    "custom_499": {
        "package_id": "custom_499",
        "name_en": "Custom Crypto Checkout + Bot / Exchange Automation",
        "name_es": "Bot Cripto a Medida + Automatización API / Exchange",
        "full_price_usd": 499.0,
        "min_deposit_usd": 249.0,
        "delivery": "3-5 days",
        "summary_en": "Bespoke multi-chain USDT/USDC checkout + custom Python backend: webhook CRM/SaaS fulfillment, Discord/Telegram roles, or Binance/Bitget/BingX signal/alert automation.",
        "summary_es": "Checkout USDT/USDC multi-red + backend Python a medida: integración webhook con tu SaaS/CRM, roles Discord/Telegram o automatización Binance/Bitget/BingX.",
    },
}

EVM_TX_RE = re.compile(r"\b0x[a-fA-F0-9]{64}\b")
SOL_TX_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{64,92}\b")
EVM_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
SOL_ADDR_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
BTC_ADDR_RE = re.compile(r"^(bc1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{25,90}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})$")


def tg_call(bot_token: str, method: str, payload: dict[str, Any] | None = None) -> tuple[bool, Any]:
    tok = (bot_token or "").strip()
    if not tok:
        return False, "Bot token vacio."
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{tok}/{method}",
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "PayGateUSDT/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("ok"):
            return True, data.get("result")
        return False, str(data.get("description") or "Telegram API error")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        try:
            desc = json.loads(body).get("description") or body
        except Exception:
            desc = body
        return False, f"HTTP {exc.code}: {desc}"
    except Exception as exc:
        return False, str(exc)


def validate_wallet_for_network(network: str, wallet: str) -> tuple[bool, str]:
    net = network.strip().lower()
    w = wallet.strip()
    if net not in SUPPORTED_NETWORKS:
        return False, f"Unsupported network '{net}'. Use polygon, bsc, arbitrum, base, ethereum, solana, or bitcoin."
    if net == "solana":
        if not SOL_ADDR_RE.match(w):
            return False, "Solana wallet must be a valid base58 public address."
        return True, ""
    if net == "bitcoin":
        if not BTC_ADDR_RE.match(w):
            return False, "Bitcoin wallet must be a valid Bech32/Taproot (bc1...) or legacy address."
        return True, ""
    if not EVM_ADDR_RE.match(w):
        return False, f"For {net.upper()} enter a valid EVM 0x... wallet address."
    return True, ""


def _is_studio_wallet(wallet: str) -> bool:
    w = (wallet or "").strip()
    if not w:
        return False
    return (
        w.lower() == (StudioConfig.PHANTOM_EVM_ADDRESS or "").strip().lower()
        or w == (StudioConfig.PHANTOM_SOL_ADDRESS or "").strip()
        or w == (StudioConfig.PHANTOM_BTC_ADDRESS or "").strip()
    )


def check_cloud_merchant_invoice_quota(
    ledger: StudioLedger,
    merchant_wallet: str,
    base_url: str,
) -> dict[str, Any]:
    """Allow 3 free settled sales per merchant wallet on hosted cloud, then require Pro ($9/mo) or Lifetime ($79)."""
    wal = (merchant_wallet or "").strip()
    if _is_studio_wallet(wal):
        return {"allowed": True, "tier": "studio", "paid_count": 0, "limit": None}
    paid_count = ledger.count_paid_invoices_for_merchant(wal) if hasattr(ledger, "count_paid_invoices_for_merchant") else 0
    licensed = ledger.has_active_merchant_license(wal) if hasattr(ledger, "has_active_merchant_license") else False
    if licensed:
        return {"allowed": True, "tier": "pro_licensed", "paid_count": paid_count, "limit": None}
    s_evm = StudioConfig.PHANTOM_EVM_ADDRESS or "0x4Ed4D0750453C027FA8398067d5af980Bcc9B6eD"
    q_ref = urllib.parse.quote(wal)
    up9 = f"{base_url.rstrip('/')}/paygate/pay?merchant={s_evm}&amount=9&network=base&title=PayGate+Cloud+Pro+Unlimited&issue_pass=1&billing_days=30&customer_ref={q_ref}"
    up79 = f"{base_url.rstrip('/')}/paygate/pay?merchant={s_evm}&amount=79&network=polygon&title=PayGate+Lifetime+White-Label+License&issue_pass=1&billing_days=3650&customer_ref={q_ref}"
    if paid_count < FREE_CLOUD_SETTLEMENTS_LIMIT:
        return {
            "allowed": True,
            "tier": "free_cloud",
            "paid_count": paid_count,
            "limit": FREE_CLOUD_SETTLEMENTS_LIMIT,
            "upgrade_pro_9_url": up9,
            "upgrade_lifetime_79_url": up79,
        }
    return {
        "allowed": False,
        "tier": "free_limit_reached",
        "paid_count": paid_count,
        "limit": FREE_CLOUD_SETTLEMENTS_LIMIT,
        "upgrade_pro_9_url": up9,
        "upgrade_lifetime_79_url": up79,
        "error": (
            f"Hosted cloud free limit reached ({paid_count}/{FREE_CLOUD_SETTLEMENTS_LIMIT} settled sales for {wal}). "
            "Activate PayGate Cloud Pro ($9/mo) or Lifetime White-Label ($79) in USDT/USDC/ETH/BNB/BTC, or self-host MIT from https://github.com/EzequielLech/paygate-usdt."
        ),
    }


def _extract_tx_for_network(network: str, text: str) -> str:
    if network == "solana":
        m = SOL_TX_RE.search(text or "")
        return m.group(0) if m else ""
    m = EVM_TX_RE.search(text or "")
    return m.group(0) if m else ""


def _format_plan_buttons(plans: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    rows: list[list[dict[str, Any]]] = []
    for p in plans[:6]:
        pid = str(p.get("id") or "mensual")
        name = str(p.get("name") or "VIP")
        price = float(p.get("price_usd") or 25.0)
        price_txt = f"{price:g}"
        rows.append([{"text": f"💎 {name} — ${price_txt} USDT", "callback_data": f"pgplan:{pid}"}])
    return rows


def verify_and_fulfill_member(
    *,
    bot: dict[str, Any],
    tg_user_id: str,
    tg_username: str,
    plan_id: str,
    tx_hash: str,
    ledger: StudioLedger,
    verifier: OnChainPaymentVerifier,
) -> dict[str, Any]:
    plans = {str(p["id"]): p for p in (bot.get("plans") or [])}
    plan = plans.get(plan_id) or (bot.get("plans") or [{}])[0]
    if not plan:
        return {"ok": False, "error": "No hay planes configurados en este bot."}

    active_count = ledger.count_active_paygate_members(bot["bot_id"])
    if bot.get("license_tier") == "trial" and active_count >= 3:
        return {
            "ok": False,
            "error": (
                "Este bot alcanzo el limite de 3 miembros de la prueba gratuita. "
                "El dueno debe activar PayGate Pro ($29/mes) o Licencia ($79) desde su panel."
            ),
        }

    if ledger.is_paygate_tx_used(bot["bot_id"], tx_hash):
        return {"ok": False, "error": "Este Tx Hash ya fue utilizado anteriormente (anti-reuso)."}

    min_price = float(plan.get("price_usd") or 1.0)
    vr = verifier.verify(
        network=bot["payout_network"],
        tx_hash=tx_hash,
        min_amount_usd=min_price,
        expected_recipient=bot["payout_wallet"],
    )
    if not vr.verified:
        return {"ok": False, "error": vr.reason}

    invite_link = ""
    channel_id = str(bot.get("channel_id") or "").strip()
    if channel_id:
        ok_inv, inv_res = tg_call(
            bot["bot_token"],
            "createChatInviteLink",
            {
                "chat_id": channel_id,
                "name": f"PayGate {tg_username or tg_user_id}",
                "member_limit": 1,
            },
        )
        if ok_inv and isinstance(inv_res, dict):
            invite_link = str(inv_res.get("invite_link") or "")

    member = ledger.activate_paygate_member(
        bot_id=bot["bot_id"],
        tg_user_id=tg_user_id,
        tg_username=tg_username,
        plan_id=str(plan.get("id") or "mensual"),
        plan_name=str(plan.get("name") or "VIP"),
        amount_usd=vr.amount_usd,
        network=bot["payout_network"],
        tx_hash=tx_hash,
        invite_link=invite_link,
        days=int(plan.get("days") or 30),
    )
    ledger.record_event(
        "PayGateUSDT",
        "member_activated",
        f"bot={bot['bot_id']} user={tg_user_id} paid=${vr.amount_usd} {vr.token} on {bot['payout_network']}",
    )
    return {"ok": True, "member": member, "invite_link": invite_link, "verification": vr}


def handle_paygate_bot_update(
    *,
    bot: dict[str, Any],
    update: dict[str, Any],
    base_url: str,
    ledger: StudioLedger,
    verifier: OnChainPaymentVerifier,
) -> dict[str, Any]:
    tok = bot["bot_token"]
    net = bot["payout_network"]
    net_info = SUPPORTED_NETWORKS.get(net, {"label": net.upper(), "tokens": "USDT/USDC"})
    plans = bot.get("plans") or []
    plan_map = {str(p["id"]): p for p in plans}

    cb = update.get("callback_query")
    if isinstance(cb, dict):
        cb_id = str(cb.get("id") or "")
        data = str(cb.get("data") or "")
        from_u = cb.get("from") or {}
        uid = str(from_u.get("id") or "")
        msg_obj = cb.get("message") or {}
        chat_id = (msg_obj.get("chat") or {}).get("id") or uid
        tg_call(tok, "answerCallbackQuery", {"callback_query_id": cb_id})

        if data.startswith("pgplan:") and chat_id:
            pid = data.split(":", 1)[1]
            plan = plan_map.get(pid)
            if not plan:
                tg_call(tok, "sendMessage", {"chat_id": chat_id, "text": "Plan no encontrado. Escribi /start para ver los planes."})
                return {"ok": True, "unknown_plan": pid}
            ledger.set_paygate_user_pending(bot["bot_id"], uid, pid)
            price = float(plan.get("price_usd") or 25.0)
            msg = (
                f"💎 *{plan['name']}*\n"
                f"• Importe exacto: *${price:g} {net_info['tokens']}*\n"
                f"• Red: *{net_info['label']}*\n"
                f"• Vigencia: *{int(plan.get('days') or 30)} dias*\n\n"
                f"1️⃣ Envia `${price:g}` a esta wallet (100% directo al creador):\n"
                f"`{bot['payout_wallet']}`\n\n"
                f"2️⃣ Apenas confirmes la transferencia, **pega aqui el Tx Hash / firma de la transaccion** y nuestro bot valida el pago en segundos para entregarte tu acceso."
            )
            tg_call(tok, "sendMessage", {"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"})
            return {"ok": True, "selected_plan": pid}
        return {"ok": True, "cb": data}

    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    from_u = msg.get("from") or {}
    uid = str(from_u.get("id") or chat_id or "")
    uname = str(from_u.get("username") or from_u.get("first_name") or uid)
    text = str(msg.get("text") or "").strip()
    if not chat_id or not text:
        return {"ok": True, "ignored": True}

    tx_hash = _extract_tx_for_network(net, text)
    if tx_hash:
        pending_pid = ledger.get_paygate_user_pending(bot["bot_id"], uid) or (str(plans[0]["id"]) if plans else "mensual")
        tg_call(tok, "sendMessage", {"chat_id": chat_id, "text": "⏳ Verificando tu pago on-chain en tiempo real..."})
        res = verify_and_fulfill_member(
            bot=bot,
            tg_user_id=uid,
            tg_username=uname,
            plan_id=pending_pid,
            tx_hash=tx_hash,
            ledger=ledger,
            verifier=verifier,
        )
        if not res["ok"]:
            tg_call(
                tok,
                "sendMessage",
                {"chat_id": chat_id, "text": f"❌ No pudimos validar todavia:\n{res['error']}\n\nSi recien enviaste la tx, espera 5 segundos y volve a pegarla."},
            )
            return {"ok": False, "error": res["error"]}

        invite = res.get("invite_link") or ""
        deliv = str(bot.get("delivery_text") or "").strip()
        exp_date = str(res["member"]["expires_at"])[:10]
        body_lines = [
            "✅ *¡Pago confirmado on-chain!*",
            f"• Plan activo: *{res['member']['plan_name']}*",
            f"• Valido hasta: `{exp_date}`",
        ]
        buttons: list[list[dict[str, Any]]] = []
        if invite:
            body_lines.append("\n👇 Usa este enlace privado *de un solo uso* para entrar ahora:")
            buttons.append([{"text": "🔓 Entrar al Canal VIP", "url": invite}])
        if deliv:
            body_lines.append(f"\n📦 *Entrega / Instrucciones:*\n{deliv}")
        if not invite and not deliv:
            body_lines.append("\nTu suscripcion quedo registrada. Contacta al admin si necesitas enlace manual.")
        tg_call(
            tok,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": "\n".join(body_lines),
                "parse_mode": "Markdown",
                **({"reply_markup": {"inline_keyboard": buttons}} if buttons else {}),
            },
        )
        return {"ok": True, "activated_member": res["member"]["id"]}

    welcome = str(bot.get("welcome_text") or "").strip() or (
        f"👋 Bienvenido a *{bot['brand_name']}*\n\n"
        f"Elegi tu pase abajo para pagar directo con *{net_info['tokens']}* en *{net.upper()}* y recibir acceso automatico al instante:"
    )
    kb = _format_plan_buttons(plans)
    if bot.get("license_tier") == "trial":
        kb.append([{"text": "⚡ Creado con PayGate USDT (Sin banco)", "url": f"{base_url}/paygate"}])
    tg_call(
        tok,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": welcome,
            "parse_mode": "Markdown",
            "reply_markup": {"inline_keyboard": kb},
        },
    )
    return {"ok": True, "menu_sent": True}


def handle_showcase_vip_interaction(
    *,
    bot_token: str,
    chat_id: int | str,
    action: str,
    base_url: str,
) -> bool:
    """Interactive showcase inside @ChainGuardVaultBot so prospects experience PayGate USDT live."""
    evm_addr = StudioConfig.PHANTOM_EVM_ADDRESS
    sol_addr = StudioConfig.PHANTOM_SOL_ADDRESS
    if action == "menu":
        text = (
            "💎 *PayGate USDT — Cobrá tu Canal VIP o Productos en USDT (Sin Banco ni Whop)*\n\n"
            "• Tus miembros pagan directo a *TU propia wallet* (Polygon, BSC, Arbitrum, Base o Solana)\n"
            "• Verificacion on-chain en 5s + entrega automatica de link único + expulsion al vencer\n"
            "• *0% comision sobre tus ventas*\n\n"
            "Proba abajo la demo interactiva de como ve el pago tu cliente o activa tu bot en 60 segundos:"
        )
        kb = [
            [{"text": "⚡ Conectar mi Bot Gratis (hasta 3 VIP)", "url": f"{base_url}/paygate"}],
            [{"text": "👀 Simular experiencia de compra VIP", "callback_data": "pgdemo:sim"}],
            [{"text": "🚀 PayGate Pro ($29 USDT/mes)", "callback_data": "pgdemo:pro"}],
            [{"text": "👑 Licencia Completa ($79 USDT unico)", "callback_data": "pgdemo:life"}],
        ]
        return tg_call(bot_token, "sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "reply_markup": {"inline_keyboard": kb}})[0]

    if action == "sim":
        text = (
            "👀 *Vista previa (como lo ve tu miembro VIP):*\n\n"
            "1. Tu cliente entra a tu `@TuBot` y toca *Acceso VIP 30 Dias — $25 USDT*.\n"
            "2. El bot le muestra tu wallet de Polygon/BSC/Solana.\n"
            "3. El cliente envia $25 USDT desde Binance/Bitget/Phantom y pega el hash en el chat.\n"
            "4. Nuestro motor verifica en blockchain en ~5 seg, le entrega un link de invitacion unico (`1 uso`) y lo expulsa solo al dia 30 si no renueva.\n\n"
            "👇 Arma tu propio bot en vivo en menos de 1 minuto:"
        )
        kb = [
            [{"text": "🛠️ Crear mi Bot en /paygate", "url": f"{base_url}/paygate"}],
            [{"text": "⬅️ Volver al menu PayGate", "callback_data": "pgdemo:menu"}],
        ]
        return tg_call(bot_token, "sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "reply_markup": {"inline_keyboard": kb}})[0]

    is_life = action == "life"
    price = "79" if is_life else "29"
    label = "Licencia Completa (Pago unico)" if is_life else "PayGate Pro (30 dias)"
    text = (
        f"💳 *{label} — ${price} USDT / USDC*\n\n"
        f"1️⃣ Envia `${price}` USDT o USDC por **Polygon, BSC (BEP20), Arbitrum o Base** a:\n"
        f"`{evm_addr}`\n\n"
        f"*(O por **Solana** a: `{sol_addr}`)*\n\n"
        "2️⃣ Luego crea/abri tu panel en `/paygate` y pega tu Tx Hash en la caja de activacion para desbloquear miembros ilimitados al instante."
    )
    kb = [
        [{"text": "✅ Ir a /paygate a activar mi bot", "url": f"{base_url}/paygate"}],
        [{"text": "⬅️ Volver", "callback_data": "pgdemo:menu"}],
    ]
    return tg_call(bot_token, "sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "reply_markup": {"inline_keyboard": kb}})[0]


def sweep_expired_paygate_members(ledger: StudioLedger) -> dict[str, Any]:
    """Kick expired VIP members without permanent ban and send a renewal reminder."""
    now_iso = datetime.now(timezone.utc).isoformat()
    expired = ledger.list_expired_paygate_members(now_iso)
    processed = 0
    kicked = 0
    for m in expired:
        processed += 1
        bot = ledger.get_paygate_bot(m["bot_id"])
        if not bot:
            ledger.mark_paygate_member_kicked(m["id"])
            continue
        channel_id = str(bot.get("channel_id") or "").strip()
        if channel_id and str(m["tg_user_id"]).isdigit():
            uid_int = int(m["tg_user_id"])
            ok_ban, _ = tg_call(bot["bot_token"], "banChatMember", {"chat_id": channel_id, "user_id": uid_int})
            if ok_ban:
                tg_call(bot["bot_token"], "unbanChatMember", {"chat_id": channel_id, "user_id": uid_int, "only_if_banned": True})
                kicked += 1
            tg_call(
                bot["bot_token"],
                "sendMessage",
                {
                    "chat_id": uid_int,
                    "text": (
                        f"⏰ Tu pase *{m['plan_name']}* en *{bot['brand_name']}* vencio.\n"
                        "Escribi /start cuando quieras renovar tu acceso al instante en USDT."
                    ),
                    "parse_mode": "Markdown",
                },
            )
        ledger.mark_paygate_member_kicked(m["id"])
    return {"checked": processed, "kicked": kicked, "timestamp": now_iso}


def notify_owner_b2b_event(message: str) -> bool:
    bot_tok = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    owner_chat = (os.getenv("OWNER_TELEGRAM_CHAT_ID") or "").strip()
    if not bot_tok or not owner_chat:
        return False
    ok, _ = tg_call(bot_tok, "sendMessage", {"chat_id": owner_chat, "text": message[:3900]})
    return ok


def verify_paygate_b2b_deposit(
    *,
    order_id: str,
    network: str,
    tx_hash: str,
    ledger: StudioLedger,
    verifier: OnChainPaymentVerifier,
) -> dict[str, Any]:
    order = ledger.get_paygate_b2b_order(order_id)
    if not order:
        return {"ok": False, "error": "B2B order not found.", "status_code": 404}
    net = (network or "").strip().lower()
    if net not in SUPPORTED_NETWORKS:
        return {"ok": False, "error": f"Unsupported network: {network}", "status_code": 400}
    tx = (tx_hash or "").strip()
    if ledger.is_tx_used(tx):
        return {"ok": False, "error": "Transaction hash already redeemed.", "status_code": 402}
    res = verifier.verify(tx_hash=tx, network=net, min_amount_usd=float(order["min_deposit_usd"]))
    if not res.verified:
        return {"ok": False, "error": res.reason or "Transaction not verified.", "status_code": 402}
    ledger.record_payment(
        tx_hash=tx,
        network=res.network,
        token=res.token,
        amount_usd=float(res.amount_usd),
        recipient=res.recipient,
        tier_id=f"paygate_b2b:{order['package_id']}",
        verification_mode="onchain_rpc",
        raw_proof=res.raw_proof,
        sender=res.sender,
    )
    updated = ledger.mark_paygate_b2b_order_paid(
        order["order_id"],
        tx_hash=tx,
        network=res.network,
        paid_amount_usd=float(res.amount_usd),
    )
    status_lbl = updated["status"] if updated else order["status"]
    ledger.record_event(
        "PayGateB2B",
        status_lbl,
        f"{order['order_id']} ({order['package_id']}) ${res.amount_usd:.2f} {res.token} via {res.network}",
    )
    notify_owner_b2b_event(
        f"💰 PAYGATE B2B ORDER VERIFIED\nOrder: {order['order_id']}\nPackage: {order['package_name']}\nClient: {order['client_name']} ({order['contact_handle']})\nPaid: ${res.amount_usd:.2f} {res.token} on {res.network}\nTx: {tx}"
    )
    return {
        "ok": True,
        "order": updated,
        "payment": {"amount_usd": res.amount_usd, "token": res.token, "network": res.network},
    }


def sign_paygate_webhook(payload_bytes: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()


def dispatch_invoice_webhook(invoice: dict[str, Any]) -> bool:
    url = str(invoice.get("webhook_url") or "").strip()
    if not url.startswith(("https://", "http://")):
        return False
    event_payload = {
        "event": "invoice.paid",
        "invoice_id": invoice["invoice_id"],
        "status": "paid",
        "amount_usd": invoice["amount_usd"],
        "paid_amount_usd": invoice["paid_amount_usd"],
        "token": invoice["token"],
        "network": invoice["network"],
        "merchant_wallet": invoice["merchant_wallet"],
        "tx_hash": invoice["tx_hash"],
        "payer": invoice["payer"],
        "paid_at": invoice["paid_at"],
        "metadata": invoice.get("metadata", {}),
    }
    raw = json.dumps(event_payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "PayGateUSDT/2.0", "X-PayGate-Event": "invoice.paid"}
    sec = str(invoice.get("webhook_secret") or "").strip()
    if sec:
        headers["X-PayGate-Signature"] = f"sha256={sign_paygate_webhook(raw, sec)}"
    req = urllib.request.Request(url, data=raw, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8):
            return True
    except Exception:
        return False


def verify_and_settle_invoice(
    *,
    invoice_id: str,
    tx_hash: str,
    ledger: StudioLedger,
    verifier: OnChainPaymentVerifier,
    network_override: str = "",
) -> dict[str, Any]:
    inv = ledger.get_paygate_invoice(invoice_id)
    if not inv:
        return {"ok": False, "status_code": 404, "error": "Invoice not found."}
    if inv["status"] == "paid":
        return {"ok": True, "invoice": inv, "already_paid": True}
    tx = (tx_hash or "").strip()
    if ledger.is_tx_used(tx):
        return {"ok": False, "status_code": 402, "error": "Transaction hash already redeemed."}
    primary_net = (network_override or inv["network"]).strip().lower()
    res = verifier.verify(
        network=primary_net,
        tx_hash=tx,
        min_amount_usd=float(inv["amount_usd"]),
        expected_recipient=inv["merchant_wallet"],
    )
    # Multi-chain EVM fallback: if buyer sent USDT/USDC to the same 0x merchant_wallet on another supported EVM chain, accept it seamlessly
    if not res.verified and inv["merchant_wallet"].strip().startswith("0x") and tx.startswith("0x"):
        for candidate_net in ("base", "polygon", "arbitrum", "bsc", "ethereum"):
            if candidate_net == primary_net:
                continue
            alt_res = verifier.verify(
                network=candidate_net,
                tx_hash=tx,
                min_amount_usd=float(inv["amount_usd"]),
                expected_recipient=inv["merchant_wallet"],
            )
            if alt_res.verified:
                res = alt_res
                break
    if not res.verified:
        return {"ok": False, "status_code": 402, "error": res.reason or "Transaction verification failed."}
    ledger.record_payment(
        tx_hash=tx,
        network=res.network,
        token=res.token,
        amount_usd=float(res.amount_usd),
        recipient=res.recipient,
        tier_id=f"paygate_invoice:{inv['invoice_id']}",
        verification_mode="onchain_rpc",
        raw_proof=res.raw_proof,
        sender=res.sender,
    )
    updated = ledger.mark_paygate_invoice_paid(
        inv["invoice_id"],
        tx_hash=tx,
        paid_amount_usd=float(res.amount_usd),
        token=res.token,
        payer=res.sender,
        network=res.network,
    )
    if updated:
        meta = dict(updated.get("metadata") or {})
        is_studio_merchant = _is_studio_wallet(updated["merchant_wallet"])
        billing_days = int(meta.get("billing_days") or 0)
        customer_ref = str(meta.get("customer_ref") or "").strip()
        meta_changed = False
        if billing_days > 0:
            try:
                paid_dt = datetime.fromisoformat(str(updated.get("paid_at") or "").replace("Z", "+00:00"))
                if paid_dt.tzinfo is None:
                    paid_dt = paid_dt.replace(tzinfo=timezone.utc)
            except Exception:
                paid_dt = datetime.now(timezone.utc)
            anchor_dt = paid_dt
            if customer_ref and hasattr(ledger, "get_paygate_subscription_status"):
                prior = ledger.get_paygate_subscription_status(
                    merchant_wallet=updated["merchant_wallet"],
                    customer_ref=customer_ref,
                    exclude_invoice_id=updated["invoice_id"],
                )
                if prior.get("active") and prior.get("expires_at"):
                    try:
                        prior_exp = datetime.fromisoformat(str(prior["expires_at"]).replace("Z", "+00:00"))
                        if prior_exp.tzinfo is None:
                            prior_exp = prior_exp.replace(tzinfo=timezone.utc)
                        if prior_exp > anchor_dt:
                            anchor_dt = prior_exp
                    except Exception:
                        pass
            meta["billing_days"] = billing_days
            meta["subscription_expires_at"] = (anchor_dt + timedelta(days=billing_days)).isoformat()
            if customer_ref:
                meta["customer_ref"] = customer_ref
            meta_changed = True
        paid_val = float(updated["paid_amount_usd"])
        if is_studio_merchant and paid_val >= 0.45:
            if paid_val < 1.99:
                pass_calls = 50
                default_days = 1
                plan_tag = "paygate_micro_24h_pass"
            elif paid_val < 5.0:
                pass_calls = 250
                default_days = 7
                plan_tag = "paygate_starter_7d_pass"
            elif paid_val < 29.0:
                pass_calls = 1000
                default_days = 30
                plan_tag = "paygate_pro_30d_pass"
            elif paid_val < 79.0:
                pass_calls = 5000
                default_days = 90
                plan_tag = "paygate_hosted_pro_90d"
            else:
                pass_calls = 25000
                default_days = 3650
                plan_tag = "paygate_lifetime_whitelabel_license"
            pass_days = billing_days if billing_days > 0 else default_days
            pass_obj = ledger.issue_api_pass(
                plan_id=plan_tag,
                tx_hash=tx,
                calls=pass_calls,
                days=pass_days,
                owner_wallet=res.sender,
            )
            meta["issue_pass"] = True
            meta["api_pass"] = pass_obj
            meta_changed = True
        audit_id_meta = str(meta.get("audit_id") or "").strip()
        if is_studio_merchant and audit_id_meta and paid_val >= 18.9 and hasattr(ledger, "mark_audit_paid"):
            audit_tier = "agency_bundle" if paid_val >= 99.0 else ("fix_kit" if paid_val >= 48.0 else "pro_audit")
            ledger.mark_audit_paid(audit_id=audit_id_meta, tier=audit_tier, tx_hash=tx)
            meta["unlocked_audit_id"] = audit_id_meta
            meta_changed = True
        wd_spec = meta.get("watchdog_spec")
        if is_studio_merchant and isinstance(wd_spec, dict) and paid_val >= 8.9 and hasattr(ledger, "create_watchdog"):
            t_type = str(wd_spec.get("target_type") or "").strip()
            t_val = str(wd_spec.get("target_value") or "").strip()
            t_net = str(wd_spec.get("network") or "base").strip().lower()
            t_hook = str(wd_spec.get("webhook_url") or "").strip()
            if t_type and t_val and t_hook:
                wd_obj = ledger.create_watchdog(
                    target_type=t_type,
                    target_value=t_val,
                    network=t_net,
                    webhook_url=t_hook,
                    tx_hash=tx,
                    baseline_hash="",
                )
                meta["activated_watchdog"] = wd_obj
                meta_changed = True
        if meta_changed:
            updated = ledger.update_paygate_invoice_metadata(updated["invoice_id"], meta) or updated
        dispatch_invoice_webhook(updated)
    ledger.record_event(
        "PayGateInvoice",
        "invoice_paid",
        f"{inv['invoice_id']} ${res.amount_usd:.2f} {res.token} on {res.network}",
    )
    final_inv = updated or inv
    return {"ok": True, "invoice": final_inv, "receipt": build_invoice_receipt(final_inv)}


def auto_detect_and_settle_invoice(
    *,
    invoice_id: str,
    ledger: StudioLedger,
    verifier: OnChainPaymentVerifier,
) -> dict[str, Any]:
    """Scan recent Transfer logs across EVM chains (or mempool on Bitcoin) to settle without manual tx_hash entry."""
    inv = ledger.get_paygate_invoice(invoice_id)
    if not inv:
        return {"ok": False, "status_code": 404, "error": "Invoice not found."}
    if inv["status"] == "paid":
        return {"ok": True, "invoice": inv, "already_paid": True}
    wal = str(inv["merchant_wallet"] or "").strip()
    primary = str(inv["network"] or "polygon").strip().lower()
    if primary == "bitcoin" and hasattr(verifier, "find_recent_btc_transfers"):
        for m in verifier.find_recent_btc_transfers(expected_recipient=wal, min_amount_usd=float(inv["amount_usd"])):
            if not m.tx_hash or ledger.is_tx_used(m.tx_hash):
                continue
            settled = verify_and_settle_invoice(
                invoice_id=invoice_id,
                tx_hash=m.tx_hash,
                ledger=ledger,
                verifier=verifier,
                network_override="bitcoin",
            )
            if settled.get("ok"):
                return settled
        return {
            "ok": False,
            "status_code": 402,
            "error": f"No confirmed Bitcoin payment >= ${float(inv['amount_usd']):.2f} BTC to {wal[:12]}... detected yet. Paste TXID once confirmed.",
        }
    if primary == "solana" and hasattr(verifier, "find_recent_solana_transfers"):
        for m in verifier.find_recent_solana_transfers(expected_recipient=wal, min_amount_usd=float(inv["amount_usd"])):
            if not m.tx_hash or ledger.is_tx_used(m.tx_hash):
                continue
            settled = verify_and_settle_invoice(
                invoice_id=invoice_id,
                tx_hash=m.tx_hash,
                ledger=ledger,
                verifier=verifier,
                network_override="solana",
            )
            if settled.get("ok"):
                return settled
        return {
            "ok": False,
            "status_code": 402,
            "error": f"No confirmed Solana payment >= ${float(inv['amount_usd']):.2f} (SOL/USDC/USDT) to {wal[:10]}... detected yet. Wait ~3s or paste signature.",
        }
    if not wal.startswith("0x") or not hasattr(verifier, "find_recent_evm_transfers"):
        return {
            "ok": False,
            "status_code": 402,
            "error": "Paste your transaction signature in the box above to verify.",
        }
    chains = [primary] + [c for c in ("base", "polygon", "arbitrum", "bsc", "ethereum") if c != primary]
    for net in chains:
        matches = verifier.find_recent_evm_transfers(
            network=net,
            expected_recipient=wal,
            min_amount_usd=float(inv["amount_usd"]),
            lookback_blocks=900,
        )
        for m in matches:
            if not m.tx_hash or ledger.is_tx_used(m.tx_hash):
                continue
            settled = verify_and_settle_invoice(
                invoice_id=invoice_id,
                tx_hash=m.tx_hash,
                ledger=ledger,
                verifier=verifier,
                network_override=net,
            )
            if settled.get("ok"):
                return settled
    if hasattr(verifier, "find_recent_eth_native_transfers"):
        min_ts = 0.0
        try:
            c_dt = datetime.fromisoformat(str(inv.get("created_at") or "").replace("Z", "+00:00"))
            if c_dt.tzinfo is None:
                c_dt = c_dt.replace(tzinfo=timezone.utc)
            min_ts = c_dt.timestamp() - 600.0
        except Exception:
            min_ts = datetime.now(timezone.utc).timestamp() - 3600.0
        for m in verifier.find_recent_eth_native_transfers(
            expected_recipient=wal,
            min_amount_usd=float(inv["amount_usd"]),
            min_timestamp_unix=min_ts,
        ):
            if not m.tx_hash or ledger.is_tx_used(m.tx_hash):
                continue
            settled = verify_and_settle_invoice(
                invoice_id=invoice_id,
                tx_hash=m.tx_hash,
                ledger=ledger,
                verifier=verifier,
                network_override="ethereum",
            )
            if settled.get("ok"):
                return settled
    return {
        "ok": False,
        "status_code": 402,
        "error": f"No unredeemed transfer >= ${float(inv['amount_usd']):.2f} USDT/USDC/ETH to {wal[:10]}... detected in recent blocks yet. Wait ~5s after confirmation or paste TX hash.",
    }


_EXPLORER_TX_URLS: dict[str, str] = {
    "polygon": "https://polygonscan.com/tx/",
    "bsc": "https://bscscan.com/tx/",
    "base": "https://basescan.org/tx/",
    "arbitrum": "https://arbiscan.io/tx/",
    "ethereum": "https://etherscan.io/tx/",
    "solana": "https://solscan.io/tx/",
    "bitcoin": "https://mempool.space/tx/",
}


def build_invoice_receipt(invoice: dict[str, Any]) -> dict[str, Any]:
    net = str(invoice.get("network") or "").lower()
    tx = str(invoice.get("tx_hash") or "")
    canonical = {
        "invoice_id": invoice["invoice_id"],
        "status": invoice.get("status", ""),
        "title": invoice.get("title", ""),
        "amount_usd": float(invoice.get("amount_usd") or 0.0),
        "paid_amount_usd": float(invoice.get("paid_amount_usd") or 0.0),
        "token": invoice.get("token", ""),
        "network": net,
        "merchant_wallet": invoice.get("merchant_wallet", ""),
        "payer": invoice.get("payer", ""),
        "tx_hash": tx,
        "paid_at": invoice.get("paid_at", ""),
    }
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    meta = invoice.get("metadata") or {}
    return {
        **canonical,
        "receipt_sha256": digest,
        "explorer_url": (_EXPLORER_TX_URLS[net] + tx) if (tx and net in _EXPLORER_TX_URLS) else "",
        "api_pass": meta.get("api_pass"),
        "customer_ref": str(meta.get("customer_ref") or ""),
        "billing_days": int(meta.get("billing_days") or 0),
        "subscription_expires_at": meta.get("subscription_expires_at"),
    }


def _wallet_payment_uri(invoice: dict[str, Any], native_amount: float = 0.0) -> str:
    net = str(invoice.get("network") or "polygon").lower()
    wal = str(invoice.get("merchant_wallet") or "").strip()
    amt = float(invoice.get("amount_usd") or 0.0)
    if net == "bitcoin":
        return f"bitcoin:{wal}?amount={native_amount:.8f}" if native_amount > 0 else f"bitcoin:{wal}"
    if net == "solana":
        usdc_mint = StudioConfig.SOLANA_STABLECOINS["USDC"][0]
        return f"solana:{wal}?amount={amt:.2f}&spl-token={usdc_mint}"
    chain_id = StudioConfig.EVM_CHAIN_IDS.get(net, 137)
    tok_map = StudioConfig.EVM_STABLECOINS.get(net, {})
    tok_addr, dec = tok_map.get("USDT") or tok_map.get("USDC") or ("", 6)
    if not tok_addr:
        return wal
    units = int(round(amt * 100)) * (10 ** max(0, dec - 2))
    return f"ethereum:{tok_addr}@{chain_id}/transfer?address={wal}&uint256={units}"


def render_paygate_invoice_html(base: str, invoice: dict[str, Any]) -> str:
    inv_id = html.escape(invoice["invoice_id"])
    title = html.escape(invoice["title"])
    net_raw = str(invoice["network"]).lower()
    net = html.escape(net_raw.upper())
    wallet = html.escape(invoice["merchant_wallet"])
    amt_f = float(invoice["amount_usd"])
    amt = f"{amt_f:.2f}"
    is_paid = invoice.get("status") == "paid"
    badge_bg = "rgba(16,185,129,.14)" if is_paid else "rgba(245,158,11,.13)"
    badge_fg = "#34d399" if is_paid else "#fbbf24"
    badge_bd = "rgba(16,185,129,.45)" if is_paid else "rgba(245,158,11,.42)"
    status_txt = "PAID · CONFIRMED ON-CHAIN" if is_paid else "AWAITING ON-CHAIN SETTLEMENT"
    tx_esc = html.escape(invoice.get("tx_hash") or "")
    redir = html.escape(invoice.get("redirect_url") or "")
    api_pass = (invoice.get("metadata") or {}).get("api_pass") or {}
    api_key_esc = html.escape(str(api_pass.get("api_key") or ""))
    receipt_url = f"{base}/paygate/receipt/{inv_id}"

    native_sym = (NATIVE_ASSET_SPECS.get(net_raw) or ("", ""))[0]
    spot_px = 0.0
    if not is_paid and native_sym:
        try:
            spot_px = fetch_native_usd_price(net_raw)
        except Exception:
            spot_px = 0.0
    native_est = ((amt_f / spot_px) * 1.005) if spot_px > 0 else 0.0
    pay_uri = html.escape(_wallet_payment_uri(invoice, native_amount=native_est))

    evm_tokens = StudioConfig.EVM_STABLECOINS.get(net_raw, {})
    evm_chain_id = StudioConfig.EVM_CHAIN_IDS.get(net_raw, 137)
    usdt_addr, usdt_dec = evm_tokens.get("USDT") or ("", 6)
    usdc_addr, usdc_dec = evm_tokens.get("USDC") or ("", 6)
    one_click_btns = ""
    chain_switcher_html = ""
    tier_switcher_html = ""
    raw_wal = invoice["merchant_wallet"].strip()
    is_studio_inv = _is_studio_wallet(raw_wal)
    if not is_paid and (raw_wal.startswith("0x") or is_studio_inv):
        pills = []
        net_choices = [("base", "Base"), ("polygon", "Polygon"), ("arbitrum", "Arbitrum"), ("bsc", "BSC (BEP-20)"), ("ethereum", "Ethereum")]
        if is_studio_inv:
            net_choices.append(("solana", "Solana (SPL + SOL)"))
            net_choices.append(("bitcoin", "Bitcoin (BTC)"))
        for c_net, c_lbl in net_choices:
            active_cls = "net-pill active" if c_net == net_raw else "net-pill"
            pills.append(f'<a href="?network={c_net}" class="{active_cls}">{c_lbl}</a>')
        chain_switcher_html = (
            '<div class="section-block">'
            '<div class="kicker">SWITCH PAYMENT NETWORK · 7 RAILS (USDT / USDC / NATIVE)</div>'
            f'<div class="pill-row">{"".join(pills)}</div>'
            '</div>'
        )
    if not is_paid and is_studio_inv:
        s_evm = StudioConfig.PHANTOM_EVM_ADDRESS or "0x4Ed4D0750453C027FA8398067d5af980Bcc9B6eD"
        t_net = net_raw if net_raw in ("base", "polygon", "arbitrum", "bsc", "ethereum") else "base"
        tier_switcher_html = (
            '<div class="delivery-banner">'
            '<div class="delivery-kicker">INSTANT AUTOMATED ON-SCREEN DELIVERY · NO EMAIL OR SIGNUP</div>'
            '<div class="delivery-sub">As soon as 1 block confirms (~3–5s on EVM/Solana), this screen automatically unlocks your <code>cg_live_...</code> API / White-Label Key and SHA-256 receipt. Quick-switch plan:</div>'
            '<div class="tier-row">'
            f'<a href="{base}/paygate/pay?merchant={s_evm}&amount=0.5&network={t_net}&title=ChainGuard+24h+Micro+Unlock+Pass&issue_pass=1&billing_days=1" class="tier-chip chip-sky">$0.50 · 24h Test Pass</a>'
            f'<a href="{base}/paygate/pay?merchant={s_evm}&amount=2&network={t_net}&title=ChainGuard+7-Day+Starter+MCP+Pass&issue_pass=1&billing_days=7" class="tier-chip chip-em">$2.00 · 7-Day Pass</a>'
            f'<a href="{base}/paygate/pay?merchant={s_evm}&amount=9&network={t_net}&title=ChainGuard+Unlimited+MCP+Pass&issue_pass=1&billing_days=30" class="tier-chip chip-vio">$9.00 · 30-Day Pro</a>'
            f'<a href="{base}/paygate/pay?merchant={s_evm}&amount=79&network={t_net}&title=PayGate+Lifetime+White-Label+License&issue_pass=1&billing_days=3650" class="tier-chip chip-amb">$79.00 · Lifetime White-Label</a>'
            '</div></div>'
        )
    if not is_paid and net_raw in StudioConfig.EVM_CHAIN_IDS:
        btns = []
        if usdt_addr:
            btns.append(f"""<button class="btn-primary" onclick="payWithWallet('{usdt_addr}', {usdt_dec}, 'USDT')">1-Click Pay ${amt} USDT</button>""")
        if usdc_addr:
            btns.append(f"""<button class="btn-secondary btn-sky" onclick="payWithWallet('{usdc_addr}', {usdc_dec}, 'USDC')">1-Click Pay ${amt} USDC</button>""")
        if native_sym:
            est_lbl = f"~{native_est:.6f} " if native_est > 0 else ""
            btns.append(f"""<button class="btn-secondary btn-vio" onclick="payNativeWithWallet()">1-Click Pay {est_lbl}{native_sym} (Native)</button>""")
        if btns:
            one_click_btns = f"""<div class="action-grid">{''.join(btns)}</div>"""
    elif not is_paid and net_raw == "solana":
        est_lbl = f"~{native_est:.5f} " if native_est > 0 else ""
        one_click_btns = (
            '<div class="action-grid">'
            f'<button class="btn-primary" onclick="paySolanaNativeWithWallet()">1-Click Pay {est_lbl}SOL (Phantom / Solflare)</button>'
            '</div>'
        )

    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=168x168&margin=6&data={urllib.parse.quote(wallet)}"

    paid_key_html = (
        f"""<div class="key-box">
          <div class="kicker" style="color:#7dd3fc;">ACTIVE LICENSE &amp; API KEY ({api_pass.get('calls_remaining', 1000)} CALLS · {html.escape(str(api_pass.get('plan_id') or 'pro_pass')).upper()})</div>
          <div class="copy-row" style="margin-top:8px;">
            <code class="mono-box">{api_key_esc}</code>
            <button class="btn-primary" style="padding:10px 16px;" onclick="navigator.clipboard.writeText('{api_key_esc}');this.textContent='Copied'">Copy Key</button>
          </div>
          <div class="hint" style="margin-top:8px;">Pass header <code>X-API-Key: {api_key_esc}</code> on <code>POST {base}/mcp</code> or any <code>/api/v1/x402/*</code> route to unlock full patches, 25 DeFi pools, and 8-chain telemetry.</div>
        </div>"""
        if api_key_esc
        else ""
    )

    asset_desc = (
        f"BTC (~{native_est:.8f} BTC)"
        if (net_raw == "bitcoin" and native_est > 0)
        else ("USDT / USDC" + (f" or ~{native_est:.6f} {native_sym}" if (native_sym and native_est > 0) else (f" or {native_sym}" if native_sym else "")))
    )

    meta_inv = invoice.get("metadata") or {}
    unlocked_aid = html.escape(str(meta_inv.get("unlocked_audit_id") or ""))
    activated_wd = meta_inv.get("activated_watchdog") or {}
    paid_badge_html = (
        f"""<div class="key-box" style="border-color:rgba(16,185,129,.5);">
          <div class="kicker" style="color:#34d399;">ON-CHAIN VERIFIED TRUST BADGE UNLOCKED · AUDIT {unlocked_aid}</div>
          <div style="margin:10px 0;"><img src="{base}/api/v1/badge/{unlocked_aid}.svg" alt="ChainGuard Verified Badge" style="height:24px;display:block;"/></div>
          <div class="copy-row">
            <code class="mono-box">[![ChainGuard Verified]({base}/api/v1/badge/{unlocked_aid}.svg)]({base}/certificate/{unlocked_aid})</code>
            <button class="btn-secondary" onclick="navigator.clipboard.writeText('[![ChainGuard Verified]({base}/api/v1/badge/{unlocked_aid}.svg)]({base}/certificate/{unlocked_aid})');this.textContent='Copied'">Copy Badge MD</button>
          </div>
          <div style="margin-top:10px;"><a class="btn-primary" href="{base}/certificate/{unlocked_aid}">Open Verified Certificate &amp; Patches -&gt;</a></div>
        </div>"""
        if unlocked_aid
        else ""
    )
    paid_wd_html = (
        f"""<div class="key-box" style="border-color:rgba(56,189,248,.5);">
          <div class="kicker" style="color:#38bdf8;">24/7 AUTOMATED SECURITY WATCHDOG ACTIVE</div>
          <div class="hint" style="margin-top:6px;">Watchdog ID: <code>{html.escape(str(activated_wd.get('watch_id') or ''))}</code> · Status: <strong style="color:#34d399;">ACTIVE</strong></div>
          <div class="hint" style="margin-top:4px;">Target: <code>{html.escape(str(activated_wd.get('target_value') or ''))}</code> ({html.escape(str(activated_wd.get('target_type') or ''))} · {html.escape(str(activated_wd.get('network') or 'base')).upper()})</div>
          <div class="hint" style="margin-top:4px;">Alert Webhook: <code>{html.escape(str(activated_wd.get('webhook_url') or ''))}</code></div>
        </div>"""
        if isinstance(activated_wd, dict) and activated_wd.get("watch_id")
        else ""
    )

    paid_block = (
        f"""<div class="glass-card confirmed-card">
          <div class="confirmed-head">
            <span class="dot-live" style="background:#10b981;"></span>
            <span>Payment Confirmed On-Chain</span>
          </div>
          <div class="hint" style="margin-top:8px;">Settled TX Hash: <code>{tx_esc}</code></div>
          {paid_badge_html}
          {paid_wd_html}
          {paid_key_html}
          <div class="pill-row" style="margin-top:16px;">
            <a class="btn-secondary" href="{receipt_url}">View Verifiable SHA-256 Receipt</a>
            {f'<a class="btn-primary" href="{redir}">Continue to Merchant -&gt;</a>' if redir else ''}
          </div>
        </div>"""
        if is_paid
        else f"""<div class="checkout-grid">
          <div class="glass-card main-pane">
            <div class="stepper">
              <div class="step done"><span>1</span> Select Network</div>
              <div class="step active"><span>2</span> Send Transfer</div>
              <div class="step"><span>3</span> Auto-Unlock</div>
            </div>
            {chain_switcher_html}
            {one_click_btns}
            <div class="section-block">
              <label class="field-label">Direct Transfer Address · Send <strong>${amt} {asset_desc}</strong> on <strong>{net}</strong></label>
              <div class="copy-row">
                <code id="mWal" class="mono-box">{wallet}</code>
                <button class="btn-secondary" onclick="navigator.clipboard.writeText('{wallet}');this.textContent='Copied'">Copy Address</button>
                <button class="btn-secondary" onclick="navigator.clipboard.writeText('{amt}');this.textContent='Copied ${amt}'">Copy ${amt}</button>
              </div>
              <div class="hint" style="margin-top:8px;">Wallet URI: <code>{pay_uri}</code></div>
            </div>
            <div class="section-block" style="margin-bottom:0;">
              <label class="field-label">On-Chain Confirmation (Auto-polls every 12s · No TX hash required)</label>
              <div class="verify-bar">
                <button class="btn-primary" style="width:100%;" onclick="autoDetectInv(false)">I Sent It — Auto-Detect On-Chain (No TX Hash Needed)</button>
              </div>
              <div class="copy-row" style="margin-top:10px;">
                <input id="invTx" class="tx-input" placeholder="Optional: paste 0x... / Solana / Bitcoin TXID for instant check"/>
                <button class="btn-secondary" onclick="verifyInv()">Verify TX</button>
              </div>
              <div class="live-status">
                <span class="dot-live"></span>
                <span id="invMsg">Listening for on-chain transfer to {wallet[:10]}... on {net}...</span>
              </div>
            </div>
          </div>

          <div class="side-pane">
            <div class="glass-card qr-card">
              <div class="kicker" style="text-align:center;">MOBILE WALLET SCAN</div>
              <div class="qr-frame">
                <img src="{qr_url}" width="164" height="164" alt="Wallet QR" onerror="this.parentElement.style.display='none'"/>
                <div class="qr-cap">SCAN IN MOBILE WALLET</div>
              </div>
              <div class="summary-rows">
                <div class="s-row"><span>Settlement</span><strong>0% Fee · Direct</strong></div>
                <div class="s-row"><span>Custody</span><strong>Non-Custodial</strong></div>
                <div class="s-row"><span>Finality</span><strong>~3–12 seconds</strong></div>
                <div class="s-row"><span>Receipt</span><strong>SHA-256 Proof</strong></div>
              </div>
              <div class="side-foot">
                <span>Short on {net} stablecoins?</span>
                <a href="{base}/recover" target="_blank">Sweep Dust / Reclaim SOL -&gt;</a>
              </div>
            </div>
            {tier_switcher_html}
          </div>
        </div>"""
    )
    cents_int = int(round(amt_f * 100))
    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Pay ${amt} {html.escape(native_sym or 'USDT')} — {title} | PayGate USDT</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&family=Plus+Jakarta+Sans:wght@600;700;800&display=swap" rel="stylesheet"/>
<style>
  :root{{--bg:#050811;--surface:rgba(13,20,36,.78);--border:rgba(148,163,184,.16);--text:#f1f5f9;--muted:#94a3b8;--em:#10b981;--sky:#38bdf8;--vio:#a78bfa;--amb:#f59e0b;}}
  *{{box-sizing:border-box}}
  body{{margin:0;min-height:100vh;background:radial-gradient(900px 420px at 15% -10%,rgba(16,185,129,.14),transparent 60%),radial-gradient(800px 420px at 90% 0%,rgba(56,189,248,.11),transparent 58%),var(--bg);color:var(--text);font-family:'Inter',system-ui,sans-serif;line-height:1.55;-webkit-font-smoothing:antialiased}}
  .topbar{{border-bottom:1px solid var(--border);background:rgba(5,8,17,.75);backdrop-filter:blur(14px);position:sticky;top:0;z-index:20}}
  .topbar-inner{{max-width:1020px;margin:0 auto;padding:12px 20px;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}}
  .brand{{display:flex;align-items:center;gap:10px;text-decoration:none;color:#fff;font-family:'Plus Jakarta Sans',sans-serif;font-weight:800;font-size:.98rem;letter-spacing:-.02em}}
  .brand-mark{{width:26px;height:26px;border-radius:8px;background:linear-gradient(135deg,#10b981,#06b6d4);display:inline-flex;align-items:center;justify-content:center;color:#02160f;font-weight:900;font-size:.78rem}}
  .nav-links{{display:flex;gap:14px;align-items:center;font-size:.82rem}}
  .nav-links a{{color:var(--muted);text-decoration:none;font-weight:600}} .nav-links a:hover{{color:#fff}}
  .wrap{{max-width:1020px;margin:0 auto;padding:28px 20px 68px}}
  .hero-strip{{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:20px;padding:22px 24px;border-radius:18px;background:var(--surface);border:1px solid var(--border);backdrop-filter:blur(16px)}}
  .status-pill{{display:inline-flex;align-items:center;gap:7px;padding:5px 12px;border-radius:999px;background:{badge_bg};border:1px solid {badge_bd};color:{badge_fg};font-size:.72rem;font-weight:700;letter-spacing:.04em;text-transform:uppercase}}
  .inv-meta{{font-size:.8rem;color:var(--muted);margin-top:8px;font-family:'JetBrains Mono',monospace}}
  .inv-title{{font-family:'Plus Jakarta Sans',sans-serif;font-size:clamp(1.35rem,2.6vw,1.8rem);font-weight:800;letter-spacing:-.03em;margin:10px 0 0;color:#fff}}
  .price-box{{text-align:right}}
  .price-main{{font-family:'Plus Jakarta Sans',sans-serif;font-size:clamp(2rem,3.6vw,2.6rem);font-weight:800;letter-spacing:-.04em;background:linear-gradient(135deg,#34d399,#38bdf8);-webkit-background-clip:text;-webkit-text-fill-color:transparent;line-height:1.1}}
  .price-sub{{font-size:.82rem;color:var(--muted);margin-top:4px;font-weight:600}}
  .checkout-grid{{display:grid;grid-template-columns:1.55fr 1fr;gap:18px;align-items:start}}
  @media(max-width:860px){{.checkout-grid{{grid-template-columns:1fr}} .price-box{{text-align:left}}}}
  .glass-card{{background:var(--surface);border:1px solid var(--border);border-radius:18px;padding:22px;backdrop-filter:blur(16px)}}
  .confirmed-card{{border-color:rgba(16,185,129,.5);background:rgba(5,46,22,.45)}}
  .confirmed-head{{display:flex;align-items:center;gap:9px;font-family:'Plus Jakarta Sans',sans-serif;font-weight:800;font-size:1.1rem;color:#6ee7b7}}
  .stepper{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px;padding-bottom:14px;border-bottom:1px solid var(--border)}}
  .step{{display:inline-flex;align-items:center;gap:7px;font-size:.76rem;font-weight:600;color:var(--muted)}}
  .step span{{width:20px;height:20px;border-radius:999px;display:inline-flex;align-items:center;justify-content:center;font-size:.7rem;font-weight:800;background:rgba(148,163,184,.12);color:var(--muted)}}
  .step.done{{color:#6ee7b7}} .step.done span{{background:rgba(16,185,129,.22);color:#34d399}}
  .step.active{{color:#fff}} .step.active span{{background:linear-gradient(135deg,#10b981,#06b6d4);color:#02160f}}
  .section-block{{margin-bottom:18px}}
  .kicker{{font-size:.7rem;font-weight:700;letter-spacing:.08em;color:var(--muted);text-transform:uppercase;margin-bottom:8px}}
  .pill-row{{display:flex;gap:7px;flex-wrap:wrap}}
  .net-pill{{display:inline-block;padding:6px 12px;border-radius:999px;border:1px solid var(--border);background:rgba(7,11,20,.7);color:#cbd5e1;font-size:.78rem;font-weight:600;text-decoration:none;transition:all .15s ease}}
  .net-pill:hover{{border-color:rgba(56,189,248,.55);color:#fff}}
  .net-pill.active{{background:linear-gradient(135deg,#10b981,#059669);border-color:#10b981;color:#02160f;font-weight:800;box-shadow:0 0 18px rgba(16,185,129,.25)}}
  .action-grid{{display:flex;gap:9px;flex-wrap:wrap;margin-bottom:18px}}
  .btn-primary{{display:inline-flex;align-items:center;justify-content:center;background:linear-gradient(135deg,#10b981,#06b6d4);color:#02160f;font-weight:800;padding:11px 18px;border-radius:11px;border:none;cursor:pointer;text-decoration:none;font-size:.86rem;letter-spacing:-.01em;transition:transform .12s ease,box-shadow .15s ease}}
  .btn-primary:hover{{transform:translateY(-1px);box-shadow:0 8px 24px rgba(16,185,129,.28)}}
  .btn-secondary{{display:inline-flex;align-items:center;justify-content:center;background:rgba(30,41,59,.75);color:#f8fafc;font-weight:700;padding:10px 14px;border-radius:11px;border:1px solid rgba(148,163,184,.24);cursor:pointer;text-decoration:none;font-size:.84rem;transition:border-color .15s ease,background .15s ease}}
  .btn-secondary:hover{{border-color:rgba(248,250,252,.45);background:rgba(30,41,59,.95)}}
  .btn-sky{{border-color:rgba(56,189,248,.45);color:#7dd3fc}}
  .btn-vio{{border-color:rgba(167,139,250,.45);color:#ddd6fe}}
  .field-label{{display:block;font-size:.82rem;color:#cbd5e1;font-weight:600;margin-bottom:7px}}
  .copy-row{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
  .mono-box{{flex:1;min-width:200px;background:rgba(2,6,23,.85);border:1px solid var(--border);padding:10px 12px;border-radius:10px;color:#6ee7b7;font-family:'JetBrains Mono',monospace;font-size:.8rem;word-break:break-all;display:block}}
  .tx-input{{flex:1;min-width:210px;padding:10px 12px;border-radius:10px;border:1px solid rgba(148,163,184,.22);background:rgba(2,6,23,.85);color:#f8fafc;font-family:'JetBrains Mono',monospace;font-size:.82rem;outline:none}}
  .tx-input:focus{{border-color:#38bdf8}}
  .hint{{font-size:.76rem;color:var(--muted);word-break:break-all}}
  code{{font-family:'JetBrains Mono',monospace;background:rgba(2,6,23,.85);padding:2px 6px;border-radius:6px;color:#6ee7b7;font-size:.78rem}}
  .live-status{{margin-top:12px;padding:10px 12px;border-radius:10px;background:rgba(56,189,248,.07);border:1px solid rgba(56,189,248,.22);display:flex;align-items:center;gap:9px;font-size:.82rem;color:#7dd3fc}}
  .dot-live{{width:8px;height:8px;border-radius:999px;background:#38bdf8;flex-shrink:0;box-shadow:0 0 10px rgba(56,189,248,.9);animation:pulse 1.8s infinite}}
  @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.35}}}}
  .side-pane{{display:flex;flex-direction:column;gap:14px}}
  .qr-card{{text-align:center}}
  .qr-frame{{display:inline-block;background:#fff;padding:10px;border-radius:14px;box-shadow:0 12px 30px rgba(0,0,0,.35);margin:6px 0 14px}}
  .qr-frame img{{display:block;border-radius:8px}}
  .qr-cap{{font-size:.66rem;color:#0f172a;font-weight:800;letter-spacing:.06em;margin-top:6px}}
  .summary-rows{{text-align:left;border-top:1px solid var(--border);padding-top:12px}}
  .s-row{{display:flex;justify-content:space-between;font-size:.8rem;padding:5px 0;color:var(--muted)}}
  .s-row strong{{color:#e2e8f0;font-weight:600}}
  .side-foot{{margin-top:12px;padding-top:12px;border-top:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;font-size:.78rem;color:var(--muted)}}
  .side-foot a{{color:#38bdf8;font-weight:700;text-decoration:none}}
  .delivery-banner{{background:var(--surface);border:1px solid rgba(56,189,248,.24);border-radius:16px;padding:16px}}
  .delivery-kicker{{font-size:.7rem;font-weight:800;letter-spacing:.07em;color:#38bdf8;margin-bottom:6px}}
  .delivery-sub{{font-size:.78rem;color:#cbd5e1;margin-bottom:10px;line-height:1.5}}
  .tier-row{{display:grid;grid-template-columns:1fr 1fr;gap:7px}}
  .tier-chip{{display:block;text-align:center;padding:7px 8px;border-radius:9px;border:1px solid;font-size:.74rem;font-weight:700;text-decoration:none}}
  .chip-sky{{border-color:rgba(56,189,248,.45);color:#7dd3fc;background:rgba(56,189,248,.07)}}
  .chip-em{{border-color:rgba(16,185,129,.45);color:#6ee7b7;background:rgba(16,185,129,.07)}}
  .chip-vio{{border-color:rgba(167,139,250,.45);color:#ddd6fe;background:rgba(167,139,250,.07)}}
  .chip-amb{{border-color:rgba(245,158,11,.45);color:#fcd34d;background:rgba(245,158,11,.07)}}
  .key-box{{margin-top:14px;background:rgba(2,6,23,.82);border:1px solid rgba(56,189,248,.4);border-radius:12px;padding:14px}}
  .footer{{margin-top:26px;text-align:center;font-size:.78rem;color:#64748b}}
  .footer a{{color:#94a3b8;text-decoration:none;font-weight:600}} .footer a:hover{{color:#38bdf8}}
</style></head><body>
<header class="topbar">
  <div class="topbar-inner">
    <a class="brand" href="{base}/paygate"><span class="brand-mark">PG</span><span>PayGate USDT</span></a>
    <nav class="nav-links">
      <a href="{base}/">ChainGuard Studio</a>
      <a href="{base}/paygate">Create Checkout</a>
      <a href="{base}/recover">Vault Recovery</a>
      <a href="https://github.com/EzequielLech/paygate-usdt" target="_blank">GitHub SDK</a>
    </nav>
  </div>
</header>
<div class="wrap">
  <div class="hero-strip">
    <div>
      <span class="status-pill">{status_txt}</span>
      <h1 class="inv-title">{title}</h1>
      <div class="inv-meta">Invoice <code>{inv_id}</code> · Network: {net} · 0% Gateway Cut</div>
    </div>
    <div class="price-box">
      <div class="price-main">${amt}</div>
      <div class="price-sub">USDT / USDC{' or ' + html.escape(native_sym) if native_sym else ''} on {net}</div>
    </div>
  </div>
  {paid_block}
  <div class="footer">
    Non-custodial settlement powered by <a href="https://github.com/EzequielLech/paygate-usdt" target="_blank">PayGate USDT Open-Source Gateway</a> · <a href="{base}/paygate">Launch your own 0% crypto checkout</a>
  </div>
</div>
<script>
const TARGET_CHAIN_HEX = '0x' + ({evm_chain_id}).toString(16);
const MERCHANT_WALLET = '{wallet}';
const CENTS_INT = {cents_int};
const AMOUNT_USD = {amt_f};
const TARGET_NETWORK = '{net_raw}';

async function paySolanaNativeWithWallet() {{
  const m = document.getElementById('invMsg');
  const provider = (window.phantom && window.phantom.solana) || window.solana || window.solflare;
  if (!provider) {{
    m.style.color = '#fca5a5';
    m.textContent = 'No Solana browser wallet detected (Phantom/Solflare). Scan QR or transfer SOL/USDC manually and paste signature.';
    return;
  }}
  try {{
    m.style.color = '#38bdf8';
    m.textContent = 'Fetching live SOL spot quote & connecting Solana wallet...';
    const qr = await fetch('/api/v1/paygate/quote?amount_usd=' + AMOUNT_USD + '&network=solana');
    const q = await qr.json();
    if (!qr.ok || !q.lamports || !q.recent_blockhash) {{
      m.style.color = '#fca5a5';
      m.textContent = 'Unable to fetch live Solana quote. Send SOL/USDC manually above.';
      return;
    }}
    if (!window.solanaWeb3) {{
      await new Promise((res, rej) => {{
        const s = document.createElement('script');
        s.src = 'https://cdn.jsdelivr.net/npm/@solana/web3.js@1.95.8/lib/index.iife.min.js';
        s.onload = res; s.onerror = rej; document.head.appendChild(s);
      }});
    }}
    const resp = await provider.connect();
    const fromPubkey = new window.solanaWeb3.PublicKey((resp && resp.publicKey ? resp.publicKey : provider.publicKey).toString());
    const toPubkey = new window.solanaWeb3.PublicKey(MERCHANT_WALLET);
    const tx = new window.solanaWeb3.Transaction({{ recentBlockhash: q.recent_blockhash, feePayer: fromPubkey }});
    tx.add(window.solanaWeb3.SystemProgram.transfer({{ fromPubkey: fromPubkey, toPubkey: toPubkey, lamports: q.lamports }}));
    m.textContent = 'Confirm ~' + q.native_amount + ' SOL ($' + AMOUNT_USD.toFixed(2) + ') in Phantom...';
    const signed = await provider.signAndSendTransaction(tx);
    const sig = (signed && signed.signature) ? signed.signature : String(signed);
    document.getElementById('invTx').value = sig;
    m.style.color = '#34d399';
    m.textContent = 'Solana TX broadcast (' + sig.slice(0, 14) + '...). Verifying on-chain...';
    pollVerify();
  }} catch (err) {{
    m.style.color = '#fca5a5';
    m.textContent = (err && err.message) ? err.message : 'Solana wallet payment cancelled.';
  }}
}}

async function payNativeWithWallet() {{
  const m = document.getElementById('invMsg');
  if (!window.ethereum) {{
    m.style.color = '#fca5a5';
    m.textContent = 'No browser wallet detected (MetaMask/Rabby/Phantom). Transfer manually above and paste TX hash.';
    return;
  }}
  try {{
    m.style.color = '#38bdf8';
    m.textContent = 'Fetching live ' + TARGET_NETWORK.toUpperCase() + ' spot quote...';
    const qr = await fetch('/api/v1/paygate/quote?amount_usd=' + AMOUNT_USD + '&network=' + encodeURIComponent(TARGET_NETWORK));
    const q = await qr.json();
    if (!qr.ok || !q.wei_hex) {{
      m.style.color = '#fca5a5';
      m.textContent = (q && q.detail) ? q.detail : 'Unable to fetch native gas quote.';
      return;
    }}
    const accs = await window.ethereum.request({{ method: 'eth_requestAccounts' }});
    const from = accs[0];
    try {{
      await window.ethereum.request({{ method: 'wallet_switchEthereumChain', params: [{{ chainId: TARGET_CHAIN_HEX }}] }});
    }} catch (swErr) {{}}
    m.textContent = 'Confirm ~' + q.native_amount + ' ' + q.native_symbol + ' ($' + AMOUNT_USD.toFixed(2) + ') in your wallet...';
    const txHash = await window.ethereum.request({{
      method: 'eth_sendTransaction',
      params: [{{ from: from, to: MERCHANT_WALLET, value: q.wei_hex }}]
    }});
    document.getElementById('invTx').value = txHash;
    m.style.color = '#34d399';
    m.textContent = 'TX broadcast (' + txHash.slice(0, 14) + '...). Waiting for RPC finality...';
    pollVerify();
  }} catch (err) {{
    m.style.color = '#fca5a5';
    m.textContent = (err && err.message) ? err.message : 'Native wallet payment cancelled.';
  }}
}}

async function payWithWallet(tokenContract, decimals, sym) {{
  const m = document.getElementById('invMsg');
  if (!window.ethereum) {{
    m.style.color = '#fca5a5';
    m.textContent = 'No browser wallet detected (MetaMask/Rabby/Phantom). Transfer manually above and paste TX hash.';
    return;
  }}
  try {{
    m.style.color = '#38bdf8';
    m.textContent = 'Connecting wallet & switching to {net}...';
    const accs = await window.ethereum.request({{ method: 'eth_requestAccounts' }});
    const from = accs[0];
    try {{
      await window.ethereum.request({{ method: 'wallet_switchEthereumChain', params: [{{ chainId: TARGET_CHAIN_HEX }}] }});
    }} catch (swErr) {{}}
    const amountRaw = BigInt(CENTS_INT) * (10n ** BigInt(Math.max(0, decimals - 2)));
    const recPad = MERCHANT_WALLET.toLowerCase().replace(/^0x/, '').padStart(64, '0');
    const amtPad = amountRaw.toString(16).padStart(64, '0');
    const calldata = '0xa9059cbb' + recPad + amtPad;
    m.textContent = 'Confirm ' + sym + ' transfer in your wallet...';
    const txHash = await window.ethereum.request({{
      method: 'eth_sendTransaction',
      params: [{{ from: from, to: tokenContract, data: calldata, value: '0x0' }}]
    }});
    document.getElementById('invTx').value = txHash;
    m.style.color = '#34d399';
    m.textContent = 'TX broadcast (' + txHash.slice(0, 14) + '...). Waiting for RPC finality...';
    pollVerify();
  }} catch (err) {{
    m.style.color = '#fca5a5';
    m.textContent = (err && err.message) ? err.message : 'Wallet payment cancelled.';
  }}
}}

let _pollCount = 0;
async function pollVerify() {{
  _pollCount += 1;
  const ok = await verifyInv(true);
  if (!ok && _pollCount < 12) {{
    setTimeout(pollVerify, 3000);
  }}
}}

async function verifyInv(silent) {{
  const m = document.getElementById('invMsg');
  const tx = document.getElementById('invTx').value.trim();
  if (!tx) {{ m.style.color = '#fca5a5'; m.textContent = 'Paste a valid transaction hash.'; return false; }}
  if (!silent) {{ m.style.color = '#94a3b8'; m.textContent = 'Checking RPC finality...'; }}
  const r = await fetch('/api/v1/paygate/invoices/{inv_id}/verify', {{
    method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ tx_hash: tx }})
  }});
  const d = await r.json();
  if (!r.ok) {{
    m.style.color = silent ? '#fcd34d' : '#fca5a5';
    m.textContent = (d.detail || 'Awaiting confirmation...') + (silent ? ' (retrying in 3s...)' : '');
    return false;
  }}
  location.reload();
  return true;
}}

async function autoDetectInv(silentBg) {{
  const m = document.getElementById('invMsg');
  if (!m) return false;
  if (!silentBg) {{
    m.style.color = '#38bdf8';
    m.textContent = 'Scanning recent blocks across Base, Polygon, Arbitrum, BSC, Ethereum, Solana & Bitcoin for your transfer...';
  }}
  try {{
    const r = await fetch('/api/v1/paygate/invoices/{inv_id}/auto-detect', {{
      method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: '{{}}'
    }});
    const d = await r.json();
    if (!r.ok) {{
      if (!silentBg) {{
        m.style.color = '#fcd34d';
        m.textContent = d.detail || 'Transfer not seen yet. Wait a few seconds or paste your TX hash.';
      }}
      return false;
    }}
    location.reload();
    return true;
  }} catch (e) {{
    return false;
  }}
}}
if ({'false' if is_paid else 'true'} && (MERCHANT_WALLET.startsWith('0x') || TARGET_NETWORK === 'bitcoin' || TARGET_NETWORK === 'solana')) {{
  setInterval(function() {{
    if (!document.hidden) autoDetectInv(true);
  }}, 12000);
}}
</script></body></html>"""


def render_paygate_receipt_html(base: str, invoice: dict[str, Any]) -> str:
    rec = build_invoice_receipt(invoice)
    inv_id = html.escape(rec["invoice_id"])
    title = html.escape(rec["title"])
    tx_esc = html.escape(rec["tx_hash"] or "Pending")
    exp_url = html.escape(rec["explorer_url"] or "")
    sha_esc = html.escape(rec["receipt_sha256"])
    api_pass = rec.get("api_pass") or {}
    key_esc = html.escape(str(api_pass.get("api_key") or ""))
    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Receipt {inv_id} · PayGate USDT</title>
<style>
  *{{box-sizing:border-box}} body{{margin:0;background:#070b14;color:#f1f5f9;font-family:Inter,system-ui,sans-serif;line-height:1.6}}
  .wrap{{max-width:680px;margin:0 auto;padding:32px 16px}}
  .card{{background:#0f172a;border:1px solid #10b981;border-radius:14px;padding:22px}}
  code{{background:#020617;padding:3px 7px;border-radius:6px;color:#6ee7b7;font-size:.82rem;word-break:break-all}}
  .row{{display:flex;justify-content:space-between;gap:12px;border-bottom:1px solid #1e293b;padding:8px 0;font-size:.9rem}}
</style></head><body><div class="wrap"><div class="card">
  <div style="color:#10b981;font-weight:900;font-size:.82rem;">VERIFIED ON-CHAIN RECEIPT</div>
  <h1 style="margin:6px 0 14px;font-size:1.5rem;">{title}</h1>
  <div class="row"><span>Invoice ID</span><code>{inv_id}</code></div>
  <div class="row"><span>Status</span><strong>{html.escape(str(rec['status']).upper())}</strong></div>
  <div class="row"><span>Amount Settled</span><strong>${float(rec['paid_amount_usd'] or rec['amount_usd']):.2f} {html.escape(str(rec['token'] or 'USDT'))}</strong></div>
  <div class="row"><span>Network</span><strong>{html.escape(str(rec['network']).upper())}</strong></div>
  <div class="row"><span>Merchant Wallet</span><code>{html.escape(str(rec['merchant_wallet']))}</code></div>
  <div class="row"><span>Transaction Hash</span><code>{tx_esc}</code></div>
  <div class="row"><span>Canonical SHA-256</span><code>{sha_esc}</code></div>
  {f'<div class="row"><span>Issued API Pass</span><code>{key_esc}</code></div>' if key_esc else ''}
  <div style="margin-top:16px;display:flex;gap:12px;flex-wrap:wrap;">
    {f'<a href="{exp_url}" target="_blank" style="color:#38bdf8;font-weight:700;">View on Block Explorer →</a>' if exp_url else ''}
    <a href="{base}/api/v1/paygate/invoices/{inv_id}/receipt" style="color:#94a3b8;">Raw JSON Receipt</a>
    <a href="{base}/paygate" style="color:#94a3b8;">PayGate USDT</a>
  </div>
</div></div></body></html>"""


def render_paygate_widget_js(base: str) -> str:
    safe_base = base.rstrip("/")
    return f"""(function(){{
  var nodes=document.querySelectorAll('[data-paygate-merchant]');
  nodes.forEach(function(el){{
    var m=el.getAttribute('data-paygate-merchant')||'';
    var a=el.getAttribute('data-paygate-amount')||'29';
    var n=el.getAttribute('data-paygate-network')||'polygon';
    var t=el.getAttribute('data-paygate-title')||'Instant Payment';
    var p=el.getAttribute('data-paygate-pass')||'0';
    var href='{safe_base}/paygate/pay?merchant='+encodeURIComponent(m)+'&amount='+encodeURIComponent(a)+'&network='+encodeURIComponent(n)+'&title='+encodeURIComponent(t)+'&issue_pass='+encodeURIComponent(p);
    if(el.tagName==='A'){{el.href=href;el.target='_blank';}}
    else{{el.style.cursor='pointer';el.addEventListener('click',function(){{window.open(href,'_blank');}});}}
    if(!el.textContent.trim()){{el.textContent='Pay $'+a+' USDT ('+n.toUpperCase()+')';}}
  }});
}})();
"""


def render_paygate_landing_html(base: str, stats: dict[str, Any], lang: str = "en") -> str:
    evm_addr = html.escape(StudioConfig.PHANTOM_EVM_ADDRESS)
    sol_addr = html.escape(StudioConfig.PHANTOM_SOL_ADDRESS)
    is_es = (lang or "en").strip().lower().startswith("es")
    pro_checkout_url = f"{base}/paygate/pay?merchant={evm_addr}&amount=9&network=base&title=PayGate+Cloud+Pro+30d&issue_pass=1&billing_days=30"
    wl_checkout_url = f"{base}/paygate/pay?merchant={evm_addr}&amount=79&network=base&title=PayGate+Lifetime+White-Label+License&issue_pass=1&billing_days=3650"
    turnkey_checkout_url = f"{base}/paygate/pay?merchant={evm_addr}&amount=99&network=base&title=Turnkey+VIP+Gate+50pct+Deposit"
    custom_checkout_url = f"{base}/paygate/pay?merchant={evm_addr}&amount=249&network=base&title=Custom+Bot+and+API+50pct+Deposit"
    t = {
        "badge": "PAYGATE USDT v2.8 · 0% COMMISSION · 7 CHAINS · DIRECT SELF-CUSTODY" if not is_es else "PAYGATE USDT v2.8 · 0% COMISION · 7 REDES · AUTO-CUSTODIA DIRECTA",
        "title": "PayGate USDT — Monetize Private Telegram Groups, Crypto Checkouts & x402 Paywalls" if not is_es else "PayGate USDT — Checkout Cripto sin Custodia, Paywalls x402 y Canales VIP Telegram",
        "h1": 'Accept USDT, USDC, ETH, SOL &amp; BTC Directly to Your Wallet — <span class="grad-text">Zero Stripe, Zero KYC, 0% Cut</span>.' if not is_es else 'Cobra en USDT, USDC, ETH, SOL y BTC Directo a tu Wallet — <span class="grad-text">Sin Stripe, Sin KYC, 0% Comision</span>.',
        "sub": "Generate Stripe-grade crypto checkout links, x402 AI agent paywalls, and automated Telegram VIP access bots in 60 seconds. Funds settle directly from buyer to your Phantom, MetaMask, hardware, or exchange address with cryptographic on-chain verification." if not is_es else "Genera links de cobro cripto nivel Stripe, paywalls x402 para agentes IA y bots VIP de Telegram en 60 segundos — sin banco, sin monotributo ni intermediarios. Los fondos van directo del comprador a tu wallet Phantom, MetaMask o exchange con verificacion on-chain automatica.",
        "cta1": "Create Checkout Link Now" if not is_es else "Crear Link de Cobro Ahora",
        "cta2": "Connect Telegram VIP Bot" if not is_es else "Conectar Bot VIP Telegram",
        "c1k": "01 · NON-CUSTODIAL" if not is_es else "01 · CERO CUSTODIA",
        "c1t": "100% direct to your wallet" if not is_es else "100% directo a tu wallet",
        "c1b": "Funds never pass through an intermediary or bank. Every transfer settles directly from buyer to your address across Base, Polygon, Arbitrum, BSC, Ethereum, Solana, and Bitcoin." if not is_es else "El dinero nunca pasa por terceros ni bancos. Cada pago llega directo a tu wallet en Base, Polygon, Arbitrum, BSC, Ethereum, Solana y Bitcoin.",
        "c2k": "02 · 1-CLICK + AUTO-DETECT" if not is_es else "02 · 1-CLIC + AUTO-DETECCION",
        "c2t": "Zero manual screenshots" if not is_es else "Cero capturas manuales",
        "c2b": "1-Click MetaMask & Phantom checkout, mobile QR codes, automatic RPC block detection without pasting TX hashes, anti-replay protection, and HMAC-SHA256 webhooks." if not is_es else "Pago en 1 clic con MetaMask y Phantom, QR movil, deteccion automatica on-chain sin pegar TX hash, proteccion anti-replay y webhooks firmados HMAC-SHA256.",
        "c3k": "03 · BUILT FOR WEB3 & AI" if not is_es else "03 · LISTO PARA WEB3 E IA",
        "c3t": "Hosted Cloud or MIT Open-Source" if not is_es else "Nube Gestionada o Open-Source MIT",
        "c3b": "Use hosted checkout pages immediately (first 3 settled sales free), drop in our Python / TypeScript x402 SDK, or monetize Claude/Cursor MCP tools via Official MCP Registry." if not is_es else "Usa el checkout gestionado al instante (primeras 3 ventas gratis), integra el SDK Python/TypeScript x402 o monetiza herramientas MCP desde el registro oficial.",
        "setup_h": "Connect Your Telegram VIP Bot in 60s (Free up to 3 active members)" if not is_es else "Conecta tu Bot VIP de Telegram en 60s (Gratis hasta 3 miembros)",
        "setup_p": "Open <strong>@BotFather</strong> in Telegram, run <code>/newbot</code>, paste the token below, and add the bot as admin with Invite &amp; Ban permissions." if not is_es else "Abri <strong>@BotFather</strong> en Telegram, ejecuta <code>/newbot</code>, pega el token abajo y agrega el bot como administrador con permisos de invitar y restringir.",
        "l_name": "Community / Product Name" if not is_es else "Nombre de tu Comunidad / Producto",
        "l_tok": "Bot Token (@BotFather)" if not is_es else "Token de tu Bot (@BotFather)",
        "l_net": "Payout Network (0% fee)" if not is_es else "Red de Cobro (0% comision)",
        "l_wal": "Your Payout Wallet (0x... or Solana)" if not is_es else "Tu Wallet Receptora (0x... o Solana)",
        "l_chan": "Private Channel/Group ID (optional, e.g. -1001234567890)" if not is_es else "ID de tu Canal/Grupo VIP (opcional, ej: -1001234567890)",
        "l_price": "Monthly Price (30 days) in USDT" if not is_es else "Precio Plan Mensual (30 dias) en USDT",
        "l_deliv": "Post-payment bonus link or instructions (optional)" if not is_es else "Entrega extra o link privado tras pagar (opcional)",
        "btn_go": "Launch My Bot Now" if not is_es else "Activar mi Bot Ahora",
        "up_h": "Transparent Pricing — 1-Click Direct On-Chain Activation" if not is_es else "Planes Transparentes — Activacion Directa On-Chain en 1 Clic",
    }
    alt_lang = "en" if is_es else "es"
    alt_lbl = "English" if is_es else "ES"
    return f"""<!DOCTYPE html><html lang="{'es' if is_es else 'en'}"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{t['title']}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&family=Plus+Jakarta+Sans:wght@600;700;800&display=swap" rel="stylesheet"/>
<style>
  :root{{--bg:#050811;--surface:rgba(13,20,36,.78);--border:rgba(148,163,184,.16);--text:#f1f5f9;--muted:#94a3b8;--em:#10b981;--sky:#38bdf8;--amb:#f59e0b;}}
  *{{box-sizing:border-box}}
  body{{margin:0;min-height:100vh;background:radial-gradient(1000px 460px at 18% -12%,rgba(16,185,129,.14),transparent 60%),radial-gradient(850px 420px at 88% -8%,rgba(56,189,248,.12),transparent 58%),var(--bg);color:var(--text);font-family:'Inter',system-ui,sans-serif;line-height:1.55;-webkit-font-smoothing:antialiased}}
  .topbar{{border-bottom:1px solid var(--border);background:rgba(5,8,17,.78);backdrop-filter:blur(16px);position:sticky;top:0;z-index:30}}
  .topbar-inner{{max-width:1120px;margin:0 auto;padding:13px 20px;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}}
  .brand{{display:flex;align-items:center;gap:10px;text-decoration:none;color:#fff;font-family:'Plus Jakarta Sans',sans-serif;font-weight:800;font-size:1rem;letter-spacing:-.02em}}
  .brand-mark{{width:28px;height:28px;border-radius:8px;background:linear-gradient(135deg,#10b981,#06b6d4);display:inline-flex;align-items:center;justify-content:center;color:#02160f;font-weight:900;font-size:.8rem}}
  .nav{{display:flex;gap:14px;align-items:center;flex-wrap:wrap;font-size:.82rem}}
  .nav a{{color:var(--muted);text-decoration:none;font-weight:600}} .nav a:hover{{color:#fff}}
  .nav-pill{{padding:6px 12px;border-radius:999px;border:1px solid rgba(16,185,129,.45);color:#6ee7b7 !important;background:rgba(16,185,129,.08)}}
  .wrap{{max-width:1120px;margin:0 auto;padding:34px 20px 80px}}
  .badge{{display:inline-flex;align-items:center;gap:8px;padding:6px 14px;border-radius:999px;background:rgba(16,185,129,.1);border:1px solid rgba(16,185,129,.4);color:#6ee7b7;font-size:.74rem;font-weight:700;letter-spacing:.05em}}
  h1{{font-family:'Plus Jakarta Sans',sans-serif;font-size:clamp(2rem,4.5vw,3.1rem);line-height:1.1;letter-spacing:-.035em;margin:16px 0 14px;color:#fff;max-width:920px}}
  .grad-text{{background:linear-gradient(135deg,#34d399,#38bdf8);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
  h2{{font-family:'Plus Jakarta Sans',sans-serif;font-size:clamp(1.3rem,2.4vw,1.65rem);letter-spacing:-.025em;color:#fff;margin:0 0 8px}}
  h3{{font-family:'Plus Jakarta Sans',sans-serif;font-size:1.05rem;letter-spacing:-.015em;color:#fff;margin:8px 0 6px}}
  .sub{{color:var(--muted);font-size:1.02rem;max-width:760px;margin:0 0 24px}}
  .hero-actions{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:26px}}
  .btn{{display:inline-flex;align-items:center;justify-content:center;background:linear-gradient(135deg,#10b981,#06b6d4);color:#02160f;font-weight:800;padding:11px 20px;border-radius:11px;border:none;cursor:pointer;text-decoration:none;font-size:.9rem;transition:transform .12s ease,box-shadow .15s ease}}
  .btn:hover{{transform:translateY(-1px);box-shadow:0 8px 24px rgba(16,185,129,.26)}}
  .btn2{{display:inline-flex;align-items:center;justify-content:center;background:rgba(30,41,59,.72);color:#f8fafc;font-weight:700;padding:11px 18px;border-radius:11px;border:1px solid rgba(148,163,184,.24);cursor:pointer;text-decoration:none;font-size:.88rem}}
  .btn2:hover{{border-color:rgba(248,250,252,.45);background:rgba(30,41,59,.95)}}
  .proof-strip{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:0 0 26px}}
  .proof-item{{background:rgba(13,20,36,.6);border:1px solid var(--border);border-radius:12px;padding:12px 14px}}
  .proof-k{{font-size:.68rem;font-weight:700;letter-spacing:.07em;color:var(--muted);text-transform:uppercase}}
  .proof-v{{font-family:'Plus Jakarta Sans',sans-serif;font-size:.98rem;font-weight:800;color:#fff;margin-top:3px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin:22px 0}}
  .price-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;margin:18px 0}}
  .card{{background:var(--surface);border:1px solid var(--border);border-radius:18px;padding:22px;backdrop-filter:blur(16px)}}
  .card-featured{{border-color:rgba(16,185,129,.5);box-shadow:0 0 32px rgba(16,185,129,.08)}}
  .card-sky{{border-color:rgba(56,189,248,.4)}}
  .card-amb{{border-color:rgba(245,158,11,.42)}}
  .kicker{{font-size:.72rem;font-weight:800;letter-spacing:.07em;color:#34d399;text-transform:uppercase}}
  .price-num{{font-family:'Plus Jakarta Sans',sans-serif;font-size:2rem;font-weight:800;letter-spacing:-.04em;color:#fff;margin:8px 0 4px}}
  .price-num span{{font-size:.82rem;color:var(--muted);font-weight:600;letter-spacing:0}}
  .feat-list{{margin:12px 0 18px;padding:0;list-style:none;font-size:.84rem;color:#cbd5e1}}
  .feat-list li{{padding:4px 0;border-bottom:1px solid rgba(148,163,184,.08)}}
  label{{display:block;font-size:.78rem;color:#cbd5e1;font-weight:600;margin-top:10px}}
  input,select,textarea{{width:100%;padding:10px 12px;border-radius:10px;border:1px solid rgba(148,163,184,.22);background:rgba(2,6,23,.85);color:#f8fafc;font-size:.88rem;margin-top:4px;font-family:inherit;outline:none}}
  input:focus,select:focus,textarea:focus{{border-color:#38bdf8}}
  code{{font-family:'JetBrains Mono',monospace;background:rgba(2,6,23,.85);padding:3px 7px;border-radius:6px;color:#6ee7b7;font-size:.8rem;word-break:break-all}}
  .ok{{color:#34d399;font-weight:700}} .err{{color:#fca5a5;font-weight:700}}
</style></head>
<body>
<header class="topbar">
  <div class="topbar-inner">
    <a class="brand" href="{base}/paygate"><span class="brand-mark">PG</span><span>PayGate USDT</span></a>
    <nav class="nav">
      <a href="{base}/">ChainGuard Studio</a>
      <a href="#invoice-link">{'Link Builder' if not is_es else 'Crear Link'}</a>
      <a href="#upgrade">{'Pricing' if not is_es else 'Planes'}</a>
      <a href="#setup">{'Telegram Bot' if not is_es else 'Bot Telegram'}</a>
      <a href="#b2b">{'Done-For-You' if not is_es else 'B2B a Medida'}</a>
      <a href="{base}/recover">Vault Recovery</a>
      <a href="https://github.com/EzequielLech/paygate-usdt" target="_blank">GitHub SDK</a>
      <a class="nav-pill" href="{base}/paygate/merchant">Merchant Dashboard</a>
      <a href="{base}/paygate?lang={alt_lang}">{alt_lbl}</a>
    </nav>
  </div>
</header>
<div class="wrap">
  <span class="badge">{t['badge']}</span>
  <h1>{t['h1']}</h1>
  <p class="sub">{t['sub']}</p>

  <div class="hero-actions">
    <a class="btn" href="#invoice-link">{t['cta1']}</a>
    <a class="btn2" href="#upgrade">{'Unlock Cloud Pro ($9/mo) or Lifetime ($79)' if not is_es else 'Activar Cloud Pro ($9/mes) o Licencia ($79)'}</a>
    <a class="btn2" href="{base}/paygate/merchant">{'Open Merchant Dashboard' if not is_es else 'Abrir Panel de Ventas'}</a>
    <a class="btn2" href="#setup">{t['cta2']}</a>
  </div>

  <div class="proof-strip">
    <div class="proof-item"><div class="proof-k">Settlement Cut</div><div class="proof-v">0% Gateway Fee</div></div>
    <div class="proof-item"><div class="proof-k">Supported Chains</div><div class="proof-v">7 Mainnets (EVM + SOL + BTC)</div></div>
    <div class="proof-item"><div class="proof-k">Checkout Speed</div><div class="proof-v">1-Click MetaMask &amp; Phantom</div></div>
    <div class="proof-item"><div class="proof-k">Distribution</div><div class="proof-v">Official MCP Registry &amp; MIT SDK</div></div>
  </div>

  <div class="card card-sky" id="invoice-link">
    <div class="kicker" style="color:#38bdf8;">{'INSTANT LIVE CHECKOUT BUILDER · 5 SECONDS' if not is_es else 'GENERADOR DE CHECKOUT EN VIVO · 5 SEGUNDOS'}</div>
    <h2 style="margin-top:6px;">{'Generate a Non-Custodial Crypto Checkout Link' if not is_es else 'Genera un Link de Cobro Cripto sin Custodia'}</h2>
    <p style="margin:0 0 14px;font-size:.88rem;color:#94a3b8;">{'Paste your wallet address and create a Stripe-grade hosted invoice page with 1-Click wallet pay, QR code, auto-detection, and optional HMAC-SHA256 webhooks. First 3 settled sales are completely free.' if not is_es else 'Pega tu wallet y genera un checkout profesional con pago en 1 clic, codigo QR, deteccion on-chain automatica y webhooks HMAC-SHA256. Tus primeras 3 ventas cobradas son gratis.'}</p>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;">
      <div>
        <label>{'Product or Service Title' if not is_es else 'Concepto / Producto'}</label>
        <input id="invTitle" placeholder="SaaS Pro Plan / Audit / VIP Access"/>
      </div>
      <div>
        <label>{'Amount (USD Equivalent)' if not is_es else 'Monto (Equivalente USD)'}</label>
        <input id="invAmt" type="number" step="0.5" value="29"/>
      </div>
      <div>
        <label>{'Default Settlement Chain' if not is_es else 'Red Principal (0% comision)'}</label>
        <select id="invNet">
          <option value="base">Base (USDC / USDT / ETH)</option>
          <option value="polygon">Polygon (USDT / USDC / POL)</option>
          <option value="arbitrum">Arbitrum (USDC / USDT / ETH)</option>
          <option value="bsc">BSC BEP-20 (USDT / USDC / BNB)</option>
          <option value="solana">Solana (USDC / USDT / SOL)</option>
          <option value="ethereum">Ethereum (USDC / USDT / ETH)</option>
          <option value="bitcoin">Bitcoin (BTC Taproot)</option>
        </select>
      </div>
      <div>
        <label>{'Your Payout Wallet (0x..., Solana, or BTC)' if not is_es else 'Tu Wallet Receptora (0x..., Solana o BTC)'}</label>
        <input id="invWallet" placeholder="0x..., Solana, or bc1p... BTC address"/>
      </div>
    </div>
    <label>{'Webhook Callback URL (optional — receives signed X-PayGate-Signature on settlement)' if not is_es else 'URL Webhook (opcional — recibe evento invoice.paid firmado)'}</label>
    <input id="invHook" placeholder="https://yourapp.com/webhooks/paygate"/>
    <div style="margin-top:14px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;">
      <button class="btn" onclick="createInstantInvoice()">{'Generate Hosted Checkout URL' if not is_es else 'Generar Link de Cobro'}</button>
      <span id="invCreateMsg" style="font-size:.88rem;"></span>
    </div>
    <div id="invCreateOut" style="margin-top:12px;"></div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="kicker">{t['c1k']}</div>
      <h3>{t['c1t']}</h3>
      <p style="color:#94a3b8;font-size:.88rem;margin:0;">{t['c1b']}</p>
    </div>
    <div class="card">
      <div class="kicker">{t['c2k']}</div>
      <h3>{t['c2t']}</h3>
      <p style="color:#94a3b8;font-size:.88rem;margin:0;">{t['c2b']}</p>
    </div>
    <div class="card">
      <div class="kicker">{t['c3k']}</div>
      <h3>{t['c3t']}</h3>
      <p style="color:#94a3b8;font-size:.88rem;margin:0;">{t['c3b']}</p>
    </div>
  </div>

  <div class="card" id="upgrade" style="margin-top:22px;">
    <div class="kicker">{'ZERO PLATFORM CUT · DIRECT ON-CHAIN ACTIVATION' if not is_es else 'CERO COMISION SOBRE VENTAS · ACTIVACION ON-CHAIN'}</div>
    <h2 style="margin-top:6px;">{t['up_h']}</h2>
    <div class="price-grid">
      <div class="card" style="background:rgba(5,8,17,.65);">
        <div class="kicker" style="color:#94a3b8;">{'STARTER CLOUD' if not is_es else 'INICIO NUBE'}</div>
        <div class="price-num">$0 <span>/ 3 settled sales</span></div>
        <ul class="feat-list">
          <li>Hosted 7-chain checkout pages</li>
          <li>1-Click MetaMask &amp; Phantom</li>
          <li>HMAC-SHA256 webhooks &amp; QR</li>
          <li>Self-host MIT SDK unlimited free</li>
        </ul>
        <a class="btn2" style="width:100%;" href="#invoice-link">{'Start Free Now' if not is_es else 'Empezar Gratis'}</a>
      </div>
      <div class="card card-featured" style="background:rgba(5,8,17,.65);">
        <div class="kicker">{'CLOUD PRO · MOST POPULAR' if not is_es else 'CLOUD PRO · MAS ELEGIDO'}</div>
        <div class="price-num">$9 <span>USDT / 30 days</span></div>
        <ul class="feat-list">
          <li>Unlimited hosted invoices &amp; sales</li>
          <li>0% cut on all volume forever</li>
          <li>Instant <code>cg_live_</code> API + MCP Pro Key</li>
          <li>24/7 RPC failover auto-detection</li>
        </ul>
        <a class="btn" style="width:100%;" href="{pro_checkout_url}">{'Unlock Cloud Pro ($9) ->' if not is_es else 'Activar Cloud Pro ($9) ->'}</a>
      </div>
      <div class="card card-sky" style="background:rgba(5,8,17,.65);">
        <div class="kicker" style="color:#38bdf8;">{'LIFETIME WHITE-LABEL' if not is_es else 'LICENCIA VITALICIA'}</div>
        <div class="price-num">$79 <span>USDT one-time</span></div>
        <ul class="feat-list">
          <li>Lifetime unlimited cloud + bots</li>
          <li>25,000-call Master API / MCP Key</li>
          <li>White-label branding removal</li>
          <li>Commercial resell &amp; agency rights</li>
        </ul>
        <a class="btn2" style="width:100%;border-color:rgba(56,189,248,.5);color:#7dd3fc;" href="{wl_checkout_url}">{'Buy Lifetime License ($79) ->' if not is_es else 'Comprar Licencia ($79) ->'}</a>
      </div>
    </div>
  </div>

  <div class="card" id="setup" style="margin-top:20px;border-color:rgba(16,185,129,.35);">
    <div class="kicker">{'TELEGRAM VIP AUTOMATION' if not is_es else 'AUTOMATIZACION TELEGRAM VIP'}</div>
    <h2 style="margin-top:6px;">{t['setup_h']}</h2>
    <p style="margin:0 0 12px;font-size:.88rem;color:#94a3b8;">{t['setup_p']}</p>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;">
      <div><label>{t['l_name']}</label><input id="bName" placeholder="Alpha Signals VIP"/></div>
      <div><label>{t['l_tok']}</label><input id="bToken" placeholder="1234567890:AAF..."/></div>
      <div>
        <label>{t['l_net']}</label>
        <select id="bNet">
          <option value="polygon">Polygon — USDT / USDC</option>
          <option value="bsc">BNB Smart Chain BEP20 — USDT / USDC</option>
          <option value="arbitrum">Arbitrum — USDT / USDC</option>
          <option value="base">Base — USDC / USDT</option>
          <option value="solana">Solana (Phantom) — USDC / USDT</option>
        </select>
      </div>
      <div><label>{t['l_wal']}</label><input id="bWallet" placeholder="0x... or Solana address"/></div>
      <div><label>{t['l_chan']}</label><input id="bChan" placeholder="-100..."/></div>
      <div><label>{t['l_price']}</label><input id="bPrice" type="number" step="0.5" value="25"/></div>
    </div>
    <label>{t['l_deliv']}</label>
    <input id="bDeliv" placeholder="https://... or instructions"/>
    <div style="margin-top:14px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;">
      <button class="btn" onclick="crearBot()">{t['btn_go']}</button>
      <a class="btn2" href="#demo">{'Simulate Buyer Flow' if not is_es else 'Simular Flujo de Compra'}</a>
      <span id="setupMsg" style="font-size:.88rem;"></span>
    </div>
    <div id="setupResult" style="margin-top:12px;"></div>
  </div>

  <div class="card card-amb" id="b2b" style="margin-top:20px;">
    <div class="kicker" style="color:#fbbf24;">{'DONE-FOR-YOU B2B DEPLOYMENT' if not is_es else 'IMPLEMENTACION B2B LLAVE EN MANO'}</div>
    <h2 style="margin-top:6px;">{'Need Custom Non-Custodial Billing or Bot Infrastructure?' if not is_es else 'Necesitas Infraestructura de Cobro o Bots a Medida?'}</h2>
    <p style="margin:0 0 14px;font-size:.88rem;color:#94a3b8;">{'Built for trading rooms, Web3 SaaS teams, and AI agent platforms replacing high-fee processors. Kick off directly with a 50% on-chain deposit.' if not is_es else 'Pensado para comunidades VIP, equipos SaaS Web3 y plataformas de agentes IA que buscan independizarse de pasarelas bancarias. Inicio inmediato con 50% de seña on-chain.'}</p>
    <div class="grid" style="margin:12px 0;">
      <div class="card" style="background:rgba(5,8,17,.7);border-color:rgba(245,158,11,.35);">
        <div class="kicker" style="color:#fbbf24;">{'TURNKEY VIP GATE (24H)' if not is_es else 'VIP GATE LLAVE EN MANO (24H)'}</div>
        <div class="price-num">$199 <span>USDT ($99 deposit)</span></div>
        <p style="font-size:.85rem;color:#94a3b8;margin:0 0 14px;">{PAYGATE_B2B_PACKAGES['turnkey_199']['summary_es' if is_es else 'summary_en']}</p>
        <a class="btn2" style="width:100%;border-color:rgba(245,158,11,.5);color:#fcd34d;" href="{turnkey_checkout_url}">{'1-Click Pay $99 Kickoff Deposit ->' if not is_es else 'Pagar Seña $99 en 1 Clic ->'}</a>
      </div>
      <div class="card" style="background:rgba(5,8,17,.7);border-color:rgba(56,189,248,.35);">
        <div class="kicker" style="color:#38bdf8;">{'CUSTOM BOT + API / EXCHANGE' if not is_es else 'BOT A MEDIDA + API / EXCHANGE'}</div>
        <div class="price-num">$499 <span>USDT ($249 deposit)</span></div>
        <p style="font-size:.85rem;color:#94a3b8;margin:0 0 14px;">{PAYGATE_B2B_PACKAGES['custom_499']['summary_es' if is_es else 'summary_en']}</p>
        <a class="btn2" style="width:100%;border-color:rgba(56,189,248,.5);color:#7dd3fc;" href="{custom_checkout_url}">{'1-Click Pay $249 Kickoff Deposit ->' if not is_es else 'Pagar Seña $249 en 1 Clic ->'}</a>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;">
      <div>
        <label>{'Solution Package' if not is_es else 'Solucion a contratar'}</label>
        <select id="b2bPkg">
          <option value="turnkey_199">{'Turnkey VIP Gate Setup — $199 USDT ($99 deposit)' if not is_es else 'VIP Gate Llave en Mano — $199 USDT ($99 seña)'}</option>
          <option value="custom_499">{'Custom Crypto Bot + Exchange/API — $499 USDT ($249 deposit)' if not is_es else 'Bot Cripto + API/Exchange a Medida — $499 USDT ($249 seña)'}</option>
        </select>
      </div>
      <div><label>{'Your Name / Brand' if not is_es else 'Tu Nombre / Marca'}</label><input id="b2bName" placeholder="e.g. Alpha Desk / Studio"/></div>
      <div><label>{'Telegram @handle or Email' if not is_es else 'Tu @usuario Telegram o Email'}</label><input id="b2bContact" placeholder="@yourhandle or founder@domain.com"/></div>
    </div>
    <label>{'Project URL / Channel & Integration Scope' if not is_es else 'Link o detalle de que necesitas automatizar'}</label>
    <textarea id="b2bScope" rows="2" placeholder="{'e.g. 400-member VIP Telegram channel + webhook to CRM' if not is_es else 'Ej: canal VIP de señales cobrando USDT + expulsion automatica'}"></textarea>
    <div style="margin-top:12px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;">
      <button class="btn" style="background:linear-gradient(135deg,#f59e0b,#eab308);color:#1f1300;" onclick="createB2BOrder()">{'Log Scope & Get Direct Deposit Invoice' if not is_es else 'Registrar Proyecto y Ver Factura USDT'}</button>
      <span id="b2bMsg" style="font-size:.88rem;"></span>
    </div>
    <div id="b2bOrderBox" style="margin-top:12px;"></div>
  </div>

  <div class="card" id="demo" style="margin-top:20px;">
    <div class="kicker" style="color:#38bdf8;">{'INTERACTIVE SIMULATOR' if not is_es else 'SIMULADOR INTERACTIVO'}</div>
    <h2 style="margin-top:6px;">{'Interactive Buyer Experience (10s Preview)' if not is_es else 'Vista Previa del Flujo de Compra (10s)'}</h2>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;">
      <div>
        <label>{'Demo Plan' if not is_es else 'Plan Demo'}</label>
        <select id="demoPlan">
          <option value="29">{'VIP Monthly (30d) — $29 USDT' if not is_es else 'VIP Mensual (30d) — $29 USDT'}</option>
          <option value="79">{'VIP Lifetime — $79 USDT' if not is_es else 'VIP Vitalicio — $79 USDT'}</option>
        </select>
      </div>
      <div>
        <label>{'Buyer Chain' if not is_es else 'Red del Comprador'}</label>
        <select id="demoChain">
          <option value="Base">Base (USDC / USDT / ETH)</option>
          <option value="Polygon">Polygon (USDT / USDC / POL)</option>
          <option value="BSC BEP20">BNB Smart Chain BEP20 (USDT / USDC / BNB)</option>
          <option value="Arbitrum">Arbitrum One (USDT / USDC / ETH)</option>
          <option value="Solana">Solana (USDC / USDT / SOL)</option>
        </select>
      </div>
      <div style="display:flex;align-items:flex-end;">
        <button class="btn2" style="width:100%;border-color:rgba(56,189,248,.45);color:#7dd3fc;" onclick="runDemoFlow()">{'Simulate Instant Settlement' if not is_es else 'Simular Cobro + Acceso VIP'}</button>
      </div>
    </div>
    <div id="demoOutput" style="margin-top:12px;"></div>
  </div>
</div>
<script>
async function crearBot() {{
  const msg = document.getElementById('setupMsg');
  const out = document.getElementById('setupResult');
  msg.className = ''; msg.textContent = 'Connecting to Telegram...';
  const r = await fetch('/api/v1/paygate/bots', {{
    method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{
      brand_name: document.getElementById('bName').value.trim(),
      bot_token: document.getElementById('bToken').value.trim(),
      payout_network: document.getElementById('bNet').value,
      payout_wallet: document.getElementById('bWallet').value.trim(),
      channel_id: document.getElementById('bChan').value.trim(),
      monthly_price_usd: parseFloat(document.getElementById('bPrice').value || '25'),
      delivery_text: document.getElementById('bDeliv').value.trim()
    }})
  }});
  const d = await r.json();
  if (!r.ok) {{
    msg.className = 'err';
    msg.textContent = d.detail || 'Setup error.';
    return;
  }}
  msg.className = 'ok';
  msg.textContent = 'Bot live!';
  const panelUrl = location.origin + d.panel_url;
  out.innerHTML = `<div class="card" style="background:rgba(5,46,22,.5);border-color:#10b981;">
    <div><strong>@${{d.bot.bot_username}}</strong> is live: <a style="color:#6ee7b7;font-weight:800;" href="https://t.me/${{d.bot.bot_username}}" target="_blank">t.me/${{d.bot.bot_username}}</a></div>
    <div style="margin-top:8px;"><strong>Save your private Admin Panel link:</strong><br/><code>${{panelUrl}}</code></div>
    <div style="margin-top:10px;"><a class="btn" href="${{panelUrl}}">Open Admin Panel -&gt;</a></div>
  </div>`;
}}

function runDemoFlow() {{
  const amt = document.getElementById('demoPlan').value;
  const ch = document.getElementById('demoChain').value;
  const out = document.getElementById('demoOutput');
  out.innerHTML = `<div style="background:rgba(2,6,23,.8);border:1px solid rgba(56,189,248,.4);border-radius:12px;padding:14px;font-size:.86rem;">
    <div><strong>Step 1 (Buyer):</strong> Selects <strong>$${{amt}} USD</strong> via <strong>${{ch}}</strong> and clicks 1-Click Pay in MetaMask or Phantom.</div>
    <div style="margin-top:6px;"><strong>Step 2 (Direct Settlement):</strong> Funds transfer straight to <em>your self-custody wallet</em> with 0% gateway fee.</div>
    <div style="margin-top:6px;"><strong>Step 3 (RPC Finality in ~3s):</strong> Anti-replay check passes, signed HMAC-SHA256 webhook fires, and buyer receives instant license key or 1-use VIP invite link.</div>
  </div>`;
}}

async function createB2BOrder() {{
  const msg = document.getElementById('b2bMsg');
  const box = document.getElementById('b2bOrderBox');
  msg.className = '';
  msg.textContent = 'Creating order...';
  const r = await fetch('/api/v1/paygate/b2b-order', {{
    method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{
      package_id: document.getElementById('b2bPkg').value,
      client_name: document.getElementById('b2bName').value.trim(),
      contact_handle: document.getElementById('b2bContact').value.trim(),
      requirements: document.getElementById('b2bScope').value.trim()
    }})
  }});
  const d = await r.json();
  if (!r.ok) {{
    msg.className = 'err';
    msg.textContent = d.detail || 'Validation error.';
    return;
  }}
  msg.className = 'ok';
  msg.textContent = 'Order created: ' + d.order.order_id;
  const ord = d.order;
  box.innerHTML = `<div class="card" style="background:rgba(2,6,23,.85);border-color:#f59e0b;">
    <div><strong>Order ID:</strong> <code>${{ord.order_id}}</code> · <strong>${{ord.package_name}}</strong></div>
    <div style="margin-top:6px;font-size:.88rem;">Send <strong>$${{ord.min_deposit_usd}} USDT/USDC</strong> (50% deposit) or full <strong>$${{ord.full_price_usd}} USDT/USDC</strong>:</div>
    <div style="margin-top:6px;font-size:.82rem;">EVM (Base / Polygon / Arbitrum / BSC / ETH): <code>${{d.wallets.evm}}</code></div>
    <div style="margin-top:4px;font-size:.82rem;">Solana (SPL USDC/USDT/SOL): <code>${{d.wallets.solana}}</code></div>
    <div style="display:grid;grid-template-columns:160px 1fr auto;gap:8px;margin-top:10px;">
      <select id="b2bNet"><option value="base">Base</option><option value="polygon">Polygon</option><option value="bsc">BSC BEP20</option><option value="arbitrum">Arbitrum</option><option value="solana">Solana</option><option value="ethereum">Ethereum</option></select>
      <input id="b2bTx" placeholder="Paste TX Hash (0x... or Solana signature)"/>
      <button class="btn" onclick="verifyB2BOrder('${{ord.order_id}}')">Verify Deposit</button>
    </div>
    <div id="b2bVerifyMsg" style="margin-top:8px;font-size:.86rem;"></div>
  </div>`;
}}

async function verifyB2BOrder(orderId) {{
  const vm = document.getElementById('b2bVerifyMsg');
  vm.className = '';
  vm.textContent = 'Verifying on-chain...';
  const r = await fetch('/api/v1/paygate/b2b-verify', {{
    method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{
      order_id: orderId,
      network: document.getElementById('b2bNet').value,
      tx_hash: document.getElementById('b2bTx').value.trim()
    }})
  }});
  const d = await r.json();
  if (!r.ok) {{
    vm.className = 'err';
    vm.textContent = d.detail || 'TX verification failed.';
    return;
  }}
  vm.className = 'ok';
  vm.textContent = 'Deposit verified on-chain! Status: ' + d.order.status + '. Delivery kickoff confirmed.';
}}

async function createInstantInvoice() {{
  const m = document.getElementById('invCreateMsg');
  const out = document.getElementById('invCreateOut');
  m.className = ''; m.textContent = 'Creating hosted checkout...';
  const r = await fetch('/api/v1/paygate/invoices', {{
    method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{
      title: document.getElementById('invTitle').value.trim() || 'Digital Service',
      amount_usd: parseFloat(document.getElementById('invAmt').value || '29'),
      network: document.getElementById('invNet').value,
      merchant_wallet: document.getElementById('invWallet').value.trim(),
      webhook_url: document.getElementById('invHook').value.trim()
    }})
  }});
  const d = await r.json();
  if (!r.ok) {{ m.className = 'err'; m.textContent = d.detail || 'Error creating invoice.'; return; }}
  m.className = 'ok'; m.textContent = 'Checkout ready!';
  out.innerHTML = `<div class="card" style="background:rgba(5,46,22,.5);border-color:#10b981;">
    <div><strong>Hosted Checkout Link Ready:</strong> <a style="color:#6ee7b7;font-weight:800;" href="${{d.checkout_url}}" target="_blank">${{d.checkout_url}}</a></div>
    <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
      <a class="btn" href="${{d.checkout_url}}" target="_blank">Open Live Checkout -&gt;</a>
      <button class="btn2" onclick="navigator.clipboard.writeText('${{d.checkout_url}}');this.textContent='Copied Link'">Copy Link</button>
      <span style="font-size:.8rem;color:#94a3b8;">ID: <code>${{d.invoice.invoice_id}}</code></span>
    </div>
  </div>`;
}}
</script></body></html>"""


def render_paygate_panel_html(base: str, bot: dict[str, Any], members: list[dict[str, Any]]) -> str:
    bid = html.escape(bot["bot_id"])
    sec = html.escape(bot["admin_secret"])
    brand = html.escape(bot["brand_name"])
    buser = html.escape(bot["bot_username"] or "tu_bot")
    tier = html.escape(bot["license_tier"].upper())
    active_m = [m for m in members if m["status"] == "active"]
    total_usdt = round(sum(float(m["amount_usd"]) for m in members), 2)
    plans = bot.get("plans") or []
    p0_price = float(plans[0]["price_usd"]) if plans else 25.0

    rows_html = ""
    for m in members[:25]:
        rows_html += (
            f"<tr><td>@{html.escape(m['tg_username'] or m['tg_user_id'])}</td>"
            f"<td>{html.escape(m['plan_name'])}</td>"
            f"<td>${float(m['amount_usd']):.2f}</td>"
            f"<td>{html.escape(m['status'])}</td>"
            f"<td>{html.escape(m['expires_at'][:10])}</td></tr>"
        )
    if not rows_html:
        rows_html = "<tr><td colspan='5' style='color:#94a3b8;'>Todavia no hay miembros pagos. Comparti tu link t.me/" + buser + "</td></tr>"

    net_opts = "".join(
        f"<option value='{k}' {'selected' if k == bot['payout_network'] else ''}>{html.escape(v['label'])}</option>"
        for k, v in SUPPORTED_NETWORKS.items()
    )

    return f"""<!DOCTYPE html><html lang="es"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Panel {brand} — PayGate USDT</title>
<style>
  *{{box-sizing:border-box}} body{{margin:0;background:#070b14;color:#f1f5f9;font-family:Inter,system-ui,sans-serif;padding:20px 14px}}
  .wrap{{max-width:920px;margin:0 auto}} .card{{background:#0f172a;border:1px solid #1e293b;border-radius:14px;padding:16px;margin-bottom:14px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-bottom:14px}}
  .kpi{{background:#070b14;border:1px solid #1e293b;border-radius:10px;padding:12px}}
  .kpi .v{{font-size:1.55rem;font-weight:900;color:#10b981}}
  label{{display:block;font-size:.8rem;color:#cbd5e1;font-weight:700;margin-top:9px}}
  input,select{{width:100%;padding:9px 11px;border-radius:8px;border:1px solid #334155;background:#070b14;color:#f8fafc;margin-top:4px}}
  .btn{{background:#10b981;color:#02160f;font-weight:800;border:none;padding:10px 16px;border-radius:9px;cursor:pointer}}
  table{{width:100%;border-collapse:collapse;font-size:.86rem}} th,td{{text-align:left;padding:8px;border-bottom:1px solid #1e293b}}
  code{{background:#020617;padding:2px 6px;border-radius:5px;color:#6ee7b7;word-break:break-all}}
</style></head>
<body><div class="wrap">
  <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:14px;">
    <div>
      <span style="font-size:.76rem;color:#10b981;font-weight:800;">PAYGATE USDT · PLAN {tier}</span>
      <h2 style="margin:2px 0 0;">{brand} (<a href="https://t.me/{buser}" target="_blank" style="color:#6ee7b7;">@{buser}</a>)</h2>
    </div>
    <a href="{base}/paygate" style="color:#94a3b8;font-size:.86rem;">← Volver a PayGate</a>
  </div>

  <div class="grid">
    <div class="kpi"><div style="font-size:.78rem;color:#94a3b8;">Ingresos Verificados</div><div class="v">${total_usdt} USDT</div></div>
    <div class="kpi"><div style="font-size:.78rem;color:#94a3b8;">Miembros Activos</div><div class="v">{len(active_m)}</div></div>
    <div class="kpi"><div style="font-size:.78rem;color:#94a3b8;">Red de Cobro</div><div class="v" style="font-size:1.15rem;">{html.escape(bot['payout_network'].upper())}</div></div>
    <div class="kpi"><div style="font-size:.78rem;color:#94a3b8;">Tu Licencia</div><div class="v" style="font-size:1.15rem;">{tier}</div></div>
  </div>

  <div class="card">
    <h3 style="margin:0 0 8px;">⚙️ Configuracion del Bot y Wallet de Cobro</h3>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px;">
      <div><label>Nombre</label><input id="cBrand" value="{brand}"/></div>
      <div><label>Red de cobro</label><select id="cNet">{net_opts}</select></div>
      <div><label>Wallet destino</label><input id="cWal" value="{html.escape(bot['payout_wallet'])}"/></div>
      <div><label>ID Canal/Grupo (-100...)</label><input id="cChan" value="{html.escape(bot['channel_id'])}"/></div>
      <div><label>Precio Mensual (USDT)</label><input id="cPrice" type="number" step="0.5" value="{p0_price:g}"/></div>
      <div><label>Link/Entrega extra</label><input id="cDeliv" value="{html.escape(bot['delivery_text'])}"/></div>
    </div>
    <div style="margin-top:12px;display:flex;gap:10px;align-items:center;">
      <button class="btn" onclick="guardar()">Guardar Cambios</button>
      <span id="saveMsg" style="font-size:.86rem;"></span>
    </div>
  </div>

  <div class="card">
    <h3 style="margin:0 0 8px;">👥 Suscriptores y Pagos</h3>
    <table><thead><tr><th>Usuario</th><th>Plan</th><th>Pago</th><th>Estado</th><th>Vence</th></tr></thead><tbody>{rows_html}</tbody></table>
  </div>

  <div class="card" style="border-color:#38bdf8;">
    <h3 style="margin:0 0 6px;">👑 Desbloquear Miembros Ilimitados ($29 USDT Pro / $79 USDT Unico)</h3>
    <p style="font-size:.85rem;color:#94a3b8;margin:0 0 10px;">Envia $29 o $79 (USDT/USDC) por Polygon/BSC/Arbitrum/Base a <code>{html.escape(StudioConfig.PHANTOM_EVM_ADDRESS)}</code> (o Solana <code>{html.escape(StudioConfig.PHANTOM_SOL_ADDRESS)}</code>) y pega tu Tx Hash abajo:</p>
    <div style="display:flex;gap:8px;flex-wrap:wrap;">
      <select id="uNet" style="flex:1;min-width:140px;"><option value="polygon">Polygon</option><option value="bsc">BSC</option><option value="arbitrum">Arbitrum</option><option value="base">Base</option><option value="solana">Solana</option></select>
      <input id="uTx" style="flex:2;min-width:220px;" placeholder="0x... o firma Solana"/>
      <button class="btn" onclick="activarPro()">Verificar y Activar</button>
    </div>
    <div id="upMsg" style="margin-top:8px;font-size:.86rem;"></div>
  </div>
</div>
<script>
const BID = {json.dumps(bid)}, SEC = {json.dumps(sec)};
async function guardar() {{
  const r = await fetch('/api/v1/paygate/bots/' + BID + '/update', {{
    method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{
      admin_secret: SEC,
      brand_name: document.getElementById('cBrand').value.trim(),
      payout_network: document.getElementById('cNet').value,
      payout_wallet: document.getElementById('cWal').value.trim(),
      channel_id: document.getElementById('cChan').value.trim(),
      monthly_price_usd: parseFloat(document.getElementById('cPrice').value || '25'),
      delivery_text: document.getElementById('cDeliv').value.trim()
    }})
  }});
  const m = document.getElementById('saveMsg');
  const d = await r.json();
  m.style.color = r.ok ? '#34d399' : '#fca5a5';
  m.textContent = r.ok ? '¡Guardado!' : (d.detail || 'Error');
}}
async function activarPro() {{
  const m = document.getElementById('upMsg');
  m.style.color = '#38bdf8'; m.textContent = 'Verificando on-chain...';
  const r = await fetch('/api/v1/paygate/bots/' + BID + '/upgrade', {{
    method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ admin_secret: SEC, network: document.getElementById('uNet').value, tx_hash: document.getElementById('uTx').value.trim() }})
  }});
  const d = await r.json();
  if (r.ok) location.reload();
  else {{ m.style.color = '#fca5a5'; m.textContent = d.detail || 'No verificado'; }}
}}
</script></body></html>"""


def render_paygate_merchant_dashboard_html(
    base: str,
    wallet: str,
    invoices: list[dict[str, Any]],
    quota_status: dict[str, Any] | None = None,
) -> str:
    """Self-serve non-custodial Merchant Revenue Dashboard & Python/TypeScript/HTML snippet generator."""
    clean_wal = wallet.strip()
    snippet_wal = clean_wal or "0xYourWalletAddress"
    wal_esc = html.escape(clean_wal)
    snip_esc = html.escape(snippet_wal)
    s_evm = StudioConfig.PHANTOM_EVM_ADDRESS or "0x4Ed4D0750453C027FA8398067d5af980Bcc9B6eD"

    paid_invs = [i for i in invoices if i.get("status") == "paid"]
    pending_invs = [i for i in invoices if i.get("status") == "pending"]
    expired_invs = [i for i in invoices if i.get("status") == "expired"]
    total_settled = round(sum(float(i.get("paid_amount_usd") or i.get("amount_usd") or 0.0) for i in paid_invs), 2)

    qs = quota_status or {}
    quota_banner = ""
    if clean_wal and qs:
        q_tier = str(qs.get("tier") or "free_cloud")
        q_paid = int(qs.get("paid_count") or len(paid_invs))
        if q_tier in ("pro_licensed", "studio"):
            quota_banner = (
                '<div class="card" style="border-color:#10b981;background:#052e16;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">'
                '<div><strong>CLOUD PLAN ACTIVE:</strong> Unlimited hosted invoices, webhooks, and 0% settlement fees enabled for this wallet.</div>'
                '</div>'
            )
        else:
            q_lim = int(qs.get("limit") or FREE_CLOUD_SETTLEMENTS_LIMIT)
            up9 = html.escape(str(qs.get("upgrade_pro_9_url") or f"{base}/paygate/pay?merchant={s_evm}&amount=9&network=base&issue_pass=1&billing_days=30"))
            up79 = html.escape(str(qs.get("upgrade_lifetime_79_url") or f"{base}/paygate/pay?merchant={s_evm}&amount=79&network=polygon&issue_pass=1&billing_days=3650"))
            border_c = "#ef4444" if not qs.get("allowed", True) else "#f59e0b"
            quota_banner = (
                f'<div class="card" style="border-color:{border_c};display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">'
                f'<div><strong>Hosted Cloud Quota:</strong> {q_paid} / {q_lim} free settled sales used for this wallet. Unlock unlimited cloud settlement anytime (or self-host MIT free):</div>'
                f'<div style="display:flex;gap:8px;flex-wrap:wrap;"><a class="btn2" style="border-color:#a855f7;color:#d8b4fe;" href="{up9}">Unlock Cloud Pro ($9/mo)</a>'
                f'<a class="btn" href="{up79}">Lifetime White-Label ($79)</a></div>'
                '</div>'
            )

    rows_html = ""
    for inv in invoices[:50]:
        inv_id = str(inv.get("invoice_id") or "")
        inv_id_esc = html.escape(inv_id)
        title_esc = html.escape(str(inv.get("title") or ""))
        net_raw = str(inv.get("network") or "").lower()
        net_esc = html.escape(net_raw.upper())
        status_raw = str(inv.get("status") or "pending").lower()
        amt_val = float(inv.get("paid_amount_usd") or inv.get("amount_usd") or 0.0) if status_raw == "paid" else float(inv.get("amount_usd") or 0.0)
        tx = str(inv.get("tx_hash") or "")
        exp_prefix = _EXPLORER_TX_URLS.get(net_raw, "")
        tx_link = f'<a href="{html.escape(exp_prefix + tx)}" target="_blank" style="color:#38bdf8;">Explorer</a>' if (tx and exp_prefix) else "-"
        status_color = "#10b981" if status_raw == "paid" else ("#f59e0b" if status_raw == "pending" else "#94a3b8")
        rows_html += (
            f"<tr>"
            f"<td><code>{inv_id_esc}</code></td>"
            f"<td>{title_esc}</td>"
            f"<td><strong>${amt_val:.2f}</strong></td>"
            f"<td>{net_esc}</td>"
            f"<td style='color:{status_color};font-weight:800;'>{html.escape(status_raw.upper())}</td>"
            f"<td><a href='{base}/paygate/checkout/{inv_id_esc}' target='_blank' style='color:#6ee7b7;'>Checkout</a> · "
            f"<a href='{base}/paygate/receipt/{inv_id_esc}' target='_blank' style='color:#cbd5e1;'>Receipt</a> · {tx_link}</td>"
            f"</tr>"
        )
    if not rows_html:
        empty_msg = (
            f"No invoices recorded yet for <code>{wal_esc}</code>. Create your first 1-click checkout link below."
            if clean_wal
            else "Enter your EVM (0x...) or Solana wallet above to load settled volume, invoices, and pre-filled integration snippets."
        )
        rows_html = f"<tr><td colspan='6' style='color:#94a3b8;'>{empty_msg}</td></tr>"

    py_snippet = html.escape(
        "from paygate_sdk import mcp_tool_paywall, require_usdt_payment\n\n"
        "@mcp_tool_paywall(\n"
        "    price_usd=9.0,\n"
        f'    merchant_wallet="{snippet_wal}",\n'
        '    network="base",\n'
        "    free_calls=3,\n"
        "    billing_days=30,\n"
        '    title="Pro MCP Pass",\n'
        ")\n"
        "def pro_tool(query: str, customer_ref: str = \"\", paygate_invoice_id: str = \"\"):\n"
        "    return {\"ok\": True, \"query\": query}\n\n"
        "@app.get(\"/api/pro\")\n"
        "@require_usdt_payment(price_usd=5.0, recipient_wallet=\"" + snippet_wal + "\")\n"
        "async def pro_api(request):\n"
        "    return {\"ok\": True}"
    )

    ts_snippet = html.escape(
        'import { withMcpToolPaywall, paygateExpressMiddleware } from "./sdk-js/index.mjs";\n\n'
        "export const proTool = withMcpToolPaywall(\n"
        "  {\n"
        "    priceUsd: 9.0,\n"
        f'    merchantWallet: "{snippet_wal}",\n'
        '    network: "base",\n'
        "    freeCalls: 3,\n"
        "    billingDays: 30,\n"
        '    title: "Pro MCP Pass",\n'
        "  },\n"
        "  async ({ query }) => ({ ok: true, query })\n"
        ");\n\n"
        'app.get("/api/pro", paygateExpressMiddleware({ priceUsd: 5.0, merchantWallet: "' + snippet_wal + '" }), (req, res) => {\n'
        "  res.json({ ok: true, paygate: req.paygate });\n"
        "});"
    )

    widget_snippet = html.escape(
        f'<a data-paygate-merchant="{snippet_wal}" data-paygate-amount="29" data-paygate-network="base" data-paygate-title="Pro Access">\n'
        "  Pay $29 USDC/USDT\n"
        "</a>\n"
        f'<script src="{base}/paygate/widget.js"></script>'
    )

    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Merchant Revenue Dashboard — PayGate USDT</title>
<style>
  *{{box-sizing:border-box}} body{{margin:0;background:#070b14;color:#f1f5f9;font-family:Inter,system-ui,sans-serif;line-height:1.55}}
  .wrap{{max-width:1040px;margin:0 auto;padding:24px 16px 64px}}
  .card{{background:#0f172a;border:1px solid #1e293b;border-radius:14px;padding:18px;margin-bottom:16px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:16px}}
  .kpi{{background:#070b14;border:1px solid #1e293b;border-radius:10px;padding:14px}}
  .kpi .v{{font-size:1.65rem;font-weight:900;color:#10b981;margin-top:4px}}
  .btn{{display:inline-block;background:#10b981;color:#02160f;font-weight:800;padding:10px 18px;border-radius:10px;border:none;cursor:pointer;text-decoration:none;font-size:.92rem}}
  .btn2{{display:inline-block;background:#1e293b;color:#f8fafc;font-weight:700;padding:8px 14px;border-radius:10px;border:1px solid #334155;cursor:pointer;text-decoration:none;font-size:.84rem}}
  label{{display:block;font-size:.82rem;color:#cbd5e1;font-weight:700;margin-top:10px}}
  input,select{{width:100%;padding:10px 12px;border-radius:9px;border:1px solid #334155;background:#070b14;color:#f8fafc;font-size:.92rem;margin-top:4px}}
  table{{width:100%;border-collapse:collapse;font-size:.85rem}} th,td{{text-align:left;padding:9px 8px;border-bottom:1px solid #1e293b}}
  pre{{background:#020617;border:1px solid #1e293b;border-radius:10px;padding:12px;overflow-x:auto;font-size:.8rem;color:#6ee7b7;margin:8px 0 0}}
  code{{background:#020617;padding:2px 6px;border-radius:5px;color:#6ee7b7;font-size:.82rem;word-break:break-all}}
</style></head><body><div class="wrap">
  <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:16px;">
    <div>
      <div style="font-size:.76rem;color:#10b981;font-weight:800;">NON-CUSTODIAL MERCHANT REVENUE DASHBOARD</div>
      <h1 style="margin:4px 0 0;font-size:1.6rem;">PayGate USDT Self-Serve Control Center</h1>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;">
      <a class="btn2" href="{base}/paygate">PayGate Home</a>
      <a class="btn2" href="https://github.com/EzequielLech/paygate-usdt" target="_blank">GitHub SDK (Python + TS)</a>
    </div>
  </div>

  <div class="card" style="border-color:#38bdf8;">
    <form method="get" action="{base}/paygate/merchant" style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;">
      <div style="flex:1;min-width:260px;">
        <label style="margin-top:0;">Inspect Merchant Payout Wallet (0x... EVM or Solana, Zero Signup)</label>
        <input name="wallet" value="{wal_esc}" placeholder="0x... or Solana address"/>
      </div>
      <button type="submit" class="btn">Load Wallet Ledger</button>
    </form>
  </div>

  <div class="grid">
    <div class="kpi"><div style="font-size:.78rem;color:#94a3b8;">Total Settled Volume</div><div class="v">${total_settled:.2f} USDT</div></div>
    <div class="kpi"><div style="font-size:.78rem;color:#94a3b8;">Paid Invoices</div><div class="v">{len(paid_invs)}</div></div>
    <div class="kpi"><div style="font-size:.78rem;color:#94a3b8;">Pending Invoices</div><div class="v" style="color:#fcd34d;">{len(pending_invs)}</div></div>
    <div class="kpi"><div style="font-size:.78rem;color:#94a3b8;">Expired / Total</div><div class="v" style="color:#cbd5e1;">{len(expired_invs)} / {len(invoices)}</div></div>
  </div>
  {quota_banner}

  <div class="card">
    <h2 style="margin:0 0 10px;font-size:1.15rem;">Invoice Ledger ({html.escape(clean_wal) if clean_wal else 'No wallet selected'})</h2>
    <div style="overflow-x:auto;">
      <table>
        <thead><tr><th>Invoice ID</th><th>Title</th><th>Amount</th><th>Network</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
  </div>

  <div class="card" style="border-color:#10b981;">
    <h2 style="margin:0 0 6px;font-size:1.15rem;">1-Click Payment Link Creator</h2>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;">
      <div><label>Title</label><input id="mTitle" value="Pro Access"/></div>
      <div><label>Amount (USD)</label><input id="mAmt" type="number" step="0.5" value="29"/></div>
      <div><label>Network</label><select id="mNet"><option value="base">Base (USDC/ETH)</option><option value="polygon">Polygon (USDT/POL)</option><option value="arbitrum">Arbitrum (USDC/ETH)</option><option value="bsc">BSC BEP20 (USDT/BNB)</option><option value="solana">Solana (USDC/SOL)</option><option value="ethereum">Ethereum (ETH/USDC)</option><option value="bitcoin">Bitcoin (BTC)</option></select></div>
      <div><label>Merchant Wallet</label><input id="mWal" value="{snip_esc}"/></div>
    </div>
    <div style="margin-top:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
      <button class="btn" onclick="createDashInvoice()">Create Hosted Checkout</button>
      <span id="mMsg" style="font-size:.88rem;color:#38bdf8;"></span>
    </div>
    <div id="mOut" style="margin-top:10px;"></div>
  </div>

  <div class="card">
    <h2 style="margin:0 0 6px;font-size:1.15rem;">Copy-Paste Integration Snippets (Pre-Filled for {snip_esc})</h2>
    <div style="margin-top:12px;">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <strong style="font-size:.9rem;color:#7dd3fc;">Python SDK (@mcp_tool_paywall + @require_usdt_payment)</strong>
        <button class="btn2" onclick="copyPre('pySnip', this)">Copy Python</button>
      </div>
      <pre id="pySnip">{py_snippet}</pre>
    </div>
    <div style="margin-top:14px;">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <strong style="font-size:.9rem;color:#6ee7b7;">TypeScript / Node.js SDK (withMcpToolPaywall + paygateExpressMiddleware)</strong>
        <button class="btn2" onclick="copyPre('tsSnip', this)">Copy TypeScript</button>
      </div>
      <pre id="tsSnip">{ts_snippet}</pre>
    </div>
    <div style="margin-top:14px;">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <strong style="font-size:.9rem;color:#fcd34d;">HTML / Markdown 1-Line Embed Widget</strong>
        <button class="btn2" onclick="copyPre('wSnip', this)">Copy HTML</button>
      </div>
      <pre id="wSnip">{widget_snippet}</pre>
    </div>
  </div>

  <div class="card" style="border-color:#f59e0b;">
    <div style="font-size:.84rem;color:#f59e0b;font-weight:800;">UPGRADE &amp; HOSTED PASSES (INSTANT NON-CUSTODIAL DELIVERY)</div>
    <p style="font-size:.88rem;color:#cbd5e1;margin:6px 0 10px;">Unlock unlimited high-rate RPC verification, full vulnerability remediation patches, and hosted infrastructure:</p>
    <div style="display:flex;gap:8px;flex-wrap:wrap;">
      <a class="btn2" style="border-color:#38bdf8;color:#7dd3fc;" href="{base}/paygate/pay?merchant={s_evm}&amount=0.5&network=base&title=ChainGuard+24h+Micro+Unlock+Pass&issue_pass=1&billing_days=1">$0.50 · 24h Micro Pass</a>
      <a class="btn2" style="border-color:#10b981;color:#6ee7b7;" href="{base}/paygate/pay?merchant={s_evm}&amount=2&network=base&title=ChainGuard+7-Day+Starter+MCP+Pass&issue_pass=1&billing_days=7">$2.00 · 7-Day Starter Pass</a>
      <a class="btn2" style="border-color:#a855f7;color:#d8b4fe;" href="{base}/paygate/pay?merchant={s_evm}&amount=9&network=base&title=ChainGuard+Unlimited+MCP+Pass&issue_pass=1&billing_days=30">$9.00 · 30-Day Unlimited</a>
      <a class="btn2" style="border-color:#f59e0b;color:#fcd34d;" href="{base}/paygate/pay?merchant={s_evm}&amount=79&network=polygon&title=PayGate+Lifetime+White-Label+License&issue_pass=1&billing_days=3650">$79.00 · Lifetime White-Label</a>
      <a class="btn" href="{base}/paygate#b2b">$199 · Turnkey B2B Deployment</a>
    </div>
  </div>
</div>
<script>
function copyPre(id, btn) {{
  const el = document.getElementById(id);
  if (!el) return;
  navigator.clipboard.writeText(el.textContent || '');
  btn.textContent = 'Copied!';
  setTimeout(() => {{ btn.textContent = 'Copy'; }}, 1800);
}}
async function createDashInvoice() {{
  const msg = document.getElementById('mMsg');
  const out = document.getElementById('mOut');
  msg.textContent = 'Creating invoice...';
  const r = await fetch('/api/v1/paygate/invoices', {{
    method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{
      title: document.getElementById('mTitle').value.trim() || 'Pro Access',
      amount_usd: parseFloat(document.getElementById('mAmt').value || '29'),
      network: document.getElementById('mNet').value,
      merchant_wallet: document.getElementById('mWal').value.trim()
    }})
  }});
  const d = await r.json();
  if (!r.ok) {{ msg.style.color = '#fca5a5'; msg.textContent = d.detail || 'Error creating invoice.'; return; }}
  msg.style.color = '#34d399'; msg.textContent = 'Checkout link ready!';
  out.innerHTML = `<div style="background:#052e16;border:1px solid #10b981;border-radius:10px;padding:10px;">
    <strong>Checkout URL:</strong> <a style="color:#6ee7b7;font-weight:800;" href="${{d.checkout_url}}" target="_blank">${{d.checkout_url}}</a>
    <span style="margin-left:8px;font-size:.82rem;">(ID: <code>${{d.invoice.invoice_id}}</code>)</span>
  </div>`;
}}
</script></body></html>"""
