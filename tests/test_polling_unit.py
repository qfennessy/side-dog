from __future__ import annotations

import json
import sqlite3
import threading
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from side_dog.integrations import (
    ACTIVITY_SCHEMA,
    SafeEvent,
    SessionKey,
    StreamCheckpoint,
)
from side_dog.polling import (
    CheckpointStore,
    ExpiringLRU,
    PollBatch,
    PollCoordinator,
    PollErrorCode,
    PollStats,
    PollTarget,
)


def safe_event(root: Path, provider: str = "codex") -> SafeEvent:
    return SafeEvent(
        schema=ACTIVITY_SCHEMA,
        timestamp="2026-09-02T16:00:00.000+00:00",
        epoch_ms=1_788_364_800_000,
        agent=provider,
        project=str(root),
        session_id="session-1",
        kind="test",
        status="success",
        title="Tests passed",
        detail="unittest",
    )


class PollValueTests(unittest.TestCase):
    def test_targets_and_batches_freeze_typed_boundary_values(self) -> None:
        root = Path("/tmp/polling-root")
        target = PollTarget.from_wire(
            root,
            {
                "codex:session-1": {
                    "agent": "codex",
                    "session_id": "session-1",
                    "root": str(root),
                }
            },
        )
        event = safe_event(target.root)
        checkpoint = StreamCheckpoint(
            SessionKey("codex", "session-1"), "/tmp/transcript", 7
        )
        batch = PollBatch(
            PollStats("codex", duration_ms=4, parse_errors=1),
            events=((target.root, event),),
            checkpoints=((target.root, checkpoint),),
        )

        self.assertEqual(target.for_provider("codex")[0].key, checkpoint.session)
        self.assertEqual(batch.events_for(target.root), (event,))
        with self.assertRaises(FrozenInstanceError):
            batch.stats = PollStats("pi")  # type: ignore[misc]
        with self.assertRaisesRegex(TypeError, "SafeEvent"):
            PollBatch(PollStats("codex"), events=((root, {}),))  # type: ignore[arg-type]

    def test_stats_accept_only_fixed_error_codes_and_nonnegative_counts(self) -> None:
        stats = PollStats("opencode", last_error=PollErrorCode.SQLITE)

        self.assertEqual(stats.last_error, PollErrorCode.SQLITE)
        self.assertNotIn("private database path", repr(stats))
        with self.assertRaises(ValueError):
            PollStats("opencode", duration_ms=-1)
        with self.assertRaises(ValueError):
            PollStats("opencode", last_error="private database path")  # type: ignore[arg-type]


class ExpiringLRUTests(unittest.TestCase):
    def test_capacity_and_expiry_are_deterministic(self) -> None:
        now = [10.0]
        values: ExpiringLRU[str, int] = ExpiringLRU(2, 5.0, clock=lambda: now[0])
        values["one"] = 1
        values["two"] = 2
        values["three"] = 3

        self.assertEqual(tuple(values), ("two", "three"))
        now[0] = 15.0
        self.assertEqual(values.expire(), ("two", "three"))
        self.assertEqual(len(values), 0)

    def test_reinserting_a_key_refreshes_its_lru_and_ttl(self) -> None:
        now = [0.0]
        values: ExpiringLRU[str, int] = ExpiringLRU(2, 3.0, clock=lambda: now[0])
        values["one"] = 1
        now[0] = 1.0
        values["two"] = 2
        values["one"] = 11
        now[0] = 2.0
        values["three"] = 3

        self.assertEqual(tuple(values), ("one", "three"))
        now[0] = 4.5
        self.assertEqual(values.peek("one"), None)
        self.assertEqual(values.peek("three"), 3)


