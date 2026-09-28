"""Image Vision ability — SELF-CONTAINED drop-in.

READS images for the agent (it does NOT generate them — that's the separate
``image_generation`` ability — and it no longer switches the agent's model —
that's the separate ``model_switcher`` ability). One tool:

  • ``process_image`` — the agent's own model often can't see images (a text-only
    or tool-only model). This tool DELEGATES one image question to a vision-capable
    model (the row ticked Image-in in App Config → Models) via a tools-free one-shot
    worker, and returns the answer. The main agent never pauses; for a sharper
    answer it simply calls again with a tighter prompt (fresh worker each time —
    cheaper and simpler than a long-lived sub-conversation).

For SUSTAINED image work the agent can instead TAKE OVER on a vision model rather
than delegating every turn — but that is the Model Switcher ability's job now:
``set_model('<vision model>')`` (and ``set_model('default')`` to revert). When the
Model Switcher ability is enabled, the attachment router's describe-mode note tells
the agent it has that option; when it isn't, the note steers it to ``process_image``.

Drop-in contract: FEATURE descriptor + build_tools()/TOOL_SCHEMAS/DESTRUCTIVE,
discovered generically by app/tools/loader.py. See plugins/abilities/_TEMPLATE.py.

╔══════════════════════════════════════════════════════════════════════════════╗
║  SISTER-SYNC: MODEL-WORKER-DELEGATE                                           ║
║  process_image runs on the shared neutral-core route app.agent.provider_completion ║
║  .ask_model (so it works even when the Orchestration ability is OFF). If that ║
║  worker's signature/return changes, update here. Model roles (TEXT/IN/OUT)    ║
║  come from App Config → Models; vision picks the IN row via                   ║
║  app.admin.settings.pick_vision_model. (grep SISTER-SYNC: MODEL-WORKER-DELEGATE)║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

from typing import Optional

TOOL_SCHEMAS: dict = {}
# process_image is read-only (it delegates a look at an image, never changes the
# session). Changing the agent's own model — including taking over on a vision
# model for sustained image work — now lives in the separate Model Switcher
# ability (set_model); this ability only READS images.
DESTRUCTIVE: set = set()


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: Optional[str] = None, enabled_providers=None, **_ctx):
    """Return {tool_name: handler} for the image_vision ability. Heavy imports stay
    lazy (inside the handlers) so scanning FEATURE stays cheap."""

    async def process_image(question: str, attachment_id: str = "") -> str:
        """Ask a vision model a specific question about an attached image and return
        its answer. Use this when your own model can't see images."""
        import json
        from app.db import get_db
        from app.admin.settings import load_llm_capabilities_for_user, pick_vision_model
        from app.agent.provider_completion import ask_model

        if not (question or "").strip():
            return json.dumps({"status": "error", "message": "question is required — say what to look for."})

        db = get_db()
        # Resolve which image to look at: explicit id, else the latest image in this session.
        att = None
        if attachment_id:
            att = await db.get_attachment(attachment_id)
            if not att:
                return json.dumps({"status": "error", "message": f"No attachment {attachment_id}."})
        else:
            try:
                atts = await db.get_session_attachments(session_id)
            except Exception:
                atts = []
            images = [a for a in atts if (a.get("mime_type") or "").lower().startswith("image/")]
            if not images:
                return json.dumps({
                    "status": "error",
                    "message": "No image is attached to this conversation. Ask the user to upload one.",
                })
            att = images[-1]  # most recent (rows come back oldest→newest)

        # Pick the configured vision (Image-in) model. Warm the catalog cache first
        # so pick_vision_model's capability guard has data to veto a mis-ticked
        # text-only model.
        try:
            from app import model_catalog
            await model_catalog.ensure_fresh()
        except Exception:
            pass
        try:
            caps = await load_llm_capabilities_for_user(user_id)
        except Exception as e:
            return json.dumps({"status": "error", "message": f"Could not read model config: {e}"})
        vision = pick_vision_model(caps)
        if not vision:
            return json.dumps({
                "status": "error",
                "code": "no_vision_model",
                "message": ("No image-input model is configured. Ask the admin to open App "
                            "Config → Models, save a vision-capable model and tick its In box."),
            })

        sys_line = (
            "You are a vision assistant. Look at the attached image and answer the "
            "question precisely and factually. Include any visible text verbatim and "
            "concrete details (objects, layout, colours, positions). Do not speculate "
            "beyond what is visible."
        )
        err_sink: list = []
        answer = await ask_model(vision, sys_line, question, attachments=[att],
                                 max_tokens=900, error_sink=err_sink)
        if not answer:
            err_text = (err_sink[-1] if err_sink else "").strip()
            # Provider account out of credits? Persist the user-facing alert in this
            # session so the human sees WHY vision is down, and give the agent the
            # REAL error instead of a generic "could not describe the image".
            if err_text:
                try:
                    from app.util.alerts import is_provider_credit_error, persist_402_alert
                    if is_provider_credit_error(err_text):
                        await persist_402_alert(err_text, user_id, session_id,
                                                vision.get("model", ""),
                                                vision.get("provider", ""))
                except Exception:
                    pass
            detail = " — {}".format(err_text) if err_text else ""
            return json.dumps({
                "status": "error",
                "code": "vision_model_error",
                "message": "The vision model ({}) could not describe the image.{}".format(
                    vision.get("model", ""), detail),
            })
        return json.dumps({
            "status": "ok",
            "model": vision.get("model", ""),
            "attachment": att.get("original_name") or att.get("id"),
            # Surface the exact prompt sent to the vision worker + its reply so the
            # tool call is fully inspectable in the UI.
            "prompt": {"system": sys_line, "question": question},
            "answer": answer,
        })

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update({
        "process_image": {
            "type": "object",
            "properties": {
                "question": {"type": "string",
                             "description": "What to find out about the image — be specific (e.g. 'what colours are in the top-right corner?')."},
                "attachment_id": {"type": "string",
                                  "description": "Optional. A specific attachment id; omit to use the most recent image in this chat."},
            },
            "required": ["question"],
        },
    })
    return {"process_image": process_image}
