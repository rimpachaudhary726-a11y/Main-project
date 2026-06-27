import os
import subprocess
import re
import time
import zipfile
import io
import base64
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

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

def fetch_url(url):
    try:
        response = requests.post(
            "https://api.firecrawl.dev/v2/scrape",
            headers={"Content-Type": "application/json"},
            json={"url": url, "formats": ["markdown"]},
            timeout=30
        )
        data = response.json()
        if not data.get("success"):
            return "Could not fetch that page: " + str(data)
        content = data.get("data", {}).get("markdown", "")
        if not content:
            return "Page fetched but no content found."
        return content[:3000]
    except requests.exceptions.RequestException as e:
        return "ERROR: could not reach the page (" + str(e) + ")"

def run_command(command):
    allowed_commands = ["ls", "pwd", "date", "whoami"]
    if command not in allowed_commands:
        return "That command is not allowed for safety reasons."
    result = subprocess.run(command, shell=True, capture_output=True, text=True)
    return result.stdout

def calculate(expression):
    allowed_chars = "0123456789+-*/(). "
    if not all(c in allowed_chars for c in expression):
        return "Invalid characters in expression. Only numbers and + - * / ( ) are allowed."
    try:
        result = eval(expression)
        return "Result: " + str(result)
    except Exception as e:
        return "Could not calculate: " + str(e)

def github_headers():
    token = os.environ["GITHUB_TOKEN"]
    return {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json"
    }

def get_github_username():
    try:
        response = requests.get(
            "https://api.github.com/user",
            headers=github_headers(),
            timeout=30
        )
        data = response.json()
        return data.get("login", "")
    except requests.exceptions.RequestException:
        return ""

def create_github_repo(repo_name, description="Created by my agent", private=False):
    try:
        response = requests.post(
            "https://api.github.com/user/repos",
            headers=github_headers(),
            json={
                "name": repo_name,
                "description": description,
                "private": private
            },
            timeout=30
        )
        data = response.json()
        if response.status_code not in [200, 201]:
            return "Could not create repo: " + str(data.get("message", data))
        return "Created repo: " + data.get("html_url", repo_name)
    except requests.exceptions.RequestException as e:
        return "ERROR: could not reach GitHub (" + str(e) + ")"

def get_github_file_sha(repo_name, file_path):
    username = get_github_username()
    if not username:
        return None
    try:
        response = requests.get(
            "https://api.github.com/repos/" + username + "/" + repo_name + "/contents/" + file_path,
            headers=github_headers(),
            timeout=30
        )
        if response.status_code == 200:
            return response.json().get("sha")
    except requests.exceptions.RequestException:
        pass
    return None

def create_github_file(repo_name, file_path, content, commit_message="Added by my agent"):
    username = get_github_username()
    if not username:
        return "Could not determine GitHub username."
    encoded_content = base64.b64encode(content.encode()).decode()
    body = {"message": commit_message, "content": encoded_content}
    sha = get_github_file_sha(repo_name, file_path)
    if sha:
        body["sha"] = sha
    try:
        response = requests.put(
            "https://api.github.com/repos/" + username + "/" + repo_name + "/contents/" + file_path,
            headers=github_headers(),
            json=body,
            timeout=30
        )
        data = response.json()
        if response.status_code not in [200, 201]:
            return "Could not create file: " + str(data.get("message", data))
        return "Created file " + file_path + " in " + repo_name
    except requests.exceptions.RequestException as e:
        return "ERROR: could not reach GitHub (" + str(e) + ")"

def trigger_github_workflow(repo_name, workflow_filename="run.yml"):
    username = get_github_username()
    url = "https://api.github.com/repos/" + username + "/" + repo_name + "/actions/workflows/" + workflow_filename + "/dispatches"
    try:
        response = requests.post(url, headers=github_headers(), json={"ref": "main"}, timeout=20)
    except requests.exceptions.RequestException as e:
        return False, "ERROR: could not reach GitHub (" + str(e) + ")"
    if response.status_code == 204:
        return True, "Triggered."
    return False, "Failed to trigger run (" + str(response.status_code) + "): " + response.text

