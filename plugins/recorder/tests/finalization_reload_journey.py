"""Supplementary same-document upload recovery across reload, isolated Chromium.

RECORDER-HOOK: verifies pending finalization identity and durable outbox recovery;
this controlled outage fixture is not the attached Chrome chat acceptance test.
"""
import asyncio
import json
import uuid
import httpx
from playwright.async_api import async_playwright

BASE = "https://dev.webagent.live"


async def main():
    run_id = "finalization-reload-" + uuid.uuid4().hex[:8]
    async with httpx.AsyncClient(base_url="http://localhost:8080") as client:
        auth = (await client.post("/api/v1/auth/login", json={"email": "admin", "password": "123456"})).json()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(viewport={"width": 1440, "height": 1000})
        await context.add_init_script("localStorage.setItem('auth_token'," + json.dumps(auth["access_token"]) + ");"
                                     "localStorage.setItem('auth_user_id'," + json.dumps(auth["user_id"]) + ");")
        page = await context.new_page()
        outage = False
        failed_uploads = 0
        async def route(r):
            nonlocal failed_uploads
            if outage:
                failed_uploads += 1
                await r.fulfill(status=503, content_type="application/json", body='{"detail":"controlled outage"}')
            else:
                await r.continue_()
        await page.route("**/api/v1/recordings", route)
        async def poll(expression):
            for _ in range(250):
                if await page.evaluate(expression):
                    return
                await page.wait_for_timeout(200)
            raise AssertionError(expression)
        async def api(path):
            return await page.evaluate("""async path=>{
                const r=await fetch(path,{headers:{Authorization:'Bearer '+localStorage.getItem('auth_token')}});
                if(!r.ok)throw Error('API '+r.status);return r.json();
            }""", path)
        try:
            await page.goto(BASE + "/app", wait_until="domcontentloaded")
            toggle = page.locator("#render-recorder-toggle")
            await toggle.wait_for(state="visible", timeout=60000)
            await toggle.click()
            await poll("window.__renderRecorderState?.enabled===true && typeof window.__recorderTrace==='function'")
            cid = await page.evaluate("window.__renderRecorderState.recording_id")
            outage = True
            await page.evaluate("run=>window.__recorderTrace('reload.fixture',{run_id:run,ordinal:0})", run_id)
            await toggle.click()
            await poll("window.__renderRecorderState?.enabled===false && window.__renderRecorderState?.finishing>0")
            await poll("!!sessionStorage.getItem('webagent.recorder.pending-finalization-v1')")
            # Inspect only identity: lease tokens stay inside browser storage.
            identity = await page.evaluate("""()=>{
                const items=JSON.parse(sessionStorage.getItem('webagent.recorder.pending-finalization-v1'));
                const item=Array.isArray(items)?items[0]:Object.values(items)[0];
                return {id:item.id,client_id:item.client_id};
            }""")
            assert identity["id"] == cid, identity
            await page.reload(wait_until="domcontentloaded")
            await toggle.wait_for(state="visible", timeout=60000)
            await poll("window.__renderRecorderState?.enabled===false && window.__renderRecorderState?.finishing>0")
            assert await page.evaluate("typeof window.__recorderTrace==='undefined'")
            assert failed_uploads > 0
            outage = False
            await poll("window.__renderRecorderState?.enabled===false && window.__renderRecorderState?.finishing===0")
            detail = await api("/api/v1/recordings/captures/" + cid)
            assert detail["capture"]["state"] == "completed", detail["capture"].get("gaps")
            assert not detail["capture"].get("gaps"), detail["capture"]["gaps"]
            events = []; cursor = 0
            for _ in range(10):
                batch = await api(f"/api/v1/recordings/captures/{cid}/events?after={cursor}&limit=500")
                events.extend(batch["events"])
                if not batch.get("has_more"):
                    break
                cursor = batch["next_cursor"]
            fixture = [e for e in events if e.get("label") == "DEBUG-TAG:reload.fixture"]
            assert len(fixture) == 1, len(fixture)
            assert fixture[0]["detail"]["run_id"] == run_id
            assert fixture[0]["client_id"] == identity["client_id"]
            assert not await page.evaluate("sessionStorage.getItem('webagent.recorder.pending-finalization-v1')")
            print(json.dumps({"result": "pass", "run_id": run_id, "capture_id": cid,
                              "failed_upload_requests": failed_uploads,
                              "checks": ["stopped capture stays stopped across reload", "same capture and client identity recovered",
                                         "pending event uploaded exactly once", "automatic finalization has no gaps", "pending intent cleared"],
                              "canonical_attached_chrome": "not_run (supplementary isolated Chromium)"}, indent=2))
        finally:
            outage = False
            await browser.close()


asyncio.run(main())
