#!/usr/bin/env python3
"""Add "Watch Video" + "Explain with ChatGPT" badges above code cells.

Usage:
    python3 scripts/add_explain_badges.py [--dry-run]

What it does
------------
For every .ipynb file under the Module folders, walks each code cell and inserts
a small markdown "badge" cell directly above it with two links:
  - Watch Video: a placeholder link (VIDEO_LINK_HERE) to be filled in later
    once the per-cell explainer videos are recorded.
  - Explain with ChatGPT: opens a new ChatGPT chat pre-filled with that cell's
    exact code and a prompt asking for a step-by-step, beginner-friendly
    explanation.

Cells that don't need an explanation badge are skipped automatically:
  - cells with no real code (comments/blank only)
  - cells that are just a lone `pass` placeholder (student scratch space)
  - cells that contain `# TODO` markers (unfinished skeleton the student must
    still complete -- nothing to explain yet)

Each notebook's original JSON indent width is auto-detected and preserved so
re-writing a file doesn't produce a noisy whole-file diff.

Re-running the script is safe and self-healing: if a code cell is already
preceded by a badge cell, its ChatGPT link is refreshed to match the code
cell's current content, and a Watch Video badge is added if it's missing.
Any real video link you've already filled in (anything other than the
VIDEO_LINK_HERE placeholder) is preserved as-is.
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
VIDEO_PLACEHOLDER = "VIDEO_LINK_HERE"
VIDEO_HREF_RE = re.compile(
    r'<a href="([^"]+)"><img src="https://img\.shields\.io/badge/Watch_Video'
)


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


def is_badge_cell(cell) -> bool:
    if not cell or cell.get("cell_type") != "markdown":
        return False
    src = "".join(cell.get("source", []))
    return BADGE_MARKER in src


def existing_video_url(cell) -> str:
    src = "".join(cell.get("source", []))
    m = VIDEO_HREF_RE.search(src)
    if m:
        return m.group(1)
    return VIDEO_PLACEHOLDER


def make_badge_cell(code_source: str, video_url: str = VIDEO_PLACEHOLDER, cell_id: str = None) -> dict:
    prompt = PROMPT_PREFIX + code_source
    encoded = urllib.parse.quote_plus(prompt)
    chatgpt_url = "https://chatgpt.com/?q=" + encoded

    html = (
        '<div style="display:flex; align-items:center; justify-content:space-between; '
        'border-left:4px solid #999; border-radius:6px; background:rgba(153,153,153,0.08); '
        'padding:8px 20px 8px 16px; margin:6px 0;">\n'
        "  <span>&#128269;&nbsp;<b>Stuck on this code?</b></span>\n"
        '  <span style="display:flex; align-items:center; gap:8px;">\n'
        f'    <a href="{video_url}"><img src="https://img.shields.io/badge/Watch_Video-red?style=flat&amp;logo=youtube&amp;logoColor=white" '
        'alt="Watch the video" style="height:26px; vertical-align:middle;"></a>\n'
        f'    <a href="{chatgpt_url}"><img src="https://img.shields.io/badge/%F0%9F%A4%96_Explain_with_ChatGPT-10a37f?style=flat" '
        'alt="Explain with ChatGPT" style="height:26px; vertical-align:middle;"></a>\n'
        "  </span>\n"
        "</div>"
    )

    return {
        "cell_type": "markdown",
        "id": cell_id or secrets.token_hex(4),
        "metadata": {},
        "source": html.splitlines(keepends=True),
    }


def process_notebook(path: str, dry_run: bool) -> tuple[int, int]:
    with open(path) as f:
        nb = json.load(f)

    cells = nb["cells"]
    new_cells = []
    added = 0
    updated = 0

    for i, cell in enumerate(cells):
        if cell["cell_type"] == "code":
            source = "".join(cell.get("source", []))
            prev_cell = cells[i - 1] if i > 0 else None
            if needs_badge(source):
                if is_badge_cell(prev_cell):
                    video_url = existing_video_url(prev_cell)
                    refreshed = make_badge_cell(source, video_url, cell_id=prev_cell.get("id"))
                    if refreshed["source"] != prev_cell.get("source"):
                        new_cells[-1] = refreshed
                        updated += 1
                else:
                    new_cells.append(make_badge_cell(source))
                    added += 1
        new_cells.append(cell)

    if (added or updated) and not dry_run:
        indent = sniff_indent(path)
        nb["cells"] = new_cells
        with open(path, "w") as f:
            json.dump(nb, f, indent=indent)
            f.write("\n")

    return added, updated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    total_added = 0
    total_updated = 0
    for path in sorted(glob.glob("Module */*.ipynb")):
        added, updated = process_notebook(path, args.dry_run)
        if added or updated:
            print(f"{path}: +{added} new, {updated} updated")
        total_added += added
        total_updated += updated

    verb = "Would touch" if args.dry_run else "Touched"
    print(f"\n{verb} {total_added} new badge(s) and {total_updated} updated badge(s).")


if __name__ == "__main__":
    main()