def get_latest_run(repo_name, workflow_filename="run.yml"):
    username = get_github_username()
    url = "https://api.github.com/repos/" + username + "/" + repo_name + "/actions/workflows/" + workflow_filename + "/runs"
    try:
        response = requests.get(url, headers=github_headers(), params={"per_page": 1}, timeout=20)
        runs = response.json().get("workflow_runs", [])
        if runs:
            return runs[0]
    except requests.exceptions.RequestException:
        pass
    return None

def wait_for_run_completion(repo_name, previous_run_id=None, workflow_filename="run.yml", timeout_seconds=300):
    waited = 0
    run = None
    while waited < timeout_seconds:
        time.sleep(10)
        waited += 10
        run = get_latest_run(repo_name, workflow_filename)
        if run and run.get("id") != previous_run_id and run.get("status") == "completed":
            return run
    return run

def get_run_logs(repo_name, run_id):
    username = get_github_username()
    url = "https://api.github.com/repos/" + username + "/" + repo_name + "/actions/runs/" + str(run_id) + "/logs"
    try:
        response = requests.get(url, headers=github_headers(), timeout=30)
        if response.status_code != 200:
            return "Could not fetch logs (" + str(response.status_code) + ")"
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            all_text = ""
            for name in z.namelist():
                if name.endswith(".txt"):
                    all_text += z.read(name).decode("utf-8", errors="ignore") + "\n"
            return all_text
    except Exception as e:
        return "ERROR reading logs: " + str(e)

CEREBRAS_KEYS = [os.environ["CEREBRAS_API_KEY"]]
_second_key = os.environ.get("CEREBRAS_API_KEY_2")
if _second_key:
    CEREBRAS_KEYS.append(_second_key)

def get_key_for_index(i):
    return CEREBRAS_KEYS[i % len(CEREBRAS_KEYS)]

_thread_local = threading.local()

def ask_ai(message):
    api_key = getattr(_thread_local, "api_key", None) or CEREBRAS_KEYS[0]
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

def summarize_url(url):
    content = fetch_url(url)
    if content.startswith("ERROR") or content.startswith("Could not"):
        return content
    prompt = "Summarize this web page content in 3-4 sentences:\n\n" + content
    return ask_ai(prompt)

MEMORY_FILE = "agent_memory.txt"

def load_memory():
    if not os.path.exists(MEMORY_FILE):
        return ""
    return read_file(MEMORY_FILE)

def with_memory(prompt):
    memory = load_memory()
    if not memory:
        return prompt
    return "What you remember about this user and project so far:\n" + memory + "\n\n" + prompt

def update_memory(interaction_summary):
    current_memory = load_memory()
    prompt = """Here is what this agent currently remembers about the user and how they like things done:
""" + (current_memory if current_memory else "(nothing yet)") + """

Here is a new interaction to learn from:
""" + interaction_summary + """

Update the memory: keep anything still useful, add any new lasting facts, preferences, or
lessons about how this user likes the agent to behave. Keep it a short bullet list, max 25
bullets, most important first. Reply with ONLY the updated bullet list, nothing else."""
    updated = ask_ai(prompt)
    write_file(MEMORY_FILE, updated.strip())
    return updated

def remember_note(note):
    current_memory = load_memory()
    new_memory = (current_memory + "\n- " + note).strip() if current_memory else "- " + note
    write_file(MEMORY_FILE, new_memory)
    return "Remembered: " + note

LESSONS_FILE = "agent_lessons.txt"

def load_lessons():
    if not os.path.exists(LESSONS_FILE):
        return ""
    return read_file(LESSONS_FILE)

def reflect_on_failure(idea, error_output):
    current_lessons = load_lessons()
    prompt = """An attempt to build this idea failed: """ + idea + """

The error was:
""" + error_output[:1500] + """

Lessons already learned from past failures:
""" + (current_lessons if current_lessons else "(none yet)") + """

Extract ONE short, general, reusable lesson from this specific failure - something that
would help avoid this entire CLASS of mistake in future, unrelated coding tasks (not specific
to this one idea). If this lesson is basically a duplicate of one already listed above, reply
with exactly: DUPLICATE
Otherwise reply with ONLY the new lesson as a single bullet line, nothing else."""
    lesson = ask_ai(prompt).strip()
    if lesson and lesson.upper() != "DUPLICATE":
        updated = (current_lessons + "\n- " + lesson).strip() if current_lessons else "- " + lesson
        write_file(LESSONS_FILE, updated)
        return lesson
    return None

