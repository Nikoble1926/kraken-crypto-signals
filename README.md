# Kraken Crypto Signals — x402 pay-per-call API

Pay-per-call crypto market-state data for AI agents, served over the [x402](https://x402.org) protocol on **Base mainnet**. An agent calls an endpoint, receives HTTP 402, pays USDC, and gets machine-readable, **ECDSA-signed** data — no accounts, no API keys.

**Live:** https://signals.nsgoods.org · **Manifest:** https://signals.nsgoods.org/.well-known/x402 · **OpenAPI:** https://signals.nsgoods.org/openapi.json
**On x402scan:** https://www.x402scan.com/server/bcc5723d-17eb-40f2-9e23-c1f2e3387dd2 · **Demo (40s):** https://youtu.be/k2WhYqK1YvQ

## Why this API
- **Signed & verifiable** — every response carries `signed_by` + `signature`; recover against the published signer to prove it is authentic and untampered.
- **Provable track-record** — a public commit-reveal hash chain (`/track-record`) lets anyone audit past calls; `/history` reveals older snapshots with their hashes.
- **Proof of edge** — `/backtest` replays the live strategy over recent history and returns win-rate + average forward return, so an agent can measure the edge before subscribing.
- **No keys, no signup, free preview** — try `/signals/preview` for free, then pay per call in USDC on Base.

## Quickstart (call & pay in ~5 minutes)
A runnable demo agent lives in [`examples/quickstart`](examples/quickstart).
It calls the FREE `/signals/preview` endpoint and then prints the decoded
x402 `402 Payment Required` challenge for a paid endpoint — no funds needed.
Add a low-balance Base hot-wallet key and the same call auto-pays in USDC on Base.

```bash
cd examples/quickstart
npm install
cp .env.example .env      # leave WALLET_PRIVATE_KEY empty for the no-funds demo
node agent.js
```

Paid endpoints: `/indicators` ($0.005), `/signals` ($0.01), `/regime/changed` ($0.02), `/history` ($0.05), `/signals/top` ($0.10), `/backtest` ($0.25) — USDC on Base mainnet (eip155:8453), payTo 0xc87a06DEE4c0E85912296002617120BBfd5EF990.

## Paid endpoints
| Path | Cost (USDC) | Description |
|------|------|-------------|
| `GET /signals?pair=BTC/USD` | $0.01 | Trading signal for one pair (BUY/SELL/HOLD + confidence) |
| `GET /signals/top` | $0.10 | Market scan — strongest BUY/SELL signals across major pairs |
| `GET /history?limit=20` | $0.05 | Revealed historical signal snapshots (commit-reveal) + verifiable hashes |
| `GET /regime/changed?since=RISK_ON` | $0.02 | Market regime (RISK_ON/RISK_OFF) + whether it changed since you last looked |
| `GET /backtest?pair=BTC/USD` | $0.25 | Walk-forward replay: win-rate + avg forward return per BUY/SELL signal |

## Free endpoints
| Path | Description |
|------|-------------|
| `GET /signals/preview` | Free sample — BTC/USD direction only |
| `GET /track-record` | Public tamper-evident hash chain |
| `GET /.well-known/x402` · `GET /openapi.json` · `GET /health` · `GET /` | Manifest, OpenAPI spec, health, landing page |

Any active Kraken `*/USD` spot pair works via `?pair=` (e.g. `ETH/USD`, `SOL/USD`, `XRP/USD`, `DOGE/USD`).

## Discovery
Listed on **[x402scan](https://www.x402scan.com/server/bcc5723d-17eb-40f2-9e23-c1f2e3387dd2)** and the **[x402 Bazaar](https://docs.cdp.coinbase.com/x402/bazaar)** (CDP Facilitator) — agents can find this API via Bazaar semantic search / merchant lookup for payTo `0xc87a06DEE4c0E85912296002617120BBfd5EF990`. Settlement runs through the [Coinbase CDP Facilitator](https://docs.cdp.coinbase.com/x402).

## Use with Claude Desktop / Cursor (MCP)
The [`mcp/`](./mcp) bridge exposes every endpoint as a tool; paid tools settle USDC on Base automatically per call, and `get_signal_preview` is free. See [`mcp/`](./mcp) for setup.

## Run
```bash
pip install -r requirements.txt
cp .env.example .env   # set your PUBLIC Base receiving address
python signals_api.py
```

## Security
Only your **public** Base receiving address goes in `.env`. Never put a private key or seed phrase in this repo.

## Disclaimer
Educational signals from a multi-timeframe strategy. Not financial advice. `/backtest` is a recent-window walk-forward replay, not a long-horizon study; past performance does not predict future results.
