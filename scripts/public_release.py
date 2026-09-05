"""Prepare a clean source copy; never publish local credentials or Git history.

Preparing is local only. --publish explicitly creates a NEW public repository
using the user's existing Git Credential Manager login and pushes the clean copy.
No token is written to disk, placed in a remote URL, or printed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
TOP = {"pyproject.toml", "requirements-dev.txt", "pytest.ini", ".env.example",
       ".gitignore", ".dockerignore", "Dockerfile", "RESULTS.md", "PREREGISTRATION.md"}
TREES = ("src", "scripts", "tests", "config", "docs", "samples", "ui/control", "data/frozen", ".github")
# The public copy carries what someone needs to install, connect and operate the
# system, plus the evidence for what it achieves and the material that evidence
# rests on. RESULTS.md is the evidence; PREREGISTRATION.md is what makes it
# checkable, holding the config and code hashes recorded before the frozen set
# existed; and RESULTS cites RECOVERY_RATE and the PRIOR_ART survey under it for
# the recovery-rate argument, so publishing RESULTS without them would leave its
# own references unresolvable. What stays private is the working record rather
# than the result: a submission checklist, a dated review and demo write-up, and
# an internal gap analysis. PUBLIC_README.md is omitted because it is published
# as README.md, not alongside it.
DOCS_PRIVATE = {"docs/PUBLIC_README.md", "docs/BUILDATHON.md", "docs/DEMO_RESULTS.md",
                "docs/FEATURE_GAPS.md", "docs/FINAL_REVIEW.md"}


def selected(root):
    files = [root/name for name in TOP if (root/name).is_file()]
    for tree in TREES:
        files.extend(p for p in (root/tree).rglob("*") if p.is_file()
                     and "__pycache__" not in p.parts and p.suffix not in (".pyc", ".log")
                     and ".local." not in p.name and not p.is_symlink()
                     and p.resolve().is_relative_to(root.resolve()))
    # The example is public; the actual redirect file is never selected.
    example = root/"config/redirect.local.example.yaml"
    if example.exists(): files.append(example)
    return sorted(set(files))


def prepare(destination, root=ROOT):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("Release directory already exists; choose a fresh path")
    from src.execution.razorpay import load_env
    env = load_env(root/".env")
    secrets = [v.encode() for k, v in env.items()
               if any(s in k for s in ("KEY_SECRET", "API_KEY", "ADMIN_TOKEN", "TOKEN_SECRET", "WEBHOOK_SECRET"))
               and len(v) >= 8 and "xxxx" not in v.lower()]
    files = {p.relative_to(root).as_posix(): p.read_bytes() for p in selected(root)
             if p.relative_to(root).as_posix() not in DOCS_PRIVATE}
    files["README.md"] = (root/"docs/PUBLIC_README.md").read_bytes()
    for name, body in files.items():
        if any(secret in body for secret in secrets):
            raise ValueError(f"Credential match in selected file: {name}; release refused")
        if re.search(rb"(?:ghp_|github_pat_|AIza)[A-Za-z0-9_-]{24,}", body):
            raise ValueError(f"Possible credential in selected file: {name}; release refused")
    destination.mkdir(parents=True)
    for name, body in files.items():
        target = destination/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    manifest = {"file_count": len(files), "files": {n: hashlib.sha256(b).hexdigest() for n, b in files.items()},
                "excluded": [".git history", ".env", "data/live", "data/console", "data/sim", "ui/data.js",
                             "redirect.local.yaml", "runtime credentials", "research record (SPEC, RESULTS, PREREGISTRATION)",
                             *sorted(DOCS_PRIVATE)]}
    (destination/"PUBLIC_RELEASE.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def github_token():
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    proc = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n", text=True,
                          capture_output=True, env=env, timeout=30)
    values = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    if proc.returncode or not values.get("password"):
        raise RuntimeError("GitHub credential unavailable; authenticate Git Credential Manager first")
    return values["password"]


def github(method, path, token, body=None):
    req = urllib.request.Request("https://api.github.com"+path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer "+token, "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "User-Agent": "recover-public-release"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404 and method == "GET": return None
        raise RuntimeError(f"GitHub {method} {path}: HTTP {e.code}; no credentials logged") from None


def publish(destination, owner, repo):
    if not re.fullmatch(r"[A-Za-z0-9-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
        raise ValueError("Invalid GitHub target")
    token = github_token()
    user = github("GET", "/user", token)
    if user["login"].lower() != owner.lower():
        raise RuntimeError("Authenticated GitHub user does not match requested owner")
    if github("GET", f"/repos/{owner}/{repo}", token) is not None:
        raise RuntimeError("Repository already exists; refusing to overwrite an existing project")
    # Scan again immediately before any publication, including staged files.
    expected = json.loads((destination/"PUBLIC_RELEASE.json").read_text())
    for name, digest in expected["files"].items():
        if hashlib.sha256((destination/name).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"Release file changed after review: {name}")
    def git(*args):
        result = subprocess.run(["git", *args], cwd=destination, capture_output=True, text=True,
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, timeout=180)
        if result.returncode:
            raise RuntimeError("Git operation failed: "+args[0]+"; inspect repository/authentication locally")
    if (destination/".git").exists():
        raise RuntimeError("Expected a fresh, history-free release directory")
    git("init", "-b", "main")
    git("config", "user.name", owner)
    git("config", "user.email", f"{user['id']}+{owner}@users.noreply.github.com")
    # The manifest stays in the staging directory as the integrity record for
    # this build; it is not part of what someone clones to run the system.
    git("add", "--", *expected["files"].keys())
    git("commit", "-m", "Build Recover: bounded Razorpay revenue recovery with verified workflows")
    created = github("POST", "/user/repos", token, {"name": repo, "private": False,
                      "description": "Diagnosis-aware revenue recovery for Razorpay test accounts: bounded campaigns, SMS/email, reconciliation and reproducible evidence.", "auto_init": False})
    del token
    git("remote", "add", "origin", created["clone_url"])
    git("push", "-u", "origin", "main")
    return created["html_url"]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("destination", type=Path)
    p.add_argument("--publish", action="store_true")
    p.add_argument("--owner", default="Unl0f4ck")
    p.add_argument("--repo", default="recover-revenue-recovery")
    args = p.parse_args()
    if args.publish:
        print(publish(args.destination.resolve(), args.owner, args.repo))
    else:
        manifest = prepare(args.destination)
        print(json.dumps({"directory": str(args.destination), "files": manifest["file_count"], "credentials_scan": "passed"}))


if __name__ == "__main__":
    main()