class CheckpointStoreTests(unittest.TestCase):
    def test_sqlite_lock_wait_is_small_and_bounded(self) -> None:
        real_connect = sqlite3.connect
        with TemporaryDirectory() as directory:
            database = Path(directory) / "native-events.sqlite3"
            with patch(
                "side_dog.polling.sqlite3.connect", wraps=real_connect
            ) as connect:
                CheckpointStore(database).load(
                    Path(directory), SessionKey("codex", "one"), "/tmp/source"
                )

        self.assertLessEqual(connect.call_args.kwargs["timeout"], 0.1)

    def test_reads_legacy_rows_and_writes_provider_qualified_rows(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "native-events.sqlite3"
            connection = sqlite3.connect(database)
            with connection:
                connection.execute(
                    "CREATE TABLE native_streams ("
                    "session_id TEXT NOT NULL, transcript_path TEXT NOT NULL, "
                    "position INTEGER NOT NULL, "
                    "PRIMARY KEY(session_id, transcript_path)) WITHOUT ROWID"
                )
                connection.execute(
                    "INSERT INTO native_streams VALUES (?, ?, ?)",
                    ("shared", "/tmp/session.jsonl", 4),
                )
            connection.close()
            store = CheckpointStore(database)
            root = Path(directory) / "repo"
            codex = SessionKey("codex", "shared")
            pi = SessionKey("pi", "shared")

            self.assertEqual(
                store.load(root, codex, "/tmp/session.jsonl"),
                StreamCheckpoint(codex, "/tmp/session.jsonl", 4),
            )
            store.save_many(
                (
                    (root, StreamCheckpoint(codex, "/tmp/session.jsonl", 11)),
                    (root, StreamCheckpoint(pi, "/tmp/session.jsonl", 22)),
                )
            )

            self.assertEqual(store.load(root, codex, "/tmp/session.jsonl").position, 11)  # type: ignore[union-attr]
            self.assertEqual(store.load(root, pi, "/tmp/session.jsonl").position, 22)  # type: ignore[union-attr]
            self.assertTrue(store.delete(root, codex, "/tmp/session.jsonl"))
            # Deleting the qualified row also removes its legacy fallback.
            self.assertIsNone(store.load(root, codex, "/tmp/session.jsonl"))

    def test_batches_each_root_database_independently(self) -> None:
        with TemporaryDirectory() as directory:
            state = Path(directory)
            store = CheckpointStore(lambda root: state / root.name / "index.sqlite3")
            first = Path(directory) / "first"
            second = Path(directory) / "second"
            checkpoint = StreamCheckpoint(
                SessionKey("cline", "one"), "/tmp/messages.json", 3
            )

            store.save_many(((first, checkpoint), (second, checkpoint)))

            self.assertEqual(
                store.load(first, checkpoint.session, checkpoint.source), checkpoint
            )
            self.assertEqual(
                store.load(second, checkpoint.session, checkpoint.source), checkpoint
            )


class BlockingAdapter:
    provider = "codex"

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.closed = False

    def poll(self, _targets: tuple[PollTarget, ...]) -> PollBatch:
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=2.0)
        checkpoint = StreamCheckpoint(
            SessionKey(self.provider, "session-1"), "/tmp/transcript", 9
        )
        return PollBatch(
            PollStats(self.provider, duration_ms=2),
            events=((self.root, safe_event(self.root, self.provider)),),
            checkpoints=((self.root, checkpoint),),
        )

    def close(self) -> None:
        self.closed = True


class FailingAdapter:
    provider = "pi"

    def poll(self, _targets: tuple[PollTarget, ...]) -> PollBatch:
        raise RuntimeError("PRIVATE_TOKEN=do-not-retain")

    def close(self) -> None:
        return


class RecordingCheckpointStore:
    def __init__(self, failures: int = 0) -> None:
        self.calls = 0
        self.failures = failures
        self.saved: list[tuple[Path, StreamCheckpoint]] = []

    def save_many(self, checkpoints: tuple[tuple[Path, StreamCheckpoint], ...]) -> None:
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise sqlite3.OperationalError("PRIVATE checkpoint path")
        self.saved.extend(checkpoints)


class RetryingEventSink:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0
        self.accepted: list[SafeEvent] = []

    def __call__(self, _root: Path, event: SafeEvent) -> None:
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise OSError("PRIVATE sink path")
        self.accepted.append(event)


