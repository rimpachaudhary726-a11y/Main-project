import os
import requests

def read_file(filename):
    with open(filename, "r") as f:
        content = f.read()
    return content

def write_file(filename, content):
    with open(filename, "w") as f:
        f.write(content)

def ask_ai(message):
    api_key = os.environ["NVIDIA_API_KEY"]
    response = requests.post(
        "https://integrate.api.nvidia.com/v1/chat/completions",
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json"
        },
        json={
            "model": "deepseek-ai/deepseek-v4-flash",
            "messages": [
                {"role": "user", "content": message}
            ]
        }
    )
    data = response.json()
    return data["choices"][0]["message"]["content"]

while True:
    user_question = input("Ask me something (or type quit): ")

    if user_question.lower() == "quit":
        print("Goodbye!")
        break

    write_keywords = ["write", "save", "create a file", "put this in a file"]
    needs_write = any(word in user_question.lower() for word in write_keywords)

    personal_keywords = ["favorite", "my color", "my project", "my name"]
    needs_file = any(word in user_question.lower() for word in personal_keywords)

    if needs_write:
        answer = ask_ai(user_question)
        write_file("output.txt", answer)
        print("Done! I wrote this to output.txt:")
        print(answer)
    else:
        if needs_file:
            file_content = read_file("notes.txt")
            final_prompt = "Here is context from a file:\n" + file_content + "\n\nNow answer this question: " + user_question
        else:
            final_prompt = user_question
        answer = ask_ai(final_prompt)
        print(answer)

    print("---")