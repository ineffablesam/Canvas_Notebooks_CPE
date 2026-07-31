"""
generate_videos.py
===================
Batch-drives the PyExplain pipeline (http://127.0.0.1:8000) for every
distinct "explain this code" video needed across the notebooks, then copies
the finished MP4 into every notebook folder that uses it as block_N.mp4.

Manual 3-step pipeline (matches PyExplain's own docs) instead of the
higher-level /api/job/create, so we get Cartesia narration WITHOUT paying
for a Cursor-agent authoring call per video:
    POST /api/analyze  -> scene script (instant, offline, deterministic)
    POST /api/narrate  -> Cartesia narration audio (background job)
    POST /api/render   -> final MP4, reusing the narrate job's audio dir

Resumable: progress is checkpointed to manifest['groups'][i]['status'] in
VIDEO_MANIFEST after every group, and re-running skips groups already 'done'.
Run with limited concurrency (WORKERS) to avoid hammering Cartesia / the
local Chromium renderer at once.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

BASE = "http://127.0.0.1:8000"
ROOT = "/Users/sam/Downloads/Canvas_Notebooks"
VIDEO_MANIFEST = "/tmp/video_manifest.json"
LOG_PATH = "/tmp/generate_videos.log"
WORKERS = int(os.environ.get("PYEXPLAIN_WORKERS", "4"))
# Cartesia account limit observed live: "Current limit: 2" concurrent TTS
# requests (429 above that). Only the narrate step hits Cartesia, so gate
# just that step at 2 concurrent while analyze/render (CPU/browser-bound,
# not Cartesia-bound) can still run with up to WORKERS threads in parallel.
CARTESIA_CONCURRENCY = int(os.environ.get("CARTESIA_CONCURRENCY", "2"))
NARRATE_SEM = threading.Semaphore(CARTESIA_CONCURRENCY)
POLL_INTERVAL = 2.0
JOB_TIMEOUT_S = 900  # 15 min ceiling per stage before we give up on a group
GIT_LOCK = threading.Lock()


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


def generate_one(group: dict) -> dict:
    gid = group["id"]
    code = group["code"]
    title = group["label"][:80]

    # 1. analyze -- instant, offline, deterministic (no Cursor agent call)
    r = requests.post(f"{BASE}/api/analyze", json={
        "code": code, "title": title, "script_mode": "auto", "framing": True,
    }, timeout=60)
    r.raise_for_status()
    script = r.json()
    if not script.get("ok"):
        raise RuntimeError(f"analyze failed: {script.get('error')}")
    script.setdefault("meta", {})["style"] = "stevens"

    # 2. narrate -- Cartesia voice, background job. Cartesia's account
    # concurrency limit is 2, so only 2 of these can be in flight at once
    # across all worker threads (the narrate job itself calls Cartesia
    # once per scene, sequentially, so holding the semaphore for the whole
    # job's duration keeps us at or under the limit).
    with NARRATE_SEM:
        r = requests.post(f"{BASE}/api/narrate", json={
            "script": script, "voice_provider": "cartesia",
        }, timeout=60)
        r.raise_for_status()
        jid = r.json()["job_id"]
        job = wait_for_job(jid, ok_statuses=("narrated",))
        narrated_script = job["script"]

    # 3. render -- reuse the narrate job_id so the audio dir is already there
    r = requests.post(f"{BASE}/api/render", json={
        "script": narrated_script, "job_id": jid, "with_audio": True, "engine": "auto",
    }, timeout=60)
    r.raise_for_status()
    render_jid = r.json()["job_id"]
    job = wait_for_job(render_jid, ok_statuses=("done",), timeout=JOB_TIMEOUT_S)

    video_src = os.path.join("/Users/sam/Desktop/CPE/PyExplain/jobs", render_jid, "video.mp4")
    if not os.path.exists(video_src):
        raise RuntimeError(f"render job {render_jid} reported done but video.mp4 missing")

    # 4. fan out to every notebook location that shares this exact code
    copied = []
    for m in group["members"]:
        dest_dir = m["notebook_dir"]
        if not os.path.isabs(dest_dir):
            dest_dir = os.path.join(ROOT, dest_dir)
        dest = os.path.join(dest_dir, f"block_{m['block_num']}.mp4")
        shutil.copy2(video_src, dest)
        copied.append(dest)

    return {"job_id": render_jid, "copied": copied}


def worker(group: dict) -> tuple:
    gid = group["id"]
    label = group["label"][:60]
    n = len(group["members"])
    for attempt in range(1, 4):
        try:
            log(f"group {gid:>3} ({n}x) start attempt {attempt}: {label}")
            result = generate_one(group)
            log(f"group {gid:>3} DONE -> {result['job_id']} copied to {len(result['copied'])} location(s)")
            rel_paths = [os.path.relpath(p, ROOT) for p in result["copied"]]
            where = rel_paths[0] if len(rel_paths) == 1 else f"{len(rel_paths)} notebooks"
            git_commit_and_push(
                result["copied"],
                f"Add explain-code video for block {group['id']}: {label} ({where})",
            )
            return gid, "done", result
        except Exception as e:
            log(f"group {gid:>3} attempt {attempt} FAILED: {e}")
            if attempt < 3:
                time.sleep(5 * attempt)
            else:
                log(f"group {gid:>3} GIVING UP after 3 attempts:\n{traceback.format_exc()}")
                return gid, "error", str(e)


def main():
    with open(VIDEO_MANIFEST) as f:
        manifest = json.load(f)
    groups = manifest["groups"]

    pending = [g for g in groups if g.get("status") != "done"]
    log(f"=== starting batch: {len(pending)}/{len(groups)} groups pending, {WORKERS} workers ===")

    by_id = {g["id"]: g for g in groups}
    done_count = 0
    error_count = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(worker, g): g["id"] for g in pending}
        for fut in as_completed(futures):
            gid, status, result = fut.result()
            by_id[gid]["status"] = status
            if status == "done":
                by_id[gid]["job_id"] = result["job_id"]
                done_count += 1
            else:
                by_id[gid]["error"] = result
                error_count += 1
            # checkpoint after every completion so we can resume on crash
            with open(VIDEO_MANIFEST, "w") as f:
                json.dump(manifest, f, indent=1)
            log(f"progress: {done_count} done, {error_count} errors, "
                f"{len(pending) - done_count - error_count} remaining")

    log(f"=== batch complete: {done_count} done, {error_count} errors ===")


if __name__ == "__main__":
    main()
