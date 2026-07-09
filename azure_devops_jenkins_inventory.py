#!/usr/bin/env python3
"""Inventory Azure DevOps repositories that contain a Jenkinsfile.

Scans one or more Azure DevOps organizations (Organization > Project >
Repository) and records which repositories contain a file matching a given
pattern (default: exactly ``Jenkinsfile``) in the root of the default branch.

Two scan modes are supported:

* ``rest``  (default) - Uses the Git REST API to list the root items of every
  repository and match them against the pattern. Always available.
* ``fast`` - Uses the Azure DevOps Code Search API to find matches with a
  single request per organization. Requires the free "Code Search"
  Marketplace extension to be installed on the organization. Falls back to
  ``rest`` automatically when the extension is unavailable.

Results are written to a CSV file.
"""

from __future__ import annotations

import argparse
import base64
import csv
import fnmatch
import json
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional

import requests

API_VERSION = "7.1"
DEFAULT_PATTERN = "Jenkinsfile"
DEV_BASE = "https://dev.azure.com"
SEARCH_BASE = "https://almsearch.dev.azure.com"
MAX_RETRIES = 5
RETRY_STATUSES = {429, 500, 502, 503, 504}
# Azure DevOps Code Search caps results per query.
CODE_SEARCH_CAP = 1000
CODE_SEARCH_PAGE = 200


@dataclass
class RepoResult:
    """A single row of the inventory output."""

    organization: str
    project: str
    repository: str
    repo_id: str
    default_branch: str
    matched_file: str
    has_jenkinsfile: bool
    web_url: str
    mode: str
    checked_at: str


class AuthError(Exception):
    """Raised when authentication against an organization fails."""


class CodeSearchUnavailable(Exception):
    """Raised when the Code Search extension is not available for an org."""


def _log(message: str) -> None:
    """Write a progress message to stderr."""
    print(message, file=sys.stderr, flush=True)


def resolve_token(org: str, per_org: dict[str, str], global_pat: Optional[str]) -> str:
    """Return the PAT to use for ``org``.

    Prefers a per-organization token, then falls back to the global PAT.
    """
    token = per_org.get(org.lower()) or global_pat
    if not token:
        raise AuthError(
            f"No token found for organization '{org}'. Set AZDO_PAT or add an "
            f"entry to AZDO_TOKENS."
        )
    return token


def parse_token_map(raw: Optional[str]) -> dict[str, str]:
    """Parse ``AZDO_TOKENS`` into a lowercased ``{org: pat}`` mapping.

    Accepts either JSON (``{"org": "pat"}``) or a comma-separated list of
    ``org=pat`` pairs.
    """
    if not raw:
        return {}
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"AZDO_TOKENS is not valid JSON: {exc}") from exc
        return {str(k).lower(): str(v) for k, v in data.items()}

    mapping: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ValueError(f"AZDO_TOKENS entry '{pair}' must be in 'org=pat' form.")
        org, pat = pair.split("=", 1)
        mapping[org.strip().lower()] = pat.strip()
    return mapping


def make_session(pat: str) -> requests.Session:
    """Build a requests session that authenticates with a PAT via HTTP Basic."""
    session = requests.Session()
    encoded = base64.b64encode(f":{pat}".encode("utf-8")).decode("ascii")
    session.headers.update({"Authorization": f"Basic {encoded}"})
    return session


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    params: Optional[dict] = None,
    json_body: Optional[dict] = None,
) -> requests.Response:
    """Perform an HTTP request with retry/backoff and auth detection.

    Returns the ``Response`` on success. Raises ``AuthError`` when the PAT is
    invalid (Azure DevOps returns a sign-in redirect / 203 in that case).
    """
    backoff = 1.0
    last_exc: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.request(
                method, url, params=params, json=json_body, timeout=30
            )
        except requests.RequestException as exc:  # network-level failure
            last_exc = exc
            if attempt == MAX_RETRIES:
                raise
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue

        # A non-interactive PAT failure surfaces as a redirect to sign-in.
        if response.status_code == 203 or "_signin" in response.url:
            raise AuthError(
                "Authentication failed (check that the PAT is valid and has "
                "Code (Read) scope for this organization)."
            )

        if response.status_code in RETRY_STATUSES:
            if attempt == MAX_RETRIES:
                return response
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else backoff
            time.sleep(delay)
            backoff = min(backoff * 2, 30)
            continue

        return response

    # Should not reach here, but re-raise the last network error if it does.
    if last_exc:
        raise last_exc
    raise RuntimeError("Request failed without a response.")


