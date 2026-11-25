import crypto from "node:crypto";

export const DEFAULT_BASE_URL = "https://chainguard-studio-production.up.railway.app";

export const CAIP2_BY_NETWORK = Object.freeze({
  base: Object.freeze(["eip155:8453", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"]),
  polygon: Object.freeze(["eip155:137", "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"]),
  arbitrum: Object.freeze(["eip155:42161", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"]),
  ethereum: Object.freeze(["eip155:1", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"]),
  bsc: Object.freeze(["eip155:56", "0x55d398326f99059fF775485246999027B3197955"]),
  solana: Object.freeze(["solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"]),
});

/**
 * Verify `X-PayGate-Signature: sha256=<hex>` using constant-time HMAC-SHA256.
 */
export function verifyWebhookSignature(payload, signatureHeader, secret) {
  if (!secret || !signatureHeader) {
    return false;
  }
  const buf = Buffer.isBuffer(payload) ? payload : Buffer.from(String(payload), "utf8");
  const expected = "sha256=" + crypto.createHmac("sha256", String(secret)).update(buf).digest("hex");
  const expectedBuf = Buffer.from(expected, "utf8");
  const actualBuf = Buffer.from(String(signatureHeader).trim(), "utf8");
  if (expectedBuf.length !== actualBuf.length) {
    return false;
  }
  return crypto.timingSafeEqual(expectedBuf, actualBuf);
}

/**
 * Build an x402 v2 compliant HTTP 402 Payment Required challenge payload.
 */
export function buildX402Challenge({
  resourceUrl,
  description = "Paid API endpoint (non-custodial USDT/USDC)",
  priceUsd,
  evmRecipient = "",
  solanaRecipient = "",
  networks = ["base", "polygon", "arbitrum", "bsc", "solana"],
}) {
  const atomic6 = String(Math.round(Number(priceUsd) * 1_000_000));
  const evmTo = String(evmRecipient || "").trim();
  const solTo = String(solanaRecipient || "").trim();
  const accepts = [];

  for (const rawNet of networks) {
    const net = String(rawNet || "").toLowerCase().trim();
    const entry = CAIP2_BY_NETWORK[net];
    if (!entry) {
      continue;
    }
    const [caipNet, asset] = entry;
    const payTo = net === "solana" ? solTo : evmTo;
    if (!payTo) {
      continue;
    }
    accepts.push({
      scheme: "exact",
      network: caipNet,
      amount: atomic6,
      maxAmountRequired: atomic6,
      resource: resourceUrl,
      description,
      mimeType: "application/json",
      payTo,
      maxTimeoutSeconds: 300,
      asset,
    });
  }

  return {
    x402Version: 2,
    error: "Payment Required",
    protocol: "x402-direct-onchain",
    amount_usd: Number(priceUsd),
    accepts,
    retry_headers: {
      "X-Payment-Network": "base | polygon | arbitrum | bsc | ethereum | solana",
      "X-Payment-Tx": "<confirmed_onchain_tx_hash>",
      "X-PayGate-Invoice": "<paid_invoice_id>",
    },
  };
}

export class PayGateClient {
  constructor(baseUrl = DEFAULT_BASE_URL, fetchImpl = globalThis.fetch) {
    this.baseUrl = String(baseUrl || DEFAULT_BASE_URL).replace(/\/+$/, "");
    this.fetchImpl = fetchImpl;
  }

  async _requestJson(method, path, body = undefined) {
    if (typeof this.fetchImpl !== "function") {
      throw new Error("Fetch API is unavailable in this runtime.");
    }
    const headers = { "User-Agent": "paygate-usdt-js/2.6.0" };
    const init = { method, headers };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const resp = await this.fetchImpl(`${this.baseUrl}${path}`, init);
    const text = await resp.text();
    let parsed;
    try {
      parsed = text ? JSON.parse(text) : {};
    } catch {
      parsed = { raw: text };
    }
    if (!resp.ok) {
      const msg = parsed?.detail || parsed?.error || `HTTP ${resp.status}`;
      const err = new Error(String(msg));
      err.status = resp.status;
      err.body = parsed;
      throw err;
    }
    return parsed;
  }

  createInvoice({
    title,
    amountUsd,
    network = "polygon",
    merchantWallet,
    ttlMinutes = 60,
    webhookUrl = "",
    webhookSecret = "",
    redirectUrl = "",
    customerRef = "",
    billingDays = 0,
    metadata = {},
  }) {
    const mergedMeta = { ...(metadata || {}) };
    if (customerRef) {
      mergedMeta.customer_ref = String(customerRef);
    }
    if (Number(billingDays) > 0) {
      mergedMeta.billing_days = Number(billingDays);
    }
    return this._requestJson("POST", "/api/v1/paygate/invoices", {
      title,
      amount_usd: Number(amountUsd),
      network,
      merchant_wallet: merchantWallet,
      ttl_minutes: Number(ttlMinutes),
      webhook_url: webhookUrl,
      webhook_secret: webhookSecret,
      redirect_url: redirectUrl,
      metadata: mergedMeta,
    });
  }

  getInvoice(invoiceId) {
    return this._requestJson("GET", `/api/v1/paygate/invoices/${encodeURIComponent(invoiceId)}`);
  }

  listInvoices({ merchantWallet = "", status = "", customerRef = "", limit = 25 } = {}) {
    const params = new URLSearchParams();
    if (merchantWallet) params.set("merchant", merchantWallet);
    if (status) params.set("status", status);
    if (customerRef) params.set("customer_ref", customerRef);
    if (limit !== undefined) params.set("limit", String(limit));
    return this._requestJson("GET", `/api/v1/paygate/invoices?${params.toString()}`);
  }

  verifyInvoice(invoiceId, txHash) {
    return this._requestJson("POST", `/api/v1/paygate/invoices/${encodeURIComponent(invoiceId)}/verify`, {
      tx_hash: txHash,
    });
  }

  autoDetectInvoice(invoiceId) {
    return this._requestJson("POST", `/api/v1/paygate/invoices/${encodeURIComponent(invoiceId)}/auto-detect`, {});
  }

  expireInvoice(invoiceId, merchantWallet = "") {
    return this._requestJson("POST", `/api/v1/paygate/invoices/${encodeURIComponent(invoiceId)}/expire`, {
      merchant_wallet: merchantWallet,
    });
  }

  checkSubscription({ merchantWallet, customerRef, amountUsd = 9, network = "base", billingDays = 30 }) {
    const params = new URLSearchParams({
      merchant: merchantWallet,
      customer_ref: customerRef,
      amount: String(amountUsd),
      network,
      billing_days: String(billingDays),
    });
    return this._requestJson("GET", `/api/v1/paygate/subscriptions/check?${params.toString()}`);
  }

  buildPaymentUrl({
    merchantWallet,
    amountUsd,
    network = "base",
    title = "Instant Payment",
    customerRef = "",
    billingDays = 0,
    issuePass = false,
  }) {
    const params = new URLSearchParams({
      merchant: merchantWallet,
      amount: String(amountUsd),
      network,
      title,
    });
    if (customerRef) params.set("customer_ref", customerRef);
    if (Number(billingDays) > 0) params.set("billing_days", String(billingDays));
    if (issuePass) params.set("issue_pass", "1");
    return `${this.baseUrl}/paygate/pay?${params.toString()}`;
  }
}

function _getHeaderCaseInsensitive(headers, targetName) {
  if (!headers) return "";
  const lower = targetName.toLowerCase();
  if (typeof headers.get === "function") {
    return String(headers.get(targetName) || headers.get(lower) || "").trim();
  }
  for (const [k, v] of Object.entries(headers)) {
    if (k.toLowerCase() === lower) {
      return Array.isArray(v) ? String(v[0] || "").trim() : String(v || "").trim();
    }
  }
  return "";
}

/**
 * 1-line Express/Connect/Next.js HTTP 402 paywall middleware with x402 v2 headers
 * and direct PayGate invoice verification.
 */
export function paygateExpressMiddleware({
  priceUsd,
  merchantWallet,
  network = "base",
  description = "Paid API endpoint",
  solanaWallet = "",
  networks = ["base", "polygon", "arbitrum", "bsc"],
  baseUrl = DEFAULT_BASE_URL,
  client = undefined,
}) {
  const sdk = client || new PayGateClient(baseUrl);
  const targetPrice = Number(priceUsd);
  const expectedWallet = String(merchantWallet || "").trim().toLowerCase();

  return async function paygateMiddleware(req, res, next) {
    const invId = _getHeaderCaseInsensitive(req?.headers, "x-paygate-invoice");
    if (invId) {
      try {
        const resData = await sdk.getInvoice(invId);
        const inv = resData?.invoice || resData;
        if (
          inv &&
          inv.status === "paid" &&
          String(inv.merchant_wallet || "").trim().toLowerCase() === expectedWallet &&
          Number(inv.paid_amount_usd || 0) + 1e-6 >= targetPrice
        ) {
          req.paygate = { mode: "invoice", invoice: inv };
          return next();
        }
      } catch {
        // Fall through to 402 challenge if invoice lookup fails or is unpaid
      }
    }

    const host = _getHeaderCaseInsensitive(req?.headers, "host") || "localhost";
    const proto = req?.protocol || "https";
    const pathUrl = req?.originalUrl || req?.url || "/";
    const resourceUrl = `${proto}://${host}${pathUrl}`;

    const challenge = buildX402Challenge({
      resourceUrl,
      description,
      priceUsd: targetPrice,
      evmRecipient: merchantWallet,
      solanaRecipient: solanaWallet,
      networks,
    });
    const checkoutUrl = sdk.buildPaymentUrl({
      merchantWallet,
      amountUsd: targetPrice,
      network,
      title: description.slice(0, 80),
    });
    const body = { ...challenge, checkout_url: checkoutUrl };
    const encodedHeader = Buffer.from(JSON.stringify(challenge), "utf8").toString("base64");

    if (typeof res.set === "function") {
      res.set({
        "PAYMENT-REQUIRED": encodedHeader,
        "X-PAYMENT-REQUIRED": encodedHeader,
        "X-Payment-Protocol": "x402",
      });
    } else if (typeof res.setHeader === "function") {
      res.setHeader("PAYMENT-REQUIRED", encodedHeader);
      res.setHeader("X-PAYMENT-REQUIRED", encodedHeader);
      res.setHeader("X-Payment-Protocol", "x402");
    }
    return res.status(402).json(body);
  };
}

/**
 * 1-line paywall wrapper for @modelcontextprotocol/sdk TypeScript/Node.js tools.
 */
export function withMcpToolPaywall(
  {
    priceUsd,
    merchantWallet,
    network = "base",
    freeCalls = 3,
    billingDays = 30,
    title = "Pro MCP Tool Pass",
    baseUrl = DEFAULT_BASE_URL,
    client = undefined,
    usageStore = new Map(),
  },
  handler
) {
  const sdk = client || new PayGateClient(baseUrl);
  const targetPrice = Number(priceUsd);
  const expectedWallet = String(merchantWallet || "").trim().toLowerCase();

  return async function paywalledMcpTool(args = {}, extra = undefined) {
    const customerRef = String(args?.customer_ref || "anonymous").trim() || "anonymous";
    const invoiceId = String(args?.paygate_invoice_id || "").trim();

    if (invoiceId) {
      try {
        const resData = await sdk.getInvoice(invoiceId);
        const inv = resData?.invoice || resData;
        if (
          inv &&
          inv.status === "paid" &&
          String(inv.merchant_wallet || "").trim().toLowerCase() === expectedWallet &&
          Number(inv.paid_amount_usd || 0) + 1e-6 >= targetPrice
        ) {
          const result = await handler(args, extra);
          if (result && typeof result === "object" && !Array.isArray(result)) {
            return {
              ...result,
              paygate_billing: {
                tier: "paid",
                mode: "invoice",
                invoice_id: invoiceId,
              },
            };
          }
          return result;
        }
      } catch {
        // Fall through to subscription / free trial quota
      }
    }

    if (Number(billingDays) > 0 && customerRef !== "anonymous") {
      try {
        const sub = await sdk.checkSubscription({
          merchantWallet,
          customerRef,
          amountUsd: targetPrice,
          network,
          billingDays,
        });
        if (sub && sub.active) {
          const result = await handler(args, extra);
          if (result && typeof result === "object" && !Array.isArray(result)) {
            return {
              ...result,
              paygate_billing: {
                tier: "paid",
                mode: "subscription",
                expires_at: sub.expires_at,
                days_remaining: sub.days_remaining,
              },
            };
          }
          return result;
        }
      } catch {
        // Fall through to free quota
      }
    }

    const checkoutUrl = sdk.buildPaymentUrl({
      merchantWallet,
      amountUsd: targetPrice,
      network,
      title,
      customerRef: customerRef === "anonymous" ? "" : customerRef,
      billingDays,
    });

    const used = Number(usageStore.get(customerRef) || 0);
    if (used < Number(freeCalls)) {
      const nextUsed = used + 1;
      usageStore.set(customerRef, nextUsed);
      const remaining = Math.max(0, Number(freeCalls) - nextUsed);
      const result = await handler(args, extra);
      if (result && typeof result === "object" && !Array.isArray(result)) {
        return {
          ...result,
          paygate_billing: {
            tier: "free_trial",
            free_calls_remaining: remaining,
            checkout_url: checkoutUrl,
          },
        };
      }
      return result;
    }

    return {
      payment_required: true,
      error: `Free MCP tool quota reached (${freeCalls} calls). Pay $${targetPrice.toFixed(2)} USDT/USDC non-custodially to unlock.`,
      amount_usd: targetPrice,
      network,
      merchant_wallet: merchantWallet,
      checkout_url: checkoutUrl,
      renewal_url: checkoutUrl,
    };
  };
}
