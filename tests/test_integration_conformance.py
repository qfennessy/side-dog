"""Shared integration contracts that do not depend on vendor fixture formats.

This matrix is deliberately test-side while integrations still live in ``cli.py``.
When Side Dog gains a production integration registry, these assertions should be
able to consume that registry instead of maintaining a second inventory here.
"""

from __future__ import annotations

import json
import re
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from side_dog.cli import (
    DISPLAY_CODING_AGENTS,
    active_agent_identities,
    display_identities,
    load_agent_identities,
    normalized_tool_events,
)
from side_dog.model import agent_label, normalize_agent


ALLOWED_CAPABILITIES = frozenset(
    {
        "collects_activity",
        "discovers_sessions",
        "project_hooks_for_activity",
        "reports_effort",
        "reports_model",
        "reports_subagents",
        "uses_common_tool_normalizer",
    }
)
ALLOWED_IDENTITY_STATUSES = frozenset({"idle", "working"})
ALLOWED_EVENT_STATUSES = frozenset({"failed", "running", "success", "unknown"})


@dataclass(frozen=True)
class IntegrationContract:
    """Small common surface every current coding-agent integration declares."""

    provider: str
    label: str
    aliases: tuple[str, ...]
    capabilities: frozenset[str]
    identity_statuses: frozenset[str] = ALLOWED_IDENTITY_STATUSES
    event_statuses: frozenset[str] = ALLOWED_EVENT_STATUSES


COMMON_CAPABILITIES = frozenset(
    {
        "collects_activity",
        "discovers_sessions",
        "reports_model",
        "uses_common_tool_normalizer",
    }
)

INTEGRATIONS = (
    IntegrationContract(
        provider="codex",
        label="Codex",
        aliases=("codex",),
        capabilities=COMMON_CAPABILITIES | {"reports_effort", "reports_subagents"},
    ),
    IntegrationContract(
        provider="claude-code",
        label="Claude",
        aliases=("claude", "claude-code"),
        capabilities=COMMON_CAPABILITIES
        | {"project_hooks_for_activity", "reports_effort"},
    ),
    IntegrationContract(
        provider="pi",
        label="Pi",
        aliases=("pi",),
        capabilities=COMMON_CAPABILITIES | {"reports_effort"},
    ),
    IntegrationContract(
        provider="opencode",
        label="Opencode",
        aliases=("opencode",),
        capabilities=COMMON_CAPABILITIES | {"reports_effort", "reports_subagents"},
    ),
    IntegrationContract(
        provider="deepseek",
        label="DeepSeek",
        aliases=("deepseek", "deepseek-harness", "dsh"),
        capabilities=COMMON_CAPABILITIES | {"reports_effort", "reports_subagents"},
    ),
    IntegrationContract(
        provider="cline",
        label="Cline",
        aliases=("cline",),
        capabilities=COMMON_CAPABILITIES | {"reports_subagents"},
    ),
    IntegrationContract(
        provider="antigravity",
        label="Antigravity",
        aliases=("antigravity", "antigravity-cli", "agy"),
        capabilities=COMMON_CAPABILITIES | {"reports_subagents"},
    ),
)