GENERATED_CODE_FILE = "generated_script.py"
MAX_CODE_ATTEMPTS = 3
CODE_TIMEOUT_SECONDS = 10

def write_code(idea, previous_error=""):
    if previous_error:
        prompt = """Write a complete, standalone Python script for this idea: """ + idea + """

A previous attempt failed with this error:
""" + previous_error + """

Fix the code so it works correctly. Reply with ONLY the Python code, no explanations, no markdown code fences."""
    else:
        prompt = """Write a complete, standalone Python script for this idea: """ + idea + """

Use only Python's built-in libraries (no external packages). Keep it simple and safe.
Reply with ONLY the Python code, no explanations, no markdown code fences."""
    code = ask_ai(prompt)
    code = code.strip()
    if code.startswith("```"):
        code = code.split("\n", 1)[1] if "\n" in code else code
        if code.endswith("```"):
            code = code.rsplit("```", 1)[0]
    write_file(GENERATED_CODE_FILE, code.strip())
    return code

def run_generated_code():
    try:
        result = subprocess.run(
            ["python3", GENERATED_CODE_FILE],
            capture_output=True,
            text=True,
            timeout=CODE_TIMEOUT_SECONDS
        )
        if result.returncode != 0:
            return False, result.stderr
        return True, result.stdout
    except subprocess.TimeoutExpired:
        return False, "Code took too long to run (timeout of " + str(CODE_TIMEOUT_SECONDS) + " seconds)."

def build_and_fix_workflow(idea):
    print("Building code for: " + idea)
    code = write_code(idea)
    for attempt in range(1, MAX_CODE_ATTEMPTS + 1):
        print("--- Attempt " + str(attempt) + " ---")
        print("Running generated code...")
        success, output = run_generated_code()
        if success:
            print("Success! Output:")
            print(output)
            return "Workflow built and ran successfully after " + str(attempt) + " attempt(s)."
        else:
            print("Failed with error:")
            print(output)
            if attempt < MAX_CODE_ATTEMPTS:
                print("Asking AI to fix the code...")
                code = write_code(idea, previous_error=output)
            else:
                return "Could not get the workflow working after " + str(MAX_CODE_ATTEMPTS) + " attempts. Last error:\n" + output
    return "Unexpected end of retry loop."

GITHUB_CODE_FILE = "app.py"
GITHUB_REQUIREMENTS_FILE = "requirements.txt"
MAX_GITHUB_ATTEMPTS = 4

