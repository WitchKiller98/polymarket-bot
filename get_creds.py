#!/usr/bin/env python3
"""
Standalone Polymarket API credential generator.
Uses only eth_account + requests — no py-clob-client needed.

Usage:
    python3 get_creds.py
"""

import os
import sys
import time

import requests
from dotenv import load_dotenv
from eth_account import Account
from eth_account.messages import encode_typed_data

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
        "Content-Type":  "application/json",
    }


def main():
    print("Generating L1 auth headers...")
    headers = make_l1_headers(PRIVATE_KEY)
    print(f"Address: {headers['POLY-ADDRESS']}")

    print("\nRequesting API credentials from Polymarket CLOB...")
    resp = requests.post(f"{CLOB_HOST}/auth/api-key", headers=headers)

    if resp.status_code == 200:
        data = resp.json()
        print("\n=== SUCCESS ===")
        print(f"API Key:    {data.get('apiKey')}")
        print(f"Secret:     {data.get('secret')}")
        print(f"Passphrase: {data.get('passphrase')}")
        print("\nAdd these to your .env:")
        print(f"POLY_API_KEY={data.get('apiKey')}")
        print(f"POLY_SECRET={data.get('secret')}")
        print(f"POLY_PASSPHRASE={data.get('passphrase')}")
    else:
        print(f"\nERROR {resp.status_code}: {resp.text}")


if __name__ == "__main__":
    main()
