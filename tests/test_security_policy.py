import re
from pathlib import Path
from unittest import TestCase

from side_dog import __version__


ROOT = Path(__file__).parents[1]
POLICY = ROOT / "SECURITY.md"
ISSUE_TEMPLATE_CONFIG = ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml"
README = ROOT / "README.md"
CHANGELOG = ROOT / "CHANGELOG.md"
PRIVATE_REPORT_URL = (
    "https://github.com/qfennessy/side-dog/security/advisories/new"
)


class SecurityPolicyTest(TestCase):
    def test_policy_routes_reports_to_github_private_reporting(self) -> None:
        policy = POLICY.read_text(encoding="utf-8")

        self.assertIn(PRIVATE_REPORT_URL, policy)
        self.assertIn(
            "Do not describe a suspected vulnerability in a public issue", policy
        )
        self.assertIn("There is no guaranteed response or remediation time", policy)

    def test_policy_warns_against_sharing_sensitive_local_activity(self) -> None:
        policy = POLICY.read_text(encoding="utf-8")

        for sensitive_item in ("tokens", "prompts", "source or file\ncontents"):
            with self.subTest(sensitive_item=sensitive_item):
                self.assertIn(sensitive_item, policy)

    def test_policy_matches_the_current_release_state(self) -> None:
        self.assert_policy_matches_release_state(
            POLICY.read_text(encoding="utf-8"),
            CHANGELOG.read_text(encoding="utf-8"),
            __version__,
        )

    def test_policy_accepts_a_dated_supported_release(self) -> None:
        self.assert_policy_matches_release_state(
            "| 1.1.x | Supported |\n| Earlier versions | Unsupported |\n",
            "## [1.1.0] - 2026-09-06\n",
            "1.1.0",
        )

    def assert_policy_matches_release_state(
        self, policy: str, changelog: str, version: str
    ) -> None:
        major, minor, _patch = version.split(".")
        release_line = f"{major}.{minor}.x"
        heading = re.search(
            rf"^## \[{re.escape(version)}\] - (?P<state>Unreleased|\d{{4}}-\d{{2}}-\d{{2}})$",
            changelog,
            re.MULTILINE,
        )

        self.assertIsNotNone(heading)
        if heading is not None and heading.group("state") == "Unreleased":
            self.assertIn(
                f"| {release_line} (`main`) | Unreleased development |", policy
            )
            dated_versions = re.findall(
                r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}$",
                changelog,
                re.MULTILINE,
            )
            self.assertTrue(dated_versions)
            latest_dated = max(
                dated_versions, key=lambda item: tuple(map(int, item.split(".")))
            )
            stable_major, stable_minor, _stable_patch = latest_dated.split(".")
            self.assertIn(
                f"| {stable_major}.{stable_minor}.x | Supported |", policy
            )
        else:
            self.assertIn(f"| {release_line} | Supported |", policy)
            self.assertNotIn(f"| {release_line} (`main`) |", policy)

    def test_public_issue_picker_routes_security_reports_privately(self) -> None:
        config = ISSUE_TEMPLATE_CONFIG.read_text(encoding="utf-8")

        self.assertIn("Report a security vulnerability privately", config)
        self.assertIn(PRIVATE_REPORT_URL, config)
        self.assertIn("Do not disclose vulnerabilities", config)

    def test_readme_links_to_the_security_policy(self) -> None:
        readme = README.read_text(encoding="utf-8")

        self.assertIn(
            "[security policy](https://github.com/qfennessy/side-dog/security/policy)",
            readme,
        )
        self.assertIn("Do not put security details", readme)
