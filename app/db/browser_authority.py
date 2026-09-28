"""Fail-closed DB facade for browser-authority agent turns.

Browser authority owns session transcripts and per-session runtime state in
IndexedDB. The server may read shared configuration and account-plane data, but
must not persist browser-owned session rows as an accidental side effect of the
normal agent loop.

This class intentionally has no ``__getattr__`` passthrough. Every operation the
agent loop is allowed to perform is enumerated below. Adding a new DB call to the
loop therefore fails loudly in browser mode until it is classified here.
"""

from __future__ import annotations

import copy
import sqlite3
import uuid
from typing import Any, Dict, Iterable, Optional


class BrowserAuthorityViolation(RuntimeError):
    """Raised when browser-owned state is addressed with the wrong identity."""


class BrowserAuthorityDB:
    """Read shared configuration, keep session writes ephemeral, fail closed."""

    authority_mode = "browser"

    def __init__(self, read_db: Any, *, user_id: str, session_id: str) -> None:
        self._read_db = read_db
        self.user_id = user_id
        self.session_id = session_id
        self._interactions: Dict[str, Dict[str, Any]] = {}
        self._active_tools: set[str] = set()
        self._active_abilities: set[str] = set()
        self._active_skills: set[str] = set()
        self._turn_count = 0
        # Task Progress is browser-owned during this request. Keep its relational
        # state in a per-facade shared-memory database: the normal task-state
        # helpers may open and close short-lived connections without ever touching
        # a server session store.
        self._task_state_uri = f"file:browser-task-{uuid.uuid4().hex}?mode=memory&cache=shared"
        self._task_state_anchor = sqlite3.connect(
            self._task_state_uri, uri=True, check_same_thread=False
        )
        self._task_state_anchor.row_factory = sqlite3.Row

    def open_task_state_connection(self) -> sqlite3.Connection:
        """Open ephemeral, browser-owned storage for Task Progress only."""
        if self._task_state_anchor is None:
            raise BrowserAuthorityViolation("browser task-state facade is closed")
        connection = sqlite3.connect(
            self._task_state_uri, uri=True, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        return connection

    def close(self) -> None:
        """Release the facade's per-turn task-state anchor exactly once."""
        anchor = self._task_state_anchor
        self._task_state_anchor = None
        if anchor is not None:
            anchor.close()

    def _assert_scope(
        self, user_id: Optional[str] = None, session_id: Optional[str] = None
    ) -> None:
        if user_id is not None and user_id != self.user_id:
            raise BrowserAuthorityViolation("browser authority user mismatch")
        if session_id is not None and session_id != self.session_id:
            raise BrowserAuthorityViolation("browser authority session mismatch")

    async def _read(self, method: str, *args: Any, **kwargs: Any) -> Any:
        target = getattr(self._read_db, method, None)
        if target is None:
            raise BrowserAuthorityViolation(
                f"shared configuration read is not available: {method}"
            )
        return await target(*args, **kwargs)

    # Shared configuration and account-plane reads.
    async def get_agent_by_id(self, *args: Any, **kwargs: Any) -> Any:
        return await self._read("get_agent_by_id", *args, **kwargs)

    async def get_agent_for_user(self, *args: Any, **kwargs: Any) -> Any:
        return await self._read("get_agent_for_user", *args, **kwargs)

    async def get_agent_connections(self, *args: Any, **kwargs: Any) -> Any:
        return await self._read("get_agent_connections", *args, **kwargs)

    async def get_agent_roles(self, *args: Any, **kwargs: Any) -> Any:
        return await self._read("get_agent_roles", *args, **kwargs)

    async def auth_element_get(self, *args: Any, **kwargs: Any) -> Any:
        return await self._read("auth_element_get", *args, **kwargs)

    async def system_prompt_fragments(self, *args: Any, **kwargs: Any) -> Any:
        return await self._read("system_prompt_fragments", *args, **kwargs)

    async def get_agent_tool_modes(self, *args: Any, **kwargs: Any) -> Any:
        return await self._read("get_agent_tool_modes", *args, **kwargs)

    async def get_agent_ability_modes(self, *args: Any, **kwargs: Any) -> Any:
        return await self._read("get_agent_ability_modes", *args, **kwargs)

    async def get_agent_discovery_default(self, *args: Any, **kwargs: Any) -> Any:
        return await self._read("get_agent_discovery_default", *args, **kwargs)

    async def list_agent_templates(self, *args: Any, **kwargs: Any) -> Any:
        return await self._read("list_agent_templates", *args, **kwargs)

    async def assemble_prompt(self, *args: Any, **kwargs: Any) -> Any:
        return await self._read("assemble_prompt", *args, **kwargs)

    async def skill_get_id_by_name(self, *args: Any, **kwargs: Any) -> Any:
        return await self._read("skill_get_id_by_name", *args, **kwargs)

    async def skill_get_rating(self, *args: Any, **kwargs: Any) -> Any:
        return await self._read("skill_get_rating", *args, **kwargs)

    # Browser-owned session reads. These never consult server session tables.
    async def is_session_dead(self, session_id: str) -> bool:
        self._assert_scope(session_id=session_id)
        return False

    async def get_session(self, user_id: str, session_id: str) -> dict:
        self._assert_scope(user_id, session_id)
        return {
            "id": self.session_id,
            "user_id": self.user_id,
            "status": "active",
            "metadata": "{}",
        }

    async def fetch_interactions(self, user_id: str, session_id: str) -> list:
        self._assert_scope(user_id, session_id)
        return [copy.deepcopy(row) for row in self._interactions.values()]

    async def get_session_active_tools(self, session_id: str) -> list:
        self._assert_scope(session_id=session_id)
        return sorted(self._active_tools)

    async def get_session_active_abilities(self, session_id: str) -> list:
        self._assert_scope(session_id=session_id)
        return sorted(self._active_abilities)

    async def get_session_suppressed_abilities(
        self, session_id: str
    ) -> list:
        self._assert_scope(session_id=session_id)
        return []

    async def get_session_file_checkout_enabled(self, session_id: str) -> bool:
        self._assert_scope(session_id=session_id)
        return True

    async def set_session_file_checkout_enabled(self, session_id: str, enabled: bool) -> bool:
        self._assert_scope(session_id=session_id)
        return bool(enabled)

    async def get_session_execution_mode(self, session_id: str) -> None:
        self._assert_scope(session_id=session_id)
        return None

    async def get_session_execution_mode_history(self, session_id: str) -> list:
        self._assert_scope(session_id=session_id)
        return []

    async def get_session_context_override(self, session_id: str) -> None:
        self._assert_scope(session_id=session_id)
        return None

    async def get_session_segments(self, user_id: str, session_id: str) -> list:
        self._assert_scope(user_id, session_id)
        return []

    async def get_session_summary(self, user_id: str, session_id: str) -> None:
        self._assert_scope(user_id, session_id)
        return None

    # Transcript writes are captured in memory only. The SSE consumer commits
    # the authoritative records to IndexedDB.
    async def insert_interaction(
        self,
        user_id: str,
        session_id: str,
        *,
        role: str,
        content: str,
        **kwargs: Any,
    ) -> str:
        self._assert_scope(user_id, session_id)
        interaction_id = str(kwargs.get("interaction_id") or uuid.uuid4())
        self._interactions[interaction_id] = {
            "id": interaction_id,
            "user_id": user_id,
            "session_id": session_id,
            "role": role,
            "content": content,
            **kwargs,
        }
        return interaction_id

    async def insert_interactions_batch(self, rows: Iterable[dict]) -> list[str]:
        ids: list[str] = []
        for row in rows:
            item = dict(row)
            user_id = item.pop("user_id", self.user_id)
            session_id = item.pop("session_id", self.session_id)
            role = item.pop("role")
            content = item.pop("content")
            ids.append(await self.insert_interaction(
                user_id, session_id, role=role, content=content, **item
            ))
        return ids

    async def update_interaction(
        self, interaction_id: str, **changes: Any
    ) -> bool:
        row = self._interactions.get(interaction_id)
        if row is None:
            return False
        row.update(changes)
        return True

    async def update_interaction_content(
        self, interaction_id: str, content: str
    ) -> bool:
        return await self.update_interaction(interaction_id, content=content)

    def export_interactions(self) -> list[dict]:
        """Return normalized ephemeral rows for history-cache construction."""
        rows: list[dict] = []
        for source in self._interactions.values():
            row = copy.deepcopy(source)
            output = row.get("output_data")
            if isinstance(output, str):
                try:
                    import json
                    output = json.loads(output)
                except Exception:
                    output = None
            if isinstance(output, dict) and output.get("tool_calls"):
                row["tool_calls"] = output["tool_calls"]
            rows.append(row)
        return rows

    # Session/run bookkeeping remains ephemeral.
    async def set_session_active_ability(
        self, session_id: str, ability_id: str, active: bool
    ) -> list[str]:
        self._assert_scope(session_id=session_id)
        if active:
            self._active_abilities.add(ability_id)
        else:
            self._active_abilities.discard(ability_id)
        return sorted(self._active_abilities)

    async def set_session_active_skill(
        self, session_id: str, skill_id: str, active: bool
    ) -> list[str]:
        self._assert_scope(session_id=session_id)
        if active:
            self._active_skills.add(skill_id)
        else:
            self._active_skills.discard(skill_id)
        return sorted(self._active_skills)

    async def run_state_heartbeat(
        self, session_id: str, *args: Any, **kwargs: Any
    ) -> None:
        self._assert_scope(session_id=session_id)
        return None

    async def run_state_set_assistant(
        self, session_id: str, *args: Any, **kwargs: Any
    ) -> None:
        self._assert_scope(session_id=session_id)
        return None

    async def increment_agent_turn_count(self, *args: Any, **kwargs: Any) -> int:
        self._turn_count += 1
        return self._turn_count

    async def bind_session_to_agent(
        self, session_id: str, agent_id: str
    ) -> None:
        self._assert_scope(session_id=session_id)
        return None

    async def skill_track_execution(
        self,
        skill_id: str,
        user_id: str,
        session_id: str,
        success: bool,
        duration_ms: int,
        **kwargs: Any,
    ) -> str:
        self._assert_scope(user_id, session_id)
        return f"browser-skill-{uuid.uuid4().hex[:12]}"

    async def check_interrupt(self, session_id: str) -> bool:
        self._assert_scope(session_id=session_id)
        return False

    async def clear_interrupt(self, session_id: str) -> None:
        self._assert_scope(session_id=session_id)
        return None

    async def replace_session_segments(self, *args: Any, **kwargs: Any) -> None:
        raise BrowserAuthorityViolation(
            "server compaction cannot write browser-authority transcript state"
        )
