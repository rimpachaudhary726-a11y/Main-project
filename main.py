import os
import requests

api_key = os.environ["NVIDIA_API_KEY"]

user_question = input("Ask me something: ")

response = requests.post(
    "https://integrate.api.nvidia.com/v1/chat/completions",
    headers={
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json"
    },
    json={
        "model": "meta/llama-3.1-8b-instruct",
        "messages": [
            {"role": "user", "content": user_question}
        ]
    }
)

data = response.json()
print(data["choices"][0]["message"]["content"])