def write_code_for_github(idea, previous_error=""):
    lessons = load_lessons()
    lessons_block = ("\n\nLESSONS LEARNED FROM PAST FAILURES (apply these too):\n" + lessons) if lessons else ""

    base_rules = """The script must run end-to-end with no manual input required (don't use
input(), since this runs non-interactively on GitHub Actions). External packages are fine if
needed (e.g. requests). If the script needs an API key, read it with
os.environ.get('CEREBRAS_API_KEY') — never hardcode any key.

CRITICAL RULES — do not violate these:
- Do NOT add silent fallback behavior that catches an error and returns a fake/placeholder
  response instead of the real result. If something fails (missing key, bad API response,
  wrong model name, network error), print the real error and exit with sys.exit(1) so the
  failure is visible, not hidden behind a fake success.
- Do NOT guess or invent an API model name or endpoint. If the idea needs the Cerebras API,
  use exactly this:
  endpoint = "https://api.cerebras.ai/v1/chat/completions"
  model = "gpt-oss-120b"
- Never wrap the core logic in a broad try/except that swallows the error and substitutes
  unrelated dummy output. A failed run must look like a failed run.""" + lessons_block

    if previous_error:
        prompt = """Write a complete, standalone Python script for this idea: """ + idea + """

A previous attempt failed with this error when run on GitHub Actions:
""" + previous_error + """

Fix the ROOT CAUSE of this exact error so the script actually works — do not just catch the
error and return a fake/fallback response instead. """ + base_rules + """
Reply in EXACTLY this format, nothing else:
CODE:
<the full python code>
REQUIREMENTS:
<one pip package per line, or NONE if no external packages are needed>"""
    else:
        prompt = """Write a complete, standalone Python script for this idea: """ + idea + """

""" + base_rules + """
Reply in EXACTLY this format, nothing else:
CODE:
<the full python code>
REQUIREMENTS:
<one pip package per line, or NONE if no external packages are needed>"""

    prompt = with_memory(prompt)

    response = ask_ai(prompt)
    retry_count = 0
    while response.startswith("ERROR") and retry_count < 2:
        time.sleep(5)
        response = ask_ai(prompt)
        retry_count += 1

    if "REQUIREMENTS:" in response:
        code_part, req_part = response.split("REQUIREMENTS:", 1)
        code = code_part.replace("CODE:", "").strip()
        requirements = req_part.strip()
    else:
        code = response.replace("CODE:", "").strip()
        requirements = ""

    if code.startswith("```"):
        code = code.split("\n", 1)[1] if "\n" in code else code
        if code.endswith("```"):
            code = code.rsplit("```", 1)[0]

    write_file(GITHUB_CODE_FILE, code.strip())

    cleaned_lines = []
    for line in requirements.split("\n"):
        line = line.strip().strip("`").strip()
        if line and line.upper() != "NONE":
            cleaned_lines.append(line)
    requirements = "\n".join(cleaned_lines)

    if requirements:
        write_file(GITHUB_REQUIREMENTS_FILE, requirements)
    elif os.path.exists(GITHUB_REQUIREMENTS_FILE):
        os.remove(GITHUB_REQUIREMENTS_FILE)
    return code

def ensure_python_runner_yaml(repo_name):
    yaml_content = """name: Run Generated Code

on:
  workflow_dispatch:

jobs:
  run:
    runs-on: ubuntu-22.04
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Install requirements
        run: |
          if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
      - name: Run script
        env:
          CEREBRAS_API_KEY: ${{ secrets.CEREBRAS_API_KEY }}
        run: python """ + GITHUB_CODE_FILE + """
"""
    return create_github_file(repo_name, ".github/workflows/run.yml", yaml_content, "Add code runner workflow")

def validate_python_on_github(repo_name):
    print(create_github_repo(repo_name))

    code_content = read_file(GITHUB_CODE_FILE)
    push_result = create_github_file(repo_name, GITHUB_CODE_FILE, code_content, "Update app.py")
    print(push_result)
    if push_result.startswith("ERROR") or push_result.startswith("Could not"):
        return False, push_result

    if os.path.exists(GITHUB_REQUIREMENTS_FILE):
        req_content = read_file(GITHUB_REQUIREMENTS_FILE)
        print(create_github_file(repo_name, GITHUB_REQUIREMENTS_FILE, req_content, "Update requirements.txt"))

    print(ensure_python_runner_yaml(repo_name))
    time.sleep(5)

    previous_run = get_latest_run(repo_name, "run.yml")
    previous_run_id = previous_run.get("id") if previous_run else None

    triggered, msg = trigger_github_workflow(repo_name, "run.yml")
    if not triggered:
        return False, msg

    print("Waiting for GitHub Actions to run your code (can take a minute or two)...")
    run = wait_for_run_completion(repo_name, previous_run_id=previous_run_id, workflow_filename="run.yml")
    if not run or run.get("id") == previous_run_id:
        return False, "Run did not complete within the timeout."

    if run.get("conclusion") == "success":
        return True, "Ran successfully on GitHub Actions."
    logs = get_run_logs(repo_name, run.get("id"))
    return False, logs[-3000:]

def is_missing_secret_error(log_text):
    text_lower = log_text.lower()
    if "environment variable" in text_lower and "not set" in text_lower:
        return True
    if "keyerror" in text_lower and ("api_key" in text_lower or "token" in text_lower):
        return True
    return False

