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
import json
import base64
import threading
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request, Response

from x402.http import FacilitatorConfig, HTTPFacilitatorClientSync, PaymentOption
from x402.http.facilitator_client_base import CreateHeadersAuthProvider
from x402.http.middleware.flask import payment_middleware
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.server import x402ResourceServerSync
from x402.extensions.bazaar import declare_discovery_extension, OutputConfig

from cdp.auth import GetAuthHeadersOptions, get_auth_headers

from eth_account import Account
from eth_account.messages import encode_defunct

load_dotenv()

# ---- Config (from .env) ----------------------------------------------------
EVM_ADDRESS = os.getenv("EVM_ADDRESS")                 # your Base wallet (receives USDC)
NETWORK = os.getenv("NETWORK", "eip155:84532")         # 84532 = Base Sepolia (test); 8453 = mainnet
FACILITATOR_URL = os.getenv("FACILITATOR_URL", "https://x402.org/facilitator")
PRICE = os.getenv("PRICE", "$0.01")                    # price per /signals call
TOP_PRICE = os.getenv("TOP_PRICE", "$0.10")            # price per /signals/top call (market scan)
HISTORY_PRICE = os.getenv("HISTORY_PRICE", "$0.05")    # /history (mirrors extra_endpoints.py)
REGIME_PRICE = os.getenv("REGIME_PRICE", "$0.02")      # /regime/changed
BACKTEST_PRICE = os.getenv("BACKTEST_PRICE", "$0.25")  # /backtest
INDICATORS_PRICE = os.getenv("INDICATORS_PRICE", "$0.005")  # /indicators (high-freq TA)
BOT_ROOT = os.getenv("BOT_ROOT", "/root/bot")          # path to the Kraken bot package
SIGNING_KEY = os.getenv("SIGNING_KEY")                 # dedicated FUND-LESS key that signs signals
CDP_API_KEY_ID = os.getenv("CDP_API_KEY_ID")           # CDP facilitator auth (Bazaar indexer requires CDP)
CDP_API_KEY_SECRET = os.getenv("CDP_API_KEY_SECRET")   # CDP facilitator auth

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


# ---- Signed signals --------------------------------------------------------
# Every signal response carries an ECDSA signature buyers can verify (recover)
# against our published signer address -> proves the signal is authentic and
# untampered. Uses a DEDICATED, FUND-LESS key (never the receiving wallet key).
_SIGNER_ADDRESS = Account.from_key(SIGNING_KEY).address if SIGNING_KEY else None


def _signed(payload):
    """Return payload + 'signed_by' (address) + 'signature' (over the JSON body
    BEFORE these two keys are added)."""
    if not SIGNING_KEY:
        return payload
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    raw = Account.sign_message(encode_defunct(text=body), private_key=SIGNING_KEY).signature
    sig = raw.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    out = dict(payload)
    out["signed_by"] = _SIGNER_ADDRESS
    out["signature"] = sig
    return out


# ---- App + x402 middleware (sync variants, Flask is sync) ------------------
app = Flask(__name__)

# ---- Facilitator wiring ----------------------------------------------------
# Active: CDP Facilitator. Bazaar indexing happens on the first successful
# settle through CDP (docs.cdp.coinbase.com/x402/bazaar). Auth is per-request
# JWT, generated by the cdp-sdk helper and adapted to the x402 SDK's
# AuthProvider protocol via CreateHeadersAuthProvider.
# Fallback - swap back if CDP fails:
#   facilitator = HTTPFacilitatorClientSync(FacilitatorConfig(url=FACILITATOR_URL))
# (FACILITATOR_URL in .env defaults to https://facilitator.payai.network)

CDP_FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"
_CDP_HOST = "api.cdp.coinbase.com"
_CDP_BASE_PATH = "/platform/v2/x402"


def _cdp_endpoint_headers(method: str, path: str) -> dict:
    return get_auth_headers(GetAuthHeadersOptions(
        api_key_id=CDP_API_KEY_ID,
        api_key_secret=CDP_API_KEY_SECRET,
        request_method=method,
        request_host=_CDP_HOST,
        request_path=path,
    ))


