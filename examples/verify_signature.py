#!/usr/bin/env python3
"""Verify that a Kraken Crypto Signals response is authentic and untampered.

Every /signals, /signals/top and /signals/preview response carries two extra
keys: `signed_by` (the publisher's address) and `signature` (an EIP-191
personal_sign over the JSON body BEFORE those two keys were added).

This script hits the FREE /signals/preview endpoint (no payment, no API key),
reproduces the server's exact canonical serialization, recovers the signing
address from the signature, and checks it equals the published signer:

    0x5e63d01d6A266BC17f577B80199a2a07B15053C7

recover == signed_by == SIGNER  =>  the signal is genuinely signed and
nothing in the payload was modified in transit.

Run:
    pip install eth_account
    python examples/verify_signature.py
"""
import json
import urllib.request

from eth_account import Account
from eth_account.messages import encode_defunct

PREVIEW_URL = "https://signals.nsgoods.org/signals/preview"

# Published signer (a dedicated, fund-less key — never the receiving wallet).
SIGNER = "0x5e63d01d6A266BC17f577B80199a2a07B15053C7"


def canonical_body(payload: dict) -> str:
    """Reproduce the server's signing input EXACTLY.

    Server (signals_api.py::_signed) signs the payload BEFORE adding the
    signed_by/signature keys, using:
        json.dumps(payload, sort_keys=True, separators=(",", ":"))
    So we strip those two keys and serialize identically.
    """
    unsigned = {k: v for k, v in payload.items() if k not in ("signed_by", "signature")}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"))


def main() -> int:
    with urllib.request.urlopen(PREVIEW_URL, timeout=30) as resp:
        response = json.loads(resp.read().decode())

    signed_by = response["signed_by"]
    signature = response["signature"]

    body = canonical_body(response)
    message = encode_defunct(text=body)
    recovered = Account.recover_message(message, signature=signature)

    print("Fetched : " + PREVIEW_URL)
    print("Body    : " + body)
    print("signed_by : " + signed_by)
    print("recovered : " + recovered)
    print("signer    : " + SIGNER)

    ok = (
        recovered.lower() == signed_by.lower() == SIGNER.lower()
    )
    if ok:
        print("VERIFIED ✓  recover == signed_by == signer")
        return 0
    print("MISMATCH  signature does not recover to the published signer")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
