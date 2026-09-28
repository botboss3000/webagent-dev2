"""One flag vocabulary across the pill, the dropdown and the Sessions page.

Locks two things:

1. the SHARED resolver's behaviour (ui/shared/js/session-flag.js) — closer
   wins, runtime is the fallback, a flag may only escalate severity, and the
   acknowledgement gate is driven by the DB-backed ``has_unread``;
2. the wiring of every consumer surface, so the bubble stops rendering the
   envelope while the two list surfaces read the resolver.

The behavioural half needs Node; it is skipped when Node is unavailable (this
container has no JS runtime, so the source assertions carry the regression).
"""

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]

SESSIONS_PAGE = ROOT / "ui/main-panel/agents/sessions/js/sessions-page.js"
DROPDOWN_LIST = ROOT / "ui/chat/elements/session-dropdown/list.js"
DROPDOWN_CONTROLLER = ROOT / "ui/chat/elements/session-dropdown/controller.js"
CHAT_BUBBLE = ROOT / "ui/chat/js/chat-bubble.js"
CLOSING_MESSAGE = ROOT / "ui/chat/js/closing-message.js"
SESSION_FLAG = ROOT / "ui/shared/js/session-flag.js"
NEEDS_INPUT_CHIP = ROOT / "ui/shared/js/needs-input-chip.js"
APP_CSS = ROOT / "ui/shared/css/app1.css"
SESSIONS_CSS = ROOT / "ui/main-panel/agents/sessions/sessions.css"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_resolver_prefers_closer_and_never_softens_runtime():
    script = textwrap.dedent(
        """
        const path = await import('node:path');
        const { pathToFileURL } = await import('node:url');
        const mod = await import(pathToFileURL(
          path.resolve('ui/shared/js/session-flag.js'),
        ).href);
        console.log(JSON.stringify({
          closerWins: mod.resolveSessionFlag(
            {run_status: 'complete', closing_flag: 'partial'}),
          runtimeFallback: mod.resolveSessionFlag(
            {run_status: 'complete', closing_flag: null}),
          planFallback: mod.resolveSessionFlag(
            {run_status: 'complete', execution_mode: 'plan'}),
          neverSoften: mod.resolveSessionFlag(
            {run_status: 'error', closing_flag: 'complete'}),
          unknownCloserAgainstComplete: mod.resolveSessionFlag(
            {run_status: 'complete', closing_flag: 'frobnicated'}),
          ackUnread: mod.sessionAckFlag(
            {run_status: 'complete', closing_flag: 'partial', has_unread: true}),
          ackRead: mod.sessionAckFlag(
            {run_status: 'complete', closing_flag: 'partial', has_unread: false}),
          ackRunning: mod.sessionAckFlag({run_status: 'running', has_unread: true}),
          ackQueued: mod.sessionAckFlag({run_status: 'queued', has_unread: true}),
          ackError: mod.sessionAckFlag({run_status: 'error', has_unread: true}),
          label: mod.flagLabel('needs_input'),
          toneSuccess: mod.flagTone('complete'),
          toneWarning: mod.flagTone('partial'),
          toneDanger: mod.flagTone('error'),
          toneNeutral: mod.flagTone('interrupted'),
          cta: mod.closingCta({closing_cta: '  ship it  '}),
        }));
        """
    )
    completed = subprocess.run(
        ["node", "--input-type=module", "-"],
        cwd=ROOT,
        input=script,
        text=True,
        capture_output=True,
        check=True,
    )
    result = json.loads(completed.stdout)

    # The closer's own verdict wins whenever the session carries one.
    assert result["closerWins"] == "partial"
    # Runtime status is the fallback, and a completed Plan turn is needs_input.
    assert result["runtimeFallback"] == "complete"
    assert result["planFallback"] == "needs_input"
    # A flag may only ESCALATE — a stray complete cannot repaint an error green.
    assert result["neverSoften"] == "error"
    assert result["unknownCloserAgainstComplete"] == "frobnicated"
    # The acknowledgement gate: unread settled flags only, never a live state.
    assert result["ackUnread"] == "partial"
    assert result["ackRead"] == ""
    assert result["ackRunning"] == ""
    assert result["ackQueued"] == ""
    assert result["ackError"] == "error"
    # Labels, tones and the closer's note.
    assert result["label"] == "Needs input"
    assert result["toneSuccess"] == "success"
    assert result["toneWarning"] == "warning"
    assert result["toneDanger"] == "danger"
    assert result["toneNeutral"] == "neutral"
    assert result["cta"] == "ship it"


