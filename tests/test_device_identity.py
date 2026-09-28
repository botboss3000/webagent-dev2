from app.devices import identity


def _reset_identity(monkeypatch):
    monkeypatch.setattr(identity, "_DEVICE_ID", None)


def _set_roots(tmp_path, monkeypatch):
    home = tmp_path / "home"
    data_root = tmp_path / "webagent" / "data"
    codex_home = tmp_path / "codex-home"
    home.mkdir()
    codex_home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("WEBAGENT_DATA_ROOT", str(data_root))
    monkeypatch.delenv("WEBAGENT_DEVICE_ID", raising=False)
    return home, data_root, codex_home


def test_device_id_persists_inside_webagent_data_root(tmp_path, monkeypatch):
    _, data_root, _ = _set_roots(tmp_path, monkeypatch)
    _reset_identity(monkeypatch)

    first = identity.device_id()
    durable_file = data_root / "instance" / "device_id"

    assert durable_file.read_text(encoding="utf-8").strip() == first
    _reset_identity(monkeypatch)
    assert identity.device_id() == first


def test_codex_state_device_id_is_migrated_and_removed(tmp_path, monkeypatch):
    _, data_root, codex_home = _set_roots(tmp_path, monkeypatch)
    old_file = codex_home / "webagent" / "device_id"
    old_file.parent.mkdir()
    old_file.write_text("codex-era-device\n", encoding="utf-8")
    _reset_identity(monkeypatch)

    assert identity.device_id() == "codex-era-device"
    assert (data_root / "instance" / "device_id").read_text(encoding="utf-8") == "codex-era-device"
    assert not old_file.exists()


def test_legacy_home_device_id_is_migrated_into_webagent(tmp_path, monkeypatch):
    home, data_root, _ = _set_roots(tmp_path, monkeypatch)
    legacy_file = home / ".webagent" / "device_id"
    legacy_file.parent.mkdir(parents=True)
    legacy_file.write_text("legacy-device\n", encoding="utf-8")
    _reset_identity(monkeypatch)

    assert identity.device_id() == "legacy-device"
    assert (data_root / "instance" / "device_id").read_text(encoding="utf-8") == "legacy-device"
    assert not legacy_file.exists()
