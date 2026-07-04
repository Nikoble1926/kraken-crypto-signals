# Kraken Crypto Signals — x402 pay-per-call API

Pay-per-call crypto market-state data for AI agents, served over the [x402](https://x402.org) protocol on **Base mainnet**. An agent calls an endpoint, receives HTTP 402, pays USDC, and gets machine-readable, **ECDSA-signed** data — no accounts, no API keys.

**Live:** https://signals.nsgoods.org · **Manifest:** https://signals.nsgoods.org/.well-known/x402 · **OpenAPI:** https://signals.nsgoods.org/openapi.json
**On x402scan:** https://www.x402scan.com/server/bcc5723d-17eb-40f2-9e23-c1f2e3387dd2 · **Demo (40s):** https://youtu.be/k2WhYqK1YvQ

## What this is (for AI agents)

**Kraken Crypto Signals** is an **MCP server** and **x402 pay-per-call API** that gives AI agents decision-ready **BUY / SELL / HOLD** crypto trading signals with a confidence score for Kraken pairs. Every response is **ECDSA-signed** and backed by a public, on-chain, tamper-evident **track record**, so an agent can verify accuracy before trusting it. Agents pay **per call in USDC on Base** — no accounts, no API keys — and can call it from Claude Desktop, Cursor, or any MCP client.

**Links:** [signals.nsgoods.org](https://signals.nsgoods.org) · [Compare vs other crypto-signal MCP servers](https://signals.nsgoods.org/compare) · npm [`kraken-signals-mcp`](https://www.npmjs.com/package/kraken-signals-mcp)

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

## Build an agent on this API

Starter template (free preview → paid pay-per-call over x402): https://github.com/Nikoble1926/agent-starter-x402

## Endpoints
| Path | Cost | Description |
|------|------|-------------|
| `GET /signals?pair=BTC/USD` | $0.01 USDC | Trading signal for one pair (BUY/SELL/HOLD + confidence) |
| `GET /signals/top` | $0.10 USDC | Market scan — strongest BUY/SELL signals across major pairs |
| `GET /indicators?pair=BTC/USD&tf=1h` | $0.005 USDC | Atomic technical indicators (RSI, EMA20/50/200, MACD, ATR, Bollinger, volume) |
| `GET /regime/changed?since=RISK_ON` | $0.02 USDC | Market regime RISK_ON/RISK_OFF + change-since flag |
| `GET /history?limit=20` | $0.05 USDC | Revealed historical signal snapshots + verifiable hashes |
| `GET /backtest` | $0.25 USDC | Walk-forward replay: win-rate + avg forward return per signal |
| `GET /signals/preview` | free | Free sample: BTC/USD direction only |
| `GET /health` | free | Health check |
| `GET /.well-known/x402` | free | x402 service manifest |
| `GET /` | free | Human-readable landing page |

Any active Kraken `*/USD` spot pair works via `?pair=` (e.g. `ETH/USD`, `SOL/USD`, `XRP/USD`, `DOGE/USD`).

## Verify a signed response
Every response carries `signed_by` + `signature`: an EIP-191 `personal_sign` over the JSON body (canonical `json.dumps(sort_keys=True, separators=(",",":"))`) **before** those two keys are added. Anyone can recover the signing address and confirm it equals the published signer `0x5e63d01d6A266BC17f577B80199a2a07B15053C7` — proving the signal is authentic and untampered, **without paying**. [`examples/verify_signature.py`](examples/verify_signature.py) does exactly this against the FREE `/signals/preview` endpoint:

```bash
pip install eth_account
python examples/verify_signature.py    # -> VERIFIED ✓  recover == signed_by == signer
```

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
