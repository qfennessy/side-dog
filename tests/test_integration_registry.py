from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import side_dog.integrations as integration_module
from side_dog.cli import agent_working_folders
from side_dog.integrations import (
    CODING_AGENT_PROVIDERS,
    INTEGRATIONS,
    INTEGRATION_ALIASES,
    INTEGRATION_REGISTRY,
    EventSource,
    IntegrationCapability,
    LazyCliCallable,
    SetupRequirement,
    integration_for,
    normalize_provider,
)


class IntegrationRegistryTest(unittest.TestCase):
    def test_registry_is_the_complete_coding_agent_inventory(self) -> None:
        self.assertEqual(
            CODING_AGENT_PROVIDERS,
            {
                "codex",
                "claude-code",
                "pi",
                "opencode",
                "cursor",
                "grok",
                "deepseek",
                "cline",
                "antigravity",
            },
        )
        self.assertEqual(tuple(INTEGRATION_REGISTRY.values()), INTEGRATIONS)
        self.assertNotIn("herdr", INTEGRATION_REGISTRY)
        self.assertIsNone(integration_for("herdr"))

    def test_aliases_resolve_to_their_one_canonical_descriptor(self) -> None:
        for descriptor in INTEGRATIONS:
            for alias in descriptor.aliases:
                with self.subTest(provider=descriptor.provider, alias=alias):
                    self.assertIs(integration_for(alias), descriptor)
                    self.assertEqual(normalize_provider(alias), descriptor.provider)
        expected_aliases = {
            alias for descriptor in INTEGRATIONS for alias in descriptor.aliases
        }
        self.assertEqual(set(INTEGRATION_ALIASES), expected_aliases)

    def test_valid_unknown_providers_remain_visible_without_a_fake_descriptor(
        self,
    ) -> None:
        for provider in ("future-agent", "future_agent.v2"):
            with self.subTest(provider=provider):
                self.assertEqual(normalize_provider(provider), provider)
                self.assertIsNone(integration_for(provider))

        self.assertEqual(normalize_provider("not a provider"), "unknown")
        self.assertIsNone(integration_for("not a provider"))

    def test_registered_hyphenated_aliases_accept_legacy_underscores(self) -> None:
        self.assertEqual(normalize_provider("claude_code"), "claude-code")
        self.assertEqual(normalize_provider("deepseek_harness"), "deepseek")
        self.assertEqual(normalize_provider("antigravity_cli"), "antigravity")
        self.assertIs(integration_for("claude_code"), integration_for("claude-code"))

    def test_registry_mappings_are_read_only(self) -> None:
        with self.assertRaises(TypeError):
            INTEGRATION_REGISTRY["other"] = INTEGRATIONS[0]  # type: ignore[index]
        with self.assertRaises(TypeError):
            INTEGRATION_ALIASES["other"] = INTEGRATIONS[0]  # type: ignore[index]

    def test_registry_rejects_duplicate_providers_and_aliases(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate integration provider"):
            integration_module._build_registry((INTEGRATIONS[0], INTEGRATIONS[0]))

        alias_collision = replace(
            INTEGRATIONS[1], provider="other", aliases=("other", "codex")
        )
        with self.assertRaisesRegex(ValueError, "duplicate integration alias"):
            integration_module._build_registry((INTEGRATIONS[0], alias_collision))

    def test_working_folder_discovery_iterates_registered_hooks(self) -> None:
        loader = Mock(return_value=[("/repo", False), ("/repo", True)])
        descriptor = SimpleNamespace(working_folders_loader=loader)

        with (
            patch("side_dog.cli.INTEGRATIONS", (descriptor,)),
            patch("side_dog.cli.herdr_snapshot", return_value={}),
            patch("side_dog.cli.worktree_root_for", side_effect=Path),
        ):
            folders = agent_working_folders(now=123.0)

        loader.assert_called_once_with(123.0)
        self.assertEqual(folders, {Path("/repo"): True})

    def test_descriptors_declare_setup_event_source_and_supported_features(
        self,
    ) -> None:
        claude = integration_for("claude")
        self.assertIsNotNone(claude)
        assert claude is not None
        self.assertIs(claude.event_source, EventSource.PROJECT_HOOKS)
        self.assertIs(claude.setup, SetupRequirement.OPTIONAL_PROJECT_HOOKS)
        self.assertTrue(
            claude.supports(IntegrationCapability.PROJECT_HOOKS_FOR_ACTIVITY)
        )

        cline = integration_for("cline")
        self.assertIsNotNone(cline)
        assert cline is not None
        self.assertIs(cline.event_source, EventSource.LOCAL_SESSION_STORE)
        self.assertEqual(cline.readiness_probe.symbol, "cline_session_sources")

        for descriptor in INTEGRATIONS:
            with self.subTest(provider=descriptor.provider):
                self.assertTrue(
                    descriptor.supports(IntegrationCapability.COLLECTS_ACTIVITY)
                )
                self.assertTrue(
                    descriptor.supports(IntegrationCapability.DISCOVERS_SESSIONS)
                )
                self.assertIsNotNone(descriptor.readiness_probe)

        self.assertEqual(
            {
                descriptor.provider
                for descriptor in INTEGRATIONS
                if descriptor.supports(IntegrationCapability.REPORTS_EFFORT)
            },
            {
                "codex",
                "claude-code",
                "pi",
                "opencode",
                "cursor",
                "grok",
                "deepseek",
                "antigravity",
            },
        )

    def test_cli_references_are_lazy_and_resolve_only_when_called(self) -> None:
        calls: list[str] = []
        reference = LazyCliCallable("probe")
        module = SimpleNamespace(probe=lambda value: calls.append(value) or "ready")

        with patch("side_dog.integrations.import_module", return_value=module) as load:
            self.assertEqual(calls, [])
            self.assertEqual(reference("side-dog"), "ready")

        load.assert_called_once_with("side_dog.cli")
        self.assertEqual(calls, ["side-dog"])

    def test_every_registered_cli_reference_exists(self) -> None:
        for descriptor in INTEGRATIONS:
            references = (
                descriptor.identity_loader,
                descriptor.metadata_loader,
                descriptor.working_folders_loader,
                descriptor.readiness_probe,
            )
            for reference in references:
                if reference is None:
                    continue
                with self.subTest(
                    provider=descriptor.provider, symbol=reference.symbol
                ):
                    self.assertTrue(callable(reference.resolve()))


if __name__ == "__main__":
    unittest.main()
