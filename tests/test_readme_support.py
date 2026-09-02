from __future__ import annotations

from pathlib import Path
from unittest import TestCase

from side_dog.integrations import (
    CONTEXT_PROVIDERS,
    INTEGRATIONS,
    SetupRequirement,
)


README = Path(__file__).parents[1] / "README.md"


def table_rows(header: str) -> list[list[str]]:
    lines = README.read_text(encoding="utf-8").splitlines()
    start = lines.index(header)
    rows: list[list[str]] = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows


class ReadmeSupportTest(TestCase):
    def test_support_table_matches_registered_integration_capabilities(self) -> None:
        agent_rows = table_rows(
            "| Agent | Finds and names sessions | Collects live activity | Setup |"
        )

        self.assertEqual(len(agent_rows), len(INTEGRATIONS))
        for row, integration in zip(agent_rows, INTEGRATIONS, strict=True):
            with self.subTest(integration=integration.provider):
                uses_hooks = (
                    integration.setup is SetupRequirement.OPTIONAL_PROJECT_HOOKS
                )
                expected_setup = (
                    "Optional project hooks: run `side-dog setup . --claude`, "
                    "then restart Claude Code"
                    if uses_hooks
                    else "None"
                )
                self.assertEqual(
                    row,
                    [
                        f"**{integration.product_name}**",
                        integration.session_discovery_summary,
                        integration.activity_source_summary,
                        expected_setup,
                    ],
                )

    def test_context_providers_are_separate_optional_rows(self) -> None:
        agent_rows = table_rows(
            "| Agent | Finds and names sessions | Collects live activity | Setup |"
        )
        rows = table_rows(
            "| Context provider | What it adds | How Side Dog uses it | Setup |"
        )

        self.assertNotIn("Herdr", " ".join(" ".join(row) for row in agent_rows))
        self.assertEqual(len(rows), len(CONTEXT_PROVIDERS))
        for row, context in zip(rows, CONTEXT_PROVIDERS, strict=True):
            self.assertEqual(
                row,
                [
                    f"**{context.product_name}**",
                    context.session_discovery_summary,
                    context.activity_source_summary,
                    "Optional",
                ],
            )

    def test_every_registered_environment_override_is_documented(self) -> None:
        readme = README.read_text(encoding="utf-8")

        for integration in INTEGRATIONS:
            for override in integration.environment_overrides:
                with self.subTest(
                    integration=integration.provider, variable=override.name
                ):
                    self.assertIn(f"`{override.name}`", readme)

    def test_sundai_inspiration_is_retained(self) -> None:
        readme = README.read_text(encoding="utf-8")

        self.assertIn("[Sundai Hack 138](https://sundai.club)", readme)
        self.assertIn(
            "Sundai Club is\na community for building and launching AI prototypes "
            "every Sunday.",
            readme,
        )
