"""
Extra paid x402 endpoints for the Kraken Crypto Signals API — kept in a separate,
self-contained module so it can be added with ZERO risk to the existing service.

signals_api.py loads this via a try/except: if anything here fails, it is skipped
and the original API runs unchanged.

Adds two paid routes (USDC on Base, x402):
  GET /history          ($0.05) — revealed historical signal snapshots (commit-reveal),
                                   with verifiable hashes; the latest snapshot is withheld.
  GET /regime/changed   ($0.02) — current market regime (RISK_ON/RISK_OFF) from the
                                   provable regime chain + whether it changed since ?since=.

Data sources (already present on the server):
  - /root/signals_api/track_record_private.jsonl  (revealed signals; commit-reveal)
  - https://regime.nsgoods.org/provable/head        (regime chain head)
"""

import os
import json
import time
import threading
import urllib.request
from datetime import datetime, timezone

from flask import jsonify, request


def register(app, routes, *, EVM_ADDRESS, NETWORK, signed, unavailable_body,
             DISCLAIMER, RouteConfig, PaymentOption,
             declare_discovery_extension, OutputConfig):
    """Mutate `routes` (so the x402 middleware paywalls the new routes) and attach
    the Flask handlers. Call this BEFORE payment_middleware(app, routes=routes, ...)."""

    HISTORY_PRICE = os.getenv("HISTORY_PRICE", "$0.05")
    REGIME_PRICE = os.getenv("REGIME_PRICE", "$0.02")
    REGIME_HEAD_URL = os.getenv("REGIME_HEAD_URL",
                                "https://regime.nsgoods.org/provable/head")
    TRACK_RECORD_PRIVATE = os.getenv(
        "TRACK_RECORD_PRIVATE", "/root/signals_api/track_record_private.jsonl")

    # ---- regime head fetch (cached 30s) ------------------------------------
    _ttl = 30
    _lock = threading.Lock()
    _cache = {}

    def fetch_regime_head():
        now = time.time()
        c = _cache.get("head")
        if c and c[0] > now:
            return c[1]
        with _lock:
            c = _cache.get("head")
            if c and c[0] > now:
                return c[1]
            req = urllib.request.Request(REGIME_HEAD_URL,
                                         headers={"User-Agent": "signals-api"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.load(r)
            head = data.get("head", data) or {}
            out = {
                "regime": head.get("regime"),
                "seq": head.get("seq", data.get("count")),
                "coin": head.get("coin"),
                "ts_utc": head.get("ts_utc"),
            }
            _cache["head"] = (now + _ttl, out)
            return out

    # ---- Bazaar discovery extensions ---------------------------------------
    hist_ext = declare_discovery_extension(
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100,
                          "default": 20,
                          "description": "How many past snapshots (1-100)."},
            },
            "required": [],
        },
        output=OutputConfig(
            example={
                "count": 1,
                "history": [{
                    "seq": 12, "ts": "2026-06-10T18:00:00+00:00",
                    "h": "d9ed7d97...", "prev": "1a9521f9...",
                    "signals": [{"pair": "BTC/USD", "signal": "BUY",
                                 "confidence": 0.62}],
                }],
                "generated_at": "2026-06-13T20:00:00+00:00",
            },
            schema={
                "type": "object",
                "properties": {
                    "count": {"type": "integer"},
                    "history": {"type": "array", "items": {"type": "object"}},
                    "generated_at": {"type": "string", "format": "date-time"},
                },
                "required": ["count", "history", "generated_at"],
            },
        ),
    )
    hist_ext["bazaar"]["info"]["input"]["method"] = "GET"

    regime_ext = declare_discovery_extension(
        input_schema={
            "type": "object",
            "properties": {
                "since": {"type": "string",
                          "description": "Regime you last saw (e.g. RISK_ON); "
                                         "response says if it changed."},
            },
            "required": [],
        },
        output=OutputConfig(
            example={
                "current_regime": "RISK_OFF", "since": "RISK_ON", "changed": True,
                "seq": 444, "coin": "BNB", "ts_utc": "2026-06-12T19:00:02+00:00",
                "generated_at": "2026-06-13T20:00:00+00:00",
            },
            schema={
                "type": "object",
                "properties": {
                    "current_regime": {"type": "string"},
                    "since": {"type": ["string", "null"]},
                    "changed": {"type": ["boolean", "null"]},
                    "seq": {"type": ["integer", "null"]},
                    "coin": {"type": ["string", "null"]},
                    "ts_utc": {"type": ["string", "null"]},
                    "generated_at": {"type": "string", "format": "date-time"},
                },
                "required": ["current_regime", "generated_at"],
            },
        ),
    )
    regime_ext["bazaar"]["info"]["input"]["method"] = "GET"

    # ---- register paid routes with the x402 middleware ---------------------
    routes["GET /history"] = RouteConfig(
        accepts=[PaymentOption(scheme="exact", pay_to=EVM_ADDRESS,
                               price=HISTORY_PRICE, network=NETWORK)],
        mime_type="application/json",
        description="Historical revealed signal snapshots (commit-reveal hash chain): "
                    "past BUY/SELL/HOLD + confidence across major pairs, each with its "
                    "verifiable hash. Bulk pay-per-call data for AI agents (USDC on Base).",
        extensions=hist_ext,
    )
    routes["GET /regime/changed"] = RouteConfig(
        accepts=[PaymentOption(scheme="exact", pay_to=EVM_ADDRESS,
                               price=REGIME_PRICE, network=NETWORK)],
        mime_type="application/json",
        description="Market-regime state (RISK_ON / RISK_OFF) from a provable chain, plus "
                    "whether it changed since the regime you last saw. Cheap poll endpoint "
                    "for AI agents to react to regime flips (USDC on Base).",
        extensions=regime_ext,
    )

    # ---- handlers ----------------------------------------------------------
    @app.route("/history")
    def history():
        try:
            limit = int(request.args.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(100, limit))

        entries = []
        try:
            with open(TRACK_RECORD_PRIVATE) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        entries.append(json.loads(line))
        except FileNotFoundError:
            entries = []

        if len(entries) < 2:
            return jsonify(unavailable_body("not enough history yet")), 503

        revealed = entries[:-1][-limit:]   # exclude latest snapshot, take last `limit`
        out = [{
            "seq": e.get("seq"), "ts": e.get("ts"),
            "h": e.get("h"), "prev": e.get("prev"),
            "signals": e.get("signals"),
        } for e in revealed]

        return jsonify(signed({
            "count": len(out),
            "note": ("Revealed historical signal snapshots (commit-reveal). The most "
                     "recent snapshot is withheld to protect the live edge. Verify each "
                     "entry's 'h' against /track-record."),
            "history": out,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "disclaimer": DISCLAIMER,
        }))

    @app.route("/regime/changed")
    def regime_changed():
        try:
            head = fetch_regime_head()
        except Exception as e:
            return jsonify(unavailable_body(
                "regime feed: {}".format(type(e).__name__))), 503

        since = request.args.get("since")
        current = head.get("regime")
        changed = (str(since).upper() != str(current).upper()) if since else None

        return jsonify(signed({
            "current_regime": current,
            "since": str(since).upper() if since else None,
            "changed": changed,
            "seq": head.get("seq"),
            "coin": head.get("coin"),
            "ts_utc": head.get("ts_utc"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "disclaimer": DISCLAIMER,
        }))

    return ["GET /history", "GET /regime/changed"]
