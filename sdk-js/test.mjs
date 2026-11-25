import test from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";

import {
  PayGateClient,
  buildX402Challenge,
  paygateExpressMiddleware,
  verifyWebhookSignature,
  withMcpToolPaywall,
} from "./index.mjs";

test("verifyWebhookSignature validates HMAC-SHA256 payloads safely", () => {
  const payload = JSON.stringify({ event: "invoice.paid", invoice_id: "inv_test123" });
  const secret = "whsec_unit_test_secret";
  const goodSig = "sha256=" + crypto.createHmac("sha256", secret).update(payload, "utf8").digest("hex");

  assert.equal(verifyWebhookSignature(payload, goodSig, secret), true);
  assert.equal(verifyWebhookSignature(payload + " ", goodSig, secret), false);
  assert.equal(verifyWebhookSignature(payload, "sha256=deadbeef", secret), false);
  assert.equal(verifyWebhookSignature(payload, goodSig, ""), false);
});

test("buildX402Challenge produces EIP-55 assets and Solana option", () => {
  const ch = buildX402Challenge({
    resourceUrl: "https://api.example.com/pro",
    priceUsd: 2.5,
    evmRecipient: "0x4Ed4D0750453C027FA8398067d5af980Bcc9B6eD",
    solanaRecipient: "3fZSMAyCEMhZwWiynbJDjoYNUT97aiV9BLzoUNroEMAz",
    networks: ["base", "solana"],
  });

  assert.equal(ch.x402Version, 2);
  assert.equal(ch.amount_usd, 2.5);
  assert.equal(ch.accepts.length, 2);
  assert.equal(ch.accepts[0].network, "eip155:8453");
  assert.equal(ch.accepts[0].asset, "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913");
  assert.equal(ch.accepts[0].amount, "2500000");
  assert.equal(ch.accepts[1].network, "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp");
});

test("PayGateClient.buildPaymentUrl builds deterministic checkout URL", () => {
  const client = new PayGateClient("https://example.up.railway.app/");
  const url = client.buildPaymentUrl({
    merchantWallet: "0x4Ed4D0750453C027FA8398067d5af980Bcc9B6eD",
    amountUsd: 9,
    network: "base",
    title: "Pro API",
    customerRef: "usr_42",
    billingDays: 30,
  });
  assert.ok(url.startsWith("https://example.up.railway.app/paygate/pay?"));
  assert.ok(url.includes("customer_ref=usr_42"));
  assert.ok(url.includes("billing_days=30"));
});

test("paygateExpressMiddleware emits 402 unpaid and unlocks paid invoices", async () => {
  const merchant = "0x4Ed4D0750453C027FA8398067d5af980Bcc9B6eD";
  const fakeClient = {
    buildPaymentUrl: () => "https://example.up.railway.app/paygate/pay?amount=5",
    getInvoice: async (id) => ({
      invoice: {
        invoice_id: id,
        status: "paid",
        merchant_wallet: merchant,
        paid_amount_usd: 5.0,
      },
    }),
  };

  const mw = paygateExpressMiddleware({
    priceUsd: 5.0,
    merchantWallet: merchant,
    client: fakeClient,
  });

  // Unpaid request -> 402 + dual headers
  const headersSent = {};
  let statusCode = 0;
  let jsonBody = null;
  const res402 = {
    set(obj) {
      Object.assign(headersSent, obj);
    },
    status(code) {
      statusCode = code;
      return {
        json(body) {
          jsonBody = body;
          return body;
        },
      };
    },
  };
  let nextCalled = false;
  await mw({ headers: { host: "api.example.com" }, originalUrl: "/signal" }, res402, () => {
    nextCalled = true;
  });

  assert.equal(nextCalled, false);
  assert.equal(statusCode, 402);
  assert.ok(headersSent["PAYMENT-REQUIRED"]);
  assert.ok(headersSent["X-PAYMENT-REQUIRED"]);
  assert.equal(jsonBody.checkout_url, "https://example.up.railway.app/paygate/pay?amount=5");

  // Paid invoice header -> next()
  const reqPaid = { headers: { "x-paygate-invoice": "inv_paid123" } };
  await mw(reqPaid, res402, () => {
    nextCalled = true;
  });
  assert.equal(nextCalled, true);
  assert.equal(reqPaid.paygate.mode, "invoice");
});

test("withMcpToolPaywall enforces free trial quota and unlocks on paid invoice", async () => {
  const merchant = "0x4Ed4D0750453C027FA8398067d5af980Bcc9B6eD";
  const fakeClient = {
    buildPaymentUrl: () => "https://example.up.railway.app/paygate/pay?amount=9",
    checkSubscription: async () => ({ active: false }),
    getInvoice: async (id) => ({
      invoice: {
        invoice_id: id,
        status: id === "inv_ok" ? "paid" : "pending",
        merchant_wallet: merchant,
        paid_amount_usd: 9.0,
      },
    }),
  };

  const tool = withMcpToolPaywall(
    {
      priceUsd: 9,
      merchantWallet: merchant,
      freeCalls: 1,
      client: fakeClient,
    },
    async ({ symbol }) => ({ symbol, verdict: "BUY" })
  );

  const first = await tool({ symbol: "ETH", customer_ref: "agent_1" });
  assert.equal(first.verdict, "BUY");
  assert.equal(first.paygate_billing.tier, "free_trial");
  assert.equal(first.paygate_billing.free_calls_remaining, 0);

  const second = await tool({ symbol: "ETH", customer_ref: "agent_1" });
  assert.equal(second.payment_required, true);
  assert.equal(second.checkout_url, "https://example.up.railway.app/paygate/pay?amount=9");

  const third = await tool({ symbol: "ETH", customer_ref: "agent_1", paygate_invoice_id: "inv_ok" });
  assert.equal(third.verdict, "BUY");
  assert.equal(third.paygate_billing.tier, "paid");
  assert.equal(third.paygate_billing.invoice_id, "inv_ok");
});
