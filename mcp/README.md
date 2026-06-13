# Use with Claude Desktop (MCP)

Wire **Kraken Crypto Signals** into Claude Desktop (or any MCP client) as paid
tools. When you ask Claude for a signal, it calls the tool, the bridge pays
**USDC on Base** automatically via [x402](https://x402.org), and returns the result.

The **preview** tool is free; the others cost a few cents per call.

## Tools

| Tool | Cost | Description |
|------|------|-------------|
| `get_signal_preview` | free | Current BTC/USD direction (BUY/SELL/HOLD), no payment |
| `get_signal` | $0.01 USDC | Signal + confidence for one pair (any Kraken `*/USD`) |
| `get_top_signals` | $0.10 USDC | Market scan — strongest signals across major pairs |
| `get_history` | $0.05 USDC | Revealed historical signal snapshots (commit-reveal) + verifiable hashes |
| `get_regime_changed` | $0.02 USDC | Market regime (RISK_ON/RISK_OFF) + whether it changed since you last looked |
| `get_backtest` | $0.25 USDC | Walk-forward replay: win-rate + avg forward return per BUY/SELL signal |
| `get_indicators` | $0.005 USDC | Atomic TA indicators (RSI, EMA, MACD, ATR, Bollinger, volume) for a pair/timeframe |

## Prerequisites

- **Node.js v20+**
- A **Base wallet private key** with a little **USDC** to spend. Use a
  **low-balance, dedicated hot wallet** — never your main wallet. Payments are
  gasless for the payer (the facilitator covers gas), so you mainly need USDC.

## Setup

```bash
cd mcp
npm install
npm run build
```

Then add this to your Claude Desktop config
(Settings -> Developer -> Edit Config):

```json
{
  "mcpServers": {
    "kraken-crypto-signals": {
      "command": "node",
      "args": ["<absolute-path-to>/kraken-crypto-signals/mcp/dist/server.js"],
      "env": {
        "EVM_PRIVATE_KEY": "0x<your-low-balance-base-wallet-private-key>",
        "RESOURCE_SERVER_URL": "https://signals.nsgoods.org"
      }
    }
  }
}
```

Restart Claude Desktop, then ask: *"Get the top crypto signals right now."*

## Security

- The `EVM_PRIVATE_KEY` is **yours** (the buyer's) and stays **local** in your
  config — it is never sent to the API. Use a throwaway hot wallet with only a
  small USDC balance.
- For spend control, see the policy-check hooks in the official guide and cap
  per-call / per-session amounts in code.

## Notes

Mirrors the official x402 MCP client pattern:
https://docs.x402.org/guides/mcp-server-with-x402 . The x402 v2 TypeScript
packages (`@x402/axios`, `@x402/evm`) evolve quickly — if `npm install` or the
build fails, check that guide for the current package names and versions.

No accounts, no API keys on the service side. Educational signals — not
financial advice.
