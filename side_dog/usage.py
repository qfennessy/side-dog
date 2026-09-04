"""Privacy-safe token usage reports from an optional local ccusage command.

Usage is cumulative state, not activity.  Nothing in this module writes to the
per-project event history, and raw ccusage rows never leave the parser.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from side_dog.config import config_usage, load_config
from side_dog.integrations import normalize_provider


USAGE_SCHEMA = "side-dog-usage-v2"
LIVE_USAGE_SCHEMA = "side-dog-live-usage-v1"
MAX_SAFE_INTEGER = 2**53 - 1
USAGE_VIEWS = ("daily", "monthly", "session")
PRICING_SOURCES = frozenset({"online", "cached", "omitted", "unknown"})
USAGE_SAMPLE_WIRE_FIELDS = frozenset(
    {
        "agent",
        "period",
        "session_id",
        "model",
        "input_tokens",
        "output_tokens",
        "cache_creation_tokens",
        "cache_read_tokens",
        "reasoning_tokens",
        "uncategorized_tokens",
        "total_tokens",
        "cost_usd",
        "cost_basis",
        "coverage",
        "last_activity",
        "unpriced_models",
    }
)
USAGE_REPORT_WIRE_FIELDS = frozenset(
    {
        "schema",
        "view",
        "status",
        "captured_epoch_ms",
        "pricing_source",
        "pricing_captured_epoch_ms",
        "detail",
        "rows",
        "totals",
    }
)
DEFAULT_COMMAND = ("ccusage",)
DEFAULT_AGENT = "claude-code"
DEFAULT_BLOCK_REFRESH_SECONDS = 10.0
DEFAULT_SESSION_REFRESH_SECONDS = 180.0
MIN_SESSION_SCAN_SECONDS = 60.0
DEFAULT_TIMEOUT_SECONDS = 20.0
BLOCK_TIMEOUT_SECONDS = 2.0


def _safe_text(value: Any, limit: int = 256) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(
        character
        for character in value[:limit]
        if character in "\t" or ord(character) >= 32
    ).strip()


def _agent(value: Any) -> str:
    return normalize_provider(_safe_text(value, 64))


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return min(MAX_SAFE_INTEGER, value) if value >= 0 else 0
    if not isinstance(value, float) or not math.isfinite(value) or value < 0:
        return 0
    return min(MAX_SAFE_INTEGER, int(value))


def _first_count(row: Mapping[str, Any], *names: str) -> int:
    for name in names:
        if name in row:
            value = row[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or (isinstance(value, float) and not math.isfinite(value))
                or value < 0
                or (isinstance(value, float) and not value.is_integer())
            ):
                raise ValueError(f"ccusage {name} must be a non-negative integer")
            return _count(value)
    return 0


def _cost_microusd(row: Mapping[str, Any]) -> int | None:
    value: Any = None
    for name in ("totalCost", "costUSD", "cost", "total_cost", "cost_usd"):
        if name in row:
            value = row[name]
            break
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    try:
        micros = int(
            (amount * Decimal(1_000_000)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
    except (InvalidOperation, ValueError, OverflowError):
        return None
    return min(MAX_SAFE_INTEGER, micros)


@dataclass(frozen=True, slots=True)
class UnpricedModel:
    model: str
    tokens: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "model", _safe_text(self.model, 128) or "unknown")
        if (
            isinstance(self.tokens, bool)
            or not isinstance(self.tokens, int)
            or not 0 <= self.tokens <= MAX_SAFE_INTEGER
        ):
            raise ValueError("unpriced model tokens must be a safe non-negative integer")

    def to_wire(self) -> dict[str, Any]:
        return {"model": self.model, "tokens": self.tokens}

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> UnpricedModel:
        if set(value) != {"model", "tokens"}:
            raise ValueError("unsupported unpriced model shape")
        return cls(_safe_text(value.get("model"), 128), _count(value.get("tokens")))


@dataclass(frozen=True, slots=True)
class UsageSample:
    """The only per-row usage shape allowed into Side Dog presentation."""

    agent: str
    period: str = ""
    session_id: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    reasoning_tokens: int = 0
    uncategorized_tokens: int = 0
    cost_microusd: int | None = None
    cost_basis: str = "unpriced"
    coverage: str = "complete"
    last_activity: str = ""
    unpriced_models: tuple[UnpricedModel, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent", _agent(self.agent))
        for name, limit in (
            ("period", 128),
            ("session_id", 512),
            ("model", 256),
            ("last_activity", 64),
        ):
            object.__setattr__(self, name, _safe_text(getattr(self, name), limit))
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_creation_tokens",
            "cache_read_tokens",
            "reasoning_tokens",
            "uncategorized_tokens",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= MAX_SAFE_INTEGER
            ):
                raise ValueError(f"{name} must be a safe non-negative integer")
        if self.cost_microusd is not None and (
            isinstance(self.cost_microusd, bool)
            or not isinstance(self.cost_microusd, int)
            or not 0 <= self.cost_microusd <= MAX_SAFE_INTEGER
        ):
            raise ValueError("cost_microusd must be a safe non-negative integer")
        if self.cost_basis not in {"recorded", "estimated", "unpriced", "omitted"}:
            raise ValueError("unsupported cost basis")
        if self.coverage not in {"complete", "partial", "unavailable", "stale"}:
            raise ValueError("unsupported usage coverage")
        if any(not isinstance(item, UnpricedModel) for item in self.unpriced_models):
            raise ValueError("unpriced_models must contain typed values")

    @property
    def total_tokens(self) -> int:
        # Reasoning is reported for context but is already part of output for
        # the providers ccusage normalizes.  Never add it a second time.
        return min(
            MAX_SAFE_INTEGER,
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + self.cache_read_tokens
            + self.uncategorized_tokens,
        )

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "agent": self.agent,
            "period": self.period,
            "session_id": self.session_id,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "uncategorized_tokens": self.uncategorized_tokens,
            "total_tokens": self.total_tokens,
            "cost_basis": self.cost_basis,
            "coverage": self.coverage,
            "last_activity": self.last_activity,
            "unpriced_models": [item.to_wire() for item in self.unpriced_models],
        }
        if self.cost_microusd is not None:
            wire["cost_usd"] = self.cost_microusd / 1_000_000
        return wire

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> UsageSample:
        unknown = set(value) - USAGE_SAMPLE_WIRE_FIELDS
        if unknown:
            raise ValueError("unsupported usage sample field")
        unpriced = value.get("unpriced_models", [])
        if not isinstance(unpriced, list) or any(
            not isinstance(item, Mapping) for item in unpriced
        ):
            raise ValueError("unpriced_models must contain objects")
        return cls(
            agent=_agent(value.get("agent")),
            period=_safe_text(value.get("period"), 128),
            session_id=_safe_text(value.get("session_id"), 512),
            model=_safe_text(value.get("model"), 256),
            input_tokens=_count(value.get("input_tokens")),
            output_tokens=_count(value.get("output_tokens")),
            cache_creation_tokens=_count(value.get("cache_creation_tokens")),
            cache_read_tokens=_count(value.get("cache_read_tokens")),
            reasoning_tokens=_count(value.get("reasoning_tokens")),
            uncategorized_tokens=_count(value.get("uncategorized_tokens")),
            cost_microusd=_cost_microusd(value),
            cost_basis=_safe_text(value.get("cost_basis"), 32) or "unpriced",
            coverage=_safe_text(value.get("coverage"), 32) or "complete",
            last_activity=_safe_text(value.get("last_activity"), 64),
            unpriced_models=tuple(UnpricedModel.from_wire(item) for item in unpriced),
        )


@dataclass(frozen=True, slots=True)
class UsageReport:
    view: str
    samples: tuple[UsageSample, ...] = ()
    status: str = "available"
    captured_epoch_ms: int = 0
    pricing_source: str = "unknown"
    pricing_captured_epoch_ms: int = 0
    detail: str = ""

    def __post_init__(self) -> None:
        if self.view not in USAGE_VIEWS:
            raise ValueError("unsupported usage view")
        if self.status not in {"available", "unavailable", "stale"}:
            raise ValueError("unsupported usage report status")
        if not self.captured_epoch_ms:
            object.__setattr__(self, "captured_epoch_ms", int(time.time() * 1000))
        if self.pricing_source not in PRICING_SOURCES:
            raise ValueError("unsupported pricing source")
        if not self.pricing_captured_epoch_ms:
            object.__setattr__(
                self, "pricing_captured_epoch_ms", self.captured_epoch_ms
            )
        object.__setattr__(self, "detail", _safe_text(self.detail, 256))

    def to_wire(self) -> dict[str, Any]:
        return {
            "schema": USAGE_SCHEMA,
            "view": self.view,
            "status": self.status,
            "captured_epoch_ms": self.captured_epoch_ms,
            "pricing_source": self.pricing_source,
            "pricing_captured_epoch_ms": self.pricing_captured_epoch_ms,
            "detail": self.detail,
            "rows": [sample.to_wire() for sample in self.samples],
            "totals": usage_totals(self.samples),
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> UsageReport:
        unknown = set(value) - USAGE_REPORT_WIRE_FIELDS
        if unknown or value.get("schema") != USAGE_SCHEMA:
            raise ValueError("unsupported usage report shape")
        rows = value.get("rows")
        if not isinstance(rows, list):
            raise ValueError("usage report rows must be an array")
        if any(not isinstance(row, Mapping) for row in rows):
            raise ValueError("usage report rows must contain objects")
        return cls(
            view=_safe_text(value.get("view"), 32),
            samples=tuple(UsageSample.from_wire(row) for row in rows),
            status=_safe_text(value.get("status"), 32),
            captured_epoch_ms=_count(value.get("captured_epoch_ms")),
            pricing_source=_safe_text(value.get("pricing_source"), 32),
            pricing_captured_epoch_ms=_count(
                value.get("pricing_captured_epoch_ms")
            ),
            detail=_safe_text(value.get("detail"), 256),
        )


@dataclass(frozen=True, slots=True)
class UsageBlock:
    status: str = "unavailable"
    captured_epoch_ms: int = 0
    pricing_source: str = "unknown"
    pricing_captured_epoch_ms: int = 0
    start_time: str = ""
    end_time: str = ""
    total_tokens: int = 0
    cost_microusd: int | None = None
    burn_rate_microusd_per_hour: int | None = None
    remaining_minutes: int = 0
    projection_cost_microusd: int | None = None
    projection_tokens: int = 0
    models: tuple[str, ...] = ()
    unpriced_models: tuple[UnpricedModel, ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"available", "unavailable", "stale"}:
            raise ValueError("unsupported usage block status")
        if self.pricing_source not in PRICING_SOURCES:
            raise ValueError("unsupported pricing source")
        if not self.captured_epoch_ms:
            object.__setattr__(self, "captured_epoch_ms", int(time.time() * 1000))
        if not self.pricing_captured_epoch_ms:
            object.__setattr__(
                self, "pricing_captured_epoch_ms", self.captured_epoch_ms
            )
        for name in ("total_tokens", "remaining_minutes", "projection_tokens"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= MAX_SAFE_INTEGER
            ):
                raise ValueError(f"{name} must be a safe non-negative integer")
        for name in ("cost_microusd", "burn_rate_microusd_per_hour", "projection_cost_microusd"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= MAX_SAFE_INTEGER
            ):
                raise ValueError(f"{name} must be a safe non-negative integer")
        object.__setattr__(self, "start_time", _safe_text(self.start_time, 64))
        object.__setattr__(self, "end_time", _safe_text(self.end_time, 64))
        object.__setattr__(
            self,
            "models",
            tuple(filter(None, (_safe_text(item, 128) for item in self.models))),
        )
        if any(not isinstance(item, UnpricedModel) for item in self.unpriced_models):
            raise ValueError("unpriced_models must contain typed values")
        object.__setattr__(self, "detail", _safe_text(self.detail, 256))

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "status": self.status,
            "captured_epoch_ms": self.captured_epoch_ms,
            "pricing_source": self.pricing_source,
            "pricing_captured_epoch_ms": self.pricing_captured_epoch_ms,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "total_tokens": self.total_tokens,
            "remaining_minutes": self.remaining_minutes,
            "projection_tokens": self.projection_tokens,
            "models": list(self.models),
            "unpriced_models": [item.to_wire() for item in self.unpriced_models],
            "detail": self.detail,
        }
        for wire_name, field_name in (
            ("cost_usd", "cost_microusd"),
            ("burn_rate_usd_per_hour", "burn_rate_microusd_per_hour"),
            ("projection_cost_usd", "projection_cost_microusd"),
        ):
            value = getattr(self, field_name)
            if value is not None:
                wire[wire_name] = value / 1_000_000
        return wire

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> UsageBlock:
        expected = {
            "status",
            "captured_epoch_ms",
            "pricing_source",
            "pricing_captured_epoch_ms",
            "start_time",
            "end_time",
            "total_tokens",
            "cost_usd",
            "burn_rate_usd_per_hour",
            "remaining_minutes",
            "projection_cost_usd",
            "projection_tokens",
            "models",
            "unpriced_models",
            "detail",
        }
        if set(value) - expected:
            raise ValueError("unsupported usage block shape")
        models = value.get("models", [])
        unpriced = value.get("unpriced_models", [])
        if not isinstance(models, list) or any(
            not isinstance(item, str) for item in models
        ):
            raise ValueError("usage block models must contain strings")
        if not isinstance(unpriced, list) or any(
            not isinstance(item, Mapping) for item in unpriced
        ):
            raise ValueError("usage block unpriced_models must contain objects")
        return cls(
            status=_safe_text(value.get("status"), 32),
            captured_epoch_ms=_count(value.get("captured_epoch_ms")),
            pricing_source=_safe_text(value.get("pricing_source"), 32),
            pricing_captured_epoch_ms=_count(
                value.get("pricing_captured_epoch_ms")
            ),
            start_time=_safe_text(value.get("start_time"), 64),
            end_time=_safe_text(value.get("end_time"), 64),
            total_tokens=_count(value.get("total_tokens")),
            cost_microusd=_cost_microusd({"cost_usd": value.get("cost_usd")}),
            burn_rate_microusd_per_hour=_cost_microusd(
                {"cost_usd": value.get("burn_rate_usd_per_hour")}
            ),
            remaining_minutes=_count(value.get("remaining_minutes")),
            projection_cost_microusd=_cost_microusd(
                {"cost_usd": value.get("projection_cost_usd")}
            ),
            projection_tokens=_count(value.get("projection_tokens")),
            models=tuple(models),
            unpriced_models=tuple(UnpricedModel.from_wire(item) for item in unpriced),
            detail=_safe_text(value.get("detail"), 256),
        )


@dataclass(frozen=True, slots=True)
class LiveUsageSnapshot:
    today: UsageReport
    history: UsageReport
    block: UsageBlock

    def to_wire(self) -> dict[str, Any]:
        return {
            "schema": LIVE_USAGE_SCHEMA,
            "today": self.today.to_wire(),
            "history": self.history.to_wire(),
            "block": self.block.to_wire(),
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> LiveUsageSnapshot:
        if set(value) != {"schema", "today", "history", "block"} or value.get(
            "schema"
        ) != LIVE_USAGE_SCHEMA:
            raise ValueError("unsupported live usage snapshot shape")
        today = value.get("today")
        history = value.get("history")
        block = value.get("block")
        if not all(isinstance(item, Mapping) for item in (today, history, block)):
            raise ValueError("live usage snapshot members must be objects")
        return cls(
            UsageReport.from_wire(today),  # type: ignore[arg-type]
            UsageReport.from_wire(history),  # type: ignore[arg-type]
            UsageBlock.from_wire(block),  # type: ignore[arg-type]
        )


def _rows(document: Any, view: str) -> list[Mapping[str, Any]]:
    if isinstance(document, list):
        if any(not isinstance(row, Mapping) for row in document):
            raise ValueError("ccusage rows must contain objects")
        return list(document)
    if not isinstance(document, Mapping):
        raise ValueError("ccusage JSON must be an object or array")
    projects = document.get("projects")
    if isinstance(projects, Mapping):
        project_rows: list[Mapping[str, Any]] = []
        for value in projects.values():
            if not isinstance(value, list):
                raise ValueError("ccusage project entries must be arrays")
            if any(not isinstance(row, Mapping) for row in value):
                raise ValueError("ccusage project rows must contain objects")
            project_rows.extend(value)
        return project_rows
    candidates = (view, "sessions" if view == "session" else view, "data")
    for name in candidates:
        value = document.get(name)
        if isinstance(value, list):
            if any(not isinstance(row, Mapping) for row in value):
                raise ValueError("ccusage rows must contain objects")
            return list(value)
    if any(name in document for name in ("inputTokens", "outputTokens", "totalTokens")):
        return [document]
    return []


def _period(row: Mapping[str, Any], view: str) -> str:
    names = {
        "daily": ("date", "period"),
        "monthly": ("month", "period"),
        "session": ("sessionId", "session", "period", "id"),
    }[view]
    return next(
        (
            _safe_text(row.get(name), 128)
            for name in names
            if _safe_text(row.get(name), 128)
        ),
        "",
    )


def _model(row: Mapping[str, Any]) -> str:
    direct = _safe_text(row.get("modelName") or row.get("model"), 256)
    if direct:
        return direct
    models = row.get("modelsUsed") or row.get("models")
    if isinstance(models, list):
        safe = [_safe_text(item, 128) for item in models]
        return ", ".join(item for item in safe if item)[:256]
    return ""


def _unpriced_models(row: Mapping[str, Any], no_cost: bool) -> tuple[UnpricedModel, ...]:
    if no_cost:
        return ()
    breakdowns = row.get("modelBreakdowns")
    if breakdowns is None:
        return ()
    if not isinstance(breakdowns, list) or any(
        not isinstance(item, Mapping) for item in breakdowns
    ):
        raise ValueError("ccusage model breakdowns must contain objects")
    unpriced: list[UnpricedModel] = []
    for item in breakdowns:
        input_tokens = _first_count(item, "inputTokens", "input_tokens")
        output_tokens = _first_count(item, "outputTokens", "output_tokens")
        cache_creation = _first_count(
            item,
            "cacheCreationTokens",
            "cacheCreationInputTokens",
            "cache_creation_tokens",
        )
        cache_read = _first_count(
            item,
            "cacheReadTokens",
            "cacheReadInputTokens",
            "cache_read_tokens",
        )
        tokens = min(
            MAX_SAFE_INTEGER,
            input_tokens + output_tokens + cache_creation + cache_read,
        )
        cost = _cost_microusd(item)
        if tokens and (cost is None or cost == 0):
            unpriced.append(UnpricedModel(_model(item), tokens))
    return tuple(unpriced)


def _sample(
    row: Mapping[str, Any],
    view: str,
    mode: str,
    no_cost: bool,
    default_agent: str,
) -> UsageSample:
    unpriced_models = _unpriced_models(row, no_cost)
    input_tokens = _first_count(row, "inputTokens", "input_tokens")
    output_tokens = _first_count(row, "outputTokens", "output_tokens")
    cache_creation = _first_count(
        row,
        "cacheCreationTokens",
        "cache_creation_tokens",
        "cache_creation_input_tokens",
        "cacheWriteTokens",
    )
    cache_read = _first_count(
        row, "cacheReadTokens", "cache_read_tokens", "cache_read_input_tokens"
    )
    reasoning = _first_count(
        row, "reasoningOutputTokens", "reasoningTokens", "reasoning_tokens"
    )
    total = input_tokens + output_tokens + cache_creation + cache_read
    uncategorized = (
        _first_count(row, "totalTokens", "total_tokens") if total == 0 else 0
    )
    total += uncategorized
    cost = None if no_cost else _cost_microusd(row)
    if no_cost:
        basis = "omitted"
        coverage = "complete"
    elif cost is None or (cost == 0 and total > 0):
        cost = None
        basis = "unpriced"
        coverage = "partial"
    else:
        basis = "recorded" if mode == "display" else "estimated"
        coverage = (
            "partial"
            if row.get("isFallback") is True or unpriced_models
            else "complete"
        )
    session_id = ""
    if view == "session":
        session_id = _period(row, view)
    return UsageSample(
        agent=_agent(row.get("agent") or row.get("source") or default_agent),
        period=_period(row, view),
        session_id=session_id,
        model=_model(row),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
        reasoning_tokens=reasoning,
        uncategorized_tokens=uncategorized,
        cost_microusd=cost,
        cost_basis=basis,
        coverage=coverage,
        last_activity=_safe_text(
            row.get("lastActivity")
            or (
                (row.get("metadata") or {}).get("lastActivity")
                if isinstance(row.get("metadata") or {}, Mapping)
                else ""
            ),
            64,
        ),
        unpriced_models=unpriced_models,
    )


def parse_ccusage_json(
    output: str,
    view: str,
    *,
    mode: str = "auto",
    no_cost: bool = False,
    default_agent: str = DEFAULT_AGENT,
    pricing_source: str = "unknown",
) -> UsageReport:
    if view not in USAGE_VIEWS:
        raise ValueError("unsupported usage view")
    if mode not in {"auto", "calculate", "display"}:
        raise ValueError("unsupported cost mode")
    try:
        document = json.loads(output)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError("ccusage returned malformed JSON") from error
    recognized = isinstance(document, list) or (
        isinstance(document, Mapping)
        and (
            isinstance(document.get("projects"), Mapping)
            or any(
                isinstance(document.get(name), list)
                for name in (
                    view,
                    "sessions" if view == "session" else view,
                    "data",
                )
            )
            or any(
                name in document
                for name in ("inputTokens", "outputTokens", "totalTokens")
            )
        )
    )
    if not recognized:
        raise ValueError("ccusage JSON shape is unsupported")
    flattened: list[Mapping[str, Any]] = []
    for row in _rows(document, view):
        agents = row.get("agents")
        if isinstance(agents, list):
            if any(not isinstance(nested, Mapping) for nested in agents):
                raise ValueError("ccusage agent rows must contain objects")
            for nested in agents:
                if isinstance(nested, Mapping):
                    flattened.append({"period": _period(row, view), **nested})
        else:
            flattened.append(row)
    samples = tuple(
        _sample(row, view, mode, no_cost, default_agent) for row in flattened
    )
    return UsageReport(
        view=view,
        samples=samples,
        pricing_source="omitted" if no_cost else pricing_source,
    )


def usage_settings(document: Mapping[str, Any] | None = None) -> dict[str, Any]:
    configured = config_usage(dict(document) if document is not None else load_config())
    legacy_refresh = float(
        configured.get("refresh_seconds", DEFAULT_SESSION_REFRESH_SECONDS)
    )
    return {
        "enabled": configured.get("enabled", True),
        "command": tuple(configured.get("command", DEFAULT_COMMAND)),
        "agent": configured.get("agent", DEFAULT_AGENT),
        "offline": configured.get("offline", False),
        "block_refresh_seconds": configured.get(
            "block_refresh_seconds", DEFAULT_BLOCK_REFRESH_SECONDS
        ),
        "session_refresh_seconds": max(
            MIN_SESSION_SCAN_SECONDS,
            float(
                configured.get(
                    "session_refresh_seconds",
                    legacy_refresh,
                )
            ),
        ),
    }


def ccusage_command(
    view: str,
    *,
    since: str | None = None,
    until: str | None = None,
    mode: str = "auto",
    no_cost: bool = False,
    offline: bool | None = None,
    settings: Mapping[str, Any] | None = None,
) -> list[str]:
    values = dict(settings or usage_settings())
    command = [str(part) for part in values.get("command", DEFAULT_COMMAND)]
    command.extend((view, "--json", "--mode", mode))
    use_offline = bool(values.get("offline", False)) if offline is None else offline
    if use_offline:
        command.append("--offline")
    if since:
        command.extend(("--since", since))
    if until:
        command.extend(("--until", until))
    if no_cost:
        command.append("--no-cost")
    return command


def ccusage_block_command(
    *,
    offline: bool | None = None,
    settings: Mapping[str, Any] | None = None,
) -> list[str]:
    values = dict(settings or usage_settings())
    command = [str(part) for part in values.get("command", DEFAULT_COMMAND)]
    command.extend(("blocks", "--active", "--json"))
    use_offline = bool(values.get("offline", False)) if offline is None else offline
    if use_offline:
        command.append("--offline")
    return command


Runner = Callable[..., subprocess.CompletedProcess[str]]


class _UsageCancelled(Exception):
    pass


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        return
    try:
        process.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            return
        process.communicate()


def _run_cancellable(
    command: list[str], timeout: float, cancel_event: threading.Event | None
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                _stop_process(process)
                raise _UsageCancelled
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_process(process)
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                continue
            return subprocess.CompletedProcess(
                command, process.returncode, stdout, stderr
            )
    except BaseException:
        _stop_process(process)
        raise


def load_ccusage(
    view: str = "session",
    *,
    since: str | None = None,
    until: str | None = None,
    mode: str = "auto",
    no_cost: bool = False,
    settings: Mapping[str, Any] | None = None,
    runner: Runner | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    cancel_event: threading.Event | None = None,
) -> UsageReport:
    values = dict(settings or usage_settings())
    if not values.get("enabled", True):
        return UsageReport(view, status="unavailable", detail="disabled in config")
    configured_command = [
        str(part) for part in values.get("command", DEFAULT_COMMAND)
    ]
    executable = configured_command[0] if configured_command else ""
    if not executable or (os.sep not in executable and shutil.which(executable) is None):
        return UsageReport(view, status="unavailable", detail="ccusage is not installed")
    preferred_offline = bool(values.get("offline", False))

    def attempt(use_offline: bool) -> UsageReport:
        command = ccusage_command(
            view,
            since=since,
            until=until,
            mode=mode,
            no_cost=no_cost,
            offline=use_offline,
            settings=values,
        )
        try:
            completed = (
                runner(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                if runner is not None
                else _run_cancellable(command, timeout, cancel_event)
            )
        except _UsageCancelled:
            return UsageReport(
                view, status="unavailable", detail="usage refresh cancelled"
            )
        except subprocess.TimeoutExpired:
            return UsageReport(view, status="unavailable", detail="ccusage timed out")
        except OSError:
            return UsageReport(
                view, status="unavailable", detail="ccusage could not start"
            )
        if completed.returncode != 0:
            return UsageReport(
                view, status="unavailable", detail="ccusage report failed"
            )
        try:
            return parse_ccusage_json(
                completed.stdout,
                view,
                mode=mode,
                no_cost=no_cost,
                default_agent=str(values.get("agent", DEFAULT_AGENT)),
                pricing_source=(
                    "omitted" if no_cost else "cached" if use_offline else "online"
                ),
            )
        except ValueError as error:
            return UsageReport(view, status="unavailable", detail=str(error))

    report = attempt(preferred_offline)
    if (
        report.status == "unavailable"
        and not preferred_offline
        and report.detail != "usage refresh cancelled"
    ):
        cached = attempt(True)
        if cached.status == "available":
            return UsageReport(
                cached.view,
                samples=cached.samples,
                status=cached.status,
                captured_epoch_ms=cached.captured_epoch_ms,
                pricing_source="cached",
                pricing_captured_epoch_ms=cached.pricing_captured_epoch_ms,
                detail="online pricing unavailable; using cached pricing",
            )
    return report


def parse_ccusage_block_json(
    output: str, *, pricing_source: str = "unknown"
) -> UsageBlock:
    try:
        document = json.loads(output)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError("ccusage returned malformed block JSON") from error
    if not isinstance(document, Mapping) or not isinstance(
        document.get("blocks"), list
    ):
        raise ValueError("ccusage block JSON shape is unsupported")
    blocks = document["blocks"]
    if any(not isinstance(item, Mapping) for item in blocks):
        raise ValueError("ccusage blocks must contain objects")
    row = next((item for item in blocks if item.get("isActive") is True), None)
    if row is None:
        return UsageBlock(
            status="unavailable",
            pricing_source=pricing_source,
            detail="no active usage block",
        )
    tokens = _first_count(row, "totalTokens")
    cost = _cost_microusd(row)
    burn = row.get("burnRate")
    projection = row.get("projection")
    if burn is not None and not isinstance(burn, Mapping):
        raise ValueError("ccusage block burn rate must be an object")
    if projection is not None and not isinstance(projection, Mapping):
        raise ValueError("ccusage block projection must be an object")
    models_value = row.get("models", [])
    if not isinstance(models_value, list) or any(
        not isinstance(item, str) for item in models_value
    ):
        raise ValueError("ccusage block models must contain strings")
    models = tuple(_safe_text(item, 128) for item in models_value if item)
    unpriced = ()
    if tokens and (cost is None or cost == 0):
        cost = None
        unpriced = (UnpricedModel(", ".join(models) or "unknown", tokens),)
    return UsageBlock(
        status="available",
        pricing_source=pricing_source,
        start_time=_safe_text(row.get("startTime"), 64),
        end_time=_safe_text(row.get("endTime"), 64),
        total_tokens=tokens,
        cost_microusd=cost,
        burn_rate_microusd_per_hour=(
            _cost_microusd({"cost": burn.get("costPerHour")})
            if isinstance(burn, Mapping)
            else None
        ),
        remaining_minutes=(
            _first_count(projection, "remainingMinutes")
            if isinstance(projection, Mapping)
            else 0
        ),
        projection_cost_microusd=(
            _cost_microusd({"cost": projection.get("totalCost")})
            if isinstance(projection, Mapping)
            else None
        ),
        projection_tokens=(
            _first_count(projection, "totalTokens")
            if isinstance(projection, Mapping)
            else 0
        ),
        models=models,
        unpriced_models=unpriced,
    )


def load_ccusage_block(
    *,
    settings: Mapping[str, Any] | None = None,
    runner: Runner | None = None,
    timeout: float = BLOCK_TIMEOUT_SECONDS,
    cancel_event: threading.Event | None = None,
) -> UsageBlock:
    values = dict(settings or usage_settings())
    if not values.get("enabled", True):
        return UsageBlock(status="unavailable", detail="disabled in config")
    configured_command = [
        str(part) for part in values.get("command", DEFAULT_COMMAND)
    ]
    executable = configured_command[0] if configured_command else ""
    if not executable or (os.sep not in executable and shutil.which(executable) is None):
        return UsageBlock(status="unavailable", detail="ccusage is not installed")
    preferred_offline = bool(values.get("offline", False))
    started = time.monotonic()

    def attempt(use_offline: bool, attempt_timeout: float) -> UsageBlock:
        command = ccusage_block_command(offline=use_offline, settings=values)
        try:
            completed = (
                runner(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=attempt_timeout,
                    check=False,
                )
                if runner is not None
                else _run_cancellable(command, attempt_timeout, cancel_event)
            )
        except _UsageCancelled:
            return UsageBlock(
                status="unavailable", detail="usage refresh cancelled"
            )
        except subprocess.TimeoutExpired:
            return UsageBlock(status="unavailable", detail="ccusage block timed out")
        except OSError:
            return UsageBlock(
                status="unavailable", detail="ccusage block could not start"
            )
        if completed.returncode != 0:
            return UsageBlock(
                status="unavailable", detail="ccusage block report failed"
            )
        try:
            return parse_ccusage_block_json(
                completed.stdout,
                pricing_source="cached" if use_offline else "online",
            )
        except ValueError as error:
            return UsageBlock(status="unavailable", detail=str(error))

    block = attempt(preferred_offline, timeout)
    if (
        block.status == "unavailable"
        and not preferred_offline
        and block.detail != "usage refresh cancelled"
    ):
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            return block
        cached = attempt(True, remaining)
        if cached.status == "available":
            return UsageBlock(
                status=cached.status,
                captured_epoch_ms=cached.captured_epoch_ms,
                pricing_source="cached",
                pricing_captured_epoch_ms=cached.pricing_captured_epoch_ms,
                start_time=cached.start_time,
                end_time=cached.end_time,
                total_tokens=cached.total_tokens,
                cost_microusd=cached.cost_microusd,
                burn_rate_microusd_per_hour=cached.burn_rate_microusd_per_hour,
                remaining_minutes=cached.remaining_minutes,
                projection_cost_microusd=cached.projection_cost_microusd,
                projection_tokens=cached.projection_tokens,
                models=cached.models,
                unpriced_models=cached.unpriced_models,
                detail="online pricing unavailable; using cached pricing",
            )
    return block


def usage_totals(samples: Iterable[UsageSample]) -> dict[str, Any]:
    rows = tuple(samples)
    input_tokens = min(MAX_SAFE_INTEGER, sum(row.input_tokens for row in rows))
    output_tokens = min(MAX_SAFE_INTEGER, sum(row.output_tokens for row in rows))
    cache_creation = min(
        MAX_SAFE_INTEGER, sum(row.cache_creation_tokens for row in rows)
    )
    cache_read = min(MAX_SAFE_INTEGER, sum(row.cache_read_tokens for row in rows))
    reasoning = min(MAX_SAFE_INTEGER, sum(row.reasoning_tokens for row in rows))
    uncategorized = min(
        MAX_SAFE_INTEGER, sum(row.uncategorized_tokens for row in rows)
    )
    total = min(
        MAX_SAFE_INTEGER,
        input_tokens
        + output_tokens
        + cache_creation
        + cache_read
        + uncategorized,
    )
    known_cost = min(
        MAX_SAFE_INTEGER, sum(row.cost_microusd or 0 for row in rows)
    )
    unpriced_by_model: dict[str, int] = {}
    for row in rows:
        for item in row.unpriced_models:
            unpriced_by_model[item.model] = min(
                MAX_SAFE_INTEGER,
                unpriced_by_model.get(item.model, 0) + item.tokens,
            )
    wholly_unpriced = sum(
        row.total_tokens
        for row in rows
        if row.cost_microusd is None and not row.unpriced_models
    )
    named_unpriced = sum(unpriced_by_model.values())
    unpriced_token_count = min(MAX_SAFE_INTEGER, wholly_unpriced + named_unpriced)
    priced_tokens = min(MAX_SAFE_INTEGER, max(0, total - unpriced_token_count))
    coverage = "complete"
    if not rows:
        coverage = "unavailable"
    elif all(row.cost_basis == "omitted" for row in rows):
        coverage = "omitted"
    elif any(row.coverage != "complete" for row in rows) or (
        priced_tokens and unpriced_token_count
    ):
        coverage = "partial"
    elif unpriced_token_count:
        coverage = "unpriced"
    totals: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_tokens": cache_creation,
        "cache_read_tokens": cache_read,
        "reasoning_tokens": reasoning,
        "uncategorized_tokens": uncategorized,
        "total_tokens": total,
        "priced_tokens": priced_tokens,
        "unpriced_tokens": unpriced_token_count,
        "unpriced_models": [
            {"model": model, "tokens": tokens}
            for model, tokens in sorted(unpriced_by_model.items())
        ],
        "pricing_coverage": coverage,
    }
    if known_cost or (rows and all(row.cost_microusd is not None for row in rows)):
        totals["cost_usd"] = known_cost / 1_000_000
    return totals


def samples_for_sessions(
    report: UsageReport, sessions: Iterable[tuple[str, str] | str]
) -> tuple[UsageSample, ...]:
    keys = {
        (_agent(item[0]), str(item[1])) if isinstance(item, tuple) else ("", str(item))
        for item in sessions
    }
    return tuple(
        sample
        for sample in report.samples
        if (sample.agent, sample.session_id) in keys
        or ("", sample.session_id) in keys
    )


def _compact_number(value: int) -> str:
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= divisor:
            return f"{value / divisor:.1f}".rstrip("0").rstrip(".") + suffix
    return str(value)


def usage_summary(
    report: UsageReport,
    sessions: Iterable[tuple[str, str] | str] | None = None,
) -> str:
    session_keys = None if sessions is None else tuple(sessions)
    selected = (
        report.samples
        if session_keys is None
        else samples_for_sessions(report, session_keys)
    )
    if not selected:
        return "Usage unavailable" + (f" · {report.detail}" if report.detail else "")
    totals = usage_totals(selected)
    total = int(totals["total_tokens"])
    cached = int(totals["cache_creation_tokens"]) + int(totals["cache_read_tokens"])
    cache_ratio = min(100, round(cached * 100 / total)) if total else 0
    cost = totals.get("cost_usd")
    coverage = str(totals["pricing_coverage"])
    if coverage == "omitted":
        cost_text = "cost omitted"
    elif cost is None:
        cost_text = "unpriced"
    else:
        priced = [row for row in selected if row.cost_microusd is not None]
        basis = (
            "API recorded"
            if priced and all(row.cost_basis == "recorded" for row in priced)
            else "API est"
        )
        partial = " · partial pricing" if coverage != "complete" else ""
        cost_text = f"{basis} ${cost:.2f}{partial}"
    suffix = " · stale" if report.status == "stale" else ""
    return f"Usage · {cost_text} · {_compact_number(total)} tok · {cache_ratio}% cached{suffix}"


def _display_agent(agent: str) -> str:
    return {
        "claude-code": "Claude",
        "codex": "Codex",
        "opencode": "OpenCode",
        "pi": "Pi",
    }.get(agent, agent.replace("-", " ").title())


def _selected(
    report: UsageReport,
    sessions: tuple[tuple[str, str] | str, ...] | None,
) -> tuple[UsageSample, ...]:
    return report.samples if sessions is None else samples_for_sessions(report, sessions)


def _cost_label(samples: Iterable[UsageSample]) -> str:
    rows = tuple(samples)
    totals = usage_totals(rows)
    cost = totals.get("cost_usd")
    coverage = totals["pricing_coverage"]
    if coverage == "omitted":
        return "API cost omitted"
    if cost is None:
        return "API cost unpriced"
    suffix = " · partial pricing" if coverage != "complete" else ""
    return f"API est ${float(cost):.2f}{suffix}"


def _captured_label(captured_epoch_ms: int) -> str:
    return datetime.fromtimestamp(captured_epoch_ms / 1000).astimezone().strftime("%H:%M")


def _aged_stale(captured_epoch_ms: int, now_epoch_ms: int, cadence: float) -> bool:
    return now_epoch_ms - captured_epoch_ms > max(0, cadence * 2 * 1000)


def _age_label(captured_epoch_ms: int, now_epoch_ms: int) -> str:
    seconds = max(0, (now_epoch_ms - captured_epoch_ms) // 1000)
    if seconds < 60:
        return f"{seconds}s old"
    minutes = seconds // 60
    return f"{minutes}m old" if minutes < 60 else f"{minutes // 60}h old"


def _unpriced_label(samples: Iterable[UsageSample]) -> str:
    entries = usage_totals(samples)["unpriced_models"]
    if not entries:
        return ""
    values = ", ".join(
        f"{item['model']} {_compact_number(int(item['tokens']))} tok"
        for item in entries
    )
    return f" · unpriced: {values}"


def _iso_epoch_ms(value: str) -> int | None:
    """Parse a ccusage block boundary without trusting it to be well formed."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return int(parsed.timestamp() * 1000)
    except (OverflowError, ValueError):
        return None


