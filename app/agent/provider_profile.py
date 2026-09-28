"""Declared behaviour of one (provider, transport, model) endpoint.

The loop makes three decisions before every request, and taking any of them from
a vendor *name* is what produced a multi-hour outage:

  1. What shape is a replayed reasoning item, and is replaying it required?
  2. May a forced ``tool_choice`` coexist with thinking on this transport?
  3. Is the request we just built one the endpoint will accept?

This module answers those as data.  Transport selection itself stays in
``response_chaining.response_chain_capability`` — that already distinguishes "the
endpoint cannot do this" from "we do not know", and it owns the request URL and
auth shape.

Why a profile instead of vendor branches
----------------------------------------
DeepSeek alone appears twice here: over ``/chat/completions`` reasoning travels as
``reasoning_content`` and a forced tool choice is fine, while over ``/responses``
it travels as ``reasoning_text`` and a forced choice cannot coexist with thinking.
A vendor-keyed design has to encode that split somewhere else — which is how one
half of the rule (disable thinking when forcing) ended up contradicting the other
half (an assistant turn produced without reasoning cannot be replayed).  The facts
here are keyed on what the endpoint does, so a vendor simply resolves to one or
more profiles.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Mapping, Optional

#: Assistant turns produced with reasoning must carry it back on the next request.
REPLAY_REQUIRED = "required"
#: The endpoint rejects replayed reasoning outright.
REPLAY_FORBIDDEN = "forbidden"
#: Reasoning may be replayed, and may equally be dropped.
REPLAY_OPTIONAL = "optional"

SHAPE_ENCRYPTED = "encrypted_content"
SHAPE_PLAIN = "reasoning_text"
SHAPE_CHAT = "reasoning_content"
SHAPE_NONE = "none"


@dataclass(frozen=True)
class ProviderProfile:
    """What one endpoint does, independent of who operates it."""

    family: str
    transport: str
    stateful: bool
    reasoning_shape: str = SHAPE_NONE
    replay_obligation: str = REPLAY_OPTIONAL
    #: False when the endpoint refuses a forced tool_choice while thinking is on.
    forced_tool_choice_with_reasoning: bool = True
    #: True when omitting the reasoning field means "think", not "do not think".
    thinking_by_default: bool = True
    #: True when a thinking-mode request is rejected for *any* assistant turn that
    #: lacks its reasoning, not only the ones carrying tool calls. Established on
    #: DeepSeek /responses by elimination: rewriting an unreplayable tool turn as
    #: prose was rejected again, which is only explicable if plain assistant turns
    #: are checked too.
    every_assistant_turn_needs_reasoning: bool = False

    @property
    def requires_replay(self) -> bool:
        return self.replay_obligation == REPLAY_REQUIRED

    @property
    def is_responses(self) -> bool:
        return self.transport == "responses"

    def describe(self) -> str:
        return (
            "family=%s transport=%s shape=%s replay=%s force_with_thinking=%s"
            % (self.family, self.transport, self.reasoning_shape,
               self.replay_obligation, self.forced_tool_choice_with_reasoning)
        )


def resolve_profile(
    *,
    family: str = "",
    transport: str = "",
    stateful: bool = False,
    provider: str = "",
    base_url: str = "",
    model: str = "",
) -> ProviderProfile:
    """Resolve the active endpoint's profile.

    ``family`` comes from the transport capability when it is known; otherwise the
    provider id is used.  ``base_url``/``model`` are accepted so a future probe can
    refine the answer without touching every call site.
    """
    fam = str(family or provider or "").strip().lower()
    transport = str(transport or "chat_completions").strip().lower()
    if transport != "responses":
        # Chat Completions: reasoning rides along as ``reasoning_content`` — but
        # only DeepSeek requires its own thinking to travel back with the turn it
        # belongs to; every other OpenAI-compatible endpoint rejects the field.
        # Replay is never *demanded* here, and a forced tool choice is unaffected.
        shape = SHAPE_CHAT if fam == "deepseek" else SHAPE_NONE
        return ProviderProfile(
            fam, transport, bool(stateful), shape, REPLAY_OPTIONAL, True, True,
        )
    if fam == "deepseek":
        # Official DeepSeek /responses: stateless full resend, plain
        # ``reasoning_text``, and a hard rule that an assistant turn produced with
        # thinking carries that thinking back. It also refuses a forced
        # tool_choice while thinking is on, so on this profile the force gives way.
        return ProviderProfile(
            fam, transport, bool(stateful), SHAPE_PLAIN, REPLAY_REQUIRED, False, True,
            every_assistant_turn_needs_reasoning=True,
        )
    if fam == "openrouter":
        # Responses SHAPE, statelessly: the router's schema admits
        # ``previous_response_id`` as null only, so nothing is retained and the
        # server never verifies a replayed reasoning item. Encrypted items are
        # still sent when we hold them, but replay is not an obligation — which
        # also keeps the pre-send check from rewriting history for a rule this
        # endpoint does not enforce.
        return ProviderProfile(
            fam, transport, bool(stateful), SHAPE_ENCRYPTED, REPLAY_OPTIONAL, True, True,
        )
    if fam in {"openai", "azure_openai", "xai"}:
        # Encrypted reasoning items; the server verifies them on replay, so a
        # replayed turn has to carry the original blob.
        return ProviderProfile(
            fam, transport, bool(stateful), SHAPE_ENCRYPTED, REPLAY_REQUIRED, True, True,
        )
    return ProviderProfile(
        fam, transport, bool(stateful), SHAPE_NONE, REPLAY_OPTIONAL, True, True,
    )


def forced_tool_choice_allowed(
    profile: ProviderProfile, *, model_supports: bool,
) -> bool:
    """Whether an enforced ``tool_choice`` may be requested for this call.

    ``model_supports`` is the catalog-derived answer ("is this model known to be
    reasoning-capable?").  The profile narrows it further: on an endpoint that
    cannot combine thinking with a forced choice it is declined outright, so the
    work policy falls back to its nudge instead.

    This is deliberately the *only* place the decision is made. The alternative —
    allow the force and compensate by switching thinking off — is what left an
    assistant turn with no reasoning for the endpoint to reject on the very next
    call.
    """
    if not model_supports:
        return False
    if profile.requires_replay and not profile.forced_tool_choice_with_reasoning:
        return False
    return True


def should_degrade_replay_to_chat(
    profile: ProviderProfile, stats: Mapping[str, Any], *, family: str,
) -> bool:
    """Whether a replay-required endpoint should degrade to Chat Completions.

    A replay-required endpoint can accept a history holding turns that carry none
    of the reasoning it demands back only by *dropping* those turns — and the drop
    pass can empty the transcript outright, which the send path then refuses.  The
    same family's Chat Completions transport declares replay ``optional``, so it
    accepts the history WHOLE.  When the pre-send repair reports it had to drop
    turns for such an endpoint, the caller should switch transport instead of
    losing them.

    Scoped to DeepSeek rather than assumed general: it is the one family here with
    both an ``/responses`` transport that demands replay and a Chat Completions
    transport that does not.
    """
    if not profile.requires_replay:
        return False
    if str(family or "").strip().lower() != "deepseek":
        return False
    return int((stats or {}).get("unreplayable_turns_dropped", 0) or 0) > 0


def count_replay_items(body: Mapping[str, Any]) -> dict:
    """Count the replay-relevant items in a Responses request body."""
    reasoning = 0
    calls = 0
    for item in body.get("input") or []:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("type") or "")
        if kind == "reasoning":
            reasoning += 1
        elif kind == "function_call":
            calls += 1
    return {"reasoning_items": reasoning, "function_call_items": calls}


def request_uses_thinking(body: Mapping[str, Any], profile: ProviderProfile) -> bool:
    """Whether this request puts the model in thinking mode.

    Matters because the replay rule is scoped to thinking mode: a caller who has
    deliberately turned thinking off should not have their tool history rewritten
    by the replay check.
    """
    reasoning = body.get("reasoning") or {}
    effort = ""
    if isinstance(reasoning, Mapping):
        effort = str(reasoning.get("effort") or "").strip().lower()
    if effort == "none":
        return False
    if effort:
        return True
    return bool(profile.thinking_by_default)


def _no_reasoning_at_all(reasoning_items: int, call_items: int) -> bool:
    """Coarse body-level proxy: calls replayed with no reasoning anywhere.

    The authoritative rule is per turn (see ``history_replay_violations``), because
    the flat body loses the grouping that says which reasoning belongs to which
    call. This stays as an assembly sanity net: it cannot be *stricter* than the
    per-turn rule, so it never blocks a request the per-turn rule allows.
    """
    return bool(call_items and not reasoning_items)


def responses_replay_violations(
    body: Mapping[str, Any], profile: ProviderProfile,
) -> List[str]:
    """Ways this built body violates the endpoint's replay rule, if any.

    Runs before the request is sent.  A body that replays tool calls with no
    reasoning at all is one the endpoint has already told us it refuses, so
    finding it locally costs a rebuild instead of a round-trip, a crash, and
    (before this existed) three identical resumes.
    """
    if not profile.requires_replay or not profile.is_responses:
        return []
    if not request_uses_thinking(body, profile):
        return []
    counts = count_replay_items(body)
    if not _no_reasoning_at_all(counts["reasoning_items"], counts["function_call_items"]):
        return []
    return [
        "%d function_call item(s) replayed with no reasoning item anywhere, and "
        "this request is in thinking mode on %s"
        % (counts["function_call_items"], profile.describe())
    ]


def reasoning_item_shape(item: Any) -> str:
    """The shape one Responses reasoning item actually carries.

    An item's ``type`` is always ``"reasoning"`` on this transport — the shape
    lives in the payload, and the shapes are not interchangeable. An
    ``encrypted_content`` blob is only valid on the endpoint that issued it (the
    server verifies the blob against its own key), while plain ``reasoning_text``
    is what a plain-thinking endpoint requires back. Counting items by type alone
    therefore credits a wrong-shape turn as replayable, replays it verbatim, and
    collects "reasoning_text ... must be passed back to the API" as a 400.
    """
    if not isinstance(item, Mapping):
        return SHAPE_NONE
    if str(item.get("type") or "") != "reasoning":
        return SHAPE_NONE
    if item.get("encrypted_content"):
        return SHAPE_ENCRYPTED
    content = item.get("content")
    if isinstance(content, (list, tuple)):
        for part in content:
            if (isinstance(part, Mapping)
                    and str(part.get("type") or "") == SHAPE_PLAIN
                    and str(part.get("text") or "").strip()):
                return SHAPE_PLAIN
    return SHAPE_NONE


def profile_accepts_shape(profile: ProviderProfile, shape: str) -> bool:
    """Whether reasoning of ``shape`` is replayable to the endpoint ``profile`` names."""
    if shape == SHAPE_NONE:
        return False
    want = profile.reasoning_shape
    if want == SHAPE_PLAIN:
        # A plain-thinking endpoint needs ``reasoning_text`` back. A turn whose
        # reasoning survived only under the Chat Completions field name counts
        # too: the body builder synthesizes the plain item from it (see
        # ``response_chaining.responses_input``). An encrypted blob does NOT —
        # this endpoint never issued it and cannot verify it.
        return shape in (SHAPE_PLAIN, SHAPE_CHAT)
    if want == SHAPE_ENCRYPTED:
        # The server verifies the blob, so only the exact encrypted item counts.
        return shape == SHAPE_ENCRYPTED
    return True


def message_reasoning_count(
    message: Mapping[str, Any], profile: Optional[ProviderProfile] = None,
) -> int:
    """Replayable reasoning carried by one assistant message.

    With ``profile`` the count is shape-aware: only reasoning of the shape the
    target endpoint names is counted, so a turn carrying the wrong shape is
    reported as lacking reasoning (and repaired by dropping it) instead of
    passing the check and being replayed into a deterministic 400.
    """
    count = 0
    for item in message.get("_responses_reasoning") or []:
        shape = reasoning_item_shape(item)
        if shape == SHAPE_NONE:
            continue
        if profile is not None and not profile_accepts_shape(profile, shape):
            continue
        count += 1
    if not count and str(message.get("reasoning_content") or "").strip():
        if profile is None or profile_accepts_shape(profile, SHAPE_CHAT):
            count = 1
    return count


def history_replay_violations(
    messages: Any, profile: ProviderProfile,
) -> List[str]:
    """The authoritative replay rule, applied to a message list.

    Per turn, not per request: an assistant turn that replays tool calls must
    carry the reasoning that produced it. The first version of this check only
    asked whether the request carried reasoning *anywhere*, and a live session
    proved that wrong — a request with 250 reasoning items and 63 function calls
    was still rejected because some of those turns had none of their own.

    This is the rule the migration checks itself against, and the one the pre-send
    gate uses, because the message list is the only place where the grouping of
    reasoning to calls is still explicit.
    """
    if not profile.requires_replay or not profile.is_responses:
        return []
    unpaired = 0
    calls = 0
    for message in messages or []:
        if not isinstance(message, Mapping):
            continue
        if str(message.get("role") or "") != "assistant":
            continue
        tool_calls = message.get("tool_calls") or []
        call_count = len(tool_calls) if isinstance(tool_calls, (list, tuple)) else 0
        calls += call_count
        if profile.every_assistant_turn_needs_reasoning and not tool_calls:
            # Every turn is checked, so a plain assistant message counts too.
            pass
        elif not call_count:
            continue
        if not message_reasoning_count(message, profile):
            unpaired += 1
    if not unpaired:
        return []
    return [
        "%d assistant turn(s) (%d replayed tool call(s)) lack the reasoning that "
        "produced them, on %s" % (unpaired, calls, profile.describe())
    ]
