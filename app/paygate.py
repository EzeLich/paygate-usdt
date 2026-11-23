"""
PayGate USDT — Multi-Tenant Telegram VIP & Digital Product Checkout Engine.
Collects USDT/USDC directly to creator self-custody wallets (Polygon, BSC, Arbitrum, Base, Solana)
without banks, Stripe, Whop, or monotributo, and auto-manages 1-use invites + expiry kicks.
"""
from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .config import StudioConfig
from .ledger import StudioLedger
from .onchain_verifier import OnChainPaymentVerifier

SUPPORTED_NETWORKS: dict[str, dict[str, str]] = {
    "polygon": {"label": "Polygon (USDT / USDC — comision ~$0.01)", "tokens": "USDT o USDC"},
    "bsc": {"label": "BNB Smart Chain BEP20 (USDT / USDC)", "tokens": "USDT o USDC"},
    "arbitrum": {"label": "Arbitrum One (USDT / USDC)", "tokens": "USDT o USDC"},
    "base": {"label": "Base (USDC / USDT)", "tokens": "USDC o USDT"},
    "solana": {"label": "Solana (USDC / USDT — Phantom)", "tokens": "USDC o USDT"},
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


def render_paygate_landing_html(base: str, stats: dict[str, Any]) -> str:
    evm_addr = html.escape(StudioConfig.PHANTOM_EVM_ADDRESS)
    sol_addr = html.escape(StudioConfig.PHANTOM_SOL_ADDRESS)
    return f"""<!DOCTYPE html><html lang="es"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>PayGate USDT — Cobrá tu Canal VIP o Producto Digital en USDT sin Banco ni Whop</title>
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
  <span class="badge">PAYGATE USDT · 0% COMISION POR VENTA · DIRECTO A TU WALLET</span>
  <h1>Cobrá tu Canal VIP de Telegram o Productos Digitales en USDT — <span style="color:#10b981;">sin banco, sin monotributo ni Whop</span>.</h1>
  <p class="sub">Tus miembros pagan en <strong>USDT / USDC (Polygon, BSC BEP20, Arbitrum, Base o Solana)</strong> directo a tu billetera Phantom, Trust, Binance o Bitget. El bot verifica la transaccion on-chain en segundos, entrega un enlace privado de 1 solo uso y expulsa automatico al vencer el mes.</p>

  <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:24px;">
    <a class="btn" href="#setup">⚡ Conectar mi Bot Gratis (60 seg)</a>
    <a class="btn2" href="https://t.me/ChainGuardVaultBot?start=vip" target="_blank">👀 Probar Demo en Telegram</a>
    <a class="btn2" href="#upgrade">👑 Activar Ilimitado ($29/mes o $79 unico)</a>
  </div>

  <div class="grid">
    <div class="card">
      <div style="color:#10b981;font-weight:800;font-size:.82rem;">1 · CERO INTERMEDIARIOS</div>
      <h3 style="margin:6px 0;">100% directo a tu wallet</h3>
      <p style="color:#94a3b8;font-size:.9rem;margin:0;">La plata nunca pasa por nuestra cuenta ni por un banco. El pago va directo de la wallet de tu comprador a tu propia direccion USDT/USDC.</p>
    </div>
    <div class="card">
      <div style="color:#10b981;font-weight:800;font-size:.82rem;">2 · CERO TRABAJO MANUAL</div>
      <h3 style="margin:6px 0;">Links únicos + expulsión automática</h3>
      <p style="color:#94a3b8;font-size:.9rem;margin:0;">Basta de pedir capturas por privado y llevar planillas. Cada compra valida el Tx Hash on-chain y genera un link privado de 1 solo ingreso.</p>
    </div>
    <div class="card">
      <div style="color:#10b981;font-weight:800;font-size:.82rem;">3 · LISTO EN 60 SEGUNDOS</div>
      <h3 style="margin:6px 0;">Sin programar nada</h3>
      <p style="color:#94a3b8;font-size:.9rem;margin:0;">Creas tu bot en @BotFather, pegas el token abajo, agregas el bot como admin en tu canal privado y ya estas cobrando. ({stats['total_bots']} bots creados · ${stats['total_volume_usdt']} USDT verificados)</p>
    </div>
  </div>

  <div class="card" id="setup" style="margin-top:20px;border-color:#10b981;">
    <h2 style="margin:0 0 6px;">🛠️ Conecta tu Bot VIP en 60 segundos (Gratis hasta 3 miembros)</h2>
    <p style="margin:0 0 12px;font-size:.88rem;color:#94a3b8;">Paso previo: en Telegram abrí <strong>@BotFather</strong> → enviá <code>/newbot</code> → copiá el token. Si tenés canal VIP, agregá tu bot como administrador con permiso para invitar y restringir usuarios.</p>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;">
      <div>
        <label>Nombre de tu Comunidad / Producto</label>
        <input id="bName" placeholder="Ej: Señales Pro Trading VIP"/>
      </div>
      <div>
        <label>Token de tu Bot (@BotFather)</label>
        <input id="bToken" placeholder="1234567890:AAF..."/>
      </div>
      <div>
        <label>Red donde queres cobrar (0% comision)</label>
        <select id="bNet">
          <option value="polygon">Polygon — USDT / USDC (recomendado)</option>
          <option value="bsc">BNB Smart Chain BEP20 — USDT / USDC</option>
          <option value="arbitrum">Arbitrum — USDT / USDC</option>
          <option value="base">Base — USDC / USDT</option>
          <option value="solana">Solana (Phantom) — USDC / USDT</option>
        </select>
      </div>
      <div>
        <label>Tu Wallet para recibir pagos (0x... o Solana)</label>
        <input id="bWallet" placeholder="0x... o direccion Solana"/>
      </div>
      <div>
        <label>ID de tu Canal/Grupo VIP (opcional, ej: -1001234567890)</label>
        <input id="bChan" placeholder="-100... (o dejalo vacio si entregas un link/archivo)"/>
      </div>
      <div>
        <label>Precio Plan Mensual (30 dias) en USDT</label>
        <input id="bPrice" type="number" step="0.5" value="25"/>
      </div>
    </div>
    <label>Entrega extra o link privado tras pagar (opcional — ideal para cursos, scripts o archivos)</label>
    <input id="bDeliv" placeholder="Ej: https://t.me/... o link de descarga / instrucciones"/>
    <div style="margin-top:14px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;">
      <button class="btn" onclick="crearBot()">⚡ Activar mi Bot Ahora</button>
      <span id="setupMsg" style="font-size:.9rem;"></span>
    </div>
    <div id="setupResult" style="margin-top:12px;"></div>
  </div>

  <div class="card" id="upgrade" style="margin-top:18px;">
    <h2 style="margin:0 0 8px;">👑 Planes PayGate USDT (Sin suscripción bancaria)</h2>
    <div class="grid" style="margin:12px 0;">
      <div class="card" style="background:#070b14;">
        <div style="font-weight:800;color:#94a3b8;">PRUEBA GRATIS</div>
        <div style="font-size:1.7rem;font-weight:900;margin:4px 0;">$0 <span style="font-size:.85rem;color:#94a3b8;">USDT</span></div>
        <p style="font-size:.86rem;color:#94a3b8;">Hasta 3 miembros activos reales para probar tu bot y validar cobros on-chain antes de pagar un centavo.</p>
      </div>
      <div class="card" style="background:#070b14;border-color:#10b981;">
        <div style="font-weight:800;color:#10b981;">PRO HOSTEADO</div>
        <div style="font-size:1.7rem;font-weight:900;margin:4px 0;">$29 <span style="font-size:.85rem;color:#94a3b8;">USDT / mes</span></div>
        <p style="font-size:.86rem;color:#94a3b8;">Miembros ilimitados, 0% comision sobre ventas, alertas de vencimiento y expulsion automatica 24/7 en nuestro servidor.</p>
      </div>
      <div class="card" style="background:#070b14;border-color:#38bdf8;">
        <div style="font-weight:800;color:#38bdf8;">LICENCIA COMPLETA</div>
        <div style="font-size:1.7rem;font-weight:900;margin:4px 0;">$79 <span style="font-size:.85rem;color:#94a3b8;">USDT pago unico</span></div>
        <p style="font-size:.86rem;color:#94a3b8;">Ilimitado para siempre sin abono mensual + marca blanca para tu comunidad.</p>
      </div>
    </div>
    <div style="background:#070b14;border:1px solid #1e293b;border-radius:10px;padding:14px;font-size:.88rem;">
      <strong>Para activar Pro ($29) o Licencia ($79) en cualquier bot ya creado:</strong><br/>
      1) Envia $29 o $79 (USDT/USDC) por <strong>Polygon / BSC / Arbitrum / Base</strong> a: <code>{evm_addr}</code><br/>
      &nbsp;&nbsp;&nbsp;&nbsp;o por <strong>Solana</strong> a: <code>{sol_addr}</code><br/>
      2) Abrí el Panel de tu bot (el link que te damos arriba al activarlo) y pegá el Tx Hash para desbloqueo instantaneo en 5 segundos.
    </div>
  </div>
</div>
<script>
async function crearBot() {{
  const msg = document.getElementById('setupMsg');
  const out = document.getElementById('setupResult');
  msg.className = ''; msg.textContent = 'Conectando con Telegram...';
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
    msg.textContent = d.detail || 'Error al configurar el bot.';
    return;
  }}
  msg.className = 'ok';
  msg.textContent = '¡Bot conectado y funcionando en vivo!';
  const panelUrl = location.origin + d.panel_url;
  out.innerHTML = `<div class="card" style="background:#052e16;border-color:#10b981;">
    <div>✅ <strong>@${{d.bot.bot_username}}</strong> ya esta online! Probalo ahora: <a style="color:#6ee7b7;font-weight:800;" href="https://t.me/${{d.bot.bot_username}}" target="_blank">t.me/${{d.bot.bot_username}}</a></div>
    <div style="margin-top:8px;">🔐 <strong>Guarda este link privado de tu Panel de Control:</strong><br/><code>${{panelUrl}}</code></div>
    <div style="margin-top:8px;"><a class="btn" href="${{panelUrl}}">Abrir mi Panel Ahora →</a></div>
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
