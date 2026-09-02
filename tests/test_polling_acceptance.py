from __future__ import annotations

import subprocess
import sys
import threading
import textwrap
import time
import unittest
from pathlib import Path

from side_dog.integrations import SafeEvent, SessionKey, StreamCheckpoint
from side_dog.polling import (
    ExpiringLRU,
    PollBatch,
    PollCoordinator,
    PollErrorCode,
    PollStats,
    PollTarget,
)


def _event(root: Path, provider: str = "codex") -> SafeEvent:
    return SafeEvent(
        timestamp="2026-09-02T16:00:00+00:00",
        epoch_ms=1_788_364_800_000,
        agent=provider,
        project=str(root),
        session_id="session-1",
        kind="session",
        status="running",
        title="Session active",
    )


def _targets() -> tuple[PollTarget, PollTarget]:
    first = PollTarget.from_wire(
        Path("/tmp/side-dog-acceptance/first"),
        {
            "codex:one": {"agent": "codex", "session_id": "one"},
            "codex:two": {"agent": "codex", "session_id": "two"},
            "pi:three": {"agent": "pi", "session_id": "three"},
        },
    )
    second = PollTarget.from_wire(
        Path("/tmp/side-dog-acceptance/second"),
        {
            "codex:four": {"agent": "codex", "session_id": "four"},
            "pi:five": {"agent": "pi", "session_id": "five"},
            "pi:six": {"agent": "pi", "session_id": "six"},
        },
    )
    return first, second


class RecordingAdapter:
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.calls: list[tuple[PollTarget, ...]] = []
        self.close_calls = 0

    def poll(self, targets: tuple[PollTarget, ...]) -> PollBatch:
        self.calls.append(targets)
        return PollBatch(PollStats(self.provider))

    def close(self) -> None:
        self.close_calls += 1


class BlockingAdapter:
    provider = "codex"

    def __init__(self, root: Path, provider: str = "codex") -> None:
        self.root = root
        self.provider = provider
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.closed = threading.Event()
        self.calls = 0
        self.active_calls = 0
        self.most_active_calls = 0
        self.close_calls = 0
        self.close_while_polling = False

    def poll(self, _targets: tuple[PollTarget, ...]) -> PollBatch:
        self.calls += 1
        self.active_calls += 1
        self.most_active_calls = max(self.most_active_calls, self.active_calls)
        self.started.set()
        try:
            if not self.release.wait(timeout=2.0):
                raise TimeoutError("acceptance test did not release adapter")
            checkpoint = StreamCheckpoint(
                SessionKey(self.provider, "session-1"),
                "/private/provider/transcript.jsonl",
                17,
            )
            return PollBatch(
                PollStats(self.provider, duration_ms=1),
                events=((self.root, _event(self.root)),),
                checkpoints=(
                    () if self.provider == "opencode" else ((self.root, checkpoint),)
                ),
            )
        finally:
            self.active_calls -= 1
            self.finished.set()

    def close(self) -> None:
        self.close_while_polling = self.active_calls > 0
        self.close_calls += 1
        self.closed.set()


class PrivateFailureAdapter:
    provider = "pi"

    def poll(self, _targets: tuple[PollTarget, ...]) -> PollBatch:
        raise RuntimeError("PRIVACY_CANARY prompt and /private/customer/path")

    def close(self) -> None:
        return


class RecordingCheckpointStore:
    def __init__(self) -> None:
        self.saved: list[tuple[Path, StreamCheckpoint]] = []

    def save_many(self, checkpoints: tuple[tuple[Path, StreamCheckpoint], ...]) -> None:
        self.saved.extend(checkpoints)


