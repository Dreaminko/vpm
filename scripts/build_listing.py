#!/usr/bin/env python3
"""Build Dreaminko's VPM Repository index.json and static landing page."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE_JSON = REPO_ROOT / "source.json"
TEMPLATE_PATH = REPO_ROOT / "templates" / "index.html"

SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)
TOKEN_RE = re.compile(r"__[A-Z0-9_]+__")

_ICON_DOWNLOAD = (
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="1.9" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true">'
    '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
    '<polyline points="7 10 12 15 17 10"/>'
    '<line x1="12" x2="12" y1="15" y2="3"/></svg>'
)

USER_AGENT = "Dreaminko-VPM-Listing/1.0"
GITHUB_API_VERSION = "2022-11-28"
HTTP_TIMEOUT_SECONDS = 30

SemVerKey = tuple[int, int, int, int, tuple[tuple[int, int | str], ...]]


def parse_semver(version: str) -> SemVerKey:
    """Parse a SemVer 2.0.0 string into a comparable sort key.

    Build metadata (+build) is validated and ignored for precedence.
    Numeric prerelease identifiers sort lower than alphanumeric ones, and
    stable releases sort higher than prereleases of the same core version.
    """
    if not isinstance(version, str):
        raise ValueError(f"Invalid SemVer 2.0.0 version: {version!r}")
    match = SEMVER_RE.match(version)
    if not match:
        raise ValueError(f"Invalid SemVer 2.0.0 version: {version!r}")

    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3))
    prerelease = match.group(4)

    if prerelease is None:
        return (major, minor, patch, 1, ())

    parts: list[tuple[int, int | str]] = []
    for segment in prerelease.split("."):
        if segment.isdigit():
            parts.append((0, int(segment)))
        else:
            parts.append((1, segment))
    return (major, minor, patch, 0, tuple(parts))


def validate_manifest(manifest: Any) -> None:
    """Validate a VPM package manifest dict."""
    if not isinstance(manifest, dict):
        raise ValueError("Manifest must be a JSON object.")

    pkg_name = manifest.get("name")
    pkg_ver = manifest.get("version")
    label = f"{pkg_name or '<unknown>'}@{pkg_ver or '<unknown>'}"

    for field in ("name", "version", "url", "zipSHA256"):
        value = manifest.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"Manifest {label} is missing required non-empty string field {field!r}."
            )

    url = manifest["url"]
    if not url.startswith(("https://", "http://")):
        raise ValueError(
            f"Manifest {label} has invalid 'url' scheme (expected http:// or https://): {url!r}"
        )

    zip_sha256 = manifest["zipSHA256"]
    if not SHA256_RE.match(zip_sha256):
        raise ValueError(
            f"Manifest {label} has invalid 'zipSHA256' (expected 64 hex chars): {zip_sha256!r}"
        )

    try:
        parse_semver(manifest["version"])
    except ValueError as exc:
        raise ValueError(f"Manifest {label} has invalid SemVer version: {exc}") from exc


def parse_next_link(link_header: str | None) -> str | None:
    """Extract the URL for rel='next' from an HTTP Link header."""
    if not link_header:
        return None

    for part in link_header.split(","):
        match = re.match(r"^\s*<([^>]+)>\s*(.*)$", part)
        if not match:
            continue
        target_url, params = match.groups()
        for rel_match in re.finditer(
            r';\s*rel\s*=\s*(?:"([^"]*)"|([^\s;]+))', params, flags=re.IGNORECASE
        ):
            rel_value = rel_match.group(1) if rel_match.group(1) is not None else rel_match.group(2)
            if "next" in rel_value.lower().split():
                return target_url
    return None


def _http_get_json(url: str, headers: dict[str, str]) -> tuple[Any, Any]:
    """Fetch JSON over HTTP(S) and return (parsed_payload, response_headers)."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw), response.headers


