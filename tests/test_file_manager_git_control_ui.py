from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPLORER = ROOT / "ui" / "main-panel" / "explorer"
FILES_JS = ROOT / "ui" / "shared" / "js" / "files.js"
GIT_JS = ROOT / "ui" / "shared" / "js" / "files-git.js"


def test_git_control_is_an_internal_file_manager_sidebar_mode():
    html = (EXPLORER / "explorer.html").read_text(encoding="utf-8")
    shell = FILES_JS.read_text(encoding="utf-8")

    assert 'id="files-panel-explorer"' in html
    assert 'data-explorer-mode="files"' in html
    assert 'id="files-show-git"' in html
    assert 'title="Open Git Control"' in html

    assert 'id="files-panel-git"' in html
    assert 'data-explorer-mode="git"' in html
    assert 'id="files-git-header-actions"' in html
    assert 'id="files-show-explorer"' in html
    assert 'title="Back to File Manager"' in html

    assert "p.dataset.explorerMode !== explorerMode" in shell
    assert not (ROOT / "ui" / "admin-tools" / "git" / "page.json").exists()


def test_git_control_commit_details_use_file_manager_tabs():
    explorer = (EXPLORER / "explorer-view.js").read_text(encoding="utf-8")
    git = GIT_JS.read_text(encoding="utf-8")

    assert "let commitTabs = [];" in explorer
    assert "const key = (info.repoId || 'webagent') + ':' + info.hash;" in explorer
    assert "activateCommitTab(key);" in explorer
    assert "loadCommitDetail(tab.hash)" in explorer
    assert "renderCommitDetail(tab.detail || {})" in explorer
    assert "files-commit-pane" in explorer

    assert "openGitPanel(rootEl, { onCommitSelected } = {})" in git
    assert "_commitSelectionHandler({" in git
    assert "export async function loadCommitDetail(hash)" in git
    assert "export function renderCommitDetail(d)" in git
    assert "renderGitMain" not in git


def test_commit_tabs_are_session_only_and_sidebar_mode_migrates():
    explorer = (EXPLORER / "explorer-view.js").read_text(encoding="utf-8")

    persist_start = explorer.index("function persistTabs()")
    persist_end = explorer.index("function persistExpanded()", persist_start)
    persist_source = explorer[persist_start:persist_end]
    assert "openTabs.map" in persist_source
    assert "commitTabs" not in persist_source

    assert "files.explorerSidebarMode" in explorer
    assert "legacyView === 'git'" in explorer
    assert "localStorage.setItem('files.sidebarView', 'explorer')" in explorer
    assert "stopGitAutoRefresh();" in explorer
    assert "sidebar.dataset.state === 'strip'" in explorer
