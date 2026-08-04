"""
generate_videos.py
===================
Batch-drives the PyExplain pipeline (http://127.0.0.1:8000) for every code
block across the notebooks that needs its own explanation video, then copies
the finished MP4 into that notebook's folder as block_N.mp4.

One job PER (notebook, cell) instance -- not deduped by identical code -- so
every video's narration is authored WITH the surrounding notebook context
(the lesson markdown and earlier code cells that came before it, see
notebook_context.py). Two notebooks can contain byte-identical code but sit
in completely different lessons (different variable provenance, different
position in the notebook -- e.g. one might be the final wrap-up block and the
other an early warm-up), so they need their own narration to genuinely read
like a human course author explaining THAT block in THAT notebook, not a
generic explainer reused wherever the code happens to match.

Manual 3-step pipeline (matches PyExplain's own docs) using the Cursor-agent
-authored /api/author instead of the deterministic /api/analyze:
analyze()'s template narration ("This line runs an action: ...") and its
sandboxed trace_execution() (which throws false NameErrors on any block that
references a variable from an earlier notebook cell, then bakes that "error"
into the outro) were both unacceptable for real narration quality. /api/author
asks a real Cursor agent (sonnet-4.5) to read the code (PLUS the notebook
context brief built per job) and write the script -- no code execution at
all, so the false-error class of bug can't happen, and the explanation is
contextual instead of templated.
    POST /api/author   -> LLM-authored scene script (Cursor agent call)
    POST /api/narrate  -> Synthesia narration audio (background job)
    POST /api/render   -> final MP4, reusing the narrate job's audio dir

Resumable: progress is checkpointed to manifest['jobs'][i]['status'] in
JOBS_MANIFEST after every job, and re-running skips jobs already 'done'.
Run with limited concurrency (WORKERS) to avoid hammering the local Chromium
renderer / Cursor agent bridge / Synthesia all at once.
"""
import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import notebook_context as nc

BASE = "http://127.0.0.1:8000"
ROOT = "/Users/sam/Downloads/Canvas_Notebooks"
JOBS_MANIFEST = "/tmp/video_jobs.json"
LOG_PATH = "/tmp/generate_videos.log"
WORKERS = int(os.environ.get("PYEXPLAIN_WORKERS", "4"))
VOICE_PROVIDER = os.environ.get("VOICE_PROVIDER", "synthesia")
# Probed live: 8 concurrent Synthesia video-creation calls (test mode) all
# returned 201 with no rate-limit/concurrency errors, so this enterprise
# account tolerates far more than Cartesia's old limit of 2.
NARRATE_CONCURRENCY = int(os.environ.get("NARRATE_CONCURRENCY", "6"))
NARRATE_SEM = threading.Semaphore(NARRATE_CONCURRENCY)
POLL_INTERVAL = 2.0
JOB_TIMEOUT_S = 1800  # 30 min ceiling per stage -- denser code (more branches/
# scenes) can legitimately take >15 min to render frame-by-frame; a real
# case timed out at 15 min while sitting at 95% and finished moments later,
# wasting a full retry-from-scratch. Better to wait longer than to restart.
GIT_LOCK = threading.Lock()

