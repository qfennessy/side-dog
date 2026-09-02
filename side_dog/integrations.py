"""Typed values shared by coding-agent integrations.

The collectors still speak dictionaries at their I/O boundaries.  These small
objects make the values inside that boundary explicit while keeping the
``side-dog-activity-v1`` JSONL and current identity dictionaries compatible.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, fields
from enum import StrEnum
from importlib import import_module
from types import MappingProxyType
from typing import Any, Callable


ACTIVITY_SCHEMA = "side-dog-activity-v1"
UNKNOWN = "unknown"

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


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    """Privacy-filtered event data at the integration/wire boundary."""

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
    extras: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        schema = str(self.schema or ACTIVITY_SCHEMA)
        if schema != ACTIVITY_SCHEMA:
            raise ValueError(f"unsupported activity schema: {schema}")
        object.__setattr__(self, "schema", schema)
        object.__setattr__(self, "agent", normalize_provider(self.agent))
        if self.session_id is not None:
            object.__setattr__(
                self, "session_id", str(self.session_id or "").strip() or UNKNOWN
            )
        object.__setattr__(self, "epoch_ms", _integer(self.epoch_ms))
        for name in ("started_epoch_ms", "lines_added", "lines_removed"):
            value = getattr(self, name)
            object.__setattr__(self, name, None if value is None else _integer(value))
        for name in (
            "timestamp",
            "project",
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
            "url",
            "git_oid",
        ):
            text = str(getattr(self, name) or "")
            if name == "status" and not text:
                text = UNKNOWN
            object.__setattr__(self, name, text)
        object.__setattr__(self, "extras", _immutable_extras(self.extras))

    @property
    def session_key(self) -> SessionKey:
        return SessionKey(self.agent, self.session_id)

    @classmethod
    def from_wire(cls, wire: Mapping[str, Any]) -> NormalizedEvent:
        if not isinstance(wire, Mapping):
            raise TypeError("normalized event must be a mapping")
        return cls(
            schema=wire.get("schema", ACTIVITY_SCHEMA),
            timestamp=wire.get("timestamp", ""),
            epoch_ms=wire.get("epoch_ms", 0),
            agent=wire.get("agent", UNKNOWN),
            project=wire.get("project", ""),
            session_id=wire.get("session_id") if "session_id" in wire else None,
            kind=wire.get("kind", ""),
            status=wire.get("status", UNKNOWN),
            title=wire.get("title", ""),
            detail=wire.get("detail", ""),
            operation_id=wire.get("operation_id", ""),
            group_id=wire.get("group_id", ""),
            source_event_id=wire.get("source_event_id", ""),
            turn_id=wire.get("turn_id", ""),
            model=wire.get("model", ""),
            effort=wire.get("effort", ""),
            started_epoch_ms=wire.get("started_epoch_ms"),
            lines_added=wire.get("lines_added"),
            lines_removed=wire.get("lines_removed"),
            url=wire.get("url", ""),
            git_oid=wire.get("git_oid", ""),
            extras=_extras(wire, cls),
        )

    def to_wire(self) -> dict[str, Any]:
        wire = _wire_extras(self.extras)
        wire.update(
            {
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
        )
        optional = {
            "session_id": self.session_id,
            "operation_id": self.operation_id,
            "group_id": self.group_id,
            "source_event_id": self.source_event_id,
            "turn_id": self.turn_id,
            "model": self.model,
            "effort": self.effort,
            "started_epoch_ms": self.started_epoch_ms,
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
            "url": self.url,
            "git_oid": self.git_oid,
        }
        wire.update(
            {key: value for key, value in optional.items() if value not in ("", None)}
        )
        return wire


ActivityEvent = NormalizedEvent


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
    """A cycle-safe reference to a codec or probe that still lives in cli.py."""

    symbol: str

    def __post_init__(self) -> None:
        if not self.symbol or not self.symbol.isidentifier():
            raise ValueError(f"invalid Side Dog CLI callable: {self.symbol!r}")

    def resolve(self) -> Callable[..., Any]:
        value = getattr(import_module("side_dog.cli"), self.symbol, None)
        if not callable(value):
            raise RuntimeError(f"Side Dog CLI callable is unavailable: {self.symbol}")
        return value

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.resolve()(*args, **kwargs)


@dataclass(frozen=True, slots=True)
class IntegrationDescriptor:
    """Internal extension seam for one supported coding agent."""

    provider: str
    label: str
    aliases: tuple[str, ...]
    capabilities: frozenset[IntegrationCapability]
    event_source: EventSource
    identity_loader: LazyCliCallable
    metadata_loader: LazyCliCallable
    working_folders_loader: LazyCliCallable
    setup: SetupRequirement = SetupRequirement.NONE
    readiness_probe: LazyCliCallable | None = None

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
        object.__setattr__(self, "aliases", aliases)
        object.__setattr__(
            self,
            "capabilities",
            frozenset(IntegrationCapability(value) for value in self.capabilities),
        )
        object.__setattr__(self, "event_source", EventSource(self.event_source))
        object.__setattr__(self, "setup", SetupRequirement(self.setup))
        if not self.label:
            raise ValueError("an integration needs a display label")

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


INTEGRATIONS = (
    IntegrationDescriptor(
        provider="codex",
        label="Codex",
        aliases=("codex",),
        capabilities=_COMMON_CAPABILITIES
        | {
            IntegrationCapability.REPORTS_EFFORT,
            IntegrationCapability.REPORTS_SUBAGENTS,
        },
        event_source=EventSource.SESSION_TRANSCRIPT,
        identity_loader=_cli("load_codex_session_identities"),
        metadata_loader=_cli("load_codex_metadata"),
        working_folders_loader=_cli("codex_working_folders"),
        readiness_probe=_cli("codex_sessions_root"),
    ),
    IntegrationDescriptor(
        provider="claude-code",
        label="Claude",
        aliases=("claude", "claude-code"),
        capabilities=_COMMON_CAPABILITIES
        | {
            IntegrationCapability.PROJECT_HOOKS_FOR_ACTIVITY,
            IntegrationCapability.REPORTS_EFFORT,
        },
        event_source=EventSource.PROJECT_HOOKS,
        identity_loader=_cli("claude_identities"),
        metadata_loader=_cli("load_claude_metadata"),
        working_folders_loader=_cli("claude_working_folders"),
        setup=SetupRequirement.OPTIONAL_PROJECT_HOOKS,
        readiness_probe=_cli("claude_session_registry"),
    ),
    IntegrationDescriptor(
        provider="pi",
        label="Pi",
        aliases=("pi",),
        capabilities=_COMMON_CAPABILITIES | {IntegrationCapability.REPORTS_EFFORT},
        event_source=EventSource.SESSION_TRANSCRIPT,
        identity_loader=_cli("load_pi_session_identities"),
        metadata_loader=_cli("load_pi_metadata"),
        working_folders_loader=_cli("pi_working_folders"),
        readiness_probe=_cli("pi_sessions_root"),
    ),
    IntegrationDescriptor(
        provider="opencode",
        label="Opencode",
        aliases=("opencode",),
        capabilities=_COMMON_CAPABILITIES
        | {
            IntegrationCapability.REPORTS_EFFORT,
            IntegrationCapability.REPORTS_SUBAGENTS,
        },
        event_source=EventSource.SQLITE,
        identity_loader=_cli("opencode_identities"),
        metadata_loader=_cli("load_opencode_metadata"),
        working_folders_loader=_cli("opencode_working_folders"),
        readiness_probe=_cli("opencode_db_path"),
    ),
    IntegrationDescriptor(
        provider="deepseek",
        label="DeepSeek",
        aliases=("deepseek", "deepseek-harness", "dsh"),
        capabilities=_COMMON_CAPABILITIES
        | {
            IntegrationCapability.REPORTS_EFFORT,
            IntegrationCapability.REPORTS_SUBAGENTS,
        },
        event_source=EventSource.SESSION_TRANSCRIPT,
        identity_loader=_cli("load_deepseek_session_identities"),
        metadata_loader=_cli("load_deepseek_metadata"),
        working_folders_loader=_cli("deepseek_working_folders"),
        readiness_probe=_cli("dsh_sessions_root"),
    ),
    IntegrationDescriptor(
        provider="cline",
        label="Cline",
        aliases=("cline",),
        capabilities=_COMMON_CAPABILITIES | {IntegrationCapability.REPORTS_SUBAGENTS},
        event_source=EventSource.LOCAL_SESSION_STORE,
        identity_loader=_cli("cline_identities"),
        metadata_loader=_cli("load_cline_metadata"),
        working_folders_loader=_cli("cline_working_folders"),
        readiness_probe=_cli("cline_session_sources"),
    ),
    IntegrationDescriptor(
        provider="antigravity",
        label="Antigravity",
        aliases=("antigravity", "antigravity-cli", "agy"),
        capabilities=_COMMON_CAPABILITIES
        | {
            IntegrationCapability.REPORTS_EFFORT,
            IntegrationCapability.REPORTS_SUBAGENTS,
        },
        event_source=EventSource.SESSION_TRANSCRIPT,
        identity_loader=_cli("load_antigravity_session_identities"),
        metadata_loader=_cli("load_antigravity_metadata"),
        working_folders_loader=_cli("antigravity_working_folders"),
        readiness_probe=_cli("antigravity_sessions_roots"),
    ),
)


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
    "EventSource",
    "INTEGRATIONS",
    "INTEGRATION_ALIASES",
    "INTEGRATION_REGISTRY",
    "IntegrationCapability",
    "IntegrationDescriptor",
    "LazyCliCallable",
    "NormalizedEvent",
    "SessionKey",
    "SetupRequirement",
    "StreamCheckpoint",
    "event_from_wire",
    "event_to_wire",
    "identity_from_wire",
    "identity_to_wire",
    "integration_for",
    "normalize_provider",
]
