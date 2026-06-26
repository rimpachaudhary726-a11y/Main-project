#!/usr/bin/env python3
"""
A simple chatbot script that conducts a multi‑turn conversation using the
Cerebras chat completion API. The conversation is driven by a predefined list
of user messages; no interactive input is required.
"""

import os
import sys
import json
import requests

API_ENDPOINT = "https://api.cerebras.ai/v1/chat/completions"
MODEL_NAME = "gpt-oss-120b"


def get_api_key() -> str:
    """Retrieve the Cerebras API key from the environment."""
    key = os.environ.get("CEREBRAS_API_KEY")
    if not key:
        print("Error: CEREBRAS_API_KEY environment variable not set", file=sys.stderr)
        sys.exit(1)
    return key


def call_cerebras_chat(api_key: str, messages: list) -> str:
    """
    Send a chat completion request to Cerebras and return the assistant's reply.

    Parameters
    ----------
    api_key : str
        Authorization token.
    messages : list
        List of message dicts following the OpenAI schema.

    Returns
    -------
    str
        The assistant's response content.

    Raises
    ------
    SystemExit
        If the request fails or the response is malformed.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
    }

    try:
        response = requests.post(API_ENDPOINT, headers=headers, json=payload, timeout=30)
    except Exception as e:
        print(f"Network error when contacting Cerebras API: {e}", file=sys.stderr)
        sys.exit(1)

    if response.status_code != 200:
        try:
            err_detail = response.json()
        except Exception:
            err_detail = response.text
        print(
            f"API error {response.status_code}: {err_detail}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        data = response.json()
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        print(f"Malformed API response: {e}", file=sys.stderr)
        sys.exit(1)

    return content


def main():
    api_key = get_api_key()

    # Initial system prompt to set the assistant's behavior (optional)
    conversation = [
        {
            "role": "system",
            "content": "You are a helpful AI assistant. Keep responses concise.",
        }
    ]

    # Pre‑defined user messages for the multi‑turn dialogue
    user_messages = [
        "Hello! Who are you?",
        "Can you tell me a short joke?",
        "Thanks! What's the weather like today in Paris?",
    ]

    for idx, user_text in enumerate(user_messages, start=1):
        # Append the user's message to the conversation history
        conversation.append({"role": "user", "content": user_text})

        # Call the API to get the assistant's reply
        assistant_reply = call_cerebras_chat(api_key, conversation)

        # Append the assistant's reply to maintain context for subsequent turns
        conversation.append({"role": "assistant", "content": assistant_reply})

        # Output the turn
        print(f"Turn {idx} - User: {user_text}")
        print(f"Turn {idx} - Assistant: {assistant_reply}")
        print("-" * 40)


if __name__ == "__main__":
    main()