def test_sessions_page_reads_the_shared_resolver_and_enriches_closer_fields():
    source = _read(SESSIONS_PAGE)

    assert "shared/js/session-flag.js" in source
    # The chip's LIFETIME is sessionAttentionFlag's, not the unread-only
    # sessionAckFlag: a clean completion drops its chip once read, while every
    # other settled flag keeps a neutral grey chip so a read-but-unfinished
    # session stays visibly unfinished.
    assert "sessionAttentionFlag(row)" in source
    assert "sessionFlagIsRead(row)" in source
    assert "flagLabel(ackFlag)" in source
    assert "flagTone(ackFlag)" in source
    # The chip carries its restore label + verb + tone hooks for the shared module.
    assert "data-needs-input-label" in source
    assert "data-needs-input-verb" in source
    assert "flag-tone-" in source
    # Both enrichment sites copy the closer's own fields off /sessions.
    assert "closing_flag: session.closing_flag" in source
    assert "closing_flag: metadata.closing_flag" in source
    # The old run_status-derived placeholder predicate is gone.
    assert "_sessionNeedsInput" not in source
    assert '>Needs input</button>' not in source


def test_dropdown_reads_the_shared_resolver_and_drops_the_constant_label():
    source = _read(DROPDOWN_LIST)

    assert "shared/js/session-flag.js" in source
    # The ROW chip shares the Sessions page's lifetime predicate, so clicking a
    # flag cannot hide it on one list surface and keep it on the other.
    assert "sessionAttentionFlag(s)" in source
    assert "sessionFlagIsRead(s)" in source
    # The COLLAPSED TITLE BAR is the current session's own ROW, collapsed: same
    # predicate, same tone/verb/closer-note inputs, so a flag that reads in the
    # open menu reads in the bar too. The unread-only sessionAckFlag used to
    # drive this element, which left the title bar blank for exactly the
    # read-but-unfinished session the menu still flagged.
    assert "sessionAttentionFlag(found)" in source
    assert "sessionFlagIsRead(found)" in source
    assert "closingCta(found)" in source
    assert "flagLabel(ackFlag)" in source
    assert "data-needs-input-label" in source
    assert "data-needs-input-verb" in source
    # ...and it renders a real chip BUTTON in the bar, framed by the status span.
    assert "session-needs-input-chip flag-tone-" in source
    assert "session-dropdown-status-chip" in source
    assert "sessionAckFlag(" not in source
    assert "_sessionNeedsInput" not in source


def test_dropdown_chip_click_honours_the_chip_verb():
    source = _read(DROPDOWN_CONTROLLER)

    # The chip's own verb decides the write: a coloured (unread) chip marks the
    # session read, a grey (read) chip puts it back to unread — which restores
    # the coloured flag AND the notification row it drives.
    assert "dataset.needsInputVerb === 'unread'" in source
    assert "markSessionUnread(needsInputChip, sid)" in source
    assert "markSessionRead(needsInputChip, sid)" in source
    # The COLLAPSED TITLE BAR's chip runs that same cycle through the same shared
    # module, consumed in the trigger handler BEFORE the menu toggle: a flag click
    # is the special condition that acts on the session, while every other click
    # in the bar still opens/closes the menu.
    assert "const titleChip = e.target.closest('.session-needs-input-chip');" in source
    assert "markSessionUnread(titleChip, chipSid)" in source
    assert "markSessionRead(titleChip, chipSid)" in source
    _chip_at = source.index("const titleChip = e.target.closest")
    assert _chip_at < source.index("openMenu();", _chip_at)
    # ...and the chip is excluded from the long-press rename gesture, which would
    # otherwise swallow a press-and-hold on the flag.
    assert ".header-delete-btn, .header-plus-btn, .session-dropdown-status, .session-needs-input-chip" in source