def cdp_create_headers() -> dict:
    """Return the per-x402-endpoint auth headers dict the SDK expects.
    Generates fresh JWTs per call so each request carries a valid Authorization.
    """
    return {
        "verify":    _cdp_endpoint_headers("POST", f"{_CDP_BASE_PATH}/verify"),
        "settle":    _cdp_endpoint_headers("POST", f"{_CDP_BASE_PATH}/settle"),
        "supported": _cdp_endpoint_headers("GET",  f"{_CDP_BASE_PATH}/supported"),
        "bazaar":    _cdp_endpoint_headers("POST", f"{_CDP_BASE_PATH}/bazaar"),
    }


if CDP_API_KEY_ID and CDP_API_KEY_SECRET:
    facilitator = HTTPFacilitatorClientSync(FacilitatorConfig(
        url=CDP_FACILITATOR_URL,
        auth_provider=CreateHeadersAuthProvider(cdp_create_headers),
    ))
else:
    # CDP creds missing -> automatic fallback to the PayAI/.env-configured URL
    facilitator = HTTPFacilitatorClientSync(FacilitatorConfig(url=FACILITATOR_URL))

server = x402ResourceServerSync(facilitator)
server.register(NETWORK, ExactEvmServerScheme())


# ---- EXTENSION-RESPONSES logging (Bazaar indexing trace) -------------------
# Coinbase's CDP facilitator returns an EXTENSION-RESPONSES header on its
# settle response. We surface it to journald so we can see whether Bazaar
# metadata is being processed or rejected after the first real payment.
@app.after_request
def _log_extension_responses(resp):
    er = resp.headers.get("EXTENSION-RESPONSES") or resp.headers.get("extension-responses")
    if er:
        try:
            print(f"[x402-bazaar] EXTENSION-RESPONSES: {er[:800]}", flush=True)
        except Exception:
            pass
    return resp


# ---- Bazaar discovery extensions (agentic.market indexer) ------------------
# Top-level "extensions.bazaar" gets injected into the 402 PAYMENT-REQUIRED
# response so indexers (e.g. agentic.market) can list this service. Output
# example + schema mirror the actual signed JSON returned post-payment.

_SIG_EXAMPLE = {
    "pair": "BTC/USD",
    "signal": "HOLD",
    "confidence": 0.42,
    "timeframe": "1m",
    "generated_at": "2026-06-06T22:00:00+00:00",
    "disclaimer": _DISCLAIMER,
    "signed_by": "0x5e63d01d6A266BC17f577B80199a2a07B15053C7",
    "signature": "0x10fafe37c311ef9f70d1c5b119454a553c82c0bb9981b09560485c3dc8072fc47bebad8022f12b240e02c38cbe4243a1f10b63f06502356c263dea35571974411c",
}

_SIG_SCHEMA = {
    "type": "object",
    "properties": {
        "pair":         {"type": "string"},
        "signal":       {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
        "confidence":   {"type": "number", "minimum": 0, "maximum": 1},
        "timeframe":    {"type": "string"},
        "generated_at": {"type": "string", "format": "date-time"},
        "disclaimer":   {"type": "string"},
        "signed_by":    {"type": "string"},
        "signature":    {"type": "string"},
    },
    "required": ["pair", "signal", "confidence", "timeframe", "generated_at"],
}

_TOP_EXAMPLE = {
    "scanned_pairs": 12,
    "actionable": 2,
    "top": [
        {"pair": "BTC/USD", "signal": "BUY", "confidence": 0.62,
         "timeframe": "1m", "generated_at": "2026-06-06T22:00:00+00:00"},
    ],
    "universe": ["BTC/USD", "ETH/USD", "SOL/USD", "..."],
    "generated_at": "2026-06-06T22:00:00+00:00",
    "disclaimer": "Educational signal. Not financial advice.",
    "signed_by": "0x5e63d01d6A266BC17f577B80199a2a07B15053C7",
    "signature": "0x...",
}

_TOP_SCHEMA = {
    "type": "object",
    "properties": {
        "scanned_pairs": {"type": "integer", "minimum": 0},
        "actionable":    {"type": "integer", "minimum": 0},
        "top": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pair":         {"type": "string"},
                    "signal":       {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
                    "confidence":   {"type": "number"},
                    "timeframe":    {"type": "string"},
                    "generated_at": {"type": "string", "format": "date-time"},
                    "disclaimer":   {"type": "string"},
                },
            },
        },
        "universe":     {"type": "array", "items": {"type": "string"}},
        "generated_at": {"type": "string", "format": "date-time"},
        "disclaimer":   {"type": "string"},
        "signed_by":    {"type": "string"},
        "signature":    {"type": "string"},
    },
    "required": ["scanned_pairs", "actionable", "top", "universe", "generated_at"],
}

