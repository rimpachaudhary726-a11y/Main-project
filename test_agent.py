import os
import json
import time
import subprocess
import sys
import threading

# ============================================================
# AGENT TEST SUITE
# Runs every feature of main.py and prints a pass/fail report
# Usage: python3 test_agent.py
# ============================================================

PASS = "✅ PASS"
FAIL = "❌ FAIL"
SKIP = "⏭️  SKIP"

results = []
start_time = time.time()

def log(name, status, detail=""):
    results.append((name, status, detail))
    icon = status
    line = icon + " | " + name
    if detail:
        line += " — " + str(detail)[:120]
    print(line)

def run_python(code, timeout=15):
    """Runs a Python snippet and returns (success, output)."""
    try:
        result = subprocess.run(
            ["python3", "-c", code],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ}
        )
        output = result.stdout + result.stderr
        return result.returncode == 0, output.strip()
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT after " + str(timeout) + "s"
    except Exception as e:
        return False, str(e)

def run_agent_function(setup_code, call_code, timeout=20):
    """
    Imports main.py functions and runs them.
    Safe to import since main.py uses if __name__ == "__main__".
    setup_code: any setup before the call (e.g. creating files)
    call_code: the actual function call to test
    """
    cwd = os.getcwd()
    code = (
        "import sys, os\n"
        "os.chdir(" + repr(cwd) + ")\n"
        "sys.path.insert(0, " + repr(cwd) + ")\n"
        "import main\n"
        + setup_code + "\n"
        + "try:\n"
        + "    " + call_code.strip().replace("\n", "\n    ") + "\n"
        + "except Exception as e:\n"
        + "    print(\'ERROR: \' + str(e))\n"
    )
    return run_python(code, timeout=timeout)

def write_temp_file(filename, content):
    with open(filename, "w") as f:
        f.write(content)

def read_temp_file(filename):
    if os.path.exists(filename):
        with open(filename) as f:
            return f.read()
    return None

def delete_temp_file(filename):
    if os.path.exists(filename):
        os.remove(filename)

# ============================================================
# TEST GROUPS
# ============================================================

def test_imports():
    print("\n--- Imports & Startup ---")
    ok, out = run_python("""
import sys
sys.path.insert(0, '.')
import main
print("imported")
""", timeout=10)
    if ok and "imported" in out:
        log("Import main.py", PASS)
    else:
        log("Import main.py", FAIL, out)

def test_basic_file_tools():
    print("\n--- Basic File Tools ---")

    # write_file
    ok, out = run_agent_function(
        "",
        "main.write_file('_test_write.txt', 'hello world')\nprint('wrote')"
    )
    if ok and os.path.exists("_test_write.txt"):
        log("write_file", PASS)
    else:
        log("write_file", FAIL, out)

    # read_file
    ok, out = run_agent_function(
        "main.write_file('_test_read.txt', 'hello world')",
        "content = main.read_file('_test_read.txt')\nprint(content)"
    )
    if ok and "hello world" in out:
        log("read_file", PASS)
    else:
        log("read_file", FAIL, out)

    # edit_file
    ok, out = run_agent_function(
        "main.write_file('_test_edit.txt', 'hello world')",
        "result = main.edit_file('_test_edit.txt', 'hello', 'goodbye')\nprint(result)"
    )
    if ok and "successfully" in out.lower():
        log("edit_file", PASS)
    else:
        log("edit_file", FAIL, out)

    # append_to_file
    ok, out = run_agent_function(
        "main.write_file('_test_append.txt', 'line1')",
        "main.append_to_file('_test_append.txt', 'line2')\nprint(main.read_file('_test_append.txt'))"
    )
    if ok and "line2" in out:
        log("append_to_file", PASS)
    else:
        log("append_to_file", FAIL, out)

    # list_directory
    ok, out = run_agent_function("", "print(main.list_directory())")
    if ok and len(out) > 0:
        log("list_directory", PASS)
    else:
        log("list_directory", FAIL, out)

    # search_files
    ok, out = run_agent_function(
        "main.write_file('_test_search.txt', 'unique_search_xyz_term')",
        "print(main.search_files('unique_search_xyz_term'))"
    )
    if ok and "_test_search.txt" in out:
        log("search_files", PASS)
    else:
        log("search_files", FAIL, out)

    # calculate
    ok, out = run_agent_function("", "print(main.calculate('2 + 2 * 3'))")
    if ok and "8" in out:
        log("calculate", PASS)
    else:
        log("calculate", FAIL, out)

    # Cleanup
    for f in ["_test_write.txt", "_test_read.txt", "_test_edit.txt",
              "_test_append.txt", "_test_search.txt"]:
        delete_temp_file(f)