# Mirrors director.py's DEFAULT_BRIEF (kept local -- director.py imports
# cursor_sdk, which may not be installed in this script's environment, and
# we only talk to PyExplain over HTTP). Extended with an explicit pointer to
# the per-job notebook context appended below it.
BASE_BRIEF = textwrap.dedent("""
    Explain this exact Python code snippet directly and fully, the way it will be
    used: as a single cell/block from a Colab notebook (often just one of several
    short blocks in the notebook, each with its own separate video). Go straight
    into the code, top to bottom, in order: what each line does, what each
    variable holds, and what the code actually prints. Do NOT invent a
    movie/story hook, a real-world analogy, or a "pause and predict" game, and
    do NOT pad the intro before getting to the code. Cover every concept that
    appears (assignment, data types, lists, built-in calls like type() and
    len(), etc.) plainly and precisely, in plain language a learner can follow.
    Keep it tight and straightforward: use as many or as few scenes as this
    snippet actually needs, don't stretch a short snippet into a long story.

    You are also given NOTEBOOK CONTEXT below: the real lesson text and earlier
    code cells from the exact notebook this block comes from. Use it to
    genuinely understand the block the way the person who wrote this lesson
    would -- where a variable came from, what the lesson is building toward,
    whether this is the wrap-up block, etc. -- and let that understanding
    sharpen the explanation. Do not narrate or re-explain the earlier context
    itself; the video is only about the target code block.
""").strip()


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def git_commit_and_push(paths: list, message: str):
    """Commit + push the given (absolute) paths. Serialized across threads
    since git's index is a single shared file. Plain author, no co-author
    trailer -- these are project commits, not chat-authored ones."""
    with GIT_LOCK:
        try:
            subprocess.run(["git", "add", "--"] + paths, cwd=ROOT, check=True,
                           capture_output=True, text=True)
            commit = subprocess.run(["git", "commit", "-m", message], cwd=ROOT,
                                    capture_output=True, text=True)
            if commit.returncode != 0 and "nothing to commit" not in commit.stdout:
                log(f"git commit warning: {commit.stdout.strip()} {commit.stderr.strip()}")
                return
            push = subprocess.run(["git", "push", "origin", "HEAD"], cwd=ROOT,
                                  capture_output=True, text=True, timeout=60)
            if push.returncode != 0:
                log(f"git push FAILED (will stay committed locally): {push.stderr.strip()[:300]}")
            else:
                log(f"git pushed: {message}")
        except Exception as e:
            log(f"git commit/push error: {e}")


def wait_for_job(jid: str, ok_statuses, timeout=JOB_TIMEOUT_S):
    start = time.time()
    while True:
        r = requests.get(f"{BASE}/api/job/{jid}", timeout=30)
        r.raise_for_status()
        job = r.json()
        status = job.get("status")
        if status in ok_statuses:
            return job
        if status in ("error", "stopped"):
            raise RuntimeError(f"job {jid} failed: {job.get('error')}")
        if time.time() - start > timeout:
            raise TimeoutError(f"job {jid} timed out in status={status}")
        time.sleep(POLL_INTERVAL)


def generate_one(job: dict) -> dict:
    code = job["code"]
    title = job["label"][:80]
    nb_path = job["file"]
    if not os.path.isabs(nb_path):
        nb_path = os.path.join(ROOT, nb_path)

    context = nc.build_context(
        nb_path, job["cell"], job["module"], job["block_num"], job["total_blocks"],
    )
    brief = f"{BASE_BRIEF}\n\n--- NOTEBOOK CONTEXT ---\n{context}"

    # 1. author -- real Cursor agent (sonnet-4.5) reads the code + notebook
    # context and writes a contextual scene script; ~30-65s (an actual model
    # call), unlike the old instant/offline /api/analyze.
    r = requests.post(f"{BASE}/api/author", json={
        "code": code, "title": title, "brief": brief, "target_sec": 90,
    }, timeout=240)
    r.raise_for_status()
    script = r.json()
    if not script.get("ok"):
        raise RuntimeError(f"author failed: {script.get('error')}")
    script.setdefault("meta", {})["style"] = "stevens"

    # 2. narrate -- background job. Concurrency-gated across all worker
    # threads regardless of provider (see VOICE_PROVIDER / NARRATE_CONCURRENCY).
    with NARRATE_SEM:
        r = requests.post(f"{BASE}/api/narrate", json={
            "script": script, "voice_provider": VOICE_PROVIDER,
        }, timeout=60)
        r.raise_for_status()
        jid = r.json()["job_id"]
        job_status = wait_for_job(jid, ok_statuses=("narrated",))
        narrated_script = job_status["script"]

    # 3. render -- reuse the narrate job_id so the audio dir is already there
    r = requests.post(f"{BASE}/api/render", json={
        "script": narrated_script, "job_id": jid, "with_audio": True, "engine": "auto",
    }, timeout=60)
    r.raise_for_status()
    render_jid = r.json()["job_id"]
    wait_for_job(render_jid, ok_statuses=("done",), timeout=JOB_TIMEOUT_S)

    video_src = os.path.join("/Users/sam/Desktop/CPE/PyExplain/jobs", render_jid, "video.mp4")
    if not os.path.exists(video_src):
        raise RuntimeError(f"render job {render_jid} reported done but video.mp4 missing")

    dest_dir = job["notebook_dir"]
    if not os.path.isabs(dest_dir):
        dest_dir = os.path.join(ROOT, dest_dir)
    dest = os.path.join(dest_dir, f"block_{job['block_num']}.mp4")
    shutil.copy2(video_src, dest)

    return {"job_id": render_jid, "copied": dest}