_BAZAAR_SIGNALS_EXT = declare_discovery_extension(
    input={"pair": "BTC/USD"},
    input_schema={
        "type": "object",
        "properties": {
            "pair": {
                "type": "string",
                "default": "BTC/USD",
                "description": "Any active Kraken */USD spot pair (e.g. BTC/USD, ETH/USD)",
            },
        },
        "required": [],
    },
    output=OutputConfig(example=_SIG_EXAMPLE, schema=_SIG_SCHEMA),
)
# declare_discovery_extension() leaves input.method blank because the middleware
# is supposed to enrich it from the route key. The schema validator runs first
# and rejects the missing method, so we set it inline.
_BAZAAR_SIGNALS_EXT["bazaar"]["info"]["input"]["method"] = "GET"

_BAZAAR_TOP_EXT = declare_discovery_extension(
    input_schema={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "default": 3,
                "description": "How many top signals to return (1-10).",
            },
        },
        "required": [],
    },
    output=OutputConfig(example=_TOP_EXAMPLE, schema=_TOP_SCHEMA),
)
_BAZAAR_TOP_EXT["bazaar"]["info"]["input"]["method"] = "GET"


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
        description="Live market-state data for one crypto pair: BUY/SELL/HOLD state + confidence from a multi-timeframe engine. Machine-readable, ECDSA-signed. Input data for AI finance agents (USDC pay-per-call).",
        extensions=_BAZAAR_SIGNALS_EXT,
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
        description="Market scan data: strongest BUY/SELL states across major crypto pairs right now, with confidence. Machine-readable input for AI trading agents (USDC pay-per-call).",
        extensions=_BAZAAR_TOP_EXT,
    ),
}
# ---- Optional extra paid endpoints (additive; safe no-op if anything fails) ----
try:
    from extra_endpoints import register as _register_extra
    _register_extra(
        app, routes,
        EVM_ADDRESS=EVM_ADDRESS, NETWORK=NETWORK, signed=_signed,
        unavailable_body=(lambda reason=None: {
            "error": "signal_unavailable",
            "message": ("Live data is temporarily unavailable. You were not "
                        "charged; please retry shortly."),
            "detail": reason}),
        DISCLAIMER=_DISCLAIMER, RouteConfig=RouteConfig, PaymentOption=PaymentOption,
        declare_discovery_extension=declare_discovery_extension,
        OutputConfig=OutputConfig,
        engine=_engine, strategy=_strategy)
except Exception as _e:
    print("[extra_endpoints] skipped:", _e, flush=True)


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
    return jsonify(_signed(payload))


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
    return jsonify(_signed(out))


@app.route("/signals/preview")
def signals_preview():
    """Free sample: BTC/USD direction only (no confidence). Lets agents/devs
    evaluate before paying for /signals (confidence + any pair) or /signals/top."""
    payload = generate_signal(_DEFAULT_PAIR)
    if payload.get("_unavailable"):
        return jsonify(_unavailable_body(payload.get("reason"))), 503
    return jsonify(_signed({
        "pair": payload.get("pair"),
        "signal": payload.get("signal"),
        "preview": True,
        "note": ("Free sample: BTC/USD direction only. Pay GET /signals for "
                 "confidence + any Kraken */USD pair, or GET /signals/top for a "
                 "market scan."),
        "timeframe": payload.get("timeframe"),
        "generated_at": payload.get("generated_at"),
        "disclaimer": payload.get("disclaimer"),
    }))


