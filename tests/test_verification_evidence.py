from app.agent.verification_evidence import (
    build_verification_event,
    result_observation_success,
)


def _result(content, *, success=True, duration_ms=17):
    return {"content": content, "success": success, "duration_ms": duration_ms}


def test_pytest_evidence_captures_semantics_without_raw_command_or_output():
    command = "python -m pytest -q tests/test_widget.py"
    event = build_verification_event(
        tool_name="run_command",
        tool_args={"command": command},
        result=_result({
            "exit_code": 0,
            "stdout": "12 passed, 2 skipped in 0.41s",
            "stderr": "",
            "timed_out": False,
        }),
        turn=3,
    )

    assert event["kind"] == "test"
    assert event["evidence_strength"] == "behavioral"
    assert event["success"] is True
    assert event["tool_success"] is True
    assert event["exit_code"] == 0
    assert event["counts"] == {"passed": 12, "skipped": 2}
    assert event["command"]["framework"] == "pytest"
    assert event["command"]["scope_hints"] == ["tests/test_widget.py"]
    assert command not in str(event)
    assert "12 passed" not in str(event)


def test_nonzero_check_is_failed_even_when_tool_handler_returned_normally():
    event = build_verification_event(
        tool_name="run_command",
        tool_args={"command": "pytest tests/test_widget.py"},
        result=_result({
            "exit_code": 1,
            "stdout": "1 failed, 11 passed in 0.50s",
            "stderr": "",
            "timed_out": False,
        }),
        turn=1,
    )

    assert event["tool_success"] is True
    assert event["success"] is False
    assert event["exit_code"] == 1
    assert event["counts"] == {"failed": 1, "passed": 11}


def test_timeout_is_failed_and_arbitrary_arguments_are_not_persisted():
    secret = "--token super-secret-value"
    event = build_verification_event(
        tool_name="run_command",
        tool_args={"command": f"pytest tests/test_api.py {secret}"},
        result=_result({
            "exit_code": -1,
            "stdout": "",
            "stderr": "timed out",
            "timed_out": True,
        }),
        turn=2,
    )

    assert event["success"] is False
    assert event["timed_out"] is True
    assert event["command"]["scope_hints"] == ["tests/test_api.py"]
    assert "super-secret-value" not in str(event)


def test_fingerprints_exclude_secret_command_arguments_and_raw_output():
    def event(secret):
        return build_verification_event(
            tool_name="run_command",
            tool_args={"command": f"pytest tests/test_api.py --token {secret}"},
            result=_result({
                "exit_code": 0, "stdout": f"3 passed\n{secret}", "stderr": "",
            }),
            turn=1,
        )

    assert event("alpha-secret")["fingerprint"] == event("beta-secret")["fingerprint"]


def test_inspection_and_runtime_events_receive_lower_evidence_strengths():
    diff = build_verification_event(
        tool_name="git_tool",
        tool_args={"operation": "diff"},
        result=_result("clean"),
        turn=1,
    )
    browser = build_verification_event(
        tool_name="browser_action",
        tool_args={"action": "get_text"},
        result=_result("visible"),
        turn=1,
    )

    assert diff["kind"] == "diff"
    assert diff["evidence_strength"] == "inspection"
    assert diff["operation"] == "diff"
    assert browser["kind"] == "runtime_observation"
    assert browser["evidence_strength"] == "runtime_observation"



def test_structured_browser_failure_is_not_successful_runtime_evidence():
    result = _result(
        "{'success': False, 'backend': 'headless', "
        "'error': 'Playwright executable missing'}"
    )
    assert result_observation_success(result) is False
    event = build_verification_event(
        tool_name="browser_action",
        tool_args={"action": "screenshot"},
        result=result,
        turn=1,
    )
    assert event["tool_success"] is True
    assert event["success"] is False


def test_structured_ok_false_is_not_successful_runtime_evidence():
    assert result_observation_success(
        _result('{"ok": false, "status": "error"}')
    ) is False
def test_python_repr_result_is_supported_for_current_loop_transport():
    event = build_verification_event(
        tool_name="run_command",
        tool_args={"command": "pytest -q"},
        result=_result(
            "{'exit_code': 0, 'stdout': '3 passed in 0.1s', "
            "'stderr': '', 'timed_out': False}"
        ),
        turn=1,
    )

    assert event["success"] is True
    assert event["counts"] == {"passed": 3}
