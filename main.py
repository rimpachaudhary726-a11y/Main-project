import os
import requests

def read_file(filename):
    with open(filename, "r") as f:
        content = f.read()
    return content

def ask_ai(message):
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
                {"role": "user", "content": message}
            ]
        }
    )
    data = response.json()
    return data["choices"][0]["message"]["content"]

user_question = input("Ask me something: ")

decision_prompt = "There is a file called notes.txt with personal facts about the user (like their favorite color and project name). The user asked: '" + user_question + "'. If answering this requires knowing personal facts about the user, reply YES. Otherwise reply NO. Reply with only one word: YES or NO."
decision = ask_ai(decision_prompt)

print("DEBUG - AI decision:", decision)

if "YES" in decision.upper():
    file_content = read_file("notes.txt")
    final_prompt = "Here is context from a file:\n" + file_content + "\n\nNow answer this question: " + user_question
else:
    final_prompt = user_question

answer = ask_ai(final_prompt)
print(answer)