"""Typed values shared by coding-agent integrations.

The collectors still speak dictionaries at their I/O boundaries.  These small
objects make the values inside that boundary explicit while keeping the
``side-dog-activity-v1`` JSONL and current identity dictionaries compatible.
"""

from __future__ import annotations

import re
from math import isfinite
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, fields
from enum import StrEnum
from importlib import import_module
from types import MappingProxyType
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit


ACTIVITY_SCHEMA = "side-dog-activity-v1"
UNKNOWN = "unknown"
MAX_SAFE_INTEGER = 9_007_199_254_740_991

_PROVIDER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_MAX_EXTRA_DEPTH = 32


def _provider_spelling(value: Any) -> str:
    provider = str(value or "").strip().casefold()
    return provider if provider and _PROVIDER_NAME.fullmatch(provider) else UNKNOWN


def normalize_provider(value: Any) -> str:
    """Return the registered provider or preserve an unfamiliar valid name."""
    provider = _provider_spelling(value)
    aliases = globals().get("INTEGRATION_ALIASES", {})
    descriptor = aliases.get(provider)
    if descriptor is None and "_" in provider:
        descriptor = aliases.get(provider.replace("_", "-"))
    return descriptor.provider if descriptor is not None else provider


class AgentStatus(StrEnum):
    UNKNOWN = UNKNOWN
    WORKING = "working"
    BLOCKED = "blocked"
    IDLE = "idle"
    DONE = "done"

    @classmethod
    def from_wire(cls, value: Any) -> AgentStatus:
        status = str(value or "").strip().casefold()
        status = {
            "active": cls.WORKING,
            "busy": cls.WORKING,
            "running": cls.WORKING,
            "inactive": cls.IDLE,
            "completed": cls.DONE,
            "stopped": cls.DONE,
        }.get(status, status)
        try:
            return cls(status)
        except ValueError:
            return cls.UNKNOWN


class AdapterHealthStatus(StrEnum):
    UNKNOWN = UNKNOWN
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"

    @classmethod
    def from_wire(cls, value: Any) -> AdapterHealthStatus:
        status = str(value or "").strip().casefold()
        try:
            return cls(status)
        except ValueError:
            return cls.UNKNOWN


@dataclass(frozen=True, slots=True)
class SessionKey:
    """An external session identifier qualified by its agent provider."""

    provider: str = UNKNOWN
    session_id: str = UNKNOWN

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", normalize_provider(self.provider))
        session_id = str(self.session_id or "").strip() or UNKNOWN
        object.__setattr__(self, "session_id", session_id)

    def __str__(self) -> str:
        return f"{self.provider}:{self.session_id}"

    def to_wire(self) -> str:
        return str(self)

    @classmethod
    def from_wire(cls, value: Any, *, provider: Any = None) -> SessionKey:
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return cls(
                value.get("provider", value.get("agent", provider)),
                value.get("session_id", value.get("session", UNKNOWN)),
            )
        text = str(value or "").strip()
        if ":" in text:
            parsed_provider, session_id = text.split(":", 1)
            return cls(parsed_provider, session_id)
        return cls(provider, text)


def _known_field_names(value_type: type[Any]) -> set[str]:
    return {item.name for item in fields(value_type) if item.name != "extras"}


def _extras(
    wire: Mapping[str, Any], value_type: type[Any], aliases: set[str] | None = None
) -> dict[str, Any]:
    known = _known_field_names(value_type) | (aliases or set())
    return {key: value for key, value in wire.items() if key not in known}


def _freeze_extra(value: Any, *, depth: int = 0) -> Any:
    if depth > _MAX_EXTRA_DEPTH:
        raise ValueError("integration metadata is nested too deeply")
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_extra(item, depth=depth + 1) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_extra(item, depth=depth + 1) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_extra(item, depth=depth + 1) for item in value)
    return deepcopy(value)


