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

def append_to_file(filename, content):
    with open(filename, "a") as f:
        f.write(content)

def edit_file(filename, old_text, new_text):
    content = read_file(filename)
    if old_text not in content:
        return "Could not find that text in " + filename + ". No changes made."
    new_content = content.replace(old_text, new_text)
    write_file(filename, new_content)
    return "Replaced text in " + filename + " successfully."

def list_directory():
    files = os.listdir(".")
    text_files = [f for f in files if os.path.isfile(f)]
    return ", ".join(text_files)

def search_files(search_term):
    matches = []
    for filename in os.listdir("."):
        if os.path.isfile(filename) and filename.endswith(".txt"):
            try:
                content = read_file(filename)
                if search_term.lower() in content.lower():
                    matches.append(filename)
            except Exception:
                pass
    if not matches:
        return "No files found containing '" + search_term + "'."
    return "Found '" + search_term + "' in: " + ", ".join(matches)

def search_web(query):
    try:
        response = requests.post(
            "https://api.firecrawl.dev/v2/search",
            headers={"Content-Type": "application/json"},
            json={"query": query, "limit": 3},
            timeout=30
        )
        data = response.json()
        if not data.get("success"):
            return "Web search failed: " + str(data)
        results = data.get("data", {}).get("web", [])
        if not results:
            return "No web results found."
        summary = ""
        for r in results:
            summary = summary + "- " + r.get("title", "") + ": " + r.get("description", "") + " (" + r.get("url", "") + ")\n"
        return summary
    except requests.exceptions.RequestException as e:
        return "ERROR: could not reach web search (" + str(e) + ")"

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

def load_history():
    if not os.path.exists("history.txt"):
        return ""
    return read_file("history.txt")

def save_to_history(question, answer):
    entry = "\nUser asked: " + question + "\nAssistant answered: " + answer + "\n"
    append_to_file("history.txt", entry)

def decide_tool(user_question):
    prompt = """You are an assistant with these tools:
- read_file: reads a file mentioned by the user
- write_file: writes AI-generated text to a file
- edit_file: changes one specific piece of text inside a file, keeping the rest the same
- list_directory: lists all files that currently exist
- search_files: searches the content of all .txt files for a specific word or phrase
- search_web: searches the live internet for current information
- run_command: runs a safe system command (date, files list, who am i)
- none: just answer directly, no tool needed

User question: """ + user_question + """

Reply with ONLY one word: read_file, write_file, edit_file, list_directory, search_files, search_web, run_command, or none."""
    return ask_ai(prompt).strip().lower()

def extract_filename(question, default):
    prompt = """The user said: """ + question + """

Does this mention a specific filename (like notes.txt, data.csv, story.txt, etc.)?
If yes, reply with ONLY that filename, nothing else.
If no specific filename is mentioned, reply with ONLY the word: """ + default
    response = ask_ai(prompt).strip()
    response = response.strip('"').strip("'").strip()
    if " " in response or len(response) > 50:
        return default
    return response

def extract_search_term(question):
    prompt = """The user said: """ + question + """

What word or phrase are they trying to search for? Reply with ONLY that word or phrase, nothing else."""
    return ask_ai(prompt).strip().strip('"').strip("'")

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
    failure_signals = ["error", "could not", "not allowed", "no changes made", "no such file"]
    result_lower = result.lower()
    return any(signal in result_lower for signal in failure_signals)

def fix_failed_step(step, failure_reason):
    prompt = """This instruction failed: """ + step + """
The failure was: """ + failure_reason + """

Rewrite the instruction as a clearer, more specific single step that avoids this failure. Reply with ONLY the new instruction, nothing else."""
    return ask_ai(prompt).strip()

