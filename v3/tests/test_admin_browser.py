"""Protocol boundaries use mock WebDriver; optional real Chromium local fixture."""
import asyncio
import base64
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from unittest import mock

spec=importlib.util.spec_from_file_location("v3_admin_browser",Path(__file__).parents[1]/"plugins/abilities/Administrator/diagnostics/adapter.py")
browser=importlib.util.module_from_spec(spec)
spec.loader.exec_module(browser)
PNG=base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aJ4cAAAAASUVORK5CYII=")
class Element:
    def __init__(self,driver,selector):self.driver=driver;self.selector=selector
    @property
    def text(self):return json.dumps({"user_id":self.driver.identity})
    def is_displayed(self):return True
    def is_enabled(self):return True
    def click(self):
        self.driver.clicks+=1;self.driver.revision+=1
        if self.driver.escape_on_click:self.driver.current_url="https://outside.invalid/"
    def send_keys(self,text):self.driver.typed.append(text);self.driver.revision+=1
class Driver:
    def __init__(self):self.current_url="";self.identity="admin";self.cookies=[];self.revision=0
    clicks=0
    escape_on_click=False
    closed=False
    failure=None
    def get(self,url):self.current_url=url;self.revision+=1
    def add_cookie(self,cookie):self.cookies.append(cookie)
    def find_element(self,by,selector):
        if self.failure:raise RuntimeError(self.failure)
        return Element(self,selector)
    def execute_script(self,script,*args):
        if "V3_OBSERVE" in script:return self.revision
        if "V3_READ" in script:return {"text":"Visible bounded text "+args[1],"missing":False,"truncated":False}
        if "V3_TARGET" in script:
            return {"allowed":True,"secret":args[0]=="#password","href":
                "https://outside.invalid/" if args[0]=="#external" else "","tag":"input"}
        if "V3_MASK" in script:self.revision+=1;return 1
        if "V3_UNMASK" in script:self.revision+=1;return None
        raise AssertionError("Unexpected native script")
    def get_screenshot_as_png(self):return PNG
    def quit(self):self.closed=True
class BrowserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.sid=str(uuid.uuid4());self.base=self.root/"workspace"/self.sid;self.base.mkdir(parents=True)
        self.trusted={"session_id":self.sid,"user_id":"admin","agent_id":"shared_default",
            "cookie":str(uuid.uuid4()),"generation":"first","expires_at":time.time()+300,
            "allowed_session_ids":[self.sid]}
        self.drivers=[]
        def factory(profile,origin):
            driver=Driver();driver.typed=[];self.drivers.append(driver);return driver
        self.service=browser.BrowserService(self.root/"private",driver_factory=factory)
    async def asyncTearDown(self):
        await self.service.close_all(self.base)
        self.temp.cleanup()
    async def call(self,name="dev_browser_action",**args):
        return await self.service.call(name,args,self.base,self.trusted)
    async def test_optional_action_schema_defaults_preserve_boundaries(self):
        self.assertEqual((await self.call("login_as_current_user",path=""))["status"],"success")
        for action in ("read","screenshot","close"):
            result=await self.call(action=action,path="",selector="",text="")
            self.assertEqual(result["status"],"success",(action,result))
        result=await self.call(action="navigate",path="/api/v3/auth/me",selector="",text=None)
        self.assertEqual(result["status"],"success",result)
        for args in [{"action":"screenshot","path":"/app","selector":"","text":""},
                     {"action":"read","path":"","selector":"","text":"substantive"},
                     {"action":"click","path":"","selector":"","text":""},
                     {"action":"navigate","path":"","selector":"","text":""},
                     {"action":"close","unknown":""}]:
            self.assertEqual((await self.call(**args))["status"],"error",args)
        args={"action":"read","selector":None,"path":None,"text":None}
        self.assertEqual(browser.validate_args("dev_browser_action",args),"read")
        self.assertEqual(browser.validate_args("dev_browser_action",{"action":"read","expected_sequence":0}),"read")
        with self.assertRaises(browser.Rejected):
            browser.validate_args("dev_browser_action",{"action":"read","expected_sequence":1})
        self.assertEqual(args,{"action":"read"})
    def test_native_read_script_preserves_javascript_newline_escape(self):
        self.assertIn("text+=chunk+'\\n';",browser.READ_SCRIPT)
        self.assertNotIn("text+=chunk+'\n';",browser.READ_SCRIPT)
    async def test_login_private_cookie_fixed_origin_and_current_routes(self):
        result=await self.call("login_as_current_user",path="/app?tab=agents&agent=shared_default&agentView=abilities")
        self.assertEqual(result["status"],"success")
        self.assertNotIn(self.trusted["cookie"],json.dumps(result))
        self.assertEqual(self.drivers[0].cookies[0]["name"],"v3_session")
        self.assertTrue(self.drivers[0].cookies[0]["httpOnly"])
        self.assertTrue(self.drivers[0].cookies[0]["secure"])
        self.assertNotIn(self.base,self.service.states[self.sid].directory.parents)
        for path in ["https://outside.invalid/","//outside.invalid/","file:///etc/passwd",
                     "/app?session_id="+str(uuid.uuid4()),"/app?tab=agents&agent=another",
                     "/api/v3/sessions/"+str(uuid.uuid4()),"/app?tab=account&cookie=x"]:
            result=await self.call(action="navigate",path=path)
            self.assertEqual(result["status"],"error",path)
        self.assertEqual((await self.call(action="navigate",path="/api/v3/auth/me"))["status"],"success")
    async def test_fresh_read_before_each_input_and_secret_targets_blocked(self):
        self.assertEqual((await self.call(action="click",selector="#button"))["status"],"error")
        read=await self.call(action="read")
        self.assertEqual(read["status"],"success")
        self.assertNotIn(self.trusted["cookie"],read["text"])
        self.assertTrue(read["fresh_for_input"])
        self.assertEqual((await self.call(action="click",selector="#button"))["status"],"success")
        self.assertEqual((await self.call(action="type",selector="#text",text="x"))["status"],"error")
        await self.call(action="read")
        self.drivers[0].revision+=1
        self.assertEqual((await self.call(action="click",selector="#button"))["status"],"error")
        await self.call(action="read")
        self.assertEqual((await self.call(action="type",selector="#password",text="secret"))["status"],"error")
        await self.call(action="read")
        self.assertEqual((await self.call(action="click",selector="#external"))["status"],"error")
        await self.call(action="read")
        self.assertEqual((await self.call(action="type",selector="#text",text="literal"))["status"],"success")
        self.assertEqual(self.drivers[0].typed,["literal"])
    async def test_input_escape_is_uncertain_not_replayed_and_closes_profile(self):
        await self.call(action="read")
        self.drivers[0].escape_on_click=True
        result=await self.call(action="click",selector="#button")
        self.assertEqual(result["status"],"uncertain")
        self.assertEqual(self.drivers[0].clicks,1)
        self.assertTrue(self.drivers[0].closed)
        self.assertNotIn(self.sid,self.service.states)
    async def test_artifacts_are_real_private_pngs_owner_checked_and_bounded(self):
        screenshot=await self.call(action="screenshot")
        self.assertEqual(screenshot["status"],"success")
        self.assertEqual(screenshot["media_type"],"image/png")
        self.assertEqual(screenshot["width"],1)
        self.assertNotIn(str(self.root),json.dumps(screenshot))
        self.assertEqual(self.service.read_artifact(self.base,self.trusted,screenshot["artifact_id"]),PNG)
        changed=dict(self.trusted,user_id="other")
        with self.assertRaises(browser.Rejected):
            self.service.read_artifact(self.base,changed,screenshot["artifact_id"])
        with self.assertRaises(browser.Rejected):
            self.service.read_artifact(self.base,self.trusted,"../secret")
        with mock.patch.object(browser,"MAX_PNG",24):
            result=await self.call(action="screenshot")
            self.assertEqual(result["status"],"error")
    async def test_rotation_expiry_unknown_fields_and_errors_never_echo_cookie(self):
        await self.call("login_as_current_user")
        original=self.drivers[0]
        self.trusted["cookie"]=str(uuid.uuid4());self.trusted["generation"]="second"
        self.assertEqual((await self.call(action="read"))["status"],"success")
        self.assertTrue(original.closed)
        self.drivers[-1].failure=self.trusted["cookie"]
        result=await self.call(action="click",selector="#button")
        self.assertNotIn(self.trusted["cookie"],json.dumps(result))
        self.assertEqual((await self.call(action="evaluate",js="return document.cookie"))["status"],"error")
        self.assertEqual((await self.call(action="read",user_id="foreign"))["status"],"error")
        self.trusted["expires_at"]=time.time()-1
        self.assertEqual((await self.call(action="read"))["status"],"error")
    async def test_recorder_has_real_bounded_frames_and_cancellation_closes(self):
        start=await self.service.record({"action":"start"},self.base,self.trusted)
        self.assertTrue(start["recording"])
        await asyncio.sleep(1.1)
        stopped=await self.service.record({"action":"stop"},self.base,self.trusted)
        self.assertFalse(stopped["recording"])
        self.assertGreaterEqual(stopped["frames"],1)
        self.assertFalse(stopped["video"])
        self.assertEqual(stopped["format"],"bounded_png_frames")
        self.assertEqual(self.service.read_artifact(self.base,self.trusted,stopped["artifacts"][0]["artifact_id"]),PNG)
        state=self.service.states[self.sid]
        await self.service.close_all(self.base)
        self.assertTrue(state.cancelled.is_set())
        self.assertTrue(state.driver.closed)
        self.assertNotIn(self.sid,self.service.states)
    async def test_identity_mismatch_and_profile_symlink_fail_closed(self):
        def wrong(profile,origin):
            driver=Driver();driver.identity="another-user";driver.typed=[];self.drivers.append(driver);return driver
        self.service.factory=wrong
        result=await self.call("login_as_current_user")
        self.assertEqual(result["status"],"error")
        self.assertTrue(self.drivers[-1].closed)
        folder=self.root/"private"/self.sid/"chrome"
        if folder.exists():
            import shutil;shutil.rmtree(folder)
        folder.symlink_to(self.base,target_is_directory=True)
        self.assertEqual((await self.call("login_as_current_user"))["status"],"error")
        self.assertEqual(list(self.base.iterdir()),[])

