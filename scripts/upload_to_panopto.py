#!/usr/bin/env python3
"""Upload rendered explainer videos to Panopto and wire the links into notebooks.

Usage:
    python3 scripts/upload_to_panopto.py --dry-run          # preview, no calls made
    python3 scripts/upload_to_panopto.py --limit 2           # try just 2 videos first
    python3 scripts/upload_to_panopto.py                     # upload everything pending

What it does
------------
Every explainer video (block_N.mp4, one per code cell with a "Stuck on this
code?" badge) already lives locally next to its notebook, recorded in
/tmp/video_jobs.json by generate_videos.py -- the same manifest records which
notebook file, which cell index, and which block_num each video belongs to.
This script, per pending job:

  1. Resolves (or creates, on first use) a subfolder of PANOPTO_FOLDER_ID
     named after the video's module (e.g. "Module 5- Python Basics with AI
     Support"), so each module's videos land in their own folder instead of
     one big flat pile. Matched/created by name via the Folders REST API,
     so this is idempotent -- re-running never creates duplicate folders.
  2. Uploads that block_N.mp4 to Panopto as its own session, in that module
     folder, via Panopto's non-interactive REST Upload API: create a
     sessionUpload -> multipart-upload the video to the S3 endpoint Panopto
     hands back -> upload a UCS manifest XML describing it -> mark the
     sessionUpload complete -> poll until Panopto finishes processing and
     returns a SessionId.
  3. Builds that session's viewer URL (.../Panopto/Pages/Viewer.aspx?id=...)
     and rewrites the notebook's badge cell (the "Watch Video" link
     immediately above that code cell) to point at it, replacing whatever
     placeholder/link was there before -- reusing add_explain_badges.py's
     own badge-cell builder so the HTML stays byte-identical in style.

Auth
----
Needs a Panopto OAuth2 API Client (Panopto admin UI: System > API Clients).
Three grant types work here, chosen via PANOPTO_GRANT_TYPE:
  - "authcode" (Client Type = Server-side Web Application) [default,
    recommended]: opens your browser ONCE to log into Panopto the normal
    way (through your school's SSO if that's how you normally sign in --
    no password is ever seen or stored by this script), then caches a
    refresh token to /tmp so every later run is fully non-interactive.
    Uploads land with YOUR real creator permissions on the folder. This is
    Panopto's own recommended approach for service/script integrations.
  - "password" (Client Type = User Based Server Application): sends
    PANOPTO_USERNAME/PANOPTO_PASSWORD directly. Only works for native
    Panopto accounts -- if your school's Panopto uses SSO (common), this
    will NOT accept your normal SSO password and needs a special
    SHA-1 "auth code" derived from an admin-only Identity Provider setting
    instead. Avoid unless you know your account is SSO-free.
  - "client_credentials" (Client Type = Server Application): simplest, no
    user login, but the resulting token has no identity/permissions, so it
    can only see/create sessions in folders that are PUBLICLY viewable.
    Fine only for a scratch/public test folder.

Required environment variables:
    PANOPTO_SERVER          e.g. yourschool.hosted.panopto.com (no scheme)
    PANOPTO_CLIENT_ID
    PANOPTO_CLIENT_SECRET
    PANOPTO_FOLDER_ID       destination folder's GUID (see below)
    PANOPTO_GRANT_TYPE      "authcode" (default), "password", or
                            "client_credentials"
    PANOPTO_REDIRECT_PORT   only for authcode; local port for the one-time
                            login redirect (default 9127) -- must match the
                            Redirect URL registered on the API Client
    PANOPTO_USERNAME        required when PANOPTO_GRANT_TYPE=password
    PANOPTO_PASSWORD        required when PANOPTO_GRANT_TYPE=password

Finding PANOPTO_FOLDER_ID: open the destination folder in Panopto's web UI
(the one that backs the "Panopto Video" tab in this Canvas course) and look
at the URL, e.g. .../Panopto/Pages/Sessions/List.aspx#folderID=%22xxxxxxxx-
xxxx-xxxx-xxxx-xxxxxxxxxxxx%22 -- the GUID inside folderID is this value.

Quickest setup: copy scripts/.env.panopto.example to scripts/.env.panopto,
fill in the blanks (it's gitignored), then:
    set -a; source scripts/.env.panopto; set +a
    python3 scripts/upload_to_panopto.py --dry-run

Dependencies: requests + boto3 (Panopto's upload endpoint speaks the S3
multipart-upload protocol) + requests_oauthlib (authcode grant only).
Install with:
    pip install -r scripts/requirements.txt

Resumable: after every successful upload, this checkpoints
panopto_status/panopto_session_id/panopto_url onto that job in
/tmp/video_jobs.json, so re-running the script skips jobs already uploaded
(matches generate_videos.py's own manifest-checkpointing convention).
"""
import argparse
import copy
import json
import os
import pickle
import sys
import threading
import time
import traceback
import webbrowser
import xml.sax.saxutils as saxutils
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingTCPServer

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import add_explain_badges as badges

