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

def edit_file(filename, old_text, new_text):
    content = read_file(filename)
    if old_text not in content:
        return "Could not find that text in " + filename + ". No changes made."
    new_content = content.replace(old_text, new_text)
    write_file(filename, new_content)
    return "Replaced text in " + filename + " successfully."

def run_command(command):
    allowed_commands = ["ls", "pwd", "date", "whoami"]
    if command not in allowed_commands:
        return "That command is not allowed for safety reasons."
    result = subprocess.run(command, shell=True, capture_output=True, text=True)
    return result.stdout

def ask_ai(message):
    api_key = os.environ["CEREBRAS_API_KEY"]
    try:
        response = requests.post(
            "https://api.cerebras.ai/v1/chat/completions",
            headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-oss-120b",
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
- edit_file: changes one specific piece of text inside notes.txt to another, keeping the rest of the file the same
- run_command: runs a safe system command (date, files list, who am i)
- none: just answer directly, no tool needed

User question: """ + user_question + """

Reply with ONLY one word: read_file, write_file, edit_file, run_command, or none."""
    return ask_ai(prompt).strip().lower()

def get_edit_details(step, filename):
    current_content = read_file(filename)
    prompt = """Here is the EXACT current content of """ + filename + """:
---
""" + current_content + """
---

The user wants to make this change: """ + step + """

Look at the exact content above and find the smallest exact piece of text that needs to change.
Reply in EXACTLY this format, nothing else, using text copied exactly from the content above (no quotes around it):
OLD: <exact text from the file above>
NEW: <the replacement text>"""
    response = ask_ai(prompt)
    old_text = ""
    new_text = ""
    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("OLD:"):
            old_text = line[4:].strip().strip('"').strip("'")
        elif line.startswith("NEW:"):
            new_text = line[4:].strip().strip('"').strip("'")
    return old_text, new_text

def make_plan(goal):
    prompt = """Break this goal into the SMALLEST number of steps possible (usually 1-3, never more than 4).
Each step must be a complete, standalone action that fully achieves part of the goal -
do NOT separate "do X" from "report X", combine them into one step.

Goal: """ + goal + """

Reply with ONLY a numbered list, one step per line. No extra text."""
    plan_text = ask_ai(prompt)
    steps = []
    for line in plan_text.split("\n"):
        line = line.strip()
        if line and line[0].isdigit():
            cleaned = line.split(".", 1)[-1].strip()
            if cleaned:
                steps.append(cleaned)
    return steps

def looks_like_failure(result):
    failure_signals = ["error", "could not", "not allowed", "no changes made"]
    result_lower = result.lower()
    return any(signal in result_lower for signal in failure_signals)

def fix_failed_step(step, failure_reason):
    prompt = """This instruction failed: """ + step + """
The failure was: """ + failure_reason + """

Rewrite the instruction as a clearer, more specific single step that avoids this failure. Reply with ONLY the new instruction, nothing else."""
    return ask_ai(prompt).strip()

def handle_single_question(question, previous_results=""):
    question_lower = question.lower()

    write_keywords = ["write", "save", "create a file", "put this in a file"]
    needs_write = any(word in question_lower for word in write_keywords)

    edit_keywords = ["change", "replace", "edit", "update"]
    needs_edit = any(word in question_lower for word in edit_keywords)

    personal_keywords = ["favorite", "my color", "my project", "my name"]
    needs_file = any(word in question_lower for word in personal_keywords)

    command_keywords = ["what files", "list files", "what is the date", "who am i"]
    needs_command = any(word in question_lower for word in command_keywords)

    if needs_command:
        tool = "run_command"
    elif needs_edit:
        tool = "edit_file"
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
    elif "edit_file" in tool:
        old_text, new_text = get_edit_details(question, "notes.txt")
        if not old_text or not new_text:
            return "Could not figure out what to change. Try being more specific."
        return edit_file("notes.txt", old_text, new_text)
    elif "write_file" in tool:
        full_question = question
        if previous_results:
            full_question = "Context from earlier steps:\n" + previous_results + "\n\nTask: " + question
        answer = ask_ai(full_question)
        write_file("output.txt", answer)
        return "Wrote to output.txt:\n" + answer
    elif "read_file" in tool:
        file_content = read_file("notes.txt")
        final_prompt = "Here is context from a file:\n" + file_content + "\n\nNow answer this question: " + question
        return ask_ai(final_prompt)
    else:
        full_question = question
        if previous_results:
            full_question = "Context from earlier steps:\n" + previous_results + "\n\nTask: " + question
        return ask_ai(full_question)

def handle_step_with_retry(step, previous_results="", max_retries=2):
    current_step = step
    for attempt in range(max_retries + 1):
        result = handle_single_question(current_step, previous_results)
        if not looks_like_failure(result):
            return result, current_step
        if attempt < max_retries:
            print("  (Attempt " + str(attempt + 1) + " failed: " + result + ")")
            current_step = fix_failed_step(current_step, result)
            print("  (Retrying with: " + current_step + ")")
    return result, current_step

while True:
    user_question = input("Ask me something, type 'goal: ...', type 'edit: filename | old text | new text', or quit: ")

    if user_question.lower() == "quit":
        print("Goodbye!")
        break

    if user_question.lower().startswith("edit:"):
        parts = user_question[5:].split("|")
        if len(parts) != 3:
            print("Format must be: edit: filename | old text | new text")
        else:
            filename = parts[0].strip()
            old_text = parts[1].strip()
            new_text = parts[2].strip()
            result = edit_file(filename, old_text, new_text)
            print(result)
    elif user_question.lower().startswith("goal:"):
        goal = user_question[5:].strip()
        print("Planning steps for goal:", goal)
        steps = make_plan(goal)
        if not steps:
            print("Could not create a plan. Try rephrasing the goal.")
        else:
            previous_results = ""
            for i, step in enumerate(steps, start=1):
                print("Step " + str(i) + ": " + step)
                result, final_step = handle_step_with_retry(step, previous_results)
                print("Result:", result)
                previous_results = previous_results + "\nStep " + str(i) + " (" + final_step + "): " + result
                print("...")
        print("Goal complete!")
    else:
        answer, _ = handle_step_with_retry(user_question)
        print(answer)

    print("---")