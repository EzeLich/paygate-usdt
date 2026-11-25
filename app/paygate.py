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
from .onchain_verifier import OnChainPaymentVerifier

SUPPORTED_NETWORKS: dict[str, dict[str, str]] = {
    "polygon": {"label": "Polygon (USDT / USDC — ~$0.01 fee)", "tokens": "USDT or USDC"},
    "bsc": {"label": "BNB Smart Chain BEP20 (USDT / USDC)", "tokens": "USDT or USDC"},
    "arbitrum": {"label": "Arbitrum One (USDT / USDC)", "tokens": "USDT or USDC"},
    "base": {"label": "Base (USDC / USDT)", "tokens": "USDC or USDT"},
    "solana": {"label": "Solana (USDC / USDT — Phantom)", "tokens": "USDC or USDT"},
    "ethereum": {"label": "Ethereum Mainnet (USDC / USDT)", "tokens": "USDC or USDT"},
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
        return False, f"Red no soportada '{net}'. Usa polygon, bsc, arbitrum, base o solana."
    if net == "solana":
        if not SOL_ADDR_RE.match(w):
            return False, "La direccion Solana debe ser una clave publica valida de Phantom/Solana."
        return True, ""
    if not EVM_ADDR_RE.match(w):
        return False, f"Para {net.upper()} debes ingresar una wallet EVM valida que empiece con 0x..."
    return True, ""


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
        is_studio_merchant = (
            updated["merchant_wallet"].strip().lower() == StudioConfig.PHANTOM_EVM_ADDRESS.strip().lower()
            or updated["merchant_wallet"].strip() == StudioConfig.PHANTOM_SOL_ADDRESS.strip()
        )
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
    """Scan recent Transfer logs across the invoice network (and fallback EVM chains) to settle without manual tx_hash entry."""
    inv = ledger.get_paygate_invoice(invoice_id)
    if not inv:
        return {"ok": False, "status_code": 404, "error": "Invoice not found."}
    if inv["status"] == "paid":
        return {"ok": True, "invoice": inv, "already_paid": True}
    wal = str(inv["merchant_wallet"] or "").strip()
    if not wal.startswith("0x") or not hasattr(verifier, "find_recent_evm_transfers"):
        return {
            "ok": False,
            "status_code": 402,
            "error": "Paste your transaction signature in the box above to verify.",
        }
    primary = str(inv["network"] or "polygon").strip().lower()
    chains = [primary] + [c for c in ("base", "polygon", "arbitrum", "bsc", "ethereum") if c != primary]
    for net in chains:
        matches = verifier.find_recent_evm_transfers(
            network=net,
            expected_recipient=wal,
            min_amount_usd=float(inv["amount_usd"]),
            lookback_blocks=400,
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
    return {
        "ok": False,
        "status_code": 402,
        "error": f"No unredeemed transfer >= ${float(inv['amount_usd']):.2f} USDT/USDC to {wal[:10]}... detected in recent blocks yet. Wait ~5s after confirmation or paste TX hash.",
    }


_EXPLORER_TX_URLS: dict[str, str] = {
    "polygon": "https://polygonscan.com/tx/",
    "bsc": "https://bscscan.com/tx/",
    "base": "https://basescan.org/tx/",
    "arbitrum": "https://arbiscan.io/tx/",
    "ethereum": "https://etherscan.io/tx/",
    "solana": "https://solscan.io/tx/",
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


def _wallet_payment_uri(invoice: dict[str, Any]) -> str:
    net = str(invoice.get("network") or "polygon").lower()
    wal = str(invoice.get("merchant_wallet") or "").strip()
    amt = float(invoice.get("amount_usd") or 0.0)
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
    badge_bg = "rgba(16,185,129,.16)" if is_paid else "rgba(245,158,11,.15)"
    badge_fg = "#6ee7b7" if is_paid else "#fcd34d"
    badge_bd = "#10b981" if is_paid else "#f59e0b"
    status_txt = "PAID · CONFIRMED ON-CHAIN" if is_paid else "PENDING ON-CHAIN TRANSFER"
    tx_esc = html.escape(invoice.get("tx_hash") or "")
    redir = html.escape(invoice.get("redirect_url") or "")
    api_pass = (invoice.get("metadata") or {}).get("api_pass") or {}
    api_key_esc = html.escape(str(api_pass.get("api_key") or ""))
    receipt_url = f"{base}/paygate/receipt/{inv_id}"
    pay_uri = html.escape(_wallet_payment_uri(invoice))

    evm_tokens = StudioConfig.EVM_STABLECOINS.get(net_raw, {})
    evm_chain_id = StudioConfig.EVM_CHAIN_IDS.get(net_raw, 137)
    usdt_addr, usdt_dec = evm_tokens.get("USDT") or ("", 6)
    usdc_addr, usdc_dec = evm_tokens.get("USDC") or ("", 6)
    one_click_btns = ""
    chain_switcher_html = ""
    tier_switcher_html = ""
    raw_wal = invoice["merchant_wallet"].strip()
    is_studio_inv = (
        raw_wal.lower() == (StudioConfig.PHANTOM_EVM_ADDRESS or "").strip().lower()
        or raw_wal == (StudioConfig.PHANTOM_SOL_ADDRESS or "").strip()
    )
    if not is_paid and (raw_wal.startswith("0x") or is_studio_inv):
        pills = []
        net_choices = [("base", "Base"), ("polygon", "Polygon"), ("arbitrum", "Arbitrum"), ("bsc", "BSC (BEP-20)"), ("ethereum", "Ethereum")]
        if is_studio_inv:
            net_choices.append(("solana", "Solana (SPL)"))
        for c_net, c_lbl in net_choices:
            active_style = "background:#10b981;color:#02160f;border-color:#10b981;font-weight:800;" if c_net == net_raw else "background:#070b14;color:#cbd5e1;border-color:#334155;"
            pills.append(
                f'<a href="?network={c_net}" style="display:inline-block;padding:5px 11px;border-radius:999px;border:1px solid;font-size:.78rem;text-decoration:none;{active_style}">{c_lbl}</a>'
            )
        chain_switcher_html = (
            '<div style="margin-bottom:14px;">'
            '<div style="font-size:.78rem;color:#94a3b8;font-weight:700;margin-bottom:6px;">SWITCH PAYMENT NETWORK (PAY FROM ANY CHAIN YOU HOLD USDT/USDC ON):</div>'
            f'<div style="display:flex;gap:6px;flex-wrap:wrap;">{"".join(pills)}</div>'
            '</div>'
        )
    if not is_paid and is_studio_inv:
        s_evm = StudioConfig.PHANTOM_EVM_ADDRESS or "0x4Ed4D0750453C027FA8398067d5af980Bcc9B6eD"
        t_net = net_raw if net_raw in ("base", "polygon", "arbitrum", "bsc", "ethereum") else "base"
        tier_switcher_html = (
            '<div style="margin-bottom:14px;padding:11px 13px;background:#070b14;border:1px solid #1e293b;border-radius:10px;">'
            '<div style="font-size:.76rem;color:#38bdf8;font-weight:800;margin-bottom:6px;">INSTANT AUTOMATED ON-SCREEN DELIVERY (NO EMAIL OR SIGNUP NEEDED):</div>'
            '<div style="font-size:.79rem;color:#cbd5e1;margin-bottom:8px;">Upon 1 block confirmation (~3–5s), this screen automatically detects your transfer and reveals your <code>cg_live_...</code> API / White-Label License Key + cryptographic SHA-256 receipt. Switch plan in 1 click:</div>'
            '<div style="display:flex;gap:6px;flex-wrap:wrap;">'
            f'<a href="{base}/paygate/pay?merchant={s_evm}&amount=0.5&network={t_net}&title=ChainGuard+24h+Micro+Unlock+Pass&issue_pass=1&billing_days=1" style="padding:4px 10px;border-radius:8px;border:1px solid #38bdf8;color:#7dd3fc;text-decoration:none;font-size:.78rem;font-weight:700;">$0.50 · 24h Test Pass (50 calls)</a>'
            f'<a href="{base}/paygate/pay?merchant={s_evm}&amount=2&network={t_net}&title=ChainGuard+7-Day+Starter+MCP+Pass&issue_pass=1&billing_days=7" style="padding:4px 10px;border-radius:8px;border:1px solid #10b981;color:#6ee7b7;text-decoration:none;font-size:.78rem;font-weight:700;">$2.00 · 7-Day Pass (250 calls)</a>'
            f'<a href="{base}/paygate/pay?merchant={s_evm}&amount=9&network={t_net}&title=ChainGuard+Unlimited+MCP+Pass&issue_pass=1&billing_days=30" style="padding:4px 10px;border-radius:8px;border:1px solid #a855f7;color:#d8b4fe;text-decoration:none;font-size:.78rem;font-weight:700;">$9.00 · 30-Day Unlimited</a>'
            f'<a href="{base}/paygate/pay?merchant={s_evm}&amount=79&network={t_net}&title=PayGate+Lifetime+White-Label+License&issue_pass=1&billing_days=3650" style="padding:4px 10px;border-radius:8px;border:1px solid #f59e0b;color:#fcd34d;text-decoration:none;font-size:.78rem;font-weight:700;">$79.00 · Lifetime White-Label</a>'
            '</div></div>'
        )
    if not is_paid and net_raw in StudioConfig.EVM_CHAIN_IDS:
        btns = []
        if usdt_addr:
            btns.append(f"""<button class="btn" onclick="payWithWallet('{usdt_addr}', {usdt_dec}, 'USDT')">1-Click Pay ${amt} USDT (Browser Wallet)</button>""")
        if usdc_addr:
            btns.append(f"""<button class="btn2" style="border-color:#38bdf8;color:#7dd3fc;" onclick="payWithWallet('{usdc_addr}', {usdc_dec}, 'USDC')">1-Click Pay ${amt} USDC (Browser Wallet)</button>""")
        if btns:
            one_click_btns = f"""<div style="margin-bottom:14px;display:flex;gap:8px;flex-wrap:wrap;">{''.join(btns)}</div>"""

    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=136x136&margin=6&data={urllib.parse.quote(wallet)}"

    paid_key_html = (
        f"""<div style="margin-top:10px;background:#020617;border:1px solid #38bdf8;border-radius:10px;padding:12px;">
          <div style="font-size:.84rem;color:#7dd3fc;font-weight:800;">Your Instant MCP &amp; Pro API License Key Is Active ({api_pass.get('calls_remaining', 1000)} calls · Plan: {html.escape(str(api_pass.get('plan_id') or 'pro_pass'))}):</div>
          <div style="display:flex;gap:8px;align-items:center;margin-top:6px;flex-wrap:wrap;">
            <code style="flex:1;padding:8px 10px;display:block;">{api_key_esc}</code>
            <button class="btn" onclick="navigator.clipboard.writeText('{api_key_esc}');this.textContent='Copied Key!'">Copy Key</button>
          </div>
          <div style="font-size:.78rem;color:#94a3b8;margin-top:6px;">Pass header <code>X-API-Key: {api_key_esc}</code> on <code>POST {base}/mcp</code> or any <code>/api/v1/x402/*</code> route to unlock full patches, 25 DeFi pools &amp; 8-chain portfolios.</div>
        </div>"""
        if api_key_esc
        else ""
    )

    paid_block = (
        f"""<div class="card" style="background:#052e16;border-color:#10b981;margin-top:16px;">
          <div style="font-weight:800;color:#6ee7b7;">Payment Confirmed On-Chain</div>
          <div style="font-size:.88rem;margin-top:6px;">TX: <code>{tx_esc}</code></div>
          {paid_key_html}
          <div style="margin-top:12px;display:flex;gap:10px;flex-wrap:wrap;">
            <a class="btn2" href="{receipt_url}">View Verifiable SHA-256 Receipt</a>
            {f'<a class="btn" href="{redir}">Continue to Merchant →</a>' if redir else ''}
          </div>
        </div>"""
        if is_paid
        else f"""<div class="card" style="margin-top:16px;">
          {tier_switcher_html}
          {chain_switcher_html}
          {one_click_btns}
          <div style="display:flex;gap:14px;align-items:flex-start;flex-wrap:wrap;">
            <div style="flex:1;min-width:240px;">
              <label>1. Send exact amount (<strong>${amt}</strong> USDT or USDC on <strong>{net}</strong>) to merchant wallet:</label>
              <div style="display:flex;gap:8px;align-items:center;margin-top:6px;flex-wrap:wrap;">
                <code id="mWal" style="flex:1;padding:10px;display:block;">{wallet}</code>
                <button class="btn2" onclick="navigator.clipboard.writeText('{wallet}');this.textContent='Copied!'">Copy Wallet</button>
                <button class="btn2" onclick="navigator.clipboard.writeText('{amt}');this.textContent='Copied ${amt}!'">Copy ${amt}</button>
              </div>
              <div style="margin-top:8px;font-size:.78rem;color:#94a3b8;">Mobile / Wallet URI: <code>{pay_uri}</code></div>
            </div>
            <div style="text-align:center;background:#fff;padding:6px;border-radius:10px;">
              <img src="{qr_url}" width="124" height="124" alt="Wallet QR" style="display:block;border-radius:6px;" onerror="this.parentElement.style.display='none'"/>
              <div style="font-size:.68rem;color:#0f172a;font-weight:800;margin-top:2px;">SCAN IN MOBILE WALLET</div>
            </div>
          </div>
          <label style="margin-top:14px;">2. Verify payment (auto-detects your transfer on-chain every 12s or click below):</label>
          <div style="display:flex;gap:8px;margin-top:6px;flex-wrap:wrap;">
            <button class="btn" onclick="autoDetectInv(false)">I Sent It — Auto-Detect On-Chain (No TX Hash Needed)</button>
          </div>
          <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap;">
            <input id="invTx" style="flex:1;min-width:220px;" placeholder="Or paste 0x... / Solana tx signature"/>
            <button class="btn2" onclick="verifyInv()">Verify by TX Hash</button>
          </div>
          <div id="invMsg" style="margin-top:10px;font-size:.9rem;color:#38bdf8;">Listening for on-chain transfer to {wallet[:10]}... on {net}...</div>
          <div style="margin-top:12px;padding-top:10px;border-top:1px solid #1e293b;font-size:.8rem;color:#94a3b8;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;">
            <span>Short on {net} USDT/USDC?</span>
            <a href="{base}/recover" target="_blank" style="color:#38bdf8;font-weight:700;">Swap Any Token or Dust into USDT/USDC →</a>
          </div>
        </div>"""
    )
    cents_int = int(round(amt_f * 100))
    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Pay ${amt} USDT — {title} | PayGate USDT</title>
<style>
  *{{box-sizing:border-box}} body{{margin:0;background:#070b14;color:#f1f5f9;font-family:Inter,system-ui,sans-serif;line-height:1.55}}
  .wrap{{max-width:680px;margin:0 auto;padding:32px 16px 64px}}
  .card{{background:#0f172a;border:1px solid #1e293b;border-radius:14px;padding:20px}}
  .badge{{display:inline-block;padding:4px 11px;border-radius:999px;background:{badge_bg};border:1px solid {badge_bd};color:{badge_fg};font-size:.76rem;font-weight:800}}
  .btn{{display:inline-block;background:#10b981;color:#02160f;font-weight:800;padding:10px 18px;border-radius:10px;border:none;cursor:pointer;text-decoration:none}}
  .btn2{{display:inline-block;background:#1e293b;color:#f8fafc;font-weight:700;padding:10px 14px;border-radius:10px;border:1px solid #334155;cursor:pointer;text-decoration:none}}
  input{{padding:10px 12px;border-radius:9px;border:1px solid #334155;background:#070b14;color:#f8fafc;font-size:.92rem}}
  label{{display:block;font-size:.84rem;color:#cbd5e1;font-weight:700}}
  code{{background:#020617;padding:4px 8px;border-radius:6px;color:#6ee7b7;font-size:.82rem;word-break:break-all}}
</style></head><body><div class="wrap">
  <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:14px;">
    <span class="badge">{status_txt}</span>
    <span style="font-size:.82rem;color:#94a3b8;">Invoice <code>{inv_id}</code> · {net}</span>
  </div>
  <div class="card">
    <div style="color:#94a3b8;font-size:.86rem;">NON-CUSTODIAL STABLECOIN CHECKOUT</div>
    <h1 style="margin:6px 0 8px;font-size:1.55rem;">{title}</h1>
    <div style="font-size:2.1rem;font-weight:900;color:#10b981;">${amt} <span style="font-size:.95rem;color:#94a3b8;">USDT / USDC ({net})</span></div>
  </div>
  {paid_block}
  <div style="margin-top:20px;text-align:center;font-size:.8rem;color:#64748b;">
    Powered by <a href="https://github.com/EzeLich/paygate-usdt" target="_blank" style="color:#38bdf8;">PayGate USDT Open-Source Gateway</a> · <a href="{base}/paygate" style="color:#94a3b8;">Create your own checkout</a>
  </div>
</div>
<script>
const TARGET_CHAIN_HEX = '0x' + ({evm_chain_id}).toString(16);
const MERCHANT_WALLET = '{wallet}';
const CENTS_INT = {cents_int};

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
    m.textContent = 'Scanning recent blocks across Base, Polygon, Arbitrum, BSC & Ethereum for your transfer...';
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
if ({'false' if is_paid else 'true'} && MERCHANT_WALLET.startsWith('0x')) {{
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
    t = {
        "badge": "PAYGATE USDT · 0% COMMISSION · DIRECT SELF-CUSTODY WALLET" if not is_es else "PAYGATE USDT · 0% COMISION POR VENTA · DIRECTO A TU WALLET",
        "title": "PayGate USDT — Monetize Private Telegram Groups & Digital Goods in USDT (No Stripe, No KYC)" if not is_es else "PayGate USDT — Cobrá tu Canal VIP o Producto Digital en USDT sin Banco ni Whop",
        "h1": 'Monetize Private Telegram Groups & Digital Products in USDT — <span style="color:#10b981;">Zero Stripe, Zero Banks, Zero KYC</span>.' if not is_es else 'Cobrá tu Canal VIP de Telegram o Productos Digitales en USDT — <span style="color:#10b981;">sin banco, sin monotributo ni Whop</span>.',
        "sub": "Members pay in <strong>USDT / USDC (Polygon, BSC BEP20, Arbitrum, Base, or Solana)</strong> straight to your Phantom, Trust, MetaMask, or exchange wallet. The bot verifies transactions on-chain in seconds, issues single-use invite links, and auto-removes expired members." if not is_es else "Tus miembros pagan en <strong>USDT / USDC (Polygon, BSC BEP20, Arbitrum, Base o Solana)</strong> directo a tu billetera Phantom, Trust, Binance o Bitget. El bot verifica la transaccion on-chain en segundos, entrega un enlace privado de 1 solo uso y expulsa automatico al vencer el mes.",
        "cta1": "Connect My Bot Free (60s)" if not is_es else "Conectar mi Bot Gratis (60 seg)",
        "cta2": "Live Telegram Demo" if not is_es else "Probar Demo en Telegram",
        "cta3": "Unlock Unlimited ($29/mo or $79)" if not is_es else "Activar Ilimitado ($29/mes o $79 unico)",
        "c1k": "1 · NON-CUSTODIAL" if not is_es else "1 · CERO INTERMEDIARIOS",
        "c1t": "100% direct to your wallet" if not is_es else "100% directo a tu wallet",
        "c1b": "Funds never touch our wallet or a bank. Every payment settles directly from buyer to your own USDT/USDC address." if not is_es else "La plata nunca pasa por nuestra cuenta ni por un banco. El pago va directo de la wallet de tu comprador a tu propia direccion USDT/USDC.",
        "c2k": "2 · ZERO MANUAL ADMIN" if not is_es else "2 · CERO TRABAJO MANUAL",
        "c2t": "1-use links + auto-kick" if not is_es else "Links únicos + expulsión automática",
        "c2b": "Stop checking screenshots by hand. Every transfer is verified via RPC with anti-replay protection and automated expiry kicks." if not is_es else "Basta de pedir capturas por privado y llevar planillas. Cada compra valida el Tx Hash on-chain y genera un link privado de 1 solo ingreso.",
        "c3k": "3 · LIVE IN 60 SECONDS" if not is_es else "3 · LISTO EN 60 SEGUNDOS",
        "c3t": "Zero code required" if not is_es else "Sin programar nada",
        "c3b": f"Create a bot in @BotFather, paste your token below, add it as admin to your VIP group, and start collecting. ({stats['total_bots']} bots · ${stats['total_volume_usdt']} USDT verified)" if not is_es else f"Creas tu bot en @BotFather, pegas el token abajo, agregas el bot como admin en tu canal privado y ya estas cobrando. ({stats['total_bots']} bots creados · ${stats['total_volume_usdt']} USDT verificados)",
        "setup_h": "Connect Your VIP Bot in 60s (Free up to 3 members)" if not is_es else "Conecta tu Bot VIP en 60 segundos (Gratis hasta 3 miembros)",
        "setup_p": "First step: open <strong>@BotFather</strong> in Telegram → send <code>/newbot</code> → copy the token. For private channels, make the bot an admin with Invite &amp; Ban permissions." if not is_es else "Paso previo: en Telegram abrí <strong>@BotFather</strong> → enviá <code>/newbot</code> → copiá el token. Si tenés canal VIP, agregá tu bot como administrador con permiso para invitar y restringir usuarios.",
        "l_name": "Community / Product Name" if not is_es else "Nombre de tu Comunidad / Producto",
        "l_tok": "Bot Token (@BotFather)" if not is_es else "Token de tu Bot (@BotFather)",
        "l_net": "Payout Network (0% fee)" if not is_es else "Red donde queres cobrar (0% comision)",
        "l_wal": "Your Payout Wallet (0x... or Solana)" if not is_es else "Tu Wallet para recibir pagos (0x... o Solana)",
        "l_chan": "Private Channel/Group ID (optional, e.g. -1001234567890)" if not is_es else "ID de tu Canal/Grupo VIP (opcional, ej: -1001234567890)",
        "l_price": "Monthly Price (30 days) in USDT" if not is_es else "Precio Plan Mensual (30 dias) en USDT",
        "l_deliv": "Post-payment bonus link or instructions (optional — ideal for scripts, courses, files)" if not is_es else "Entrega extra o link privado tras pagar (opcional — ideal para cursos, scripts o archivos)",
        "btn_go": "Launch My Bot Now" if not is_es else "Activar mi Bot Ahora",
        "up_h": "Transparent Pricing (Direct On-Chain USDC/USDT)" if not is_es else "Planes PayGate USDT (Sin suscripción bancaria)",
        "f_t": "FREE TRIAL" if not is_es else "PRUEBA GRATIS",
        "f_b": "Up to 3 active paid members so you can test live RPC verification before paying anything." if not is_es else "Hasta 3 miembros activos reales para probar tu bot y validar cobros on-chain antes de pagar un centavo.",
        "p_t": "HOSTED PRO ($29/mo)" if not is_es else "PRO HOSTEADO ($29/mes)",
        "p_b": "Unlimited members, 0% revenue cut, expiry DMs, and 24/7 automated channel kick on our cloud." if not is_es else "Miembros ilimitados, 0% comision sobre ventas, alertas de vencimiento y expulsion automatica 24/7 en nuestro servidor.",
        "l_t": "LIFETIME LICENSE ($79)" if not is_es else "LICENCIA COMPLETA ($79)",
        "l_b": "Unlimited members forever with zero monthly fees + white-label branding." if not is_es else "Ilimitado para siempre sin abono mensual + marca blanca para tu comunidad.",
        "up_box": f"<strong>To upgrade any bot to Pro ($29) or Lifetime ($79):</strong><br/>1) Send $29 or $79 (USDT/USDC) on <strong>Polygon / BSC / Arbitrum / Base</strong> to: <code>{evm_addr}</code><br/>&nbsp;&nbsp;&nbsp;&nbsp;or on <strong>Solana</strong> to: <code>{sol_addr}</code><br/>2) Open your bot's admin panel link above and paste the Tx Hash for instant activation." if not is_es else f"<strong>Para activar Pro ($29) o Licencia ($79) en cualquier bot ya creado:</strong><br/>1) Envia $29 o $79 (USDT/USDC) por <strong>Polygon / BSC / Arbitrum / Base</strong> a: <code>{evm_addr}</code><br/>&nbsp;&nbsp;&nbsp;&nbsp;o por <strong>Solana</strong> a: <code>{sol_addr}</code><br/>2) Abrí el Panel de tu bot (el link que te damos arriba al activarlo) y pegá el Tx Hash para desbloqueo instantaneo en 5 segundos.",
    }
    alt_lang = "en" if is_es else "es"
    alt_lbl = "English" if is_es else "Español"
    return f"""<!DOCTYPE html><html lang="{'es' if is_es else 'en'}"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{t['title']}</title>
<style>
  *{{box-sizing:border-box}}
  body{{margin:0;background:#070b14;color:#f1f5f9;font-family:Inter,system-ui,-apple-system,sans-serif;line-height:1.55}}
  .wrap{{max-width:980px;margin:0 auto;padding:24px 16px 64px}}
  .badge{{display:inline-block;padding:5px 12px;border-radius:999px;background:rgba(16,185,129,.12);border:1px solid #10b981;color:#6ee7b7;font-size:.78rem;font-weight:800}}
  h1{{font-size:clamp(1.75rem,4vw,2.6rem);line-height:1.15;margin:14px 0 10px}}
  .sub{{color:#94a3b8;font-size:1.03rem;max-width:740px;margin-bottom:20px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:14px;margin:20px 0}}
  .card{{background:#0f172a;border:1px solid #1e293b;border-radius:14px;padding:18px}}
  .btn{{display:inline-block;background:#10b981;color:#02160f;font-weight:800;padding:11px 18px;border-radius:10px;border:none;cursor:pointer;text-decoration:none;font-size:.94rem}}
  .btn2{{display:inline-block;background:#1e293b;color:#f8fafc;font-weight:700;padding:11px 18px;border-radius:10px;border:1px solid #334155;cursor:pointer;text-decoration:none;font-size:.94rem}}
  label{{display:block;font-size:.82rem;color:#cbd5e1;font-weight:700;margin-top:11px}}
  input,select,textarea{{width:100%;padding:10px 12px;border-radius:9px;border:1px solid #334155;background:#070b14;color:#f8fafc;font-size:.92rem;margin-top:4px}}
  code{{background:#020617;padding:3px 7px;border-radius:6px;color:#6ee7b7;font-size:.82rem;word-break:break-all}}
  .ok{{color:#34d399;font-weight:700}} .err{{color:#fca5a5;font-weight:700}}
</style></head>
<body><div class="wrap">
  <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
    <span class="badge">{t['badge']}</span>
    <div style="display:flex;gap:8px;flex-wrap:wrap;">
      <a class="btn2" style="padding:6px 12px;font-size:.8rem;border-color:#10b981;color:#6ee7b7;" href="{base}/paygate/merchant">Merchant Dashboard</a>
      <a class="btn2" style="padding:6px 12px;font-size:.8rem;" href="https://github.com/EzequielLech/paygate-usdt" target="_blank">GitHub Open-Source SDK &amp; API</a>
      <a class="btn2" style="padding:6px 12px;font-size:.8rem;" href="#invoice-link">{'Instant Invoice' if not is_es else 'Link de Cobro'}</a>
      <a class="btn2" style="padding:6px 12px;font-size:.8rem;" href="#demo">{'Live Demo' if not is_es else 'Demo en Vivo'}</a>
      <a class="btn2" style="padding:6px 12px;font-size:.8rem;" href="#b2b">{'Done-For-You B2B' if not is_es else 'Setup Llave en Mano'}</a>
      <a class="btn2" style="padding:6px 12px;font-size:.8rem;" href="{base}/paygate?lang={alt_lang}">{alt_lbl}</a>
    </div>
  </div>
  <div style="margin-top:14px;background:#0b1325;border:1px solid #1e3a5f;border-radius:12px;padding:12px 16px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">
    <div style="font-size:.88rem;color:#cbd5e1;">
      <strong>Open-Source Python + TypeScript/Node.js SDK + x402 + Signed Webhooks + MCP Server:</strong>
      Deploy <code>paygate-usdt</code> self-hosted (MIT) or use our managed cloud &amp; B2B integration.
    </div>
    <a class="btn2" style="padding:7px 13px;font-size:.82rem;border-color:#38bdf8;color:#7dd3fc;" href="https://github.com/EzequielLech/paygate-usdt" target="_blank">github.com/EzequielLech/paygate-usdt -></a>
  </div>
  <h1>{t['h1']}</h1>
  <p class="sub">{t['sub']}</p>

  <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:24px;">
    <a class="btn" href="#b2b">{'Deploy For My Business ($199+)' if not is_es else 'Implementar para mi Negocio ($199+)'}</a>
    <a class="btn2" href="{base}/paygate/merchant">{'Merchant Dashboard' if not is_es else 'Panel de Ventas'}</a>
    <a class="btn2" href="#setup">{t['cta1']}</a>
    <a class="btn2" href="#demo">{'Interactive Preview (10s)' if not is_es else 'Probar Flujo Interactivo (10s)'}</a>
    <a class="btn2" href="https://t.me/ChainGuardVaultBot?start=vip" target="_blank">{t['cta2']}</a>
  </div>

  <div class="grid">
    <div class="card">
      <div style="color:#10b981;font-weight:800;font-size:.82rem;">{t['c1k']}</div>
      <h3 style="margin:6px 0;">{t['c1t']}</h3>
      <p style="color:#94a3b8;font-size:.9rem;margin:0;">{t['c1b']}</p>
    </div>
    <div class="card">
      <div style="color:#10b981;font-weight:800;font-size:.82rem;">{t['c2k']}</div>
      <h3 style="margin:6px 0;">{t['c2t']}</h3>
      <p style="color:#94a3b8;font-size:.9rem;margin:0;">{t['c2b']}</p>
    </div>
    <div class="card">
      <div style="color:#10b981;font-weight:800;font-size:.82rem;">{t['c3k']}</div>
      <h3 style="margin:6px 0;">{t['c3t']}</h3>
      <p style="color:#94a3b8;font-size:.9rem;margin:0;">{t['c3b']}</p>
    </div>
  </div>

  <div class="card" id="invoice-link" style="margin-top:20px;border-color:#38bdf8;">
    <h2 style="margin:0 0 6px;">{'Create Instant USDT / USDC Payment Link (No Bot Required)' if not is_es else 'Crear Link de Cobro Instantaneo en USDT / USDC (Sin Bot)'}</h2>
    <p style="margin:0 0 12px;font-size:.88rem;color:#94a3b8;">{'Generate a hosted non-custodial invoice page + optional HMAC-SHA256 webhook callback in 5 seconds. Funds settle 100% directly to your wallet.' if not is_es else 'Genera un link de cobro directo a tu wallet + webhook firmado HMAC-SHA256 en 5 segundos, sin custodia ni intermediarios.'}</p>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;">
      <div>
        <label>{'Invoice Title' if not is_es else 'Concepto / Producto'}</label>
        <input id="invTitle" placeholder="SaaS License / Consulting / VIP Access"/>
      </div>
      <div>
        <label>{'Amount (USDT / USDC)' if not is_es else 'Monto (USDT / USDC)'}</label>
        <input id="invAmt" type="number" step="0.5" value="50"/>
      </div>
      <div>
        <label>{'Network (0% fee)' if not is_es else 'Red (0% comision)'}</label>
        <select id="invNet">
          <option value="polygon">Polygon</option>
          <option value="bsc">BSC BEP20</option>
          <option value="base">Base</option>
          <option value="arbitrum">Arbitrum</option>
          <option value="solana">Solana</option>
          <option value="ethereum">Ethereum</option>
        </select>
      </div>
      <div>
        <label>{'Merchant Wallet (0x... or Solana)' if not is_es else 'Tu Wallet Receptora (0x... o Solana)'}</label>
        <input id="invWallet" placeholder="0x... or Solana address"/>
      </div>
    </div>
    <label>{'Webhook Callback URL (optional — receives signed X-PayGate-Signature)' if not is_es else 'URL Webhook (opcional — recibe evento invoice.paid firmado)'}</label>
    <input id="invHook" placeholder="https://yourapp.com/webhooks/paygate"/>
    <div style="margin-top:12px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;">
      <button class="btn2" style="border-color:#38bdf8;color:#7dd3fc;" onclick="createInstantInvoice()">{'Generate Hosted Checkout URL' if not is_es else 'Generar Link de Cobro'}</button>
      <span id="invCreateMsg" style="font-size:.9rem;"></span>
    </div>
    <div id="invCreateOut" style="margin-top:12px;"></div>
  </div>

  <div class="card" id="setup" style="margin-top:20px;border-color:#10b981;">
    <h2 style="margin:0 0 6px;">{t['setup_h']}</h2>
    <p style="margin:0 0 12px;font-size:.88rem;color:#94a3b8;">{t['setup_p']}</p>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;">
      <div>
        <label>{t['l_name']}</label>
        <input id="bName" placeholder="Alpha Signals VIP"/>
      </div>
      <div>
        <label>{t['l_tok']}</label>
        <input id="bToken" placeholder="1234567890:AAF..."/>
      </div>
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
      <div>
        <label>{t['l_wal']}</label>
        <input id="bWallet" placeholder="0x... or Solana address"/>
      </div>
      <div>
        <label>{t['l_chan']}</label>
        <input id="bChan" placeholder="-100..."/>
      </div>
      <div>
        <label>{t['l_price']}</label>
        <input id="bPrice" type="number" step="0.5" value="25"/>
      </div>
    </div>
    <label>{t['l_deliv']}</label>
    <input id="bDeliv" placeholder="https://... or instructions"/>
    <div style="margin-top:14px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;">
      <button class="btn" onclick="crearBot()">{t['btn_go']}</button>
      <span id="setupMsg" style="font-size:.9rem;"></span>
    </div>
    <div id="setupResult" style="margin-top:12px;"></div>
  </div>

  <div class="card" id="demo" style="margin-top:20px;border-color:#38bdf8;">
    <h2 style="margin:0 0 6px;">{'Interactive Buyer Experience (No Bot Token Needed)' if not is_es else 'Simulador del Flujo de Compra (Sin Token previo)'}</h2>
    <p style="margin:0 0 12px;font-size:.88rem;color:#94a3b8;">{'Click below to preview the exact non-custodial flow your paying members see inside Telegram:' if not is_es else 'Hace clic abajo para ver exactamente lo que vive un miembro al pagar en tu comunidad VIP:'}</p>
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
          <option value="Polygon">Polygon (USDT / USDC)</option>
          <option value="BSC BEP20">BNB Smart Chain BEP20 (USDT / USDC)</option>
          <option value="Base">Base (USDC / USDT)</option>
          <option value="Arbitrum">Arbitrum One (USDT / USDC)</option>
          <option value="Solana">Solana (USDC / USDT)</option>
        </select>
      </div>
      <div style="display:flex;align-items:flex-end;">
        <button class="btn2" style="width:100%;border-color:#38bdf8;color:#7dd3fc;" onclick="runDemoFlow()">{'Simulate Instant Settlement' if not is_es else 'Simular Cobro + Acceso VIP'}</button>
      </div>
    </div>
    <div id="demoOutput" style="margin-top:12px;"></div>
  </div>

  <div class="card" id="b2b" style="margin-top:20px;border-color:#f59e0b;">
    <span class="badge" style="border-color:#f59e0b;color:#fcd34d;background:rgba(245,158,11,.12);">{'DONE-FOR-YOU B2B INFRASTRUCTURE' if not is_es else 'IMPLEMENTACION B2B LLAVE EN MANO'}</span>
    <h2 style="margin:10px 0 6px;">{'Want Us to Deploy & Customize Your Payment Infrastructure?' if not is_es else 'Queres que lo dejemos funcionando a medida para tu negocio?'}</h2>
    <p style="margin:0 0 12px;font-size:.9rem;color:#94a3b8;">{'Built for paid Telegram signal/alpha rooms, SaaS teams blocked by Stripe/Whop, and crypto businesses requiring custom bot/API automation. Pay directly in USDT/USDC with 50% kickoff deposit.' if not is_es else 'Ideal para comunidades VIP de trading/educación, SaaS sin Stripe/Whop o empresas cripto que necesitan bots y automatización Python a medida. Cobro directo en USDT/USDC con seña del 50%.'}</p>
    <div class="grid" style="margin:12px 0;">
      <div class="card" style="background:#070b14;border-color:#f59e0b;">
        <div style="font-weight:800;color:#fcd34d;">{'TURNKEY VIP GATE (24H)' if not is_es else 'VIP GATE LLAVE EN MANO (24H)'}</div>
        <div style="font-size:1.7rem;font-weight:900;margin:4px 0;">$199 <span style="font-size:.85rem;color:#94a3b8;">USDT ({'50% deposit: $99' if not is_es else 'seña 50%: $99'})</span></div>
        <p style="font-size:.86rem;color:#94a3b8;">{PAYGATE_B2B_PACKAGES['turnkey_199']['summary_es' if is_es else 'summary_en']}</p>
      </div>
      <div class="card" style="background:#070b14;border-color:#38bdf8;">
        <div style="font-weight:800;color:#7dd3fc;">{'CUSTOM BOT + API / EXCHANGE' if not is_es else 'BOT A MEDIDA + API / EXCHANGE'}</div>
        <div style="font-size:1.7rem;font-weight:900;margin:4px 0;">$499 <span style="font-size:.85rem;color:#94a3b8;">USDT ({'50% deposit: $249' if not is_es else 'seña 50%: $249'})</span></div>
        <p style="font-size:.86rem;color:#94a3b8;">{PAYGATE_B2B_PACKAGES['custom_499']['summary_es' if is_es else 'summary_en']}</p>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;">
      <div>
        <label>{'Solution Package' if not is_es else 'Solución a contratar'}</label>
        <select id="b2bPkg">
          <option value="turnkey_199">{'Turnkey VIP Gate Setup — $199 USDT ($99 deposit)' if not is_es else 'VIP Gate Llave en Mano — $199 USDT ($99 seña)'}</option>
          <option value="custom_499">{'Custom Crypto Bot + Exchange/API — $499 USDT ($249 deposit)' if not is_es else 'Bot Cripto + API/Exchange a Medida — $499 USDT ($249 seña)'}</option>
        </select>
      </div>
      <div>
        <label>{'Your Name / Brand' if not is_es else 'Tu Nombre / Marca'}</label>
        <input id="b2bName" placeholder="e.g. Alpha Desk / Studio"/>
      </div>
      <div>
        <label>{'Telegram @handle or Email' if not is_es else 'Tu @usuario Telegram o Email'}</label>
        <input id="b2bContact" placeholder="@yourhandle or founder@domain.com"/>
      </div>
    </div>
    <label>{'Project URL / Channel & Integration Scope' if not is_es else 'Link o detalle de qué necesitás automatizar'}</label>
    <textarea id="b2bScope" rows="2" placeholder="{'e.g. 400-member VIP Telegram channel migrating off manual TRC20/Whop + webhook to Notion/CRM' if not is_es else 'Ej: canal VIP de señales cobrando USDT manual, quiero bot + expulsión automática + alertas'}"></textarea>
    <div style="margin-top:12px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;">
      <button class="btn" style="background:#f59e0b;color:#1f1300;" onclick="createB2BOrder()">{'Request Deployment & Get USDT Invoice' if not is_es else 'Reservar Implementacion y Ver Factura USDT'}</button>
      <span id="b2bMsg" style="font-size:.9rem;"></span>
    </div>
    <div id="b2bOrderBox" style="margin-top:12px;"></div>
  </div>

  <div class="card" id="upgrade" style="margin-top:18px;">
    <h2 style="margin:0 0 8px;">{t['up_h']}</h2>
    <div class="grid" style="margin:12px 0;">
      <div class="card" style="background:#070b14;">
        <div style="font-weight:800;color:#94a3b8;">{t['f_t']}</div>
        <div style="font-size:1.7rem;font-weight:900;margin:4px 0;">$0 <span style="font-size:.85rem;color:#94a3b8;">USDT</span></div>
        <p style="font-size:.86rem;color:#94a3b8;">{t['f_b']}</p>
      </div>
      <div class="card" style="background:#070b14;border-color:#10b981;">
        <div style="font-weight:800;color:#10b981;">{t['p_t']}</div>
        <div style="font-size:1.7rem;font-weight:900;margin:4px 0;">$29 <span style="font-size:.85rem;color:#94a3b8;">USDT / mo</span></div>
        <p style="font-size:.86rem;color:#94a3b8;">{t['p_b']}</p>
      </div>
      <div class="card" style="background:#070b14;border-color:#38bdf8;">
        <div style="font-weight:800;color:#38bdf8;">{t['l_t']}</div>
        <div style="font-size:1.7rem;font-weight:900;margin:4px 0;">$79 <span style="font-size:.85rem;color:#94a3b8;">USDT one-time</span></div>
        <p style="font-size:.86rem;color:#94a3b8;">{t['l_b']}</p>
      </div>
    </div>
    <div style="background:#070b14;border:1px solid #1e293b;border-radius:10px;padding:14px;font-size:.88rem;">
      {t['up_box']}
    </div>
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
  out.innerHTML = `<div class="card" style="background:#052e16;border-color:#10b981;">
    <div><strong>@${{d.bot.bot_username}}</strong> is live: <a style="color:#6ee7b7;font-weight:800;" href="https://t.me/${{d.bot.bot_username}}" target="_blank">t.me/${{d.bot.bot_username}}</a></div>
    <div style="margin-top:8px;"><strong>Save your private Admin Panel link:</strong><br/><code>${{panelUrl}}</code></div>
    <div style="margin-top:8px;"><a class="btn" href="${{panelUrl}}">Open Admin Panel -></a></div>
  </div>`;
}}

function runDemoFlow() {{
  const amt = document.getElementById('demoPlan').value;
  const ch = document.getElementById('demoChain').value;
  const out = document.getElementById('demoOutput');
  out.innerHTML = `<div style="background:#070b14;border:1px solid #38bdf8;border-radius:10px;padding:14px;font-size:.88rem;">
    <div><strong>Step 1 (Buyer in Telegram):</strong> Sends <code>/start</code> → selects <strong>$${{amt}} USDT</strong> via <strong>${{ch}}</strong>.</div>
    <div style="margin-top:6px;"><strong>Step 2 (Direct Wallet Settlement):</strong> Buyer transfers $${{amt}} straight to <em>your wallet</em> (0% fee held by PayGate) and pastes TX hash.</div>
    <div style="margin-top:6px;"><strong>Step 3 (RPC Verified in ~2s):</strong> Anti-replay check passes → Bot issues 1-use invite link <code>t.me/+singleUse7x9K</code> (<code>member_limit=1</code>) + schedules auto-kick on expiry.</div>
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
  box.innerHTML = `<div class="card" style="background:#070b14;border-color:#f59e0b;">
    <div><strong>Order ID:</strong> <code>${{ord.order_id}}</code> · <strong>${{ord.package_name}}</strong></div>
    <div style="margin-top:6px;font-size:.88rem;">Send <strong>$${{ord.min_deposit_usd}} USDT/USDC</strong> (50% deposit) or full <strong>$${{ord.full_price_usd}} USDT/USDC</strong>:</div>
    <div style="margin-top:6px;font-size:.84rem;">• EVM (Polygon / BSC / Arbitrum / Base / ETH): <code>${{d.wallets.evm}}</code></div>
    <div style="margin-top:4px;font-size:.84rem;">• Solana (SPL USDC/USDT): <code>${{d.wallets.solana}}</code></div>
    <div style="display:grid;grid-template-columns:160px 1fr auto;gap:8px;margin-top:10px;">
      <select id="b2bNet"><option value="polygon">Polygon</option><option value="bsc">BSC BEP20</option><option value="base">Base</option><option value="arbitrum">Arbitrum</option><option value="solana">Solana</option><option value="ethereum">Ethereum</option></select>
      <input id="b2bTx" placeholder="Paste TX Hash (0x... or Solana signature)"/>
      <button class="btn" onclick="verifyB2BOrder('${{ord.order_id}}')">Verify Deposit</button>
    </div>
    <div id="b2bVerifyMsg" style="margin-top:8px;font-size:.88rem;"></div>
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
  m.className = ''; m.textContent = 'Creating invoice...';
  const r = await fetch('/api/v1/paygate/invoices', {{
    method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{
      title: document.getElementById('invTitle').value.trim() || 'Digital Service',
      amount_usd: parseFloat(document.getElementById('invAmt').value || '50'),
      network: document.getElementById('invNet').value,
      merchant_wallet: document.getElementById('invWallet').value.trim(),
      webhook_url: document.getElementById('invHook').value.trim()
    }})
  }});
  const d = await r.json();
  if (!r.ok) {{ m.className = 'err'; m.textContent = d.detail || 'Error creating invoice.'; return; }}
  m.className = 'ok'; m.textContent = 'Invoice created!';
  out.innerHTML = `<div class="card" style="background:#052e16;border-color:#10b981;">
    <div><strong>Checkout Link Ready:</strong> <a style="color:#6ee7b7;font-weight:800;" href="${{d.checkout_url}}" target="_blank">${{d.checkout_url}}</a></div>
    <div style="margin-top:6px;font-size:.84rem;">ID: <code>${{d.invoice.invoice_id}}</code></div>
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


def render_paygate_merchant_dashboard_html(base: str, wallet: str, invoices: list[dict[str, Any]]) -> str:
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
      <div><label>Network</label><select id="mNet"><option value="base">Base</option><option value="polygon">Polygon</option><option value="arbitrum">Arbitrum</option><option value="bsc">BSC BEP20</option><option value="solana">Solana</option><option value="ethereum">Ethereum</option></select></div>
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
