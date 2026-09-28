from app.agent.work_policy import WorkPolicy, WorkState, is_meaningful_tool


def test_user_wording_never_creates_a_tool_obligation() -> None:
    for content in ("Add a Save button.", "Please inspect the logs.", "Continue", "Why did this happen?"):
        policy = WorkPolicy.initial([{"role": "user", "content": content}])
        assert policy.state is WorkState.CONVERSATION
        assert not policy.requires_tool


def test_durable_plan_task_and_resume_state_remain_obligations() -> None:
    messages = [{"role": "user", "content": "Anything"}]
    assert WorkPolicy.initial(messages, requires_plan=True).transition_reason == "plan_required"
    assert WorkPolicy.initial(messages, task_pending=True).transition_reason == "task_progress_gap"
    assert WorkPolicy.initial(messages, resume_reason="incomplete").transition_reason == "resume:incomplete"


def test_meaningful_tool_and_checkpoint_update_durable_state() -> None:
    policy = WorkPolicy.initial([{"role": "user", "content": "Anything"}], requires_plan=True)
    policy.record_tool("shell", succeeded=True)
    assert policy.state is WorkState.WORK_SATISFIED
    policy.record_checkpoint(task_pending=True)
    assert policy.state is WorkState.WORK_PENDING
    policy.record_checkpoint(task_pending=False)
    assert policy.state is WorkState.WORK_SATISFIED


def test_blocking_tool_failure_allows_an_honest_final_response() -> None:
    policy = WorkPolicy.initial([{"role": "user", "content": "Anything"}], requires_plan=True)
    policy.record_tool("browser", succeeded=False, error_type="permission_denied")
    assert policy.state is WorkState.BLOCKED


def test_diagnostic_records_auto_tool_choice() -> None:
    policy = WorkPolicy.initial([{"role": "user", "content": "Add it."}])
    assert policy.diagnostic(transport="responses", requested_tool_choice="auto", outcome="prose_allowed") == {
        "state": "conversation", "transition_reason": "conversation",
        "transport": "responses", "requested_tool_choice": "auto", "outcome": "prose_allowed",
    }


def test_meaningful_tool_classification() -> None:
    assert is_meaningful_tool("browser")
    assert not is_meaningful_tool("present_plan")
