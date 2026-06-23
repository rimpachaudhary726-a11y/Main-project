import os
import subprocess
import requests

def read_file(filename):
    with open(filename, "r") as f:
        content = f.read()
    return content

def write_file(filename, content):
    with open(filename, "w") as f:
        f.write(content)

def run_command(command):
    allowed_commands = ["ls", "pwd", "date", "whoami"]
    if command not in allowed_commands:
        return "That command is not allowed for safety reasons."
    result = subprocess.run(command, shell=True, capture_output=True, text=True)
    return result.stdout

def ask_ai(message):
    api_key = os.environ["NVIDIA_API_KEY"]
    try:
        response = requests.post(
            "https://integrate.api.nvidia.com/v1/chat/completions",
            headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type": "application/json"
            },
            json={
                "model": "z-ai/glm-5.1",
                "messages": [
                    {"role": "user", "content": message}
                ]
            },
            timeout=30
        )
    except requests.exceptions.RequestException as e:
        return "ERROR: could not reach the AI service (" + str(e) + ")"

    data = response.json()
    if "choices" not in data:
        return "ERROR from API: " + str(data)
    return data["choices"][0]["message"]["content"]

def decide_tool(user_question):
    prompt = """You are an assistant with these tools:
- read_file: reads notes.txt for personal info (favorite color, project name)
- write_file: writes AI-generated text to output.txt
- run_command: runs a safe system command (date, files list, who am i)
- none: just answer directly, no tool needed

User question: """ + user_question + """

Reply with ONLY one word: read_file, write_file, run_command, or none."""
    return ask_ai(prompt).strip().lower()

while True:
    user_question = input("Ask me something (or type quit): ")

    if user_question.lower() == "quit":
        print("Goodbye!")
        break

    tool = decide_tool(user_question)
    print("DEBUG - tool chosen:", repr(tool))

    if "run_command" in tool:
        if "date" in user_question.lower():
            output = run_command("date")
        elif "who" in user_question.lower():
            output = run_command("whoami")
        else:
            output = run_command("ls")
        print("Command output:", output)
    elif "write_file" in tool:
        answer = ask_ai(user_question)
        write_file("output.txt", answer)
        print("Done! I wrote this to output.txt:")
        print(answer)
    elif "read_file" in tool:
        file_content = read_file("notes.txt")
        final_prompt = "Here is context from a file:\n" + file_content + "\n\nNow answer this question: " + user_question
        answer = ask_ai(final_prompt)
        print(answer)
    else:
        answer = ask_ai(user_question)
        print(answer)

    print("---")