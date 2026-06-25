#!/usr/bin/env python3
"""
A minimal ChatGPT‑style chatbot that uses the Cerebras API.

It expects the environment variable CEREBRAS_API_KEY to be set.
Usage examples (run in a non‑interactive CI environment):

    # Pass prompt as a CLI argument
    python chatbot.py "Tell me a joke."

    # Pipe prompt via stdin
    echo "What is the capital of France?" | python chatbot.py
"""

import os
import sys
import json
import urllib.parse
from typing import List, Dict

import requests


API_ENDPOINT = "https://api.cerebras.ai/v1/chat/completions"
MODEL_NAME = "gpt-oss-120b"


def get_api_key() -> str:
    """Retrieve the Cerebras API key from the environment."""
    key = os.environ.get("CEREBRAS_API_KEY")
    if not key:
        print(
            "ERROR: environment variable CEREBRAS_API_KEY is not set.", file=sys.stderr
        )
        sys.exit(1)
    return key


def build_prompt() -> str:
    """
    Determine the user prompt.

    Preference order:
    1. Command‑line arguments (joined with spaces)
    2. Entire stdin content
    """
    if len(sys.argv) > 1:
        return " ".join(sys.argv[1:]).strip()
    # Read from stdin (if piped)
    data = sys.stdin.read()
    return data.strip()


def build_payload(prompt: str) -> Dict:
    """Create the JSON payload expected by the Cerebras chat endpoint."""
    return {
        "model": MODEL_NAME,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 1024,
        "temperature": 0.7,
    }


def call_cerebras_api(api_key: str, payload: Dict) -> Dict:
    """Send the request to Cerebras and return the parsed JSON response."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(API_ENDPOINT, headers=headers, json=payload, timeout=30)
    except requests.RequestException as exc:
        print(f"ERROR: network request failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if not response.ok:
        # Try to include any JSON error message the API returned
        try:
            err_detail = response.json()
            err_msg = json.dumps(err_detail, indent=2)
        except Exception:
            err_msg = response.text
        print(
            f"ERROR: API request failed with status {response.status_code}:\n{err_msg}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        return response.json()
    except json.JSONDecodeError as exc:
        print(f"ERROR: failed to parse JSON response: {exc}", file=sys.stderr)
        sys.exit(1)


def extract_reply(api_response: Dict) -> str:
    """Extract the assistant's reply from the API response."""
    try:
        # The expected shape is: {"choices": [{"message": {"content": "..."}}, ...]}
        return api_response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        print(f"ERROR: unexpected API response format: {exc}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    api_key = get_api_key()
    prompt = build_prompt()

    if not prompt:
        print(
            "ERROR: no prompt supplied. Provide a prompt as arguments or via stdin.",
            file=sys.stderr,
        )
        sys.exit(1)

    payload = build_payload(prompt)
    response_json = call_cerebras_api(api_key, payload)
    reply = extract_reply(response_json)

    print(reply)


if __name__ == "__main__":
    main()