def build_and_fix_on_github(idea, repo_name):
    print("Building: " + idea)
    _thread_local.api_key = get_key_for_index(0)
    write_code_for_github(idea)
    for attempt in range(1, MAX_GITHUB_ATTEMPTS + 1):
        _thread_local.api_key = get_key_for_index(attempt)
        print("--- Attempt " + str(attempt) + " --- (using API key #" + str((attempt % len(CEREBRAS_KEYS)) + 1) + ")")
        success, output = validate_python_on_github(repo_name)
        if success:
            username = get_github_username()
            return "Working version pushed after " + str(attempt) + " attempt(s): https://github.com/" + username + "/" + repo_name
        print("Failed:")
        print(output)
        lesson = reflect_on_failure(idea, output)
        if lesson:
            print("Learned: " + lesson)
        if is_missing_secret_error(output):
            return ("Stopped early: this looks like a missing repo secret, not a code bug.\n"
                     "Add the required secret on the repo (Settings -> Secrets and variables -> Actions),\n"
                     "then run this same 'make:' command again.\nError seen:\n" + output)
        if attempt < MAX_GITHUB_ATTEMPTS:
            _thread_local.api_key = get_key_for_index(attempt + 1)
            write_code_for_github(idea, previous_error=output)
        else:
            return "Could not get a working version after " + str(MAX_GITHUB_ATTEMPTS) + " attempts. Last error:\n" + output
    return "Unexpected end of retry loop."

def load_history():
    if not os.path.exists("history.txt"):
        return ""
    return read_file("history.txt")

def save_to_history(question, answer):
    entry = "\nUser asked: " + question + "\nAssistant answered: " + answer + "\n"
    append_to_file("history.txt", entry)

def extract_math_expression(question):
    match = re.search(r"[\d\s\+\-\*/\(\)\.]{2,}", question)
    if match:
        return match.group(0).strip()
    return ""

def decide_tool(user_question):
    prompt = """You are an assistant with these tools:
- read_file: reads a file mentioned by the user
- write_file: writes AI-generated text to a file
- edit_file: changes one specific piece of text inside a file, keeping the rest the same
- list_directory: lists all files that currently exist
- search_files: searches the content of all .txt files for a specific word or phrase
- search_web: searches the live internet for current information
- fetch_url: fetches the actual content of a specific web page when a URL is given
- summarize_url: fetches a web page and gives a short summary instead of full content
- calculate: performs exact math calculations
- run_command: runs a safe system command (date, files list, who am i)
- none: just answer directly, no tool needed

User question: """ + user_question + """

Reply with ONLY one word: read_file, write_file, edit_file, list_directory, search_files, search_web, fetch_url, summarize_url, calculate, run_command, or none."""
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

def extract_url(question):
    match = re.search(r"https?://\S+", question)
    if match:
        return match.group(0).strip(".,)")
    return ""

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
    prompt = """Break this goal into the SMALLEST number of independent, standalone steps possible
(usually 1-5, never more than 5). Each step must be a complete, standalone action that fully
achieves part of the goal AND does not depend on the output of any other step, since these
steps will run simultaneously, not in order. Do NOT separate "do X" from "report X", combine
them into one step.

Goal: """ + goal + """

Reply with ONLY a numbered list, one step per line. No extra text."""
    plan_text = ask_ai(with_memory(prompt))
    steps = []
    for line in plan_text.split("\n"):
        line = line.strip()
        if line and line[0].isdigit():
            cleaned = line.split(".", 1)[-1].strip()
            if cleaned:
                steps.append(cleaned)
    return steps

def looks_like_failure(result):
    failure_signals = ["error", "could not", "not allowed", "no changes made", "no such file", "invalid characters"]
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

    url_in_question = extract_url(question)
    wants_summary = "summarize" in question_lower or "summary" in question_lower
    wants_fetch = "fetch" in question_lower or "get this page" in question_lower

    web_keywords = ["search the web", "look up", "google", "what is happening", "latest news", "current price"]
    needs_web = any(word in question_lower for word in web_keywords)

    math_keywords = ["calculate", "what is", "plus", "minus", "times", "divided by"]
    math_expression = extract_math_expression(question)
    needs_calculate = math_expression != "" and any(word in question_lower for word in math_keywords)

    memory_keywords = ["earlier", "before", "previously", "remember", "did i ask", "did i say"]
    needs_memory = any(word in question_lower for word in memory_keywords)

    if needs_calculate:
        tool = "calculate"
    elif needs_write:
        tool = "write_file"
    elif url_in_question and wants_summary:
        tool = "summarize_url"
    elif url_in_question:
        tool = "fetch_url"
    elif needs_web:
        tool = "search_web"
    elif needs_search_files:
        tool = "search_files"
    elif needs_list:
        tool = "list_directory"
    elif needs_command:
        tool = "run_command"
    elif needs_edit:
        tool = "edit_file"
    elif needs_memory:
        tool = "memory"
    elif needs_file:
        tool = "read_file"
    else:
        tool = decide_tool(question)

    if "calculate" in tool:
        expression = extract_math_expression(question)
        if not expression:
            return "Could not find a math expression to calculate."
        return calculate(expression)
    elif "summarize_url" in tool:
        url = extract_url(question)
        if not url:
            return "Could not find a URL in your message."
        return summarize_url(url)
    elif "fetch_url" in tool:
        url = extract_url(question)
        if not url:
            return "Could not find a URL in your message."
        return fetch_url(url)
    elif "search_web" in tool:
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
        return ask_ai(with_memory(full_question))

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