@unittest.skipUnless(os.environ.get("V3_BROWSER_REAL_TEST")=="1","requires image Chromium/Selenium; run explicitly")
class RealBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_local_fixture_identity_dom_input_and_png(self):
        sid=str(uuid.uuid4());cookie=str(uuid.uuid4())
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                if self.path=="/api/v3/auth/me":
                    body=json.dumps({"user_id":"admin" if self.headers.get("Cookie")=="v3_session="+cookie else "unauthenticated"}).encode()
                    media="application/json"
                elif self.path.startswith("/app"):
                    body=b'<html><body><p id="text">Fixture ready</p><button id="go" onclick="document.getElementById(\'text\').textContent=\'Clicked\'">Go</button><input id="password" type="password" value="PRIVATE_VALUE"></body></html>'
                    media="text/html"
                else:body=b"ready";media="text/plain"
                self.send_response(200);self.send_header("Content-Type",media);self.end_headers();self.wfile.write(body)
        server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);base=root/"workspace"/sid;base.mkdir(parents=True)
            trusted={"session_id":sid,"user_id":"admin","agent_id":"shared_default","cookie":cookie,
                "generation":"fixture","expires_at":time.time()+300,"allowed_session_ids":[sid]}
            service=browser.BrowserService(root/"private",test_origin="http://127.0.0.1:"+str(server.server_port))
            try:
                login=await service.call("login_as_current_user",{},base,trusted)
                self.assertEqual(login["status"],"success",login)
                self.assertNotIn(cookie,json.dumps(login))
                read=await service.call("dev_browser_action",{"action":"read"},base,trusted)
                self.assertEqual(read["status"],"success",read)
                self.assertIn("Fixture ready",read["text"])
                self.assertNotIn("PRIVATE_VALUE",read["text"])
                clicked=await service.call("dev_browser_action",{"action":"click","selector":"#go"},base,trusted)
                self.assertEqual(clicked["status"],"success",clicked)
                read=await service.call("dev_browser_action",{"action":"read"},base,trusted)
                self.assertIn("Clicked",read["text"])
                shot=await service.call("dev_browser_action",{"action":"screenshot"},base,trusted)
                self.assertEqual(shot["status"],"success",shot)
                image=service.read_artifact(base,trusted,shot["artifact_id"])
                self.assertTrue(image.startswith(b"\x89PNG"))
            finally:await service.close_all(base)
        server.shutdown();server.server_close();thread.join()

if __name__=="__main__":unittest.main()
