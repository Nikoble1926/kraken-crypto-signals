# Kraken Signals MCP

Wire **Kraken Crypto Signals** into **Claude Desktop**, **Cursor**, or any MCP
client as tools. Ask your agent for a signal and it calls the tool; for the paid
tools the bridge pays **USDC on Base** automatically via [x402](https://x402.org)
and returns the result.

The **`get_signal_preview`** tool is **free and needs no wallet key**. The other
tools cost a few cents per call and activate only when you set a Base wallet key.

## Quick start (no clone, no build)

Add this to your client config and you're done — `npx` fetches and runs it:

**Claude Desktop** — Settings → Developer → Edit Config:

```json
{
  "mcpServers": {
    "kraken-signals": {
      "command": "npx",
      "args": ["-y", "kraken-signals-mcp"]
    }
  }
}
```

That gives you the **free preview** out of the box. To enable the **paid** tools,
add a low-balance Base wallet key:

```json
{
  "mcpServers": {
    "kraken-signals": {
      "command": "npx",
      "args": ["-y", "kraken-signals-mcp"],
      "env": {
        "EVM_PRIVATE_KEY": "0x<your-low-balance-base-wallet-private-key>"
      }
    }
  }
}
```

**Cursor** — Settings → MCP → Add new server, same `command`/`args`/`env`.

Restart the client, then ask: *"Get the BTC signal preview"* (free) or
*"Get the top crypto signals right now"* (paid).

## Tools

| Tool | Cost | Description |
|------|------|-------------|
| `get_signal_preview` | **free** | Current BTC/USD direction (BUY/SELL/HOLD), no wallet key required |
| `get_signal` | $0.01 USDC | Signal + confidence for one pair (any Kraken `*/USD`) |
| `get_top_signals` | $0.10 USDC | Market scan — strongest signals across major pairs |
| `get_history` | $0.05 USDC | Revealed historical signal snapshots (commit-reveal) + verifiable hashes |
| `get_regime_changed` | $0.02 USDC | Market regime (RISK_ON/RISK_OFF) + whether it changed since you last looked |
| `get_backtest` | $0.25 USDC | Walk-forward replay: win-rate + avg forward return per BUY/SELL signal |
| `get_indicators` | $0.005 USDC | Atomic TA indicators (RSI, EMA, MACD, ATR, Bollinger, volume) for a pair/timeframe |

## Paid mode (optional)

The paid tools need an `EVM_PRIVATE_KEY`: a **Base wallet private key** for a
**low-balance, dedicated hot wallet** holding a little **USDC** — never your main
wallet. Payments are gasless for the payer (the facilitator covers gas), so you
mainly need a small USDC balance. Without the key, paid tools return a clear
"set EVM_PRIVATE_KEY" message and the free preview keeps working.

`RESOURCE_SERVER_URL` is optional and defaults to `https://signals.nsgoods.org`.

## Run from source (dev)

```bash
cd mcp
npm install
npm run build
node dist/server.js   # or: npm run dev
```

## Security

- The `EVM_PRIVATE_KEY` is **yours** (the buyer's) and stays **local** in your
  client config — it is never sent to the API. Use a throwaway hot wallet with
  only a small USDC balance.
- For spend control, see the policy-check hooks in the
  [official x402 MCP guide](https://docs.x402.org/guides/mcp-server-with-x402)
  and cap per-call / per-session amounts.

## Notes

Mirrors the official x402 MCP client pattern:
https://docs.x402.org/guides/mcp-server-with-x402 . The x402 v2 TypeScript
packages (`@x402/axios`, `@x402/evm`) evolve quickly — if `npm install` or the
build fails, check that guide for current package names/versions.

No accounts, no API keys on the service side. Educational signals — not
financial advice. MIT licensed.