def test_title_bar_chip_answers_the_keyboard_like_the_menu_chip():
    source = _read(DROPDOWN_CONTROLLER)

    # The collapsed bar's chip is a real BUTTON, and the wrapper's Enter/Space
    # handler decides whether that button ever sees its own activation: while the
    # chip was missing from this guard, a keyboard confirm opened the menu
    # instead of advancing the flag. The title bar's chip must answer Enter/Space
    # exactly as the menu's chip does — and that menu listener intercepts only
    # the search input, so leaving it aside is what makes the two surfaces
    # identical for a user who never touches the mouse.
    assert "#session-dropdown-kebab, #session-dropdown-delete, .session-row-title-input, .session-needs-input-chip" in source
    assert "const inp = e.target.closest('.session-manage-search-input');" in source


def test_sessions_page_chip_click_honours_the_chip_verb():
    source = _read(SESSIONS_PAGE)

    # The SAME verb contract as the chat dropdown, on the same chip: the coloured
    # flag chip marks the session read (dropping a `complete` chip and greying
    # every other flag), the grey read-but-unfinished chip puts it back to unread
    # so the coloured flag returns. The handler used to write "read" whatever the
    # chip carried, so a grey chip could never restore its flag on this surface.
    assert "dataset.needsInputVerb === 'unread'" in source
    assert "await _setSessionReadState(sid, !toUnread);" in source


def test_bubble_strips_the_envelope_without_rendering_the_badge():
    bubble = _read(CHAT_BUBBLE)
    closer = _read(CLOSING_MESSAGE)

    # The strip stays (the content must keep the envelope for the backend).
    assert "parseClosingEnvelope" in bubble
    assert "parseClosingEnvelope" in closer
    # ...but the badge/note render path is retired everywhere.
    assert "applyClosingPresentation" not in bubble
    assert "applyClosingPresentation" not in closer
    assert "closingFlagLabel" not in closer


def test_dead_bubble_footer_css_is_removed_and_tone_classes_remain():
    css = _read(APP_CSS)

    assert ".closing-message-footer" not in css
    assert ".closing-flag-complete" not in css
    assert ".closing-cta" not in css
    # The chip's per-flag tones stay — including the tone that was missing before.
    assert ".session-needs-input-chip.flag-tone-success" in css
    assert ".session-needs-input-chip.flag-tone-danger" in css
    assert ".session-needs-input-chip.flag-tone-info" in css
    assert ".session-needs-input-chip.flag-tone-neutral" in css
    # The title bar's status span is now only the FRAME around a real chip
    # button, so it carries no tone of its own — tone selectors on it would be
    # dead colour, and were the tell that the bar rendered plain text while the
    # menu rendered a chip.
    assert ".session-dropdown-status.session-dropdown-status-chip" in css
    assert ".session-dropdown-status.flag-tone-" not in css


def test_title_bar_chip_shows_every_flag_tone_the_row_chip_shows():
    css = _read(APP_CSS)

    # Five tones, not four: the warning family (needs_input / partial /
    # unverified) is the base state of the chip and must be scoped under
    # #chat-header as well, or it is the one flag that falls back to the mute
    # grey in the title bar while the row chip renders it amber.
    for tone in ("warning", "success", "danger", "info", "neutral"):
        assert f"#chat-header .session-needs-input-chip.flag-tone-{tone}" in css
    # Both TRANSIENT stages — the closer's note, then the armed confirm — keep a
    # stronger wash on every tone, so a half-finished click reads as a stage
    # rather than as the resting flag.
    for tone in ("warning", "success", "danger", "info", "neutral"):
        assert f"#chat-header .session-needs-input-chip.flag-tone-{tone}.noting" in css
        assert f"#chat-header .session-needs-input-chip.flag-tone-{tone}.armed" in css
    # A note is a sentence: capped and ellipsized in the bar as well, so it
    # cannot stretch the title row.
    assert "max-width: 190px" in css