def _thaw_extra(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_extra(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_extra(item) for item in value]
    if isinstance(value, frozenset):
        return [_thaw_extra(item) for item in value]
    return deepcopy(value)


def _immutable_extras(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({key: _freeze_extra(item) for key, item in value.items()})


def _wire_extras(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _thaw_extra(item) for key, item in value.items()}


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    """A coding-agent session as shown by the terminal and browser panel."""

    agent: str = UNKNOWN
    session_id: str = UNKNOWN
    status: AgentStatus = AgentStatus.UNKNOWN
    root: str = ""
    pane_id: str = ""
    workspace_id: str = ""
    tab_id: str = ""
    working_root: str = ""
    label: str = ""
    model: str = ""
    effort: str = ""
    extras: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent", normalize_provider(self.agent))
        object.__setattr__(
            self, "session_id", str(self.session_id or "").strip() or UNKNOWN
        )
        object.__setattr__(self, "status", AgentStatus.from_wire(self.status))
        for name in (
            "root",
            "pane_id",
            "workspace_id",
            "tab_id",
            "working_root",
            "label",
            "model",
            "effort",
        ):
            object.__setattr__(self, name, str(getattr(self, name) or ""))
        object.__setattr__(self, "extras", _immutable_extras(self.extras))

    @property
    def key(self) -> SessionKey:
        return SessionKey(self.agent, self.session_id)

    @classmethod
    def from_wire(
        cls, wire: Mapping[str, Any], *, key: SessionKey | str | None = None
    ) -> AgentIdentity:
        if not isinstance(wire, Mapping):
            raise TypeError("agent identity must be a mapping")
        parsed_key = SessionKey.from_wire(key) if key is not None else None
        agent = wire.get("agent") or (parsed_key.provider if parsed_key else UNKNOWN)
        session_id = wire.get("session_id") or (
            parsed_key.session_id if parsed_key else UNKNOWN
        )
        return cls(
            agent=agent,
            session_id=session_id,
            status=AgentStatus.from_wire(wire.get("status")),
            root=wire.get("root", ""),
            pane_id=wire.get("pane_id", ""),
            workspace_id=wire.get("workspace_id", ""),
            tab_id=wire.get("tab_id", ""),
            working_root=wire.get("working_root", ""),
            label=wire.get("label", ""),
            model=wire.get("model", ""),
            effort=wire.get("effort", ""),
            extras=_extras(wire, cls),
        )

    def to_wire(self) -> dict[str, Any]:
        wire = _wire_extras(self.extras)
        wire.update(
            {
                "agent": self.agent,
                "session_id": self.session_id,
                "root": self.root,
                "pane_id": self.pane_id,
                "workspace_id": self.workspace_id,
                "tab_id": self.tab_id,
                "working_root": self.working_root,
                "status": self.status.value,
                "label": self.label,
                "model": self.model,
                "effort": self.effort,
            }
        )
        return wire


# This is deliberately an explicit, closed set.  New collector metadata must be
# reviewed here before it can cross the persistence boundary.
SAFE_EVENT_KINDS = frozenset(
    {
        "branch",
        "command",
        "commit",
        "config",
        "file",
        "github",
        "issue",
        "merge",
        "pr",
        "push",
        "search",
        "session",
        "test",
        "todo",
        "worktree",
    }
)
SAFE_EVENT_STATUSES = frozenset({"failed", "running", "success", UNKNOWN})
SAFE_EVENT_FIELDS = frozenset(
    {
        "schema",
        "timestamp",
        "epoch_ms",
        "agent",
        "project",
        "session_id",
        "kind",
        "status",
        "title",
        "detail",
        "operation_id",
        "group_id",
        "source_event_id",
        "turn_id",
        "model",
        "effort",
        "started_epoch_ms",
        "lines_added",
        "lines_removed",
        "url",
        "git_oid",
        "herdr_pane_id",
        "herdr_tab_id",
        "herdr_workspace_id",
        "github_state",
        "github_fingerprint",
        "github",
    }
)
# ``repeat_count`` and ``first_timestamp`` are panel-derived presentation
# values, not fields collectors are allowed to persist.
PANEL_SAFE_EVENT_FIELDS = frozenset(
    {
        "agent",
        "detail",
        "effort",
        "epoch_ms",
        "git_oid",
        "github",
        "group_id",
        "kind",
        "lines_added",
        "lines_removed",
        "model",
        "operation_id",
        "session_id",
        "started_epoch_ms",
        "status",
        "timestamp",
        "title",
        "turn_id",
        "url",
    }
)

_SAFE_GITHUB_FIELDS = frozenset(
    {
        "number",
        "url",
        "title",
        "state",
        "draft",
        "branch",
        "review",
        "merge_state",
        "mergeable",
        "ci",
        "checks_total",
        "checks_passed",
        "checks_pending",
        "checks_failed",
        "created_at",
        "updated_at",
        "closed_at",
        "merged_at",
        "coverage",
    }
)
_SAFE_TEXT_LIMITS = {
    "timestamp": 64,
    "project": 4096,
    "session_id": 512,
    "title": 256,
    "detail": 4096,
    "operation_id": 512,
    "group_id": 512,
    "source_event_id": 1024,
    "turn_id": 512,
    "model": 256,
    "effort": 128,
    "url": 2048,
    "git_oid": 128,
    "herdr_pane_id": 512,
    "herdr_tab_id": 512,
    "herdr_workspace_id": 512,
    "github_state": 64,
    "github_fingerprint": 512,
}


def _safe_text(value: Any, *, limit: int, field_name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    # Control characters can smuggle additional terminal/JSONL presentation.
    text = "".join(
        character
        for character in value[:limit]
        if character >= " " or character == "\t"
    )
    return text[:limit]


def _safe_optional_integer(value: Any, *, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    parsed = value
    if parsed < 0 or parsed > MAX_SAFE_INTEGER:
        raise ValueError(f"{field_name} is outside the safe integer range")
    return parsed


def _safe_http_url(value: Any, *, field_name: str) -> str:
    text = _safe_text(value, limit=2048, field_name=field_name)
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return ""
        hostname = parsed.hostname
        if ":" in hostname:
            hostname = f"[{hostname}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit(
            (parsed.scheme.casefold(), f"{hostname}{port}", parsed.path, "", "")
        )[:2048]
    except ValueError:
        return ""


def _safe_github_metadata(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("github must be a mapping")
    unknown = set(value) - _SAFE_GITHUB_FIELDS
    if unknown:
        raise ValueError("github contains unapproved fields")
    safe: dict[str, Any] = {}
    for key, item in value.items():
        if key in {
            "number",
            "checks_total",
            "checks_passed",
            "checks_pending",
            "checks_failed",
        }:
            safe[key] = _safe_optional_integer(item, field_name=f"github.{key}")
        elif key == "coverage" and isinstance(item, (int, float)):
            if (
                isinstance(item, bool)
                or isinstance(item, int)
                and abs(item) > MAX_SAFE_INTEGER
                or isinstance(item, float)
                and not isfinite(item)
            ):
                raise ValueError("github.coverage has an unsupported value")
            safe[key] = item
        elif isinstance(item, bool) or item is None:
            safe[key] = item
        elif key == "url":
            safe[key] = _safe_http_url(item, field_name="github.url")
        elif isinstance(item, str):
            safe[key] = _safe_text(item, limit=2048, field_name=f"github.{key}")
        else:
            raise ValueError(f"github.{key} has an unsupported value")
    return MappingProxyType(safe)


@dataclass(frozen=True, slots=True)
class SafeEvent:
    """The only event shape approved for local persistence and panel output."""

    schema: str = ACTIVITY_SCHEMA
    timestamp: str = ""
    epoch_ms: int = 0
    agent: str = UNKNOWN
    project: str = ""
    session_id: str | None = None
    kind: str = ""
    status: str = UNKNOWN
    title: str = ""
    detail: str = ""
    operation_id: str = ""
    group_id: str = ""
    source_event_id: str = ""
    turn_id: str = ""
    model: str = ""
    effort: str = ""
    started_epoch_ms: int | None = None
    lines_added: int | None = None
    lines_removed: int | None = None
    url: str = ""
    git_oid: str = ""
    herdr_pane_id: str = ""
    herdr_tab_id: str = ""
    herdr_workspace_id: str = ""
    github_state: str = ""
    github_fingerprint: str = ""
    github: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.schema != ACTIVITY_SCHEMA:
            raise ValueError("unsupported activity schema")
        agent = normalize_provider(self.agent)
        if agent == UNKNOWN and str(self.agent).strip().casefold() != UNKNOWN:
            raise ValueError("invalid agent provider")
        object.__setattr__(self, "agent", agent)
        if self.kind not in SAFE_EVENT_KINDS:
            raise ValueError("invalid event kind")
        if self.status not in SAFE_EVENT_STATUSES:
            raise ValueError("invalid event status")
        if isinstance(self.epoch_ms, bool) or not isinstance(self.epoch_ms, int):
            raise ValueError("epoch_ms must be an integer")
        epoch_ms = self.epoch_ms
        if epoch_ms < 0 or epoch_ms > MAX_SAFE_INTEGER:
            raise ValueError("epoch_ms is outside the safe integer range")
        object.__setattr__(self, "epoch_ms", epoch_ms)
        for name in ("started_epoch_ms", "lines_added", "lines_removed"):
            object.__setattr__(
                self,
                name,
                _safe_optional_integer(getattr(self, name), field_name=name),
            )
        for name, limit in _SAFE_TEXT_LIMITS.items():
            value = getattr(self, name)
            if name == "session_id" and value is None:
                continue
            object.__setattr__(
                self, name, _safe_text(value, limit=limit, field_name=name)
            )
        if self.session_id == "":
            object.__setattr__(self, "session_id", UNKNOWN)
        object.__setattr__(self, "url", _safe_http_url(self.url, field_name="url"))
        object.__setattr__(self, "github", _safe_github_metadata(self.github))

    @property
    def session_key(self) -> SessionKey:
        return SessionKey(self.agent, self.session_id)

    @classmethod
    def from_wire(cls, wire: Mapping[str, Any]) -> SafeEvent:
        if not isinstance(wire, Mapping):
            raise TypeError("safe event must be a mapping")
        if set(wire) - SAFE_EVENT_FIELDS:
            raise ValueError("safe event contains unapproved fields")
        return cls(**{key: wire[key] for key in SAFE_EVENT_FIELDS if key in wire})

    @classmethod
    def wire_fields(cls) -> frozenset[str]:
        return SAFE_EVENT_FIELDS

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "schema": self.schema,
            "timestamp": self.timestamp,
            "epoch_ms": self.epoch_ms,
            "agent": self.agent,
            "project": self.project,
            "kind": self.kind,
            "status": self.status,
            "title": self.title,
            "detail": self.detail,
        }
        optional = {
            name: getattr(self, name)
            for name in SAFE_EVENT_FIELDS
            if name not in wire and name != "schema"
        }
        wire.update(
            {
                key: _thaw_extra(value)
                for key, value in optional.items()
                if value not in ("", None)
            }
        )
        return wire


# Public compatibility names now share the same closed, privacy-safe shape.
NormalizedEvent = SafeEvent
ActivityEvent = SafeEvent


@dataclass(frozen=True, slots=True)
class StreamCheckpoint:
    """A durable position in one provider-qualified session stream."""

    session: SessionKey = field(default_factory=SessionKey)
    source: str = ""
    position: int = 0
    updated_epoch_ms: int | None = None
    extras: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "session", SessionKey.from_wire(self.session))
        object.__setattr__(self, "source", str(self.source or ""))
        object.__setattr__(self, "position", max(0, _integer(self.position, default=0)))
        if self.updated_epoch_ms is not None:
            object.__setattr__(
                self, "updated_epoch_ms", _integer(self.updated_epoch_ms)
            )
        object.__setattr__(self, "extras", _immutable_extras(self.extras))

    @classmethod
    def from_wire(cls, wire: Mapping[str, Any]) -> StreamCheckpoint:
        if not isinstance(wire, Mapping):
            raise TypeError("stream checkpoint must be a mapping")
        session_value = wire.get("session_key")
        if session_value is None:
            session_value = {
                "agent": wire.get("agent", wire.get("provider", UNKNOWN)),
                "session_id": wire.get("session_id", UNKNOWN),
            }
        return cls(
            session=SessionKey.from_wire(session_value),
            source=wire.get(
                "source", wire.get("transcript_path", wire.get("path", ""))
            ),
            position=wire.get("position", 0),
            updated_epoch_ms=wire.get("updated_epoch_ms"),
            extras=_extras(
                wire,
                cls,
                {
                    "session_key",
                    "agent",
                    "provider",
                    "session_id",
                    "transcript_path",
                    "path",
                },
            ),
        )

    def to_wire(self) -> dict[str, Any]:
        wire = _wire_extras(self.extras)
        wire.update(
            {
                "session_key": self.session.to_wire(),
                "agent": self.session.provider,
                "session_id": self.session.session_id,
                "source": self.source,
                "position": self.position,
            }
        )
        if self.updated_epoch_ms is not None:
            wire["updated_epoch_ms"] = self.updated_epoch_ms
        return wire


@dataclass(frozen=True, slots=True)
class AdapterHealth:
    """One integration adapter's latest non-fatal health report."""

    adapter: str = UNKNOWN
    status: AdapterHealthStatus = AdapterHealthStatus.UNKNOWN
    detail: str = ""
    checked_epoch_ms: int | None = None
    extras: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "adapter", normalize_provider(self.adapter))
        object.__setattr__(self, "status", AdapterHealthStatus.from_wire(self.status))
        object.__setattr__(self, "detail", str(self.detail or ""))
        if self.checked_epoch_ms is not None:
            object.__setattr__(
                self, "checked_epoch_ms", _integer(self.checked_epoch_ms)
            )
        object.__setattr__(self, "extras", _immutable_extras(self.extras))

    @classmethod
    def from_wire(cls, wire: Mapping[str, Any]) -> AdapterHealth:
        if not isinstance(wire, Mapping):
            raise TypeError("adapter health must be a mapping")
        return cls(
            adapter=wire.get("adapter", wire.get("agent", UNKNOWN)),
            status=AdapterHealthStatus.from_wire(wire.get("status")),
            detail=wire.get("detail", wire.get("error", "")),
            checked_epoch_ms=wire.get("checked_epoch_ms"),
            extras=_extras(wire, cls, {"agent", "error"}),
        )

    def to_wire(self) -> dict[str, Any]:
        wire = _wire_extras(self.extras)
        wire.update(
            {
                "adapter": self.adapter,
                "status": self.status.value,
                "detail": self.detail,
            }
        )
        if self.checked_epoch_ms is not None:
            wire["checked_epoch_ms"] = self.checked_epoch_ms
        return wire


class IntegrationCapability(StrEnum):
    """Features orchestration may safely ask an integration to provide."""

    COLLECTS_ACTIVITY = "collects_activity"
    DISCOVERS_SESSIONS = "discovers_sessions"
    PROJECT_HOOKS_FOR_ACTIVITY = "project_hooks_for_activity"
    REPORTS_EFFORT = "reports_effort"
    REPORTS_MODEL = "reports_model"
    REPORTS_SUBAGENTS = "reports_subagents"
    USES_COMMON_TOOL_NORMALIZER = "uses_common_tool_normalizer"


class EventSource(StrEnum):
    PROJECT_HOOKS = "project_hooks"
    SESSION_TRANSCRIPT = "session_transcript"
    SQLITE = "sqlite"
    LOCAL_SESSION_STORE = "local_session_store"


class SetupRequirement(StrEnum):
    NONE = "none"
    OPTIONAL_PROJECT_HOOKS = "optional_project_hooks"


@dataclass(frozen=True, slots=True)
class LazyCliCallable:
    """A cycle-safe reference to an integration callable."""

    symbol: str
    module: str = "side_dog.cli"

    def __post_init__(self) -> None:
        if not self.symbol or not self.symbol.isidentifier():
            raise ValueError(f"invalid Side Dog callable: {self.symbol!r}")
        if not self.module or any(
            not part.isidentifier() for part in self.module.split(".")
        ):
            raise ValueError(f"invalid Side Dog module: {self.module!r}")

    def resolve(self) -> Callable[..., Any]:
        value = getattr(import_module(self.module), self.symbol, None)
        if not callable(value):
            raise RuntimeError(
                f"Side Dog callable is unavailable: {self.module}.{self.symbol}"
            )
        return value

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.resolve()(*args, **kwargs)


@dataclass(frozen=True, slots=True)
class EnvironmentOverride:
    """A supported environment variable and the location it changes."""

    name: str
    purpose: str

    def __post_init__(self) -> None:
        name = str(self.name).strip()
        purpose = str(self.purpose).strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise ValueError(f"invalid environment override: {name!r}")
        if not purpose:
            raise ValueError("an environment override needs a purpose")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "purpose", purpose)


