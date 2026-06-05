# Kraken Crypto Signals — x402 pay-per-call API

Pay-per-call crypto trading signals for AI agents, served over the
[x402](https://x402.org) protocol on **Base mainnet**. An agent calls
`GET /signals`, receives HTTP 402, pays USDC, and gets a BUY/SELL/HOLD signal.

**Live:** https://signals.nsgoods.org
**Manifest:** https://signals.nsgoods.org/.well-known/x402

## Endpoints
| Path | Cost | Description |
|------|------|-------------|
| `GET /signals?pair=BTC/USD` | $0.01 USDC | Multi-timeframe trading signal |
| `GET /health` | free | Health check |
| `GET /.well-known/x402` | free | x402 service manifest |

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
