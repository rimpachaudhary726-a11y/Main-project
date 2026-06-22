import os
import requests

api_key = os.environ["NVIDIA_API_KEY"]

response = requests.post(
    "https://integrate.api.nvidia.com/v1/chat/completions",
    headers={
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json"
    },
    json={
        "model": "meta/llama-3.1-8b-instruct",
        "messages": [
            {"role": "user", "content": "Say hello in one short sentence."}
        ]
    }
)

data = response.json()
print(data["choices"][0]["message"]["content"])