def _usage_bar(block: UsageBlock, now_epoch_ms: int, cells: int) -> str:
    start = _iso_epoch_ms(block.start_time)
    end = _iso_epoch_ms(block.end_time)
    if start is None or end is None or end <= start:
        return ""
    elapsed = min(1.0, max(0.0, (now_epoch_ms - start) / (end - start)))
    filled = min(cells, max(0, int(elapsed * cells + 0.5)))
    return "▰" * filled + "▱" * (cells - filled)


def _remaining_label(block: UsageBlock, now_epoch_ms: int) -> str:
    remaining = block.remaining_minutes
    end = _iso_epoch_ms(block.end_time)
    if not remaining and end is not None:
        remaining = max(0, math.ceil((end - now_epoch_ms) / 60_000))
    hours, minutes = divmod(remaining, 60)
    return f"{hours}h {minutes}m left" if hours else f"{minutes}m left"


def _gauge_unpriced_label(
    block: UsageBlock, today: Iterable[UsageSample]
) -> str:
    """Prefer the largest observed count when block and today overlap."""
    entries: dict[str, int] = {
        item.model: item.tokens for item in block.unpriced_models
    }
    totals = usage_totals(today)
    for item in totals["unpriced_models"]:
        model = str(item["model"])
        entries[model] = max(entries.get(model, 0), int(item["tokens"]))
    unnamed = int(totals["unpriced_tokens"]) - sum(
        int(item["tokens"]) for item in totals["unpriced_models"]
    )
    if unnamed > 0:
        entries["unknown"] = max(entries.get("unknown", 0), unnamed)
    if not entries:
        return "partial pricing" if totals["pricing_coverage"] == "partial" else ""
    return "unpriced: " + ", ".join(
        f"{model} {_compact_number(tokens)} tok"
        for model, tokens in sorted(entries.items())
    )