class IntegrationContractTest(TestCase):
    def test_matrix_matches_the_supported_agent_inventory(self) -> None:
        providers = {contract.provider for contract in INTEGRATIONS}

        self.assertEqual(len(INTEGRATIONS), 7)
        self.assertEqual(len(providers), len(INTEGRATIONS))
        self.assertEqual(providers, DISPLAY_CODING_AGENTS)

    def test_aliases_normalize_to_one_canonical_name_and_label(self) -> None:
        for contract in INTEGRATIONS:
            with self.subTest(provider=contract.provider):
                self.assertIn(contract.provider, contract.aliases)
                self.assertEqual(
                    {normalize_agent(alias) for alias in contract.aliases},
                    {contract.provider},
                )
                self.assertEqual(agent_label(contract.provider), contract.label)

    def test_equal_external_session_ids_survive_the_production_display_path(
        self,
    ) -> None:
        source_names = {
            "codex": "load_codex_session_identities",
            "claude-code": "claude_identities",
            "pi": "load_pi_session_identities",
            "opencode": "opencode_identities",
            "deepseek": "load_deepseek_session_identities",
            "cline": "cline_identities",
            "antigravity": "load_antigravity_session_identities",
        }
        with ExitStack() as stack:
            stack.enter_context(
                patch("side_dog.cli.load_herdr_identities", return_value={})
            )
            for contract in INTEGRATIONS:
                stack.enter_context(
                    patch(
                        f"side_dog.cli.{source_names[contract.provider]}",
                        return_value={
                            "shared-session": {
                                "agent": contract.provider,
                                "label": contract.label,
                                "session_id": "shared-session",
                                "status": "working",
                            }
                        },
                    )
                )
            identities = load_agent_identities(Path("/tmp/conformance"))
        records = [
            {
                "agent": contract.provider,
                "session_id": "shared-session",
                "model": f"{contract.provider}-model",
            }
            for contract in INTEGRATIONS
        ]

        displayed = active_agent_identities(display_identities(records, identities))

        self.assertEqual(len(displayed), len(INTEGRATIONS))
        self.assertEqual(
            {identity["agent"] for identity in displayed},
            {contract.provider for contract in INTEGRATIONS},
        )
        self.assertEqual(
            {identity["label"] for identity in displayed},
            {contract.label for contract in INTEGRATIONS},
        )
        self.assertEqual(
            {identity["model"] for identity in displayed},
            {f"{contract.provider}-model" for contract in INTEGRATIONS},
        )

    def test_capabilities_and_statuses_use_the_shared_safe_vocabulary(self) -> None:
        for contract in INTEGRATIONS:
            with self.subTest(provider=contract.provider):
                self.assertTrue(COMMON_CAPABILITIES <= contract.capabilities)
                self.assertTrue(contract.capabilities <= ALLOWED_CAPABILITIES)
                self.assertTrue(
                    all(
                        re.fullmatch(r"[a-z][a-z0-9_]*", capability)
                        for capability in contract.capabilities
                    )
                )
                self.assertEqual(contract.identity_statuses, ALLOWED_IDENTITY_STATUSES)
                self.assertEqual(contract.event_statuses, ALLOWED_EVENT_STATUSES)

        hook_users = {
            contract.provider
            for contract in INTEGRATIONS
            if "project_hooks_for_activity" in contract.capabilities
        }
        self.assertEqual(hook_users, {"claude-code"})

    def test_common_normalizer_emits_only_declared_event_statuses(self) -> None:
        for contract in INTEGRATIONS:
            for status in contract.event_statuses:
                with self.subTest(provider=contract.provider, status=status):
                    events = normalized_tool_events(
                        {
                            "agent": contract.provider,
                            "session_id": "shared-session",
                            "tool_use_id": "test-call",
                            "tool_name": "Bash",
                            "tool_input": {"command": "pytest tests"},
                        },
                        Path("/tmp/side-dog-conformance"),
                        status=status,
                    )

                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0]["agent"], contract.provider)
                    self.assertEqual(events[0]["kind"], "test")
                    self.assertEqual(events[0]["status"], status)
                    self.assertIn(events[0]["status"], ALLOWED_EVENT_STATUSES)

    def test_common_normalizer_does_not_copy_private_command_data(self) -> None:
        private_value = "SIDE_DOG_PRIVACY_CANARY_74c15b"
        private_path = "/Users/private-person/secret-project"
        command = (
            f"PRIVATE_TOKEN={private_value} {private_path}/private-runner "
            f"--prompt {private_value}"
        )

        for contract in INTEGRATIONS:
            with self.subTest(provider=contract.provider):
                events = normalized_tool_events(
                    {
                        "agent": contract.provider,
                        "session_id": "shared-session",
                        "tool_use_id": "private-call",
                        "tool_name": "Bash",
                        "tool_input": {"command": command},
                    },
                    Path("/tmp/side-dog-conformance"),
                    status="failed",
                )
                serialized = json.dumps(events)

                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["kind"], "command")
                self.assertEqual(events[0]["detail"], "private-runner")
                self.assertNotIn(private_value, serialized)
                self.assertNotIn(private_path, serialized)
                self.assertNotIn(command, serialized)
