#!/usr/bin/env python3
"""Populate version, default_branch, and install_as fields in libraries.json.

Queries GitHub / Codeberg APIs for each library's latest tag (or HEAD commit
hash when no tags exist) and default branch name.  Run before releases:

    uv run python scripts/update_library_versions.py
"""

import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlparse

LIBRARIES_JSON = Path(__file__).resolve().parent.parent / "src" / "neuscad" / "resources" / "libraries.json"

GITHUB_HEADERS = {"Accept": "application/vnd.github.v3+json", "User-Agent": "NeuSCAD-Library-Updater"}
CODEBERG_HEADERS = {"Accept": "application/json", "User-Agent": "NeuSCAD-Library-Updater"}


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
        raise


def _fetch_github(owner: str, repo: str) -> tuple[str | None, str | None]:
    """Return (latest_version, default_branch) for a GitHub repo."""
    repo_info = _api_get(f"https://api.github.com/repos/{owner}/{repo}", GITHUB_HEADERS)
    if repo_info is None:
        return None, None
    default_branch = repo_info.get("default_branch", "main")

    tags = _api_get(f"https://api.github.com/repos/{owner}/{repo}/tags?per_page=1", GITHUB_HEADERS)
    if tags:
        return tags[0]["name"], default_branch

    commits = _api_get(f"https://api.github.com/repos/{owner}/{repo}/commits?per_page=1&sha={default_branch}", GITHUB_HEADERS)
    if commits:
        return commits[0]["sha"][:7], default_branch
    return None, default_branch


def _fetch_codeberg(owner: str, repo: str) -> tuple[str | None, str | None]:
    """Return (latest_version, default_branch) for a Codeberg repo."""
    repo_info = _api_get(f"https://codeberg.org/api/v1/repos/{owner}/{repo}", CODEBERG_HEADERS)
    if repo_info is None:
        return None, None
    default_branch = repo_info.get("default_branch", "main")

    tags = _api_get(f"https://codeberg.org/api/v1/repos/{owner}/{repo}/tags?limit=1", CODEBERG_HEADERS)
    if tags:
        return tags[0]["name"], default_branch

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
        if branch:
            lib["default_branch"] = branch
            print(f"branch={branch}", end=" ")

        lib.setdefault("install_as", repo)
        print(f"install_as={lib['install_as']}")

        time.sleep(0.5)

    with open(LIBRARIES_JSON, "w") as f:
        json.dump(libraries, f, indent=2)
        f.write("\n")

    print(f"\nUpdated {LIBRARIES_JSON}")


if __name__ == "__main__":
    main()