def usage_gauge_line(
    snapshot: LiveUsageSnapshot,
    sessions: Iterable[tuple[str, str] | str] | None = None,
    contexts: Iterable[Mapping[str, Any]] = (),
    *,
    root_count: int = 1,
    now_epoch_ms: int | None = None,
    session_cadence: float = DEFAULT_SESSION_REFRESH_SECONDS,
    block_cadence: float = DEFAULT_BLOCK_REFRESH_SECONDS,
    width: int | None = None,
    include_details: bool = False,
) -> tuple[str, tuple[str, ...]]:
    """Render the block gauge and optional lifetime detail for expansion.

    Narrow layouts remove age, today, half of the bar, then pace in that
    order. A pricing gap is never exchanged for the capture age: its model
    and token count occupy the final, high-priority field instead.
    """
    del contexts  # Agent attribution remains in the expanded session rows.
    now_ms = int(time.time() * 1000) if now_epoch_ms is None else now_epoch_ms
    keys = None if sessions is None else tuple(sessions)
    today = _selected(snapshot.today, keys)
    block = snapshot.block

    if block.status in {"available", "stale"}:
        if block.cost_microusd is not None:
            block_cost = f"${block.cost_microusd / 1_000_000:.2f} this block"
        elif block.pricing_source == "omitted":
            block_cost = "cost omitted this block"
        else:
            block_cost = "unpriced this block"
        pace = (
            f"${block.burn_rate_microusd_per_hour / 1_000_000:.2f}/hr"
            if block.burn_rate_microusd_per_hour is not None
            else ""
        )
    else:
        block_cost = "block unavailable"
        pace = ""

    if today:
        today_totals = usage_totals(today)
        today_cost = today_totals.get("cost_usd")
        if today_cost is not None:
            today_label = f"today ${float(today_cost):.2f}"
        elif today_totals["pricing_coverage"] == "omitted":
            today_label = "today cost omitted"
        else:
            today_label = "today unpriced"
    else:
        today_label = "today unavailable"

    stale = (
        block.status == "stale"
        or (
            block.status == "available"
            and _aged_stale(block.captured_epoch_ms, now_ms, block_cadence)
        )
        or snapshot.today.status == "stale"
        or (
            snapshot.today.status == "available"
            and _aged_stale(
                snapshot.today.captured_epoch_ms, now_ms, session_cadence
            )
        )
    )
    contributing_captures = [
        *(
            [block.captured_epoch_ms]
            if block.status in {"available", "stale"}
            else []
        ),
        *([snapshot.today.captured_epoch_ms] if today else []),
    ]
    oldest_capture = min(contributing_captures) if contributing_captures else None
    unpriced = _gauge_unpriced_label(block, today)

    def render_candidate(
        *, age: bool, show_today: bool, bar_cells: int, show_pace: bool
    ) -> str:
        bar = (
            _usage_bar(block, now_ms, bar_cells)
            if block.status in {"available", "stale"}
            else ""
        )
        leading = " ".join(
            part
            for part in (
                block_cost,
                bar,
                _remaining_label(block, now_ms)
                if block.status in {"available", "stale"}
                else "",
            )
            if part
        )
        fields = [leading]
        if show_pace and pace:
            fields.append(pace)
        if show_today:
            fields.append(today_label)
        if stale:
            fields.append("stale")
        if unpriced:
            fields.append(unpriced)
        elif age and oldest_capture is not None:
            fields.append(f"as of {_captured_label(oldest_capture)}")
        return " · ".join(fields)

    candidates = (
        {"age": True, "show_today": True, "bar_cells": 8, "show_pace": True},
        {"age": False, "show_today": True, "bar_cells": 8, "show_pace": True},
        {"age": False, "show_today": False, "bar_cells": 8, "show_pace": True},
        {"age": False, "show_today": False, "bar_cells": 4, "show_pace": True},
        {"age": False, "show_today": False, "bar_cells": 4, "show_pace": False},
    )
    line = render_candidate(**candidates[-1])
    for candidate in candidates:
        rendered = render_candidate(**candidate)
        line = rendered
        if width is None or len(rendered) <= width:
            break
    if width is not None and len(line) > width and unpriced:
        if block.status not in {"available", "stale"}:
            compact_block = "no block"
        elif block.cost_microusd is not None:
            compact_block = f"${block.cost_microusd / 1_000_000:.2f} block"
        elif block.pricing_source == "omitted":
            compact_block = "cost omitted"
        else:
            compact_block = "unpriced block"
        warning_only = (
            unpriced
            if len(unpriced) <= width
            else "partial pricing"
            if len("partial pricing") <= width
            else "partial"[: max(0, width)]
        )
        critical_fields = (compact_block, "stale" if stale else "", unpriced)
        critical_candidates = (
            " · ".join(part for part in critical_fields if part),
            f"{compact_block} · {unpriced}",
            warning_only,
        )
        line = next(
            (
                candidate
                for candidate in critical_candidates
                if len(candidate) <= width
            ),
            warning_only,
        )

    details: tuple[str, ...] = ()
    if include_details:
        details = (
            live_usage_lines(
                snapshot,
                keys,
                (),
                root_count=root_count,
                now_epoch_ms=now_ms,
                session_cadence=session_cadence,
                block_cadence=block_cadence,
            )[2],
        )
    return line, details


