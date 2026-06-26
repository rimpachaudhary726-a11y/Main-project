#!/usr/bin/env python3
"""
A simple chatbot client that calls the Cerebras chat completion API.

The script expects two environment variables:
- CEREBRAS_API_KEY: the API key for authentication.
- CHAT_PROMPT: the user prompt to send to the model.

If either variable is missing, the script prints an error message to stderr
and exits with a non‑zero status code.

The response from the model is printed to stdout.
"""

import os
import sys
import json

try:
    import requests
except ImportError as e:
    print("Missing required package 'requests'. Install it via pip.", file=sys.stderr)
    raise

# Constants for the Cerebras API
API_ENDPOINT = "https://api.cerebras.ai/v1/chat/completions"
MODEL_NAME = "gpt-oss-120b"

def get_env_var(name: str) -> str:
    """Retrieve an environment variable or exit with an error."""
    value = os.environ.get(name)
    if not value:
        print(f"Error: environment variable '{name}' is not set.", file=sys.stderr)
        sys.exit(1)
    return value

def build_payload(prompt: str) -> dict:
    """Create the JSON payload for the API request."""
    return {
        "model": MODEL_NAME,
        "messages": [
            {"role": "user", "content": prompt}
        ]
    }

def call_cerebras_api(api_key: str, payload: dict) -> dict:
    """Send a POST request to the Cerebras chat completions endpoint."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    try:
        response = requests.post(API_ENDPOINT, headers=headers, json=payload, timeout=30)
    except requests.RequestException as e:
        print(f"Network error while contacting Cerebras API: {e}", file=sys.stderr)
        sys.exit(1)

    if response.status_code != 200:
        # Print the full response body for debugging
        print(f"Cerebras API returned error {response.status_code}: {response.text}", file=sys.stderr)
        sys.exit(1)

    try:
        return response.json()
    except json.JSONDecodeError as e:
        print(f"Failed to parse JSON response from Cerebras API: {e}", file=sys.stderr)
        sys.exit(1)

def extract_reply(api_response: dict) -> str:
    """Extract the assistant's reply from the API response."""
    try:
        # Typical response shape: {"choices": [{"message": {"content": "..."} }], ...}
        return api_response["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        print(f"Unexpected response format from Cerebras API: {e}\nFull response: {api_response}", file=sys.stderr)
        sys.exit(1)

def main() -> None:
    api_key = get_env_var("CEREBRAS_API_KEY")
    prompt = get_env_var("CHAT_PROMPT")

    payload = build_payload(prompt)
    api_response = call_cerebras_api(api_key, payload)
    reply = extract_reply(api_response)

    print(reply)

if __name__ == "__main__":
    main()