class PollingAcceptanceTests(unittest.TestCase):
    def test_default_executor_does_not_join_blocked_poll_at_process_exit(self) -> None:
        source = textwrap.dedent(
            """
            import threading
            from pathlib import Path

            from side_dog.polling import PollCoordinator, PollTarget

            started = threading.Event()

            class BlockedAdapter:
                provider = "codex"

                def poll(self, _targets):
                    started.set()
                    threading.Event().wait()

                def close(self):
                    return

            coordinator = PollCoordinator((BlockedAdapter(),))
            coordinator.tick((PollTarget(Path("/tmp/blocked-exit")),))
            assert started.wait(timeout=1.0)
            coordinator.close(wait=False)
            """
        )

        completed = subprocess.run(
            [sys.executable, "-c", source],
            cwd=Path(__file__).resolve().parents[1],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_waiting_close_still_joins_poll_before_adapter_cleanup(self) -> None:
        root = Path("/tmp/side-dog-acceptance/waiting-close").resolve()
        adapter = BlockingAdapter(root)
        coordinator = PollCoordinator((adapter,))
        close_returned = threading.Event()

        def close_coordinator() -> None:
            coordinator.close(wait=True)
            close_returned.set()

        coordinator.tick((PollTarget(root),))
        self.assertTrue(adapter.started.wait(timeout=1.0))
        closer = threading.Thread(target=close_coordinator)
        closer.start()
        try:
            self.assertFalse(close_returned.wait(timeout=0.05))
            self.assertFalse(adapter.closed.is_set())
            adapter.release.set()
            self.assertTrue(close_returned.wait(timeout=1.0))
        finally:
            adapter.release.set()
            closer.join(timeout=1.0)
            coordinator.close(wait=False)

        self.assertFalse(closer.is_alive())
        self.assertTrue(adapter.finished.is_set())
        self.assertEqual(adapter.close_calls, 1)
        self.assertFalse(adapter.close_while_polling)

    def test_two_roots_and_many_sessions_poll_each_provider_once_per_tick(self) -> None:
        targets = _targets()
        codex = RecordingAdapter("codex")
        pi = RecordingAdapter("pi")
        coordinator = PollCoordinator((codex, pi))
        try:
            coordinator.tick(targets)
            completed = coordinator.drain(timeout=1.0)
        finally:
            coordinator.close()

        self.assertCountEqual((batch.provider for batch in completed), ("codex", "pi"))
        self.assertEqual(len(codex.calls), 1)
        self.assertEqual(len(pi.calls), 1)
        self.assertEqual(codex.calls[0], targets)
        self.assertEqual(pi.calls[0], targets)
        self.assertEqual(
            sum(len(target.for_provider("codex")) for target in codex.calls[0]), 3
        )
        self.assertEqual(
            sum(len(target.for_provider("pi")) for target in pi.calls[0]), 3
        )

    def test_slow_provider_never_has_more_than_one_poll_in_flight(self) -> None:
        root = Path("/tmp/side-dog-acceptance/slow").resolve()
        adapter = BlockingAdapter(root)
        coordinator = PollCoordinator((adapter,))
        try:
            coordinator.tick((PollTarget.from_wire(root, {}),))
            self.assertTrue(adapter.started.wait(timeout=1.0))

            for _ in range(5):
                self.assertEqual(
                    coordinator.tick((PollTarget.from_wire(root, {}),)), ()
                )

            self.assertEqual(adapter.calls, 1)
            self.assertEqual(adapter.most_active_calls, 1)
            self.assertEqual(coordinator.pending_providers, {"codex"})
            adapter.release.set()
            self.assertEqual(len(coordinator.drain(timeout=1.0)), 1)
        finally:
            adapter.release.set()
            coordinator.close()

    def test_removed_root_rejects_late_events_and_checkpoints(self) -> None:
        root = Path("/tmp/side-dog-acceptance/removed").resolve()
        adapter = BlockingAdapter(root)
        events: list[tuple[Path, SafeEvent]] = []
        checkpoints = RecordingCheckpointStore()
        coordinator = PollCoordinator(
            (adapter,),
            event_sink=lambda event_root, event: events.append((event_root, event)),
            checkpoint_store=checkpoints,  # type: ignore[arg-type]
        )
        try:
            coordinator.tick((PollTarget.from_wire(root, {}),))
            self.assertTrue(adapter.started.wait(timeout=1.0))

            coordinator.tick(())
            adapter.release.set()
            completed = coordinator.drain(timeout=1.0)
        finally:
            adapter.release.set()
            coordinator.close()

        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].events, ())
        self.assertEqual(completed[0].checkpoints, ())
        self.assertNotIn("/private/provider/transcript.jsonl", repr(completed))
        self.assertEqual(events, [])
        self.assertEqual(checkpoints.saved, [])

    def test_hung_poll_times_out_once_then_applies_late_result_internally(
        self,
    ) -> None:
        root = Path("/tmp/side-dog-acceptance/hung").resolve()
        adapter = BlockingAdapter(root)
        now = [0.0]
        events: list[SafeEvent] = []
        checkpoints = RecordingCheckpointStore()
        coordinator = PollCoordinator(
            (adapter,),
            event_sink=lambda _root, event: events.append(event),
            checkpoint_store=checkpoints,  # type: ignore[arg-type]
            poll_timeout=5.0,
            clock=lambda: now[0],
        )
        try:
            self.assertEqual(coordinator.tick((PollTarget(root),)), ())
            self.assertTrue(adapter.started.wait(timeout=1.0))

            now[0] = 5.0
            timed_out = coordinator.tick((PollTarget(root),))
            self.assertEqual(len(timed_out), 1)
            self.assertEqual(timed_out[0].stats.last_error, PollErrorCode.TIMEOUT)
            self.assertEqual(timed_out[0].stats.duration_ms, 5000)
            self.assertEqual(timed_out[0].checkpoints, ())
            self.assertEqual(coordinator.tick((PollTarget(root),)), ())
            self.assertEqual(adapter.calls, 1)
            self.assertEqual(coordinator.pending_providers, {"codex"})

            adapter.release.set()
            self.assertTrue(adapter.finished.wait(timeout=1.0))
            self.assertEqual(coordinator.drain(timeout=1.0), ())
            self.assertEqual(coordinator.pending_providers, frozenset())
            self.assertEqual(len(events), 1)
            self.assertEqual(len(checkpoints.saved), 1)
            self.assertEqual(
                checkpoints.saved[0][1].source,
                "/private/provider/transcript.jsonl",
            )
            self.assertEqual(
                coordinator.stats["codex"].last_error, PollErrorCode.TIMEOUT
            )

            # Only a later normally completed generation clears timeout health.
            self.assertEqual(coordinator.tick((PollTarget(root),)), ())
            completed = coordinator.drain(timeout=1.0)
            self.assertEqual(len(completed), 1)
            self.assertIsNone(coordinator.stats["codex"].last_error)
        finally:
            adapter.release.set()
            coordinator.close()

        self.assertEqual(adapter.calls, 2)

    def test_timed_out_late_result_still_filters_a_removed_root(self) -> None:
        root = Path("/tmp/side-dog-acceptance/timed-out-removed").resolve()
        adapter = BlockingAdapter(root)
        now = [0.0]
        events: list[SafeEvent] = []
        checkpoints = RecordingCheckpointStore()
        coordinator = PollCoordinator(
            (adapter,),
            event_sink=lambda _root, event: events.append(event),
            checkpoint_store=checkpoints,  # type: ignore[arg-type]
            poll_timeout=5.0,
            clock=lambda: now[0],
        )
        try:
            coordinator.tick((PollTarget(root),))
            self.assertTrue(adapter.started.wait(timeout=1.0))
            now[0] = 5.0
            timed_out = coordinator.tick((PollTarget(root),))
            self.assertEqual(timed_out[0].stats.last_error, PollErrorCode.TIMEOUT)

            # Retire the root before the timed-out worker returns.
            self.assertEqual(coordinator.tick(()), ())
            adapter.release.set()
            self.assertTrue(adapter.finished.wait(timeout=1.0))
            self.assertEqual(coordinator.drain(timeout=1.0), ())
        finally:
            adapter.release.set()
            coordinator.close()

        self.assertEqual(events, [])
        self.assertEqual(checkpoints.saved, [])
        self.assertEqual(coordinator.stats["codex"].last_error, PollErrorCode.TIMEOUT)

    def test_timed_out_late_apply_failure_retries_without_clearing_timeout(
        self,
    ) -> None:
        root = Path("/tmp/side-dog-acceptance/timed-out-retry").resolve()
        adapter = BlockingAdapter(root)
        now = [0.0]
        attempts = [0]
        events: list[SafeEvent] = []
        checkpoints = RecordingCheckpointStore()

        def retrying_sink(_root: Path, event: SafeEvent) -> None:
            attempts[0] += 1
            if attempts[0] == 1:
                raise OSError("PRIVATE late sink path")
            events.append(event)

        coordinator = PollCoordinator(
            (adapter,),
            event_sink=retrying_sink,
            checkpoint_store=checkpoints,  # type: ignore[arg-type]
            poll_timeout=5.0,
            clock=lambda: now[0],
        )
        try:
            coordinator.tick((PollTarget(root),))
            self.assertTrue(adapter.started.wait(timeout=1.0))
            now[0] = 5.0
            timed_out = coordinator.tick((PollTarget(root),))
            self.assertEqual(timed_out[0].stats.last_error, PollErrorCode.TIMEOUT)

            adapter.release.set()
            self.assertTrue(adapter.finished.wait(timeout=1.0))
            self.assertEqual(coordinator.drain(timeout=1.0), ())
            self.assertEqual(events, [])
            self.assertEqual(checkpoints.saved, [])
            self.assertEqual(coordinator.pending_providers, {"codex"})

            self.assertEqual(coordinator.drain(timeout=0.0), ())
        finally:
            adapter.release.set()
            coordinator.close()

        self.assertEqual(len(events), 1)
        self.assertEqual(len(checkpoints.saved), 1)
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(coordinator.pending_providers, frozenset())
        self.assertEqual(coordinator.stats["codex"].last_error, PollErrorCode.TIMEOUT)

    def test_nonblocking_close_defers_adapter_close_until_poll_finishes(self) -> None:
        root = Path("/tmp/side-dog-acceptance/deferred-close").resolve()
        adapter = BlockingAdapter(root, provider="opencode")
        events: list[SafeEvent] = []
        checkpoints = RecordingCheckpointStore()
        coordinator = PollCoordinator(
            (adapter,),
            event_sink=lambda _root, event: events.append(event),
            checkpoint_store=checkpoints,  # type: ignore[arg-type]
        )
        try:
            coordinator.tick((PollTarget(root),))
            self.assertTrue(adapter.started.wait(timeout=1.0))

            coordinator.close(wait=False)
            coordinator.close(wait=False)
            self.assertFalse(adapter.closed.wait(timeout=0.05))
            self.assertEqual(adapter.close_calls, 0)

            adapter.release.set()
            self.assertTrue(adapter.finished.wait(timeout=1.0))
            self.assertTrue(adapter.closed.wait(timeout=1.0))
        finally:
            adapter.release.set()
            coordinator.close(wait=False)

        self.assertEqual(adapter.close_calls, 1)
        self.assertFalse(adapter.close_while_polling)
        self.assertEqual(len(events), 1)
        self.assertEqual(checkpoints.saved, [])

    def test_nonblocking_close_drains_an_existing_apply_retry(self) -> None:
        root = Path("/tmp/side-dog-acceptance/close-retry").resolve()
        adapter = BlockingAdapter(root)
        attempts = [0]
        events: list[SafeEvent] = []
        checkpoints = RecordingCheckpointStore()

        def retrying_sink(_root: Path, event: SafeEvent) -> None:
            attempts[0] += 1
            if attempts[0] == 1:
                raise OSError("PRIVATE close retry path")
            events.append(event)

        coordinator = PollCoordinator(
            (adapter,),
            event_sink=retrying_sink,
            checkpoint_store=checkpoints,  # type: ignore[arg-type]
        )
        try:
            coordinator.tick((PollTarget(root),))
            self.assertTrue(adapter.started.wait(timeout=1.0))
            adapter.release.set()
            completed = coordinator.drain(timeout=1.0)
            self.assertEqual(completed[0].stats.last_error, PollErrorCode.IO)
            self.assertEqual(coordinator.pending_providers, {"codex"})

            coordinator.close(wait=False)
            self.assertTrue(adapter.closed.wait(timeout=1.0))
        finally:
            adapter.release.set()
            coordinator.close(wait=False)

        self.assertEqual(adapter.calls, 1)
        self.assertEqual(attempts[0], 2)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(checkpoints.saved), 1)
        self.assertEqual(adapter.close_calls, 1)
        self.assertFalse(adapter.close_while_polling)

    def _assert_permanent_failure_has_bounded_close(self, *, wait: bool) -> None:
        root = Path(f"/tmp/side-dog-acceptance/permanent-{wait}").resolve()
        adapter = BlockingAdapter(root)
        attempts = [0]
        checkpoints = RecordingCheckpointStore()

        def failing_sink(_root: Path, _event: SafeEvent) -> None:
            attempts[0] += 1
            raise OSError("PRIVATE permanent sink path")

        coordinator = PollCoordinator(
            (adapter,),
            event_sink=failing_sink,
            checkpoint_store=checkpoints,  # type: ignore[arg-type]
        )
        try:
            coordinator.tick((PollTarget(root),))
            self.assertTrue(adapter.started.wait(timeout=1.0))
            adapter.release.set()
            completed = coordinator.drain(timeout=1.0)
            self.assertEqual(completed[0].stats.last_error, PollErrorCode.IO)

            started = time.monotonic()
            coordinator.close(wait=wait)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.5)
            self.assertTrue(adapter.closed.wait(timeout=1.0))
        finally:
            adapter.release.set()
            coordinator.close(wait=False)

        self.assertGreaterEqual(attempts[0], 2)
        self.assertLessEqual(attempts[0], 4)
        self.assertEqual(checkpoints.saved, [])
        self.assertEqual(adapter.close_calls, 1)
        self.assertEqual(coordinator.pending_providers, frozenset())

    def test_waiting_close_bounds_permanent_apply_failure(self) -> None:
        self._assert_permanent_failure_has_bounded_close(wait=True)

    def test_nonblocking_close_eventually_closes_after_permanent_failure(self) -> None:
        self._assert_permanent_failure_has_bounded_close(wait=False)

    def test_health_reports_fixed_error_code_without_private_failure_text(self) -> None:
        coordinator = PollCoordinator((PrivateFailureAdapter(),))
        try:
            coordinator.tick(_targets())
            completed = coordinator.drain(timeout=1.0)
            health = coordinator.stats
        finally:
            coordinator.close()

        self.assertEqual(completed[0].stats.last_error, PollErrorCode.UNKNOWN)
        self.assertEqual(health["pi"].last_error, PollErrorCode.UNKNOWN)
        health_text = repr((completed, health))
        self.assertNotIn("PRIVACY_CANARY", health_text)
        self.assertNotIn("customer", health_text)
        self.assertNotIn("/private", health_text)

    def test_drain_settles_only_current_poll_and_close_is_idempotent(self) -> None:
        root = Path("/tmp/side-dog-acceptance/drain").resolve()
        adapter = BlockingAdapter(root)
        coordinator = PollCoordinator((adapter,))
        try:
            coordinator.tick((PollTarget.from_wire(root, {}),))
            self.assertTrue(adapter.started.wait(timeout=1.0))
            adapter.release.set()

            self.assertEqual(len(coordinator.drain(timeout=1.0)), 1)
            self.assertEqual(coordinator.drain(timeout=1.0), ())
            self.assertEqual(adapter.calls, 1)

            coordinator.close()
            coordinator.close()
            self.assertEqual(adapter.close_calls, 1)
            self.assertEqual(coordinator.pending_providers, frozenset())
            self.assertEqual(coordinator.tick((PollTarget.from_wire(root, {}),)), ())
            self.assertEqual(adapter.calls, 1)
        finally:
            adapter.release.set()
            coordinator.close()

    def test_public_state_cache_is_capacity_bounded_and_expires_at_ttl(self) -> None:
        now = [100.0]
        state: ExpiringLRU[str, str] = ExpiringLRU(
            max_items=2, ttl_seconds=5.0, clock=lambda: now[0]
        )
        state["first"] = "one"
        now[0] = 101.0
        state["second"] = "two"
        now[0] = 102.0
        state["third"] = "three"

        self.assertEqual(tuple(state), ("second", "third"))
        self.assertLessEqual(len(state), state.max_items)

        now[0] = 106.0
        self.assertEqual(state.expire(), ("second",))
        self.assertEqual(tuple(state), ("third",))
        now[0] = 107.0
        self.assertEqual(state.expire(), ("third",))
        self.assertEqual(len(state), 0)


if __name__ == "__main__":
    unittest.main()
