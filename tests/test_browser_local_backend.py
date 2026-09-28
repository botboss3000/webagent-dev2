import asyncio
import os
from pathlib import Path
from unittest.mock import patch

from app.tools import browser


def test_find_chrome_recognizes_playwright_container_install(tmp_path: Path):
    executable = tmp_path / "chromium-1234" / "chrome-linux64" / "chrome"
    executable.parent.mkdir(parents=True)
    executable.touch()

    with (
        patch.object(browser.sys, "platform", "linux"),
        patch.object(browser.shutil, "which", return_value=None),
        patch.dict(
            os.environ,
            {
                "PLAYWRIGHT_BROWSERS_PATH": str(tmp_path),
                "WEBAGENT_LOCAL_BROWSER_PATH": "",
            },
            clear=False,
        ),
    ):
        assert browser._find_chrome() == str(executable)


def test_local_backend_reports_missing_display_not_missing_browser():
    with (
        patch.object(browser.sys, "platform", "linux"),
        patch.object(browser, "_find_chrome", return_value="/installed/chromium"),
        patch.object(browser, "_cdp_endpoint_open", return_value=False),
        patch.dict(
            os.environ,
            {"DISPLAY": "", "WAYLAND_DISPLAY": ""},
            clear=False,
        ),
    ):
        result = asyncio.run(browser._launch_local_chrome())

    assert result["ok"] is False
    assert result["error"] == "no_display"
    assert "installed for the headless Playwright backend" in result["message"]