def test_title_bar_flag_and_its_counter_carry_the_flag_tone():
    css = _read(APP_CSS)
    sessions_css = _read(SESSIONS_CSS)
    sessions_page = _read(SESSIONS_PAGE)

    # The chat title bar's flag is an inline BUTTON inside #chat-header, and the
    # generic `#chat-header button` rule carries an ID, so it outranks the chip's
    # tone classes — without these scoped rules every flag there rendered in the
    # mute grey whatever its flag was.
    assert "#chat-header .session-needs-input-chip.flag-tone-success" in css
    assert "#chat-header .session-needs-input-chip.flag-tone-danger" in css
    assert "#chat-header .session-needs-input-chip.flag-tone-info" in css
    assert "#chat-header .session-needs-input-chip.flag-tone-neutral" in css
    # The counter beside the flag takes the SAME tone as the chip, on both list
    # surfaces, instead of counting down amber whichever flag is shown.
    assert ".session-needs-input-counter.flag-tone-success" in css
    assert ".session-needs-input-counter.flag-tone-danger" in css
    assert ".session-needs-input-counter.flag-tone-neutral" in css
    assert ".sess-title-time-wrap.needs-input-counter.flag-tone-success .sess-title-time" in sessions_css
    assert ".sess-title-time-wrap.needs-input-counter.flag-tone-neutral .sess-title-time" in sessions_css
    # Both surfaces stamp the counter from the same resolved flag/tone as the chip.
    assert "flag-tone-${ackTone}" in _read(DROPDOWN_LIST)
    assert "flagTone(ackFlag)" in sessions_page


def test_title_bar_renders_the_rows_live_counter_beside_the_flag():
    source = _read(DROPDOWN_LIST)
    css = _read(APP_CSS)

    # The collapsed title bar is the current session's own ROW, collapsed — so it
    # carries the row's flag+counter PAIRING, not the chip alone: the same
    # activity timestamp, the same [data-ts] counter and the same group wrapper.
    # Without the counter the bar read "Partial" where its own menu row read
    # "Partial 42s" — the flag showed but its recency did not.
    assert 'statusEl.innerHTML = `<span class="session-row-status-group">' in source
    assert "</button>${needsInputCounter}</span>`" in source
    assert "const needsInputAt = found.activity_at || found.updated_at;" in source
    assert "session-needs-input-counter flag-tone-" in source
    # The counter is a plain [data-ts] span, and the 1s tick already refreshes
    # exactly this element, so the bar's recency counts up live like the row's.
    assert "#session-dropdown-status [data-ts]" in source
    # The menu's group carries a -6px row-alignment nudge; the bar's group sits
    # flush inside its own container, whose 6px gap is the label→flag spacing.
    assert "#session-dropdown-status .session-row-status-group" in css


def test_shared_chip_restores_the_caller_rendered_label():
    source = _read(NEEDS_INPUT_CHIP)

    assert "dataset.needsInputLabel" in source


def test_shared_chip_cycles_flag_note_then_armed_with_two_second_resets():
    source = _read(NEEDS_INPUT_CHIP)

    # Three stages: the flag, the closer's own note, then the confirmation.
    assert "const STAGE_IDLE = 'idle';" in source
    assert "const STAGE_NOTE = 'note';" in source
    assert "const STAGE_ARMED = 'armed';" in source
    # The note rides on the chip like the restore label and the verb, so a
    # re-render cannot lose it mid-interaction.
    assert "dataset.needsInputNote" in source
    # Both TRANSIENT stages (note and armed) share the 2s auto-reset to the flag.
    assert "const STAGE_TIMEOUT_MS = 2000;" in source
    assert "function _armStageTimeout" in source
    assert source.count("_armStageTimeout(button);") == 2
    # The cycle order: idle → note, note → armed, armed → confirm.
    idle = source.index("if (stage === STAGE_IDLE)")
    note = source.index("if (stage === STAGE_NOTE)")
    armed = source.index("button.dataset.needsInputPending = '1';")
    assert idle < note < armed
    # A session with no closer note still arms on the first click (the old
    # two-click contract), so the note stage never becomes a dead end.
    assert "else _arm(button);" in source
    # The module never derives the note itself — it is handed over on the chip's
    # dataset by the callers, so this stays presentation-only.
    assert "function _note(button)" in source
    assert "_note(button)" in source
    assert "import " not in source


