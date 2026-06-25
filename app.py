#!/usr/bin/env python3
"""
Standalone chatbot script using the Cerebras API.

It reads the entire stdin as the user's prompt, sends it to the Cerebras
chat endpoint, and prints the model's response to stdout.

The script exits with a non‑zero status if:
- CEREBRAS_API_KEY is missing
- No input is provided
- The HTTP request fails or returns a non‑200 status
- The response payload is malformed
"""

import json
import os
import sys
from typing import Any, Dict

try:
    import requests
except ImportError as e:
    print("ERROR: Required package 'requests' is not installed.", file=sys.stderr)
    raise e


API_ENDPOINT = "https://api.cerebras.ai/v1/chat/completions"
MODEL_NAME = "gpt-oss-120b"


def get_api_key() -> str:
    """
    Retrieve the Cerebras API key from the environment.
    Exit with an error if it is not set.
    """
    api_key = os.getenv("CEREBRAS_API_KEY")
    if not api_key:
        print(
            "ERROR: Environment variable CEREBRAS_API_KEY is not set.",
            file=sys.stderr,
        )
        sys.exit(1)
    return api_key


def read_prompt() -> str:
    """
    Read the entire stdin as a single prompt string.
    Exit with an error if stdin is empty.
    """
    prompt = sys.stdin.read()
    if not prompt.strip():
        print("ERROR: No input provided to the chatbot.", file=sys.stderr)
        sys.exit(1)
    return prompt.strip()


def build_payload(prompt: str) -> Dict[str, Any]:
    """
    Construct the JSON payload expected by the Cerebras chat API.
    """
    return {
        "model": MODEL_NAME,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        # Optional parameters – adjust as needed
        "max_tokens": 512,
        "temperature": 0.7,
    }


def call_cerebras_api(api_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Send a POST request to the Cerebras chat endpoint.
    Returns the parsed JSON response on success.
    Exits with an error on network failure or non‑200 status.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            API_ENDPOINT,
            headers=headers,
            json=payload,
            timeout=30,
        )
    except requests.RequestException as exc:
        print(f"ERROR: Network request failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if not response.ok:
        # Attempt to include any error details returned by the API
        try:
            error_detail = response.json()
        except Exception:
            error_detail = response.text
        print(
            f"ERROR: API request failed with status {response.status_code}: {error_detail}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        return response.json()
    except json.JSONDecodeError as exc:
        print(f"ERROR: Failed to parse JSON response: {exc}", file=sys.stderr)
        sys.exit(1)


def extract_reply(api_response: Dict[str, Any]) -> str:
    """
    Extract the assistant's reply from the API response.
    Exits with an error if the expected fields are missing.
    """
    try:
        # According to the OpenAI‑compatible schema:
        # {"choices": [{"message": {"role": "assistant", "content": "..."}}, ...]}
        choice = api_response["choices"][0]
        message = choice["message"]
        content = message["content"]
        return content.strip()
    except (KeyError, IndexError) as exc:
        print(
            f"ERROR: Unexpected API response format: missing field {exc}",
            file=sys.stderr,
        )
        sys.exit(1)


def main() -> None:
    api_key = get_api_key()
    user_prompt = read_prompt()
    payload = build_payload(user_prompt)
    response_json = call_cerebras_api(api_key, payload)
    reply = extract_reply(response_json)
    print(reply)


if __name__ == "__main__":
    main()