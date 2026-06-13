/**
 * Kraken Crypto Signals — MCP server (x402 buyer bridge)
 * ------------------------------------------------------
 * Exposes the live x402 API at https://signals.nsgoods.org as MCP tools so an
 * agent in Claude Desktop (or any MCP client) can call them and pay per call.
 *
 * Payment is handled automatically by @x402/axios: when an endpoint returns
 * HTTP 402, the wrapper signs a USDC payment on Base with YOUR wallet and
 * retries. You only pay for the paid tools; the preview is free.
 *
 * Mirrors the official x402 MCP client pattern:
 *   https://docs.x402.org/guides/mcp-server-with-x402
 * (x402 v2 packages evolve — if install/build fails, check that guide for the
 *  current package names/versions.)
 */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import axios from "axios";
import { x402Client, wrapAxiosWithPayment } from "@x402/axios";
import { ExactEvmScheme } from "@x402/evm/exact/client";
import { privateKeyToAccount } from "viem/accounts";
import { config } from "dotenv";

config();

const evmPrivateKey = process.env.EVM_PRIVATE_KEY as `0x${string}` | undefined;
const baseURL = process.env.RESOURCE_SERVER_URL || "https://signals.nsgoods.org";

if (!evmPrivateKey) {
  throw new Error(
    "EVM_PRIVATE_KEY is required (a Base wallet with a little USDC — use a low-balance hot wallet)."
  );
}

const client = new x402Client();
client.register("eip155:*", new ExactEvmScheme(privateKeyToAccount(evmPrivateKey)));
const api = wrapAxiosWithPayment(axios.create({ baseURL }), client);

const server = new McpServer({ name: "Kraken Crypto Signals", version: "1.0.0" });

server.tool(
  "get_signal_preview",
  "FREE sample: current BTC/USD signal direction (BUY/SELL/HOLD), no payment.",
  {},
  async () => {
    const r = await api.get("/signals/preview");
    return { content: [{ type: "text", text: JSON.stringify(r.data) }] };
  }
);

server.tool(
  "get_signal",
  "Paid ($0.01 USDC): trading signal + confidence for one pair. Any active Kraken */USD pair.",
  { pair: z.string().default("BTC/USD").describe("e.g. BTC/USD, ETH/USD, SOL/USD") },
  async ({ pair }) => {
    const r = await api.get("/signals", { params: { pair } });
    return { content: [{ type: "text", text: JSON.stringify(r.data) }] };
  }
);

server.tool(
  "get_top_signals",
  "Paid ($0.10 USDC): market scan — strongest BUY/SELL signals across major pairs.",
  { limit: z.number().min(1).max(10).default(3).describe("How many top signals (1-10)") },
  async ({ limit }) => {
    const r = await api.get("/signals/top", { params: { limit } });
    return { content: [{ type: "text", text: JSON.stringify(r.data) }] };
  }
);

server.tool(
  "get_history",
  "Paid ($0.05 USDC): revealed historical signal snapshots (commit-reveal hash chain). Returns past BUY/SELL/HOLD calls with their verifiable hashes; the latest snapshot is withheld.",
  { limit: z.number().int().min(1).max(100).default(20).describe("How many past snapshots (1-100)") },
  async ({ limit }) => {
    const r = await api.get("/history", { params: { limit } });
    return { content: [{ type: "text", text: JSON.stringify(r.data) }] };
  }
);

server.tool(
  "get_regime_changed",
  "Paid ($0.02 USDC): current market regime (RISK_ON/RISK_OFF) from a provable chain + whether it changed since the regime you last saw. Cheap poll endpoint to react to regime flips.",
  { since: z.string().optional().describe("Regime you last saw (e.g. RISK_ON). If omitted, response reports the current regime only.") },
  async ({ since }) => {
    const params: Record<string, string> = {};
    if (since) params.since = since;
    const r = await api.get("/regime/changed", { params });
    return { content: [{ type: "text", text: JSON.stringify(r.data) }] };
  }
);

server.tool(
  "get_backtest",
  "Paid ($0.25 USDC): walk-forward replay of the live multi-timeframe strategy over recent 1m history. Returns win-rate + average forward return per BUY/SELL signal so an agent can measure recent edge before subscribing.",
  {
    pair: z.string().default("BTC/USD").describe("Kraken */USD pair, e.g. BTC/USD, ETH/USD"),
    forward: z.number().int().min(5).max(120).default(30).describe("Forward window in 1m bars to score each signal against (5-120)"),
    step: z.number().int().min(1).max(15).default(5).describe("Walk-forward step in 1m bars (1-15). Smaller = denser sample but slower."),
  },
  async ({ pair, forward, step }) => {
    const r = await api.get("/backtest", { params: { pair, forward, step } });
    return { content: [{ type: "text", text: JSON.stringify(r.data) }] };
  }
);

server.tool(
  "get_indicators",
  "Paid ($0.005 USDC): atomic TA indicators (RSI, EMA, MACD, ATR, Bollinger, volume) for a pair/timeframe. Cheap high-frequency poll endpoint.",
  {
    pair: z.string().default("BTC/USD").describe("Kraken */USD pair, e.g. BTC/USD, ETH/USD"),
    tf: z.enum(["1m", "5m", "15m", "30m", "1h", "4h", "1d"]).default("1h").describe("Candle timeframe"),
    name: z.string().optional().describe("Optional: return only one indicator (e.g. rsi14, ema50, macd_hist)"),
  },
  async ({ pair, tf, name }) => {
    const params: Record<string, string> = { pair, tf };
    if (name) params.name = name;
    const r = await api.get("/indicators", { params });
    return { content: [{ type: "text", text: JSON.stringify(r.data) }] };
  }
);

const transport = new StdioServerTransport();
await server.connect(transport);
