"""Publish the current working tree to GitHub through the REST API.

`git push` cannot be used from this machine: github.com:443 is blocked here,
while api.github.com and uploads.github.com answer. So the commit is assembled
server-side with the Git Data API:

    blobs -> tree (based on the existing remote tree) -> commit -> ref -> tag

The new commit's parent is the current remote head, so history is preserved and
nothing is force-pushed.

Usage:
    GITHUB_TOKEN=... python tools/publish_release.py --repo owner/name \
        --asset release/x.zip --tag v1.0.8 --title "..." --notes-file notes.md
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
ROOT = Path(__file__).resolve().parent.parent


def call(method: str, url: str, payload=None, raw: bytes | None = None,
         content_type="application/json"):
    data = None
    if raw is not None:
        data = raw
    elif payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", "Bearer %s" % TOKEN)
    request.add_header("Accept", "application/vnd.github+json")
    if data is not None:
        request.add_header("Content-Type", content_type)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=120) as response:
            body = response.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:400]
        raise SystemExit("%s %s -> %s %s" % (method, url, error.code, detail))


def local_files() -> list[tuple[str, bytes]]:
    """Tracked files only: git ls-files honours .gitignore."""
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                         capture_output=True, check=True).stdout
    names = [name for name in out.decode("utf-8").split("\0") if name]
    files = []
    for name in names:
        path = ROOT / name
        if path.is_file():
            files.append((name, path.read_bytes()))
    return files


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", default="master")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--notes-file", required=True)
    parser.add_argument("--asset", required=True)
    parser.add_argument("--message-file", default="release-commit-message.txt")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-release", action="store_true",
                        help="only update the branch, skip tag and release")
    args = parser.parse_args()

    if not TOKEN:
        raise SystemExit("GITHUB_TOKEN is not set")

    repo = args.repo
    ref = call("GET", "%s/repos/%s/git/ref/heads/%s" % (API, repo, args.branch))
    base_sha = ref["object"]["sha"]
    base_commit = call("GET", "%s/repos/%s/git/commits/%s" % (API, repo, base_sha))
    base_tree = base_commit["tree"]["sha"]
    print("base %s tree %s" % (base_sha[:8], base_tree[:8]))

    files = local_files()
    print("files to upload: %d" % len(files))

    def blobs(item):
        name, body = item
        created = call("POST", "%s/repos/%s/git/blobs" % (API, repo), {
            "content": base64.b64encode(body).decode("ascii"),
            "encoding": "base64",
        })
        return name, created["sha"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        uploaded = list(pool.map(blobs, files))
    print("blobs uploaded: %d" % len(uploaded))

    ours = {name for name, _ in uploaded}
    remote_tree = call("GET", "%s/repos/%s/git/trees/%s?recursive=1"
                       % (API, repo, base_tree))
    removed = [entry["path"] for entry in remote_tree.get("tree", [])
               if entry["type"] == "blob" and entry["path"] not in ours]
    print("files to delete: %d %s" % (len(removed), removed[:5]))

    entries = [{"path": name, "mode": "100644", "type": "blob", "sha": sha}
               for name, sha in uploaded]
    entries += [{"path": path, "mode": "100644", "type": "blob", "sha": None}
                for path in removed]
    tree = call("POST", "%s/repos/%s/git/trees" % (API, repo),
                {"base_tree": base_tree, "tree": entries})
    print("tree %s" % tree["sha"])

    me = call("GET", "%s/user" % API)
    identity = {"name": me.get("name") or me["login"],
                "email": "%s@users.noreply.github.com" % me["login"]}
    message = Path(args.message_file).read_text(encoding="utf-8")
    commit = call("POST", "%s/repos/%s/git/commits" % (API, repo), {
        "message": message,
        "tree": tree["sha"],
        "parents": [base_sha],
        "author": identity,
        "committer": identity,
    })
    print("commit %s" % commit["sha"])

    if args.dry_run:
        print("dry run: stopping before ref update")
        return 0

    call("PATCH", "%s/repos/%s/git/refs/heads/%s" % (API, repo, args.branch),
         {"sha": commit["sha"], "force": False})
    print("branch %s updated" % args.branch)

    if args.no_release:
        print("branch only: skipping tag and release")
        return 0

    try:
        call("POST", "%s/repos/%s/git/refs" % (API, repo),
             {"ref": "refs/tags/%s" % args.tag, "sha": commit["sha"]})
        print("tag %s created" % args.tag)
    except SystemExit as error:
        print("tag skipped: %s" % error)

    notes = Path(args.notes_file).read_text(encoding="utf-8")
    release = call("POST", "%s/repos/%s/releases" % (API, repo), {
        "tag_name": args.tag,
        "name": args.title,
        "body": notes,
        "draft": False,
        "prerelease": False,
    })
    print("release %s" % release["html_url"])

    asset_path = Path(args.asset)
    body = asset_path.read_bytes()
    name = asset_path.name
    uploaded_asset = call(
        "POST",
        "%s/repos/%s/releases/%d/assets?name=%s" % (UPLOADS, repo,
                                                    release["id"], name),
        raw=body, content_type="application/zip")
    print("asset %s (%d bytes) %s" % (uploaded_asset["name"],
                                      uploaded_asset["size"],
                                      uploaded_asset["browser_download_url"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
