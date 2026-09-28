"""
Output Summarizer — LEGACY re-export shim.

The close-out loop (summary + checklist audit + send-back) lives in
``app/agent/output_closer.py``; this module remains as a thin alias so any
external import of ``app.agent.output_summarizer`` keeps working. New code
should import from ``app.agent.output_closer``.
"""

from app.agent.output_closer import *  # noqa: F401,F403
from app.agent import output_closer as _closer

# Explicit aliases for symbols renamed in the closer module.
run_output_summarizer = _closer.run_output_closer
_load_summarizer_prompt = _closer._load_closer_prompt
_attempt_summary_call = _closer._attempt_closer_call
_stamp_summary_attempt = _closer._stamp_closer_attempt
_FALLBACK_SUMMARIZER_PROMPT = _closer._FALLBACK_CLOSER_PROMPT
