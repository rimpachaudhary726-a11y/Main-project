#!/usr/bin/env python3
"""
A minimal standalone chatbot that talks to the Cerebras API.

The script reads a user prompt from the command line arguments or stdin,
sends it to the Cerebras chat completion endpoint, and prints the assistant
reply.

It exits with a non‑zero status and a clear error message if:
* The CEREBRAS_API_KEY environment variable is missing.
* The HTTP request fails or returns a non‑200 status.
* The response payload is malformed.
"""

import os
import sys
import json
import textwrap

import requests

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
API_ENDPOINT = "https://api.cerebras.ai/v1/chat/completions"
MODEL_NAME = "gpt-oss-120b"
TIMEOUT = 30  # seconds for the HTTP request


def fatal(msg: str) -> None:
    """Print an error message to stderr and exit with status 1."""
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def get_api_key() -> str:
    """Retrieve the Cerebras API key from the environment."""
    key = os.getenv("CEREBRAS_API_KEY")
    if not key:
        fatal("CEREBRAS_API_KEY environment variable is not set.")
    return key


def get_user_prompt() -> str:
    """
    Determine the user prompt.

    Preference order:
    1. Command‑line arguments (joined with spaces)
    2. Entire stdin (useful in CI pipelines)
    3. A default placeholder prompt (ensures the script can run without input)
    """
    if len(sys.argv) > 1:
        return " ".join(sys.argv[1:]).strip()

    # Read everything from stdin (non‑blocking if the stream is empty)
    if not sys.stdin.isatty():
        data = sys.stdin.read()
        if data.strip():
            return data.strip()

    # Fallback default – the script still produces a valid request.
    return "Hello, how are you?"


def call_cerebras_chat(api_key: str, prompt: str) -> str:
    """Send a chat request to the Cerebras API and return the assistant's reply."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "user", "content": prompt}
        ]
    }

    try:
        response = requests.post(
            API_ENDPOINT,
            headers=headers,
            json=payload,
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        fatal(f"Network error while contacting Cerebras API: {exc}")

    if response.status_code != 200:
        # Attempt to surface the API's error message for debugging.
        try:
            err_detail = response.json()
            err_msg = json.dumps(err_detail, indent=2)
        except Exception:
            err_msg = response.text
        fatal(
            f"Cerebras API returned HTTP {response.status_code}.\n"
            f"Response body:\n{err_msg}"
        )

    try:
        data = response.json()
        # Expected format: {"choices": [{"message": {"content": "..."}}, ...], ...}
        choice = data["choices"][0]
        message = choice["message"]
        content = message["content"]
    except (KeyError, IndexError, TypeError) as exc:
        fatal(f"Malformed response from Cerebras API: {exc}\nFull payload: {data}")

    return content


def main() -> None:
    api_key = get_api_key()
    prompt = get_user_prompt()
    reply = call_cerebras_chat(api_key, prompt)

    # Print the reply; using textwrap to keep CI logs tidy.
    print(textwrap.dedent(reply).strip())


if __name__ == "__main__":
    main()