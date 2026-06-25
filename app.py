#!/usr/bin/env python3
"""
A minimal chatbot that forwards a user prompt to the Cerebras chat API
and prints the assistant's response.

Usage (non‑interactive):
    python chatbot.py "Your prompt here"

The script reads the Cerebras API key from the environment variable
CEREBRAS_API_KEY. If the key is missing or the request fails, the script
exits with a non‑zero status and prints the real error.
"""

import argparse
import json
import os
import sys

import requests


API_ENDPOINT = "https://api.cerebras.ai/v1/chat/completions"
MODEL_NAME = "gpt-oss-120b"


def build_payload(prompt: str) -> dict:
    """Create the JSON payload expected by the Cerebras API."""
    return {
        "model": MODEL_NAME,
        "messages": [
            {"role": "user", "content": prompt}
        ]
    }


def call_cerebras_api(api_key: str, payload: dict) -> str:
    """
    Send the request to Cerebras and return the assistant's reply.

    Raises:
        RuntimeError: If the response is malformed or missing expected fields.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    response = requests.post(API_ENDPOINT, headers=headers, json=payload, timeout=30)
    # Raise an HTTPError for bad status codes (4xx/5xx)
    response.raise_for_status()

    data = response.json()

    # Expected structure: {"choices": [{"message": {"content": "..."}}, ...], ...}
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Unexpected API response format: {e}\nFull response: {json.dumps(data, indent=2)}") from e


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Simple Cerebras chatbot (non‑interactive)."
    )
    parser.add_argument(
        "prompt",
        nargs="+",
        help="Prompt text to send to the chatbot. If multiple words are provided, they are joined with spaces."
    )
    args = parser.parse_args()

    prompt_text = " ".join(args.prompt).strip()
    if not prompt_text:
        print("Error: Prompt is empty.", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("CEREBRAS_API_KEY")
    if not api_key:
        print("Error: Environment variable CEREBRAS_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)

    payload = build_payload(prompt_text)

    try:
        answer = call_cerebras_api(api_key, payload)
    except requests.exceptions.RequestException as req_err:
        print(f"Network or HTTP error while contacting Cerebras API: {req_err}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as rt_err:
        print(f"Error processing API response: {rt_err}", file=sys.stderr)
        sys.exit(1)

    print(answer)


if __name__ == "__main__":
    main()