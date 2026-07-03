#!/usr/bin/env python3
"""
auto_readme_generator.py

A simple command‑line utility that scans a directory for Python automation scripts,
extracts each script’s module docstring and any comment‑based usage examples,
and generates a concise README (as a markdown table) listing:

* Script name
* Brief description
* Required inputs (if documented)
* Example command(s)

Only the Python standard library is used.
"""

import os
import sys
import argparse
import ast
import re
import textwrap

def get_module_docstring(filepath):
    """Return the module‑level docstring of a Python file, or None."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
        return ast.get_docstring(tree)
    except (SyntaxError, UnicodeDecodeError) as e:
        sys.stderr.write(f"Warning: could not parse {filepath}: {e}\n")
        return None

def extract_comments(content):
    """
    Return a list of comment lines (including the leading '#') from the source.
    """
    return [line for line in content.splitlines() if line.lstrip().startswith("#")]

def parse_docstring(doc):
    """
    Very loosely parse a docstring to obtain:
        - brief description (first non‑empty line)
        - required inputs (lines after a line containing 'Args:' or 'Parameters:')
        - examples (lines after a line containing 'Example:')

    Returns (description, required_inputs, examples) where required_inputs
    and examples are strings (may be empty).
    """
    if not doc:
        return ("", "", "")

    lines = doc.expandtabs().splitlines()
    # Description: first non‑empty line
    description = ""
    for line in lines:
        stripped = line.strip()
        if stripped:
            description = stripped
            break

    required = []
    examples = []

    # Helpers to capture indented blocks after a marker
    def capture_block(start_marker):
        block = []
        capture = False
        for line in lines:
            if capture:
                if line.strip() == "" and block:
                    break
                block.append(line.rstrip())
            elif start_marker.lower() in line.lower():
                capture = True
        return block

    # Required inputs block
    req_block = capture_block("Args:")
    if not req_block:
        req_block = capture_block("Parameters:")
    if req_block:
        # Keep only lines that look like bullet points or 'name: description'
        for l in req_block:
            stripped = l.strip()
            if stripped.startswith("-") or ":" in stripped:
                required.append(stripped.lstrip("- ").rstrip())
        required_str = " ".join(required)
    else:
        required_str = ""

    # Examples block from docstring
    ex_block = capture_block("Example:")
    if ex_block:
        examples = [l.strip() for l in ex_block if l.strip()]
        examples_str = " ".join(examples)
    else:
        examples_str = ""

    return (description, required_str, examples_str)

def extract_comment_examples(comment_lines):
    """
    From a list of comment lines, extract lines that look like usage examples.
    Heuristic: a comment containing the word 'example' (case‑insensitive) followed
    by a line that looks like a command (starts with a word and spaces, no leading '#').
    Returns a list of example strings.
    """
    examples = []
    for i, line in enumerate(comment_lines):
        if re.search(r"example", line, re.IGNORECASE):
            # Look ahead a few lines for something that looks like a command
            for nxt in comment_lines[i+1:i+4]:
                cmd = nxt.lstrip("# ").strip()
                if cmd and not cmd.lower().startswith("example"):
                    examples.append(cmd)
                    break
    return examples

def scan_directory(root_dir):
    """
    Scan ``root_dir`` for *.py files (non‑package scripts only) and collect
    metadata for each.
    Returns a list of dictionaries with keys: name, description, inputs, example.
    """
    entries = []
    for entry in os.listdir(root_dir):
        if not entry.lower().endswith(".py"):
            continue
        full_path = os.path.join(root_dir, entry)
        if not os.path.isfile(full_path):
            continue

        docstring = get_module_docstring(full_path)
        description, inputs, doc_examples = parse_docstring(docstring)

        # Fallback to comment‑based examples if docstring has none
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()
        comment_lines = extract_comments(content)
        comment_examples = extract_comment_examples(comment_lines)

        # Prefer docstring examples, else comment examples
        example = doc_examples if doc_examples else ("; ".join(comment_examples) if comment_examples else "")

        entries.append({
            "name": entry,
            "description": description,
            "inputs": inputs,
            "example": example
        })
    return entries

def generate_markdown_table(entries):
    """
    Produce a markdown table string from ``entries``.
    """
    header = ["Script", "Description", "Required Inputs", "Example Command"]
    sep = ["---"] * len(header)

    rows = [header, sep]
    for e in entries:
        rows.append([
            e["name"],
            e["description"],
            e["inputs"],
            e["example"]
        ])

    # Pad columns for readability
    col_widths = [max(len(str(cell)) for cell in col) for col in zip(*rows)]
    lines = []
    for row in rows:
        padded = [str(cell).ljust(col_widths[i]) for i, cell in enumerate(row)]
        lines.append("| " + " | ".join(padded) + " |")
    return "\n".join(lines)

def main():
    parser = argparse.ArgumentParser(
        description="Generate a README markdown table from Python automation scripts."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory to scan (default: current working directory)"
    )
    parser.add_argument(
        "-o", "--output",
        default="README_AUTO_GENERATED.md",
        help="Output markdown file name (default: README_AUTO_GENERATED.md)"
    )
    args = parser.parse_args()

    root = os.path.abspath(args.directory)
    if not os.path.isdir(root):
        sys.stderr.write(f"Error: {root} is not a directory.\n")
        sys.exit(1)

    # Safety check: ensure we can read the directory
    try:
        os.listdir(root)
    except PermissionError as e:
        sys.stderr.write(f"Error: insufficient permissions to read {root}: {e}\n")
        sys.exit(1)

    entries = scan_directory(root)
    if not entries:
        sys.stderr.write("No Python scripts found in the specified directory.\n")
        sys.exit(0)

    markdown = generate_markdown_table(entries)

    output_path = os.path.join(root, args.output)
    try:
        with open(output_path, "w", encoding="utf-8") as out_f:
            out_f.write("# Auto‑generated README\n\n")
            out_f.write(markdown + "\n")
        print(f"README generated at: {output_path}")
    except OSError as e:
        sys.stderr.write(f"Failed to write output file: {e}\n")
        sys.exit(1)

if __name__ == "__main__":
    main()