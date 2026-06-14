/**
 * Kraken Crypto Signals — x402 demo agent
 * ---------------------------------------
 * Demonstrates two things, in this order:
 *   1) The FREE preview endpoint  /signals/preview   — works without any wallet.
 *   2) A paid call to             /indicators        — auto-pays $0.005 USDC on
 *      Base mainnet via x402 IF a WALLET_PRIVATE_KEY is set; otherwise it
 *      makes a plain GET and prints the 402 challenge so you can see what
 *      your agent would receive in production.
 *
 * Mirrors the @x402/axios + viem pattern used in ../../mcp/server.ts.
 *
 * Usage:
 *   cp .env.example .env
 *   npm install
 *   node agent.js
 *
 * SAFETY: A paid call spends a small amount of REAL USDC on Base. Use a
 *         dedicated low-balance hot wallet — never your main one.
 */
import "dotenv/config";
import axios from "axios";

const BASE_URL = process.env.BASE_URL || "https://signals.nsgoods.org";
const KEY = process.env.WALLET_PRIVATE_KEY?.trim() || "";

function banner(title) {
  console.log("\n" + "=".repeat(72));
  console.log(title);
  console.log("=".repeat(72));
}

function pretty(x) {
  try { return JSON.stringify(x, null, 2); } catch { return String(x); }
}

async function freePreview() {
  banner("1) FREE preview  ·  GET /signals/preview  (no wallet needed)");
  try {
    const r = await axios.get(`${BASE_URL}/signals/preview`, { timeout: 15000 });
    console.log(`HTTP ${r.status}`);
    console.log(pretty(r.data));
    return true;
  } catch (err) {
    const status = err?.response?.status ?? "?";
    console.error(`  ERROR  HTTP ${status}`);
    console.error("  ", err?.response?.data ?? err?.message ?? err);
    return false;
  }
}

async function paidIndicatorsWithKey() {
  banner("2) PAID  ·  GET /indicators?pair=BTC/USD&tf=1h  (auto-paying via x402)");

  // Dynamic imports so users without a key never need these packages.
  const { x402Client, wrapAxiosWithPayment } = await import("@x402/axios");
  const { ExactEvmScheme } = await import("@x402/evm/exact/client");
  const { privateKeyToAccount } = await import("viem/accounts");

  const keyHex = KEY.startsWith("0x") ? KEY : `0x${KEY}`;
  const account = privateKeyToAccount(keyHex);

  const client = new x402Client();
  client.register("eip155:*", new ExactEvmScheme(account));
  const api = wrapAxiosWithPayment(axios.create({ baseURL: BASE_URL, timeout: 30000 }), client);

  console.log(`  buyer wallet  : ${account.address}`);
  console.log(`  endpoint      : GET /indicators?pair=BTC/USD&tf=1h`);
  console.log(`  price         : $0.005 USDC on Base mainnet`);

  try {
    const r = await api.get("/indicators", { params: { pair: "BTC/USD", tf: "1h" } });
    console.log(`\nHTTP ${r.status}`);
    console.log(pretty(r.data));
    console.log("\n  ✓ paid call succeeded — $0.005 USDC was settled on Base via x402.");
    return true;
  } catch (err) {
    const status = err?.response?.status ?? "?";
    console.error(`  ERROR  HTTP ${status}`);
    console.error("  ", err?.response?.data ?? err?.message ?? err);
    console.error("  Possible causes: wallet has no USDC; key is malformed; facilitator down.");
    return false;
  }
}

async function show402ChallengeNoFunds() {
  banner("2) NO-FUNDS DEMO  ·  GET /indicators  (no key → show 402 challenge only)");
  console.log("  (set WALLET_PRIVATE_KEY in .env to auto-pay and get the actual data)");

  try {
    // Treat any status as resolved so we can inspect the 402 challenge.
    const r = await axios.get(`${BASE_URL}/indicators`, {
      params: { pair: "BTC/USD", tf: "1h" },
      timeout: 15000,
      validateStatus: () => true,
    });
    console.log(`\nHTTP ${r.status}`);

    if (r.status !== 402) {
      console.log("  Unexpected non-402 status. Body:");
      console.log(pretty(r.data));
      return true;
    }

    console.log("  ↑ This is the x402 'Payment Required' challenge — your wallet would auto-sign and replay.");

    // The x402 v2 challenge is delivered as a base64-encoded JSON blob in the
    // PAYMENT-REQUIRED header (response body is intentionally empty).
    const headerRaw = r.headers["payment-required"] || r.headers["PAYMENT-REQUIRED"];
    let challenge = null;
    if (headerRaw) {
      try {
        challenge = JSON.parse(Buffer.from(headerRaw, "base64").toString("utf-8"));
      } catch (e) {
        console.error("  could not decode PAYMENT-REQUIRED header:", e?.message);
      }
    } else {
      console.log("  (no PAYMENT-REQUIRED header — server may use a different x402 version)");
    }

    if (challenge) {
      console.log("\nDecoded PAYMENT-REQUIRED challenge:");
      console.log(pretty(challenge));

      const accepts = challenge.accepts || [];
      if (accepts.length) {
        const a = accepts[0];
        console.log("\n  first accepts[] entry (this is what gets signed and re-sent):");
        console.log(`    scheme   : ${a.scheme || "?"}`);
        console.log(`    network  : ${a.network || "?"}      (eip155:8453 = Base mainnet)`);
        console.log(`    asset    : ${a.asset || "?"}`);
        console.log(`    amount   : ${a.amount || "?"}              (micro-USDC; 5000 = $0.005)`);
        console.log(`    payTo    : ${a.payTo || "?"}`);
        console.log(`    maxTimeoutSeconds: ${a.maxTimeoutSeconds ?? "?"}`);
      }
    }

    console.log("\n  Next step: drop a small USDC balance into a fresh Base hot wallet,");
    console.log("             paste its private key into .env as WALLET_PRIVATE_KEY,");
    console.log("             and re-run `node agent.js` — the same call will now auto-pay.");
    return true;
  } catch (err) {
    console.error("  ERROR fetching the 402 challenge:");
    console.error("  ", err?.message ?? err);
    return false;
  }
}

async function main() {
  console.log("Kraken Crypto Signals — x402 demo agent");
  console.log(`baseURL: ${BASE_URL}`);
  console.log(`wallet : ${KEY ? "(loaded from .env)" : "(none — no-funds demo mode)"}`);

  const previewOk = await freePreview();
  if (!previewOk) {
    console.error("\nFree preview failed — aborting before any paid attempt.");
    process.exit(1);
  }

  if (KEY) {
    const paidOk = await paidIndicatorsWithKey();
    process.exit(paidOk ? 0 : 2);
  } else {
    const demoOk = await show402ChallengeNoFunds();
    process.exit(demoOk ? 0 : 3);
  }
}

main().catch((err) => {
  console.error("Unhandled error:", err?.stack || err?.message || err);
  process.exit(99);
});
