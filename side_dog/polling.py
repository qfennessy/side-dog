"""Shared polling lifecycle for coding-agent integrations.

Collectors may inspect private local sources in worker threads and return
checkpoints for internal persistence. Values returned by the coordinator expose
only validated :class:`SafeEvent` instances; checkpoint sources are stripped.
The coordinator deliberately knows nothing about collector wire formats or
``side_dog.cli`` so integrations can use it without an import cycle.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, MutableMapping
from concurrent.futures import (
    CancelledError,
    Executor,
    Future,
    ThreadPoolExecutor,
    wait as wait_futures,
)
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

from .integrations import AgentIdentity, SafeEvent, SessionKey, StreamCheckpoint


class PollErrorCode(StrEnum):
    """Fixed diagnostics that cannot copy private source or exception text."""

    CANCELLED = "cancelled"
    INVALID_RESULT = "invalid_result"
    IO = "io"
    PARSE = "parse"
    SQLITE = "sqlite"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PollTarget:
    """One authoritative watched root and its immutable agent identities."""

    root: Path
    identities: tuple[AgentIdentity, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "root", Path(self.root).expanduser().resolve(strict=False)
        )
        source: Iterable[Any]
        if isinstance(self.identities, Mapping):
            source = self.identities.values()
        else:
            source = self.identities
        normalized = tuple(
            value
            if isinstance(value, AgentIdentity)
            else AgentIdentity.from_wire(value)
            for value in source
        )
        object.__setattr__(self, "identities", normalized)

    @classmethod
    def from_wire(
        cls, root: Path, identities: Mapping[str, Mapping[str, Any]]
    ) -> PollTarget:
        return cls(
            root,
            tuple(
                AgentIdentity.from_wire(value, key=key)
                for key, value in identities.items()
            ),
        )

    def for_provider(self, provider: str) -> tuple[AgentIdentity, ...]:
        return tuple(
            identity for identity in self.identities if identity.agent == provider
        )


@dataclass(frozen=True, slots=True)
class PollStats:
    """Privacy-safe numeric health from one adapter poll."""

    provider: str
    duration_ms: int = 0
    parse_errors: int = 0
    last_error: PollErrorCode | None = None

    def __post_init__(self) -> None:
        provider = SessionKey(self.provider, "poll").provider
        duration_ms = _nonnegative_integer(self.duration_ms, "duration_ms")
        parse_errors = _nonnegative_integer(self.parse_errors, "parse_errors")
        last_error = (
            None if self.last_error in (None, "") else PollErrorCode(self.last_error)
        )
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "duration_ms", duration_ms)
        object.__setattr__(self, "parse_errors", parse_errors)
        object.__setattr__(self, "last_error", last_error)


def _nonnegative_integer(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    try:
        parsed = int(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError(f"{name} must be a non-negative integer") from None
    if parsed < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return parsed


@dataclass(frozen=True, slots=True)
class PollBatch:
    """Immutable internal output from one adapter invocation.

    Each event and checkpoint is paired with its authoritative project root.
    Checkpoint sources may be private and are available only for coordinator
    persistence; coordinator-returned copies always have empty checkpoints.
    Raw observations, source rows, commands, and exception messages have no
    field through which they can leave the adapter worker.
    """

    stats: PollStats
    events: tuple[tuple[Path, SafeEvent], ...] = ()
    checkpoints: tuple[tuple[Path, StreamCheckpoint], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.stats, PollStats):
            raise TypeError("poll batch stats must be PollStats")
        events: list[tuple[Path, SafeEvent]] = []
        for root, event in self.events:
            if not isinstance(event, SafeEvent):
                raise TypeError("poll batch events must be SafeEvent values")
            events.append((Path(root).expanduser().resolve(strict=False), event))
        checkpoints: list[tuple[Path, StreamCheckpoint]] = []
        for root, checkpoint in self.checkpoints:
            if not isinstance(checkpoint, StreamCheckpoint):
                raise TypeError(
                    "poll batch checkpoints must be StreamCheckpoint values"
                )
            checkpoints.append(
                (Path(root).expanduser().resolve(strict=False), checkpoint)
            )
        object.__setattr__(self, "events", tuple(events))
        object.__setattr__(self, "checkpoints", tuple(checkpoints))

    @property
    def provider(self) -> str:
        return self.stats.provider

    def events_for(self, root: Path) -> tuple[SafeEvent, ...]:
        canonical = Path(root).expanduser().resolve(strict=False)
        return tuple(
            event for event_root, event in self.events if event_root == canonical
        )


K = TypeVar("K")
V = TypeVar("V")


class ExpiringLRU(MutableMapping[K, V], Generic[K, V]):
    """A small insertion-refreshing LRU with deterministic TTL eviction."""

    def __init__(
        self,
        max_items: int,
        ttl_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(max_items, bool) or max_items < 1:
            raise ValueError("max_items must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.max_items = int(max_items)
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._items: OrderedDict[K, tuple[float, V]] = OrderedDict()

    def expire(self, now: float | None = None) -> tuple[K, ...]:
        moment = self._clock() if now is None else now
        expired = tuple(
            key for key, (deadline, _value) in self._items.items() if deadline <= moment
        )
        for key in expired:
            self._items.pop(key, None)
        return expired

    def __getitem__(self, key: K) -> V:
        self.expire()
        deadline, value = self._items[key]
        self._items.move_to_end(key)
        return value

    def __setitem__(self, key: K, value: V) -> None:
        now = self._clock()
        self.expire(now)
        self._items[key] = (now + self.ttl_seconds, value)
        self._items.move_to_end(key)
        while len(self._items) > self.max_items:
            self._items.popitem(last=False)

    def __delitem__(self, key: K) -> None:
        del self._items[key]

    def __iter__(self) -> Iterator[K]:
        self.expire()
        return iter(tuple(self._items))

    def __len__(self) -> int:
        self.expire()
        return len(self._items)

    def peek(self, key: K, default: V | None = None) -> V | None:
        """Read without refreshing LRU order or expiry."""
        self.expire()
        found = self._items.get(key)
        return default if found is None else found[1]


DatabasePath = Path | Callable[[Path], Path]


class CheckpointStore:
    """Adapter-neutral access to the existing ``native_streams`` table.

    The historical table and column names remain unchanged so existing cursor
    databases continue to work.  Provider-qualified ``SessionKey`` strings are
    used for all new writes, with a read-only fallback for legacy unqualified
    session rows.
    """

    def __init__(self, database_path: DatabasePath) -> None:
        self._database_path = database_path

    def path_for(self, root: Path) -> Path:
        selected = (
            self._database_path(root)
            if callable(self._database_path)
            else self._database_path
        )
        return Path(selected).expanduser()

    def _connection(self, root: Path) -> sqlite3.Connection:
        path = self.path_for(root)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=0.1)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        with connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS native_streams ("
                "session_id TEXT NOT NULL, transcript_path TEXT NOT NULL, "
                "position INTEGER NOT NULL, "
                "PRIMARY KEY(session_id, transcript_path)) WITHOUT ROWID"
            )
        return connection

    def load(
        self, root: Path, session: SessionKey | str, source: str | os.PathLike[str]
    ) -> StreamCheckpoint | None:
        key = SessionKey.from_wire(session)
        source_text = os.fspath(source)
        connection = self._connection(root)
        try:
            row = connection.execute(
                "SELECT position FROM native_streams "
                "WHERE session_id = ? AND transcript_path = ?",
                (key.to_wire(), source_text),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT position FROM native_streams "
                    "WHERE session_id = ? AND transcript_path = ?",
                    (key.session_id, source_text),
                ).fetchone()
        finally:
            connection.close()
        if row is None or not isinstance(row[0], int):
            return None
        return StreamCheckpoint(key, source_text, row[0])

    def save(self, root: Path, checkpoint: StreamCheckpoint) -> None:
        self.save_many(((root, checkpoint),))

    def save_many(self, checkpoints: Iterable[tuple[Path, StreamCheckpoint]]) -> None:
        grouped: dict[Path, list[StreamCheckpoint]] = {}
        roots: dict[Path, Path] = {}
        for root, checkpoint in checkpoints:
            if not isinstance(checkpoint, StreamCheckpoint):
                raise TypeError("checkpoint must be StreamCheckpoint")
            database = self.path_for(root)
            grouped.setdefault(database, []).append(checkpoint)
            roots.setdefault(database, Path(root))
        for database, values in grouped.items():
            connection = self._connection(roots[database])
            try:
                with connection:
                    connection.executemany(
                        "INSERT INTO native_streams"
                        "(session_id, transcript_path, position) VALUES (?, ?, ?) "
                        "ON CONFLICT(session_id, transcript_path) DO UPDATE SET "
                        "position = excluded.position",
                        (
                            (
                                checkpoint.session.to_wire(),
                                checkpoint.source,
                                checkpoint.position,
                            )
                            for checkpoint in values
                        ),
                    )
            finally:
                connection.close()

    def delete(
        self, root: Path, session: SessionKey | str, source: str | os.PathLike[str]
    ) -> bool:
        key = SessionKey.from_wire(session)
        connection = self._connection(root)
        try:
            with connection:
                cursor = connection.execute(
                    "DELETE FROM native_streams "
                    "WHERE session_id IN (?, ?) AND transcript_path = ?",
                    (key.to_wire(), key.session_id, os.fspath(source)),
                )
                return cursor.rowcount > 0
        finally:
            connection.close()


@runtime_checkable
class PollAdapter(Protocol):
    """Provider collector owned by one :class:`PollCoordinator`."""

    provider: str

    def poll(self, targets: tuple[PollTarget, ...]) -> PollBatch: ...

    def close(self) -> None: ...


EventSink = Callable[[Path, SafeEvent], None]
_SHUTDOWN_APPLY_ATTEMPTS = 3
_SHUTDOWN_RETRY_DELAY_SECONDS = 0.05


@dataclass(slots=True)
class _PendingPoll:
    future: Future[PollBatch]
    submitted_at: float
    timed_out: bool = False


@dataclass(slots=True)
class _RetryApply:
    batch: PollBatch
    update_stats: bool


@dataclass(frozen=True, slots=True)
class _ApplyOutcome:
    public: PollBatch
    retry: PollBatch | None


class PollCoordinator:
    """Schedule each provider at most once without blocking a display tick."""

    def __init__(
        self,
        adapters: Iterable[PollAdapter],
        *,
        event_sink: EventSink | None = None,
        checkpoint_store: CheckpointStore | None = None,
        executor: Executor | None = None,
        poll_timeout: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        try:
            normalized_timeout = float(poll_timeout)
        except (TypeError, ValueError):
            raise ValueError("poll_timeout must be finite and positive") from None
        if not math.isfinite(normalized_timeout) or normalized_timeout <= 0:
            raise ValueError("poll_timeout must be finite and positive")
        indexed: dict[str, PollAdapter] = {}
        for adapter in adapters:
            provider = SessionKey(adapter.provider, "poll").provider
            if provider in indexed:
                raise ValueError(f"duplicate polling adapter: {provider}")
            indexed[provider] = adapter
        self._adapters = MappingProxyType(indexed)
        self._event_sink = event_sink
        self._checkpoint_store = checkpoint_store
        self._poll_timeout = normalized_timeout
        self._clock = clock
        self._executor = executor or ThreadPoolExecutor(
            max_workers=max(1, len(indexed)), thread_name_prefix="side-dog-poll"
        )
        self._owns_executor = executor is None
        self._pending: dict[str, _PendingPoll] = {}
        self._retry_apply: dict[str, _RetryApply] = {}
        self._active_roots: frozenset[Path] = frozenset()
        self._stats: dict[str, PollStats] = {}
        self._closed_adapters: set[str] = set()
        self._shutdown_workers: set[str] = set()
        self._closed = False
        self._lock = threading.RLock()

    @property
    def pending_providers(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._pending.keys() | self._retry_apply.keys())

    @property
    def stats(self) -> Mapping[str, PollStats]:
        with self._lock:
            return MappingProxyType(dict(self._stats))

    def tick(self, targets: Iterable[PollTarget]) -> tuple[PollBatch, ...]:
        """Collect ready results and start idle providers; never wait for work."""
        snapshot = tuple(
            target if isinstance(target, PollTarget) else PollTarget(*target)
            for target in targets
        )
        with self._lock:
            if self._closed:
                return ()
            self._active_roots = frozenset(target.root for target in snapshot)
            completed = self._collect_done()
            for provider, adapter in self._adapters.items():
                if provider not in self._pending and provider not in self._retry_apply:
                    self._pending[provider] = _PendingPoll(
                        submitted_at=self._clock(),
                        future=self._executor.submit(adapter.poll, snapshot),
                    )
            return completed

    def drain(self, timeout: float | None = None) -> tuple[PollBatch, ...]:
        """Wait for the currently submitted generation, without scheduling more."""
        with self._lock:
            now = self._clock()
            records = tuple(self._pending.values())
            pending = tuple(
                record.future
                for record in records
                if timeout is not None or not record.timed_out
            )
            remaining = tuple(
                max(0.0, record.submitted_at + self._poll_timeout - now)
                for record in records
                if not record.timed_out
            )
        if pending:
            caller_wait = None if timeout is None else max(0.0, timeout)
            poll_wait = min(remaining) if remaining else None
            if caller_wait is None:
                effective_timeout = poll_wait
            elif poll_wait is None:
                effective_timeout = caller_wait
            else:
                effective_timeout = min(caller_wait, poll_wait)
            wait_futures(pending, timeout=effective_timeout)
        with self._lock:
            return self._collect_done()

    def _collect_done(self) -> tuple[PollBatch, ...]:
        completed: list[PollBatch] = []
        for provider, retry in tuple(self._retry_apply.items()):
            outcome = self._apply(retry.batch, update_stats=retry.update_stats)
            if outcome.retry is None:
                del self._retry_apply[provider]
            else:
                retry.batch = outcome.retry
        now = self._clock()
        for provider, record in tuple(self._pending.items()):
            if not record.future.done():
                if (
                    not record.timed_out
                    and now - record.submitted_at >= self._poll_timeout
                ):
                    record.timed_out = True
                    batch = PollBatch(
                        PollStats(
                            provider,
                            duration_ms=max(0, int((now - record.submitted_at) * 1000)),
                            last_error=PollErrorCode.TIMEOUT,
                        )
                    )
                    completed.append(self._apply(batch).public)
                continue
            del self._pending[provider]
            if record.timed_out:
                # The worker may have advanced retained adapter cursor state before
                # returning. Apply a valid late result exactly once so those events
                # are not lost, but do not publish a second completion or clear the
                # timeout health until a later normal poll succeeds.
                try:
                    batch = record.future.result()
                    if not isinstance(batch, PollBatch) or batch.provider != provider:
                        raise TypeError("invalid poll result")
                except Exception:
                    continue
                outcome = self._apply(batch, update_stats=False)
                if outcome.retry is not None:
                    self._retry_apply[provider] = _RetryApply(
                        outcome.retry, update_stats=False
                    )
                continue
            try:
                batch = record.future.result()
                if not isinstance(batch, PollBatch) or batch.provider != provider:
                    raise TypeError("invalid poll result")
            except Exception as error:
                batch = PollBatch(PollStats(provider, last_error=_error_code(error)))
            outcome = self._apply(batch)
            if outcome.retry is not None:
                self._retry_apply[provider] = _RetryApply(
                    outcome.retry, update_stats=True
                )
            completed.append(outcome.public)
        return tuple(completed)

    def _apply(self, batch: PollBatch, *, update_stats: bool = True) -> _ApplyOutcome:
        active_events = tuple(
            (root, event) for root, event in batch.events if root in self._active_roots
        )
        active_checkpoints = tuple(
            (root, checkpoint)
            for root, checkpoint in batch.checkpoints
            if root in self._active_roots
        )
        apply_error: PollErrorCode | None = None
        retry_events: tuple[tuple[Path, SafeEvent], ...] = ()
        retry_checkpoints: tuple[tuple[Path, StreamCheckpoint], ...] = ()
        if self._event_sink is not None:
            for index, (root, event) in enumerate(active_events):
                try:
                    self._event_sink(root, event)
                except Exception as error:
                    apply_error = _error_code(error)
                    retry_events = active_events[index:]
                    retry_checkpoints = active_checkpoints
                    break
        # Never advance a cursor past an event that its sink failed to accept.
        if (
            apply_error is None
            and self._checkpoint_store is not None
            and active_checkpoints
        ):
            try:
                self._checkpoint_store.save_many(active_checkpoints)
            except Exception as error:
                apply_error = _error_code(error)
                retry_checkpoints = active_checkpoints
        stats = (
            batch.stats
            if apply_error is None
            else PollStats(
                batch.provider,
                duration_ms=batch.stats.duration_ms,
                parse_errors=batch.stats.parse_errors,
                last_error=apply_error,
            )
        )
        if update_stats:
            self._stats[batch.provider] = stats
        # Checkpoint source strings are adapter-private and must not cross the
        # coordinator's public tick/drain boundary after internal persistence.
        public = PollBatch(stats, events=active_events)
        retry = (
            None
            if apply_error is None
            else PollBatch(
                batch.stats,
                events=retry_events,
                checkpoints=retry_checkpoints,
            )
        )
        return _ApplyOutcome(public, retry)

    def close(self, *, wait: bool = True) -> None:
        """Settle collected state before closing adapters, optionally in background."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending = dict(self._pending)
            self._pending.clear()
            for record in pending.values():
                record.future.cancel()
        if wait:
            for provider in self._adapters:
                self._finish_shutdown(provider, pending.get(provider))
        else:
            for provider in self._adapters:
                record = pending.get(provider)
                with self._lock:
                    needs_settlement = (
                        record is not None or provider in self._retry_apply
                    )
                if needs_settlement:
                    self._start_shutdown_worker(provider, record)
                else:
                    self._close_adapter(provider)
        if self._owns_executor:
            self._executor.shutdown(wait=wait, cancel_futures=True)  # type: ignore[attr-defined]

    def _start_shutdown_worker(
        self, provider: str, record: _PendingPoll | None
    ) -> None:
        with self._lock:
            if provider in self._shutdown_workers:
                return
            self._shutdown_workers.add(provider)
        threading.Thread(
            target=self._finish_shutdown,
            args=(provider, record),
            name=f"side-dog-close-{provider}",
            daemon=True,
        ).start()

    def _finish_shutdown(self, provider: str, record: _PendingPoll | None) -> None:
        apply_attempts = 0
        if record is not None:
            try:
                batch = record.future.result()
                if not isinstance(batch, PollBatch) or batch.provider != provider:
                    raise TypeError("invalid poll result")
            except Exception:
                batch = None
            if batch is not None:
                with self._lock:
                    outcome = self._apply(batch, update_stats=not record.timed_out)
                    apply_attempts += 1
                    if outcome.retry is not None:
                        self._retry_apply[provider] = _RetryApply(
                            outcome.retry, update_stats=not record.timed_out
                        )
        while apply_attempts < _SHUTDOWN_APPLY_ATTEMPTS:
            with self._lock:
                retry = self._retry_apply.get(provider)
                if retry is None:
                    break
            if apply_attempts:
                threading.Event().wait(_SHUTDOWN_RETRY_DELAY_SECONDS)
            with self._lock:
                retry = self._retry_apply.get(provider)
                if retry is None:
                    break
                outcome = self._apply(retry.batch, update_stats=retry.update_stats)
                apply_attempts += 1
                if outcome.retry is None:
                    del self._retry_apply[provider]
                    break
                retry.batch = outcome.retry
        # A permanent sink failure cannot be reconciled during shutdown. Bound the
        # best-effort attempts, release private batch state, and still clean up.
        with self._lock:
            self._retry_apply.pop(provider, None)
        self._close_adapter(provider)

    def _close_adapter(self, provider: str) -> None:
        with self._lock:
            if provider in self._closed_adapters:
                return
            self._closed_adapters.add(provider)
        try:
            self._adapters[provider].close()
        except Exception:
            # Cleanup is best-effort, and private adapter exception text must not
            # escape from a worker callback or make repeated close unsafe.
            return

    def __enter__(self) -> PollCoordinator:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _error_code(error: BaseException) -> PollErrorCode:
    if isinstance(error, CancelledError):
        return PollErrorCode.CANCELLED
    if isinstance(error, TimeoutError):
        return PollErrorCode.TIMEOUT
    if isinstance(error, json.JSONDecodeError):
        return PollErrorCode.PARSE
    if isinstance(error, sqlite3.Error):
        return PollErrorCode.SQLITE
    if isinstance(error, OSError):
        return PollErrorCode.IO
    if isinstance(error, TypeError):
        return PollErrorCode.INVALID_RESULT
    return PollErrorCode.UNKNOWN


__all__ = [
    "CheckpointStore",
    "ExpiringLRU",
    "PollAdapter",
    "PollBatch",
    "PollCoordinator",
    "PollErrorCode",
    "PollStats",
    "PollTarget",
]
