import asyncio
import importlib.util
import json
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "plugins/abilities/Administrator/v3_build/v3_build.py"
spec = importlib.util.spec_from_file_location("v3_build_test", PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

def test_discovery_and_schemas():
    from app import abilities
    abilities.reload()
    feature = abilities.ability_feature_with_skill("v3_build")
    assert feature and feature["display_name"] == "V3 Build"
    assert set(m.build_tools()) == set(m.TOOL_SCHEMAS)
    assert m.DESTRUCTIVE == {"v3_cargo_task", "v3_build_image", "v3_worker_submit", "login_as_current_user_v3"}
    descriptor = json.loads(PATH.with_suffix(".json").read_text())
    assert set(descriptor["tools"]) == set(m.TOOL_SCHEMAS)

def test_reject_arbitrary_task_and_timeout():
    tools = m.build_tools()
    for task, timeout in [("clean", 600), ("test; rm -rf /", 600), ("test", 1801), ("test", True)]:
        result = json.loads(asyncio.run(tools["v3_cargo_task"](task, timeout)))
        assert result["status"] == "error"

def test_block_without_prerequisites(monkeypatch):
    async def probe(): return {"native_ready": False, "image_ready": False}
    monkeypatch.setattr(m, "_probe", probe)
    for name, args in [("v3_cargo_task", ("check",)), ("v3_build_image", ())]:
        result = json.loads(asyncio.run(m.build_tools()[name](*args)))
        assert result["status"] == "blocked"
    assert not m._BUSY

def test_cargo_fixed_command_and_clean_target(monkeypatch):
    calls = []
    async def probe(): return {"native_ready": True}
    async def run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert Path(kwargs["env"]["CARGO_TARGET_DIR"]).is_dir()
        return {"exit_code": 0, "timed_out": False}
    monkeypatch.setattr(m, "_probe", probe)
    monkeypatch.setattr(m, "_run", run)
    monkeypatch.setattr(m.shutil, "which", lambda name: "/bin/" + name)
    result = json.loads(asyncio.run(m.build_tools()["v3_cargo_task"]("release")))
    assert result["status"] == "ok"
    argv, kwargs = calls[0]
    assert argv == ["/bin/cargo", "build", "--release", "--locked", "--manifest-path", str(m.ROOT / "v3/Cargo.toml"), "-j", "2"]
    assert not Path(kwargs["env"]["CARGO_TARGET_DIR"]).exists()

def test_environment_does_not_inherit_secrets(monkeypatch):
    monkeypatch.setenv("V3_MESSAGE_API_TOKEN", "sensitive")
    monkeypatch.setenv("OPENAI_API_KEY", "sensitive")
    assert "V3_MESSAGE_API_TOKEN" not in m._env()
    assert "OPENAI_API_KEY" not in m._env()

def test_archive_excludes_runtime_data_and_rejects_symlink(tmp_path, monkeypatch):
    import hashlib
    import tempfile
    import tarfile
    (tmp_path / "v3").mkdir()
    (tmp_path / "v3/input").mkdir()
    asset = tmp_path / "asset"; asset.write_text("fixture")
    (tmp_path / "v3/ui-baseline.json").write_text(json.dumps({"assets": {"asset": hashlib.sha256(asset.read_bytes()).hexdigest()}}))
    (tmp_path / "v3/input/source").write_text("code")
    (tmp_path / "v3/input/__pycache__").mkdir()
    (tmp_path / "v3/input/__pycache__/secret.pyc").write_text("cached")
    (tmp_path / "v3/data").mkdir()
    (tmp_path / "v3/data/secret").write_text("must not enter image")
    monkeypatch.setattr(m, "ROOT", tmp_path)
    monkeypatch.setattr(m, "CONTEXT", ("v3/input",))
    with tempfile.TemporaryFile() as f:
        m._context_archive(f)
        with tarfile.open(fileobj=f) as archive:
            names = archive.getnames()
            assert names == ["v3/input", "v3/input/source"]
    (tmp_path / "v3/input/link").symlink_to(asset)
    import pytest
    with tempfile.TemporaryFile() as f, pytest.raises(ValueError, match="link"):
        m._context_archive(f)

def test_subprocess_timeout():
    import sys
    result = asyncio.run(m._run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.05))
    assert result["timed_out"] and result["exit_code"] != 0

def test_v3_login_only_in_build_ability():
    from plugins.abilities.Administrator.diagnostics import diagnostics
    assert "login_as_current_user_v3" not in diagnostics.build_tools()
    assert "login_as_current_user_v3" not in diagnostics.TOOL_SCHEMAS
    assert "login_as_current_user_v3" in m.build_tools(user_id="fixture-admin")

def test_v3_login_missing_identity_and_configuration(monkeypatch):
    result = json.loads(asyncio.run(m.build_tools()["login_as_current_user_v3"]()))
    assert result["status"] == "error" and "signed-in" in result["message"]
    monkeypatch.delenv("V3_BROWSER_LOGIN_TOKEN", raising=False)
    monkeypatch.delenv("V3_BROWSER_LOGIN_CALLER_ID", raising=False)
    result = json.loads(asyncio.run(m.build_tools(user_id="fixture-admin")["login_as_current_user_v3"]()))
    assert result["status"] == "error" and "not configured" in result["message"]

def test_v3_login_mapping_and_origin(monkeypatch):
    import pytest
    monkeypatch.setenv("V3_BROWSER_LOGIN_TOKEN", "x" * 40)
    monkeypatch.setenv("V3_BROWSER_LOGIN_CALLER_ID", "fixture-admin")
    monkeypatch.setenv("V3_DIAGNOSTICS_ORIGIN", "https://dev.webagent.live")
    assert m._v3_login_config("fixture-admin")[0] == "https://dev.webagent.live"
    with pytest.raises(ValueError, match="Current user"):
        m._v3_login_config("other-user")
    for origin in ["http://remote.example", "https://user:pass@example.com", "https://example.com/path"]:
        monkeypatch.setenv("V3_DIAGNOSTICS_ORIGIN", origin)
        with pytest.raises(ValueError, match="HTTPS origin"):
            m._v3_login_config("fixture-admin")

def test_v3_login_rejects_external_route(monkeypatch):
    monkeypatch.setenv("V3_BROWSER_LOGIN_TOKEN", "x" * 40)
    monkeypatch.setenv("V3_BROWSER_LOGIN_CALLER_ID", "fixture-admin")
    monkeypatch.setenv("V3_DIAGNOSTICS_ORIGIN", "https://dev.webagent.live")
    for path in ["https://other.example/app", "//other.example/app", "/app\\bad", "/app\n"]:
        result = json.loads(asyncio.run(m.build_tools(user_id="fixture-admin")["login_as_current_user_v3"](path=path)))
        assert result["status"] == "error" and "relative route" in result["message"]
