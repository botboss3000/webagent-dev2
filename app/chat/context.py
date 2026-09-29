"""Task-local execution authority, inherited by helpers and checked before publication."""
from contextvars import ContextVar
current = ContextVar("controlled_chat_execution", default=None)
