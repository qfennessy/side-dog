from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).parents[1]
POLICY = ROOT / "SECURITY.md"
ISSUE_TEMPLATE_CONFIG = ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml"
README = ROOT / "README.md"
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

    def test_public_issue_picker_routes_security_reports_privately(self) -> None:
        config = ISSUE_TEMPLATE_CONFIG.read_text(encoding="utf-8")

        self.assertIn("Report a security vulnerability privately", config)
        self.assertIn(PRIVATE_REPORT_URL, config)
        self.assertIn("Do not disclose vulnerabilities", config)

    def test_readme_links_to_the_security_policy(self) -> None:
        readme = README.read_text(encoding="utf-8")

        self.assertIn("[security policy](SECURITY.md)", readme)
        self.assertIn("Do not put security details", readme)
