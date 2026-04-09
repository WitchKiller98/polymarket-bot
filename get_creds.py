#!/usr/bin/env python3
"""
Standalone Polymarket API credential generator.
Uses only eth_account + stdlib — no py-clob-client or requests needed.

Usage:
    python3 get_creds.py
"""

import json
import os
import sys
import time
import urllib.request

from dotenv import load_dotenv
from eth_account import Account

load_dotenv()

PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY")
CLOB_HOST   = "https://clob.polymarket.com"
CHAIN_ID    = 137  # Polygon mainnet

if not PRIVATE_KEY:
    sys.exit("ERROR: POLY_PRIVATE_KEY not set in .env")


def make_l1_headers(private_key: str) -> dict:
    """Build Polymarket L1 auth headers using EIP-712 signing."""
    acct      = Account.from_key(private_key)
    address   = acct.address
    timestamp = str(int(time.time()))
    nonce     = 0
    message   = "This message attests that I control the given wallet"

    typed_data = {
        "domain": {
            "name":    "ClobAuthDomain",
            "version": "1",
            "chainId": CHAIN_ID,
        },
        "types": {
            "EIP712Domain": [
                {"name": "name",    "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "ClobAuth": [
                {"name": "address",   "type": "string"},
                {"name": "timestamp", "type": "string"},
                {"name": "nonce",     "type": "uint256"},
                {"name": "message",   "type": "string"},
            ],
        },
        "primaryType": "ClobAuth",
        "message": {
            "address":   address,
            "timestamp": timestamp,
            "nonce":     nonce,
            "message":   message,
        },
    }

    signed    = Account.sign_typed_data(acct.key, full_message=typed_data)
    signature = signed.signature.hex()

    return {
        "POLY-ADDRESS":   address,
        "POLY-SIGNATURE": signature,
        "POLY-TIMESTAMP": timestamp,
        "POLY-NONCE":     str(nonce),
        "Content-Type":   "application/json",
    }


def post_json(url: str, headers: dict) -> tuple[int, dict]:
    """POST with stdlib only — no requests/aiohttp needed."""
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
            return resp.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return e.code, {"error": body}


def main():
    print("Generating L1 auth headers...")
    headers = make_l1_headers(PRIVATE_KEY)
    print(f"Address: {headers['POLY-ADDRESS']}")

    print("\nRequesting API credentials from Polymarket CLOB...")
    status, data = post_json(f"{CLOB_HOST}/auth/api-key", headers)

    if status == 200 and "apiKey" in data:
        print("\n=== SUCCESS ===")
        print(f"API Key:    {data['apiKey']}")
        print(f"Secret:     {data['secret']}")
        print(f"Passphrase: {data['passphrase']}")
        print("\nAdd these to your .env:")
        print(f"POLY_API_KEY={data['apiKey']}")
        print(f"POLY_SECRET={data['secret']}")
        print(f"POLY_PASSPHRASE={data['passphrase']}")
    else:
        print(f"\nERROR {status}: {data}")


if __name__ == "__main__":
    main()
