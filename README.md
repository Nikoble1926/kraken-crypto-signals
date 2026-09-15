### kraken-crypto-signals

Pay per call crypto market state and regime signals for AI agents, served over the x402 protocol. Every response is a signed, verifiable verdict.

**What it does**

- Market state signals and technical indicators, returned on demand per call over HTTP 402
- Built for autonomous agents that need a signed read of current conditions
- Free preview endpoints so you can see the shape before you pay

**How it works**

- Signed with EIP-191; every verdict is canonicalized and independently verifiable
- Settled per call in USDC on Base
- You pay only for a successful `2xx` response

**Live**: [signals.nsgoods.org](https://signals.nsgoods.org)

Part of **nsgoods**, a suite of signed x402 data oracles. Hub: [x402.nsgoods.org](https://x402.nsgoods.org)
