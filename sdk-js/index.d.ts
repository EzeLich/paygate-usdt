export declare const DEFAULT_BASE_URL: string;

export declare const CAIP2_BY_NETWORK: Readonly<Record<string, readonly [string, string]>>;

export interface X402AcceptOption {
  scheme: "exact";
  network: string;
  amount: string;
  maxAmountRequired: string;
  resource: string;
  description: string;
  mimeType: string;
  payTo: string;
  maxTimeoutSeconds: number;
  asset: string;
}

export interface X402Challenge {
  x402Version: 2;
  error: "Payment Required";
  protocol: "x402-direct-onchain";
  amount_usd: number;
  accepts: X402AcceptOption[];
  retry_headers: {
    "X-Payment-Network": string;
    "X-Payment-Tx": string;
    "X-PayGate-Invoice": string;
  };
}

export interface PayGateInvoice {
  invoice_id: string;
  created_at: string;
  expires_at: string;
  title: string;
  amount_usd: number;
  network: string;
  merchant_wallet: string;
  status: "pending" | "paid" | "expired" | string;
  tx_hash?: string;
  paid_amount_usd?: number;
  token?: string;
  payer?: string;
  paid_at?: string;
  metadata?: Record<string, unknown>;
}

export interface CreateInvoiceOptions {
  title: string;
  amountUsd: number;
  network?: string;
  merchantWallet: string;
  ttlMinutes?: number;
  webhookUrl?: string;
  webhookSecret?: string;
  redirectUrl?: string;
  customerRef?: string;
  billingDays?: number;
  metadata?: Record<string, unknown>;
}

export interface ListInvoicesOptions {
  merchantWallet?: string;
  status?: string;
  customerRef?: string;
  limit?: number;
}

export interface CheckSubscriptionOptions {
  merchantWallet: string;
  customerRef: string;
  amountUsd?: number;
  network?: string;
  billingDays?: number;
}

export interface BuildPaymentUrlOptions {
  merchantWallet: string;
  amountUsd: number;
  network?: string;
  title?: string;
  customerRef?: string;
  billingDays?: number;
  issuePass?: boolean;
}

export function verifyWebhookSignature(
  payload: string | Buffer,
  signatureHeader: string,
  secret: string
): boolean;

export function buildX402Challenge(options: {
  resourceUrl: string;
  description?: string;
  priceUsd: number;
  evmRecipient?: string;
  solanaRecipient?: string;
  networks?: string[];
}): X402Challenge;

export class PayGateClient {
  baseUrl: string;
  constructor(baseUrl?: string, fetchImpl?: typeof globalThis.fetch);
  createInvoice(options: CreateInvoiceOptions): Promise<{ ok: boolean; checkout_url: string; invoice: PayGateInvoice }>;
  getInvoice(invoiceId: string): Promise<{ ok: boolean; checkout_url?: string; invoice: PayGateInvoice }>;
  listInvoices(options?: ListInvoicesOptions): Promise<{ ok: boolean; count: number; invoices: PayGateInvoice[] }>;
  verifyInvoice(invoiceId: string, txHash: string): Promise<{ ok: boolean; invoice: PayGateInvoice }>;
  autoDetectInvoice(invoiceId: string): Promise<{ ok: boolean; invoice: PayGateInvoice }>;
  expireInvoice(invoiceId: string, merchantWallet?: string): Promise<{ ok: boolean; invoice: PayGateInvoice }>;
  checkSubscription(options: CheckSubscriptionOptions): Promise<{
    active: boolean;
    customer_ref: string;
    merchant_wallet: string;
    expires_at: string | null;
    days_remaining: number;
    renewal_url?: string;
  }>;
  buildPaymentUrl(options: BuildPaymentUrlOptions): string;
}

export interface ExpressMiddlewareOptions {
  priceUsd: number;
  merchantWallet: string;
  network?: string;
  description?: string;
  solanaWallet?: string;
  networks?: string[];
  baseUrl?: string;
  client?: PayGateClient;
}

export function paygateExpressMiddleware(
  options: ExpressMiddlewareOptions
): (req: any, res: any, next: () => void) => Promise<unknown>;

export interface McpPaywallOptions {
  priceUsd: number;
  merchantWallet: string;
  network?: string;
  freeCalls?: number;
  billingDays?: number;
  title?: string;
  baseUrl?: string;
  client?: PayGateClient;
  usageStore?: Map<string, number>;
}

export function withMcpToolPaywall<TArgs extends Record<string, unknown>, TResult>(
  options: McpPaywallOptions,
  handler: (args: TArgs, extra?: unknown) => Promise<TResult> | TResult
): (args?: TArgs, extra?: unknown) => Promise<TResult | Record<string, unknown>>;