def test_both_list_surfaces_stamp_the_closers_note_on_the_chip():
    page = _read(SESSIONS_PAGE)
    listing = _read(DROPDOWN_LIST)

    for source in (page, listing):
        assert "closingCta" in source
        assert "data-needs-input-note" in source
    # The note shown on the first click IS the closer's own line — the same one
    # the pop-up notification carries — not a locally re-derived guess.
    assert "closingCta(row)" in page
    assert "closingCta(s)" in listing


def test_chip_note_stage_has_its_own_style_on_every_tone():
    css = _read(APP_CSS)

    # A note is a sentence, not a flag label, so it is capped and ellipsized
    # rather than allowed to stretch the title cell.
    assert ".session-needs-input-chip.noting" in css
    assert "text-overflow: ellipsis" in css
    # Every tone's hover/armed background also covers the note stage, so the
    # chip does not drop back to the base amber while it shows the note.
    for tone in ("warning", "success", "danger", "info", "neutral"):
        prefix = ".session-needs-input-chip" if tone == "warning" else f".session-needs-input-chip.flag-tone-{tone}"
        assert f"{prefix}.noting" in css or f"{prefix}:hover,\n{prefix}.armed,\n{prefix}.noting" in css


def test_title_bar_flag_and_counter_carry_the_flag_tone():
    css = _read(APP_CSS)
    sessions_css = _read(SESSIONS_CSS)

    # The chat title bar's flag is a BUTTON inside #chat-header, whose generic
    # `#chat-header button` rule carries an ID and so outranks the tone classes —
    # without these scoped rules every flag up there rendered mute grey whatever
    # the session's flag was.
    assert "#chat-header .session-needs-input-chip.flag-tone-success" in css
    assert "#chat-header .session-needs-input-chip.flag-tone-danger" in css
    assert "#chat-header .session-needs-input-chip.flag-tone-info" in css
    assert "#chat-header .session-needs-input-chip.flag-tone-neutral" in css
    # The counter beside the flag takes the SAME tone as the chip on both list
    # surfaces, so the pair reads as one flag instead of always counting amber.
    assert ".session-needs-input-counter.flag-tone-success" in css
    assert ".session-needs-input-counter.flag-tone-danger" in css
    assert ".session-needs-input-counter.flag-tone-neutral" in css
    assert ".sess-title-time-wrap.needs-input-counter.flag-tone-success .sess-title-time" in sessions_css
    assert ".sess-title-time-wrap.needs-input-counter.flag-tone-danger .sess-title-time" in sessions_css
    assert ".sess-title-time-wrap.needs-input-counter.flag-tone-neutral .sess-title-time" in sessions_css
    # …and both surfaces stamp that tone from the resolver, not from a run status.
    assert "flag-tone-${ackTone}" in _read(DROPDOWN_LIST)
    assert "flag-tone-' + _esc(ackRead ? 'neutral' : flagTone(ackFlag))" in _read(SESSIONS_PAGE)


def test_session_flag_module_exports_the_acknowledgement_predicate():
    source = _read(SESSION_FLAG)

    assert "export function sessionAckFlag" in source
    assert "export function resolveSessionFlag" in source


# ── Notification surfaces (panel rows + desktop pills) ───────────────────────

NOTIFICATION = ROOT / "ui/chat/js/session-notification.js"
SIDE_PANEL = ROOT / "ui/chat/chat-side-panel.html"
INDEX_HTML = ROOT / "index.html"
SESSION_LOAD = ROOT / "ui/chat/js/session-load.js"
CHAT_UI_CONFIG = ROOT / "data/config/chat_ui.json"

