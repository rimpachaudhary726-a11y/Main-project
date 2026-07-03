#!/usr/bin/env python3
"""
A tiny utility that fetches the current Bitcoin price (USD) and prints it.
It tries two public endpoints – CoinDesk and CoinGecko – and exits with
a non‑zero status if both fail. No interactive input is required.
"""

import sys
import json
from typing import Callable

import requests


def fetch_json(url: str) -> dict:
    """GET ``url`` and return the parsed JSON body.

    Raises:
        requests.exceptions.RequestException – on network/HTTP errors.
        ValueError – if the response cannot be decoded as JSON.
    """
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return response.json()


def main() -> None:
    # Public endpoints (name, URL, extractor function)
    endpoints = [
        (
            "CoinDesk",
            "https://api.coindesk.com/v1/bpi/currentprice/BTC.json",
            lambda d: d["bpi"]["USD"]["rate_float"],  # type: ignore[index]
        ),
        (
            "CoinGecko",
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
            lambda d: d["bitcoin"]["usd"],  # type: ignore[index]
        ),
    ]

    for name, url, extractor in endpoints:
        try:
            data = fetch_json(url)
            price = extractor(data)  # may raise KeyError/TypeError if format unexpected
            print(f"Current Bitcoin price (via {name}): ${price}")
            return  # success – exit normally
        except Exception as exc:  # noqa: BLE001 – we want to surface any failure
            print(f"Error fetching price from {name}: {exc}", file=sys.stderr)

    # If we reach this point all attempts failed
    sys.exit(1)


if __name__ == "__main__":
    main()