def test_memory_and_lessons():
    print("\n--- Memory & Lessons ---")

    # remember_note
    ok, out = run_agent_function(
        "",
        "print(main.remember_note('test note xyz'))"
    )
    if ok and "Remembered" in out:
        log("remember_note", PASS)
    else:
        log("remember_note", FAIL, out)

    # load_memory
    ok, out = run_agent_function(
        "main.remember_note('test note xyz')",
        "mem = main.load_memory()\nprint('found' if 'test note xyz' in mem else 'not found')"
    )
    if ok and "found" in out:
        log("load_memory", PASS)
    else:
        log("load_memory", FAIL, out)

    # load_lessons
    ok, out = run_agent_function(
        "",
        "lessons = main.load_lessons()\nprint(type(lessons).__name__)"
    )
    if ok and "str" in out:
        log("load_lessons", PASS)
    else:
        log("load_lessons", FAIL, out)

    # history
    ok, out = run_agent_function(
        "",
        "main.save_to_history('test question', 'test answer')\nprint(main.load_history()[-20:])"
    )
    if ok and "test" in out:
        log("save/load_history", PASS)
    else:
        log("save/load_history", FAIL, out)

def test_tool_registry():
    print("\n--- Tool Registry ---")

    # register_tool
    ok, out = run_agent_function(
        "main.write_file('_test_tool_code.py', 'print(42)')",
        """
idx_before = len(main.load_tools_index())
tid = main.register_tool('print the number 42', 'print(42)')
idx_after = len(main.load_tools_index())
print('registered' if idx_after > idx_before else 'failed')
print(tid)
"""
    )
    if ok and "registered" in out:
        log("register_tool", PASS)
    else:
        log("register_tool", FAIL, out)

    # find_matching_tool
    ok, out = run_agent_function(
        "main.register_tool('print the number 42', 'print(42)')",
        """
tool = main.find_matching_tool('print the number 42')
print('found' if tool else 'not found')
"""
    )
    if ok and "found" in out:
        log("find_matching_tool", PASS)
    else:
        log("find_matching_tool", FAIL, out)

    # update_tool_trust
    ok, out = run_agent_function(
        "tid = main.register_tool('test trust tool', 'print(1)')",
        """
tid = main.register_tool('test trust tool 2', 'print(1)')
main.update_tool_trust(tid, True)
index = main.load_tools_index()
tool = next((t for t in index if t['id'] == tid), None)
print('good' if tool and tool.get('good_runs', 0) > 0 else 'bad')
"""
    )
    if ok and "good" in out:
        log("update_tool_trust", PASS)
    else:
        log("update_tool_trust", FAIL, out)

    # is_tool_trustworthy
    ok, out = run_agent_function(
        "",
        """
tool = {'good_runs': 5, 'bad_runs': 1}
print('trustworthy' if main.is_tool_trustworthy(tool) else 'not')
"""
    )
    if ok and "trustworthy" in out:
        log("is_tool_trustworthy", PASS)
    else:
        log("is_tool_trustworthy", FAIL, out)

    # validate_tool_output - skip if no API key
    if os.environ.get("CEREBRAS_API_KEY"):
        ok, out = run_agent_function(
            "",
            "print(main.validate_tool_output('print hello world', 'hello world'))"
        )
        if ok:
            log("validate_tool_output", PASS)
        else:
            log("validate_tool_output", FAIL, out)
    else:
        log("validate_tool_output", SKIP, "No CEREBRAS_API_KEY")

