"""
x402 pay-per-call Crypto Signals API (Base / EVM)
--------------------------------------------------
Wraps the existing Kraken bot signal logic (StrategyEngine.analyze_multi_timeframe)
in an x402 paywall. An agent calls GET /signals -> 402 -> pays USDC on Base -> gets
the signal JSON.

DEPLOY: run this on the SAME server as the bot (BOT_ROOT=/root/bot) so it can import
DataEngine/StrategyEngine and reuse the venv + OHLCV cache. It runs as a SEPARATE
process and only reads PUBLIC market data -- it does not touch the running bot.

SECURITY: only your PUBLIC Base receiving address (EVM_ADDRESS) goes here.
Never put a private key / seed phrase in this file or on the server.

Based on the official Coinbase x402 Flask example (confirmed API, June 2026).
"""

import os
import sys
import time
import base64
import threading
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request, Response

from x402.http import FacilitatorConfig, HTTPFacilitatorClientSync, PaymentOption
from x402.http.middleware.flask import payment_middleware
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.server import x402ResourceServerSync

load_dotenv()

# ---- Config (from .env) ----------------------------------------------------
EVM_ADDRESS = os.getenv("EVM_ADDRESS")                 # your Base wallet (receives USDC)
NETWORK = os.getenv("NETWORK", "eip155:84532")         # 84532 = Base Sepolia (test); 8453 = mainnet
FACILITATOR_URL = os.getenv("FACILITATOR_URL", "https://x402.org/facilitator")
PRICE = os.getenv("PRICE", "$0.01")                    # price per /signals call
TOP_PRICE = os.getenv("TOP_PRICE", "$0.10")            # price per /signals/top call (market scan)
BOT_ROOT = os.getenv("BOT_ROOT", "/root/bot")          # path to the Kraken bot package

if not EVM_ADDRESS:
    raise ValueError("Missing EVM_ADDRESS in .env (your Base receiving address)")

# ---- Make the bot package importable ---------------------------------------
if BOT_ROOT not in sys.path:
    sys.path.insert(0, BOT_ROOT)

from core.data_engine import DataEngine            # noqa: E402
from core.strategy_engine import StrategyEngine    # noqa: E402

# ---- Signal adapter (real Kraken logic, cached per pair) -------------------
_DEFAULT_PAIR = "BTC/USD"
_DEFAULT_TIMEFRAME = "1m"          # strategy's primary tf; 5m/15m are confirmations
_CACHE_TTL_SECONDS = 8             # matches DataEngine's 1m candle cache
_SCORE_NORMALIZER = 100.0          # rough cap for score -> 0..1 confidence
_DISCLAIMER = (
    "Educational signal from a multi-timeframe Kraken strategy. "
    "Not financial advice. Markets can move against any signal; "
    "use independent risk management."
)

_engine = DataEngine()
_strategy = StrategyEngine()
_lock = threading.Lock()
_cache = {}   # pair -> (expires_at, payload)


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def _compute(pair):
    mtf = _engine.get_multi_timeframe_candles(pair)
    result = _strategy.analyze_multi_timeframe(mtf)
    signal = str(result.get("signal", "HOLD")).upper()
    score = float(result.get("score", 0) or 0)
    confidence = _clamp(score / _SCORE_NORMALIZER) if result.get("valid") else 0.0
    return {
        "pair": pair,
        "signal": signal,
        "confidence": round(confidence, 3),
        "timeframe": _DEFAULT_TIMEFRAME,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "disclaimer": _DISCLAIMER,
    }


