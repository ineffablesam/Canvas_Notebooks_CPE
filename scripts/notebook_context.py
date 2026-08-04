"""
notebook_context.py
====================
Builds a "what came before this cell" context blob from the *actual* notebook
file, so the video-authoring agent understands a code block the way a human
who wrote the whole lesson would: what the notebook has already taught, what
variables earlier cells defined, and where in the notebook this block sits
(e.g. "this is the last exercise in the notebook").

Without this, the agent only ever sees an isolated snippet -- it can't know
that `features_df` was built two cells earlier, or that this is a wrap-up
block, so its explanation reads generically instead of like real course
narration.
"""
import json
import os
import re

_BADGE_MARKERS = ("Stuck on this code", "<table")
_ATTACHMENT_IMG = re.compile(r"!\[[^\]]*\]\(attachment:[^)]*\)")


def _is_badge_cell(src: str) -> bool:
    s = src.strip()
    return any(marker in src for marker in _BADGE_MARKERS) and s.startswith("<table")


def _clean_markdown(src: str) -> str:
    text = _ATTACHMENT_IMG.sub("", src)
    return text.strip()


def load_cells(nb_path: str) -> list:
    with open(nb_path) as f:
        nb = json.load(f)
    return nb["cells"]


def notebook_title(cells: list) -> str:
    for c in cells:
        if c.get("cell_type") == "markdown":
            src = "".join(c.get("source", []))
            if _is_badge_cell(src):
                continue
            first_line = _clean_markdown(src).splitlines()[0] if _clean_markdown(src) else ""
            return first_line.lstrip("#").strip()
    return ""


def build_context(nb_path: str, cell_idx: int, module_name: str,
                   block_num: int, total_blocks: int, max_chars: int = 7000) -> str:
    """Everything a human course author would already know before writing an
    explanation for the code cell at `cell_idx`: the lesson's markdown so far
    and any earlier code cells (for variable/definition provenance)."""
    cells = load_cells(nb_path)
    title = notebook_title(cells)

    parts = []
    for c in cells[:cell_idx]:
        src = "".join(c.get("source", []))
        if not src.strip():
            continue
        if c.get("cell_type") == "markdown":
            if _is_badge_cell(src):
                continue
            text = _clean_markdown(src)
            if text:
                parts.append(f"[LESSON TEXT]\n{text}")
        elif c.get("cell_type") == "code":
            parts.append(f"[EARLIER CODE CELL]\n{src.strip()}")

    body = "\n\n".join(parts)
    if len(body) > max_chars:
        # keep the most recent context (closest to the target cell) -- that's
        # what actually explains variable provenance for THIS block.
        body = "...(earlier notebook content omitted)...\n\n" + body[-max_chars:]

    position = (
        f"This is the LAST of the {total_blocks} code blocks that get their own "
        f"explanation video in this notebook (block {block_num} of {total_blocks})."
        if block_num == total_blocks else
        f"This is block {block_num} of {total_blocks} code blocks that get their "
        f"own explanation video in this notebook."
    )

    header = f'Notebook: "{title}" (module: {module_name}).' if title else f"Module: {module_name}."

    return (
        f"{header} {position}\n\n"
        "Below is everything in this notebook BEFORE the target code block "
        "(lesson instructions the student already read, and any earlier code "
        "cells), given ONLY so you understand where variables/data came from "
        "and how this block fits the lesson. Do not re-explain or narrate any "
        "of this earlier material -- the video is about the target block only. "
        "Reference it only when it genuinely helps (e.g. \"features_df, built "
        "in an earlier cell, ...\" or noting this wraps up the notebook).\n\n"
        f"{body if body else '(this is the first cell in the notebook -- no earlier context)'}"
    )