_LANDING_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>Kraken Crypto Signals - x402 API</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="talentapp:project_verification" content="5ba32c6b051dd9be1a05c3de8e074080ba65dafda1659e3f6407ad97c2877f9a4d493422a60a1495e6a76cc4f9e0fd46abbafc2cba7d4e67cccf62bb0e1b7740">
<style>body{font-family:system-ui,Arial,sans-serif;max-width:680px;margin:40px auto;padding:0 16px;line-height:1.55;color:#111;background:#fafafa}code{background:#eee;padding:2px 6px;border-radius:4px}.box{background:#fff;border:1px solid #e3e3e3;border-radius:10px;padding:16px 20px;margin:16px 0}a{color:#1f6feb}</style>
</head><body>
<h1>Kraken Crypto Signals</h1>
<p>Machine-readable market-state data for AI agents: <b>BUY / SELL / HOLD</b> + confidence from a live multi-timeframe strategy.</p>
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
        "description": "Pay-per-call crypto market-state data (BUY/SELL/HOLD + confidence) for AI agents over x402 on Base.",
        "contact": {
            "name": "Kraken Crypto Signals (Nikoble1926)",
            "url": "https://github.com/Nikoble1926/kraken-crypto-signals",
        },
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
        "/history": {
            "get": {
                "operationId": "getHistory",
                "summary": "Historical revealed signal snapshots + verifiable hashes",
                "security": [{"x402Payment": []}],
                "x-payment-info": {
                    "protocols": ["x402"],
                    "price": {"mode": "fixed", "currency": "USD",
                              "amount": _price_amount(HISTORY_PRICE)},
                    "network": NETWORK,
                    "payTo": EVM_ADDRESS,
                },
                "parameters": [
                    {"name": "limit", "in": "query", "required": False,
                     "description": "How many past snapshots to return (1-100, default 20).",
                     "schema": {"type": "integer", "default": 20,
                                "minimum": 1, "maximum": 100}}
                ],
                "responses": {
                    "200": {"description": "Revealed signal snapshots (commit-reveal hash chain)."},
                    "402": {"description": "Payment required (x402 challenge)."},
                    "503": {"description": "Not enough history yet (no charge)."},
                },
            }
        },
        "/regime/changed": {
            "get": {
                "operationId": "getRegimeChanged",
                "summary": "Market regime (RISK_ON/RISK_OFF) + change-since flag",
                "security": [{"x402Payment": []}],
                "x-payment-info": {
                    "protocols": ["x402"],
                    "price": {"mode": "fixed", "currency": "USD",
                              "amount": _price_amount(REGIME_PRICE)},
                    "network": NETWORK,
                    "payTo": EVM_ADDRESS,
                },
                "parameters": [
                    {"name": "since", "in": "query", "required": False,
                     "description": "Regime you last saw (e.g. RISK_ON); response reports if it changed.",
                     "schema": {"type": "string", "example": "RISK_ON"}}
                ],
                "responses": {
                    "200": {"description": "Current regime + change indicator."},
                    "402": {"description": "Payment required (x402 challenge)."},
                    "503": {"description": "Regime feed unavailable (no charge)."},
                },
            }
        },
        "/backtest": {
            "get": {
                "operationId": "getBacktest",
                "summary": "Walk-forward replay: win-rate + avg forward return per signal",
                "security": [{"x402Payment": []}],
                "x-payment-info": {
                    "protocols": ["x402"],
                    "price": {"mode": "fixed", "currency": "USD",
                              "amount": _price_amount(BACKTEST_PRICE)},
                    "network": NETWORK,
                    "payTo": EVM_ADDRESS,
                },
                "parameters": [
                    {"name": "pair", "in": "query", "required": False,
                     "description": "Any active Kraken */USD spot pair.",
                     "schema": {"type": "string", "default": _DEFAULT_PAIR,
                                "example": "ETH/USD"}},
                    {"name": "forward", "in": "query", "required": False,
                     "description": "Forward window in 1m bars to score each signal (5-120, default 30).",
                     "schema": {"type": "integer", "default": 30,
                                "minimum": 5, "maximum": 120}},
                    {"name": "step", "in": "query", "required": False,
                     "description": "Walk-forward step in 1m bars (1-15, default 5).",
                     "schema": {"type": "integer", "default": 5,
                                "minimum": 1, "maximum": 15}},
                ],
                "responses": {
                    "200": {"description": "Recent walk-forward replay summary."},
                    "402": {"description": "Payment required (x402 challenge)."},
                    "503": {"description": "Backtest unavailable (no charge)."},
                },
            }
        },
        "/indicators": {
            "get": {
                "operationId": "getIndicators",
                "summary": "Atomic technical indicators (RSI, EMA, MACD, ATR, Bollinger, volume)",
                "security": [{"x402Payment": []}],
                "x-payment-info": {
                    "protocols": ["x402"],
                    "price": {"mode": "fixed", "currency": "USD",
                              "amount": _price_amount(INDICATORS_PRICE)},
                    "network": NETWORK,
                    "payTo": EVM_ADDRESS,
                },
                "parameters": [
                    {"name": "pair", "in": "query", "required": False,
                     "description": "Any active Kraken */USD spot pair.",
                     "schema": {"type": "string", "default": _DEFAULT_PAIR,
                                "example": "ETH/USD"}},
                    {"name": "tf", "in": "query", "required": False,
                     "description": "Candle timeframe.",
                     "schema": {"type": "string", "default": "1h",
                                "enum": ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]}},
                    {"name": "name", "in": "query", "required": False,
                     "description": "Optional: return only one indicator (e.g. rsi14, ema50, macd_hist).",
                     "schema": {"type": "string", "example": "rsi14"}},
                ],
                "responses": {
                    "200": {"description": "Indicator snapshot for the requested pair/timeframe."},
                    "402": {"description": "Payment required (x402 challenge)."},
                    "503": {"description": "Indicators unavailable (no charge)."},
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


_TRACK_RECORD_PUBLIC = os.getenv(
    "TRACK_RECORD_PUBLIC", "/root/signals_api/track_record_public.jsonl"
)


@app.route("/track-record")
def track_record():
    """Public, tamper-evident commitments to past signal snapshots (commit-reveal):
    only a hash chain is public now; the full snapshot can be revealed later to
    prove a past call without leaking live signals."""
    entries = []
    head = "GENESIS"
    try:
        with open(_TRACK_RECORD_PUBLIC) as f:
            for line in f:
                line = line.strip()
                if line:
                    e = json.loads(line)
                    entries.append(e)
                    head = e.get("h", head)
    except FileNotFoundError:
        pass
    return jsonify({
        "scheme": ("Append-only hash chain. Each h = sha256 of the full signal "
                   "snapshot (incl. a nonce). Only hashes are public now; reveal "
                   "an entry later to prove a past call. Verify: each entry's "
                   "'prev' must equal the previous entry's 'h'."),
        "count": len(entries),
        "head": head,
        "entries": entries[-50:],
    })


_YIELD_STATE = os.getenv("YIELD_STATE", "/root/signals_api/yield_agent_state.json")


@app.route("/yield-paper")
def yield_paper():
    """Paper-mode yield agent: which vault, live APY, simulated balance/ROI.
    NO real funds -- a free simulation of stablecoin yield-rotation on Base."""
    try:
        st = json.load(open(_YIELD_STATE))
    except FileNotFoundError:
        return jsonify({"mode": "paper", "status": "no data yet"})
    try:
        roi = round((st.get("balance", 0) / 1000.0 - 1) * 100, 3)
    except Exception:
        roi = None
    return jsonify({
        "mode": "paper (simulation only, NO real funds)",
        "in_protocol": st.get("project"),
        "live_apy_pct": st.get("apy"),
        "sim_balance_usd": round(st.get("balance", 0), 4),
        "roi_pct_since_start": roi,
        "rotations": st.get("rotations"),
        "started": st.get("started"),
        "last_update": st.get("last_ts"),
    })


@app.route("/health")
def health_check():
    return jsonify({"status": "ok"})


_LLMS_TXT = (
    "# Kraken Crypto Signals — x402 pay-per-call API\n"
    "Machine-readable crypto market-state data for AI agents, paid per call in USDC on Base via x402. "
    "No accounts, no API keys. Every response is ECDSA-signed and verifiable.\n"
    "\n"
    "Base: https://signals.nsgoods.org\n"
    "Manifest: https://signals.nsgoods.org/.well-known/x402\n"
    "OpenAPI: https://signals.nsgoods.org/openapi.json\n"
    "Agent card (A2A): https://signals.nsgoods.org/.well-known/agent.json\n"
    "MCP bridge: https://github.com/Nikoble1926/kraken-crypto-signals/tree/main/mcp\n"
    "Quickstart (runnable demo agent, free preview + 402 challenge, ~5 min): "
    "https://github.com/Nikoble1926/kraken-crypto-signals/tree/main/examples/quickstart\n"
    "\n"
    "Paid endpoints (USDC on Base):\n"
    "- GET /signals?pair=BTC/USD   trading signal BUY/SELL/HOLD + confidence   $0.01\n"
    "- GET /signals/top            market scan, strongest signals             $0.10\n"
    "- GET /history?limit=20       revealed historical snapshots + hashes      $0.05\n"
    "- GET /regime/changed?since=RISK_ON   market regime + change flag         $0.02\n"
    "- GET /backtest?pair=BTC/USD  walk-forward win-rate + avg forward return  $0.25\n"
    "\n"
    "Free:\n"
    "- GET /signals/preview        sample BTC/USD direction\n"
    "- GET /track-record           public commit-reveal hash chain\n"
    "\n"
    "How to pay: call an endpoint; on HTTP 402, settle the x402 payment (USDC on Base) and retry. "
    "See https://x402.org. Not financial advice.\n"
)


@app.route("/llms.txt")
def llms_txt():
    return Response(_LLMS_TXT, mimetype="text/plain; charset=utf-8")


@app.route("/.well-known/agent.json")
def agent_card():
    return jsonify({
        "name": "Kraken Crypto Signals",
        "description": (
            "Pay-per-call crypto market-state data (BUY/SELL/HOLD + confidence) "
            "for AI agents over x402 on Base. ECDSA-signed, provable track-record, "
            "backtest proof-of-edge."
        ),
        "url": "https://signals.nsgoods.org",
        "version": "1.0.0",
        "provider": {
            "organization": "Nikoble1926",
            "url": "https://github.com/Nikoble1926/kraken-crypto-signals",
        },
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {"id": "signals", "name": "Trading signal",
             "description": "BUY/SELL/HOLD + confidence for one Kraken */USD pair. $0.01 USDC via x402.",
             "tags": ["crypto", "trading", "x402", "signals"]},
            {"id": "signals_top", "name": "Market scan",
             "description": "Strongest BUY/SELL signals across major pairs. $0.10 USDC.",
             "tags": ["crypto", "scan", "x402"]},
            {"id": "history", "name": "Historical signals",
             "description": "Revealed commit-reveal signal snapshots + verifiable hashes. $0.05 USDC.",
             "tags": ["crypto", "history", "x402"]},
            {"id": "regime_changed", "name": "Regime change",
             "description": "RISK_ON/RISK_OFF regime + change-since flag. $0.02 USDC.",
             "tags": ["crypto", "regime", "x402"]},
            {"id": "backtest", "name": "Strategy backtest",
             "description": "Walk-forward win-rate + avg forward return per signal. $0.25 USDC.",
             "tags": ["crypto", "backtest", "x402"]},
        ],
        "x402": {
            "network": NETWORK,
            "payTo": EVM_ADDRESS,
            "manifest": "https://signals.nsgoods.org/.well-known/x402",
        },
    })


@app.route("/.well-known/x402")
def x402_manifest():
    return jsonify({
        "x402Version": 2,
        "name": "Kraken Crypto Signals",
        "description": "Pay-per-call crypto market-state data (BUY/SELL/HOLD + confidence) for AI agents over x402 on Base. ECDSA-signed responses, free preview endpoint, no API keys.",
        "pairs": (
            "All active Kraken */USD spot pairs (e.g. BTC/USD, ETH/USD, SOL/USD, "
            "XRP/USD, DOGE/USD). Pass any with ?pair=."
        ),
        "signer": _SIGNER_ADDRESS,
        "signature_scheme": (
            "Every /signals, /signals/top and /signals/preview response includes "
            "'signed_by' (address) and 'signature'. Verify with Ethereum "
            "personal_sign / ecrecover over the JSON body WITHOUT the signed_by & "
            "signature keys, serialized json.dumps(sort_keys=True, "
            "separators=(',',':')). recover == signed_by => authentic & untampered."
        ),
        "endpoints": [
            {"path": "/signals", "method": "GET", "price": PRICE,
             "network": NETWORK, "payTo": EVM_ADDRESS, "mimeType": "application/json",
             "params": {"pair": "e.g. BTC/USD (default)"},
             "description": "Trading signal for one pair"},
            {"path": "/signals/top", "method": "GET", "price": TOP_PRICE,
             "network": NETWORK, "payTo": EVM_ADDRESS, "mimeType": "application/json",
             "description": "Market scan: strongest BUY/SELL signals across major pairs"},
            {"path": "/indicators", "method": "GET", "price": INDICATORS_PRICE,
             "network": NETWORK, "payTo": EVM_ADDRESS, "mimeType": "application/json",
             "params": {"pair": "e.g. BTC/USD (default)",
                        "tf": "1m|5m|15m|30m|1h (default)|4h|1d",
                        "name": "optional, return only one (e.g. rsi14, ema50, macd_hist)"},
             "description": "Atomic technical indicators (RSI, EMA, MACD, ATR, Bollinger, volume) for any pair/timeframe"},
        ],
        "disclaimer": _DISCLAIMER,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=4021, debug=False)
