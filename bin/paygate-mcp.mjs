#!/usr/bin/env node
import readline from "node:readline";

const MCP_URL = process.env.PAYGATE_MCP_URL || "https://chainguard-studio-production.up.railway.app/paygate/mcp";
const API_KEY = process.env.CHAINGUARD_API_KEY || "";
const args = process.argv.slice(2);

if (args.includes("--version") || args.includes("-v")) {
  console.log("2.9.0");
  process.exit(0);
}
if (args.includes("--help") || args.includes("-h")) {
  console.log("PayGate USDT v2.9.0 — Zero-install 7-chain crypto checkout MCP bridge (npx -y github:EzequielLech/paygate-usdt)");
  process.exit(0);
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });
rl.on("line", async (line) => {
  const raw = line.trim();
  if (!raw) return;
  let msg;
  try {
    msg = JSON.parse(raw);
  } catch {
    process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "Parse error" } }) + "\n");
    return;
  }
  if (msg.id === undefined || msg.id === null) return;
  const headers = { "Content-Type": "application/json", "Accept": "application/json" };
  if (API_KEY) headers["X-API-Key"] = API_KEY;
  try {
    const res = await fetch(MCP_URL, { method: "POST", headers, body: JSON.stringify(msg) });
    const data = await res.json();
    process.stdout.write(JSON.stringify(data) + "\n");
  } catch (err) {
    process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id: msg.id, error: { code: -32603, message: String(err.message || err) } }) + "\n");
  }
});