ROOT = "/Users/sam/Downloads/Canvas_Notebooks"
JOBS_MANIFEST = "/tmp/video_jobs.json"

PART_SIZE = 5 * 1024 * 1024  # S3 multipart minimum; Panopto caps parts at 25MB
WORKERS = int(os.environ.get("PANOPTO_WORKERS", "3"))
POLL_INTERVAL = 5.0
PROCESS_TIMEOUT_S = 1800  # 30 min ceiling for Panopto to finish processing one video

MAX_ATTEMPTS = 5
BACKOFF_S = [10, 20, 40, 80]

MANIFEST_LOCK = threading.Lock()

# Populated by load_config() -- kept as None at import time so --dry-run and
# --help work without any Panopto credentials configured.
SERVER = CLIENT_ID = CLIENT_SECRET = FOLDER_ID = GRANT_TYPE = USERNAME = PASSWORD = None
REDIRECT_PORT = None

UCS_MANIFEST_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<Session xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns="http://tempuri.org/UniversalCaptureSpecification/v1">
  <Title>{title}</Title>
  <Description>{description}</Description>
  <Date>{date}</Date>
  <ThumbnailTime>PT5S</ThumbnailTime>
  <Videos>
    <Video>
      <Start>PT0S</Start>
      <File>{filename}</File>
      <Type>Primary</Type>
    </Video>
  </Videos>