def get_projects(session: requests.Session, org: str) -> Iterator[str]:
    """Yield the names of all projects in ``org`` (handles pagination)."""
    url = f"{DEV_BASE}/{org}/_apis/projects"
    continuation: Optional[str] = None
    while True:
        params = {"api-version": API_VERSION, "$top": 200}
        if continuation:
            params["continuationToken"] = continuation
        response = request_json(session, "GET", url, params=params)
        response.raise_for_status()
        payload = response.json()
        for project in payload.get("value", []):
            yield project["name"]
        continuation = response.headers.get("x-ms-continuationtoken")
        if not continuation:
            break


def get_repos(session: requests.Session, org: str, project: str) -> list[dict]:
    """Return the repositories in ``project`` that are enabled and non-empty."""
    url = f"{DEV_BASE}/{org}/{project}/_apis/git/repositories"
    response = request_json(session, "GET", url, params={"api-version": API_VERSION})
    response.raise_for_status()
    repos = response.json().get("value", [])
    return [r for r in repos if not r.get("isDisabled") and r.get("defaultBranch")]


def list_root_items(session: requests.Session, org: str, project: str, repo_id: str) -> list[str]:
    """Return the names of the files/folders in the root of the default branch."""
    url = f"{DEV_BASE}/{org}/{project}/_apis/git/repositories/{repo_id}/items"
    params = {
        "api-version": API_VERSION,
        "scopePath": "/",
        "recursionLevel": "OneLevel",
    }
    response = request_json(session, "GET", url, params=params)
    if response.status_code == 404:
        # Empty repository or no items on the default branch.
        return []
    response.raise_for_status()
    names: list[str] = []
    for item in response.json().get("value", []):
        path = item.get("path", "")
        if path == "/":
            continue
        # Root-level entry: path like "/Jenkinsfile" with no further slashes.
        trimmed = path.lstrip("/")
        if "/" in trimmed:
            continue
        if not item.get("isFolder"):
            names.append(trimmed)
    return names


def match_pattern(names: Iterable[str], pattern: str) -> Optional[str]:
    """Return the first name matching ``pattern`` (case-insensitive), else None."""
    lowered = pattern.lower()
    for name in names:
        if fnmatch.fnmatch(name.lower(), lowered):
            return name
    return None


def scan_rest(
    session: requests.Session,
    org: str,
    pattern: str,
    matches_only: bool,
) -> Iterator[RepoResult]:
    """Scan an organization using the Git REST API."""
    for project in get_projects(session, org):
        _log(f"  [{org}] project: {project}")
        for repo in get_repos(session, org, project):
            names = list_root_items(session, org, project, repo["id"])
            matched = match_pattern(names, pattern)
            if matched is None and matches_only:
                continue
            yield RepoResult(
                organization=org,
                project=project,
                repository=repo["name"],
                repo_id=repo["id"],
                default_branch=(repo.get("defaultBranch") or "").replace(
                    "refs/heads/", ""
                ),
                matched_file=matched or "",
                has_jenkinsfile=matched is not None,
                web_url=repo.get("webUrl", ""),
                mode="rest",
                checked_at=_now(),
            )


def scan_code_search(
    session: requests.Session,
    org: str,
    pattern: str,
) -> Iterator[RepoResult]:
    """Scan an organization using the Code Search API.

    Raises ``CodeSearchUnavailable`` when the extension is not installed or the
    result set exceeds the cap (in which case the caller should fall back).
    """
    url = f"{SEARCH_BASE}/{org}/_apis/search/codesearchresults"
    skip = 0
    seen: set[tuple[str, str]] = set()
    while True:
        body = {
            "searchText": "Jenkinsfile",
            "$top": CODE_SEARCH_PAGE,
            "$skip": skip,
            "includeFacets": False,
        }
        response = request_json(
            session, "POST", url, params={"api-version": API_VERSION}, json_body=body
        )
        if response.status_code in (404, 400):
            raise CodeSearchUnavailable(
                f"Code Search is not available for organization '{org}'."
            )
        response.raise_for_status()
        payload = response.json()
        total = payload.get("count", 0)
        if total > CODE_SEARCH_CAP:
            raise CodeSearchUnavailable(
                f"Code Search returned {total} results for '{org}', exceeding the "
                f"{CODE_SEARCH_CAP} cap."
            )
        results = payload.get("results", [])
        if not results:
            break
        for item in results:
            file_name = item.get("fileName", "")
            path = item.get("path", "")
            # Root-only: path is like "/Jenkinsfile".
            if path.count("/") != 1:
                continue
            if not fnmatch.fnmatch(file_name.lower(), pattern.lower()):
                continue
            repo = item.get("repository", {})
            project = item.get("project", {})
            key = (repo.get("id", ""), file_name)
            if key in seen:
                continue
            seen.add(key)
            yield RepoResult(
                organization=org,
                project=project.get("name", ""),
                repository=repo.get("name", ""),
                repo_id=repo.get("id", ""),
                default_branch="",
                matched_file=file_name,
                has_jenkinsfile=True,
                web_url="",
                mode="fast",
                checked_at=_now(),
            )
        skip += CODE_SEARCH_PAGE
        if skip >= total:
            break