# Every flag the shared resolver can emit needs a glyph + a tone rule on each
# notification surface, or it would silently fall back to the neutral glyph.
ALL_FLAGS = (
    "complete", "needs_input", "partial", "unverified",
    "error", "interrupted", "running", "queued",
)


def test_notification_surfaces_share_the_flag_glyphs():
    source = _read(NOTIFICATION)

    # One glyph per flag, from the shared resolver's own vocabulary.
    assert "FLAG_SVGS" in source
    assert "function _flagIcon" in source
    for flag in ALL_FLAGS:
        assert f"{flag}: _flagSvg(" in source
    # Both surfaces carry the glyph — the pill leads with it as well as the chip.
    assert "sn-row-icon" in source
    assert "sn-pill-icon" in source
    assert "closingCta(session)" in source


def test_panel_rows_re_derive_the_flag_instead_of_freezing_at_build_time():
    source = _read(NOTIFICATION)

    # Built once, then re-stamped from the cache on every poll — a closer record
    # lands a beat after the status flip, so a build-time-only row would freeze
    # at the runtime fallback ("Complete"/green).
    assert "function _applyRowFlag" in source
    assert "function _refreshActiveFlags" in source
    assert "_refreshActiveFlags();" in source
    # The row leads with the session name, then the closer's note, then time.
    order = (
        source.index("textWrap.appendChild(titleEl);"),
        source.index("textWrap.appendChild(noteEl);"),
        source.index("textWrap.appendChild(timeEl);"),
    )
    assert order == tuple(sorted(order))
    # The flag label is no longer a row line — the glyph + tone carry it.
    assert "sn-row-label" not in source


def test_panel_row_css_is_three_lines_with_per_flag_tones():
    css = _read(SIDE_PANEL)

    # Line 2 collapses when the session carries no closer note.
    assert ".sn-row-note[hidden]" in css
    assert ".sn-row-label" not in css
    for flag in ALL_FLAGS:
        if flag == "complete":
            continue  # complete is the base tone on .sn-row-icon
        assert f'.sn-row[data-flag="{flag}"]' in css
    # Tones come from the design-system palette, not raw hex.
    assert "rgba(var(--warning-rgb)" in css
    assert "rgba(var(--danger-rgb)" in css


def test_pill_glyph_tones_track_the_chip_palette():
    css = _read(APP_CSS)

    assert ".sn-pill-icon" in css
    assert '.sn-pill[data-flag="error"] .sn-pill-icon' in css
    assert '.sn-pill[data-flag="needs_input"] .sn-pill-icon' in css
    assert '.sn-pill[data-flag="partial"] .sn-pill-icon' in css
    assert '.sn-pill[data-flag="unverified"] .sn-pill-icon' in css


def test_notification_item_itself_carries_its_flag_tone():
    """The ITEM takes its flag's colour, not only its glyph: a notification has to
    read as its flag before you look at the small icon. Both notification surfaces
    need the rule, and every flag the resolver can emit needs its tone."""
    pill_css = _read(APP_CSS)
    row_css = _read(SIDE_PANEL)

    for flag in ALL_FLAGS:
        assert f'.sn-pill[data-flag="{flag}"]' in pill_css
        assert f'.sn-row[data-flag="{flag}"]' in row_css
    # Tones come from the design-system palette, not raw hex, so both themes resolve.
    assert "rgba(var(--success-rgb)" in pill_css
    assert "rgba(var(--warning-rgb)" in pill_css


def test_pill_is_a_two_line_item_like_a_sessions_page_row():
    """The header pill carries the same three-element anatomy as a panel row and a
    sessions-page row: the session NAME leads, with the closer's note and the
    time-ago on the line BELOW it. A one-line pill truncated the name to a few
    characters and buried the note beside it."""
    source = _read(NOTIFICATION)
    css = _read(APP_CSS)

    # Line 1 = flag chip + session name; line 2 = the closer's note + the timer.
    order = (
        source.index("topLine.appendChild(flagEl);"),
        source.index("topLine.appendChild(titleEl);"),
        source.index("subLine.appendChild(noteEl);"),
        source.index("subLine.appendChild(timeEl);"),
    )
    assert order == tuple(sorted(order))
    # The glyph is the item's rail, and the stacked column holds the two lines.
    assert "sn-pill-text" in source
    assert "sn-pill-line-sub" in source
    assert ".sn-pill-text" in css
    assert ".sn-pill-line-sub" in css