def run_step_parallel(index, step, history):
    _thread_local.api_key = get_key_for_index(index)
    result, final_step = handle_step_with_retry(step, "", history)
    return index, result, final_step

conversation_history = load_history()
if conversation_history:
    print("Loaded previous conversation history.")

while True:
    user_question = input("Ask me something, type 'goal: ...', type 'build: <idea>', type 'make: <idea> | repo_name', type 'github: repo_name | file_path | content', type 'remember: <note>', type 'lessons', or quit: ")

    if user_question.lower() == "quit":
        print("Goodbye!")
        break

    if user_question.lower().startswith("github:"):
        parts = user_question[7:].split("|")
        if len(parts) != 3:
            print("Format must be: github: repo_name | file_path | content")
        else:
            repo_name = parts[0].strip()
            file_path = parts[1].strip()
            content = parts[2].strip()
            repo_result = create_github_repo(repo_name)
            print(repo_result)
            file_result = create_github_file(repo_name, file_path, content)
            print(file_result)
    elif user_question.lower().startswith("remember:"):
        note = user_question[9:].strip()
        print(remember_note(note))
    elif user_question.lower() == "lessons":
        lessons = load_lessons()
        print(lessons if lessons else "No lessons learned yet.")
    elif user_question.lower().startswith("make:"):
        parts = user_question[5:].split("|")
        if len(parts) != 2:
            print("Format must be: make: idea | repo_name")
        else:
            idea = parts[0].strip()
            repo_name = parts[1].strip()
            result = build_and_fix_on_github(idea, repo_name)
            print(result)
            update_memory("User asked to build: " + idea + " (repo: " + repo_name + "). Result: " + result[:500])
    elif user_question.lower().startswith("build:"):
        idea = user_question[6:].strip()
        result = build_and_fix_workflow(idea)
        print(result)
        save_to_history(user_question, result)
        conversation_history = conversation_history + "\nUser asked: " + user_question + "\nAssistant answered: " + result + "\n"
    elif user_question.lower().startswith("edit:"):
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
            previous_results = ""
        else:
            print("Running " + str(len(steps)) + " step(s) in parallel across " + str(len(CEREBRAS_KEYS)) + " API key(s)...")
            results_by_index = {}
            with ThreadPoolExecutor(max_workers=len(steps)) as executor:
                futures = [executor.submit(run_step_parallel, i, step, conversation_history) for i, step in enumerate(steps)]
                for future in as_completed(futures):
                    index, result, final_step = future.result()
                    results_by_index[index] = (result, final_step)
                    print("Step " + str(index + 1) + " (" + final_step + ") finished: " + result[:200])
            previous_results = ""
            for i in range(len(steps)):
                result, final_step = results_by_index[i]
                previous_results = previous_results + "\nStep " + str(i + 1) + " (" + final_step + "): " + result
        print("Goal complete!")
        save_to_history(goal, previous_results)
        conversation_history = conversation_history + previous_results
    else:
        answer, _ = handle_step_with_retry(user_question, "", conversation_history)
        print(answer)
        save_to_history(user_question, answer)
        conversation_history = conversation_history + "\nUser asked: " + user_question + "\nAssistant answered: " + answer + "\n"

    print("---")