class PollCoordinatorTests(unittest.TestCase):
    def test_worker_start_failure_rolls_back_and_close_stays_clean(self) -> None:
        adapter = BlockingAdapter(Path("/tmp/start-failure"))
        coordinator = PollCoordinator((adapter,))

        with patch(
            "side_dog.polling.threading.Thread.start",
            side_effect=RuntimeError("PRIVATE thread start failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "PRIVATE thread start failure"):
                coordinator.tick((PollTarget(adapter.root),))

        coordinator.close()
        coordinator.close()

        self.assertEqual(adapter.calls, 0)
        self.assertTrue(adapter.closed)
        self.assertEqual(coordinator.pending_providers, frozenset())

    def test_poll_timeout_must_be_finite_and_positive(self) -> None:
        for value in (0.0, -1.0, float("inf"), float("nan")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "finite and positive"):
                    PollCoordinator((), poll_timeout=value)

    def test_tick_never_submits_a_second_inflight_poll_for_a_provider(self) -> None:
        root = Path("/tmp/coordinator-root")
        adapter = BlockingAdapter(root)
        coordinator = PollCoordinator((adapter,))
        target = PollTarget(root)
        try:
            self.assertEqual(coordinator.tick((target,)), ())
            self.assertTrue(adapter.started.wait(timeout=1.0))
            self.assertEqual(coordinator.tick((target,)), ())
            self.assertEqual(adapter.calls, 1)
            self.assertEqual(coordinator.pending_providers, {"codex"})
            adapter.release.set()
            completed = coordinator.drain(timeout=1.0)
        finally:
            adapter.release.set()
            coordinator.close()

        self.assertEqual(len(completed), 1)
        self.assertEqual(adapter.calls, 1)
        self.assertTrue(adapter.closed)

    def test_completed_batches_apply_safe_events_and_checkpoints(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "repo").resolve()
            root.mkdir()
            adapter = BlockingAdapter(root)
            events: list[tuple[Path, SafeEvent]] = []
            store = CheckpointStore(Path(directory) / "index.sqlite3")
            coordinator = PollCoordinator(
                (adapter,),
                event_sink=lambda target, event: events.append((target, event)),
                checkpoint_store=store,
            )
            try:
                coordinator.tick((PollTarget(root),))
                self.assertTrue(adapter.started.wait(timeout=1.0))
                adapter.release.set()
                completed = coordinator.drain(timeout=1.0)
            finally:
                adapter.release.set()
                coordinator.close()

            self.assertEqual(len(completed), 1)
            self.assertEqual(events[0][1].title, "Tests passed")
            self.assertEqual(completed[0].checkpoints, ())
            self.assertNotIn("/tmp/transcript", repr(completed))
            checkpoint = store.load(
                root, SessionKey("codex", "session-1"), "/tmp/transcript"
            )
            self.assertIsNotNone(checkpoint)
            self.assertEqual(checkpoint.position, 9)  # type: ignore[union-attr]

    def test_adapter_exception_exposes_only_a_fixed_error_code(self) -> None:
        coordinator = PollCoordinator((FailingAdapter(),))
        try:
            coordinator.tick((PollTarget(Path("/tmp/repo")),))
            completed = coordinator.drain(timeout=1.0)
        finally:
            coordinator.close()

        self.assertEqual(completed[0].stats.last_error, PollErrorCode.UNKNOWN)
        self.assertNotIn("PRIVATE_TOKEN", repr(completed))
        self.assertNotIn("do-not-retain", json.dumps(str(completed)))

    def test_event_sink_failure_retries_before_polling_again_or_checkpointing(
        self,
    ) -> None:
        root = Path("/tmp/sink-failure").resolve()
        adapter = BlockingAdapter(root)
        sink = RetryingEventSink(failures=1)
        store = RecordingCheckpointStore()

        coordinator = PollCoordinator(
            (adapter,),
            event_sink=sink,
            checkpoint_store=store,  # type: ignore[arg-type]
        )
        try:
            coordinator.tick((PollTarget(root),))
            self.assertTrue(adapter.started.wait(timeout=1.0))
            adapter.release.set()
            completed = coordinator.drain(timeout=1.0)
            self.assertEqual(completed[0].stats.last_error, PollErrorCode.IO)
            self.assertEqual(coordinator.pending_providers, {"codex"})
            self.assertEqual(adapter.calls, 1)
            self.assertEqual(sink.accepted, [])
            self.assertEqual(store.calls, 0)

            # Drain retries application only; it must not poll the adapter again.
            self.assertEqual(coordinator.drain(timeout=0.0), ())
        finally:
            adapter.release.set()
            coordinator.close()

        self.assertEqual(adapter.calls, 1)
        self.assertEqual(len(sink.accepted), 1)
        self.assertEqual(store.calls, 1)
        self.assertEqual(len(store.saved), 1)
        self.assertEqual(store.saved[0][1].position, 9)
        self.assertIsNone(coordinator.stats["codex"].last_error)
        self.assertNotIn("PRIVATE", repr((completed, coordinator.stats)))
        self.assertEqual(completed[0].checkpoints, ())

    def test_checkpoint_failure_retries_without_reapplying_events(self) -> None:
        root = Path("/tmp/checkpoint-failure").resolve()
        adapter = BlockingAdapter(root)
        events: list[SafeEvent] = []
        store = RecordingCheckpointStore(failures=1)
        coordinator = PollCoordinator(
            (adapter,),
            event_sink=lambda _root, event: events.append(event),
            checkpoint_store=store,  # type: ignore[arg-type]
        )
        try:
            coordinator.tick((PollTarget(root),))
            self.assertTrue(adapter.started.wait(timeout=1.0))
            adapter.release.set()
            completed = coordinator.drain(timeout=1.0)
            self.assertEqual(completed[0].stats.last_error, PollErrorCode.SQLITE)
            self.assertEqual(coordinator.pending_providers, {"codex"})
            self.assertEqual(len(events), 1)

            self.assertEqual(coordinator.drain(timeout=0.0), ())
        finally:
            adapter.release.set()
            coordinator.close()

        self.assertEqual(len(events), 1)
        self.assertEqual(store.calls, 2)
        self.assertEqual(len(store.saved), 1)
        self.assertEqual(adapter.calls, 1)
        self.assertIsNone(coordinator.stats["codex"].last_error)
        self.assertNotIn("PRIVATE", repr((completed, coordinator.stats)))
        self.assertEqual(completed[0].checkpoints, ())

    def test_result_for_a_retired_root_is_not_applied(self) -> None:
        root = Path("/tmp/retired-root").resolve()
        adapter = BlockingAdapter(root)
        events: list[SafeEvent] = []
        coordinator = PollCoordinator(
            (adapter,), event_sink=lambda _root, event: events.append(event)
        )
        try:
            coordinator.tick((PollTarget(root),))
            self.assertTrue(adapter.started.wait(timeout=1.0))
            # A new target snapshot retires the root while its poll is in flight.
            coordinator.tick(())
            adapter.release.set()
            coordinator.drain(timeout=1.0)
        finally:
            adapter.release.set()
            coordinator.close()

        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