def fetch_repo_manifests(repo: str, token: str | None = None) -> list[dict[str, Any]]:
    """Fetch and validate all non-draft release package.json manifests from a GitHub repo."""
    if not isinstance(repo, str) or "/" not in repo or repo.count("/") != 1:
        raise ValueError(f"Invalid GitHub repository slug: {repo!r}")

    owner, name = repo.split("/", 1)
    if not owner.strip() or not name.strip():
        raise ValueError(f"Invalid GitHub repository slug: {repo!r}")

    api_headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    if token:
        api_headers["Authorization"] = f"Bearer {token}"

    # Do not send GitHub Authorization header to release asset URLs (S3/Azure redirects reject it).
    asset_headers = {
        "Accept": "application/json, */*",
        "User-Agent": USER_AGENT,
    }

    manifests: list[dict[str, Any]] = []
    next_url: str | None = (
        f"https://api.github.com/repos/{urllib.parse.quote(owner)}/"
        f"{urllib.parse.quote(name)}/releases?per_page=100"
    )

    while next_url:
        releases, resp_headers = _http_get_json(next_url, api_headers)
        if not isinstance(releases, list):
            raise ValueError(f"Unexpected GitHub Releases API response for {repo}: expected list.")

        for release in releases:
            if not isinstance(release, dict) or release.get("draft") is True:
                continue

            for asset in release.get("assets") or []:
                if not isinstance(asset, dict) or asset.get("name") != "package.json":
                    continue
                download_url = asset.get("browser_download_url")
                if not isinstance(download_url, str) or not download_url.startswith(
                    ("https://", "http://")
                ):
                    raise ValueError(
                        f"Release asset 'package.json' in {repo} has invalid browser_download_url: {download_url!r}"
                    )
                manifest, _ = _http_get_json(download_url, asset_headers)
                validate_manifest(manifest)
                manifests.append(manifest)

        next_url = parse_next_link(resp_headers.get("Link") if resp_headers else None)

    return manifests


def aggregate_packages(
    entries: list[tuple[dict[str, Any], str | None]],
) -> tuple[dict[str, dict[str, dict[str, dict[str, Any]]]], dict[str, str]]:
    """Validate, group, and SemVer-sort package manifests without mutating them."""
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    repo_by_pkg: dict[str, str] = {}

    for manifest, repo in entries:
        validate_manifest(manifest)
        pkg_id = manifest["name"]
        version = manifest["version"]
        grouped.setdefault(pkg_id, {})
        if version not in grouped[pkg_id]:
            grouped[pkg_id][version] = manifest
        if repo and pkg_id not in repo_by_pkg:
            repo_by_pkg[pkg_id] = repo

    packages: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    for pkg_id in sorted(grouped):
        sorted_versions = sorted(grouped[pkg_id], key=parse_semver, reverse=True)
        packages[pkg_id] = {
            "versions": {ver: grouped[pkg_id][ver] for ver in sorted_versions}
        }

    return packages, repo_by_pkg


def _is_http_url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("https://", "http://"))


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _resolve_package_github_url(
    pkg_id: str, latest_manifest: dict[str, Any], repo_by_pkg: dict[str, str]
) -> str | None:
    repo = repo_by_pkg.get(pkg_id)
    if repo:
        return f"https://github.com/{repo}"

    author = latest_manifest.get("author")
    if isinstance(author, dict):
        author_url = author.get("url")
        if isinstance(author_url, str) and author_url.startswith(
            ("https://github.com/", "http://github.com/")
        ):
            return author_url
    return None