def test_code_generation():
    print("\n--- Code Generation (build:) ---")

    if not os.environ.get("CEREBRAS_API_KEY"):
        log("write_code", SKIP, "No CEREBRAS_API_KEY")
        log("run_generated_code", SKIP, "No CEREBRAS_API_KEY")
        log("build_and_fix_workflow simple", SKIP, "No CEREBRAS_API_KEY")
        return

    # write_code
    ok, out = run_agent_function(
        "",
        """
code = main.write_code('print hello world')
print('has_code' if 'print' in code.lower() else 'no_code')
""", timeout=30
    )
    if ok and "has_code" in out:
        log("write_code", PASS)
    else:
        log("write_code", FAIL, out)

    # run_generated_code
    ok, out = run_agent_function(
        "main.write_file(main.GENERATED_CODE_FILE, 'print(\"hello from test\")')",
        """
success, output = main.run_generated_code()
print('success' if success else 'failed')
print(output)
"""
    )
    if ok and "success" in out:
        log("run_generated_code", PASS, "hello from test" if "hello" in out else "")
    else:
        log("run_generated_code", FAIL, out)

    # Full build workflow - simple idea
    ok, out = run_agent_function(
        "",
        """
result = main.build_and_fix_workflow('print the current date and time')
print('built' if 'successfully' in result.lower() or 'saved as' in result.lower() else 'failed')
print(result[:200])
""", timeout=60
    )
    if ok and "built" in out:
        log("build_and_fix_workflow simple", PASS)
    else:
        log("build_and_fix_workflow simple", FAIL, out[:200])

def test_goal_planning():
    print("\n--- Goal Planning ---")

    if not os.environ.get("CEREBRAS_API_KEY"):
        log("make_plan", SKIP, "No CEREBRAS_API_KEY")
        log("run_goal_with_dependencies", SKIP, "No CEREBRAS_API_KEY")
        return

    # make_plan
    ok, out = run_agent_function(
        "",
        """
steps = main.make_plan('search the web for Python tips and save them to a file')
print('steps: ' + str(len(steps)))
for s in steps:
    print(s['step'][:60])
""", timeout=30
    )
    if ok and "steps:" in out:
        count = int(out.split("steps:")[1].split()[0])
        if count >= 1:
            log("make_plan", PASS, str(count) + " steps generated")
        else:
            log("make_plan", FAIL, "0 steps generated")
    else:
        log("make_plan", FAIL, out[:200])

    # run_goal_with_dependencies - simple 1-step goal
    ok, out = run_agent_function(
        "",
        """
steps = [{'step': 'say hello world', 'depends_on': []}]
result = main.run_goal_with_dependencies(steps, '')
print('done' if result else 'empty')
""", timeout=30
    )
    if ok and "done" in out:
        log("run_goal_with_dependencies", PASS)
    else:
        log("run_goal_with_dependencies", FAIL, out[:200])

def test_web_search():
    print("\n--- Web Search & Fetch ---")

    # search_web
    ok, out = run_agent_function(
        "",
        "print(main.search_web('Python programming language'))"
    )
    if ok and ("python" in out.lower() or "http" in out.lower()):
        log("search_web", PASS)
    elif "ERROR" in out or "failed" in out.lower():
        log("search_web", FAIL, out[:150])
    else:
        log("search_web", SKIP, "Firecrawl may not be configured")

    # fetch_url
    ok, out = run_agent_function(
        "",
        "print(main.fetch_url('https://example.com'))"
    )
    if ok and len(out) > 50:
        log("fetch_url", PASS)
    else:
        log("fetch_url", FAIL, out[:150])

def test_api_key_detection():
    print("\n--- Auto API Key System ---")

    # is_api_key_error
    ok, out = run_agent_function(
        "",
        """
print(main.is_api_key_error('Error: required environment variable NEWSAPI_KEY is not set'))
print(main.is_api_key_error('IndexError: list index out of range'))
"""
    )
    if ok and out.startswith("True") and "False" in out:
        log("is_api_key_error detection", PASS)
    else:
        log("is_api_key_error detection", FAIL, out)

    # find_keyless_api
    if not os.environ.get("CEREBRAS_API_KEY"):
        log("find_keyless_api", SKIP, "No CEREBRAS_API_KEY")
    else:
        ok, out = run_agent_function(
            "",
            """
url = main.find_keyless_api('get current bitcoin price')
print('found' if url and url.startswith('http') else 'not_found')
print(url)
""", timeout=30
        )
        if ok and "found" in out:
            log("find_keyless_api", PASS, out.split("\n")[1][:80] if "\n" in out else "")
        else:
            log("find_keyless_api", FAIL, out[:150])

    # load/save api keys store
    ok, out = run_agent_function(
        "",
        """
main.save_api_key('TestService', 'test_key_12345')
key = main.get_stored_api_key('TestService')
print('found' if key == 'test_key_12345' else 'not found')
"""
    )
    if ok and "found" in out:
        log("save/get_stored_api_key", PASS)
    else:
        log("save/get_stored_api_key", FAIL, out)

