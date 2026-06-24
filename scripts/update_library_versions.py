#!/usr/bin/env python3
"""Populate version, default_branch, and install_as fields in libraries.json.

Uses `git ls-remote` for GitHub repos (no API rate limits) and the Codeberg
REST API for Codeberg repos.  Run before releases:

    uv run python scripts/update_library_versions.py
"""

import json
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlparse

LIBRARIES_JSON = Path(__file__).resolve().parent.parent / "src" / "belfryscad" / "resources" / "libraries.json"

CODEBERG_HEADERS = {"Accept": "application/json", "User-Agent": "BelfrySCAD-Library-Updater"}


def _parse_repo(download_url: str) -> tuple[str, str, str]:
    """Return (host, owner, repo) from a GitHub or Codeberg URL."""
    parsed = urlparse(download_url.rstrip("/"))
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2:
        raise ValueError(f"Cannot parse owner/repo from {download_url}")
    return parsed.hostname, parts[0], parts[1]


def _api_get(url: str, headers: dict) -> dict | list | None:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            print(f"  Rate limited ({e.code}) on {url}", file=sys.stderr)
            return None
        if e.code == 404:
            return None
        raise


def _version_sort_key(tag: str) -> tuple:
    """Extract numeric components for sorting. '2.0.682' > '1.0' > '0.9'."""
    nums = re.findall(r"\d+", tag)
    return tuple(int(n) for n in nums) if nums else (0,)


def _fetch_github(owner: str, repo: str) -> tuple[str | None, str | None]:
    """Return (latest_version, default_branch) using git ls-remote (no API)."""
    url = f"https://github.com/{owner}/{repo}.git"
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--symref", url, "HEAD", "refs/tags/*", "refs/heads/*"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None, None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None, None

    lines = result.stdout.strip().splitlines()

    # Parse default branch from symref line: "ref: refs/heads/main\tHEAD"
    default_branch = "main"
    for line in lines:
        if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
            default_branch = line.split("ref: refs/heads/")[1].split("\t")[0]
            break

    # Collect tags (skip ^{} dereferenced entries)
    tags = []
    head_sha = None
    for line in lines:
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        sha, ref = parts
        if ref == "HEAD":
            head_sha = sha
        elif ref.startswith("refs/tags/") and not ref.endswith("^{}"):
            tag = ref[len("refs/tags/"):]
            tags.append(tag)

    if tags:
        tags.sort(key=_version_sort_key)
        return tags[-1], default_branch

    if head_sha:
        return head_sha[:7], default_branch

    return None, default_branch


def _fetch_codeberg(owner: str, repo: str) -> tuple[str | None, str | None]:
    """Return (latest_version, default_branch) for a Codeberg repo."""
    repo_info = _api_get(f"https://codeberg.org/api/v1/repos/{owner}/{repo}", CODEBERG_HEADERS)
    if repo_info is None:
        return None, None
    default_branch = repo_info.get("default_branch", "main")

    try:
        releases = _api_get(f"https://codeberg.org/api/v1/repos/{owner}/{repo}/releases?limit=1", CODEBERG_HEADERS)
        if releases and releases[0].get("tag_name"):
            return releases[0]["tag_name"], default_branch
    except (urllib.error.HTTPError, IndexError, KeyError):
        pass

    branch_info = _api_get(f"https://codeberg.org/api/v1/repos/{owner}/{repo}/branches/{default_branch}", CODEBERG_HEADERS)
    if branch_info and "commit" in branch_info:
        return branch_info["commit"]["id"][:7], default_branch
    return None, default_branch


def main():
    with open(LIBRARIES_JSON) as f:
        libraries = json.load(f)

    for lib in libraries:
        name = lib["name"]
        try:
            host, owner, repo = _parse_repo(lib["download_url"])
        except ValueError as e:
            print(f"  SKIP {name}: {e}", file=sys.stderr)
            continue

        print(f"  {name} ({host}/{owner}/{repo})...", end=" ", flush=True)

        if host == "github.com":
            version, branch = _fetch_github(owner, repo)
        elif host == "codeberg.org":
            version, branch = _fetch_codeberg(owner, repo)
        else:
            print(f"unknown host {host}", file=sys.stderr)
            continue

        if version:
            lib["version"] = version
            print(f"v={version}", end=" ")
        else:
            print("(no version found)", end=" ")
        if branch:
            lib["default_branch"] = branch
            print(f"branch={branch}", end=" ")

        lib.setdefault("install_as", repo)
        print(f"install_as={lib['install_as']}")

        time.sleep(2)

    with open(LIBRARIES_JSON, "w") as f:
        json.dump(libraries, f, indent=2)
        f.write("\n")

    print(f"\nUpdated {LIBRARIES_JSON}")


if __name__ == "__main__":
    main()