def _render_package_card(
    pkg_id: str,
    versions: dict[str, dict[str, Any]],
    repo_by_pkg: dict[str, str],
    expand_by_default: bool,
) -> str:
    version_items = list(versions.items())
    latest_ver, latest = version_items[0]
    display_name = str(latest.get("displayName") or pkg_id)
    description = str(latest.get("description") or "")
    release_count = len(version_items)
    release_label = f"{release_count} release" if release_count == 1 else f"{release_count} releases"

    dep_pills: list[str] = []
    vpm_deps = latest.get("vpmDependencies")
    if isinstance(vpm_deps, dict):
        for dep_id, constraint in vpm_deps.items():
            dep_pills.append(f'<span class="dep-pill">{_esc(dep_id)} {_esc(constraint)}</span>')
    dep_html = f'<div class="dep-list">{"".join(dep_pills)}</div>' if dep_pills else ""

    links: list[str] = []
    github_url = _resolve_package_github_url(pkg_id, latest, repo_by_pkg)
    if github_url:
        links.append(
            f'<a class="package-link" href="{_esc(github_url)}" target="_blank" '
            f'rel="noopener noreferrer">GitHub</a>'
        )
    docs_url = latest.get("documentationUrl")
    if _is_http_url(docs_url):
        links.append(
            f'<a class="package-link" href="{_esc(docs_url)}" target="_blank" '
            f'rel="noopener noreferrer">Docs</a>'
        )
    releases_url = latest.get("changelogUrl")
    if not _is_http_url(releases_url):
        releases_url = f"{github_url.rstrip('/')}/releases" if github_url else None
    if _is_http_url(releases_url):
        links.append(
            f'<a class="package-link" href="{_esc(releases_url)}" target="_blank" '
            f'rel="noopener noreferrer">Releases</a>'
        )
    links_html = f'<div class="package-links">{"".join(links)}</div>' if links else ""

    meta_row_html = (
        f'<div class="package-meta-row">{dep_html}{links_html}</div>'
        if (dep_html or links_html)
        else ""
    )

    rows_html: list[str] = []
    for idx, (ver, manifest) in enumerate(version_items):
        latest_badge = ' <span class="chip chip-sm">Latest</span>' if idx == 0 else ""
        sha = str(manifest["zipSHA256"])
        short_sha = f"{sha[:16]}..."
        dl_url = str(manifest["url"])
        rows_html.append(
            "<tr>"
            f'<td><code class="pkg-id">{_esc(pkg_id)}</code></td>'
            f'<td><span class="ver-cell"><strong class="ver-num">{_esc(ver)}</strong>{latest_badge}</span></td>'
            f'<td class="col-sha"><code class="sha-code" title="{_esc(sha)}">{_esc(short_sha)}</code></td>'
            f'<td><a class="btn-download" href="{_esc(dl_url)}">{_ICON_DOWNLOAD}<span>Download .zip</span></a></td>'
            "</tr>"
        )

    open_attr = " open" if expand_by_default else ""
    desc_html = f'<p class="package-desc">{_esc(description)}</p>' if description else ""

    return (
        '<section class="card package-card">'
        '<div class="package-head">'
        '<div class="package-title-group">'
        f"<h2>{_esc(display_name)}</h2>"
        f'<code class="pkg-id">{_esc(pkg_id)}</code>'
        f'<span class="chip chip-mono">{_esc(latest_ver)}</span>'
        "</div>"
        f'<span class="release-count">{_esc(release_label)}</span>'
        "</div>"
        f"{desc_html}"
        f"{meta_row_html}"
        f'<details class="versions-details"{open_attr}>'
        f"<summary>Version history ({_esc(release_label)})</summary>"
        '<div class="table-wrap">'
        "<table>"
        "<thead>"
        "<tr>"
        "<th>Package</th>"
        "<th>Version</th>"
        '<th class="col-sha">SHA-256</th>'
        "<th>Archive</th>"
        "</tr>"
        "</thead>"
        f'<tbody>{"".join(rows_html)}</tbody>'
        "</table>"
        "</div>"
        "</details>"
        "</section>"
    )


def render_html(
    source: dict[str, Any],
    listing: dict[str, Any],
    repo_by_pkg: dict[str, str],
    template_path: Path = TEMPLATE_PATH,
) -> str:
    """Render the static landing page HTML from templates/index.html."""
    template = template_path.read_text(encoding="utf-8")

    raw_author = source.get("author")
    if isinstance(raw_author, dict):
        author_name = str(raw_author.get("name") or listing.get("author") or "Dreaminko")
        author_url = raw_author.get("url")
    else:
        author_name = str(raw_author or listing.get("author") or "Dreaminko")
        author_url = None

    safe_author_url = author_url if _is_http_url(author_url) else "https://github.com/Dreaminko"
    if _is_http_url(author_url):
        author_html = (
            f'<a href="{_esc(author_url)}" target="_blank" '
            f'rel="noopener noreferrer">{_esc(author_name)}</a>'
        )
    else:
        author_html = _esc(author_name)

    packages: dict[str, dict[str, dict[str, Any]]] = listing.get("packages", {})
    pkg_count = len(packages)
    total_releases = sum(len(pkg.get("versions", {})) for pkg in packages.values())
    pkg_word = "package" if pkg_count == 1 else "packages"
    rel_word = "release" if total_releases == 1 else "releases"
    summary_chip = (
        f'<span class="chip chip-mono">{pkg_count} {pkg_word} · {total_releases} {rel_word}</span>'
    )

    non_empty_packages = [
        (pkg_id, pkg_data["versions"])
        for pkg_id, pkg_data in packages.items()
        if pkg_data.get("versions")
    ]
    non_empty_packages.sort(
        key=lambda item: (
            str(next(iter(item[1].values())).get("displayName") or item[0]).lower(),
            item[0].lower(),
        )
    )

    if not non_empty_packages:
        packages_html = (
            '<section class="card package-card">'
            '<p class="empty-state">No packages published yet.</p>'
            "</section>"
        )
    else:
        expand_by_default = len(non_empty_packages) == 1
        packages_html = "\n".join(
            _render_package_card(pkg_id, versions, repo_by_pkg, expand_by_default)
            for pkg_id, versions in non_empty_packages
        )

    listing_url = str(listing["url"])
    tokens = {
        "__LISTING_NAME__": _esc(listing["name"]),
        "__BRAND_NAME__": _esc(author_name),
        "__DESCRIPTION__": _esc(source.get("description") or ""),
        "__AUTHOR_URL__": _esc(safe_author_url),
        "__AUTHOR_HTML__": author_html,
        "__LISTING_URL__": _esc(listing_url),
        "__VCC_URI__": _esc(f"vcc://vpm/addRepo?url={listing_url}"),
        "__SUMMARY_CHIP__": summary_chip,
        "__PACKAGES_HTML__": packages_html,
    }

    return TOKEN_RE.sub(lambda match: tokens[match.group(0)], template)