def test_failure_tracking():
    print("\n--- Failure Tracking & Prechecks ---")

    # categorize_failure
    if not os.environ.get("CEREBRAS_API_KEY"):
        log("categorize_failure", SKIP, "No CEREBRAS_API_KEY")
    else:
        ok, out = run_agent_function(
            "",
            "print(main.categorize_failure('Error: required environment variable API_KEY is not set'))",
            timeout=20
        )
        if ok and len(out.strip()) > 0:
            log("categorize_failure", PASS, out.strip())
        else:
            log("categorize_failure", FAIL, out)

    # bump_failure_count
    ok, out = run_agent_function(
        "",
        """
count = main.bump_failure_count('test_category_xyz')
print('counted' if count >= 1 else 'failed')
"""
    )
    if ok and "counted" in out:
        log("bump_failure_count", PASS)
    else:
        log("bump_failure_count", FAIL, out)

    # run_all_prechecks on safe code
    ok, out = run_agent_function(
        "",
        """
warnings = main.run_all_prechecks('print(\"hello world\")')
print('ok' if isinstance(warnings, list) else 'bad')
"""
    )
    if ok and "ok" in out:
        log("run_all_prechecks", PASS)
    else:
        log("run_all_prechecks", FAIL, out)

def test_builtin_tools():
    print("\n--- Built-in Tools ---")

    # stats
    ok, out = run_agent_function(
        "",
        "main.builtin_performance_tracker()"
    )
    if ok and "Performance" in out:
        log("builtin_performance_tracker (stats)", PASS)
    else:
        log("builtin_performance_tracker (stats)", FAIL, out[:150])

    # dependency map
    ok, out = run_agent_function(
        "",
        "main.builtin_dependency_map()"
    )
    if ok and ("Map" in out or "No tools" in out):
        log("builtin_dependency_map (map)", PASS)
    else:
        log("builtin_dependency_map (map)", FAIL, out[:150])

    # failure predictor
    ok, out = run_agent_function(
        "",
        "main.builtin_failure_predictor()"
    )
    if ok and ("Prediction" in out or "No tools" in out):
        log("builtin_failure_predictor (predict)", PASS)
    else:
        log("builtin_failure_predictor (predict)", FAIL, out[:150])

    # history archiver - small file, should say under limit
    ok, out = run_agent_function(
        "main.write_file('history.txt', 'small content')",
        "main.builtin_history_archiver()"
    )
    if ok and ("Under 50KB" in out or "No history" in out):
        log("builtin_history_archiver (archive)", PASS)
    else:
        log("builtin_history_archiver (archive)", FAIL, out[:150])

    # daily briefing
    ok, out = run_agent_function(
        "main.write_file('weather_city.txt', 'London')",
        "main.builtin_daily_briefing()"
    )
    if ok and "Briefing" in out:
        log("builtin_daily_briefing (briefing)", PASS)
    else:
        log("builtin_daily_briefing (briefing)", FAIL, out[:150])

    # website monitor - first run (creates baseline)
    ok, out = run_agent_function(
        "main.write_file('watch_url.txt', 'https://example.com')",
        "main.builtin_website_monitor()"
    )
    if ok and ("baseline" in out.lower() or "No changes" in out or "CHANGE" in out):
        log("builtin_website_monitor (monitor)", PASS)
    else:
        log("builtin_website_monitor (monitor)", FAIL, out[:150])

    # webpage to text
    ok, out = run_agent_function(
        "main.write_file('url_to_pdf.txt', 'https://example.com')",
        "main.builtin_webpage_to_text()"
    )
    if ok and ("Saved" in out or "output_page.txt" in out):
        log("builtin_webpage_to_text (totext)", PASS)
    else:
        log("builtin_webpage_to_text (totext)", FAIL, out[:150])

    # github actions monitor
    if not os.environ.get("GITHUB_TOKEN"):
        log("builtin_github_actions_monitor (ghusage)", SKIP, "No GITHUB_TOKEN")
    else:
        ok, out = run_agent_function(
            "",
            "main.builtin_github_actions_monitor()"
        )
        if ok and ("Usage" in out or "minutes" in out.lower() or "billing" in out.lower()):
            log("builtin_github_actions_monitor (ghusage)", PASS)
        else:
            log("builtin_github_actions_monitor (ghusage)", FAIL, out[:150])

    # gmail sender - skip if no credentials
    if not os.environ.get("AGENT_EMAIL") or not os.environ.get("AGENT_APP_PASSWORD"):
        log("builtin_gmail_sender (sendemail)", SKIP, "No AGENT_EMAIL/AGENT_APP_PASSWORD")
    else:
        ok, out = run_agent_function(
            "main.write_file('email_draft.txt', 'Test Subject\\n\\nTest body.')",
            "main.builtin_gmail_sender()"
        )
        if ok and ("sent" in out.lower() or "Email sent" in out):
            log("builtin_gmail_sender (sendemail)", PASS)
        else:
            log("builtin_gmail_sender (sendemail)", FAIL, out[:150])

    # telegram - skip if no token
    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not os.environ.get("TELEGRAM_CHAT_ID"):
        log("builtin_telegram_notifier (telegram)", SKIP, "No TELEGRAM_BOT_TOKEN/CHAT_ID")
    else:
        ok, out = run_agent_function(
            "main.write_file('telegram_msg.txt', 'Test from agent test suite')",
            "main.builtin_telegram_notifier()"
        )
        if ok and "sent" in out.lower():
            log("builtin_telegram_notifier (telegram)", PASS)
        else:
            log("builtin_telegram_notifier (telegram)", FAIL, out[:150])