def handle_single_question(question, previous_results="", history=""):
    question_lower = question.lower()

    write_keywords = ["write", "save", "create a file", "put this in a file"]
    needs_write = any(word in question_lower for word in write_keywords)

    edit_keywords = ["change", "replace", "edit", "update"]
    needs_edit = any(word in question_lower for word in edit_keywords)

    personal_keywords = ["favorite", "my color", "my project", "my name"]
    needs_file = any(word in question_lower for word in personal_keywords)

    command_keywords = ["what is the date", "who am i"]
    needs_command = any(word in question_lower for word in command_keywords)

    list_keywords = ["what files", "list files", "list all files", "show files", "what files exist"]
    needs_list = any(word in question_lower for word in list_keywords)

    search_file_keywords = ["search for", "find files with", "which file has", "which file contains"]
    needs_search_files = any(word in question_lower for word in search_file_keywords)

    web_keywords = ["search the web", "look up", "google", "what is happening", "latest news", "current price"]
    needs_web = any(word in question_lower for word in web_keywords)

    memory_keywords = ["earlier", "before", "previously", "remember", "did i ask", "did i say"]
    needs_memory = any(word in question_lower for word in memory_keywords)

    if needs_web:
        tool = "search_web"
    elif needs_search_files:
        tool = "search_files"
    elif needs_list:
        tool = "list_directory"
    elif needs_command:
        tool = "run_command"
    elif needs_edit:
        tool = "edit_file"
    elif needs_write:
        tool = "write_file"
    elif needs_memory:
        tool = "memory"
    elif needs_file:
        tool = "read_file"
    else:
        tool = decide_tool(question)

    if "search_web" in tool:
        return search_web(question)
    elif "search_files" in tool:
        search_term = extract_search_term(question)
        return search_files(search_term)
    elif "list_directory" in tool:
        return "Files: " + list_directory()
    elif "run_command" in tool:
        if "date" in question_lower:
            return run_command("date")
        elif "who" in question_lower:
            return run_command("whoami")
        else:
            return run_command("ls")
    elif "memory" in tool:
        if not history:
            return "I don't have any earlier conversation history yet."
        final_prompt = "Here is our earlier conversation history:\n" + history + "\n\nNow answer this question: " + question
        return ask_ai(final_prompt)
    elif "edit_file" in tool:
        filename = extract_filename(question, "notes.txt")
        if not os.path.exists(filename):
            return "No such file: " + filename
        old_text, new_text = get_edit_details(question, filename)
        if not old_text or not new_text:
            return "Could not figure out what to change. Try being more specific."
        return edit_file(filename, old_text, new_text)
    elif "write_file" in tool:
        filename = extract_filename(question, "output.txt")
        full_question = question
        if previous_results:
            full_question = "Context from earlier steps:\n" + previous_results + "\n\nTask: " + question
        answer = ask_ai(full_question)
        write_file(filename, answer)
        return "Wrote to " + filename + ":\n" + answer
    elif "read_file" in tool:
        filename = extract_filename(question, "notes.txt")
        if not os.path.exists(filename):
            return "No such file: " + filename
        file_content = read_file(filename)
        final_prompt = "Here is context from a file:\n" + file_content + "\n\nNow answer this question: " + question
        return ask_ai(final_prompt)
    else:
        full_question = question
        if previous_results:
            full_question = "Context from earlier steps:\n" + previous_results + "\n\nTask: " + question
        return ask_ai(full_question)

def handle_step_with_retry(step, previous_results="", history="", max_retries=2):
    current_step = step
    for attempt in range(max_retries + 1):
        result = handle_single_question(current_step, previous_results, history)
        if not looks_like_failure(result):
            return result, current_step
        if attempt < max_retries:
            print("  (Attempt " + str(attempt + 1) + " failed: " + result + ")")
            current_step = fix_failed_step(current_step, result)
            print("  (Retrying with: " + current_step + ")")
    return result, current_step

conversation_history = load_history()
if conversation_history:
    print("Loaded previous conversation history.")

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
                result, final_step = handle_step_with_retry(step, previous_results, conversation_history)
                print("Result:", result)
                previous_results = previous_results + "\nStep " + str(i) + " (" + final_step + "): " + result
                print("...")
        print("Goal complete!")
        save_to_history(goal, previous_results)
        conversation_history = conversation_history + previous_results
    else:
        answer, _ = handle_step_with_retry(user_question, "", conversation_history)
        print(answer)
        save_to_history(user_question, answer)
        conversation_history = conversation_history + "\nUser asked: " + user_question + "\nAssistant answered: " + answer + "\n"

    print("---")