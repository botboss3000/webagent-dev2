"""The only service that admits execution output to the public conversation."""
import inspect
from .contracts import SAFE_FAILURE
from .repository import digest

class PublicationService:
    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def validate_text(text):
        if not isinstance(text, str) or not text.strip() or len(text) > 100000:
            return ["message"]
        return []

    async def notice(self, run_id, generation, text):
        run = await self.repository.run(run_id)
        missing = self.validate_text(text)
        return await self.repository.decide(
            run_id,generation,"notice",{"text":text},accepted=not missing,reasons=missing,
            policy_digest=digest(run["policy"]),
        )

    async def agent_auth(self, run_id, generation, payload):
        """Publish only the fields needed to display the agent sign-in form."""
        if not isinstance(payload,dict) or payload.get("ui") != "agent_auth":
            return None
        safe={
            "ui":"agent_auth",
            "local_signup_mode":payload.get("local_signup_mode")
                if payload.get("local_signup_mode") in {"open","invite","disabled"} else "disabled",
            "app_login_enabled":payload.get("app_login_enabled") is True,
        }
        run=await self.repository.run(run_id)
        return await self.repository.decide(
            run_id,generation,"component",safe,accepted=True,reasons=[],
            policy_digest=digest(run["policy"]))

    async def final(self, run_id, generation, text, status, *, repair=None, manual_retry=False):
        """Finalize with bounded, tool-free repairs; never publish a rejected draft."""
        from app.chat.closing import missing_requirements, attempts_exhausted
        run = await self.repository.run(run_id)
        if not await self.repository.freeze_finalization(run_id,generation,text,status):
            return {"pending_input":True}
        policy = run["policy"]
        budget_policy = dict(policy)
        if manual_retry:
            # A user-requested linked attempt has its own repair allowance even
            # when automatic closer retries were disabled. Validation rules and
            # the frozen policy digest remain those of the original run.
            budget_policy["max_attempts"] = max(1,int(policy.get("max_attempts") or 0))
            budget_policy["max_attempts_per_requirement"] = max(
                1,int(policy.get("max_attempts_per_requirement") or 0))
        counts, attempt = {}, 0
        status = {} if status is None else status
        while True:
            validation_error = False
            try:
                missing = self.validate_text(text)
                status_valid = (isinstance(status,dict) and not set(status)-{"flag","cta"}
                                and all(isinstance(value,str) for value in status.values()))
                if not status_valid:
                    missing.append("status_schema")
                else:
                    missing += missing_requirements(text,policy,status)
                    if len(status.get("cta", "")) > 80:
                        missing.append("notification")
                missing = list(dict.fromkeys(missing))
            except Exception:
                missing, validation_error = ["validator_error"], True
            result = await self.repository.decide(
                run_id,generation,"answer",{"text":text,"status":status},
                accepted=not missing,reasons=missing,policy_digest=digest(policy),terminal=True,
            )
            if not missing:
                return result
            if validation_error or repair is None or attempts_exhausted(missing,attempt,counts,budget_policy):
                break
            for requirement in missing:
                counts[requirement] = counts.get(requirement,0) + 1
            attempt += 1
            try:
                revised = repair(text,dict(status),list(missing),dict(policy))
                revised = await revised if inspect.isawaitable(revised) else revised
                if not isinstance(revised,dict):
                    break
                metadata_only = set(missing) <= {"flag","notification","status_schema"}
                if not metadata_only:
                    text = revised.get("text",text)
                status = revised.get("status",status)
                if not isinstance(status,dict):
                    status = {}
            except Exception:
                break
        return await self.repository.decide(
            run_id,generation,"failure",{
                "text":SAFE_FAILURE,"system_outcome":"failed",
                "retry":{"kind":"finalization","candidate_id":result["candidate_id"]},
            },accepted=True,reasons=[],policy_digest=digest(policy),terminal=True,outcome="failed",
        )
