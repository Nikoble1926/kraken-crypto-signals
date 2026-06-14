# Quickstart — Kraken Crypto Signals (x402 demo agent)

Copy-paste demo for the live x402 pay-per-call signals API at **https://signals.nsgoods.org**. A tiny Node.js agent that:

1. Calls the **free** preview endpoint and prints the result (no wallet needed).
2. If you set a Base wallet private key, makes **one cheap paid call** ($0.005 USDC) to `/indicators` via x402.
3. If you don't set a key, it makes the same call without paying so you can **see the HTTP 402 challenge** (price, network, payTo) — exactly what your own agent would receive in production.

> ⚠️ **Paid calls spend real USDC on Base mainnet.** Use a dedicated, low-balance hot wallet — never your main one.

## Endpoints (used by this demo)

| Path | Cost | What it returns |
|---|---|---|
| `GET /signals/preview` | **free** | Sample BTC/USD signal direction |
| `GET /indicators?pair=BTC/USD&tf=1h` | $0.005 USDC | Atomic TA indicators (RSI, EMA, MACD, ATR, Bollinger, volume) |

The full paid catalog (this demo only hits the first two): `/signals` ($0.01), `/regime/changed` ($0.02), `/history` ($0.05), `/signals/top` ($0.10), `/backtest` ($0.25). All paid in USDC on Base via x402. See the [main README](../../README.md) and the [MCP bridge](../../mcp) for the full picture.

## Run in 3 steps

```bash
cd examples/quickstart

# 1. install deps
npm install

# 2. configure (optional — leave WALLET_PRIVATE_KEY empty for the no-funds demo)
cp .env.example .env

# 3. run
node agent.js
```

## What you'll see

**Without a key in `.env`:**
- ✅ `/signals/preview` returns a JSON signal (proves the API works)
- ✅ `/indicators` returns the x402 **402 Payment Required** challenge with the live `accepts[]` (network = `eip155:8453`, asset = USDC, price = `5000` micro-USDC = $0.005, `payTo` = receiving address)
- Clear printed instruction to set `WALLET_PRIVATE_KEY` if you want to auto-pay

**With a funded `WALLET_PRIVATE_KEY` (Base hot wallet, a few cents of USDC):**
- ✅ Same `/signals/preview` call
- ✅ `/indicators` call auto-pays $0.005 in USDC on Base and prints the signed JSON response (RSI, EMA, MACD, ATR, Bollinger, volume)

## How the payment flow works

The agent uses the official x402 v2 buyer-side pattern (same one as our [`mcp/server.ts`](../../mcp/server.ts)):

```js
import { x402Client, wrapAxiosWithPayment } from "@x402/axios";
import { ExactEvmScheme } from "@x402/evm/exact/client";
import { privateKeyToAccount } from "viem/accounts";

const client = new x402Client();
client.register("eip155:*", new ExactEvmScheme(privateKeyToAccount(KEY)));
const api = wrapAxiosWithPayment(axios.create({ baseURL }), client);

const r = await api.get("/indicators", { params: { pair: "BTC/USD", tf: "1h" } });
```

The `wrapAxiosWithPayment` wrapper:
1. Sends the GET request
2. On HTTP 402, parses `accepts[]`, signs an EIP-3009 USDC transfer with your viem account
3. Replays the request with the `X-PAYMENT` header
4. Returns the signed JSON body

The whole round-trip is gasless for you (the Coinbase CDP facilitator covers gas) — you only need a USDC balance.

## Security

- The `.env` file with your private key stays on your machine. Never commit it.
- `.gitignore` should already cover `.env`. If you fork this, double-check.
- Use a **hot wallet** with a small USDC balance (a few dollars is plenty for thousands of calls at these prices).
- Verify signed responses against the API's published `signed_by` address (see the [main README](../../README.md) for the verify recipe).

## Disclaimer

Educational signals. Not financial advice.