def generate_signal(pair=_DEFAULT_PAIR):
    """Latest trading signal as a JSON-serializable dict, cached per pair."""
    pair = (pair or _DEFAULT_PAIR).upper().replace("-", "/")
    now = time.time()
    cached = _cache.get(pair)
    if cached and cached[0] > now:
        return cached[1]
    with _lock:
        cached = _cache.get(pair)
        if cached and cached[0] > now:
            return cached[1]
        try:
            payload = _compute(pair)
        except Exception as e:
            # Data pipeline failure -> mark unavailable so the route returns
            # 503 (no charge on the standard x402 settle-after-success flow)
            # instead of selling a junk HOLD signal.
            unavailable = {
                "_unavailable": True,
                "pair": pair,
                "reason": "{}: {}".format(type(e).__name__, e),
            }
            _cache[pair] = (now + 2, unavailable)  # short cache during outage
            return unavailable
        _cache[pair] = (now + _CACHE_TTL_SECONDS, payload)
        return payload


# ---- Premium: market scan (strongest signals across major pairs) -----------
# Curated set kept small so a scan stays fast and never hammers Kraken's API.
_TOP_PAIRS = [
    "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "DOGE/USD", "ADA/USD",
    "AVAX/USD", "LINK/USD", "LTC/USD", "DOT/USD", "XLM/USD", "UNI/USD",
]
_TOP_CACHE_TTL_SECONDS = 45         # whole-scan cache; keeps /top fast + cheap to serve
_TOP_DEFAULT_LIMIT = 3              # how many strongest signals to return
_top_lock = threading.Lock()        # separate from _lock to avoid nested-lock deadlock
_top_cache = {}                     # "top" -> (expires_at, payload)


def generate_top(limit=_TOP_DEFAULT_LIMIT):
    """Scan major pairs and return the strongest BUY/SELL signals right now."""
    now = time.time()
    cached = _top_cache.get("top")
    if cached and cached[0] > now:
        return cached[1]
    with _top_lock:
        cached = _top_cache.get("top")
        if cached and cached[0] > now:
            return cached[1]
        scanned = []
        for pair in _TOP_PAIRS:
            try:
                # generate_signal uses its own (_lock) per-pair cache -> safe here
                s = generate_signal(pair)
            except Exception:
                continue
            if s.get("_unavailable"):
                continue
            scanned.append(s)
        if not scanned:
            # whole-market data outage -> let the route return 503 (no charge)
            return {"_unavailable": True, "reason": "market data unavailable"}
        actionable = [s for s in scanned if s.get("signal") in ("BUY", "SELL")]
        actionable.sort(key=lambda s: s.get("confidence", 0), reverse=True)
        payload = {
            "scanned_pairs": len(scanned),
            "actionable": len(actionable),
            "top": actionable[:10],  # cache up to 10; route slices to ?limit
            "universe": _TOP_PAIRS,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "disclaimer": _DISCLAIMER,
        }
        _top_cache["top"] = (now + _TOP_CACHE_TTL_SECONDS, payload)
        return payload


# ---- App + x402 middleware (sync variants, Flask is sync) ------------------
app = Flask(__name__)

facilitator = HTTPFacilitatorClientSync(FacilitatorConfig(url=FACILITATOR_URL))
server = x402ResourceServerSync(facilitator)
server.register(NETWORK, ExactEvmServerScheme())

routes = {
    "GET /signals": RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                pay_to=EVM_ADDRESS,
                price=PRICE,
                network=NETWORK,
            ),
        ],
        mime_type="application/json",
        description="Crypto trading signal for one pair (USDC pay-per-call)",
    ),
    "GET /signals/top": RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                pay_to=EVM_ADDRESS,
                price=TOP_PRICE,
                network=NETWORK,
            ),
        ],
        mime_type="application/json",
        description="Market scan: strongest BUY/SELL signals across major pairs (USDC pay-per-call)",
    ),
}
payment_middleware(app, routes=routes, server=server)


# ---- Routes ----------------------------------------------------------------
def _unavailable_body(reason=None):
    return {
        "error": "signal_unavailable",
        "message": ("Live market data is temporarily unavailable. "
                    "You were not charged; please retry shortly."),
        "detail": reason,
    }


