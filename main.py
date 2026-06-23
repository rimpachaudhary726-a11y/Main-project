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
                "model": "deepseek-ai/deepseek-v4-flash",
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

def make_plan(goal):
    prompt = """Break this goal into 2-4 short, numbered steps. Each step should be something simple like answering a question, writing a file, or running a command.
Goal: """ + goal + """

Reply with ONLY a numbered list, one short step per line. No extra text."""
    plan_text = ask_ai(prompt)
    steps = []
    for line in plan_text.split("\n"):
        line = line.strip()
        if line and line[0].isdigit():
            cleaned = line.split(".", 1)[-1].strip()
            if cleaned:
                steps.append(cleaned)
    return steps

def handle_single_question(question):
    question_lower = question.lower()

    write_keywords = ["write", "save", "create a file", "put this in a file"]
    needs_write = any(word in question_lower for word in write_keywords)

    personal_keywords = ["favorite", "my color", "my project", "my name"]
    needs_file = any(word in question_lower for word in personal_keywords)

    command_keywords = ["what files", "list files", "what is the date", "who am i"]
    needs_command = any(word in question_lower for word in command_keywords)

    if needs_command:
        tool = "run_command"
    elif needs_write:
        tool = "write_file"
    elif needs_file:
        tool = "read_file"
    else:
        tool = decide_tool(question)

    if "run_command" in tool:
        if "date" in question_lower:
            return run_command("date")
        elif "who" in question_lower:
            return run_command("whoami")
        else:
            return run_command("ls")
    elif "write_file" in tool:
        answer = ask_ai(question)
        write_file("output.txt", answer)
        return "Wrote to output.txt:\n" + answer
    elif "read_file" in tool:
        file_content = read_file("notes.txt")
        final_prompt = "Here is context from a file:\n" + file_content + "\n\nNow answer this question: " + question
        return ask_ai(final_prompt)
    else:
        return ask_ai(question)

while True:
    user_question = input("Ask me something, or type 'goal: ...' for multi-step (or quit): ")

    if user_question.lower() == "quit":
        print("Goodbye!")
        break

    if user_question.lower().startswith("goal:"):
        goal = user_question[5:].strip()
        print("Planning steps for goal:", goal)
        steps = make_plan(goal)
        if not steps:
            print("Could not create a plan. Try rephrasing the goal.")
        else:
            for i, step in enumerate(steps, start=1):
                print("Step " + str(i) + ": " + step)
                result = handle_single_question(step)
                print("Result:", result)
                print("...")
        print("Goal complete!")
    else:
        answer = handle_single_question(user_question)
        print(answer)

    print("---")