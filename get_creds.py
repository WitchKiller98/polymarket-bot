#!/usr/bin/env python3
"""
Standalone Polymarket API credential generator.
Manual EIP-712 signing — no py-clob-client/eip712-structs/pysha3 needed.
Only uses: eth_account, eth_abi, eth_utils (all already installed).

Usage:
    python3 get_creds.py
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

from dotenv import load_dotenv
from eth_account import Account
from eth_abi import encode as abi_encode
from eth_utils import keccak

load_dotenv()

PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY")
CLOB_HOST   = "https://clob.polymarket.com"
CHAIN_ID    = 137  # Polygon mainnet

if not PRIVATE_KEY:
    sys.exit("ERROR: POLY_PRIVATE_KEY not set in .env")

# ---- EIP-712 constants ----

DOMAIN_TYPEHASH = keccak(b"EIP712Domain(string name,string version,uint256 chainId)")
CLOB_AUTH_TYPEHASH = keccak(b"ClobAuth(string address,string timestamp,uint256 nonce,string message)")

DOMAIN_SEPARATOR = keccak(abi_encode(
    ["bytes32", "bytes32", "bytes32", "uint256"],
    [DOMAIN_TYPEHASH, keccak(b"ClobAuthDomain"), keccak(b"1"), CHAIN_ID],
))

MSG_TEXT = "This message attests that I control the given wallet"


def sign_clob_auth(private_key: str) -> tuple[str, str, str, str]:
    """Sign a ClobAuth EIP-712 message. Returns (address, sig, timestamp, nonce)."""
    acct      = Account.from_key(private_key)
    address   = acct.address
    timestamp = str(int(time.time()))
    nonce     = 0

    # hashStruct(ClobAuth)
    struct_hash = keccak(abi_encode(
        ["bytes32", "bytes32", "bytes32", "uint256", "bytes32"],
        [
            CLOB_AUTH_TYPEHASH,
            keccak(address.encode()),
            keccak(timestamp.encode()),
            nonce,
            keccak(MSG_TEXT.encode()),
        ],
    ))

    # EIP-712: \x19\x01 ‖ domainSeparator ‖ hashStruct
    digest = keccak(b"\x19\x01" + DOMAIN_SEPARATOR + struct_hash)

    signed  = Account.unsafe_sign_hash(digest, acct.key)
    sig_hex = "0x" + signed.signature.hex()

    return address, sig_hex, timestamp, str(nonce)


def _make_request(url: str, headers: dict, method: str, data: bytes | None = None) -> tuple[int, dict]:
    """HTTP request preserving exact header casing (urllib.capitalize() breaks POLY-* headers)."""
    req = urllib.request.Request(url, data=data, method=method)
    # add_unredirected_header bypasses capitalize()
    for k, v in headers.items():
        req.add_unredirected_header(k, v)
    req.add_unredirected_header("User-Agent", "Mozilla/5.0 polymarket-bot/0.1")
    req.add_unredirected_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()}


def post_json(url: str, headers: dict) -> tuple[int, dict]:
    return _make_request(url, headers, "POST", data=b"{}")


def get_json(url: str, headers: dict) -> tuple[int, dict]:
    return _make_request(url, headers, "GET")


def main():
    print("Signing EIP-712 ClobAuth message...")
    address, signature, timestamp, nonce = sign_clob_auth(PRIVATE_KEY)

    print(f"  Address:   {address}")
    print(f"  Timestamp: {timestamp}")
    print(f"  Sig len:   {len(signature)}")
    print(f"  Sig:       {signature[:20]}...{signature[-8:]}")

    headers = {
        "POLY-ADDRESS":   address,
        "POLY-SIGNATURE": signature,
        "POLY-TIMESTAMP": timestamp,
        "POLY-NONCE":     nonce,
        "Content-Type":   "application/json",
    }

    # Try creating new credentials first
    print("\n1) Trying POST /auth/api-key (create new)...")
    status, data = post_json(f"{CLOB_HOST}/auth/api-key", dict(headers))
    print(f"   Status: {status}")

    if status == 200 and "apiKey" in data:
        print_creds(data)
        return

    print(f"   Response: {data}")

    # Fall back to deriving existing credentials
    print("\n2) Trying GET /auth/derive-api-key (derive existing)...")
    status, data = get_json(f"{CLOB_HOST}/auth/derive-api-key", dict(headers))
    print(f"   Status: {status}")

    if status == 200 and "apiKey" in data:
        print_creds(data)
        return

    print(f"   Response: {data}")
    print("\nBoth methods failed. Check that your POLY_PRIVATE_KEY is correct.")


def print_creds(data: dict):
    api_key    = data.get("apiKey", "")
    secret     = data.get("secret", "")
    passphrase = data.get("passphrase", "")
    print("\n=== SUCCESS ===")
    print(f"API Key:    {api_key}")
    print(f"Secret:     {secret}")
    print(f"Passphrase: {passphrase}")
    print("\nAdd these to your .env:")
    print(f"POLY_API_KEY={api_key}")
    print(f"POLY_SECRET={secret}")
    print(f"POLY_PASSPHRASE={passphrase}")


if __name__ == "__main__":
    main()