@app.route("/signals")
def signals():
    pair = request.args.get("pair", _DEFAULT_PAIR)
    payload = generate_signal(pair)
    if payload.get("_unavailable"):
        return jsonify(_unavailable_body(payload.get("reason"))), 503
    return jsonify(payload)


@app.route("/signals/top")
def signals_top():
    try:
        limit = int(request.args.get("limit", _TOP_DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = _TOP_DEFAULT_LIMIT
    limit = max(1, min(10, limit))
    payload = generate_top()
    if payload.get("_unavailable"):
        return jsonify(_unavailable_body(payload.get("reason"))), 503
    out = dict(payload)
    out["top"] = payload.get("top", [])[:limit]
    return jsonify(out)


@app.route("/signals/preview")
def signals_preview():
    """Free sample: BTC/USD direction only (no confidence). Lets agents/devs
    evaluate before paying for /signals (confidence + any pair) or /signals/top."""
    payload = generate_signal(_DEFAULT_PAIR)
    if payload.get("_unavailable"):
        return jsonify(_unavailable_body(payload.get("reason"))), 503
    return jsonify({
        "pair": payload.get("pair"),
        "signal": payload.get("signal"),
        "preview": True,
        "note": ("Free sample: BTC/USD direction only. Pay GET /signals for "
                 "confidence + any Kraken */USD pair, or GET /signals/top for a "
                 "market scan."),
        "timeframe": payload.get("timeframe"),
        "generated_at": payload.get("generated_at"),
        "disclaimer": payload.get("disclaimer"),
    })


_LANDING_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>Kraken Crypto Signals &mdash; x402 API</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font-family:system-ui,Arial,sans-serif;max-width:680px;margin:40px auto;padding:0 16px;line-height:1.55;color:#111;background:#fafafa}code{background:#eee;padding:2px 6px;border-radius:4px}.box{background:#fff;border:1px solid #e3e3e3;border-radius:10px;padding:16px 20px;margin:16px 0}a{color:#1f6feb}</style>
</head><body>
<h1>Kraken Crypto Signals</h1>
<p>Pay-per-call crypto trading signals (<b>BUY / SELL / HOLD</b> + confidence) from a live multi-timeframe Kraken strategy. Built for AI agents via the <b>x402</b> protocol on Base.</p>
<div class="box"><b>Agent endpoints (paid in USDC):</b><br>
<code>GET /signals?pair=BTC/USD</code> &mdash; one pair &middot; $0.01<br>
<code>GET /signals/top</code> &mdash; market scan, strongest signals &middot; $0.10</div>
<div class="box">Any active Kraken <code>*/USD</code> pair works via <code>?pair=</code> (ETH/USD, SOL/USD, XRP/USD, &hellip;).</div>
<p>Machine-readable manifest: <a href="/.well-known/x402">/.well-known/x402</a> &middot; Code: <a href="https://github.com/Nikoble1926/kraken-crypto-signals">GitHub</a></p>
<p style="color:#666;font-size:14px">Educational signals. Not financial advice.</p>
</body></html>"""


@app.route("/")
def home():
    return _LANDING_HTML


def _price_amount(p):
    return p.lstrip("$") if isinstance(p, str) else str(p)


# ---- OpenAPI 3.1 discovery doc (x402scan reads /openapi.json first) --------
OPENAPI_SPEC = {
    "openapi": "3.1.0",
    "info": {
        "title": "Kraken Crypto Signals",
        "version": "1.0.0",
        "description": ("Pay-per-call crypto trading signals (BUY/SELL/HOLD + "
                        "confidence) for AI agents over x402 on Base."),
    },
    "servers": [{"url": "https://signals.nsgoods.org"}],
    "components": {
        "securitySchemes": {
            "x402Payment": {
                "type": "apiKey",
                "in": "header",
                "name": "X-PAYMENT",
                "description": "x402 payment payload (USDC on Base).",
            }
        },
        "schemas": {
            "Signal": {
                "type": "object",
                "properties": {
                    "pair": {"type": "string", "example": "BTC/USD"},
                    "signal": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
                    "confidence": {"type": "number", "format": "float"},
                    "timeframe": {"type": "string"},
                    "generated_at": {"type": "string", "format": "date-time"},
                    "disclaimer": {"type": "string"},
                },
            },
            "TopSignals": {
                "type": "object",
                "properties": {
                    "scanned_pairs": {"type": "integer"},
                    "actionable": {"type": "integer"},
                    "top": {"type": "array",
                            "items": {"$ref": "#/components/schemas/Signal"}},
                    "universe": {"type": "array", "items": {"type": "string"}},
                    "generated_at": {"type": "string", "format": "date-time"},
                    "disclaimer": {"type": "string"},
                },
            },
        },
    },
    "paths": {
        "/signals": {
            "get": {
                "operationId": "getSignal",
                "summary": "Trading signal for one pair (BUY/SELL/HOLD + confidence)",
                "security": [{"x402Payment": []}],
                "x-payment-info": {
                    "protocols": ["x402"],
                    "price": {"mode": "fixed", "currency": "USD",
                              "amount": _price_amount(PRICE)},
                    "network": NETWORK,
                    "payTo": EVM_ADDRESS,
                },
                "parameters": [
                    {"name": "pair", "in": "query", "required": False,
                     "description": "Any active Kraken */USD spot pair.",
                     "schema": {"type": "string", "default": _DEFAULT_PAIR,
                                "example": "ETH/USD"}}
                ],
                "responses": {
                    "200": {"description": "Trading signal",
                            "content": {"application/json": {
                                "schema": {"$ref": "#/components/schemas/Signal"}}}},
                    "402": {"description": "Payment required (x402 challenge)."},
                    "503": {"description": "Market data unavailable (no charge)."},
                },
            }
        },
        "/signals/top": {
            "get": {
                "operationId": "getTopSignals",
                "summary": "Market scan: strongest BUY/SELL signals across major pairs",
                "security": [{"x402Payment": []}],
                "x-payment-info": {
                    "protocols": ["x402"],
                    "price": {"mode": "fixed", "currency": "USD",
                              "amount": _price_amount(TOP_PRICE)},
                    "network": NETWORK,
                    "payTo": EVM_ADDRESS,
                },
                "parameters": [
                    {"name": "limit", "in": "query", "required": False,
                     "description": "How many top signals to return (1-10, default 3).",
                     "schema": {"type": "integer", "default": _TOP_DEFAULT_LIMIT,
                                "minimum": 1, "maximum": 10}}
                ],
                "responses": {
                    "200": {"description": "Strongest signals",
                            "content": {"application/json": {
                                "schema": {"$ref": "#/components/schemas/TopSignals"}}}},
                    "402": {"description": "Payment required (x402 challenge)."},
                    "503": {"description": "Market data unavailable (no charge)."},
                },
            }
        },
        "/signals/preview": {
            "get": {
                "operationId": "getSignalPreview",
                "summary": "Free sample signal (BTC/USD direction, no payment)",
                "security": [],
                "responses": {
                    "200": {"description": "Free preview signal"},
                    "503": {"description": "Market data unavailable."},
                },
            }
        },
    },
}


@app.route("/openapi.json")
def openapi_spec():
    return jsonify(OPENAPI_SPEC)


_FAVICON_ICO = base64.b64decode(
    "AAABAAIAEBAAAAAAIACwAgAAJgAAACAgAAAAACAACQEAANYCAACJUE5HDQoaCgAAAA1JSERSAAAAEAAAABAIBgAAAB/z/2EAAAJ3SURBVHicZZO/i1xVGIaf9zvn3rl3d3Zms7sBEYmFP0CIwopCGhFt1MK/IKWNna2NlWBhk8LCJtj4J1gYwXQKiuCCWggSFDSSZCcbd5OZuXPnnPNZzKxuyIHD27zvez6+wyOAjXNPvSGrPsD9ErgAY30M8PUFCsiRvvOy/HB278ZXGu49/aqXcA2pwYtjEmVlFzAvTi0R9F+JIxPunSy/aV7sikSD54SQzxMUxwS9w6VRzbnKSH46koTnJNF4sSsG2nd3l1n0vtDu7xHGNcpOn52XhxVbQfTFscrwRQZZdOGgfQMvgBCU6ZL2hR1ss8L7wjCKgrMZRBPE8vaU5tkx5EK53wvzEs8uLIxqcFHaSNtGvnh+h+1KvP+E+Piw47O3n+Sx1x5nfnPKybU/mf04sYjAk2MbkQufv04c1YzffY509VeG39+h3WvYKoW2NsbvXaStAhtvXSAd90y/uf3/6wBhq0JNIAwrNAik4hQgORSHPOnweSIddfgsgXi4wNfOU82nQYcC2CBAtJWGVTQ+VLBOeFnp+doYBLFrxqaJfLLE60B5sMT7DDpbIIi7AxREvTOgbyMf/XHCKyb+niWu/7Pg7uXrVJef4cEPhyxuHBPGNRGnyGS+LEw++YX2xT3y3Y7pt7f49E5Htz3gy0nHrVRop4kiKLOELzJqY4kgQ+5k1+HVnzn/zkXuf32T/NeMOKppgd3KOMqONiLzn44o0yVU5iAz8ANJ8uKp2t6k++0YggijipSdxoQDuThWG/ODCel4kSxG4X5wBiYacPcuS5VhQSwKvDSM/N5l7iUnCBAuTL6GSY/gbC7K6ntPaawk4orGR3D+F23mRMyTTFjTAAAAAElFTkSuQmCCiVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAA0ElEQVR4nGNkwAK4hdT+YxOnFHx9d4sRXQxFgFYW43MIE70tR7eLid6WozuCiZBCWgMmavj+qokoWfq4hdT+D3wIEKNIeZf3wDqAloAuDsAXgizkGoqe8JD52mdeE20O2Q4gBNB9jcy/67aVsAOINYBSMDIS4agDRh0wqB1As4IIuaxQ3uWNs+wg2wHIxe1VE1GSil9kMHijgNggpBQMeAjQxQH4Qm/gQwBbdwkd0Cr+v767xTjgIQD3Pb17R7CQZ0IXoKflDAxovWMYoGf3HACyV0Trq971hwAAAABJRU5ErkJggg=="
)


@app.route("/favicon.ico")
def favicon():
    return Response(_FAVICON_ICO, mimetype="image/x-icon")


@app.route("/health")
def health_check():
    return jsonify({"status": "ok"})


@app.route("/.well-known/x402")
def x402_manifest():
    return jsonify({
        "x402Version": 2,
        "name": "Kraken Crypto Signals",
        "description": "Pay-per-call multi-timeframe crypto trading signals (BUY/SELL/HOLD).",
        "pairs": (
            "All active Kraken */USD spot pairs (e.g. BTC/USD, ETH/USD, SOL/USD, "
            "XRP/USD, DOGE/USD). Pass any with ?pair=."
        ),
        "endpoints": [
            {"path": "/signals", "method": "GET", "price": PRICE,
             "network": NETWORK, "payTo": EVM_ADDRESS, "mimeType": "application/json",
             "params": {"pair": "e.g. BTC/USD (default)"},
             "description": "Trading signal for one pair"},
            {"path": "/signals/top", "method": "GET", "price": TOP_PRICE,
             "network": NETWORK, "payTo": EVM_ADDRESS, "mimeType": "application/json",
             "description": "Market scan: strongest BUY/SELL signals across major pairs"},
        ],
        "disclaimer": _DISCLAIMER,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=4021, debug=False)
