"""Execution boundary for trusted and future sandboxed agent code.

User/agent workspace Python is data, never an import source for the web server.
The Docker implementation will satisfy this protocol in a later phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence


@dataclass(frozen=True)
class RunnerLimits:
    timeout_seconds: int = 30
    memory_mb: int = 256
    cpu_count: float = 1.0
    process_count: int = 32


@dataclass(frozen=True)
class ExecutionRequest:
    actor_user_id: str
    subject_kind: str
    subject_id: str
    agent_id: str
    entrypoint: str
    arguments: Sequence[str] = ()
    environment: Mapping[str, str] = field(default_factory=dict)
    readonly_mounts: Sequence[str] = ()
    writable_mount: Optional[str] = None
    network_policy: str = "none"
    limits: RunnerLimits = field(default_factory=RunnerLimits)


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)


class CodeRunner(Protocol):
    async def execute(self, request: ExecutionRequest) -> ExecutionResult: ...


def assert_trusted_in_process_path(path: str | Path) -> Path:
    """Permit bundled app/plugin code and reject every runtime data directory."""
    from app.runtime_mode import data_root

    candidate = Path(path).resolve()
    runtime_root = data_root().resolve()
    try:
        candidate.relative_to(runtime_root)
    except ValueError:
        return candidate
    raise PermissionError("Workspace Python cannot execute in the WebAgent process")


class DockerSandboxRunner:
    """Reserved implementation point; deliberately unavailable in phase one."""

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        raise RuntimeError("Docker sandbox execution is not enabled")

