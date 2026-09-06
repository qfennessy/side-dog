from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
DEPENDABOT_CONFIG = ROOT / ".github" / "dependabot.yml"
DEPENDENCY_REVIEW_WORKFLOW = (
    ROOT / ".github" / "workflows" / "dependency-review.yml"
)
MAINTENANCE_DOC = ROOT / "docs" / "dependency-maintenance.md"


def ecosystem_block(config: str, ecosystem: str) -> str:
    match = re.search(
        rf'^  - package-ecosystem: "{re.escape(ecosystem)}"\n'
        r"(?P<body>.*?)(?=^  - package-ecosystem:|\Z)",
        config,
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"missing Dependabot ecosystem: {ecosystem}")
    return match.group("body")


class DependencyAutomationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dependabot = DEPENDABOT_CONFIG.read_text(encoding="utf-8")
        cls.dependency_review = DEPENDENCY_REVIEW_WORKFLOW.read_text(
            encoding="utf-8"
        )
        cls.maintenance_doc = MAINTENANCE_DOC.read_text(encoding="utf-8")

    def test_dependabot_uses_native_uv_updates_with_bounded_weekly_prs(self) -> None:
        self.assertTrue(self.dependabot.startswith("version: 2\n"))
        uv = ecosystem_block(self.dependabot, "uv")
        self.assertIn('directory: "/"', uv)
        self.assertIn('interval: "weekly"', uv)
        self.assertIn('day: "monday"', uv)
        self.assertIn("open-pull-requests-limit: 2", uv)
        self.assertIn('versioning-strategy: "increase-if-necessary"', uv)
        self.assertIn("default-days: 7", uv)
        self.assertIn("python-minor-and-patch:", uv)
        self.assertIn('- "minor"', uv)
        self.assertIn('- "patch"', uv)

    def test_dependabot_groups_actions_updates_and_limits_noise(self) -> None:
        actions = ecosystem_block(self.dependabot, "github-actions")
        self.assertIn('directory: "/"', actions)
        self.assertIn('interval: "weekly"', actions)
        self.assertIn('day: "wednesday"', actions)
        self.assertIn("open-pull-requests-limit: 1", actions)
        self.assertIn("default-days: 7", actions)
        self.assertIn("github-actions:", actions)
        self.assertIn('- "*"', actions)

    def test_dependency_review_is_read_only_and_blocks_high_risk_changes(self) -> None:
        workflow = self.dependency_review
        self.assertIn("on:\n  pull_request:\n    branches:\n      - main", workflow)
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertNotRegex(workflow, r"(?m)^\s+[\w-]+: write$")
        self.assertRegex(
            workflow,
            r"actions/dependency-review-action@[0-9a-f]{40} # v5\.0\.0",
        )
        self.assertIn("fail-on-severity: high", workflow)

    def test_maintainer_doc_records_cadence_ownership_and_live_check(self) -> None:
        doc = self.maintenance_doc
        self.assertIn("Maintainers own", doc)
        self.assertIn("uv sync --locked", doc)
        self.assertIn("Check for updates", doc)
        self.assertIn("successful last-checked time", doc)
        self.assertIn("cannot be completed from an unmerged", doc)


if __name__ == "__main__":
    unittest.main()