def _now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def load_orgs(args_orgs: Optional[list[str]], env_orgs: Optional[str], org_file: Optional[str]) -> list[str]:
    """Resolve the list of organizations from CLI, env, or a file."""
    orgs: list[str] = []
    if args_orgs:
        orgs.extend(args_orgs)
    if env_orgs:
        orgs.extend(o.strip() for o in env_orgs.split(","))
    if org_file:
        with open(org_file, "r", encoding="utf-8") as handle:
            orgs.extend(line.strip() for line in handle)
    # De-duplicate while preserving order and dropping blanks.
    seen: set[str] = set()
    unique: list[str] = []
    for org in orgs:
        if org and org not in seen:
            seen.add(org)
            unique.append(org)
    return unique


def write_csv(rows: list[RepoResult], output_path: str) -> None:
    """Write results to ``output_path`` as CSV."""
    fieldnames = list(RepoResult.__annotations__.keys())
    with open(output_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inventory Azure DevOps repos containing a Jenkinsfile."
    )
    parser.add_argument(
        "--org",
        action="append",
        dest="orgs",
        metavar="ORG",
        help="Organization name (repeatable). Overrides/adds to AZDO_ORGS.",
    )
    parser.add_argument(
        "--orgs-file",
        help="Path to a file with one organization name per line.",
    )
    parser.add_argument(
        "--pattern",
        default=DEFAULT_PATTERN,
        help=f"Glob pattern for the filename (default: {DEFAULT_PATTERN!r}).",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Use the Code Search API (falls back to REST if unavailable).",
    )
    parser.add_argument(
        "--matches-only",
        action="store_true",
        help="Only write rows for repos that contain a match (REST mode).",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="jenkins_inventory.csv",
        help="Output CSV path (default: jenkins_inventory.csv).",
    )
    return parser.parse_args(argv)


def scan_org(
    org: str,
    token: str,
    args: argparse.Namespace,
) -> list[RepoResult]:
    """Scan a single organization, honoring fast/rest mode and fallback."""
    session = make_session(token)
    if args.fast:
        try:
            _log(f"[{org}] scanning via Code Search (fast mode)...")
            return list(scan_code_search(session, org, args.pattern))
        except CodeSearchUnavailable as exc:
            _log(f"[{org}] {exc} Falling back to REST mode.")
    _log(f"[{org}] scanning via REST API...")
    return list(scan_rest(session, org, args.pattern, args.matches_only))


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    global_pat = os.environ.get("AZDO_PAT")
    try:
        per_org = parse_token_map(os.environ.get("AZDO_TOKENS"))
    except ValueError as exc:
        _log(f"Error: {exc}")
        return 2

    orgs = load_orgs(args.orgs, os.environ.get("AZDO_ORGS"), args.orgs_file)
    if not orgs:
        _log(
            "Error: no organizations provided. Use --org, --orgs-file, or set "
            "AZDO_ORGS."
        )
        return 2

    all_rows: list[RepoResult] = []
    failures: list[str] = []
    for org in orgs:
        try:
            token = resolve_token(org, per_org, global_pat)
            rows = scan_org(org, token, args)
            all_rows.extend(rows)
            matches = sum(1 for r in rows if r.has_jenkinsfile)
            _log(f"[{org}] done: {matches} match(es), {len(rows)} row(s).")
        except AuthError as exc:
            _log(f"[{org}] ERROR: {exc}")
            failures.append(org)
        except requests.HTTPError as exc:
            _log(f"[{org}] ERROR: HTTP failure: {exc}")
            failures.append(org)

    write_csv(all_rows, args.output)
    total_matches = sum(1 for r in all_rows if r.has_jenkinsfile)
    _log(
        f"\nWrote {len(all_rows)} row(s) ({total_matches} match(es)) to "
        f"{args.output}."
    )
    if failures:
        _log(f"Organizations that failed: {', '.join(failures)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
