import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.abilities.Administrator.git_control import repository_access as module


def test_repository_urls_strip_secrets_and_reject_other_hosts():
    assert module.github_repository('https://user:SECRET@github.com/botboss3000/webagent-dev.git?token=SECRET') == 'botboss3000/webagent-dev'
    assert module.github_repository('git@github.com:botboss3000/webagent-dev.git') == 'botboss3000/webagent-dev'
    assert module.github_repository('https://evil.example/owner/repo') == ''


def test_guest_cannot_read_shared_vault(monkeypatch):
    from app import db
    vault = SimpleNamespace(is_user_admin=AsyncMock(return_value=False), auth_element_get=AsyncMock())
    monkeypatch.setattr(db, 'get_app_db', lambda: vault)
    result = asyncio.run(module.repository_access(user_id='guest'))
    assert result['status'] == 'blocked'
    vault.auth_element_get.assert_not_called()


def test_configuration_only_uses_app_vault_and_returns_no_secret(monkeypatch):
    from app import db, git_repos
    vault = SimpleNamespace(is_user_admin=AsyncMock(return_value=True),
                            auth_element_get=AsyncMock(return_value={'secret_ref': 'TEST_SECRET'}))
    monkeypatch.setattr(db, 'get_app_db', lambda: vault)
    monkeypatch.setattr(git_repos, 'list_repos', lambda: [
        {'id': git_repos.BUILTIN_ID, 'remote_url': 'https://user:TEST_SECRET@github.com/owner/repo.git', 'active': True}])
    probe = AsyncMock()
    monkeypatch.setattr(module, 'probe_repository', probe)
    result = asyncio.run(module.repository_access(user_id='admin', verify_remote=False))
    vault.auth_element_get.assert_awaited_once_with('admin', 'deploy_github_token', 'default')
    probe.assert_not_called()
    assert 'TEST_SECRET' not in json.dumps(result)
    assert result['repositories'][0]['credential'] == {'source': 'app_db_vault', 'configured': True}


def test_plugin_registers_read_only_tool():
    from app.abilities import ability_module
    plugin = ability_module('git_control')
    handlers = plugin.build_tools(user_id='admin')
    assert 'repository_access' in handlers
    assert 'repository_access' in plugin.TOOL_SCHEMAS
    assert 'repository_access' not in plugin.DESTRUCTIVE


def test_codebase_admin_does_not_register_git_discovery():
    from app.abilities import ability_module
    plugin = ability_module('codebase_admin')
    handlers = plugin.build_tools(user_id='admin')
    assert 'repository_access' not in handlers
    assert 'repository_access' not in plugin.TOOL_SCHEMAS
