#!/usr/bin/env python3
"""
Multi‑turn chatbot using Cerebras API.

The script runs without any interactive input. It iterates over a predefined list of
user messages, sends the cumulative conversation to the Cerebras chat endpoint,
and prints the assistant's reply.

If the API returns HTTP 429 (rate‑limit / token quota exceeded), the script respects
the server‑provided `Retry-After` header (or falls back to a 60‑second wait) and
retries the request up to a limited number of attempts. Any other error results
in an immediate exit with a clear error message, as required.
"""

import os
import sys
import json
import time
from typing import List, Dict

import requests

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
API_ENDPOINT = "https://api.cerebras.ai/v1/chat/completions"
MODEL_NAME = "gpt-oss-120b"
SYSTEM_PROMPT = "You are a helpful assistant."

# Maximum number of retries for a 429 response
MAX_RETRIES = 5

# ----------------------------------------------------------------------
# Helper functions
# ----------------------------------------------------------------------
def get_api_key() -> str:
    """Fetch the Cerebras API key from the environment."""
    key = os.environ.get("CEREBRAS_API_KEY")
    if not key:
        print("ERROR: CEREBRAS_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    return key


def build_payload(messages: List[Dict[str, str]]) -> Dict:
    """Create the JSON payload expected by the Cerebras chat endpoint."""
    return {
        "model": MODEL_NAME,
        "messages": messages,
    }


def call_cerebras_chat(api_key: str, messages: List[Dict[str, str]]) -> str:
    """
    Send a chat request to Cerebras, handling rate‑limit (429) responses.

    Returns the assistant's content on success.
    On any non‑429 error the function prints the server response and exits.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = build_payload(messages)

    for attempt in range(1, MAX_RETRIES + 1):
        response = requests.post(API_ENDPOINT, headers=headers, json=payload, timeout=30)

        if response.status_code == 200:
            # Successful response – extract assistant message
            try:
                data = response.json()
                # The API returns a list under 'choices'; each has a 'message' dict
                assistant_msg = data["choices"][0]["message"]["content"]
                return assistant_msg.strip()
            except (KeyError, ValueError) as e:
                print(f"ERROR: Unexpected response format: {e}", file=sys.stderr)
                print("Response body:", response.text, file=sys.stderr)
                sys.exit(1)

        elif response.status_code == 429:
            # Rate limit / token quota exceeded – obey Retry-After if present
            retry_after = response.headers.get("Retry-After")
            wait_seconds = int(retry_after) if retry_after and retry_after.isdigit() else 60
            print(
                f"INFO: Rate limit hit (attempt {attempt}/{MAX_RETRIES}). "
                f"Waiting {wait_seconds}s before retry...",
                file=sys.stderr,
            )
            time.sleep(wait_seconds)
            continue  # retry

        else:
            # Any other error – surface it and abort
            print(
                f"ERROR: API returned non‑200 status code {response.status_code}: {response.text}",
                file=sys.stderr,
            )
            sys.exit(1)

    # If we exit the loop, retries were exhausted
    print(
        f"ERROR: Exceeded maximum retries ({MAX_RETRIES}) for rate‑limited request.",
        file=sys.stderr,
    )
    sys.exit(1)


def main() -> None:
    # ----------- Conversation setup ------------------------------------
    api_key = get_api_key()

    # Pre‑defined list of user messages (no interactive input)
    user_messages = [
        "Hello! How are you today?",
        "Tell me a short joke.",
        "What's the capital of France?",
        "Can you give me a brief summary of the plot of 'Inception'?",
    ]

    # Initialise the message list with a system prompt
    conversation: List[Dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]

    for user_msg in user_messages:
        # Append user's turn
        conversation.append({"role": "user", "content": user_msg})
        print(f"User: {user_msg}")

        # Call the API to get assistant's response
        assistant_reply = call_cerebras_chat(api_key, conversation)

        # Append assistant's response to the history for future context
        conversation.append({"role": "assistant", "content": assistant_reply})

        print(f"Assistant: {assistant_reply}\n")


if __name__ == "__main__":
    main()