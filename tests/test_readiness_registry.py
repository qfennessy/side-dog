from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from side_dog.doctor import _cline_locations, _override_guidance, integration_readiness
from side_dog.integrations import (
    CONTEXT_PROVIDERS,
    HERDR_CONTEXT,
    T3CODE_CONTEXT,
    INTEGRATIONS,
    AdapterHealth,
    AdapterHealthStatus,
    SetupRequirement,
)


class ReadinessRegistryTests(unittest.TestCase):
    def test_every_agent_declares_end_user_support_facts(self) -> None:
        self.assertEqual(len(INTEGRATIONS), 10)
        for descriptor in INTEGRATIONS:
            with self.subTest(provider=descriptor.provider):
                self.assertTrue(descriptor.product_name)
                self.assertTrue(descriptor.session_discovery_summary)
                self.assertTrue(descriptor.activity_source_summary)
                self.assertIsNotNone(descriptor.readiness_probe)

        hooked = [
            descriptor.provider
            for descriptor in INTEGRATIONS
            if descriptor.setup is SetupRequirement.OPTIONAL_PROJECT_HOOKS
        ]
        self.assertEqual(hooked, ["claude-code"])

    def test_environment_overrides_are_ordered_and_complete(self) -> None:
        overrides = {
            descriptor.provider: tuple(
                item.name for item in descriptor.environment_overrides
            )
            for descriptor in INTEGRATIONS
        }
        self.assertEqual(
            overrides,
            {
                "codex": ("CODEX_HOME",),
                "claude-code": (),
                "pi": ("PI_CODING_AGENT_DIR",),
                "opencode": ("XDG_DATA_HOME",),
                "crush": ("CRUSH_GLOBAL_DATA",),
                "cursor": ("T3CODE_HOME",),
                "grok": ("T3CODE_HOME",),
                "deepseek": ("DSH_HOME",),
                "cline": (
                    "CLINE_DIR",
                    "CLINE_DATA_DIR",
                    "CLINE_DB_DATA_DIR",
                    "CLINE_SESSION_DATA_DIR",
                ),
                "antigravity": ("ANTIGRAVITY_APP_DATA_DIR", "GEMINI_HOME"),
            },
        )

    def test_environment_override_guidance_explains_each_override(self) -> None:
        for descriptor in INTEGRATIONS:
            guidance = _override_guidance(descriptor.provider)
            for override in descriptor.environment_overrides:
                with self.subTest(provider=descriptor.provider, override=override.name):
                    self.assertIn(override.name, guidance)
                    self.assertIn(override.purpose, guidance)

    def test_context_sources_are_optional_and_not_coding_agents(self) -> None:
        self.assertEqual(CONTEXT_PROVIDERS, (HERDR_CONTEXT, T3CODE_CONTEXT))
        self.assertEqual(HERDR_CONTEXT.key, "herdr")
        self.assertTrue(HERDR_CONTEXT.optional)
        providers = {item.provider for item in INTEGRATIONS}
        self.assertNotIn("herdr", providers)
        self.assertNotIn("t3code", providers)
        self.assertIn("pane", HERDR_CONTEXT.session_discovery_summary)
        self.assertIn("Cursor", T3CODE_CONTEXT.activity_source_summary)

    def test_all_registered_probes_return_typed_unavailable_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            environment = {"HOME": os.fspath(Path(directory) / "home")}
            with patch("side_dog.doctor.shutil.which", return_value=None):
                health = [
                    descriptor.readiness_probe(root, environment)
                    for descriptor in INTEGRATIONS
                    if descriptor.readiness_probe is not None
                ]
            self.assertFalse((Path(directory) / "home").exists())

        self.assertEqual(len(health), len(INTEGRATIONS))
        self.assertEqual(
            {item.adapter for item in health},
            {descriptor.provider for descriptor in INTEGRATIONS},
        )
        self.assertTrue(
            all(item.status is AdapterHealthStatus.UNAVAILABLE for item in health)
        )

    def test_opencode_probe_checks_sqlite_read_only(self) -> None:
        descriptor = next(item for item in INTEGRATIONS if item.provider == "opencode")
        assert descriptor.readiness_probe is not None
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            database = data / "opencode" / "opencode.db"
            database.parent.mkdir(parents=True)
            connection = sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE session ("
                "id TEXT, directory TEXT, title TEXT, model TEXT, agent TEXT, "
                "parent_id TEXT, time_updated INTEGER)"
            )
            connection.execute(
                "CREATE TABLE part ("
                "id TEXT, data TEXT, time_updated INTEGER, session_id TEXT)"
            )
            connection.commit()
            connection.close()
            before = database.read_bytes()

            health = descriptor.readiness_probe(
                Path(directory), {"HOME": directory, "XDG_DATA_HOME": os.fspath(data)}
            )

            self.assertEqual(database.read_bytes(), before)
        self.assertIs(health.status, AdapterHealthStatus.AVAILABLE)

    def test_opencode_probe_degrades_for_incomplete_sqlite_schema(self) -> None:
        descriptor = next(item for item in INTEGRATIONS if item.provider == "opencode")
        assert descriptor.readiness_probe is not None
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            database = data / "opencode" / "opencode.db"
            database.parent.mkdir(parents=True)
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE session (id TEXT)")
            connection.commit()
            connection.close()

            health = descriptor.readiness_probe(
                Path(directory), {"HOME": directory, "XDG_DATA_HOME": os.fspath(data)}
            )

        self.assertIs(health.status, AdapterHealthStatus.DEGRADED)

    def test_opencode_probe_degrades_for_missing_or_incompatible_part_table(
        self,
    ) -> None:
        descriptor = next(item for item in INTEGRATIONS if item.provider == "opencode")
        assert descriptor.readiness_probe is not None
        part_schemas = {
            "missing": None,
            "missing session foreign key": (
                "CREATE TABLE part (id TEXT, data TEXT, time_updated INTEGER)"
            ),
        }
        for scenario, part_schema in part_schemas.items():
            with (
                self.subTest(scenario=scenario),
                tempfile.TemporaryDirectory() as directory,
            ):
                data = Path(directory) / "data"
                database = data / "opencode" / "opencode.db"
                database.parent.mkdir(parents=True)
                connection = sqlite3.connect(database)
                connection.execute(
                    "CREATE TABLE session ("
                    "id TEXT, directory TEXT, title TEXT, model TEXT, agent TEXT, "
                    "parent_id TEXT, time_updated INTEGER)"
                )
                if part_schema is not None:
                    connection.execute(part_schema)
                connection.commit()
                connection.close()

                health = descriptor.readiness_probe(
                    Path(directory),
                    {"HOME": directory, "XDG_DATA_HOME": os.fspath(data)},
                )

            self.assertIs(health.status, AdapterHealthStatus.DEGRADED)

    def test_opencode_explicit_missing_data_home_is_degraded(self) -> None:
        descriptor = next(item for item in INTEGRATIONS if item.provider == "opencode")
        assert descriptor.readiness_probe is not None
        with tempfile.TemporaryDirectory() as directory:
            health = descriptor.readiness_probe(
                Path(directory),
                {
                    "HOME": directory,
                    "XDG_DATA_HOME": os.fspath(Path(directory) / "missing"),
                },
            )

        self.assertIs(health.status, AdapterHealthStatus.DEGRADED)

    def test_opencode_relative_data_home_is_degraded(self) -> None:
        descriptor = next(item for item in INTEGRATIONS if item.provider == "opencode")
        assert descriptor.readiness_probe is not None

        health = descriptor.readiness_probe(
            Path.cwd(), {"HOME": os.fspath(Path.home()), "XDG_DATA_HOME": "data"}
        )

        self.assertIs(health.status, AdapterHealthStatus.DEGRADED)
        self.assertIn("absolute path", health.detail)

    def test_cline_probe_checks_sqlite_query_schema(self) -> None:
        descriptor = next(item for item in INTEGRATIONS if item.provider == "cline")
        assert descriptor.readiness_probe is not None
        with tempfile.TemporaryDirectory() as directory:
            database_directory = Path(directory) / "db"
            database_directory.mkdir()
            database = database_directory / "sessions.db"
            connection = sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE sessions ("
                "session_id TEXT, pid INTEGER, status TEXT, cwd TEXT, "
                "workspace_root TEXT, model TEXT, metadata_json TEXT, "
                "messages_path TEXT, updated_at TEXT, started_at TEXT, "
                "parent_session_id TEXT, is_subagent INTEGER)"
            )
            connection.commit()
            connection.close()
            before = database.read_bytes()

            health = descriptor.readiness_probe(
                Path(directory),
                {"HOME": directory, "CLINE_DB_DATA_DIR": os.fspath(database_directory)},
            )

            self.assertEqual(database.read_bytes(), before)
        self.assertIs(health.status, AdapterHealthStatus.AVAILABLE)

    def test_cline_probe_degrades_for_incomplete_sqlite_schema(self) -> None:
        descriptor = next(item for item in INTEGRATIONS if item.provider == "cline")
        assert descriptor.readiness_probe is not None
        with tempfile.TemporaryDirectory() as directory:
            database_directory = Path(directory) / "db"
            database_directory.mkdir()
            database = database_directory / "sessions.db"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE sessions (session_id TEXT)")
            connection.commit()
            connection.close()

            health = descriptor.readiness_probe(
                Path(directory),
                {"HOME": directory, "CLINE_DB_DATA_DIR": os.fspath(database_directory)},
            )

        self.assertIs(health.status, AdapterHealthStatus.DEGRADED)

    def test_cline_empty_overrides_use_default_locations_and_are_unavailable(
        self,
    ) -> None:
        descriptor = next(item for item in INTEGRATIONS if item.provider == "cline")
        assert descriptor.readiness_probe is not None
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            environment = {
                "HOME": os.fspath(home),
                "CLINE_DIR": "",
                "CLINE_DATA_DIR": "",
                "CLINE_DB_DATA_DIR": "",
                "CLINE_SESSION_DATA_DIR": "",
            }

            sessions, database = _cline_locations(environment)
            health = descriptor.readiness_probe(Path(directory), environment)

        self.assertEqual(sessions, home / ".cline" / "data" / "sessions")
        self.assertEqual(database, home / ".cline" / "data" / "db" / "sessions.db")
        self.assertIs(health.status, AdapterHealthStatus.UNAVAILABLE)

    def test_cline_relative_database_locations_are_degraded(self) -> None:
        descriptor = next(item for item in INTEGRATIONS if item.provider == "cline")
        assert descriptor.readiness_probe is not None
        for override in ("CLINE_DIR", "CLINE_DATA_DIR", "CLINE_DB_DATA_DIR"):
            with self.subTest(override=override):
                health = descriptor.readiness_probe(
                    Path.cwd(),
                    {"HOME": os.fspath(Path.home()), override: "cline-db"},
                )

                self.assertIs(health.status, AdapterHealthStatus.DEGRADED)
                self.assertIn(f"{override} must be an absolute path", health.detail)

    def test_gemini_home_alone_does_not_make_antigravity_degraded(self) -> None:
        descriptor = next(
            item for item in INTEGRATIONS if item.provider == "antigravity"
        )
        assert descriptor.readiness_probe is not None
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / ".gemini").mkdir()
            health = descriptor.readiness_probe(Path(directory), {"HOME": directory})

        self.assertIs(health.status, AdapterHealthStatus.UNAVAILABLE)

    def test_antigravity_explicit_missing_app_data_is_degraded(self) -> None:
        descriptor = next(
            item for item in INTEGRATIONS if item.provider == "antigravity"
        )
        assert descriptor.readiness_probe is not None
        with tempfile.TemporaryDirectory() as directory:
            health = descriptor.readiness_probe(
                Path(directory),
                {
                    "HOME": directory,
                    "ANTIGRAVITY_APP_DATA_DIR": os.fspath(Path(directory) / "missing"),
                },
            )

        self.assertIs(health.status, AdapterHealthStatus.DEGRADED)

    def test_antigravity_explicit_missing_gemini_home_is_degraded(self) -> None:
        descriptor = next(
            item for item in INTEGRATIONS if item.provider == "antigravity"
        )
        assert descriptor.readiness_probe is not None
        with tempfile.TemporaryDirectory() as directory:
            health = descriptor.readiness_probe(
                Path(directory),
                {
                    "HOME": directory,
                    "GEMINI_HOME": os.fspath(Path(directory) / "missing"),
                },
            )

        self.assertIs(health.status, AdapterHealthStatus.DEGRADED)

    def test_bad_probe_is_degraded_without_exposing_the_exception(self) -> None:
        def bad_probe(_root, _environment):
            raise RuntimeError("prompt and source content")

        descriptor = replace(INTEGRATIONS[0], readiness_probe=bad_probe)
        health = integration_readiness(descriptor, Path("/tmp"), {})

        self.assertIs(health.status, AdapterHealthStatus.DEGRADED)
        self.assertNotIn("prompt", health.detail)

    def test_adapter_health_result_is_provider_scoped(self) -> None:
        descriptor = replace(
            INTEGRATIONS[0],
            readiness_probe=lambda _root, _environment: AdapterHealth(
                "pi", AdapterHealthStatus.AVAILABLE, "ready"
            ),
        )
        health = integration_readiness(descriptor, Path("/tmp"), {})

        self.assertEqual(health.adapter, descriptor.provider)
        self.assertIs(health.status, AdapterHealthStatus.DEGRADED)


if __name__ == "__main__":
    unittest.main()
