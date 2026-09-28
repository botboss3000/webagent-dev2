"""Task Progress Knowledge ability shell.

The runtime policy lives in :mod:`app.agent.task_progress_config` because task
state and history assembly are core loop concerns.  This drop-in descriptor
places the policy in the Knowledge ability group and supplies its agent-facing
configuration surface without registering a second runtime or configuration
store. The active-task prompt carries the binding, task-specific operating instruction.
The sibling always-on skill supplies durable task-authoring guidance whenever the
ability is enabled.
"""

from __future__ import annotations

# Task Progress is intentionally configuration-only. Its checkpoint utilities
# remain always-on Core tools, while this ability exposes the per-agent policy.
