"""Browser-to-report wiring regression with a visibly labelled synthetic transcript.

Run in webagent-dev: python plugins/recorder/tests/measurement_journey.py
Uses real recorder observers, uploads, rules, controls, and socket reconnect.
No model workload or application source mutation occurs inside the browser.
This isolated server Chromium fixture is not attached-user journey acceptance.
"""
import asyncio
import json
import time
import httpx
from playwright.async_api import async_playwright

BASE = "https://dev.webagent.live"

async def main():
    async with httpx.AsyncClient(base_url="http://localhost:8080") as client:
        login = await client.post("/api/v1/auth/login", json={"email":"admin","password":"123456"})
        login.raise_for_status()
        auth = login.json()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(viewport={"width":1440,"height":1000})
        await context.add_init_script("localStorage.setItem('auth_token',"+json.dumps(auth["access_token"])+");"
                                     "localStorage.setItem('auth_user_id',"+json.dumps(auth["user_id"])+");")
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e).splitlines()[0][:160]))
        cid = None
        async def api(path):
            return await page.evaluate("""async path => {
                const response=await fetch(path,{headers:{Authorization:'Bearer '+localStorage.getItem('auth_token')}});
                if(!response.ok)throw Error('Recorder API '+response.status);
                return response.json();
            }""", path)
        async def poll(expression, timeout=30000):
            deadline=time.monotonic()+timeout/1000
            while time.monotonic()<deadline:
                if await page.evaluate(expression): return
                await page.wait_for_timeout(200)
            raise AssertionError("Browser condition did not settle: "+expression)
        try:
            await page.goto(BASE+"/app", wait_until="domcontentloaded")
            toggle = page.locator("#render-recorder-toggle")
            await toggle.wait_for(state="visible", timeout=60000)
            # The app's asynchronous footer configuration relocates controls
            # by ID during boot. Let that finish before installing fixture IDs.
            await page.wait_for_timeout(5000)
            # A dedicated synthetic root prevents unrelated history/virtualization
            # from defining this contract fixture's independent expected values.
            await page.evaluate("""async () => {
                const {app}=await import('/ui/shared/js/state.js');
                const old=document.getElementById('chat-messages-inner');
                if(old)old.id='measurement-original-transcript';
                const root=document.createElement('div');root.id='chat-messages-inner';
                root.dataset.recorderFixture='synthetic-measurement';
                root.style.cssText='position:fixed;left:60px;top:130px;width:650px;height:600px;overflow:auto;z-index:9999;background:var(--bg-elev);color:var(--fg-1);padding:16px';
                root.innerHTML='<strong>Synthetic recorder wiring fixture</strong>';
                document.body.prepend(root);app._chatScroller=root;app.isProcessing=false;
                if(!app.currentSessionId)app.currentSessionId=crypto.randomUUID();
                const stop=document.getElementById('chat-stop-btn');
                if(!stop || !app.refreshRunControls)throw Error('Chat run controls not initialized');
                app._runControlsActive=false;app.refreshRunControls();
                const user=document.createElement('div');user.className='chat-bubble user turn-anchor';
                user.dataset.msgId='fixture-user';user.dataset.sessionId=app.currentSessionId;
                user.dataset.sessionSeq='1';user.innerHTML='<div class="bubble-body">Synthetic user</div>';
                root.append(user);app.getUserTurnAnchorState=()=>({active:user.classList.contains('turn-anchor'),released_by_user:false});document.body.classList.remove('light-mode');
            }""")
            await toggle.click()
            await poll("window.__renderRecorderState?.enabled===true && typeof window.__recorderTrace==='function'")
            cid = await page.evaluate("window.__renderRecorderState.recording_id")
            await page.evaluate("""async () => {
                const {app}=await import('/ui/shared/js/state.js');
                window.__recorderTrace('run.started',{session_id:app.currentSessionId,run_id:'fixture-user'});
                app.isProcessing=true;app.beginRunControls('fixture-user');
                const root=document.getElementById('chat-messages-inner');
                const group=document.createElement('div');group.id='measurement-group';
                group.className='chat-bubble agent activity-group';group.dataset.activityTurnId='fixture-user';
                group.dataset.runKey='fixture-user:1';group.dataset.sessionId=app.currentSessionId;
                group.innerHTML='<div class="live-activity-note">Synthetic meaningful progress</div><span class="bubble-tool-calls-label">0 tools</span><div class="bubble-tool-calls-panel" style="display:block"></div>';
                root.append(group);
            }""")
            await page.wait_for_timeout(700)
            await page.evaluate("""() => {
                const group=document.getElementById('measurement-group');
                group.querySelector('.bubble-tool-calls-panel').innerHTML='<div class="ca-tool-row" data-detail-msg-id="fixture-tool"><div class="ca-activity-entry-body">Synthetic tool content</div></div>';
                group.querySelector('.bubble-tool-calls-label').textContent='1 tool / 1 message';
                const source=document.createElement('div');
                source.className='ca-tool-row ca-response-row';source.dataset.detailMsgId='fixture-preview-source';
                source.innerHTML='<div class="ca-activity-entry-body">Complete synthetic preview</div>';
                source.__entry={_finalized:true};
                group.querySelector('.bubble-tool-calls-panel').append(source);
                const preview=source.cloneNode(true);preview.classList.add('bubble-tool-calls-preview');
                preview.__sourceRow=source;preview.__entry=source.__entry;
                group.append(preview);
                window.__recorderTrace('cache.stream.monotonic',{interaction_id:'fixture-tool',decision:'apply',incoming_length:30,current_length:0});
                const control=document.createElement('button');control.id='chat-measurement-button';
                control.textContent='Synthetic measurement control';control.setAttribute('aria-label','Synthetic measurement control');
                control.style.cssText='display:block;outline:2px solid currentColor';
                document.getElementById('chat-messages-inner').append(control);
            }""")
            await page.wait_for_timeout(700)
            control = page.locator("#chat-measurement-button")
            await control.click()
            await control.press("Enter")
            await page.evaluate("document.body.classList.add('light-mode')")
            await page.wait_for_timeout(700)
            await page.evaluate("""async () => {
                const {app}=await import('/ui/shared/js/state.js');
                const closer=document.createElement('div');closer.className='chat-bubble agent terminal-closer';
                closer.dataset.msgId='fixture-final';closer.dataset.ownerRun='fixture-user';
                closer.dataset.sessionId=app.currentSessionId;closer.dataset.sessionSeq='2';
                closer.innerHTML='<div class="bubble-body">Synthetic final answer</div>';
                document.getElementById('chat-messages-inner').append(closer);
                app.isProcessing=false;app.endRunControls('fixture-user');
                window.__recorderTrace('run.completed',{session_id:app.currentSessionId,run_id:'fixture-user'});
                const notice=document.createElement('div');notice.className='system-compaction-transient';
                notice.dataset.compactionInteractionId='fixture-compaction';notice.textContent='Synthetic compaction notice';
                document.getElementById('chat-messages-inner').append(notice);
                setTimeout(()=>notice.remove(),2000);
            }""")
            await page.wait_for_timeout(3000)
            # Reconnect the fixture browser's actual transport. The recorder
            # must see production connection hooks and compare the surviving DOM.
            await poll("import('/ui/shared/js/state.js').then(({app})=>app.agentWs?.readyState===1)")
            await page.evaluate("import('/ui/shared/js/state.js').then(({app})=>app.agentWs.close())")
            await poll("import('/ui/shared/js/state.js').then(({app})=>app.agentWs?.readyState===1)")
            await page.wait_for_timeout(2500)
            await toggle.click()
            await poll("window.__renderRecorderState?.enabled===false && ['ready','degraded'].includes(window.__renderRecorderState?.phase)")
            detail = await api("/api/v1/recordings/captures/"+cid)
            checks = {x["id"]:x for x in detail["report"]["checks"]}
            statuses = {key:value["result"] for key,value in checks.items()}
            events, cursor = [], 0
            for _ in range(10):
                batch = await api(f"/api/v1/recordings/captures/{cid}/events?after={cursor}&limit=500")
                events.extend(batch["events"])
                if not batch.get("has_more"): break
                cursor = batch["next_cursor"]
            kinds = {e.get("kind") for e in events}
            assert {"checkpoint","journey.keyboard","journey.recovery","compaction.notice"} <= kinds, sorted(kinds)
            print(json.dumps({"runId":"measurement-"+str(int(time.time())),"capture_id":cid,
                "fixture":"synthetic browser-to-report integration","canonical_attached_chrome":"blocked",
                "state":detail["capture"]["state"],"gaps":detail["capture"].get("gaps",[]),
                "checks":statuses,"unpassed":{k:{"actual":v["actual"],"limitations":v["limitations"]} for k,v in checks.items() if v["result"]!="pass"},
                "page_errors":errors},indent=2))
            # Footer configuration can reveal the actual Stop control while
            # the synthetic session is idle. Assert that this observed violation
            # is reported, rather than weakening the rule to force a green page.
            stop_samples = [e["detail"] for e in events if e.get("kind") == "checkpoint"
                            and e.get("detail", {}).get("run_state") in ("idle", "running", "completed")]
            assert stop_samples, "No sampled Stop evidence"
            stop_violation = any(sample.get("stop_visible") != (sample["run_state"] == "running")
                                 for sample in stop_samples)
            assert statuses["controls.stop"] == ("fail" if stop_violation else "pass"), statuses
            assert all(value=="pass" for key,value in statuses.items() if key!="controls.stop"), statuses
            assert not errors, errors
            print("PASS all requirement producers/evaluators; Stop verdict agrees with observed lifecycle")
        except Exception:
            print(json.dumps({"capture_id":cid,"page_errors":errors,
                "recorder":await page.evaluate("({phase:window.__renderRecorderState?.phase,enabled:window.__renderRecorderState?.enabled,gaps:window.__renderRecorderState?.gaps,trace:typeof window.__recorderTrace})")}))
            raise
        finally:
            if await page.evaluate("window.__renderRecorderState?.enabled===true"):
                await page.locator("#render-recorder-toggle").click()
                await page.wait_for_timeout(2000)
            await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