@dataclass(frozen=True, slots=True)
class IntegrationDescriptor:
    """Internal extension seam for one supported coding agent."""

    provider: str
    label: str
    product_name: str
    aliases: tuple[str, ...]
    capabilities: frozenset[IntegrationCapability]
    event_source: EventSource
    session_discovery_summary: str
    activity_source_summary: str
    identity_loader: LazyCliCallable
    metadata_loader: LazyCliCallable
    working_folders_loader: LazyCliCallable
    setup: SetupRequirement = SetupRequirement.NONE
    readiness_probe: LazyCliCallable | None = None
    environment_overrides: tuple[EnvironmentOverride, ...] = ()

    def __post_init__(self) -> None:
        provider = _provider_spelling(self.provider)
        if provider == UNKNOWN:
            raise ValueError("an integration needs a canonical provider")
        aliases = tuple(
            dict.fromkeys(_provider_spelling(alias) for alias in self.aliases)
        )
        if provider not in aliases or UNKNOWN in aliases:
            raise ValueError("integration aliases must include the canonical provider")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "label", str(self.label).strip())
        object.__setattr__(self, "product_name", str(self.product_name).strip())
        object.__setattr__(self, "aliases", aliases)
        object.__setattr__(
            self,
            "capabilities",
            frozenset(IntegrationCapability(value) for value in self.capabilities),
        )
        object.__setattr__(self, "event_source", EventSource(self.event_source))
        object.__setattr__(self, "setup", SetupRequirement(self.setup))
        object.__setattr__(
            self,
            "session_discovery_summary",
            str(self.session_discovery_summary).strip(),
        )
        object.__setattr__(
            self,
            "activity_source_summary",
            str(self.activity_source_summary).strip(),
        )
        object.__setattr__(
            self,
            "environment_overrides",
            tuple(
                EnvironmentOverride(item.name, item.purpose)
                for item in self.environment_overrides
            ),
        )
        if not self.label or not self.product_name:
            raise ValueError("an integration needs display and product names")
        if not self.session_discovery_summary or not self.activity_source_summary:
            raise ValueError("an integration needs user-facing support summaries")
        override_names = [item.name for item in self.environment_overrides]
        if len(override_names) != len(set(override_names)):
            raise ValueError("integration environment overrides must be unique")

    def supports(self, capability: IntegrationCapability | str) -> bool:
        return IntegrationCapability(capability) in self.capabilities


