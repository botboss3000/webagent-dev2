"""The public chat contract. Execution events are deliberately a different type."""
from enum import StrEnum
from typing import Any, Literal
import json
from pydantic import BaseModel, ConfigDict, Field, field_validator

class State(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    FINALIZING = "finalizing"
    STOPPING = "stopping"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"

TERMINAL = frozenset({State.COMPLETED, State.CANCELLED, State.FAILED})
KINDS = frozenset({"user", "answer", "notice", "failure", "attachment", "component"})
SAFE_FAILURE = "I couldn't produce a validated final response. You can retry the final response without repeating the work."

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

class Send(StrictModel):
    client_message_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=100000)
    display_text: str | None = Field(default=None, max_length=100000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=50)
    execution_mode: Literal["ask", "plan", "auto", "wkspc"] = "ask"
    app_control: dict[str, Any] | None = None

    @field_validator("app_control")
    @classmethod
    def validate_app_control(cls, value):
        if value is None:
            return None
        allowed={"intent","label","descriptor","region","page","selector",
                 "styles","html","x","y","text","mode","directions","centre","radius"}
        if set(value)-allowed or len(json.dumps(value,ensure_ascii=False))>4096:
            raise ValueError("Invalid app control fingerprint")
        return value

class SessionCreate(StrictModel):
    id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    agent_id: str = Field(min_length=1, max_length=128)
    title: str = Field(default="New Session", max_length=200)

class PublicItem(StrictModel):
    id: str
    session_id: str
    run_id: str | None
    position: int
    revision: int
    schema_version: int = 1
    kind: Literal["user", "answer", "notice", "failure", "attachment", "component"]
    payload: dict[str, Any]
    created_at: str
    deleted: bool = False
    terminal: bool

class Conflict(Exception):
    pass

class Fenced(Conflict):
    pass

class NotFound(Exception):
    pass