MAX_ATTEMPTS = 6
# Covers a real internet blip: 10s, 20s, 40s, 80s, 120s between attempts
# (~4.5 min of backoff total) instead of the old 5s/10s that burned through
# all retries in 15s during a longer outage.
BACKOFF_S = [10, 20, 40, 80, 120]


def _network_ok() -> bool:
    # NOTE: this must check real internet/DNS, not our own local server --
    # {BASE} is http://127.0.0.1:8000, which stays up fine during an actual
    # internet outage, so checking it here made this a no-op (learned the
    # hard way: a real disconnect burned through all 6 attempts in <1 min
    # instead of waiting, because this always returned True).
    try:
        requests.get("https://api.synthesia.io", timeout=10)
        return True
    except Exception:
        return False


def worker(job: dict) -> tuple:
    jid = job["id"]
    label = job["label"][:60]
    where = f"{job['notebook_dir']}/block_{job['block_num']}.mp4"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            log(f"job {jid:>3} start attempt {attempt}: {label} -> {where}")
            result = generate_one(job)
            log(f"job {jid:>3} DONE -> {result['job_id']} -> {result['copied']}")
            git_commit_and_push(
                [result["copied"]],
                f"Add explain-code video for {where}: {label}",
            )
            return jid, "done", result
        except Exception as e:
            log(f"job {jid:>3} attempt {attempt} FAILED: {e}")
            if attempt < MAX_ATTEMPTS:
                wait = BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)]
                # If it looks like a network outage, keep waiting in longer
                # increments (without burning an attempt) until it's back,
                # instead of racing through attempts while offline.
                while not _network_ok():
                    log(f"job {jid:>3}: network/server unreachable, waiting 15s before retrying...")
                    time.sleep(15)
                time.sleep(wait)
            else:
                log(f"job {jid:>3} GIVING UP after {MAX_ATTEMPTS} attempts:\n{traceback.format_exc()}")
                return jid, "error", str(e)


def main():
    with open(JOBS_MANIFEST) as f:
        manifest = json.load(f)
    jobs = manifest["jobs"]

    pending = [j for j in jobs if j.get("status") != "done"]
    log(f"=== starting batch: {len(pending)}/{len(jobs)} jobs pending, {WORKERS} workers ===")

    by_id = {j["id"]: j for j in jobs}
    done_count = 0
    error_count = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(worker, j): j["id"] for j in pending}
        for fut in as_completed(futures):
            jid, status, result = fut.result()
            by_id[jid]["status"] = status
            if status == "done":
                by_id[jid]["job_id"] = result["job_id"]
                done_count += 1
            else:
                by_id[jid]["error"] = result
                error_count += 1
            # checkpoint after every completion so we can resume on crash
            with open(JOBS_MANIFEST, "w") as f:
                json.dump(manifest, f, indent=1)
            log(f"progress: {done_count} done, {error_count} errors, "
                f"{len(pending) - done_count - error_count} remaining")

    log(f"=== batch complete: {done_count} done, {error_count} errors ===")


if __name__ == "__main__":
    main()
