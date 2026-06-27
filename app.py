#!/usr/bin/env python3
"""
Simple chatbot client for the Cerebras API.

The script sends a user message (taken from the environment variable
CEREBRAS_PROMPT or a default prompt) to the Cerebras chat endpoint and
prints the assistant's reply.

All errors are printed to stderr and cause the script to exit with a non‑zero
status, as required.
"""

import os
import sys
import json

import requests  # external dependency


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
API_ENDPOINT = "https://api.cerebras.ai/v1/chat/completions"
MODEL_NAME = "gpt-oss-120b"

# Retrieve the API key; abort if it is missing.
API_KEY = os.environ.get("CEREBRAS_API_KEY")
if not API_KEY:
    print("Error: environment variable CEREBRAS_API_KEY is not set.", file=sys.stderr)
    sys.exit(1)

# Retrieve the prompt; use a reasonable default if none is supplied.
USER_PROMPT = os.environ.get("CEREBRAS_PROMPT")
if not USER_PROMPT:
    USER_PROMPT = "Hello! Can you tell me a short joke about cats?"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def build_request_body(prompt: str) -> dict:
    """Construct the JSON payload expected by the Cerebras chat API."""
    return {
        "model": MODEL_NAME,
        "messages": [
            {"role": "user", "content": prompt}
        ]
    }

def call_cerebras_api(payload: dict) -> dict:
    """POST the payload to the Cerebras endpoint and return the JSON response."""
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(API_ENDPOINT, headers=headers, json=payload, timeout=30)
    except requests.RequestException as exc:
        print(f"Network error while contacting Cerebras API: {exc}", file=sys.stderr)
        sys.exit(1)

    if response.status_code != 200:
        # Attempt to provide the response body for easier debugging.
        try:
            error_detail = response.json()
        except json.JSONDecodeError:
            error_detail = response.text
        print(
            f"API request failed with status {response.status_code}:\n{error_detail}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        return response.json()
    except json.JSONDecodeError as exc:
        print(f"Failed to decode JSON response: {exc}", file=sys.stderr)
        sys.exit(1)


def extract_reply(api_response: dict) -> str:
    """Pull the assistant's reply from the API response structure."""
    try:
        # The typical OpenAI‑compatible format:
        # {"choices": [{"message": {"content": "..."}}, ...], ...}
        return api_response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        print(f"Unexpected API response format: {exc}\nResponse: {api_response}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main execution
# ---------------------------------------------------------------------------
def main() -> None:
    payload = build_request_body(USER_PROMPT)
    api_response = call_cerebras_api(payload)
    reply = extract_reply(api_response)
    print(reply)


if __name__ == "__main__":
    main()