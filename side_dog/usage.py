"""Privacy-safe token usage reports from an optional local ccusage command.

Usage is cumulative state, not activity.  Nothing in this module writes to the
per-project event history, and raw ccusage rows never leave the parser.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from side_dog.config import config_usage, load_config


USAGE_SCHEMA = "side-dog-usage-v1"
MAX_SAFE_INTEGER = 2**53 - 1
USAGE_VIEWS = ("daily", "monthly", "session")
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
        "total_tokens",
        "cost_usd",
        "cost_basis",
        "coverage",
        "last_activity",
    }
)
USAGE_REPORT_WIRE_FIELDS = frozenset(
    {"schema", "view", "status", "captured_epoch_ms", "detail", "rows", "totals"}
)
CCUSAGE_AGENT_ALIASES = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "pi-agent": "pi",
    "pi": "pi",
    "codex": "codex",
    "opencode": "opencode",
    "antigravity": "antigravity",
    "grok": "grok",
}
DEFAULT_COMMAND = ("ccusage",)
DEFAULT_AGENT = "claude-code"
DEFAULT_REFRESH_SECONDS = 30.0
DEFAULT_TIMEOUT_SECONDS = 20.0


def _safe_text(value: Any, limit: int = 256) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(
        character
        for character in value[:limit]
        if character in "\t" or ord(character) >= 32
    ).strip()


def _agent(value: Any) -> str:
    raw = _safe_text(value, 64).casefold().replace("_", "-")
    return CCUSAGE_AGENT_ALIASES.get(raw, raw or "unknown")


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if not math.isfinite(value) or value < 0:
        return 0
    return min(MAX_SAFE_INTEGER, int(value))


def _first_count(row: Mapping[str, Any], *names: str) -> int:
    for name in names:
        if name in row:
            return _count(row[name])
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
    cost_microusd: int | None = None
    cost_basis: str = "unpriced"
    coverage: str = "complete"
    last_activity: str = ""

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

    @property
    def total_tokens(self) -> int:
        # Reasoning is reported for context but is already part of output for
        # the providers ccusage normalizes.  Never add it a second time.
        return min(
            MAX_SAFE_INTEGER,
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + self.cache_read_tokens,
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
            "total_tokens": self.total_tokens,
            "cost_basis": self.cost_basis,
            "coverage": self.coverage,
            "last_activity": self.last_activity,
        }
        if self.cost_microusd is not None:
            wire["cost_usd"] = self.cost_microusd / 1_000_000
        return wire

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> UsageSample:
        unknown = set(value) - USAGE_SAMPLE_WIRE_FIELDS
        if unknown:
            raise ValueError("unsupported usage sample field")
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
            cost_microusd=_cost_microusd(value),
            cost_basis=_safe_text(value.get("cost_basis"), 32) or "unpriced",
            coverage=_safe_text(value.get("coverage"), 32) or "complete",
            last_activity=_safe_text(value.get("last_activity"), 64),
        )


@dataclass(frozen=True, slots=True)
class UsageReport:
    view: str
    samples: tuple[UsageSample, ...] = ()
    status: str = "available"
    captured_epoch_ms: int = 0
    detail: str = ""

    def __post_init__(self) -> None:
        if self.view not in USAGE_VIEWS:
            raise ValueError("unsupported usage view")
        if self.status not in {"available", "unavailable", "stale"}:
            raise ValueError("unsupported usage report status")
        if not self.captured_epoch_ms:
            object.__setattr__(self, "captured_epoch_ms", int(time.time() * 1000))
        object.__setattr__(self, "detail", _safe_text(self.detail, 256))

    def to_wire(self) -> dict[str, Any]:
        return {
            "schema": USAGE_SCHEMA,
            "view": self.view,
            "status": self.status,
            "captured_epoch_ms": self.captured_epoch_ms,
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
            detail=_safe_text(value.get("detail"), 256),
        )


def _rows(document: Any, view: str) -> list[Mapping[str, Any]]:
    if isinstance(document, list):
        return [row for row in document if isinstance(row, Mapping)]
    if not isinstance(document, Mapping):
        raise ValueError("ccusage JSON must be an object or array")
    projects = document.get("projects")
    if isinstance(projects, Mapping):
        project_rows: list[Mapping[str, Any]] = []
        for value in projects.values():
            if isinstance(value, list):
                project_rows.extend(
                    row for row in value if isinstance(row, Mapping)
                )
        return project_rows
    candidates = (view, "sessions" if view == "session" else view, "data")
    for name in candidates:
        value = document.get(name)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, Mapping)]
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


def _sample(
    row: Mapping[str, Any],
    view: str,
    mode: str,
    no_cost: bool,
    default_agent: str,
) -> UsageSample:
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
        coverage = "partial" if row.get("isFallback") is True else "complete"
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
    )


def parse_ccusage_json(
    output: str,
    view: str,
    *,
    mode: str = "auto",
    no_cost: bool = False,
    default_agent: str = DEFAULT_AGENT,
) -> UsageReport:
    if view not in USAGE_VIEWS:
        raise ValueError("unsupported usage view")
    if mode not in {"auto", "calculate", "display"}:
        raise ValueError("unsupported cost mode")
    try:
        document = json.loads(output)
    except json.JSONDecodeError as error:
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
            for nested in agents:
                if isinstance(nested, Mapping):
                    flattened.append({"period": _period(row, view), **nested})
        else:
            flattened.append(row)
    samples = tuple(
        _sample(row, view, mode, no_cost, default_agent) for row in flattened
    )
    return UsageReport(view=view, samples=samples)


def usage_settings(document: Mapping[str, Any] | None = None) -> dict[str, Any]:
    configured = config_usage(dict(document) if document is not None else load_config())
    return {
        "enabled": configured.get("enabled", True),
        "command": tuple(configured.get("command", DEFAULT_COMMAND)),
        "agent": configured.get("agent", DEFAULT_AGENT),
        "offline": configured.get("offline", True),
        "refresh_seconds": configured.get(
            "refresh_seconds", DEFAULT_REFRESH_SECONDS
        ),
    }


def ccusage_command(
    view: str,
    *,
    since: str | None = None,
    until: str | None = None,
    mode: str = "auto",
    no_cost: bool = False,
    project: str | None = None,
    settings: Mapping[str, Any] | None = None,
) -> list[str]:
    values = dict(settings or usage_settings())
    command = [str(part) for part in values.get("command", DEFAULT_COMMAND)]
    command.extend((view, "--json", "--mode", mode))
    if values.get("offline", True):
        command.append("--offline")
    if since:
        command.extend(("--since", since))
    if until:
        command.extend(("--until", until))
    if no_cost:
        command.append("--no-cost")
    if project and view != "session":
        command.extend(("--instances", "--project", project))
    return command


Runner = Callable[..., subprocess.CompletedProcess[str]]


def load_ccusage(
    view: str = "session",
    *,
    since: str | None = None,
    until: str | None = None,
    mode: str = "auto",
    no_cost: bool = False,
    project: str | None = None,
    settings: Mapping[str, Any] | None = None,
    runner: Runner = subprocess.run,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> UsageReport:
    values = dict(settings or usage_settings())
    if not values.get("enabled", True):
        return UsageReport(view, status="unavailable", detail="disabled in config")
    command = ccusage_command(
        view,
        since=since,
        until=until,
        mode=mode,
        no_cost=no_cost,
        project=project,
        settings=values,
    )
    executable = command[0] if command else ""
    if not executable or (os.sep not in executable and shutil.which(executable) is None):
        return UsageReport(view, status="unavailable", detail="ccusage is not installed")
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return UsageReport(view, status="unavailable", detail="ccusage timed out")
    except OSError:
        return UsageReport(view, status="unavailable", detail="ccusage could not start")
    if completed.returncode != 0:
        return UsageReport(view, status="unavailable", detail="ccusage report failed")
    try:
        return parse_ccusage_json(
            completed.stdout,
            view,
            mode=mode,
            no_cost=no_cost,
            default_agent=str(values.get("agent", DEFAULT_AGENT)),
        )
    except ValueError as error:
        return UsageReport(view, status="unavailable", detail=str(error))


def usage_totals(samples: Iterable[UsageSample]) -> dict[str, Any]:
    rows = tuple(samples)
    input_tokens = min(MAX_SAFE_INTEGER, sum(row.input_tokens for row in rows))
    output_tokens = min(MAX_SAFE_INTEGER, sum(row.output_tokens for row in rows))
    cache_creation = min(
        MAX_SAFE_INTEGER, sum(row.cache_creation_tokens for row in rows)
    )
    cache_read = min(MAX_SAFE_INTEGER, sum(row.cache_read_tokens for row in rows))
    reasoning = min(MAX_SAFE_INTEGER, sum(row.reasoning_tokens for row in rows))
    total = min(
        MAX_SAFE_INTEGER, input_tokens + output_tokens + cache_creation + cache_read
    )
    known_cost = min(
        MAX_SAFE_INTEGER, sum(row.cost_microusd or 0 for row in rows)
    )
    priced_tokens = min(
        MAX_SAFE_INTEGER,
        sum(row.total_tokens for row in rows if row.cost_microusd is not None),
    )
    coverage = (
        "unavailable"
        if not rows
        else "omitted"
        if all(row.cost_basis == "omitted" for row in rows)
        else "partial"
        if any(row.coverage != "complete" for row in rows)
        else "complete"
        if priced_tokens == total or total == 0
        else "partial"
        if priced_tokens
        else "unpriced"
    )
    totals: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_tokens": cache_creation,
        "cache_read_tokens": cache_read,
        "reasoning_tokens": reasoning,
        "total_tokens": total,
        "priced_tokens": priced_tokens,
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
    cache_ratio = round(cached * 100 / total) if total else 0
    cost = totals.get("cost_usd")
    coverage = str(totals["pricing_coverage"])
    if coverage == "omitted":
        cost_text = "cost omitted"
    elif cost is None:
        cost_text = "unpriced"
    else:
        priced = [row for row in selected if row.cost_microusd is not None]
        basis = (
            "recorded"
            if priced and all(row.cost_basis == "recorded" for row in priced)
            else "est"
        )
        partial = " · partial pricing" if coverage != "complete" else ""
        cost_text = f"${cost:.2f} {basis}{partial}"
    suffix = " · stale" if report.status == "stale" else ""
    return f"Usage · {cost_text} · {_compact_number(total)} tok · {cache_ratio}% cached{suffix}"


def usage_summary_wire(
    report: UsageReport,
    sessions: Iterable[tuple[str, str] | str] | None = None,
) -> dict[str, Any]:
    session_keys = None if sessions is None else tuple(sessions)
    selected = (
        report.samples
        if session_keys is None
        else samples_for_sessions(report, session_keys)
    )
    rows = []
    for sample in selected:
        row = sample.to_wire()
        for field in ("period", "session_id", "last_activity"):
            row.pop(field, None)
        rows.append(row)
    return {
        "schema": USAGE_SCHEMA,
        "status": report.status,
        "label": usage_summary(report, session_keys),
        "totals": usage_totals(selected),
        "rows": rows,
    }


def render_usage_table(report: UsageReport) -> str:
    if not report.samples:
        return usage_summary(report)
    headings = ("Agent", "Period/session", "Model", "Input", "Output", "Cache", "Cost")
    rows: list[tuple[str, ...]] = []
    for sample in report.samples:
        cached = sample.cache_creation_tokens + sample.cache_read_tokens
        cost = (
            f"${sample.cost_microusd / 1_000_000:.2f} {sample.cost_basis}"
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
    """One non-blocking, replace-only session usage snapshot."""

    def __init__(
        self,
        *,
        settings: Mapping[str, Any] | None = None,
        loader: Callable[..., UsageReport] = load_ccusage,
    ) -> None:
        self.settings = dict(settings or usage_settings())
        self.loader = loader
        self.report = UsageReport("session", status="unavailable", detail="loading")
        self._future: Future[UsageReport] | None = None
        self._last_started = float("-inf")
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="side-dog-usage")

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
            if result.status == "available":
                self.report = result
            elif self.report.samples:
                self.report = UsageReport(
                    "session",
                    samples=self.report.samples,
                    status="stale",
                    captured_epoch_ms=self.report.captured_epoch_ms,
                    detail=result.detail,
                )
            else:
                self.report = result
            changed = True
        refresh = float(self.settings.get("refresh_seconds", DEFAULT_REFRESH_SECONDS))
        if self._future is None and moment - self._last_started >= refresh:
            self._last_started = moment
            self._future = self._executor.submit(
                self.loader, "session", settings=self.settings
            )
        return changed

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


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
    report = load_ccusage(
        "session",
        no_cost=True,
        settings=values,
        runner=subprocess.run,
        timeout=5,
    )
    if report.status != "available":
        return (
            "warn",
            f"Optional ccusage is installed ({version}) but its JSON report "
            "is incompatible or unavailable.",
        )
    return "ok", f"Optional ccusage is ready ({version}); JSON report compatible."