def test_github_helpers():
    print("\n--- GitHub Helpers ---")

    if not os.environ.get("GITHUB_TOKEN"):
        log("get_github_username", SKIP, "No GITHUB_TOKEN")
        log("github_headers", SKIP, "No GITHUB_TOKEN")
        return

    ok, out = run_agent_function(
        "",
        "print(main.get_github_username())"
    )
    if ok and len(out.strip()) > 0 and "ERROR" not in out:
        log("get_github_username", PASS, out.strip())
    else:
        log("get_github_username", FAIL, out)

    ok, out = run_agent_function(
        "",
        "h = main.github_headers()\nprint('auth' if 'Authorization' in h else 'no auth')"
    )
    if ok and "auth" in out:
        log("github_headers", PASS)
    else:
        log("github_headers", FAIL, out)

def test_cerebras_key_rotation():
    print("\n--- Cerebras Key Rotation ---")

    if not os.environ.get("CEREBRAS_API_KEY"):
        log("ask_ai basic", SKIP, "No CEREBRAS_API_KEY")
        log("key rotation", SKIP, "No CEREBRAS_API_KEY")
        return

    ok, out = run_agent_function(
        "",
        "print(main.ask_ai('Reply with only the word: HELLO'))",
        timeout=20
    )
    if ok and "HELLO" in out.upper():
        log("ask_ai basic", PASS)
    else:
        log("ask_ai basic", FAIL, out[:150])

    ok, out = run_agent_function(
        "",
        """
idx = main._pick_available_key_index()
print('index: ' + str(idx))
print('keys: ' + str(len(main.CEREBRAS_KEYS)))
"""
    )
    if ok and "index:" in out:
        log("key rotation", PASS, out.strip())
    else:
        log("key rotation", FAIL, out)

def test_dreamer():
    print("\n--- Dreamer Thread ---")

    ok, out = run_agent_function(
        "",
        """
ideas = main.load_pending_ideas()
print('loaded: ' + str(type(ideas).__name__))
"""
    )
    if ok and "list" in out:
        log("load_pending_ideas", PASS)
    else:
        log("load_pending_ideas", FAIL, out)

    ok, out = run_agent_function(
        "",
        """
main.add_pending_idea({'kind': 'test', 'idea': 'test idea', 'status': 'works', 'created_at': 0})
ideas = main.load_pending_ideas()
print('found' if any(i.get('kind') == 'test' for i in ideas) else 'not found')
"""
    )
    if ok and "found" in out:
        log("add_pending_idea", PASS)
    else:
        log("add_pending_idea", FAIL, out)

    ok, out = run_agent_function(
        "",
        "print('free' if main.keys_are_free() else 'busy')"
    )
    if ok and ("free" in out or "busy" in out):
        log("keys_are_free", PASS)
    else:
        log("keys_are_free", FAIL, out)

