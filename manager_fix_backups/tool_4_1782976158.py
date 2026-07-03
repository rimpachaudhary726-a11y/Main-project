#!/usr/bin/env python3
import sys
import json

# Try to use requests if available, otherwise fall back to urllib
try:
    import requests

    def get_bitcoin_price():
        try:
            response = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "bitcoin", "vs_currencies": "usd"},
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
            return data["bitcoin"]["usd"]
        except Exception as e:
            print(f"Error fetching price: {e}", file=sys.stderr)
            return None

except ImportError:
    import urllib.request
    import urllib.error

    def get_bitcoin_price():
        try:
            url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
            with urllib.request.urlopen(url, timeout=10) as resp:
                if resp.status != 200:
                    raise urllib.error.HTTPError(url, resp.status, resp.reason, resp.headers, None)
                data = json.loads(resp.read().decode())
                return data["bitcoin"]["usd"]
        except Exception as e:
            print(f"Error fetching price: {e}", file=sys.stderr)
            return None


def main():
    price = get_bitcoin_price()
    if price is not None:
        print(f"Current Bitcoin price: ${price}")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()