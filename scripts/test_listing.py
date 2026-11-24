#!/usr/bin/env python3
"""Offline unit tests for scripts/build_listing.py."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_listing  # noqa: E402


def _make_manifest(
    *,
    name: str = "dev.dreaminko.unitext",
    display_name: str = "UniText",
    version: str = "1.2.0",
    description: str = "Subtitle prop for VRChat.",
    url: str | None = None,
    sha256: str = "a" * 64,
    deps: dict[str, str] | None = None,
) -> dict:
    return {
        "name": name,
        "displayName": display_name,
        "version": version,
        "unity": "2022.3",
        "description": description,
        "author": {
            "name": "Dreaminko",
            "url": "https://github.com/Dreaminko/UniText",
        },
        "documentationUrl": "https://github.com/Dreaminko/UniText/blob/main/docs/PROTOCOL.md",
        "changelogUrl": "https://github.com/Dreaminko/UniText/releases",
        "url": url
        or f"https://github.com/Dreaminko/UniText/releases/download/{version}/{name}-{version}.zip",
        "zipSHA256": sha256,
        "vpmDependencies": deps
        if deps is not None
        else {"com.vrchat.avatars": ">=3.7.0", "nadena.dev.modular-avatar": ">=1.10.0"},
    }


class TestSemVer(unittest.TestCase):
    def test_precedence_ordering(self) -> None:
        shuffled = [
            "1.2.0-beta.2",
            "1.9.0",
            "1.2.0-alpha.1",
            "1.10.0",
            "1.2.0",
            "2.0.0",
            "1.2.0-rc.1",
            "1.12.3",
            "1.2.0-beta.10",
        ]
        ordered = sorted(shuffled, key=build_listing.parse_semver, reverse=True)
        self.assertEqual(
            ordered,
            [
                "2.0.0",
                "1.12.3",
                "1.10.0",
                "1.9.0",
                "1.2.0",
                "1.2.0-rc.1",
                "1.2.0-beta.10",
                "1.2.0-beta.2",
                "1.2.0-alpha.1",
            ],
        )

    def test_numeric_vs_alphanumeric_and_prefix_length(self) -> None:
        self.assertGreater(
            build_listing.parse_semver("1.0.0-alpha"),
            build_listing.parse_semver("1.0.0-1"),
        )
        self.assertGreater(
            build_listing.parse_semver("1.0.0-alpha.1"),
            build_listing.parse_semver("1.0.0-alpha"),
        )

    def test_build_metadata_ignored(self) -> None:
        self.assertEqual(
            build_listing.parse_semver("1.2.0+build.1"),
            build_listing.parse_semver("1.2.0+build.999"),
        )
        self.assertEqual(
            build_listing.parse_semver("1.2.0-rc.1+sha.abc"),
            build_listing.parse_semver("1.2.0-rc.1+sha.def"),
        )

    def test_invalid_versions_raise(self) -> None:
        for bad in ("", "1.2", "01.2.3", "v1.2.3", "1.2.3-", "1.2.3-01", "1.2.3+", 123):
            with self.subTest(version=bad):
                with self.assertRaises(ValueError):
                    build_listing.parse_semver(bad)  # type: ignore[arg-type]


class TestManifestValidation(unittest.TestCase):
    def test_valid_manifest(self) -> None:
        build_listing.validate_manifest(_make_manifest(sha256="0123456789abcdef" * 4))
        build_listing.validate_manifest(_make_manifest(sha256="0123456789ABCDEF" * 4))

    def test_non_dict_rejected(self) -> None:
        for bad in (None, [], "string"):
            with self.subTest(manifest=bad):
                with self.assertRaises(ValueError):
                    build_listing.validate_manifest(bad)

    def test_missing_or_empty_required_fields_rejected(self) -> None:
        for field in ("name", "version", "url", "zipSHA256"):
            with self.subTest(missing=field):
                manifest = _make_manifest()
                del manifest[field]
                with self.assertRaises(ValueError):
                    build_listing.validate_manifest(manifest)

            with self.subTest(empty=field):
                manifest = _make_manifest()
                manifest[field] = "   "
                with self.assertRaises(ValueError):
                    build_listing.validate_manifest(manifest)

    def test_invalid_url_scheme_rejected(self) -> None:
        for bad_url in ("ftp://example.com/a.zip", "javascript:alert(1)", "/local/path.zip"):
            with self.subTest(url=bad_url):
                with self.assertRaises(ValueError):
                    build_listing.validate_manifest(_make_manifest(url=bad_url))

    def test_invalid_sha256_rejected(self) -> None:
        for bad_sha in ("abc", "a" * 63, "a" * 65, "g" * 64):
            with self.subTest(sha=bad_sha):
                with self.assertRaises(ValueError):
                    build_listing.validate_manifest(_make_manifest(sha256=bad_sha))

    def test_invalid_semver_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_listing.validate_manifest(_make_manifest(version="1.2"))


class TestLinkHeaderParsing(unittest.TestCase):
    def test_extracts_next_link(self) -> None:
        header = (
            '<https://api.github.com/repositories/1/releases?per_page=100&page=1>; rel="prev", '
            '<https://api.github.com/repositories/1/releases?per_page=100&page=3>; rel="next", '
            '<https://api.github.com/repositories/1/releases?per_page=100&page=5>; rel="last"'
        )
        self.assertEqual(
            build_listing.parse_next_link(header),
            "https://api.github.com/repositories/1/releases?per_page=100&page=3",
        )

    def test_unquoted_rel_next(self) -> None:
        header = "<https://api.github.com/repos/a/b/releases?page=2>; rel=next"
        self.assertEqual(
            build_listing.parse_next_link(header),
            "https://api.github.com/repos/a/b/releases?page=2",
        )

    def test_no_next_link_returns_none(self) -> None:
        self.assertIsNone(build_listing.parse_next_link(None))
        self.assertIsNone(build_listing.parse_next_link(""))
        self.assertIsNone(
            build_listing.parse_next_link(
                '<https://api.github.com/repos/a/b/releases?page=1>; rel="prev"'
            )
        )


class TestOfflineAggregationAndRendering(unittest.TestCase):
    def test_multi_package_offline_cli_build_and_xss_escaping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            out_dir = tmp_path / "_site"

            manifests = [
                _make_manifest(
                    name="dev.dreaminko.unitext",
                    display_name="UniText",
                    version="1.2.0-beta.2",
                    sha256="1" * 64,
                ),
                _make_manifest(
                    name="dev.dreaminko.unitext",
                    display_name="UniText",
                    version="1.2.0",
                    sha256="2" * 64,
                ),
                _make_manifest(
                    name="dev.dreaminko.unitext",
                    display_name="UniText",
                    version="1.2.0-beta.10",
                    sha256="3" * 64,
                ),
                _make_manifest(
                    name="dev.dreaminko.unitext",
                    display_name="UniText",
                    version="1.10.0",
                    sha256="4" * 64,
                ),
                _make_manifest(
                    name="dev.dreaminko.unitext",
                    display_name="UniText",
                    version="1.2.0-rc.1",
                    sha256="5" * 64,
                ),
                _make_manifest(
                    name="dev.dreaminko.secondpkg",
                    display_name="Second Package",
                    version="0.2.0",
                    description='Second pkg <script>alert("xss")</script> description',
                    url="https://github.com/Dreaminko/SecondPkg/releases/download/0.2.0/dev.dreaminko.secondpkg-0.2.0.zip",
                    sha256="6" * 64,
                    deps={"com.vrchat.base": ">=3.5.0"},
                ),
                _make_manifest(
                    name="dev.dreaminko.secondpkg",
                    display_name="Second Package",
                    version="0.10.0",
                    description='Second pkg <script>alert("xss")</script> description',
                    url="https://github.com/Dreaminko/SecondPkg/releases/download/0.10.0/dev.dreaminko.secondpkg-0.10.0.zip",
                    sha256="7" * 64,
                    deps={"com.vrchat.base": ">=3.5.0", "nadena.dev.ndmf": ">=1.5.0"},
                ),
            ]

            cmd = [
                sys.executable,
                str(SCRIPTS_DIR / "build_listing.py"),
                "--offline",
                "--out-dir",
                str(out_dir),
            ]
            for idx, manifest in enumerate(manifests):
                pkg_file = tmp_path / f"pkg_{idx}.json"
                pkg_file.write_text(json.dumps(manifest), encoding="utf-8")
                cmd.extend(["--local-package-json", str(pkg_file)])

            result = subprocess.run(
                cmd,
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)

            index_json_path = out_dir / "index.json"
            index_html_path = out_dir / "index.html"
            nojekyll_path = out_dir / ".nojekyll"

            self.assertTrue(index_json_path.is_file())
            self.assertTrue(index_html_path.is_file())
            self.assertTrue(nojekyll_path.is_file())

            listing = json.loads(index_json_path.read_text(encoding="utf-8"))
            self.assertEqual(listing["id"], "dev.dreaminko.vpm")
            self.assertEqual(listing["name"], "Dreaminko's VPM Repository")
            self.assertEqual(listing["url"], "https://vpm.dreamo.ink/index.json")
            self.assertEqual(listing["author"], "Dreaminko")

            self.assertIn("dev.dreaminko.unitext", listing["packages"])
            self.assertIn("dev.dreaminko.secondpkg", listing["packages"])

            self.assertEqual(
                list(listing["packages"]["dev.dreaminko.unitext"]["versions"].keys()),
                ["1.10.0", "1.2.0", "1.2.0-rc.1", "1.2.0-beta.10", "1.2.0-beta.2"],
            )
            self.assertEqual(
                list(listing["packages"]["dev.dreaminko.secondpkg"]["versions"].keys()),
                ["0.10.0", "0.2.0"],
            )

            html_text = index_html_path.read_text(encoding="utf-8")
            self.assertIn("data-theme", html_text)
            self.assertIn("vcc://vpm/addRepo?url=https://vpm.dreamo.ink/index.json", html_text)
            self.assertIn("2 packages · 7 releases", html_text)
            self.assertIn("UniText", html_text)
            self.assertIn("Second Package", html_text)
            self.assertIn("dev.dreaminko.unitext", html_text)
            self.assertIn("dev.dreaminko.secondpkg", html_text)
            self.assertIn("com.vrchat.avatars &gt;=3.7.0", html_text)
            self.assertIn("nadena.dev.ndmf &gt;=1.5.0", html_text)
            self.assertIn(
                "https://github.com/Dreaminko/SecondPkg/releases/download/0.10.0/dev.dreaminko.secondpkg-0.10.0.zip",
                html_text,
            )
            self.assertIsNone(re.search(r"__[A-Z0-9_]+__", html_text))

            # XSS payload must be escaped, never emitted raw
            self.assertNotIn('<script>alert("xss")</script>', html_text)
            self.assertIn("&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;", html_text)

    def test_single_package_expands_details_and_empty_state_renders(self) -> None:
        source = json.loads((REPO_ROOT / "source.json").read_text(encoding="utf-8"))

        # Single package expands <details class="versions-details" open>
        packages_single, repo_by_pkg = build_listing.aggregate_packages(
            [(_make_manifest(), "Dreaminko/UniText")]
        )
        listing_single = build_listing.build_listing_data(source, packages_single)
        html_single = build_listing.render_html(source, listing_single, repo_by_pkg)
        self.assertIn('<details class="versions-details" open>', html_single)
        self.assertIn("1 package · 1 release", html_single)
        self.assertIn("https://github.com/Dreaminko/UniText", html_single)
        self.assertIsNone(re.search(r"__[A-Z0-9_]+__", html_single))

        # Empty listing renders empty state cleanly
        listing_empty = build_listing.build_listing_data(source, {})
        html_empty = build_listing.render_html(source, listing_empty, {})
        self.assertIn("No packages published yet.", html_empty)
        self.assertIn("0 packages · 0 releases", html_empty)
        self.assertIsNone(re.search(r"__[A-Z0-9_]+__", html_empty))

    def test_dispatch_retry_logic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_dir = Path(tmp_dir) / "_site"
            old_manifest = _make_manifest(version="1.1.0")
            new_manifest = _make_manifest(version="1.2.0")

            with (
                mock.patch.object(
                    build_listing,
                    "fetch_repo_manifests",
                    side_effect=[[old_manifest], [old_manifest, new_manifest]],
                ) as mocked_fetch,
                mock.patch.object(build_listing.time, "sleep") as mocked_sleep,
            ):
                rc = build_listing.main(
                    [
                        "--out-dir",
                        str(out_dir),
                        "--expect-package",
                        "dev.dreaminko.unitext",
                        "--expect-version",
                        "1.2.0",
                        "--retries",
                        "3",
                        "--retry-delay",
                        "0",
                    ]
                )

            self.assertEqual(rc, 0)
            self.assertEqual(mocked_fetch.call_count, 2)
            mocked_sleep.assert_called_once_with(0.0)
            listing = json.loads((out_dir / "index.json").read_text(encoding="utf-8"))
            self.assertIn("1.2.0", listing["packages"]["dev.dreaminko.unitext"]["versions"])

            # Fails with non-zero exit code if target version never appears
            with (
                mock.patch.object(
                    build_listing,
                    "fetch_repo_manifests",
                    return_value=[old_manifest],
                ),
                mock.patch.object(build_listing.time, "sleep"),
            ):
                rc_fail = build_listing.main(
                    [
                        "--out-dir",
                        str(out_dir),
                        "--expect-package",
                        "dev.dreaminko.unitext",
                        "--expect-version",
                        "9.9.9",
                        "--retries",
                        "2",
                        "--retry-delay",
                        "0",
                    ]
                )
            self.assertEqual(rc_fail, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