def live_usage_lines(
    snapshot: LiveUsageSnapshot,
    sessions: Iterable[tuple[str, str] | str] | None = None,
    contexts: Iterable[Mapping[str, Any]] = (),
    *,
    root_count: int = 1,
    now_epoch_ms: int | None = None,
    session_cadence: float = DEFAULT_SESSION_REFRESH_SECONDS,
    block_cadence: float = DEFAULT_BLOCK_REFRESH_SECONDS,
) -> tuple[str, str, str]:
    """Render independently captured today, active-block, and history states."""
    now_ms = int(time.time() * 1000) if now_epoch_ms is None else now_epoch_ms
    keys = None if sessions is None else tuple(sessions)
    today = _selected(snapshot.today, keys)
    history = _selected(snapshot.history, keys)
    scope = f"{root_count} shown roots" if root_count > 1 else "shown root"
    if today:
        totals = usage_totals(today)
        stale = snapshot.today.status == "stale" or _aged_stale(
            snapshot.today.captured_epoch_ms, now_ms, session_cadence
        )
        today_line = (
            f"Today · {scope} · {_cost_label(today)} · "
            f"{_compact_number(int(totals['total_tokens']))} tok · "
            f"as of {_captured_label(snapshot.today.captured_epoch_ms)}"
            f"{' · stale' if stale else ''}{_unpriced_label(today)}"
        )
    else:
        detail = (
            snapshot.today.detail
            or ("no matched sessions" if snapshot.today.status == "available" else "loading")
        )
        today_line = (
            f"Today · {scope} · {detail} · "
            f"as of {_captured_label(snapshot.today.captured_epoch_ms)}"
        )

    active = sum(
        1
        for context in contexts
        if _safe_text(context.get("status"), 32).lower()
        in {"active", "running", "working"}
    )
    block = snapshot.block
    if block.status in {"available", "stale"}:
        block_cost = (
            f"API est ${block.cost_microusd / 1_000_000:.2f}"
            if block.cost_microusd is not None
            else "API cost unpriced"
        )
        burn = (
            f"API pace ${block.burn_rate_microusd_per_hour / 1_000_000:.2f}/hr"
            if block.burn_rate_microusd_per_hour is not None
            else "API rate unavailable"
        )
        hours, minutes = divmod(block.remaining_minutes, 60)
        remaining = f"ends in {hours}h {minutes:02d}m" if hours else f"ends in {minutes}m"
        stale = block.status == "stale" or _aged_stale(
            block.captured_epoch_ms, now_ms, block_cadence
        )
        noun = "session" if active == 1 else "sessions"
        block_line = (
            "Current 5h window · machine-wide · "
            f"{block_cost} · {burn} · {remaining} · "
            f"{active} active {noun} · as of {_captured_label(block.captured_epoch_ms)}"
            f"{' · stale' if stale else ''}"
        )
    else:
        block_line = (
            "Current 5h window · machine-wide · "
            f"{block.detail or 'loading'} · "
            f"as of {_captured_label(block.captured_epoch_ms)}"
        )

    if history:
        totals = usage_totals(history)
        stale = snapshot.history.status == "stale" or _aged_stale(
            snapshot.history.captured_epoch_ms, now_ms, session_cadence
        )
        noun = "session" if len(history) == 1 else "sessions"
        history_line = (
            f"Tracked lifetime · {scope} · {_cost_label(history)} · "
            f"{_compact_number(int(totals['total_tokens']))} tok · "
            f"{len(history)} matched {noun} · "
            f"as of {_captured_label(snapshot.history.captured_epoch_ms)}"
            f"{' · stale' if stale else ''}{_unpriced_label(history)}"
        )
    else:
        detail = (
            snapshot.history.detail
            or (
                "no matched sessions"
                if snapshot.history.status == "available"
                else "loading"
            )
        )
        history_line = (
            f"Tracked lifetime · {scope} · {detail} · "
            f"as of {_captured_label(snapshot.history.captured_epoch_ms)}"
        )
    return today_line, block_line, history_line


