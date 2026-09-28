"""Focused guards for the post-bootstrap interactive-core boundary."""

from __future__ import annotations

import asyncio
import ast
from pathlib import Path

from fastapi import APIRouter


ROOT = Path(__file__).resolve().parents[1]


def test_main_manifest_does_not_eagerly_import_optional_implementations() -> None:
    """Optional integrations must not sneak back into app.main's import list."""
    tree = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))
    forbidden = {
        "app.api.github",
        "app.api.deploy",
        "app.api.diagnostics",
        "app.admin.optimizer",
        "app.admin.integrations",
        "plugins.engines.api",
        "app.p2p.server",
        "app.api.admin_p2p",
    }
    # Imports inside explicit startup/first-use functions are allowed. Only
    # module-level statements execute while constructing the interactive core.
    top_level_modules = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not (forbidden & top_level_modules), forbidden & top_level_modules


def test_essential_sync_startup_work_is_offloaded() -> None:
    """Cold filesystem/config checks must not freeze the bootstrap ASGI loop."""
    tree = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))
    startup = next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "startup"
    )
    offloaded = {
        ast.unparse(call.args[0])
        for call in ast.walk(startup)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "to_thread"
        and call.args
    }
    assert {"set_safety_lock_active", "_kill_switch.init", "db_crypto.reconcile", "get_recorder"} <= offloaded


def test_interactive_core_registers_first_experience_routes() -> None:
    from app.main import app

    expected_names = (
        "health_check",
        "status",
        "get_pages_catalog",
        "list_sessions",
        "chat_send",
    )
    for name in expected_names:
        assert app.url_path_for(name)


def test_optional_router_loader_reports_detail_and_invalidates_openapi(monkeypatch) -> None:
    import app.main as main
    from app.optional_routes import OptionalRoute

    fake_router = APIRouter()

    @fake_router.get("/api/v1/test-late-capability")
    async def _late_capability():
        return {"ok": True}

    included = []
    monkeypatch.setattr(main, "OPTIONAL_ROUTES", (OptionalRoute("late_test", "unused"),))
    monkeypatch.setattr(main, "load_optional_router", lambda _spec: fake_router)
    monkeypatch.setattr(main.app, "include_router", included.append)
    main.app.state.optional_routers_mounted = False
    main.app.openapi_schema = {"stale": True}

    asyncio.run(main._mount_optional_routers())

    assert included == [fake_router]
    assert main.app.state.optional_routers_mounted is True
    assert main.app.state.startup_active_detail == ""
    assert main.app.openapi_schema is None
    main.app.state.optional_routers_mounted = False
