#!/usr/bin/env python3
"""
Simple Chatbot script.

- Tries to use the Cerebras API if the environment variable CEREBRAS_API_KEY is set.
- Falls back to a deterministic, rule‑based reply (reverses the user's text) when the key is missing.
- Reads a list of prompts from the environment variable CHAT_INPUTS (JSON array of strings).
  If CHAT_INPUTS is not set, a small default list is used.
- Prints each user message and the bot's reply.

The script is fully non‑interactive and suitable for running in CI/CD pipelines
such as GitHub Actions.
"""

import os
import json
import sys
import textwrap
from typing import List

try:
    import requests
except ImportError:  # pragma: no cover
    sys.stderr.write("The 'requests' package is required.\n")
    sys.exit(1)


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY")
CHAT_INPUTS_RAW = os.environ.get("CHAT_INPUTS")

# Default demo prompts (used when CHAT_INPUTS is unset or malformed)
DEFAULT_PROMPTS = [
    "Hello!",
    "Can you tell me a joke?",
    "What is the capital of France?",
    "Thanks, goodbye."
]


def load_prompts() -> List[str]:
    """Parse CHAT_INPUTS environment variable or fall back to defaults."""
    if not CHAT_INPUTS_RAW:
        return DEFAULT_PROMPTS

    try:
        data = json.loads(CHAT_INPUTS_RAW)
        if isinstance(data, list) and all(isinstance(item, str) for item in data):
            return data
        else:
            raise ValueError
    except Exception:
        sys.stderr.write(
            "CHAT_INPUTS is set but could not be parsed as a JSON list of strings. "
            "Falling back to default prompts.\n"
        )
        return DEFAULT_PROMPTS


def cerebras_query(message: str) -> str:
    """
    Send a request to the Cerebras API (if an API key is available).

    The actual endpoint and payload format are based on the public Cerebras
    documentation as of 2026. Adjust as needed for a real deployment.
    """
    endpoint = "https://api.cerebras.ai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {CEREBRAS_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "cerebras-llama-7b",   # example model; may differ
        "messages": [{"role": "user", "content": message}],
        "max_tokens": 200,
        "temperature": 0.7,
    }

    try:
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # Expected structure: {"choices": [{"message": {"content": "..."} }]}
        return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        sys.stderr.write(f"Error contacting Cerebras API: {exc}\n")
        return None


def fallback_reply(message: str) -> str:
    """
    Very simple deterministic reply used when the API key is missing or the request
    fails. This example just reverses the input text and adds a prefix.
    """
    reversed_msg = message[::-1]
    return f"(fallback) You said: {reversed_msg}"


def get_reply(message: str) -> str:
    """Obtain a reply either from Cerebras or via the fallback implementation."""
    if CEREBRAS_API_KEY:
        reply = cerebras_query(message)
        if reply:
            return reply
        else:
            # API call failed; fall back
            return fallback_reply(message)
    else:
        # No API key – use fallback
        return fallback_reply(message)


def main() -> None:
    prompts = load_prompts()

    for idx, user_msg in enumerate(prompts, start=1):
        print(f"User [{idx}]: {user_msg}")
        bot_reply = get_reply(user_msg)
        # Indent multiline replies for readability
        indented_reply = textwrap.indent(bot_reply, "  ")
        print(f"Bot  [{idx}]:\n{indented_reply}\n")


if __name__ == "__main__":
    main()