# ── Notification row set = the unread STATE, not an event log ────────────────

def test_notification_row_set_is_the_unread_state_not_an_event_log():
    source = _read(NOTIFICATION)

    # The row set is the SAME predicate the Sessions-page chip uses, so the two
    # surfaces list the same sessions by construction instead of by coincidence.
    assert "sessionAckFlag" in source
    assert "if (!sessionAckFlag(s)) continue;" in source
    assert "function _desiredRows" in source
    assert "function _syncRowsFromCache" in source
    # Reconciled on the poll AND on every session-list republish, so the panel
    # paints from the initial list load instead of waiting a 15s tick.
    assert "_syncRowsFromCache();" in source
    assert "sessionDropdownStore.subscribe(" in source

    # The retired event model leaves nothing behind: no page-load status
    # snapshot (the reason already-finished sessions never surfaced), no
    # per-page notified set, no server-toast reconcile, no dismissal writes.
    for gone in (
        "_lastStatusMap",
        "_notifiedSessionIds",
        "_retrySaves",
        "_reconcileServerToasts",
        "_fetchServerToasts",
        "_apiSave",
        "_apiDismiss",
        "_API_BASE",
    ):
        assert gone not in source, gone


def test_notification_acknowledgement_writes_the_db_read_state():
    source = _read(NOTIFICATION)

    # Dismissing a row/pill ("✕" or "Clear all") marks the SESSION READ — the
    # same write the Sessions-page chip performs, so one acknowledgement serves
    # both surfaces and follows the user across devices.
    assert "sessions/${encodeURIComponent(sid)}/read" in source
    assert "session:visited" in source
    assert "function _dismissRow" in source
    assert "function _clearAll" in source
    assert "_markSessionRead(sid)" in source
    # Mark-unread restores the row, off the DB-backed has_unread.
    assert "session:unread" in source
    # The per-user notification table is no longer the panel's source of truth.
    assert "'/api/v1/db/session-notifications" not in source


# ── A visit is a glance, not a read ─────────────────────────────────────────

def test_visiting_a_session_does_not_acknowledge_it():
    source = _read(SESSION_LOAD)

    # Opening/switching to a session must NOT write the read state by default: the
    # unread flag and its notification row have to survive a visit, so the user can
    # flip through sessions to glance without silently clearing them. The switch's
    # own POST /sessions/{id}/read and its session:visited fan-out sit behind the
    # `mark_read_on_visit` toggle, which defaults OFF — a visit is silent unless the
    # operator explicitly opts back in.
    assert "chatUiFlag('mark_read_on_visit', false) && isSwitch" in source
    assert '"mark_read_on_visit": false' in _read(CHAT_UI_CONFIG)
    # ...and the acknowledgements that always apply are only the EXPLICIT ones: the
    # flag chip's cycle / row menu (dropdown controller) and a notification row's
    # ✕ / "Clear all" (notification module).
    assert "/read?db=user.db" in _read(DROPDOWN_CONTROLLER)
    assert "sessions/${encodeURIComponent(sid)}/read" in _read(NOTIFICATION)


# ── Opening a notification is a glance, not a read ──────────────────────────

def test_opening_a_notification_opens_without_acknowledging_it():
    source = _read(NOTIFICATION)

    # Clicking a notification — a mobile panel row OR a desktop pill — only OPENS
    # its session. It must not acknowledge it, so the notification survives the
    # visit and the user can glance through sessions without silently losing the
    # unread ones. These exact snippets pin both open paths dismiss-free:
    # re-adding a _dismissRow() call to either handler breaks the match.
    assert (
        "  row.addEventListener('click', (e) => {\n"
        "    if (e.target.closest('button')) return;\n"
        "    switchToSession(sid);\n"
        "  });"
    ) in source
    assert (
        "  const openSession = () => {\n"
        "    switchToSession(sid);\n"
        "  };"
    ) in source

    # Dismissal therefore stays wired to the EXPLICIT acknowledgements only: the
    # two ✕ buttons (one per surface). A third _dismissRow() call would mean an
    # open path had silently re-acquired the acknowledgement again.
    assert source.count("_dismissRow(sid);") == 2