def _session_rows(
    snapshot: LiveUsageSnapshot,
    sessions: tuple[tuple[str, str] | str, ...] | None,
    contexts: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    today = _selected(snapshot.today, sessions)
    history = _selected(snapshot.history, sessions)
    today_by_key = {(row.agent, row.session_id): row for row in today}
    context_by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    for context in contexts:
        session_id = context.get("session_id")
        if isinstance(session_id, str) and session_id:
            context_by_key[(_agent(context.get("agent")), session_id)] = context
    history_by_key = {(row.agent, row.session_id): row for row in history}
    combined = [
        *history,
        *(row for key, row in today_by_key.items() if key not in history_by_key),
    ]
    ordered = sorted(
        combined,
        key=lambda row: row.last_activity,
        reverse=True,
    )
    rows: list[dict[str, Any]] = []
    for index, lifetime in enumerate(ordered, 1):
        key = (lifetime.agent, lifetime.session_id)
        current = today_by_key.get(key)
        lifetime = history_by_key.get(key, lifetime)
        context = context_by_key.get(key, {})
        status = _safe_text(context.get("status"), 32).lower()
        status = "active" if status in {"active", "running", "working"} else (
            "idle" if context else "history"
        )
        label = _safe_text(context.get("label"), 128) or (
            f"{_display_agent(lifetime.agent)} session {index}"
        )
        row: dict[str, Any] = {
            "agent": lifetime.agent,
            "label": label,
            "status": status,
            "model": _safe_text(context.get("model"), 256) or lifetime.model,
            "today_tokens": current.total_tokens if current else 0,
            "lifetime_tokens": lifetime.total_tokens,
            "coverage": lifetime.coverage,
            "last_activity": (
                _safe_text(context.get("last_activity"), 64)
                or (current.last_activity if current else "")
                or lifetime.last_activity
            ),
        }
        if current and current.cost_microusd is not None:
            row["today_cost_usd"] = current.cost_microusd / 1_000_000
        if lifetime.cost_microusd is not None:
            row["lifetime_cost_usd"] = lifetime.cost_microusd / 1_000_000
        rows.append(row)
    return rows


def usage_summary_wire(
    report: UsageReport | LiveUsageSnapshot,
    sessions: Iterable[tuple[str, str] | str] | None = None,
    contexts: Iterable[Mapping[str, Any]] = (),
    *,
    root_count: int = 1,
    now_epoch_ms: int | None = None,
    session_cadence: float = DEFAULT_SESSION_REFRESH_SECONDS,
    block_cadence: float = DEFAULT_BLOCK_REFRESH_SECONDS,
    include_pricing_age: bool = True,
) -> dict[str, Any]:
    snapshot = (
        report
        if isinstance(report, LiveUsageSnapshot)
        else LiveUsageSnapshot(
            UsageReport("session", status="unavailable", detail="loading"),
            report,
            UsageBlock(detail="loading"),
        )
    )
    session_keys = None if sessions is None else tuple(sessions)
    gauge, detail_lines = usage_gauge_line(
        snapshot,
        session_keys,
        contexts,
        root_count=root_count,
        now_epoch_ms=now_epoch_ms,
        session_cadence=session_cadence,
        block_cadence=block_cadence,
        include_details=True,
    )
    now_ms = int(time.time() * 1000) if now_epoch_ms is None else now_epoch_ms
    pricing = {
        "today": {
            "source": snapshot.today.pricing_source,
            "captured_epoch_ms": snapshot.today.pricing_captured_epoch_ms,
        },
        "history": {
            "source": snapshot.history.pricing_source,
            "captured_epoch_ms": snapshot.history.pricing_captured_epoch_ms,
        },
        "block": {
            "source": snapshot.block.pricing_source,
            "captured_epoch_ms": snapshot.block.pricing_captured_epoch_ms,
        },
    }
    if include_pricing_age:
        for value in pricing.values():
            value["age"] = _age_label(int(value["captured_epoch_ms"]), now_ms)
    return {
        "schema": LIVE_USAGE_SCHEMA,
        "status": snapshot.history.status,
        "label": gauge,
        "lines": [gauge, *detail_lines],
        "pricing": pricing,
        "pricing_label": (
            "Pricing · "
            + " · ".join(
                f"{name} {value['source']} ({value['age']})"
                for name, value in pricing.items()
            )
            if include_pricing_age
            else ""
        ),
        "rows": _session_rows(snapshot, session_keys, contexts),
    }


def render_usage_table(report: UsageReport) -> str:
    if not report.samples:
        return usage_summary(report)
    headings = (
        "Agent",
        "Period/session",
        "Model",
        "Input",
        "Output",
        "Cache",
        "Other",
        "Cost",
    )
    rows: list[tuple[str, ...]] = []
    for sample in report.samples:
        cached = sample.cache_creation_tokens + sample.cache_read_tokens
        cost = (
            f"API {'recorded' if sample.cost_basis == 'recorded' else 'est'} "
            f"${sample.cost_microusd / 1_000_000:.2f}"
            if sample.cost_microusd is not None
            else "unpriced" if sample.cost_basis != "omitted" else "omitted"
        )
        rows.append(
            (
                sample.agent,
                sample.period or "—",
                sample.model or "—",
                _compact_number(sample.input_tokens),
                _compact_number(sample.output_tokens),
                _compact_number(cached),
                _compact_number(sample.uncategorized_tokens),
                cost,
            )
        )
    widths = [len(heading) for heading in headings]
    for row in rows:
        widths = [max(width, len(value)) for width, value in zip(widths, row, strict=True)]
    line = "  ".join(heading.ljust(width) for heading, width in zip(headings, widths, strict=True))
    rendered = [line, "  ".join("─" * width for width in widths)]
    rendered.extend(
        "  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True))
        for row in rows
    )
    rendered.append(usage_summary(report))
    return "\n".join(rendered)


class UsageMonitor:
    """Independent fast block and staggered slow session snapshots."""

    def __init__(
        self,
        *,
        settings: Mapping[str, Any] | None = None,
        loader: Callable[..., UsageReport] = load_ccusage,
        block_loader: Callable[..., UsageBlock] = load_ccusage_block,
    ) -> None:
        self.settings = dict(settings or usage_settings())
        self.loader = loader
        self.block_loader = block_loader
        self.today_report = UsageReport(
            "session", status="unavailable", detail="loading"
        )
        self.report = UsageReport("session", status="unavailable", detail="loading")
        self.block = UsageBlock(detail="loading")
        self._future: Future[UsageReport] | None = None
        self._future_kind = "history"
        self._block_future: Future[UsageBlock] | None = None
        self._last_slow_started = float("-inf")
        self._last_block_started = float("-inf")
        self._last_completed = {"history": float("-inf"), "today": float("-inf")}
        self._next_slow_kind = "history"
        self._cancel_event = threading.Event()
        self._slow_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="side-dog-usage"
        )
        self._block_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="side-dog-usage-block"
        )

    @property
    def snapshot(self) -> LiveUsageSnapshot:
        return LiveUsageSnapshot(self.today_report, self.report, self.block)

    @staticmethod
    def _retain_report(previous: UsageReport, result: UsageReport) -> UsageReport:
        if result.status == "available":
            return result
        if not previous.samples:
            return result
        return UsageReport(
            "session",
            samples=previous.samples,
            status="stale",
            captured_epoch_ms=previous.captured_epoch_ms,
            pricing_source=previous.pricing_source,
            pricing_captured_epoch_ms=previous.pricing_captured_epoch_ms,
            detail=result.detail,
        )

    @staticmethod
    def _retain_block(previous: UsageBlock, result: UsageBlock) -> UsageBlock:
        if result.status == "available":
            return result
        if result.detail == "no active usage block":
            return result
        if previous.status != "available" and previous.status != "stale":
            return result
        return UsageBlock(
            status="stale",
            captured_epoch_ms=previous.captured_epoch_ms,
            pricing_source=previous.pricing_source,
            pricing_captured_epoch_ms=previous.pricing_captured_epoch_ms,
            start_time=previous.start_time,
            end_time=previous.end_time,
            total_tokens=previous.total_tokens,
            cost_microusd=previous.cost_microusd,
            burn_rate_microusd_per_hour=previous.burn_rate_microusd_per_hour,
            remaining_minutes=previous.remaining_minutes,
            projection_cost_microusd=previous.projection_cost_microusd,
            projection_tokens=previous.projection_tokens,
            models=previous.models,
            unpriced_models=previous.unpriced_models,
            detail=result.detail,
        )

    def tick(self, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        changed = False
        if self._future is not None and self._future.done():
            try:
                result = self._future.result()
            except Exception:
                result = UsageReport(
                    "session", status="unavailable", detail="usage refresh failed"
                )
            self._future = None
            if self._future_kind == "today":
                self.today_report = self._retain_report(self.today_report, result)
            else:
                self.report = self._retain_report(self.report, result)
            self._last_completed[self._future_kind] = moment
            changed = True
        if self._block_future is not None and self._block_future.done():
            try:
                block = self._block_future.result()
            except Exception:
                block = UsageBlock(detail="usage block refresh failed")
            self._block_future = None
            self.block = self._retain_block(self.block, block)
            changed = True

        session_refresh = max(
            MIN_SESSION_SCAN_SECONDS,
            float(
                self.settings.get(
                    "session_refresh_seconds",
                    self.settings.get("refresh_seconds", DEFAULT_SESSION_REFRESH_SECONDS),
                )
            ),
        )
        slow_gap = min(session_refresh, MIN_SESSION_SCAN_SECONDS)
        if self._future is None and moment - self._last_slow_started >= slow_gap:
            kind = self._next_slow_kind
            never_loaded = (
                self.report.detail == "loading"
                if kind == "history"
                else self.today_report.detail == "loading"
            )
            if never_loaded or moment - self._last_completed[kind] >= session_refresh:
                self._future_kind = kind
                self._next_slow_kind = "today" if kind == "history" else "history"
                self._last_slow_started = moment
                kwargs: dict[str, Any] = {
                    "settings": self.settings,
                    "cancel_event": self._cancel_event,
                }
                if kind == "today":
                    kwargs["since"] = datetime.now().astimezone().date().isoformat()
                self._future = self._slow_executor.submit(
                    self.loader, "session", **kwargs
                )

        block_refresh = float(
            self.settings.get("block_refresh_seconds", DEFAULT_BLOCK_REFRESH_SECONDS)
        )
        if (
            self._block_future is None
            and moment - self._last_block_started >= block_refresh
        ):
            self._last_block_started = moment
            self._block_future = self._block_executor.submit(
                self.block_loader,
                settings=self.settings,
                cancel_event=self._cancel_event,
            )
        return changed

    def close(self) -> None:
        self._cancel_event.set()
        self._slow_executor.shutdown(wait=True, cancel_futures=True)
        self._block_executor.shutdown(wait=True, cancel_futures=True)


def ccusage_readiness(settings: Mapping[str, Any] | None = None) -> tuple[str, str]:
    values = dict(settings or usage_settings())
    if not values.get("enabled", True):
        return "info", "Token usage reporting is disabled in config."
    command = tuple(values.get("command", DEFAULT_COMMAND))
    executable = str(command[0]) if command else ""
    if not executable or (os.sep not in executable and shutil.which(executable) is None):
        return "info", "Optional ccusage is absent; token usage is unavailable."
    try:
        completed = subprocess.run(
            [*map(str, command), "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "warn", "Optional ccusage could not be checked; token usage is unavailable."
    if completed.returncode != 0:
        return "warn", "Optional ccusage did not report a version; token usage may be unavailable."
    version = _safe_text(completed.stdout or completed.stderr, 80) or "version available"
    for view in ("session", "daily"):
        report = load_ccusage(
            view,
            settings=values,
            runner=subprocess.run,
            timeout=5,
        )
        if report.status != "available":
            return (
                "warn",
                f"Optional ccusage is installed ({version}) but its {view} JSON "
                "report is incompatible or unavailable.",
            )
    return "ok", f"Optional ccusage is ready ({version}); JSON report compatible."
