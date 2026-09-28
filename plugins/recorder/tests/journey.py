"""Recorder-to-report regression, run inside webagent-dev.

python plugins/recorder/tests/journey.py --record
Optional --capture-id checks an existing report without making a recording.
Uses isolated server Chromium with the real CSP and caches; attached-user
Chrome acceptance remains blocked. No chat workload or agent fix is started.
"""
import argparse
import asyncio
import json
import time
from urllib.parse import urlsplit

import httpx
from playwright.async_api import async_playwright

BASE = "https://dev.webagent.live"


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--capture-id")
    args = parser.parse_args()
    run = {"runId": "recorder-report-" + str(int(time.time())),
           "seed": "recorder-report-csp-cache", "cases": [],
           "environment": {"url": BASE + "/app", "identity": "admin",
                           "browser": "isolated server Chromium"},
           "canonical_attached_chrome": "blocked"}
    errors, failed_requests = [], []

    async with httpx.AsyncClient(base_url="http://localhost:8080", timeout=30) as client:
        response = await client.post("/api/v1/auth/login",
                                     json={"email": "admin", "password": "123456"})
        response.raise_for_status()
        auth = response.json()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(viewport={"width":1440, "height":1000})
        await context.add_init_script(
            "localStorage.setItem('auth_token'," + json.dumps(auth["access_token"]) + ");"
            "localStorage.setItem('auth_user_id'," + json.dumps(auth["user_id"]) + ");")
        page = await context.new_page()
        page.on("pageerror", lambda e: errors.append(str(e).splitlines()[0][:200]))
        page.on("console", lambda m: errors.append(m.text[:300])
                if m.type == "error" and ("Content Security Policy" in m.text or "mount error" in m.text) else None)
        page.on("response", lambda r: failed_requests.append(
            {"status": r.status, "path": urlsplit(r.url).path}) if r.status >= 400 else None)

        async def poll(predicate, message, seconds=30):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if await predicate():
                    return
                await page.wait_for_timeout(200)
            raise AssertionError(message)

        def passed(action, expected, actual):
            run["cases"].append({"id": "S" + str(len(run["cases"]) + 1),
                                 "action": action, "expected": expected,
                                 "actual": actual, "result": "pass"})

        async def api(path):
            result = await page.evaluate("""async path => {
                const response = await fetch(path, {
                    headers: {Authorization: 'Bearer ' + localStorage.getItem('auth_token')},
                    cache: 'no-store'});
                return {status: response.status, data: await response.json()};
            }""", path)
            assert result["status"] == 200, (path, result["status"])
            return result["data"]

        async def open_reports():
            await page.locator("#main-tab-select").select_option("genui", force=True)
            root = page.locator("#genui-host #journey-reports")
            for _ in range(100):
                if await root.is_visible():
                    break
                card = page.locator(".genui-gallery-card").filter(has_text="Journey Reports")
                if await card.count() and await card.first.is_visible():
                    await card.first.click()
                    break
                await page.wait_for_timeout(200)
            await root.wait_for(state="visible", timeout=20000)
            await page.locator("#genui-host #jr-captures .jr-capture").first.wait_for(
                state="visible", timeout=20000)

        async def select_capture(cid):
            row = page.locator("#genui-host #jr-captures .jr-capture").filter(has_text=cid)
            await row.wait_for(state="visible", timeout=20000)
            await row.click()
            async def selected():
                return cid in await page.locator("#genui-host #jr-detail").inner_text()
            await poll(selected, "Selected report did not identify capture")
            assert await page.locator("#genui-host details[data-key^='check-']").count() > 0

        try:
            await page.goto(BASE + "/app", wait_until="domcontentloaded", timeout=60000)
            toggle = page.locator("#render-recorder-toggle")
            await toggle.wait_for(state="visible", timeout=60000)
            cid = args.capture_id
            if args.record:
                await toggle.click()
                await poll(lambda: page.evaluate("window.__renderRecorderState?.enabled===true"),
                           "Recording did not start")
                cid = await page.evaluate("window.__renderRecorderState.recording_id")
                await page.wait_for_timeout(2500)  # exercise the periodic upload boundary
                await toggle.click()
                await poll(lambda: page.evaluate(
                    "window.__renderRecorderState?.enabled===false && window.__renderRecorderState?.phase==='ready'"),
                    "Recording did not stop with a ready report")
                captured = await api("/api/v1/recordings/captures/" + cid)
                assert captured["capture"]["state"] == "completed", captured["capture"]["state"]
                assert not captured["capture"]["gaps"], captured["capture"]["gaps"]
                passed("Toggle recorder on and off", "Persisted completed capture without gaps", cid)
            await open_reports()
            if not cid:
                cid = (await api("/api/v1/recordings/captures"))["captures"][0]["id"]
            await select_capture(cid)
            passed("Open Journey Reports and select capture",
                   "Specific recording and evaluated checks visible", cid)
            detail = await api("/api/v1/recordings/captures/" + cid)
            assert await page.locator("#genui-host #jr-detail figure").count() == len(detail["markers"])
            passed("Compare issue markers", "UI count matches persisted markers", len(detail["markers"]))

            refresh = page.locator("#genui-host #jr-refresh")
            await refresh.focus()
            async with page.expect_response(lambda r: urlsplit(r.url).path == "/api/v1/recordings/captures"):
                await page.keyboard.press("Enter")
            await select_capture(cid)
            passed("Keyboard Refresh", "Capture and checks remain visible", cid)

            for theme in ("dark", "light"):
                await page.evaluate("(light)=>document.body.classList.toggle('light-mode',light)", theme == "light")
                await page.set_viewport_size({"width":390, "height":844})
                overflow = await page.evaluate("""()=>{
                    const host=document.getElementById('genui-host');
                    return Math.max(host.scrollWidth-host.clientWidth,
                                    document.documentElement.scrollWidth-innerWidth);
                }""")
                assert overflow <= 2, (theme, overflow)
                passed("Render " + theme + " at 390px", "No horizontal overflow", overflow)
            await page.set_viewport_size({"width":1440, "height":1000})

            await page.reload(wait_until="domcontentloaded")
            await toggle.wait_for(state="visible", timeout=60000)
            # The app can restore Account rather than GenUI; explicitly reopen
            # the same report without resetting the warm browser profile.
            await open_reports()
            await select_capture(cid)
            passed("Warm reload and reopen GenUI", "Same capture and checks visible", cid)
            assert not errors, errors
            recorder_errors = [r for r in failed_requests if r["path"].startswith("/api/v1/recordings")]
            assert not recorder_errors, recorder_errors
            passed("Inspect runtime errors", "No CSP, mount, page, or recorder API errors", [])
            run.update(result="pass", capture_id=cid)
        except Exception as error:
            run.update(result="fail", failure=type(error).__name__ + ": " + str(error)[:700])
            raise
        finally:
            run.update(page_errors=errors, http_errors=failed_requests)
            print(json.dumps(run, indent=2), flush=True)
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