</Session>
"""


def load_config():
    global SERVER, CLIENT_ID, CLIENT_SECRET, FOLDER_ID, GRANT_TYPE, USERNAME, PASSWORD, REDIRECT_PORT
    required = ["PANOPTO_SERVER", "PANOPTO_CLIENT_ID", "PANOPTO_CLIENT_SECRET", "PANOPTO_FOLDER_ID"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        sys.exit(
            "Missing required environment variable(s): " + ", ".join(missing) +
            "\nSee the module docstring (top of scripts/upload_to_panopto.py) for what each one is."
        )
    SERVER = os.environ["PANOPTO_SERVER"]
    CLIENT_ID = os.environ["PANOPTO_CLIENT_ID"]
    CLIENT_SECRET = os.environ["PANOPTO_CLIENT_SECRET"]
    FOLDER_ID = os.environ["PANOPTO_FOLDER_ID"]
    GRANT_TYPE = os.environ.get("PANOPTO_GRANT_TYPE", "authcode")
    USERNAME = os.environ.get("PANOPTO_USERNAME")
    PASSWORD = os.environ.get("PANOPTO_PASSWORD")
    REDIRECT_PORT = int(os.environ.get("PANOPTO_REDIRECT_PORT", "9127"))
    if GRANT_TYPE == "password" and not (USERNAME and PASSWORD):
        sys.exit("PANOPTO_GRANT_TYPE=password also requires PANOPTO_USERNAME and PANOPTO_PASSWORD.")
    if GRANT_TYPE == "authcode":
        try:
            import requests_oauthlib  # noqa: F401
        except ImportError:
            sys.exit("Missing dependency 'requests_oauthlib'. Run: pip install -r scripts/requirements.txt")
    try:
        import boto3  # noqa: F401
    except ImportError:
        sys.exit("Missing dependency 'boto3'. Run: pip install -r scripts/requirements.txt")


class _RedirectHandler(BaseHTTPRequestHandler):
    """Catches the single browser redirect at the end of the one-time login."""

    def do_GET(self):
        self.server.last_get_path = self.path
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(b"Panopto login received. You can close this tab and return to the terminal.")

    def log_message(self, *args):
        pass


class _RedirectServer(ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(self, port):
        self.last_get_path = None
        super().__init__(("", port), _RedirectHandler)


class PanoptoAuth:
    """OAuth2 token holder; refreshes itself on expiry or a 401/403 response.

    - authcode: one interactive browser login (through your school's normal
      SSO if applicable), then a cached refresh token for every later run.
    - password / client_credentials: direct, non-interactive token request.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.token = None
        self.expires_at = 0
        if GRANT_TYPE == "authcode":
            self._token_cache_path = f"/tmp/panopto_token_{SERVER}_{CLIENT_ID}.cache"
            self._redirect_url = f"http://localhost:{REDIRECT_PORT}/redirect"
            self._authcode_login_or_refresh()
        else:
            self._refresh_direct()

    def _refresh_direct(self):
        data = {"grant_type": GRANT_TYPE, "scope": "openid api"}
        if GRANT_TYPE == "password":
            data.update({"username": USERNAME, "password": PASSWORD})
        resp = requests.post(
            f"https://{SERVER}/Panopto/oauth2/connect/token",
            data=data, auth=(CLIENT_ID, CLIENT_SECRET), timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        self.token = body["access_token"]
        self.expires_at = time.time() + body.get("expires_in", 3600) - 60

    def _authcode_login_or_refresh(self):
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"  # localhost redirect is http, not https
        from requests_oauthlib import OAuth2Session

        token = None
        try:
            with open(self._token_cache_path, "rb") as f:
                token = pickle.load(f)
        except Exception:
            pass

        token_endpoint = f"https://{SERVER}/Panopto/oauth2/connect/token"
        if token:
            try:
                session = OAuth2Session(CLIENT_ID, token=token)
                new_token = session.refresh_token(
                    token_endpoint, client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                )
                self._store_token(new_token)
                return
            except Exception as e:
                print(f"Cached Panopto token could not be refreshed ({e}); logging in again.")

        session = OAuth2Session(
            CLIENT_ID, scope=["openid", "api", "offline_access"], redirect_uri=self._redirect_url,
        )
        auth_url, _ = session.authorization_url(f"https://{SERVER}/Panopto/oauth2/connect/authorize")
        print(f"\nOpening your browser to log into Panopto once:\n{auth_url}\n")
        webbrowser.open_new_tab(auth_url)

        with _RedirectServer(REDIRECT_PORT) as httpd:
            print(f"Waiting for the login redirect on http://localhost:{REDIRECT_PORT} ...", flush=True)
            httpd.handle_request()
            while httpd.last_get_path is None:
                time.sleep(0.2)
            redirected_path = httpd.last_get_path

        print("Redirect received, exchanging it for an access token...", flush=True)
        new_token = session.fetch_token(
            token_endpoint, client_secret=CLIENT_SECRET,
            authorization_response=f"http://localhost:{REDIRECT_PORT}{redirected_path}",
        )
        self._store_token(new_token)
        print("Logged in. This token will be reused/refreshed automatically on future runs.\n", flush=True)

    def _store_token(self, token):
        self.token = token["access_token"]
        self.expires_at = time.time() + token.get("expires_in", 3600) - 60
        with open(self._token_cache_path, "wb") as f:
            pickle.dump(token, f)

    def headers(self, force_refresh=False):
        with self._lock:
            if force_refresh or time.time() >= self.expires_at:
                if GRANT_TYPE == "authcode":
                    self._authcode_login_or_refresh()
                else:
                    self._refresh_direct()
            return {"Authorization": f"Bearer {self.token}"}


def _request(auth, method, url, **kwargs):
    """requests wrapper that retries exactly once after refreshing the token
    on 401/403 (mirrors Panopto's own sample client)."""
    headers = kwargs.pop("headers", {})
    resp = requests.request(method, url, headers={**auth.headers(), **headers}, timeout=60, **kwargs)
    if resp.status_code in (401, 403):
        resp = requests.request(method, url, headers={**auth.headers(force_refresh=True), **headers}, timeout=60, **kwargs)
    resp.raise_for_status()
    return resp


def create_session_upload(auth, folder_id):
    resp = _request(
        auth, "POST", f"https://{SERVER}/Panopto/PublicAPI/REST/sessionUpload",
        json={"FolderId": folder_id}, headers={"content-type": "application/json"},
    )
    return resp.json()


MODULE_FOLDER_LOCK = threading.Lock()
_module_folder_cache = {}  # module name -> folder GUID, populated lazily per run


def _list_child_folders(auth, parent_id):
    all_folders = []
    page = 0
    while True:
        resp = _request(
            auth, "GET", f"https://{SERVER}/Panopto/api/v1/folders/{parent_id}/children",
            params={"pageNumber": page},
        )
        results = resp.json().get("Results", [])
        if not results:
            break
        all_folders.extend(results)
        page += 1
    return all_folders


def get_or_create_module_folder(auth, module_name):
    """Finds (or creates, on first use) a subfolder of PANOPTO_FOLDER_ID named
    after this module, so each module's videos land in their own folder
    instead of one big flat pile. Cached in-memory per run; re-runs just
    re-discover the same folder by name (idempotent -- safe to re-run)."""
    if not module_name:
        return FOLDER_ID
    with MODULE_FOLDER_LOCK:
        if module_name in _module_folder_cache:
            return _module_folder_cache[module_name]

        existing = next((f for f in _list_child_folders(auth, FOLDER_ID) if f.get("Name") == module_name), None)
        if existing:
            folder_id = existing["Id"]
        else:
            print(f"Creating Panopto folder '{module_name}'...", flush=True)
            resp = _request(
                auth, "POST", f"https://{SERVER}/Panopto/api/v1/folders",
                json={"Name": module_name, "Parent": FOLDER_ID}, headers={"content-type": "application/json"},
            )
            folder_id = resp.json()["Id"]

        _module_folder_cache[module_name] = folder_id
        return folder_id


def s3_client_for(upload_target):
    import boto3
    # upload_target looks like https://{service endpoint}/{bucket}/{prefix}
    elements = upload_target.split("/")
    service_endpoint = "/".join(elements[:-2])
    bucket = elements[-2]
    prefix = elements[-1]
    client = boto3.session.Session().client(
        service_name="s3", endpoint_url=service_endpoint,
        aws_access_key_id="dummy", aws_secret_access_key="dummy",
        config=boto3.session.Config(signature_version="s3"),
    )
    return client, bucket, prefix


def multipart_upload(s3, bucket, object_key, file_path):
    mpu = s3.create_multipart_upload(Bucket=bucket, Key=object_key)
    mpu_id = mpu["UploadId"]
    parts = []
    with open(file_path, "rb") as f:
        i = 1
        while True:
            data = f.read(PART_SIZE)
            if not data:
                break
            part = s3.upload_part(Body=data, Bucket=bucket, Key=object_key, UploadId=mpu_id, PartNumber=i)
            parts.append({"PartNumber": i, "ETag": part["ETag"]})
            i += 1
    s3.complete_multipart_upload(Bucket=bucket, Key=object_key, UploadId=mpu_id, MultipartUpload={"Parts": parts})


def upload_manifest_xml(s3, bucket, prefix, title, filename):
    xml = UCS_MANIFEST_TEMPLATE.format(
        title=saxutils.escape(title),
        description=saxutils.escape(f"Explainer video for {filename}"),
        date=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "-00:00",
        filename=filename,
    )
    tmp_path = f"/tmp/panopto_manifest_{threading.get_ident()}.xml"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(xml)
    try:
        multipart_upload(s3, bucket, f"{prefix}/upload_manifest.xml", tmp_path)
    finally:
        os.remove(tmp_path)


def finish_upload(auth, session_upload):
    payload = copy.copy(session_upload)
    payload["State"] = 1  # UploadComplete -> tells Panopto to start processing
    _request(
        auth, "PUT", f"https://{SERVER}/Panopto/PublicAPI/REST/sessionUpload/{session_upload['ID']}",
        json=payload, headers={"content-type": "application/json"},
    )


def poll_until_complete(auth, upload_id):
    url = f"https://{SERVER}/Panopto/PublicAPI/REST/sessionUpload/{upload_id}"
    start = time.time()
    while True:
        body = _request(auth, "GET", url).json()
        if body.get("State") == 4:  # Complete
            return body
        if body.get("State") == 5:  # Error, per Panopto's SessionUploadState enum
            raise RuntimeError(f"Panopto reported an upload error: {body}")
        if time.time() - start > PROCESS_TIMEOUT_S:
            raise TimeoutError(f"Panopto processing timed out (state={body.get('State')})")
        time.sleep(POLL_INTERVAL)


def find_badge_idx(cells, code_idx):
    """The badge cell is normally right above the code cell (code_idx - 1);
    search a few cells back in case indices drifted since the manifest was
    built (e.g. a cell was inserted/removed by hand)."""
    for i in range(code_idx - 1, max(code_idx - 5, -1), -1):
        if badges.is_badge_cell(cells[i]):
            return i
    return None


def patch_notebook_badge(nb_path, code_cell_idx, video_url):
    abs_path = nb_path if os.path.isabs(nb_path) else os.path.join(ROOT, nb_path)
    with open(abs_path) as f:
        nb = json.load(f)
    cells = nb["cells"]
    badge_idx = find_badge_idx(cells, code_cell_idx)
    if badge_idx is None:
        raise RuntimeError(f"No badge cell found above cell {code_cell_idx} in {nb_path}")
    refreshed = badges.make_badge_cell(video_url, cell_id=cells[badge_idx].get("id"))
    if refreshed["source"] == cells[badge_idx].get("source"):
        return
    cells[badge_idx] = refreshed
    indent = badges.sniff_indent(abs_path)
    with open(abs_path, "w") as f:
        json.dump(nb, f, indent=indent)
        f.write("\n")


def upload_one(auth, job) -> tuple:
    """Uploads job's video to Panopto, waits for processing, patches the
    notebook's badge cell, and returns (session_id, viewer_url)."""
    notebook_dir = job["notebook_dir"]
    abs_dir = notebook_dir if os.path.isabs(notebook_dir) else os.path.join(ROOT, notebook_dir)
    mp4_path = os.path.join(abs_dir, f"block_{job['block_num']}.mp4")
    if not os.path.exists(mp4_path):
        raise FileNotFoundError(mp4_path)

    tag = f"[job {job['id']}]"
    title = (
        f"{job.get('module', '')}: {os.path.basename(notebook_dir)} "
        f"(block {job['block_num']}/{job.get('total_blocks', '?')}) - {job.get('label', '')}"
    )[:200]

    folder_id = get_or_create_module_folder(auth, job.get("module", ""))

    print(f"{tag} creating Panopto session upload...", flush=True)
    session_upload = create_session_upload(auth, folder_id)
    s3, bucket, prefix = s3_client_for(session_upload["UploadTarget"])
    filename = os.path.basename(mp4_path)

    size_mb = os.path.getsize(mp4_path) / (1024 * 1024)
    print(f"{tag} uploading {filename} ({size_mb:.1f} MB)...", flush=True)
    multipart_upload(s3, bucket, f"{prefix}/{filename}", mp4_path)

    print(f"{tag} uploading manifest and finishing upload...", flush=True)
    upload_manifest_xml(s3, bucket, prefix, title, filename)
    finish_upload(auth, session_upload)

    print(f"{tag} waiting for Panopto to finish processing (can take a few minutes)...", flush=True)
    result = poll_until_complete(auth, session_upload["ID"])

    session_id = result.get("SessionId")
    if not session_id:
        raise RuntimeError(f"Panopto finished processing but returned no SessionId: {result}")
    viewer_url = f"https://{SERVER}/Panopto/Pages/Viewer.aspx?id={session_id}"

    print(f"{tag} patching notebook badge cell...", flush=True)
    patch_notebook_badge(job["file"], job["cell"], viewer_url)
    return session_id, viewer_url


def checkpoint(manifest):
    with MANIFEST_LOCK:
        with open(JOBS_MANIFEST, "w") as f:
            json.dump(manifest, f, indent=1)


def worker(auth, job, manifest):
    where = f"{job['notebook_dir']}/block_{job['block_num']}.mp4"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            session_id, viewer_url = upload_one(auth, job)
            job["panopto_status"] = "done"
            job["panopto_session_id"] = session_id
            job["panopto_url"] = viewer_url
            checkpoint(manifest)
            return job["id"], "done", viewer_url
        except Exception as e:
            print(f"[job {job['id']}] attempt {attempt} FAILED ({where}): {e}", flush=True)
            if attempt < MAX_ATTEMPTS:
                time.sleep(BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)])
            else:
                job["panopto_status"] = "error"
                job["panopto_error"] = str(e)
                checkpoint(manifest)
                print(traceback.format_exc(), flush=True)
                return job["id"], "error", str(e)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Preview pending uploads without calling Panopto or touching notebooks.")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N pending jobs (try 1-2 before running the full batch).")
    args = parser.parse_args()

    with open(JOBS_MANIFEST) as f:
        manifest = json.load(f)
    jobs = manifest["jobs"]
    all_pending = [j for j in jobs if j.get("panopto_status") != "done"]
    pending = all_pending[:args.limit] if args.limit else all_pending

    if args.dry_run:
        for j in pending:
            print(f"Would upload {j['notebook_dir']}/block_{j['block_num']}.mp4 "
                  f"-> patch cell {j['cell']} of {j['file']}")
        print(f"\nWould upload {len(pending)} video(s) this run "
              f"({len(all_pending)} pending total, "
              f"{len(jobs) - len(all_pending)} already marked done).")
        return

    if not pending:
        print("Nothing pending -- all jobs already have panopto_status == 'done'.")
        return

    load_config()
    auth = PanoptoAuth()

    done_count = error_count = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(worker, auth, j, manifest): j["id"] for j in pending}
        for fut in as_completed(futures):
            jid, status, result = fut.result()
            if status == "done":
                done_count += 1
                print(f"[job {jid}] DONE -> {result}", flush=True)
            else:
                error_count += 1
            print(f"progress: {done_count} done, {error_count} errors, "
                  f"{len(pending) - done_count - error_count} remaining", flush=True)

    print(f"\n=== batch complete: {done_count} done, {error_count} errors ===")


if __name__ == "__main__":
    main()
