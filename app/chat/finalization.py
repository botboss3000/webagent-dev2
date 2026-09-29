"""Tool-free repair of a stored closing draft in a linked retry run."""
import json

from app.agent.provider_completion import (
    SubscriptionResponseClient, complete_text, completion_answer,
)


async def repair_callback(execution, agent_id, user_request):
    from app.admin.settings import resolve_agent_role_config
    from app.db import get_db

    agent = await get_db().get_agent_by_id(agent_id)
    if not agent:
        return None
    config = await resolve_agent_role_config(execution.repo.owner_id, agent, "standard")
    model = str(config.get("model") or "")
    provider = str(config.get("provider") or "")
    if not model:
        return None
    if provider == "openai_subscription":
        client = SubscriptionResponseClient(
            scope=str(config.get("credential_scope") or "platform"),
            entry_id=str(config.get("entry_id") or ""),
            user_id=str(config.get("_credential_user_id") or execution.repo.owner_id),
            agent_id=str(config.get("_credential_agent_id") or ""),
        )
    else:
        if not config.get("base_url") or not config.get("api_key"):
            return None
        try:
            from openai import AsyncOpenAI
        except ImportError:
            from app.openai_compat import AsyncOpenAI
        client = AsyncOpenAI(base_url=config["base_url"], api_key=config["api_key"], timeout=60)

    async def repair(text, status, missing, policy):
        metadata_only = set(missing) <= {"flag", "notification", "status_schema"}
        instruction = (
            "You are repairing a final chat answer already drafted by the working agent. "
            "Do not perform work, call tools, or claim new work was done. "
            "Return only a JSON object. "
        )
        if metadata_only:
            instruction += (
                'The answer text is fixed. Return only {"status":{"flag":"...","cta":"..."}}. '
                "Do not return or rewrite the answer text. "
            )
        else:
            instruction += (
                'Return {"text":"...","status":{"flag":"...","cta":"..."}}. '
                "Repair only the listed requirements and preserve the draft's factual content. "
            )
        request = {
            "user_request":user_request,"draft": text, "status": status, "missing": missing,
            "allowed_flags": policy.get("flags", []),
            "notification_max_words": policy.get("cta_max_words"),
            "format": policy.get("format", ""),
            "output_limit": policy.get("max_tokens"),
        }
        try:
            response = await complete_text(
                client, model=model,
                messages=[{"role":"system","content":instruction},
                          {"role":"user","content":json.dumps(request,ensure_ascii=False)}],
                max_tokens=1200, temperature=0, timeout=60,
                provider=provider, base_url=str(config.get("base_url") or ""),
                strict_max_tokens=True,
            )
            raw=completion_answer(response)
            await execution.repo.journal(execution.run_id,execution.generation,
                                         "finalization_repair_output",{"text":raw,"missing":missing})
            parsed=json.loads(raw)
            if not isinstance(parsed,dict) or set(parsed)-{"text","status"}:
                return None
            if not isinstance(parsed.get("status"),dict):
                return None
            if metadata_only and "text" in parsed:
                return None
            return parsed
        except Exception as error:
            await execution.repo.journal(execution.run_id,execution.generation,
                                         "finalization_repair_error",
                                         {"error_type":type(error).__name__})
            return None

    return repair