# ── Dual notifications: the counter beside "Chat" on the desktop header ─────

def _enclosing_at_rules(css: str, needle: str):
    """The at-rules that contain the first line matching ``needle`` ([] = global).

    Walks the sheet tracking brace depth so a rule authored inside a media query
    can be told apart from the same rule authored at the top level.
    """
    stack = []
    depth = 0
    for line in css.splitlines():
        if needle in line:
            return [rule for _, rule in stack]
        opens = line.count("{")
        closes = line.count("}")
        if "@media" in line and opens:
            stack.append((depth, line.strip()))
        depth += opens - closes
        while stack and depth <= stack[-1][0]:
            stack.pop()
    return []


def test_desktop_counter_beside_chat_opens_the_shared_panel():
    """Desktop runs BOTH notification surfaces at once: the header pill strip AND
    the counter beside "Chat" that mobile uses to open the panel. The counter is
    not a mobile-only control, and clicking it must reach the same panel even
    though the pills are already on screen."""
    html = _read(INDEX_HTML)
    source = _read(NOTIFICATION)
    css = _read(APP_CSS)

    # The main app header authors the two controls as ONE joined cluster — the
    # counter button immediately LEFT of the "Chat" label, inside #status-right,
    # which every layout renders.
    start = html.index('<span class="chat-header-controls">')
    end = html.index("<!-- The new-session (+) button moved", start)
    cluster = html[start:end]
    assert "<span>Chat</span>" in cluster
    assert 'id="chat-notification-toggle-btn"' in cluster
    assert cluster.index('id="chat-notification-toggle-btn"') < cluster.index('id="chat-toggle-btn"')
    assert '<span class="chat-notification-counter" aria-hidden="true">0</span>' in cluster
    # The desktop pill strip stays as well — dual notifications, not a swap.
    assert 'id="session-notification-inline"' in html

    # Clicking the counter opens the notification panel; with no notifications it
    # falls back to the plain chat toggle so an empty panel never opens.
    assert "e.target.closest('#chat-notification-toggle-btn')" in html
    assert "notificationBtn.classList.contains('has-notifications')" in html
    assert "window.__toggleSessionNotificationPanel();" in html
    assert "window.__toggleChat();" in html

    # ...and the shared state is published on EVERY layout: neither the count
    # write nor the panel toggle carries a desktop bail-out.
    count_start = source.index("function _updateCount()")
    count_body = source[count_start:source.index("\n}\n", count_start)]
    assert "counter.textContent = String(count);" in count_body
    assert "notificationToggle.classList.toggle('has-notifications', count > 0);" in count_body
    assert "_isDesktopLayout" not in count_body
    assert "_renderInline();" in count_body  # the pills still render beside it

    toggle_start = source.index("export function toggleSessionNotificationPanel()")
    toggle_body = source[toggle_start:source.index("\n}\n", toggle_start)]
    assert "if (_activeRows.size === 0) return;" in toggle_body
    assert "panel.hidden = !opening;" in toggle_body
    assert "_isDesktopLayout" not in toggle_body

    # The counter's own styles sit OUTSIDE every max-width media query, so no
    # breakpoint can hide the counter on desktop.
    assert ".chat-notification-counter {" in css
    assert "#chat-notification-toggle-btn.has-notifications .chat-notification-counter { display: inline-flex; }" in css
    assert _enclosing_at_rules(css, ".chat-notification-counter {") == []
    assert _enclosing_at_rules(css, "#chat-notification-toggle-btn.has-notifications .chat-notification-counter") == []
    # The desktop pill strip is styled globally too, so both surfaces coexist.
    assert _enclosing_at_rules(css, ".session-notification-inline {") == []
    assert ".sn-pill {" in css

