# Kraken Crypto Signals — x402 pay-per-call API

Pay-per-call crypto market-state data (BUY/SELL/HOLD + confidence) for AI
agents, served over the [x402](https://x402.org) protocol on **Base mainnet**.
An agent calls an endpoint, receives HTTP 402, pays USDC, and gets the
machine-readable, ECDSA-signed data — no accounts, no API keys.

**Live:** https://signals.nsgoods.org
**Manifest:** https://signals.nsgoods.org/.well-known/x402

**Demo video (40s):** https://youtu.be/k2WhYqK1YvQ

**Discovery:** Listed on the [x402 Bazaar](https://docs.cdp.coinbase.com/x402/bazaar) (CDP Facilitator) — AI agents can find this API via Bazaar semantic search and the merchant lookup for payTo `0xc87a06DEE4c0E85912296002617120BBfd5EF990`.

Settlement runs through the [Coinbase CDP Facilitator](https://docs.cdp.coinbase.com/x402) (with KYT screening on USDC transfers). Previously settled through the [PayAI](https://facilitator.payai.network) facilitator before the switch to CDP.

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
