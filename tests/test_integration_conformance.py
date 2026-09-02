"""Shared integration contracts that do not depend on vendor fixture formats."""

from __future__ import annotations

import json
import re
from contextlib import ExitStack
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from side_dog.cli import (
    DISPLAY_CODING_AGENTS,
    active_agent_identities,
    display_identities,
    load_agent_identities,
    normalized_tool_events,
    sync_native_streams,
)
from side_dog.integrations import (
    CODING_AGENT_PROVIDERS,
    INTEGRATIONS,
    IntegrationCapability,
    SetupRequirement,
)
from side_dog.model import agent_label, lane_key, normalize_agent
from side_dog.panel import PanelFeed


ALLOWED_CAPABILITIES = frozenset(IntegrationCapability)
ALLOWED_EVENT_STATUSES = frozenset({"failed", "running", "success", "unknown"})


COMMON_CAPABILITIES = frozenset(
    {
        IntegrationCapability.COLLECTS_ACTIVITY,
        IntegrationCapability.DISCOVERS_SESSIONS,
        IntegrationCapability.REPORTS_MODEL,
        IntegrationCapability.USES_COMMON_TOOL_NORMALIZER,
    }
)


class IntegrationContractTest(TestCase):
    def test_matrix_matches_the_supported_agent_inventory(self) -> None:
        providers = {contract.provider for contract in INTEGRATIONS}

        self.assertEqual(len(INTEGRATIONS), 9)
        self.assertEqual(len(providers), len(INTEGRATIONS))
        self.assertEqual(providers, CODING_AGENT_PROVIDERS)
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
            "cursor": "cursor_identities",
            "grok": "grok_identities",
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
                                "root": "/tmp/conformance",
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

        panel_rows = PanelFeed._agent_rows(identities, Path("/tmp/conformance"))
        self.assertEqual(len(panel_rows), len(INTEGRATIONS))
        self.assertEqual(
            {row["agent"] for row in panel_rows},
            {contract.provider for contract in INTEGRATIONS},
        )

        native_identities = {
            key: identity
            for key, identity in identities.items()
            if identity["agent"] in {"codex", "antigravity"}
        }
        streams = {}
        with (
            patch(
                "side_dog.cli._native_session_path",
                side_effect=lambda agent, _session: Path(f"/tmp/{agent}.jsonl"),
            ),
            patch("side_dog.cli.load_native_stream_position", return_value=0),
        ):
            attached = sync_native_streams(
                Path("/tmp/conformance"), native_identities, streams
            )
        self.assertEqual(
            set(streams),
            {"codex:shared-session", "antigravity:shared-session"},
        )
        self.assertEqual(set(attached), set(streams))
        self.assertEqual(
            {stream.agent for stream in streams.values()}, {"codex", "antigravity"}
        )
        self.assertEqual(
            {
                lane_key(
                    {"agent": contract.provider, "session_id": "shared-session"},
                    identities,
                )
                for contract in INTEGRATIONS
            },
            {f"{contract.provider}:shared-session" for contract in INTEGRATIONS},
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

        hook_users = {
            contract.provider
            for contract in INTEGRATIONS
            if contract.supports(IntegrationCapability.PROJECT_HOOKS_FOR_ACTIVITY)
        }
        self.assertEqual(hook_users, {"claude-code"})
        self.assertEqual(
            {
                contract.provider
                for contract in INTEGRATIONS
                if contract.setup is SetupRequirement.OPTIONAL_PROJECT_HOOKS
            },
            {"claude-code"},
        )

    def test_common_normalizer_emits_only_declared_event_statuses(self) -> None:
        for contract in INTEGRATIONS:
            for status in ALLOWED_EVENT_STATUSES:
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