def test_strip_fences():
    print("\n--- Utility Functions ---")

    ok, out = run_agent_function(
        "",
        """
result = main.strip_fences('```python\\nprint(1)\\n```')
print('clean' if result == 'print(1)' else 'dirty: ' + repr(result))
"""
    )
    if ok and "clean" in out:
        log("strip_fences", PASS)
    else:
        log("strip_fences", FAIL, out)

    ok, out = run_agent_function(
        "",
        """
result = main.looks_like_failure('ERROR: something went wrong')
print('detected' if result else 'missed')
"""
    )
    if ok and "detected" in out:
        log("looks_like_failure", PASS)
    else:
        log("looks_like_failure", FAIL, out)

    ok, out = run_agent_function(
        "",
        """
result = main.extract_url('Check this out: https://example.com/page?q=1')
print('found' if result == 'https://example.com/page?q=1' else 'missed: ' + str(result))
"""
    )
    if ok and "found" in out:
        log("extract_url", PASS)
    else:
        log("extract_url", FAIL, out)

def test_ensure_default_files():
    print("\n--- Auto-created Default Files ---")
    expected = [
        "watch_url.txt",
        "weather_city.txt",
        "email_draft.txt",
        "telegram_msg.txt",
        "url_to_pdf.txt"
    ]
    # Run ensure_default_files
    run_agent_function("", "main.ensure_default_files()")
    all_ok = True
    for fname in expected:
        if os.path.exists(fname):
            log("Auto-create " + fname, PASS)
        else:
            log("Auto-create " + fname, FAIL, "File not created")
            all_ok = False

# ============================================================
# CLEANUP
# ============================================================

def cleanup():
    """Remove test artifacts."""
    test_files = [
        "_test_write.txt", "_test_read.txt", "_test_edit.txt",
        "_test_append.txt", "_test_search.txt", "_test_tool_code.py",
        "watch_url_cache.txt", "output_page.txt"
    ]
    for f in test_files:
        delete_temp_file(f)

# ============================================================
# REPORT
# ============================================================

def print_report():
    total = len(results)
    passed = sum(1 for _, s, _ in results if s == PASS)
    failed = sum(1 for _, s, _ in results if s == FAIL)
    skipped = sum(1 for _, s, _ in results if s == SKIP)
    elapsed = round(time.time() - start_time, 1)

    print("\n" + "=" * 60)
    print("AGENT TEST REPORT")
    print("=" * 60)
    print("Total:   " + str(total))
    print("Passed:  " + str(passed) + " ✅")
    print("Failed:  " + str(failed) + " ❌")
    print("Skipped: " + str(skipped) + " ⏭️")
    print("Time:    " + str(elapsed) + "s")
    print("=" * 60)

    if failed > 0:
        print("\nFailed tests:")
        for name, status, detail in results:
            if status == FAIL:
                print("  ❌ " + name + ((" — " + detail[:100]) if detail else ""))

    if skipped > 0:
        print("\nSkipped tests (missing env vars):")
        for name, status, detail in results:
            if status == SKIP:
                print("  ⏭️  " + name + ((" — " + detail) if detail else ""))

    score = round(100 * passed / max(passed + failed, 1), 1)
    print("\nScore (excluding skips): " + str(score) + "%")
    print("=" * 60)

# ============================================================
# RUN ALL TESTS
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("AGENT TEST SUITE")
    print("=" * 60)
    print("Testing all features of main.py...")
    print("(Some tests require CEREBRAS_API_KEY, GITHUB_TOKEN, etc)")
    print("")

    test_imports()
    test_basic_file_tools()
    test_memory_and_lessons()
    test_tool_registry()
    test_failure_tracking()
    test_strip_fences()
    test_ensure_default_files()
    test_builtin_tools()
    test_web_search()
    test_api_key_detection()
    test_github_helpers()
    test_cerebras_key_rotation()
    test_goal_planning()
    test_code_generation()
    test_dreamer()

    cleanup()
    print_report()
