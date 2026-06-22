import os
import requests

def read_file(filename):
    with open(filename, "r") as f:
        content = f.read()
    return content

api_key = os.environ["NVIDIA_API_KEY"]

file_content = read_file("notes.txt")
user_question = input("Ask me something: ")
full_message = "Here is some context from a file:\n" + file_content + "\n\nNow answer this question: " + user_question

print("DEBUG - full message being sent:", full_message)

response = requests.post(
    "https://integrate.api.nvidia.com/v1/chat/completions",
    headers={
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json"
    },
    json={
        "model": "meta/llama-3.1-8b-instruct",
        "messages": [
            {"role": "user", "content": full_message}
        ]
    }
)

data = response.json()
print(data["choices"][0]["message"]["content"])