def build_listing_data(
    source: dict[str, Any],
    packages: dict[str, dict[str, dict[str, dict[str, Any]]]],
) -> dict[str, Any]:
    """Assemble the VCC-compatible index.json structure."""
    raw_author = source.get("author")
    if isinstance(raw_author, dict):
        author_name = str(raw_author.get("name") or "Dreaminko")
    else:
        author_name = str(raw_author or "Dreaminko")

    return {
        "name": str(source["name"]),
        "id": str(source["id"]),
        "url": str(source["url"]),
        "author": author_name,
        "packages": packages,
    }


def _load_local_manifests(paths: list[Path]) -> list[tuple[dict[str, Any], str | None]]:
    entries: list[tuple[dict[str, Any], str | None]] = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        validate_manifest(data)
        entries.append((data, None))
    return entries


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build Dreaminko's VPM Repository index.json and landing page."
    )
    parser.add_argument(
        "--source-json",
        type=Path,
        default=DEFAULT_SOURCE_JSON,
        help="Path to source.json (default: repo root source.json).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="Output directory for index.json, index.html, and .nojekyll.",
    )
    parser.add_argument(
        "--token",
        type=str,
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub token for Releases API requests (default: GITHUB_TOKEN env var).",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip GitHub API requests and build only from --local-package-json inputs.",
    )
    parser.add_argument(
        "--local-package-json",
        type=Path,
        action="append",
        default=[],
        help="Local package.json manifest file to include (repeatable).",
    )
    parser.add_argument(
        "--expect-package",
        type=str,
        default=None,
        help="Package ID expected to appear (from repository_dispatch).",
    )
    parser.add_argument(
        "--expect-version",
        type=str,
        default=None,
        help="Package version expected to appear (from repository_dispatch).",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=5,
        help="Maximum fetch attempts when waiting for an expected release (default: 5).",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=5.0,
        help="Delay in seconds between retries (default: 5.0).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if bool(args.expect_package) != bool(args.expect_version):
        print(
            "Error: --expect-package and --expect-version must be provided together.",
            file=sys.stderr,
        )
        return 2
    if args.retries < 1:
        print("Error: --retries must be >= 1.", file=sys.stderr)
        return 2
    if args.retry_delay < 0:
        print("Error: --retry-delay must be >= 0.", file=sys.stderr)
        return 2

    source = json.loads(args.source_json.read_text(encoding="utf-8"))
    github_repos = source.get("githubRepos") or []
    if not isinstance(github_repos, list):
        print("Error: source.json 'githubRepos' must be a list.", file=sys.stderr)
        return 1

    token = args.token.strip() if isinstance(args.token, str) and args.token.strip() else None
    local_entries = _load_local_manifests(args.local_package_json)

    expecting = bool(args.expect_package and args.expect_version)
    max_attempts = args.retries if (not args.offline and expecting) else 1

    packages: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    repo_by_pkg: dict[str, str] = {}

    for attempt in range(1, max_attempts + 1):
        entries = list(local_entries)
        if not args.offline:
            try:
                for repo in github_repos:
                    for manifest in fetch_repo_manifests(repo, token):
                        entries.append((manifest, repo))
            except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
                if attempt < max_attempts:
                    print(
                        f"Attempt {attempt}/{max_attempts} failed ({exc}); "
                        f"retrying in {args.retry_delay}s...",
                        file=sys.stderr,
                    )
                    time.sleep(args.retry_delay)
                    continue
                print(f"Error fetching upstream releases: {exc}", file=sys.stderr)
                return 1

        packages, repo_by_pkg = aggregate_packages(entries)

        if expecting:
            versions_map = packages.get(args.expect_package, {}).get("versions", {})
            if args.expect_version in versions_map:
                break
            if attempt < max_attempts:
                print(
                    f"Expected release {args.expect_package}@{args.expect_version} not visible yet "
                    f"(attempt {attempt}/{max_attempts}); retrying in {args.retry_delay}s...",
                    file=sys.stderr,
                )
                time.sleep(args.retry_delay)
                continue
            print(
                f"Error: expected release {args.expect_package}@{args.expect_version} "
                f"not found after {max_attempts} attempt(s).",
                file=sys.stderr,
            )
            return 1

    listing = build_listing_data(source, packages)
    rendered_html = render_html(source, listing, repo_by_pkg)

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.json").write_text(
        json.dumps(listing, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (out_dir / "index.html").write_text(rendered_html, encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