_COMMON_CAPABILITIES = frozenset(
    {
        IntegrationCapability.COLLECTS_ACTIVITY,
        IntegrationCapability.DISCOVERS_SESSIONS,
        IntegrationCapability.REPORTS_MODEL,
        IntegrationCapability.USES_COMMON_TOOL_NORMALIZER,
    }
)


def _cli(symbol: str) -> LazyCliCallable:
    return LazyCliCallable(symbol)


def _doctor(symbol: str) -> LazyCliCallable:
    return LazyCliCallable(symbol, module="side_dog.doctor")


INTEGRATIONS = (
    IntegrationDescriptor(
        provider="codex",
        label="Codex",
        product_name="Codex",
        aliases=("codex",),
        capabilities=_COMMON_CAPABILITIES
        | {
            IntegrationCapability.REPORTS_EFFORT,
            IntegrationCapability.REPORTS_SUBAGENTS,
        },
        event_source=EventSource.SESSION_TRANSCRIPT,
        session_discovery_summary="Yes, including terminal and Codex Desktop sessions",
        activity_source_summary="Yes, from Codex's local session stream",
        identity_loader=_cli("load_codex_session_identities"),
        metadata_loader=_cli("load_codex_metadata"),
        working_folders_loader=_cli("codex_working_folders"),
        readiness_probe=_doctor("codex_readiness"),
        environment_overrides=(
            EnvironmentOverride("CODEX_HOME", "Codex data directory"),
        ),
    ),
    IntegrationDescriptor(
        provider="claude-code",
        label="Claude",
        product_name="Claude Code",
        aliases=("claude", "claude-code"),
        capabilities=_COMMON_CAPABILITIES
        | {
            IntegrationCapability.PROJECT_HOOKS_FOR_ACTIVITY,
            IntegrationCapability.REPORTS_EFFORT,
        },
        event_source=EventSource.PROJECT_HOOKS,
        session_discovery_summary="Yes, including terminal, desktop, and editor sessions",
        activity_source_summary="Yes, after project hooks are installed",
        identity_loader=_cli("claude_identities"),
        metadata_loader=_cli("load_claude_metadata"),
        working_folders_loader=_cli("claude_working_folders"),
        setup=SetupRequirement.OPTIONAL_PROJECT_HOOKS,
        readiness_probe=_doctor("claude_readiness"),
    ),
    IntegrationDescriptor(
        provider="pi",
        label="Pi",
        product_name="Pi",
        aliases=("pi",),
        capabilities=_COMMON_CAPABILITIES | {IntegrationCapability.REPORTS_EFFORT},
        event_source=EventSource.SESSION_TRANSCRIPT,
        session_discovery_summary="Yes",
        activity_source_summary="Yes, from Pi's local session files",
        identity_loader=_cli("load_pi_session_identities"),
        metadata_loader=_cli("load_pi_metadata"),
        working_folders_loader=_cli("pi_working_folders"),
        readiness_probe=_doctor("pi_readiness"),
        environment_overrides=(
            EnvironmentOverride("PI_CODING_AGENT_DIR", "Pi agent data directory"),
        ),
    ),
    IntegrationDescriptor(
        provider="opencode",
        label="Opencode",
        product_name="OpenCode",
        aliases=("opencode",),
        capabilities=_COMMON_CAPABILITIES
        | {
            IntegrationCapability.REPORTS_EFFORT,
            IntegrationCapability.REPORTS_SUBAGENTS,
        },
        event_source=EventSource.SQLITE,
        session_discovery_summary="Yes",
        activity_source_summary="Yes, from OpenCode's local SQLite store",
        identity_loader=_cli("opencode_identities"),
        metadata_loader=_cli("load_opencode_metadata"),
        working_folders_loader=_cli("opencode_working_folders"),
        readiness_probe=_doctor("opencode_readiness"),
        environment_overrides=(
            EnvironmentOverride("XDG_DATA_HOME", "OpenCode data directory parent"),
        ),
    ),
    IntegrationDescriptor(
        provider="cursor",
        label="Cursor",
        product_name="Cursor Agent",
        aliases=("cursor", "cursor-agent"),
        capabilities=_COMMON_CAPABILITIES | {IntegrationCapability.REPORTS_EFFORT},
        event_source=EventSource.SQLITE,
        session_discovery_summary="Yes, when launched through T3 Code",
        activity_source_summary="Yes, from T3 Code's projected activity store",
        identity_loader=_cli("cursor_identities"),
        metadata_loader=_cli("load_cursor_metadata"),
        working_folders_loader=_cli("cursor_working_folders"),
        readiness_probe=_doctor("cursor_readiness"),
        environment_overrides=(
            EnvironmentOverride("T3CODE_HOME", "T3 Code data directory"),
        ),
    ),
    IntegrationDescriptor(
        provider="grok",
        label="Grok",
        product_name="Grok Build",
        aliases=("grok", "grok-build"),
        capabilities=_COMMON_CAPABILITIES | {IntegrationCapability.REPORTS_EFFORT},
        event_source=EventSource.SQLITE,
        session_discovery_summary="Yes, when launched through T3 Code",
        activity_source_summary="Yes, from T3 Code's projected activity store",
        identity_loader=_cli("grok_identities"),
        metadata_loader=_cli("load_grok_metadata"),
        working_folders_loader=_cli("grok_working_folders"),
        readiness_probe=_doctor("grok_readiness"),
        environment_overrides=(
            EnvironmentOverride("T3CODE_HOME", "T3 Code data directory"),
        ),
    ),
    IntegrationDescriptor(
        provider="deepseek",
        label="DeepSeek",
        product_name="DeepSeek Harness",
        aliases=("deepseek", "deepseek-harness", "dsh"),
        capabilities=_COMMON_CAPABILITIES
        | {
            IntegrationCapability.REPORTS_EFFORT,
            IntegrationCapability.REPORTS_SUBAGENTS,
        },
        event_source=EventSource.SESSION_TRANSCRIPT,
        session_discovery_summary="Yes",
        activity_source_summary="Yes, from Harness session logs",
        identity_loader=_cli("load_deepseek_session_identities"),
        metadata_loader=_cli("load_deepseek_metadata"),
        working_folders_loader=_cli("deepseek_working_folders"),
        readiness_probe=_doctor("deepseek_readiness"),
        environment_overrides=(
            EnvironmentOverride("DSH_HOME", "DeepSeek Harness data directory"),
        ),
    ),
    IntegrationDescriptor(
        provider="cline",
        label="Cline",
        product_name="Cline",
        aliases=("cline",),
        capabilities=_COMMON_CAPABILITIES | {IntegrationCapability.REPORTS_SUBAGENTS},
        event_source=EventSource.LOCAL_SESSION_STORE,
        session_discovery_summary="Yes, across CLI, editor, desktop, and background sessions",
        activity_source_summary="Yes, from Cline's local session store",
        identity_loader=_cli("cline_identities"),
        metadata_loader=_cli("load_cline_metadata"),
        working_folders_loader=_cli("cline_working_folders"),
        readiness_probe=_doctor("cline_session_sources"),
        environment_overrides=(
            EnvironmentOverride("CLINE_DIR", "Cline home directory"),
            EnvironmentOverride("CLINE_DATA_DIR", "Cline data directory"),
            EnvironmentOverride("CLINE_DB_DATA_DIR", "Cline database directory"),
            EnvironmentOverride(
                "CLINE_SESSION_DATA_DIR", "Cline file-backed session directory"
            ),
        ),
    ),
    IntegrationDescriptor(
        provider="antigravity",
        label="Antigravity",
        product_name="Antigravity CLI",
        aliases=("antigravity", "antigravity-cli", "agy"),
        capabilities=_COMMON_CAPABILITIES
        | {
            IntegrationCapability.REPORTS_EFFORT,
            IntegrationCapability.REPORTS_SUBAGENTS,
        },
        event_source=EventSource.SESSION_TRANSCRIPT,
        session_discovery_summary="Yes",
        activity_source_summary="Yes, from Antigravity's local history and transcripts",
        identity_loader=_cli("load_antigravity_session_identities"),
        metadata_loader=_cli("load_antigravity_metadata"),
        working_folders_loader=_cli("antigravity_working_folders"),
        readiness_probe=_doctor("antigravity_readiness"),
        environment_overrides=(
            EnvironmentOverride(
                "ANTIGRAVITY_APP_DATA_DIR", "Antigravity application data directory"
            ),
            EnvironmentOverride("GEMINI_HOME", "Gemini data directory parent"),
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class ContextProviderDescriptor:
    """Optional context that enriches coding-agent sessions without being one."""

    key: str
    product_name: str
    session_discovery_summary: str
    activity_source_summary: str
    setup_summary: str
    optional: bool = True

    def __post_init__(self) -> None:
        key = _provider_spelling(self.key)
        values = {
            "product_name": str(self.product_name).strip(),
            "session_discovery_summary": str(self.session_discovery_summary).strip(),
            "activity_source_summary": str(self.activity_source_summary).strip(),
            "setup_summary": str(self.setup_summary).strip(),
        }
        if key == UNKNOWN or not all(values.values()):
            raise ValueError("a context provider needs a key and support metadata")
        object.__setattr__(self, "key", key)
        for name, value in values.items():
            object.__setattr__(self, name, value)


HERDR_CONTEXT = ContextProviderDescriptor(
    key="herdr",
    product_name="Herdr",
    session_discovery_summary="Adds pane, tab, workspace, and terminal-title details",
    activity_source_summary="Routes activity to the right terminal context",
    setup_summary="Optional",
)
T3CODE_CONTEXT = ContextProviderDescriptor(
    key="t3code",
    product_name="T3 Code",
    session_discovery_summary="Adds thread title, provider, status, and worktree details",
    activity_source_summary="Supplies projected activity for Cursor and Grok Build",
    setup_summary="Optional; no Side Dog hooks",
)
CONTEXT_PROVIDERS = (HERDR_CONTEXT, T3CODE_CONTEXT)


def _build_registry(
    descriptors: tuple[IntegrationDescriptor, ...],
) -> tuple[Mapping[str, IntegrationDescriptor], Mapping[str, IntegrationDescriptor]]:
    providers: dict[str, IntegrationDescriptor] = {}
    aliases: dict[str, IntegrationDescriptor] = {}
    for descriptor in descriptors:
        if descriptor.provider in providers:
            raise ValueError(f"duplicate integration provider: {descriptor.provider}")
        providers[descriptor.provider] = descriptor
        for alias in descriptor.aliases:
            if alias in aliases:
                raise ValueError(f"duplicate integration alias: {alias}")
            aliases[alias] = descriptor
    return MappingProxyType(providers), MappingProxyType(aliases)


INTEGRATION_REGISTRY, INTEGRATION_ALIASES = _build_registry(INTEGRATIONS)
CODING_AGENT_PROVIDERS = frozenset(INTEGRATION_REGISTRY)


def integration_for(value: Any) -> IntegrationDescriptor | None:
    """Look up a registered agent without inventing a fallback descriptor."""
    return INTEGRATION_ALIASES.get(normalize_provider(value))


def _integer(value: Any, *, default: int | None = None) -> int:
    if isinstance(value, bool):
        if default is not None:
            return default
        raise ValueError("boolean is not an integer")
    try:
        return int(value)
    except (OverflowError, TypeError, ValueError):
        if default is not None:
            return default
        raise ValueError(f"expected an integer, got {value!r}") from None


def identity_from_wire(
    wire: Mapping[str, Any], *, key: SessionKey | str | None = None
) -> AgentIdentity:
    return AgentIdentity.from_wire(wire, key=key)


def identity_to_wire(identity: AgentIdentity) -> dict[str, Any]:
    return identity.to_wire()


def event_from_wire(wire: Mapping[str, Any]) -> NormalizedEvent:
    return NormalizedEvent.from_wire(wire)


def event_to_wire(event: NormalizedEvent) -> dict[str, Any]:
    return event.to_wire()


__all__ = [
    "ACTIVITY_SCHEMA",
    "UNKNOWN",
    "ActivityEvent",
    "AdapterHealth",
    "AdapterHealthStatus",
    "AgentIdentity",
    "AgentStatus",
    "CODING_AGENT_PROVIDERS",
    "CONTEXT_PROVIDERS",
    "ContextProviderDescriptor",
    "EnvironmentOverride",
    "EventSource",
    "HERDR_CONTEXT",
    "INTEGRATIONS",
    "INTEGRATION_ALIASES",
    "INTEGRATION_REGISTRY",
    "IntegrationCapability",
    "IntegrationDescriptor",
    "LazyCliCallable",
    "MAX_SAFE_INTEGER",
    "NormalizedEvent",
    "PANEL_SAFE_EVENT_FIELDS",
    "SAFE_EVENT_FIELDS",
    "SAFE_EVENT_KINDS",
    "SAFE_EVENT_STATUSES",
    "SafeEvent",
    "SessionKey",
    "SetupRequirement",
    "StreamCheckpoint",
    "T3CODE_CONTEXT",
    "event_from_wire",
    "event_to_wire",
    "identity_from_wire",
    "identity_to_wire",
    "integration_for",
    "normalize_provider",
]
