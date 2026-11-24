"""
SQLite Append-Only Treasury & Audit Ledger with SHA-256 integrity and anti-replay protection.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any


class StudioLedger:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS audits (
                    audit_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_name TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    risk_score INTEGER NOT NULL,
                    estimated_savings_usd REAL NOT NULL,
                    findings_count INTEGER NOT NULL,
                    report_json TEXT NOT NULL,
                    certificate_sha256 TEXT NOT NULL,
                    paid INTEGER NOT NULL DEFAULT 0,
                    tx_hash TEXT
                );

                CREATE TABLE IF NOT EXISTS payments (
                    tx_hash TEXT PRIMARY KEY,
                    verified_at TEXT NOT NULL,
                    network TEXT NOT NULL,
                    token TEXT NOT NULL,
                    amount_usd REAL NOT NULL,
                    sender TEXT,
                    recipient TEXT NOT NULL,
                    tier_id TEXT NOT NULL,
                    audit_id TEXT,
                    verification_mode TEXT NOT NULL,
                    raw_proof_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS api_passes (
                    api_key TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    calls_remaining INTEGER NOT NULL,
                    plan_id TEXT NOT NULL,
                    tx_hash TEXT NOT NULL,
                    owner_wallet TEXT
                );

                CREATE TABLE IF NOT EXISTS watchdogs (
                    watch_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_value TEXT NOT NULL,
                    network TEXT NOT NULL,
                    webhook_url TEXT NOT NULL,
                    baseline_hash TEXT,
                    last_status TEXT NOT NULL DEFAULT 'ACTIVE',
                    last_checked TEXT,
                    tx_hash TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS whitelabel_projects (
                    slug TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    project_name TEXT NOT NULL,
                    network TEXT NOT NULL,
                    token_address TEXT NOT NULL,
                    token_symbol TEXT NOT NULL,
                    token_decimals INTEGER NOT NULL DEFAULT 18,
                    telegram_url TEXT DEFAULT '',
                    website_url TEXT DEFAULT '',
                    referrer_wallet TEXT DEFAULT '',
                    tx_hash TEXT NOT NULL,
                    payment_network TEXT NOT NULL,
                    amount_usd REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS whop_companies (
                    company_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    brand_name TEXT NOT NULL DEFAULT 'My Community',
                    accent_color TEXT NOT NULL DEFAULT '#10b981',
                    plan TEXT NOT NULL DEFAULT 'free',
                    steps_json TEXT NOT NULL,
                    bonus_label TEXT NOT NULL DEFAULT 'Claim Welcome Bonus',
                    bonus_url TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS whop_reviews (
                    review_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    company_id TEXT NOT NULL,
                    experience_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    author_name TEXT NOT NULL,
                    rating INTEGER NOT NULL,
                    outcome_tag TEXT NOT NULL DEFAULT 'Verified Member',
                    comment TEXT NOT NULL,
                    approved INTEGER NOT NULL DEFAULT 1,
                    pinned INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS whop_onboarding (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_id TEXT NOT NULL,
                    experience_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    completed_steps_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(experience_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS paygate_bots (
                    bot_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    brand_name TEXT NOT NULL,
                    bot_token TEXT NOT NULL,
                    bot_username TEXT NOT NULL DEFAULT '',
                    channel_id TEXT NOT NULL DEFAULT '',
                    payout_network TEXT NOT NULL DEFAULT 'polygon',
                    payout_wallet TEXT NOT NULL,
                    plans_json TEXT NOT NULL,
                    license_tier TEXT NOT NULL DEFAULT 'trial',
                    admin_secret TEXT NOT NULL,
                    welcome_text TEXT NOT NULL DEFAULT '',
                    delivery_text TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS paygate_pending (
                    bot_id TEXT NOT NULL,
                    tg_user_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (bot_id, tg_user_id)
                );

                CREATE TABLE IF NOT EXISTS paygate_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    tg_user_id TEXT NOT NULL,
                    tg_username TEXT NOT NULL DEFAULT '',
                    plan_id TEXT NOT NULL,
                    plan_name TEXT NOT NULL,
                    amount_usd REAL NOT NULL,
                    network TEXT NOT NULL,
                    tx_hash TEXT NOT NULL,
                    invite_link TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    expires_at TEXT NOT NULL,
                    UNIQUE(bot_id, tx_hash)
                );

                CREATE TABLE IF NOT EXISTS polypulse_subs (
                    sub_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    tg_chat_id TEXT NOT NULL DEFAULT '',
                    webhook_url TEXT NOT NULL DEFAULT '',
                    min_usd REAL NOT NULL DEFAULT 5000.0,
                    watch_wallets_json TEXT NOT NULL DEFAULT '[]',
                    tier TEXT NOT NULL DEFAULT 'pro_30d',
                    tx_hash TEXT NOT NULL UNIQUE,
                    network TEXT NOT NULL,
                    amount_usd REAL NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_txs_json TEXT NOT NULL DEFAULT '[]'
                );

                CREATE TABLE IF NOT EXISTS paygate_b2b_orders (
                    order_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    package_id TEXT NOT NULL,
                    package_name TEXT NOT NULL,
                    full_price_usd REAL NOT NULL,
                    min_deposit_usd REAL NOT NULL,
                    client_name TEXT NOT NULL,
                    contact_handle TEXT NOT NULL,
                    project_url TEXT NOT NULL DEFAULT '',
                    requirements TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    tx_hash TEXT NOT NULL DEFAULT '',
                    network TEXT NOT NULL DEFAULT '',
                    paid_amount_usd REAL NOT NULL DEFAULT 0.0,
                    verified_at TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS paygate_invoices (
                    invoice_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    title TEXT NOT NULL,
                    amount_usd REAL NOT NULL,
                    network TEXT NOT NULL,
                    merchant_wallet TEXT NOT NULL,
                    webhook_url TEXT NOT NULL DEFAULT '',
                    webhook_secret TEXT NOT NULL DEFAULT '',
                    redirect_url TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    tx_hash TEXT NOT NULL DEFAULT '',
                    paid_amount_usd REAL NOT NULL DEFAULT 0.0,
                    token TEXT NOT NULL DEFAULT '',
                    payer TEXT NOT NULL DEFAULT '',
                    paid_at TEXT NOT NULL DEFAULT ''
                );
                """
            )

    def record_event(self, agent_name: str, action: str, detail: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_events (created_at, agent_name, action, detail) VALUES (?, ?, ?, ?)",
                (now, agent_name, action, detail),
            )

    def save_audit(
        self,
        target_type: str,
        target_name: str,
        tier: str,
        risk_score: int,
        estimated_savings_usd: float,
        report: dict[str, Any],
        paid: bool = False,
        tx_hash: str | None = None,
    ) -> dict[str, Any]:
        audit_id = f"aud_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        canonical = json.dumps(report, sort_keys=True, ensure_ascii=False)
        cert_sha = hashlib.sha256(f"{audit_id}:{now}:{canonical}".encode("utf-8")).hexdigest()
        findings_count = len(report.get("findings") or [])

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audits (
                    audit_id, created_at, target_type, target_name, tier,
                    risk_score, estimated_savings_usd, findings_count,
                    report_json, certificate_sha256, paid, tx_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    now,
                    target_type,
                    target_name,
                    tier,
                    risk_score,
                    estimated_savings_usd,
                    findings_count,
                    canonical,
                    cert_sha,
                    1 if paid else 0,
                    tx_hash,
                ),
            )
        return {
            "audit_id": audit_id,
            "created_at": now,
            "certificate_sha256": cert_sha,
            "paid": paid,
        }

    def get_audit(self, audit_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM audits WHERE audit_id = ?", (audit_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["report"] = json.loads(d.pop("report_json"))
        d["paid"] = bool(d["paid"])
        return d

    def mark_audit_paid(self, audit_id: str, tier: str, tx_hash: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE audits SET paid = 1, tier = ?, tx_hash = ? WHERE audit_id = ?",
                (tier, tx_hash, audit_id),
            )

    def is_tx_used(self, tx_hash: str) -> bool:
        norm = tx_hash.strip().lower()
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM payments WHERE lower(tx_hash) = ?", (norm,)).fetchone()
        return row is not None

    def record_payment(
        self,
        tx_hash: str,
        network: str,
        token: str,
        amount_usd: float,
        recipient: str,
        tier_id: str,
        verification_mode: str,
        raw_proof: dict[str, Any],
        sender: str = "",
        audit_id: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO payments (
                    tx_hash, verified_at, network, token, amount_usd,
                    sender, recipient, tier_id, audit_id, verification_mode, raw_proof_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tx_hash.strip(),
                    now,
                    network,
                    token,
                    amount_usd,
                    sender,
                    recipient,
                    tier_id,
                    audit_id,
                    verification_mode,
                    json.dumps(raw_proof, sort_keys=True),
                ),
            )

    def issue_api_pass(
        self,
        plan_id: str,
        tx_hash: str,
        calls: int = 1000,
        days: int = 30,
        owner_wallet: str = "",
    ) -> dict[str, Any]:
        api_key = f"cg_live_{uuid.uuid4().hex[:24]}"
        now_dt = datetime.now(timezone.utc)
        exp_dt = now_dt + timedelta(days=days)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO api_passes (api_key, created_at, expires_at, calls_remaining, plan_id, tx_hash, owner_wallet)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (api_key, now_dt.isoformat(), exp_dt.isoformat(), calls, plan_id, tx_hash, owner_wallet),
            )
        return {
            "api_key": api_key,
            "plan_id": plan_id,
            "calls_remaining": calls,
            "expires_at": exp_dt.isoformat(),
        }

    def validate_and_consume_pass(self, api_key: str) -> tuple[bool, dict[str, Any]]:
        key = (api_key or "").strip()
        if not key:
            return False, {"error": "Missing API key"}
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM api_passes WHERE api_key = ?", (key,)).fetchone()
            if not row:
                return False, {"error": "Invalid API key"}
            if row["expires_at"] < now_iso:
                return False, {"error": "API pass expired"}
            if int(row["calls_remaining"]) <= 0:
                return False, {"error": "API pass calls exhausted"}
            conn.execute(
                "UPDATE api_passes SET calls_remaining = calls_remaining - 1 WHERE api_key = ?",
                (key,),
            )
            return True, {
                "plan_id": row["plan_id"],
                "calls_remaining": int(row["calls_remaining"]) - 1,
                "expires_at": row["expires_at"],
            }

    def create_watchdog(
        self,
        target_type: str,
        target_value: str,
        network: str,
        webhook_url: str,
        tx_hash: str,
        baseline_hash: str = "",
    ) -> dict[str, Any]:
        watch_id = f"wd_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO watchdogs (
                    watch_id, created_at, target_type, target_value, network,
                    webhook_url, baseline_hash, last_status, last_checked, tx_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
                """,
                (watch_id, now, target_type, target_value, network, webhook_url, baseline_hash, now, tx_hash),
            )
        return {
            "watch_id": watch_id,
            "target_type": target_type,
            "target_value": target_value,
            "network": network,
            "baseline_hash": baseline_hash,
            "status": "ACTIVE",
        }

    def list_watchdogs(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM watchdogs ORDER BY created_at DESC LIMIT 50").fetchall()
        return [dict(r) for r in rows]

    def update_watchdog_status(self, watch_id: str, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE watchdogs SET last_status = ?, last_checked = ? WHERE watch_id = ?",
                (status, now, watch_id),
            )

    def create_whitelabel_project(
        self,
        slug: str,
        project_name: str,
        network: str,
        token_address: str,
        token_symbol: str,
        token_decimals: int,
        tx_hash: str,
        payment_network: str,
        amount_usd: float,
        telegram_url: str = "",
        website_url: str = "",
        referrer_wallet: str = "",
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        clean_slug = slug.strip().lower()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO whitelabel_projects (
                    slug, created_at, project_name, network, token_address, token_symbol,
                    token_decimals, telegram_url, website_url, referrer_wallet,
                    tx_hash, payment_network, amount_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_slug,
                    now,
                    project_name.strip(),
                    network.strip().lower(),
                    token_address.strip(),
                    token_symbol.strip().upper(),
                    int(token_decimals or 18),
                    telegram_url.strip(),
                    website_url.strip(),
                    referrer_wallet.strip(),
                    tx_hash.strip(),
                    payment_network.strip().lower(),
                    float(amount_usd),
                ),
            )
        return self.get_whitelabel_project(clean_slug) or {}

    def get_whitelabel_project(self, slug: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM whitelabel_projects WHERE lower(slug) = ?",
                (slug.strip().lower(),),
            ).fetchone()
        return dict(row) if row else None

    def list_whitelabel_projects(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT slug, created_at, project_name, network, token_address, token_symbol, telegram_url, website_url FROM whitelabel_projects ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
        return [dict(r) for r in rows]

    # --- WHOP APP STORE (`ProofPulse`: Verified Reviews + Onboarding + Proof Wall) ---
    DEFAULT_WHOP_STEPS = [
        {"id": "step_1", "title": "Introduce yourself in the community chat", "hint": "Say hello and share what goal you are chasing this month."},
        {"id": "step_2", "title": "Read the Start-Here playbook", "hint": "Takes 2 minutes and unlocks your full roadmap."},
        {"id": "step_3", "title": "Leave your verified Day-1 impression", "hint": "Helps keep the community high-signal and unlocks your bonus."},
    ]

    def get_or_create_whop_company(self, company_id: str) -> dict[str, Any]:
        cid = (company_id or "biz_demo").strip()[:64]
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM whop_companies WHERE company_id = ?", (cid,)).fetchone()
            if not row:
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """
                    INSERT INTO whop_companies (company_id, created_at, brand_name, accent_color, plan, steps_json, bonus_label, bonus_url)
                    VALUES (?, ?, ?, '#10b981', 'free', ?, 'Claim Member Quick-Start Pack', '')
                    """,
                    (cid, now, "Community Hub" if cid.startswith("biz_") else cid, json.dumps(self.DEFAULT_WHOP_STEPS)),
                )
                row = conn.execute("SELECT * FROM whop_companies WHERE company_id = ?", (cid,)).fetchone()
        d = dict(row)
        d["steps"] = json.loads(d.pop("steps_json"))
        return d

    def update_whop_company(
        self,
        company_id: str,
        brand_name: str | None = None,
        accent_color: str | None = None,
        steps: list[dict[str, str]] | None = None,
        bonus_label: str | None = None,
        bonus_url: str | None = None,
        plan: str | None = None,
    ) -> dict[str, Any]:
        cur = self.get_or_create_whop_company(company_id)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE whop_companies
                SET brand_name = ?, accent_color = ?, steps_json = ?, bonus_label = ?, bonus_url = ?, plan = ?
                WHERE company_id = ?
                """,
                (
                    (brand_name if brand_name is not None else cur["brand_name"])[:64],
                    (accent_color if accent_color is not None else cur["accent_color"])[:16],
                    json.dumps(steps if steps is not None else cur["steps"]),
                    (bonus_label if bonus_label is not None else cur["bonus_label"])[:80],
                    (bonus_url if bonus_url is not None else cur["bonus_url"])[:300],
                    (plan if plan is not None else cur["plan"])[:24],
                    cur["company_id"],
                ),
            )
        return self.get_or_create_whop_company(cur["company_id"])

    def add_whop_review(
        self,
        company_id: str,
        experience_id: str,
        user_id: str,
        author_name: str,
        rating: int,
        outcome_tag: str,
        comment: str,
    ) -> dict[str, Any]:
        comp = self.get_or_create_whop_company(company_id)
        with self._connect() as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM whop_reviews WHERE company_id = ?", (comp["company_id"],)).fetchone()[0]
            if comp["plan"] == "free" and int(cnt) >= 10:
                raise ValueError("Free tier limit (10 verified reviews) reached. Upgrade to Pro in Dashboard to unlock unlimited reviews.")
            rid = f"rev_{uuid.uuid4().hex[:10]}"
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO whop_reviews (review_id, created_at, company_id, experience_id, user_id, author_name, rating, outcome_tag, comment, approved, pinned)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0)
                """,
                (
                    rid,
                    now,
                    comp["company_id"],
                    experience_id.strip()[:64],
                    user_id.strip()[:64],
                    author_name.strip()[:48] or "Verified Member",
                    max(1, min(5, int(rating))),
                    outcome_tag.strip()[:36] or "Verified Member",
                    comment.strip()[:500],
                ),
            )
            row = conn.execute("SELECT * FROM whop_reviews WHERE review_id = ?", (rid,)).fetchone()
        return dict(row)

    def list_whop_reviews(self, company_id: str, approved_only: bool = False) -> list[dict[str, Any]]:
        cid = (company_id or "biz_demo").strip()[:64]
        sql = "SELECT * FROM whop_reviews WHERE company_id = ?"
        if approved_only:
            sql += " AND approved = 1"
        sql += " ORDER BY pinned DESC, created_at DESC LIMIT 100"
        with self._connect() as conn:
            rows = conn.execute(sql, (cid,)).fetchall()
        return [dict(r) for r in rows]

    def moderate_whop_review(self, company_id: str, review_id: str, approved: bool | None = None, pinned: bool | None = None) -> dict[str, Any] | None:
        cid = (company_id or "biz_demo").strip()[:64]
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM whop_reviews WHERE company_id = ? AND review_id = ?", (cid, review_id)).fetchone()
            if not row:
                return None
            new_app = int(approved) if approved is not None else int(row["approved"])
            new_pin = int(pinned) if pinned is not None else int(row["pinned"])
            conn.execute(
                "UPDATE whop_reviews SET approved = ?, pinned = ? WHERE company_id = ? AND review_id = ?",
                (new_app, new_pin, cid, review_id),
            )
            updated = conn.execute("SELECT * FROM whop_reviews WHERE review_id = ?", (review_id,)).fetchone()
        return dict(updated) if updated else None

    def toggle_whop_onboarding_step(self, company_id: str, experience_id: str, user_id: str, step_id: str) -> list[str]:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT completed_steps_json FROM whop_onboarding WHERE experience_id = ? AND user_id = ?",
                (experience_id, user_id),
            ).fetchone()
            steps = set(json.loads(row["completed_steps_json"])) if row else set()
            if step_id in steps:
                steps.remove(step_id)
            else:
                steps.add(step_id)
            out = sorted(steps)
            conn.execute(
                """
                INSERT INTO whop_onboarding (company_id, experience_id, user_id, completed_steps_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(experience_id, user_id) DO UPDATE SET completed_steps_json = excluded.completed_steps_json, updated_at = excluded.updated_at
                """,
                (company_id, experience_id, user_id, json.dumps(out), now),
            )
        return out

    def get_whop_onboarding_progress(self, experience_id: str, user_id: str) -> list[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT completed_steps_json FROM whop_onboarding WHERE experience_id = ? AND user_id = ?",
                (experience_id, user_id),
            ).fetchone()
        return json.loads(row["completed_steps_json"]) if row else []

    def get_whop_company_metrics(self, company_id: str) -> dict[str, Any]:
        comp = self.get_or_create_whop_company(company_id)
        reviews = self.list_whop_reviews(comp["company_id"])
        approved = [r for r in reviews if r["approved"]]
        avg = round(sum(int(r["rating"]) for r in approved) / len(approved), 2) if approved else 5.0
        with self._connect() as conn:
            onb_rows = conn.execute(
                "SELECT completed_steps_json FROM whop_onboarding WHERE company_id = ?",
                (comp["company_id"],),
            ).fetchall()
        total_steps = max(1, len(comp["steps"]))
        completed_full = sum(1 for r in onb_rows if len(json.loads(r["completed_steps_json"])) >= total_steps)
        return {
            "company": comp,
            "total_reviews": len(reviews),
            "approved_reviews": len(approved),
            "avg_rating": avg,
            "members_onboarded": len(onb_rows),
            "members_completed_all": completed_full,
        }

    # ------------------------------------------------------------------
    # PayGate USDT Multi-Tenant Telegram VIP & Digital Delivery Ledger
    # ------------------------------------------------------------------
    @staticmethod
    def _default_paygate_plans() -> list[dict[str, Any]]:
        return [
            {"id": "mensual", "name": "Acceso VIP 30 Dias", "price_usd": 25.0, "days": 30},
            {"id": "trimestral", "name": "Pase 90 Dias (Ahorras 20%)", "price_usd": 60.0, "days": 90},
        ]

    @staticmethod
    def _row_to_paygate_bot(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["plans"] = json.loads(d.pop("plans_json") or "[]")
        return d

    def create_paygate_bot(
        self,
        brand_name: str,
        bot_token: str,
        bot_username: str,
        channel_id: str,
        payout_network: str,
        payout_wallet: str,
        plans: list[dict[str, Any]] | None = None,
        welcome_text: str = "",
        delivery_text: str = "",
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        bot_id = f"pg_{uuid.uuid4().hex[:10]}"
        admin_secret = secrets.token_urlsafe(18)
        plan_list = plans or self._default_paygate_plans()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO paygate_bots (
                    bot_id, created_at, brand_name, bot_token, bot_username, channel_id,
                    payout_network, payout_wallet, plans_json, license_tier, admin_secret,
                    welcome_text, delivery_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'trial', ?, ?, ?)
                """,
                (
                    bot_id,
                    now,
                    brand_name.strip()[:80] or "Canal VIP USDT",
                    bot_token.strip(),
                    bot_username.strip().lstrip("@")[:64],
                    channel_id.strip()[:64],
                    payout_network.strip().lower()[:20] or "polygon",
                    payout_wallet.strip()[:120],
                    json.dumps(plan_list),
                    admin_secret,
                    welcome_text.strip()[:500],
                    delivery_text.strip()[:500],
                ),
            )
            row = conn.execute("SELECT * FROM paygate_bots WHERE bot_id = ?", (bot_id,)).fetchone()
        return self._row_to_paygate_bot(row)

    def get_paygate_bot(self, bot_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM paygate_bots WHERE bot_id = ?", (bot_id.strip(),)).fetchone()
        return self._row_to_paygate_bot(row) if row else None

    def update_paygate_bot(
        self,
        bot_id: str,
        *,
        brand_name: str | None = None,
        channel_id: str | None = None,
        payout_network: str | None = None,
        payout_wallet: str | None = None,
        plans: list[dict[str, Any]] | None = None,
        license_tier: str | None = None,
        welcome_text: str | None = None,
        delivery_text: str | None = None,
    ) -> dict[str, Any] | None:
        cur = self.get_paygate_bot(bot_id)
        if not cur:
            return None
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE paygate_bots SET
                    brand_name = ?, channel_id = ?, payout_network = ?, payout_wallet = ?,
                    plans_json = ?, license_tier = ?, welcome_text = ?, delivery_text = ?
                WHERE bot_id = ?
                """,
                (
                    (brand_name.strip()[:80] if brand_name is not None else cur["brand_name"]),
                    (channel_id.strip()[:64] if channel_id is not None else cur["channel_id"]),
                    (payout_network.strip().lower()[:20] if payout_network is not None else cur["payout_network"]),
                    (payout_wallet.strip()[:120] if payout_wallet is not None else cur["payout_wallet"]),
                    json.dumps(plans if plans is not None else cur["plans"]),
                    (license_tier.strip()[:20] if license_tier is not None else cur["license_tier"]),
                    (welcome_text.strip()[:500] if welcome_text is not None else cur["welcome_text"]),
                    (delivery_text.strip()[:500] if delivery_text is not None else cur["delivery_text"]),
                    cur["bot_id"],
                ),
            )
            row = conn.execute("SELECT * FROM paygate_bots WHERE bot_id = ?", (cur["bot_id"],)).fetchone()
        return self._row_to_paygate_bot(row) if row else None

    def set_paygate_user_pending(self, bot_id: str, tg_user_id: str, plan_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO paygate_pending (bot_id, tg_user_id, plan_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(bot_id, tg_user_id) DO UPDATE SET plan_id = excluded.plan_id, updated_at = excluded.updated_at
                """,
                (bot_id, str(tg_user_id), plan_id, now),
            )

    def get_paygate_user_pending(self, bot_id: str, tg_user_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT plan_id FROM paygate_pending WHERE bot_id = ? AND tg_user_id = ?",
                (bot_id, str(tg_user_id)),
            ).fetchone()
        return str(row["plan_id"]) if row else None

    def is_paygate_tx_used(self, bot_id: str, tx_hash: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM paygate_members WHERE bot_id = ? AND LOWER(tx_hash) = LOWER(?)",
                (bot_id, tx_hash.strip()),
            ).fetchone()
        return row is not None

    def count_active_paygate_members(self, bot_id: str) -> int:
        with self._connect() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM paygate_members WHERE bot_id = ? AND status = 'active'",
                    (bot_id,),
                ).fetchone()[0]
            )

    def activate_paygate_member(
        self,
        *,
        bot_id: str,
        tg_user_id: str,
        tg_username: str,
        plan_id: str,
        plan_name: str,
        amount_usd: float,
        network: str,
        tx_hash: str,
        invite_link: str,
        days: int,
    ) -> dict[str, Any]:
        now_dt = datetime.now(timezone.utc)
        exp_dt = now_dt + timedelta(days=max(1, int(days)))
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO paygate_members (
                    created_at, bot_id, tg_user_id, tg_username, plan_id, plan_name,
                    amount_usd, network, tx_hash, invite_link, status, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                """,
                (
                    now_dt.isoformat(),
                    bot_id,
                    str(tg_user_id),
                    tg_username.strip()[:64],
                    plan_id,
                    plan_name,
                    float(amount_usd),
                    network,
                    tx_hash.strip(),
                    invite_link,
                    exp_dt.isoformat(),
                ),
            )
            row = conn.execute("SELECT * FROM paygate_members WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def list_paygate_members(self, bot_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paygate_members WHERE bot_id = ? ORDER BY id DESC LIMIT 100",
                (bot_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_expired_paygate_members(self, now_iso: str | None = None) -> list[dict[str, Any]]:
        cutoff = now_iso or datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paygate_members WHERE status = 'active' AND expires_at <= ? LIMIT 100",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_paygate_member_kicked(self, member_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE paygate_members SET status = 'kicked' WHERE id = ?", (member_id,))

    def get_paygate_global_stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            bots = conn.execute("SELECT COUNT(*) FROM paygate_bots").fetchone()[0]
            active_m = conn.execute("SELECT COUNT(*) FROM paygate_members WHERE status = 'active'").fetchone()[0]
            vol = conn.execute("SELECT COALESCE(SUM(amount_usd), 0.0) FROM paygate_members").fetchone()[0]
        return {"total_bots": int(bots), "active_members": int(active_m), "total_volume_usdt": round(float(vol), 2)}

    # ------------------------------------------------------------------
    # PolyPulse — Polymarket Data API v2 Pro Alert Subscriptions
    # ------------------------------------------------------------------
    def create_polypulse_sub(
        self,
        *,
        tg_chat_id: str,
        webhook_url: str,
        min_usd: float,
        watch_wallets: list[str],
        tier: str,
        tx_hash: str,
        network: str,
        amount_usd: float,
        days: int,
    ) -> dict[str, Any]:
        now_dt = datetime.now(timezone.utc)
        exp_dt = now_dt + timedelta(days=max(1, int(days)))
        sub_id = f"pp_{uuid.uuid4().hex[:10]}"
        wallets_norm = [w.strip().lower() for w in watch_wallets if w.strip()][:25]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO polypulse_subs (
                    sub_id, created_at, tg_chat_id, webhook_url, min_usd,
                    watch_wallets_json, tier, tx_hash, network, amount_usd, expires_at, last_seen_txs_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]')
                """,
                (
                    sub_id,
                    now_dt.isoformat(),
                    tg_chat_id.strip()[:64],
                    webhook_url.strip()[:300],
                    max(500.0, float(min_usd)),
                    json.dumps(wallets_norm),
                    tier,
                    tx_hash.strip(),
                    network.strip(),
                    float(amount_usd),
                    exp_dt.isoformat(),
                ),
            )
            row = conn.execute("SELECT * FROM polypulse_subs WHERE sub_id = ?", (sub_id,)).fetchone()
        out = dict(row)
        out["watch_wallets"] = json.loads(out.pop("watch_wallets_json") or "[]")
        out["last_seen_txs"] = json.loads(out.pop("last_seen_txs_json") or "[]")
        return out

    def list_active_polypulse_subs(self) -> list[dict[str, Any]]:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM polypulse_subs WHERE expires_at > ? ORDER BY created_at DESC LIMIT 200",
                (now_iso,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["watch_wallets"] = json.loads(d.pop("watch_wallets_json") or "[]")
            d["last_seen_txs"] = json.loads(d.pop("last_seen_txs_json") or "[]")
            out.append(d)
        return out

    def update_polypulse_sub_seen(self, sub_id: str, seen_txs: list[str]) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE polypulse_subs SET last_seen_txs_json = ? WHERE sub_id = ?",
                (json.dumps(seen_txs[-80:]), sub_id),
            )

    def create_paygate_b2b_order(
        self,
        *,
        package_id: str,
        package_name: str,
        full_price_usd: float,
        min_deposit_usd: float,
        client_name: str,
        contact_handle: str,
        project_url: str = "",
        requirements: str = "",
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        order_id = f"b2b_{uuid.uuid4().hex[:10]}"
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO paygate_b2b_orders (
                    order_id, created_at, package_id, package_name,
                    full_price_usd, min_deposit_usd, client_name,
                    contact_handle, project_url, requirements, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    order_id,
                    now,
                    package_id.strip()[:40],
                    package_name.strip()[:120],
                    float(full_price_usd),
                    float(min_deposit_usd),
                    client_name.strip()[:100],
                    contact_handle.strip()[:120],
                    project_url.strip()[:240],
                    requirements.strip()[:1000],
                ),
            )
            row = conn.execute("SELECT * FROM paygate_b2b_orders WHERE order_id = ?", (order_id,)).fetchone()
        return dict(row)

    def get_paygate_b2b_order(self, order_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM paygate_b2b_orders WHERE order_id = ?", (order_id.strip(),)).fetchone()
        return dict(row) if row else None

    def mark_paygate_b2b_order_paid(
        self,
        order_id: str,
        *,
        tx_hash: str,
        network: str,
        paid_amount_usd: float,
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM paygate_b2b_orders WHERE order_id = ?", (order_id.strip(),)).fetchone()
            if not row:
                return None
            new_status = "paid" if float(paid_amount_usd) + 0.01 >= float(row["full_price_usd"]) else "deposit_verified"
            conn.execute(
                """
                UPDATE paygate_b2b_orders
                SET status = ?, tx_hash = ?, network = ?, paid_amount_usd = ?, verified_at = ?
                WHERE order_id = ?
                """,
                (new_status, tx_hash.strip(), network.strip(), float(paid_amount_usd), now, order_id.strip()),
            )
            updated = conn.execute("SELECT * FROM paygate_b2b_orders WHERE order_id = ?", (order_id.strip(),)).fetchone()
        return dict(updated) if updated else None

    def create_paygate_invoice(
        self,
        *,
        title: str,
        amount_usd: float,
        network: str,
        merchant_wallet: str,
        ttl_minutes: int = 60,
        webhook_url: str = "",
        webhook_secret: str = "",
        redirect_url: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now_dt = datetime.now(timezone.utc)
        exp_dt = now_dt + timedelta(minutes=max(5, min(43200, int(ttl_minutes))))
        invoice_id = f"inv_{uuid.uuid4().hex[:12]}"
        meta_json = json.dumps(metadata or {})
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO paygate_invoices (
                    invoice_id, created_at, expires_at, title, amount_usd,
                    network, merchant_wallet, webhook_url, webhook_secret,
                    redirect_url, metadata_json, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    invoice_id,
                    now_dt.isoformat(),
                    exp_dt.isoformat(),
                    title.strip()[:140],
                    float(amount_usd),
                    network.strip().lower()[:24],
                    merchant_wallet.strip()[:120],
                    webhook_url.strip()[:300],
                    webhook_secret.strip()[:128],
                    redirect_url.strip()[:300],
                    meta_json,
                ),
            )
            row = conn.execute("SELECT * FROM paygate_invoices WHERE invoice_id = ?", (invoice_id,)).fetchone()
        out = dict(row)
        out["metadata"] = json.loads(out.pop("metadata_json") or "{}")
        return out

    def get_paygate_invoice(self, invoice_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM paygate_invoices WHERE invoice_id = ?", (invoice_id.strip(),)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["metadata"] = json.loads(out.pop("metadata_json") or "{}")
        return out

    def mark_paygate_invoice_paid(
        self,
        invoice_id: str,
        *,
        tx_hash: str,
        paid_amount_usd: float,
        token: str,
        payer: str,
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE paygate_invoices
                SET status = 'paid', tx_hash = ?, paid_amount_usd = ?, token = ?, payer = ?, paid_at = ?
                WHERE invoice_id = ?
                """,
                (tx_hash.strip(), float(paid_amount_usd), token.strip()[:24], payer.strip()[:120], now, invoice_id.strip()),
            )
            row = conn.execute("SELECT * FROM paygate_invoices WHERE invoice_id = ?", (invoice_id.strip(),)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["metadata"] = json.loads(out.pop("metadata_json") or "{}")
        return out

    def get_stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            total_audits = conn.execute("SELECT COUNT(*) FROM audits").fetchone()[0]
            paid_audits = conn.execute("SELECT COUNT(*) FROM audits WHERE paid = 1").fetchone()[0]
            total_rev = conn.execute("SELECT COALESCE(SUM(amount_usd), 0.0) FROM payments").fetchone()[0]
            payments_count = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
            active_passes = conn.execute("SELECT COUNT(*) FROM api_passes WHERE calls_remaining > 0").fetchone()[0]
            active_watchdogs = conn.execute("SELECT COUNT(*) FROM watchdogs").fetchone()[0]
            recent_events = [
                dict(r)
                for r in conn.execute(
                    "SELECT created_at, agent_name, action, detail FROM agent_events ORDER BY id DESC LIMIT 12"
                ).fetchall()
            ]
            recent_payments = [
                dict(r)
                for r in conn.execute(
                    "SELECT tx_hash, verified_at, network, token, amount_usd, tier_id FROM payments ORDER BY verified_at DESC LIMIT 10"
                ).fetchall()
            ]
        return {
            "total_audits": int(total_audits),
            "paid_audits": int(paid_audits),
            "verified_payments_count": int(payments_count),
            "active_api_passes": int(active_passes),
            "active_watchdogs": int(active_watchdogs),
            "verified_revenue_usdt": round(float(total_rev), 2),
            "recent_events": recent_events,
            "recent_payments": recent_payments,
        }
