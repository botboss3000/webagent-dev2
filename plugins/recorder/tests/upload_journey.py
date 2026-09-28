"""Supplementary recorder upload stress/retry journey; isolated server Chromium."""
import asyncio
import json
import httpx
from playwright.async_api import async_playwright

BASE = "https://dev.webagent.live"

async def main():
    async with httpx.AsyncClient(base_url="http://localhost:8080") as client:
        auth=(await client.post("/api/v1/auth/login",json={"email":"admin","password":"123456"})).json()
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,args=["--no-sandbox"])
        context=await browser.new_context(viewport={"width":1440,"height":1000})
        await context.add_init_script("localStorage.setItem('auth_token',"+json.dumps(auth["access_token"])+");"
                                     "localStorage.setItem('auth_user_id',"+json.dumps(auth["user_id"])+");")
        page=await context.new_page()
        sizes=[];outage=False
        async def route(r):
            sizes.append(len(r.request.post_data_buffer or b""))
            if outage: await r.fulfill(status=503,content_type="application/json",body='{"detail":"controlled outage"}')
            else: await r.continue_()
        await page.route("**/api/v1/recordings",route)
        async def poll(expression):
            for _ in range(200):
                if await page.evaluate(expression):return
                await page.wait_for_timeout(200)
            raise AssertionError(expression)
        async def api(path):
            return await page.evaluate("""async path=>{
                const r=await fetch(path,{headers:{Authorization:'Bearer '+localStorage.getItem('auth_token')}});
                if(!r.ok)throw Error('API '+r.status);
                return r.json();
            }""",path)
        await page.goto(BASE+"/app",wait_until="domcontentloaded")
        toggle=page.locator("#render-recorder-toggle")
        await toggle.wait_for(state="visible",timeout=60000)
        await toggle.click()
        await poll("window.__renderRecorderState?.enabled===true && typeof window.__recorderTrace==='function'")
        cid=await page.evaluate("window.__renderRecorderState.recording_id")
        try:
            await page.evaluate("""()=>{
                for(let i=0;i<220;i++)window.__recorderTrace('upload.fixture',{ordinal:i,value:'😀'.repeat(10000)});
                const root=document.getElementById('chat-messages-inner') || document.getElementById('chat-messages');
                const node=document.createElement('div');node.id='upload-large-fixture';
                node.textContent='begin-of-fixture'+'完整😀'.repeat(20000)+'end-of-fixture';root.append(node);
            }""")
            await page.wait_for_timeout(8000)
            outage=True
            await page.evaluate("window.__recorderTrace('upload.fixture',{ordinal:220,value:'pending-at-stop'})")
            await toggle.click()
            await poll("window.__renderRecorderState?.enabled===false && window.__renderRecorderState?.finishing>0")
            assert await page.evaluate("typeof window.__recorderTrace==='undefined'")
            paused=await api("/api/v1/recordings/captures/"+cid)
            assert paused["capture"]["state"] in ("recording","degraded"),paused["capture"]["state"]
            outage=False
            await poll("window.__renderRecorderState?.enabled===false && window.__renderRecorderState?.finishing===0")
            detail=await api("/api/v1/recordings/captures/"+cid)
            events=[];cursor=0
            for _ in range(40):
                batch=await api(f"/api/v1/recordings/captures/{cid}/events?after={cursor}&limit=500")
                events.extend(batch["events"])
                if not batch.get("has_more"):break
                cursor=batch["next_cursor"]
            fixture=[e for e in events if e.get("label")=="DEBUG-TAG:upload.fixture"]
            assert {e["detail"]["ordinal"] for e in fixture}==set(range(221)),len(fixture)
            payloads={}
            for e in events:
                if e.get("kind")=="html.part":
                    d=e["detail"];payloads.setdefault(d["payload_id"],[]).append((d["part"],e.get("html","")))
            rebuilt=["".join(text for _,text in sorted(parts)) for parts in payloads.values()]
            assert any("begin-of-fixture" in text and "end-of-fixture" in text for text in rebuilt)
            assert max(sizes)<1024*1024,max(sizes)
            assert detail["capture"]["state"]=="completed",detail["capture"].get("gaps")
            assert not detail["capture"].get("gaps"),detail["capture"]["gaps"]
            print(json.dumps({"result":"pass","capture_id":cid,"fixture_events":len(fixture),
                "largest_upload_bytes":max(sizes),"upload_requests":len(sizes),
                "checks":["8+ MiB burst","large HTML retained","503 stops capture without finalizing",
                          "automatic retry ACKs every event","completed report has no gaps"],
                "canonical_attached_chrome":"blocked"},indent=2))
        finally:
            outage=False
            if await page.evaluate("window.__renderRecorderState?.enabled"):
                await toggle.click()
                await page.wait_for_timeout(2000)
            await browser.close()

asyncio.run(main())
