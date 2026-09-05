from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from side_dog.cli import (
    SCHEMA,
    STARTUP_HISTORY_TAIL_LIMIT,
    STARTUP_USAGE_SESSION_LIMIT,
    STATE_ENV,
    _read_new_events_handle,
    _read_startup_summary,
    _startup_summary_digest,
    events_path,
    initialize_watch_root,
    load_startup_history,
    read_new_events,
    startup_summary_path,
)
from side_dog.model import github_fingerprint, latest_delivery_context


class StartupSummaryTests(unittest.TestCase):
    def event(
        self,
        root: Path,
        index: int,
        **changes: object,
    ) -> dict[str, object]:
        epoch_ms = 1_700_000_000_000 + index
        event: dict[str, object] = {
            "schema": SCHEMA,
            "timestamp": datetime.fromtimestamp(
                epoch_ms / 1000, timezone.utc
            ).isoformat(timespec="milliseconds"),
            "epoch_ms": epoch_ms,
            "agent": "filesystem",
            "project": os.fspath(root.resolve()),
            "kind": "file",
            "status": "success",
            "title": "File changed",
            "detail": f"src/file-{index}.py",
        }
        event.update(changes)
        return event

    def write_events(
        self, path: Path, events: list[dict[str, object]], *, newline: bool = True
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(
            json.dumps(event, separators=(",", ":"), ensure_ascii=False)
            for event in events
        )
        path.write_text(body + ("\n" if newline and body else ""), encoding="utf-8")

    def append_event(self, path: Path, event: dict[str, object]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")

    def resign_summary(self, value: dict[str, object]) -> None:
        unsigned = {key: item for key, item in value.items() if key != "checksum"}
        value["checksum"] = _startup_summary_digest(unsigned)

    def test_cold_build_persists_only_a_bounded_validated_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            path = Path(directory) / "state" / "events.jsonl"
            events = [
                self.event(root, index) for index in range(STARTUP_HISTORY_TAIL_LIMIT + 25)
            ]
            self.write_events(path, events)

            startup = load_startup_history(root, path)

            self.assertEqual(startup.cache_status, "cold")
            self.assertEqual(len(startup.records), STARTUP_HISTORY_TAIL_LIMIT)
            self.assertEqual(startup.records[0]["detail"], "src/file-25.py")
            self.assertEqual(startup.position, path.stat().st_size)
            summary = json.loads(path.with_name("startup-summary.json").read_text())
            self.assertEqual(summary["schema"], "side-dog-startup-summary-v1")
            self.assertEqual(summary["event_schema"], SCHEMA)
            self.assertEqual(summary["project"], os.fspath(root.resolve()))
            self.assertEqual(summary["offset"], path.stat().st_size)
            self.assertEqual(len(summary["tail"]), STARTUP_HISTORY_TAIL_LIMIT)
            self.assertEqual(summary["source"]["size"], summary["offset"])
            self.assertEqual(len(summary["source"]["head_sha256"]), 64)
            self.assertEqual(len(summary["source"]["boundary_sha256"]), 64)

    def test_warm_reuse_does_not_replay_the_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            path = Path(directory) / "events.jsonl"
            self.write_events(path, [self.event(root, index) for index in range(20)])
            cold = load_startup_history(root, path)

            with patch("side_dog.cli.read_new_events", wraps=read_new_events) as reader:
                warm = load_startup_history(root, path)

            self.assertEqual(warm.cache_status, "warm")
            self.assertEqual(warm.records, cold.records)
            self.assertEqual(warm.position, cold.position)
            reader.assert_not_called()

    def test_suffix_reuse_starts_at_the_saved_cursor_and_updates_aggregates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            path = Path(directory) / "events.jsonl"
            first = self.event(root, 1, agent="codex", session_id="session-old")
            self.write_events(path, [first])
            cold = load_startup_history(root, path)
            github = {
                "number": 133,
                "title": "Startup summary",
                "state": "OPEN",
                "branch": "codex/issue-133-startup-summary",
                "merge_state": "CLEAN",
                "checks_total": 1,
                "checks_passed": 1,
                "checks_pending": 0,
                "checks_failed": 0,
            }
            appended = self.event(
                root,
                2,
                agent="github",
                session_id="session-new",
                kind="github",
                title="PR #133 checks passed",
                detail="",
                turn_id="delivery-133",
                github=github,
                github_fingerprint=github_fingerprint(github),
            )
            self.append_event(path, appended)

            with patch("side_dog.cli.read_new_events", wraps=read_new_events) as reader:
                updated = load_startup_history(root, path)

            self.assertEqual(updated.cache_status, "suffix")
            reader.assert_called_once_with(path, cold.position, root.resolve())
            self.assertEqual(updated.position, path.stat().st_size)
            self.assertEqual(updated.latest_github, updated.records[-1])
            self.assertEqual(updated.latest_delivery, updated.records[-1])
            self.assertEqual(
                set(updated.usage_sessions),
                {("codex", "session-old"), ("github", "session-new")},
            )

    def test_usage_sessions_keep_a_bounded_most_recent_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            path = Path(directory) / "events.jsonl"
            events = [
                self.event(root, index, agent="codex", session_id=f"session-{index}")
                for index in range(STARTUP_USAGE_SESSION_LIMIT + 25)
            ]
            self.write_events(path, events)

            cold = load_startup_history(root, path)

            self.assertEqual(len(cold.usage_sessions), STARTUP_USAGE_SESSION_LIMIT)
            self.assertEqual(cold.usage_sessions[0], ("codex", "session-25"))
            self.assertEqual(
                cold.usage_sessions[-1],
                ("codex", f"session-{STARTUP_USAGE_SESSION_LIMIT + 24}"),
            )
            summary = json.loads(path.with_name("startup-summary.json").read_text())
            self.assertEqual(
                len(summary["usage_sessions"]), STARTUP_USAGE_SESSION_LIMIT
            )

            self.append_event(
                path,
                self.event(
                    root,
                    STARTUP_USAGE_SESSION_LIMIT + 25,
                    agent="codex",
                    session_id="session-new",
                ),
            )
            updated = load_startup_history(root, path)

            self.assertEqual(updated.cache_status, "suffix")
            self.assertEqual(len(updated.usage_sessions), STARTUP_USAGE_SESSION_LIMIT)
            self.assertEqual(updated.usage_sessions[0], ("codex", "session-26"))
            self.assertEqual(updated.usage_sessions[-1], ("codex", "session-new"))

            oversized = json.loads(
                path.with_name("startup-summary.json").read_text()
            )
            oversized["usage_sessions"].append(["codex", "overflow-session"])
            self.resign_summary(oversized)
            path.with_name("startup-summary.json").write_text(
                json.dumps(oversized), encoding="utf-8"
            )

            rebuilt = load_startup_history(root, path)

            self.assertEqual(rebuilt.cache_status, "invalidated")
            self.assertEqual(
                len(rebuilt.usage_sessions), STARTUP_USAGE_SESSION_LIMIT
            )
            repaired = json.loads(
                path.with_name("startup-summary.json").read_text()
            )
            self.assertEqual(
                len(repaired["usage_sessions"]), STARTUP_USAGE_SESSION_LIMIT
            )

    def test_partial_suffix_is_recovered_after_the_line_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            path = Path(directory) / "events.jsonl"
            self.write_events(path, [self.event(root, 1)])
            cold = load_startup_history(root, path)
            encoded = json.dumps(self.event(root, 2), separators=(",", ":")).encode()
            split = len(encoded) // 2
            with path.open("ab") as handle:
                handle.write(encoded[:split])

            partial = load_startup_history(root, path)

            self.assertEqual(partial.cache_status, "invalidated")
            self.assertEqual(partial.position, cold.position)
            self.assertEqual(len(partial.records), 1)
            with path.open("ab") as handle:
                handle.write(encoded[split:] + b"\n")

            recovered = load_startup_history(root, path)

            self.assertEqual(recovered.cache_status, "suffix")
            self.assertEqual(len(recovered.records), 2)
            self.assertEqual(recovered.records[-1]["detail"], "src/file-2.py")
            self.assertEqual(recovered.position, path.stat().st_size)

    def test_atomic_summary_failure_keeps_old_summary_and_loses_no_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            path = Path(directory) / "events.jsonl"
            self.write_events(path, [self.event(root, 1)])
            load_startup_history(root, path)
            summary_path = path.with_name("startup-summary.json")
            before = summary_path.read_bytes()
            self.append_event(path, self.event(root, 2))

            with patch("side_dog.cli.os.replace", side_effect=OSError("crash")):
                current = load_startup_history(root, path)

            self.assertEqual(current.records[-1]["detail"], "src/file-2.py")
            self.assertEqual(current.position, path.stat().st_size)
            self.assertEqual(summary_path.read_bytes(), before)
            repaired = load_startup_history(root, path)
            self.assertEqual(repaired.cache_status, "suffix")
            self.assertEqual([item["detail"] for item in repaired.records], [
                "src/file-1.py",
                "src/file-2.py",
            ])

    def test_corruption_versions_identity_and_source_changes_invalidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            path = Path(directory) / "events.jsonl"
            self.write_events(path, [self.event(root, 1)])
            load_startup_history(root, path)
            summary_path = path.with_name("startup-summary.json")

            summary_path.write_text("not json", encoding="utf-8")
            self.assertEqual(load_startup_history(root, path).cache_status, "invalidated")

            value = json.loads(summary_path.read_text())
            value["privacy_policy"] = "obsolete-policy"
            self.resign_summary(value)
            summary_path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(load_startup_history(root, path).cache_status, "invalidated")

            value = json.loads(summary_path.read_text())
            value["project"] = os.fspath(Path(directory) / "moved-project")
            self.resign_summary(value)
            summary_path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(load_startup_history(root, path).cache_status, "invalidated")

            self.write_events(path, [self.event(root, 9)])
            truncated = load_startup_history(root, path)
            self.assertEqual(truncated.cache_status, "invalidated")
            self.assertEqual([item["detail"] for item in truncated.records], [
                "src/file-9.py"
            ])

            replacement = path.with_name("replacement.jsonl")
            self.write_events(replacement, [self.event(root, 10)])
            os.replace(replacement, path)
            replaced = load_startup_history(root, path)
            self.assertEqual(replaced.cache_status, "invalidated")
            self.assertEqual(replaced.records[-1]["detail"], "src/file-10.py")

    def test_oversized_json_integer_invalidates_and_repairs_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            path = Path(directory) / "events.jsonl"
            self.write_events(path, [self.event(root, 1)])
            load_startup_history(root, path)
            summary_path = path.with_name("startup-summary.json")
            summary_path.write_text(
                '{"schema":' + ("9" * 641) + "}",
                encoding="utf-8",
            )
            previous_limit = sys.get_int_max_str_digits()
            try:
                sys.set_int_max_str_digits(640)
                rebuilt = load_startup_history(root, path)
            finally:
                sys.set_int_max_str_digits(previous_limit)

            self.assertEqual(rebuilt.cache_status, "invalidated")
            self.assertEqual(rebuilt.records[0]["detail"], "src/file-1.py")
            repaired = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(repaired["schema"], "side-dog-startup-summary-v1")

    def test_invalid_unicode_cannot_abort_summary_validation_or_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            path = Path(directory) / "events.jsonl"
            self.write_events(path, [self.event(root, 1)])
            load_startup_history(root, path)
            summary_path = path.with_name("startup-summary.json")
            corrupt = json.loads(summary_path.read_text(encoding="utf-8"))
            corrupt["usage_sessions"] = [["codex", "\ud800"]]
            summary_path.write_text(
                json.dumps(corrupt, ensure_ascii=True),
                encoding="utf-8",
            )

            rebuilt = load_startup_history(root, path)

            self.assertEqual(rebuilt.cache_status, "invalidated")
            self.assertEqual(rebuilt.records[0]["detail"], "src/file-1.py")

            summary_path.unlink()
            event = self.event(root, 2, agent="codex", session_id="\ud800")
            path.write_text(
                json.dumps(event, separators=(",", ":"), ensure_ascii=True) + "\n",
                encoding="utf-8",
            )

            cold = load_startup_history(root, path)
            warm = load_startup_history(root, path)

            self.assertEqual(cold.cache_status, "cold")
            self.assertEqual(cold.usage_sessions, (("codex", "\ud800"),))
            self.assertEqual(warm.cache_status, "warm")
            self.assertEqual(warm.usage_sessions, cold.usage_sessions)
            self.assertIn("\\ud800", summary_path.read_text(encoding="utf-8"))

    def test_rebuild_retries_if_source_rotates_after_scan(self) -> None:
        for invalidated in (False, True):
            with self.subTest(invalidated=invalidated):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory) / "project"
                    root.mkdir()
                    path = Path(directory) / "events.jsonl"
                    replacement = path.with_name("replacement.jsonl")
                    self.write_events(path, [self.event(root, 1)])
                    self.write_events(replacement, [self.event(root, 9)])
                    self.assertEqual(path.stat().st_size, replacement.stat().st_size)
                    if invalidated:
                        path.with_name("startup-summary.json").write_text(
                            "{}", encoding="utf-8"
                        )
                    rotated = False

                    def replace_after_scan(*args: Any, **kwargs: Any) -> Any:
                        nonlocal rotated
                        result = _read_new_events_handle(*args, **kwargs)
                        if not rotated:
                            os.replace(replacement, path)
                            rotated = True
                        return result

                    with patch(
                        "side_dog.cli._read_new_events_handle",
                        side_effect=replace_after_scan,
                    ):
                        rebuilt = load_startup_history(root, path)

                    warm = load_startup_history(root, path)
                    expected_status = "invalidated" if invalidated else "cold"
                    self.assertEqual(rebuilt.cache_status, expected_status)
                    self.assertEqual(
                        [record["detail"] for record in rebuilt.records],
                        ["src/file-9.py"],
                    )
                    self.assertEqual(warm.cache_status, "warm")
                    self.assertEqual(warm.records, rebuilt.records)
                    summary = json.loads(
                        path.with_name("startup-summary.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    self.assertEqual(summary["source"]["inode"], path.stat().st_ino)

    def test_replacement_after_summary_validation_rebuilds_current_source(
        self,
    ) -> None:
        for replacement_indexes in ((9,), (9, 10)):
            with self.subTest(replacement_indexes=replacement_indexes):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory) / "project"
                    root.mkdir()
                    path = Path(directory) / "events.jsonl"
                    self.write_events(path, [self.event(root, 1)])
                    load_startup_history(root, path)
                    replacement = path.with_name("replacement.jsonl")
                    self.write_events(
                        replacement,
                        [self.event(root, index) for index in replacement_indexes],
                    )

                    def replace_after_validation(*args: Any, **kwargs: Any) -> Any:
                        validated = _read_startup_summary(*args, **kwargs)
                        os.replace(replacement, path)
                        return validated

                    with patch(
                        "side_dog.cli._read_startup_summary",
                        side_effect=replace_after_validation,
                    ):
                        rebuilt = load_startup_history(root, path)

                    self.assertEqual(rebuilt.cache_status, "invalidated")
                    self.assertEqual(
                        [record["detail"] for record in rebuilt.records],
                        [f"src/file-{index}.py" for index in replacement_indexes],
                    )
                    self.assertEqual(rebuilt.position, path.stat().st_size)

    def test_malformed_suffix_falls_back_and_self_repairs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            path = Path(directory) / "events.jsonl"
            self.write_events(path, [self.event(root, 1)])
            load_startup_history(root, path)
            with path.open("ab") as handle:
                handle.write(b"{not-json}\n")
            self.append_event(path, self.event(root, 2))

            rebuilt = load_startup_history(root, path)
            warm = load_startup_history(root, path)

            self.assertEqual(rebuilt.cache_status, "invalidated")
            self.assertEqual(
                [item["detail"] for item in rebuilt.records],
                ["src/file-1.py", "src/file-2.py"],
            )
            self.assertEqual(warm.cache_status, "warm")
            self.assertEqual(warm.records, rebuilt.records)

    def test_legacy_v1_history_is_normalized_then_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            path = Path(directory) / "events.jsonl"
            legacy = {
                "schema": SCHEMA,
                "epoch_ms": 123,
                "kind": "file",
                "status": "success",
                "title": "Wrote file",
                "detail": "legacy.py",
                "future": {"ignored": True},
            }
            self.write_events(path, [legacy], newline=False)

            cold = load_startup_history(root, path)
            warm = load_startup_history(root, path)

            self.assertEqual(cold.records[0]["agent"], "unknown")
            self.assertEqual(cold.records[0]["project"], os.fspath(root.resolve()))
            self.assertNotIn("future", cold.records[0])
            self.assertEqual(warm.cache_status, "warm")
            self.assertEqual(warm.records, cold.records)

    def test_summary_never_copies_rejected_or_extra_private_input(self) -> None:
        canaries = {
            "prompt": "PROMPT-CANARY-133",
            "response": "RESPONSE-CANARY-133",
            "output": "OUTPUT-CANARY-133",
            "diff": "DIFF-CANARY-133",
            "file_contents": "CONTENTS-CANARY-133",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            path = Path(directory) / "events.jsonl"
            scrubbed = self.event(root, 1, **canaries)
            outside = self.event(root, 2, detail="/private/secret.txt")
            home_relative = self.event(root, 3, detail="~/.ssh/id_rsa")
            raw_command = self.event(
                root,
                4,
                kind="command",
                status="failed",
                title="Command failed",
                detail="cat private-secret.txt",
            )
            self.write_events(path, [scrubbed, outside, home_relative, raw_command])

            startup = load_startup_history(root, path)
            persisted = path.with_name("startup-summary.json").read_text()

            self.assertEqual(len(startup.records), 1)
            self.assertEqual(startup.records[0]["detail"], "src/file-1.py")
            for key, canary in canaries.items():
                self.assertNotIn(key, startup.records[0])
                self.assertNotIn(canary, persisted)
            self.assertNotIn("/private/secret.txt", persisted)
            self.assertNotIn("~/.ssh/id_rsa", persisted)
            self.assertNotIn("cat private-secret.txt", persisted)

            summary_path = path.with_name("startup-summary.json")
            value = json.loads(summary_path.read_text())
            value["tail"][0].update(
                kind="command",
                status="failed",
                title="Command failed",
                detail="cat private-secret.txt",
            )
            self.resign_summary(value)
            summary_path.write_text(json.dumps(value), encoding="utf-8")

            repaired = load_startup_history(root, path)

            self.assertEqual(repaired.cache_status, "invalidated")
            self.assertNotIn("cat private-secret.txt", summary_path.read_text())

    def test_summary_rejects_persisted_paths_that_escape_through_symlinks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            outside = Path(directory) / "outside"
            outside.mkdir()
            (root / "link").symlink_to(outside, target_is_directory=True)
            path = Path(directory) / "events.jsonl"
            self.write_events(
                path,
                [
                    self.event(root, 1, detail="link/id_rsa"),
                    self.event(root, 2, detail="src/safe.py"),
                ],
            )

            startup = load_startup_history(root, path)
            persisted = path.with_name("startup-summary.json").read_text()

            self.assertEqual(
                [record["detail"] for record in startup.records],
                ["src/safe.py"],
            )
            self.assertNotIn("link/id_rsa", persisted)

    def test_github_delivery_usage_grouping_and_cursor_match_a_full_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            state = Path(directory) / "state"
            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                path = events_path(root)
                github = {
                    "number": 133,
                    "title": "Startup summary",
                    "state": "OPEN",
                    "branch": "feature/startup-summary",
                    "merge_state": "CLEAN",
                    "checks_total": 1,
                    "checks_passed": 1,
                    "checks_pending": 0,
                    "checks_failed": 0,
                }
                old_context = [
                    self.event(
                        root,
                        1,
                        agent="git",
                        session_id="branch-session",
                        kind="branch",
                        title="Branch switched",
                        detail="feature/startup-summary",
                        turn_id="delivery-turn",
                    ),
                    self.event(
                        root,
                        2,
                        agent="github",
                        session_id="github-session",
                        kind="github",
                        title="PR #133 checks passed",
                        detail="",
                        turn_id="delivery-turn",
                        github=github,
                        github_fingerprint=github_fingerprint(github),
                    ),
                    self.event(
                        root,
                        3,
                        agent="codex",
                        session_id="delivery-session",
                        kind="push",
                        title="Branch pushed",
                        detail="feature/startup-summary",
                        turn_id="delivery-turn",
                        group_id="delivery-group",
                    ),
                ]
                passive = [
                    self.event(root, 100 + index) for index in range(600)
                ]
                self.write_events(path, [*old_context, *passive])
                full, full_position = read_new_events(path, 0, root)

                startup = load_startup_history(root)

                self.assertEqual(startup.records, tuple(full[-500:]))
                self.assertEqual(startup.position, full_position)
                self.assertEqual(startup.latest_github, full[1])
                self.assertEqual(
                    latest_delivery_context([startup.latest_delivery]),
                    latest_delivery_context(full),
                )
                self.assertEqual(
                    set(startup.usage_sessions),
                    {
                        ("git", "branch-session"),
                        ("github", "github-session"),
                        ("codex", "delivery-session"),
                    },
                )

                with (
                    patch("side_dog.cli.snapshot", return_value={}),
                    patch("side_dog.cli.root_is_missing", return_value=False),
                    patch(
                        "side_dog.cli.load_git_state",
                        return_value={
                            "branch": "feature/startup-summary",
                            "oid": "abcdef1234567890",
                            "short_oid": "abcdef1",
                            "repository": "side-dog",
                        },
                    ),
                ):
                    watched = initialize_watch_root(root, 60.0)

                self.assertEqual(len(watched.records), 200)
                self.assertEqual(watched.position, full_position)
                self.assertEqual(watched.github_status, github)
                self.assertEqual(
                    watched.last_github_fingerprint, github_fingerprint(github)
                )
                self.assertEqual(watched.last_github_delivery_id, "delivery-turn")
                self.assertFalse(watched.delivery_context_reset)
                self.assertEqual(set(watched.usage_sessions), set(startup.usage_sessions))
                self.assertTrue(startup_summary_path(root).exists())


if __name__ == "__main__":
    unittest.main()
