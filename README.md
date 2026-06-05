# Kraken Crypto Signals — x402 pay-per-call API

Pay-per-call crypto trading signals for AI agents, served over the
[x402](https://x402.org) protocol on **Base mainnet**. An agent calls an
endpoint, receives HTTP 402, pays USDC, and gets the signal.

**Live:** https://signals.nsgoods.org
**Manifest:** https://signals.nsgoods.org/.well-known/x402

## Endpoints
| Path | Cost | Description |
|------|------|-------------|
| `GET /signals?pair=BTC/USD` | $0.01 USDC | Trading signal for one pair (BUY/SELL/HOLD + confidence) |
| `GET /signals/top` | $0.10 USDC | Market scan — strongest BUY/SELL signals across major pairs |
| `GET /health` | free | Health check |
| `GET /.well-known/x402` | free | x402 service manifest |
| `GET /` | free | Human-readable landing page |

Any active Kraken `*/USD` spot pair works via `?pair=` (e.g. `ETH/USD`, `SOL/USD`, `XRP/USD`, `DOGE/USD`).

## Use with Claude Desktop (MCP)

Add these signals as paid tools in Claude Desktop (or any MCP client) - the bridge pays USDC on Base automatically per call. The `get_signal_preview` tool is free.

See [`mcp/`](./mcp) for setup.

## Run
```bash
pip install -r requirements.txt
cp .env.example .env   # set your PUBLIC Base receiving address
python signals_api.py
```

## Security
Only your **public** Base receiving address goes in `.env`.
Never put a private key or seed phrase in this repo.

## Disclaimer
Educational signals from a multi-timeframe strategy. Not financial advice.
