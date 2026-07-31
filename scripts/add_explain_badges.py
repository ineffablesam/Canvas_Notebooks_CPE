#!/usr/bin/env python3
"""Add "Explain with ChatGPT" badges above code cells across all lesson notebooks.

Usage:
    python3 scripts/add_explain_badges.py [--dry-run]

What it does
------------
For every .ipynb file under the Module folders, walks each code cell and inserts
a small markdown "badge" cell directly above it with a link that opens a new
ChatGPT chat pre-filled with that cell's exact code and a prompt asking for a
step-by-step, beginner-friendly explanation.

Cells that don't need an explanation badge are skipped automatically:
  - cells with no real code (comments/blank only)
  - cells that are just a lone `pass` placeholder (student scratch space)
  - cells that contain `# TODO` markers (unfinished skeleton the student must
    still complete -- nothing to explain yet)

Each notebook's original JSON indent width is auto-detected and preserved so
re-writing a file doesn't produce a noisy whole-file diff.

Re-running the script is safe: if a code cell is already immediately preceded
by a markdown cell containing a chatgpt.com link, it is left untouched (so any
manually customized badges, e.g. ones that also include a video link, are
never overwritten).
"""
import argparse
import glob
import json
import re
import secrets
import urllib.parse

PROMPT_PREFIX = (
    "Explain what this Python code does, step by step, in simple "
    "beginner-friendly terms. Do not just repeat the code back to me; "
    "explain the *why* behind each line.\n\n"
)

BADGE_MARKER = "chatgpt.com/?q="


def sniff_indent(path: str) -> int:
    with open(path) as f:
        for line in f:
            m = re.match(r'^( +)"cells"\s*:', line)
            if m:
                return len(m.group(1))
    return 1


def needs_badge(source: str) -> bool:
    lines = source.split("\n")
    non_comment = [l for l in lines if l.strip() and not l.strip().startswith("#")]
    if not non_comment:
        return False  # no real code (comments/blank only)
    if len(non_comment) == 1 and non_comment[0].strip() == "pass":
        return False  # empty student scratch space
    if "TODO" in source:
        return False  # unfinished skeleton, nothing to explain yet
    return True


def already_has_badge(prev_cell) -> bool:
    if not prev_cell or prev_cell.get("cell_type") != "markdown":
        return False
    src = "".join(prev_cell.get("source", []))
    return BADGE_MARKER in src


def make_badge_cell(code_source: str) -> dict:
    prompt = PROMPT_PREFIX + code_source
    encoded = urllib.parse.quote_plus(prompt)
    chatgpt_url = "https://chatgpt.com/?q=" + encoded

    html = (
        '<div style="display:flex; align-items:center; justify-content:space-between; '
        'border-left:4px solid #999; border-radius:6px; background:rgba(153,153,153,0.08); '
        'padding:8px 20px 8px 16px; margin:6px 0;">\n'
        "  <span>&#128269;&nbsp;<b>Stuck on this code?</b></span>\n"
        '  <span style="display:flex; align-items:center; gap:8px;">\n'
        f'    <a href="{chatgpt_url}"><img src="https://img.shields.io/badge/%F0%9F%A4%96_Explain_with_ChatGPT-10a37f?style=flat" '
        'alt="Explain with ChatGPT" style="height:26px; vertical-align:middle;"></a>\n'
        "  </span>\n"
        "</div>"
    )

    return {
        "cell_type": "markdown",
        "id": secrets.token_hex(4),
        "metadata": {},
        "source": html.splitlines(keepends=True),
    }


def process_notebook(path: str, dry_run: bool) -> int:
    with open(path) as f:
        nb = json.load(f)

    cells = nb["cells"]
    new_cells = []
    added = 0

    for i, cell in enumerate(cells):
        if cell["cell_type"] == "code":
            source = "".join(cell.get("source", []))
            prev_cell = cells[i - 1] if i > 0 else None
            if needs_badge(source) and not already_has_badge(prev_cell):
                new_cells.append(make_badge_cell(source))
                added += 1
        new_cells.append(cell)

    if added and not dry_run:
        indent = sniff_indent(path)
        nb["cells"] = new_cells
        with open(path, "w") as f:
            json.dump(nb, f, indent=indent)
            f.write("\n")

    return added


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    total = 0
    for path in sorted(glob.glob("Module */*.ipynb")):
        added = process_notebook(path, args.dry_run)
        if added:
            print(f"{path}: +{added} badge(s)")
        total += added

    print(f"\n{'Would add' if args.dry_run else 'Added'} {total} badge(s) total.")


if __name__ == "__main__":
    main()
