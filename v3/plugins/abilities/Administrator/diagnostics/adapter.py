"""Private fixed-origin browser execution; Rust owns authentication and approval.

Trusted cookie metadata is a separate dispatcher field, never a model argument.
There is no caller-supplied JavaScript, origin, profile or filesystem path.
"""
from __future__ import annotations
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import stat
import struct
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ORIGIN="https://dev.webagent.live"
PRIVATE_ROOT=Path("/run/v3/worker/browser")
MAX_TEXT=16384
MAX_PNG=1024*1024
MAX_ARTIFACTS=40
MAX_SESSIONS=8
MAX_PRIVATE_SESSIONS=32
MAX_ARTIFACT_BYTES=64*1024*1024
IDLE_SECONDS=180
READ_FRESH_SECONDS=30
PAGE_TIMEOUT=10
CALL_TIMEOUT=20

# Fixed native scripts only. Model text can enter a CSS selector or literal
# send_keys argument, never script source. Every browser output is untrusted.
OBSERVE_SCRIPT="""
/* V3_OBSERVE */
if (!window.__v3DiagnosticObservation) {
 const state={revision:0};
 new MutationObserver(()=>{state.revision++;}).observe(document.documentElement,
  {subtree:true,childList:true,attributes:true,characterData:true});
 window.__v3DiagnosticObservation=state;
}
return window.__v3DiagnosticObservation.revision;
"""
READ_SCRIPT=r"""
/* V3_READ */
const root=document.querySelector(arguments[0]||'body');
if (!root) return {text:'',missing:true,truncated:false};
const walk=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
let text='',count=0,truncated=false,node;
while ((node=walk.nextNode())) {
 if (++count>5000) {truncated=true;break;}
 const parent=node.parentElement;
 if (!parent || parent.closest('script,style,input,textarea,[data-secret],[data-sensitive]')) continue;
 const style=getComputedStyle(parent);
 if (style.display==='none'||style.visibility==='hidden') continue;
 let chunk=node.nodeValue.replaceAll(arguments[1],'[redacted]');
 if (text.length+chunk.length>arguments[2]) {text+=chunk.slice(0,arguments[2]-text.length);truncated=true;break;}
 text+=chunk+'\n';
}
return {text:text.slice(0,arguments[2]),missing:false,truncated:truncated};
"""
TARGET_SCRIPT="""
/* V3_TARGET */
const target=document.querySelector(arguments[0]);
const observed=document.querySelector(arguments[1]||'body');
if (!target||!observed||!observed.contains(target)) return {allowed:false};
const secret=target.matches('input[type=password],input[type=file]') || target.closest('[data-secret],[data-sensitive]')!==null ||
 /password|secret|token|credential|api.?key/i.test([target.id,target.name,target.autocomplete].join(' '));
return {allowed:true,secret:secret,href:target.closest('a[href]')?.href||'',tag:target.tagName.toLowerCase()};
"""
MASK_SCRIPT="""
/* V3_MASK */
const masks=[];
const candidates=[...document.querySelectorAll('input,textarea,[data-secret],[data-sensitive]')];
const walk=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
let node,count=0;
while ((node=walk.nextNode()) && ++count<=5000) {
 if (node.nodeValue.includes(arguments[0]) && node.parentElement) candidates.push(node.parentElement);
}
for (const node of candidates.slice(0,2000)) {
 const rect=node.getBoundingClientRect();
 if (!rect.width||!rect.height) continue;
 const mask=document.createElement('div');
 mask.setAttribute('data-v3-private-mask','1');
 Object.assign(mask.style,{position:'fixed',left:rect.left+'px',top:rect.top+'px',
  width:rect.width+'px',height:rect.height+'px',background:'#555',zIndex:'2147483647'});
 document.documentElement.appendChild(mask);masks.push(mask);
}
return masks.length;
"""
UNMASK_SCRIPT="""
/* V3_UNMASK */
document.querySelectorAll('[data-v3-private-mask]').forEach(node=>node.remove());
"""

class Rejected(Exception):pass
class Uncertain(Exception):pass
def canonical_id(value):
    try:return isinstance(value,str) and str(uuid.UUID(value))==value
    except (ValueError,AttributeError):return False
def validate_trusted(base,trusted):
    if not isinstance(trusted,dict):raise Rejected("Trusted browser authorization required")
    sid=trusted.get("session_id")
    if not canonical_id(sid) or Path(base).name!=sid:raise Rejected("Browser session ownership mismatch")
    if not isinstance(trusted.get("user_id"),str) or not re.fullmatch(r"[a-z0-9_-]{1,64}",trusted["user_id"]):
        raise Rejected("Invalid trusted caller")
    if not isinstance(trusted.get("agent_id"),str) or not re.fullmatch(r"[a-z0-9_-]{1,64}",trusted["agent_id"]):
        raise Rejected("Invalid trusted agent")
    if not canonical_id(trusted.get("cookie")):raise Rejected("Invalid trusted authentication")
    if not isinstance(trusted.get("generation"),str) or not 1<=len(trusted["generation"])<=128:
        raise Rejected("Invalid trusted generation")
    expires=trusted.get("expires_at")
    if type(expires) not in (int,float) or not time.time()<expires<=time.time()+601:
        raise Rejected("Browser authorization expired or exceeds lease")
    if trusted.get("allowed_session_ids")!=[sid]:raise Rejected("Browser routes require the current session")
    return dict(trusted)
def validate_path(path,trusted):
    if not isinstance(path,str) or not 1<=len(path)<=2048 or any(ord(c)<32 for c in path) or "\\" in path:
        raise Rejected("Invalid development route")
    parsed=urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/") or "%" in parsed.path:
        raise Rejected("Only exact relative development routes are supported")
    if "//" in parsed.path or any(part in (".","..") for part in parsed.path.split("/")):
        raise Rejected("Invalid development route")
    pairs=parse_qsl(parsed.query,keep_blank_values=True)
    if len(pairs)!=len(dict(pairs)):raise Rejected("Duplicate route parameters")
    query=dict(pairs)
    if parsed.path=="/app":
        if set(query)-{"tab","agent","agentView","session_id"}:raise Rejected("Unsupported app route parameters")
        if query.get("tab","chat") not in {"chat","agents","account"}:raise Rejected("Unsupported development page")
        if "session_id" in query and query["session_id"]!=trusted["session_id"]:raise Rejected("Another chat session is unavailable")
        if "agent" in query and query["agent"]!=trusted["agent_id"]:raise Rejected("Another agent is unavailable")
        if query.get("agentView","config") not in {"config","model","abilities","modes","prompts"}:
            raise Rejected("Unsupported agent view")
        # Avoid an unconstrained restored conversation on a bare app route.
        query.setdefault("session_id",trusted["session_id"])
        return urlunsplit(("","",parsed.path,urlencode(query),""))
    if query:raise Rejected("API navigation does not accept query parameters")
    if parsed.path=="/api/v3/auth/me":return parsed.path
    prefix="/api/v3/sessions/"+trusted["session_id"]
    if parsed.path in {prefix,prefix+"/abilities",prefix+"/mode"}:return parsed.path
    if re.fullmatch(re.escape(prefix)+r"/runs/[a-f0-9-]{36}/diagnostics",parsed.path):
        if canonical_id(parsed.path.split("/")[-2]):return parsed.path
    raise Rejected("Unsupported current-session read route")
def validate_args(name,args):
    if not isinstance(args,dict):raise Rejected("Browser arguments must be an object")
    if name in {"login_as_current_user","login_as_current_user_v3"}:
        allowed={"path"};action="login"
    elif name=="dev_browser_action":
        allowed={"action","path","selector","text","expected_sequence"};action=args.get("action")
        if action not in {"navigate","read","click","type","screenshot","close"}:raise Rejected("Unsupported browser action")
    elif name=="dev_browser_record":
        allowed={"action"};action=args.get("action")
        if action not in {"start","stop","status"}:raise Rejected("Unsupported recorder action")
    else:raise Rejected("Unknown Diagnostics browser tool")
    if set(args)-allowed:raise Rejected("Unknown browser argument")
    permitted={"login":{"path"},"navigate":{"path"},"read":{"selector"},"click":{"selector"},
        "type":{"selector","text"},"screenshot":set(),"close":set(),"start":set(),"stop":set(),"status":set()}
    for key in list(args):
        if key not in ({"action"}|permitted[action]):
            if key=="expected_sequence" and type(args[key]) is int and args[key]==0:
                del args[key];continue
            if args[key] not in ("",None):raise Rejected("Arguments do not apply to this action")
            del args[key]
    if action=="read" and args.get("selector") in ("",None):args.pop("selector",None)
    if action=="login" and args.get("path") in ("",None):args.pop("path",None)
    if "selector" in args and (not isinstance(args["selector"],str) or not 1<=len(args["selector"])<=256 or "\0" in args["selector"]):
        raise Rejected("Invalid bounded CSS selector")
    if "text" in args and (not isinstance(args["text"],str) or len(args["text"].encode())>4096 or "\0" in args["text"]):
        raise Rejected("Literal text exceeds limit")
    if action in {"click","type"} and not args.get("selector"):raise Rejected("Action requires a selector")
    if action=="type" and "text" not in args:raise Rejected("Type requires literal text")
    return action
def png_metadata(data):
    if not isinstance(data,bytes) or not 24<=len(data)<=MAX_PNG or data[:8]!=b"\x89PNG\r\n\x1a\n" or data[12:16]!=b"IHDR":
        raise Rejected("Screenshot is not a bounded PNG")
    width,height=struct.unpack(">II",data[16:24])
    if not 1<=width<=1600 or not 1<=height<=1200:raise Rejected("Screenshot dimensions exceed viewport bounds")
    return {"media_type":"image/png","bytes":len(data),"width":width,"height":height,
        "sha256":hashlib.sha256(data).hexdigest()}
def process_stamp(pid):
    try:
        text=Path("/proc")/str(pid)/"stat"
        return text.read_text().rsplit(")",1)[1].split()[19]
    except (OSError,IndexError):return None
def cleanup_prior(directory):
    record=directory/"driver.json"
    if not record.exists():return
    if record.is_symlink() or record.stat().st_size>1024:raise Rejected("Invalid browser process receipt")
    data=json.loads(record.read_text())
    pid=data.get("pid")
    if type(pid) is int and pid>1 and process_stamp(pid)==data.get("stamp"):
        try:
            if os.getpgid(pid)==pid:
                os.killpg(pid,signal.SIGTERM)
                deadline=time.monotonic()+1
                while process_stamp(pid)==data.get("stamp") and time.monotonic()<deadline:time.sleep(0.05)
                if process_stamp(pid)==data.get("stamp"):os.killpg(pid,signal.SIGKILL)
        except OSError:pass
    record.unlink()
def chrome_factory(profile,origin):
    # Selenium's debug transport logging includes cookie command bodies.
    # This private worker suppresses those libraries instead of trusting log redaction.
    for name in ("selenium","selenium.webdriver.remote.remote_connection","urllib3","urllib3.connectionpool"):
        logger=logging.getLogger(name);logger.setLevel(logging.CRITICAL);logger.propagate=False
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    parsed=urlsplit(origin);port=parsed.port or (443 if parsed.scheme=="https" else 80)
    options=webdriver.ChromeOptions()
    options.binary_location="/usr/bin/chromium"
    for argument in ("--headless=new","--no-sandbox","--disable-dev-shm-usage",
        "--disable-background-networking","--disable-extensions","--disable-sync",
        "--no-first-run","--no-default-browser-check","--disk-cache-size=1048576",
        "--media-cache-size=1048576","--window-size=1280,900"):
        options.add_argument(argument)
    options.add_argument("--user-data-dir="+str(profile))
    # Unrelated origins cannot receive browser network traffic. Only the fixed
    # origin bypasses an intentionally unavailable local proxy.
    options.add_argument("--proxy-server=http://127.0.0.1:9")
    options.add_argument("--proxy-bypass-list=<-loopback>;"+parsed.hostname+":"+str(port))
    options.add_experimental_option("prefs",{"download_restrictions":3,
        "credentials_enable_service":False,"profile.password_manager_enabled":False})
    service=Service(executable_path="/usr/bin/chromedriver",log_path="/dev/null",
        popen_kw={"start_new_session":True},
        env={"PATH":"/usr/bin:/bin","HOME":str(profile),"TMPDIR":"/tmp",
             "XDG_CONFIG_HOME":str(profile/"config"),"XDG_CACHE_HOME":str(profile/"cache"),"LANG":"C.UTF-8"})
    driver=webdriver.Chrome(service=service,options=options)
    driver.set_page_load_timeout(PAGE_TIMEOUT)
    driver.set_script_timeout(5)
    driver.set_window_size(1280,900)
    return driver

class BrowserState:
    def __init__(self,sid,user,generation,cookie,expires,driver,directory):
        self.sid=sid;self.user=user;self.generation=generation
        self.cookie=cookie;self.expires=expires;self.driver=driver;self.directory=directory
        self.lock=threading.RLock();self.cancelled=threading.Event()
        self.touched=time.monotonic();self.observed=None;self.observed_url=""
        self.observed_selector="";self.observed_at=0

class BrowserService:
    def __init__(self,private_root=PRIVATE_ROOT,*,test_origin=None,driver_factory=None):
        self.origin=ORIGIN
        if test_origin is not None:
            parsed=urlsplit(test_origin)
            if parsed.scheme!="http" or parsed.hostname not in {"127.0.0.1","localhost","::1"} or parsed.path or parsed.query or parsed.fragment:
                raise ValueError("Test browser origin must be an explicit loopback HTTP origin")
            self.origin=test_origin.rstrip("/")
        self.root=Path(private_root)
        self.factory=driver_factory or chrome_factory
        self.states={}
        self.guard=threading.RLock()
        self.recordings={}
        self.record_guards={}
    def directory(self,sid,user):
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        if self.root.is_symlink():raise Rejected("Invalid private browser root")
        directory=self.root/sid
        children=list(self.root.iterdir())
        if not directory.exists() and len(children)>=MAX_PRIVATE_SESSIONS:
            raise Rejected("Private browser storage session quota reached")
        directory.mkdir(mode=0o700,exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():raise Rejected("Invalid private browser session")
        os.chmod(directory,0o700)
        owner=directory/"owner.json"
        if owner.exists():
            if owner.is_symlink() or owner.stat().st_size>1024:raise Rejected("Invalid browser ownership record")
            if json.loads(owner.read_text()).get("user_id")!=user:raise Rejected("Private browser belongs to another caller")
        else:
            with owner.open("x") as output:json.dump({"user_id":user,"session_id":sid},output)
            os.chmod(owner,0o600)
        artifacts=directory/"artifacts";artifacts.mkdir(mode=0o700,exist_ok=True)
        if artifacts.is_symlink():raise Rejected("Invalid artifact directory")
        return directory
    def safe_url(self,state):
        parsed=urlsplit(state.driver.current_url)
        origin=urlunsplit((parsed.scheme,parsed.netloc,"","",""))
        if origin!=self.origin:raise Uncertain("Browser left the fixed development origin; inspect the action before retrying")
        return validate_path(urlunsplit(("","",parsed.path,parsed.query,"")),{
            "session_id":state.sid,"agent_id":self._agent(state)})
    def _agent(self,state):
        return state.agent
    def kill(self,state):
        state.cancelled.set();state.observed=None
        try:
            process=state.driver.service.process
            if process and process.poll() is None and os.getpgid(process.pid)==process.pid:
                os.killpg(process.pid,signal.SIGTERM)
        except (AttributeError,OSError):pass
        with contextlib.suppress(Exception):state.driver.quit()
        with contextlib.suppress(OSError):(state.directory/"driver.json").unlink()
        profile=state.directory/"chrome"
        if profile.exists() and not profile.is_symlink():
            with contextlib.suppress(OSError):shutil.rmtree(profile)
    def drop(self,sid):
        with self.guard:
            state=self.states.pop(sid,None)
            if state:self.kill(state)
    def purge(self):
        for sid,state in list(self.states.items()):
            if state.expires<=time.time() or time.monotonic()-state.touched>IDLE_SECONDS:self.drop(sid)
    def open(self,trusted):
        sid=trusted["session_id"];self.purge()
        state=self.states.get(sid)
        if state and (state.user!=trusted["user_id"]):raise Rejected("Browser caller mismatch")
        if state and (state.cookie!=trusted["cookie"] or state.generation!=trusted["generation"] or state.agent!=trusted["agent_id"]):
            self.drop(sid);state=None
        if state:return state
        if len(self.states)>=MAX_SESSIONS:raise Rejected("Browser session limit reached")
        directory=self.directory(sid,trusted["user_id"])
        cleanup_prior(directory)
        profile=directory/"chrome"
        if profile.is_symlink():raise Rejected("Invalid browser profile")
        if profile.exists():shutil.rmtree(profile) # Restart never restores an authenticated profile.
        profile.mkdir(mode=0o700)
        driver=self.factory(profile,self.origin)
        state=BrowserState(sid,trusted["user_id"],trusted["generation"],trusted["cookie"],
            trusted["expires_at"],driver,directory)
        state.agent=trusted["agent_id"]
        try:
            try:
                process=driver.service.process
                if process and process.poll() is None:
                    with (directory/"driver.json").open("x") as output:
                        json.dump({"pid":process.pid,"stamp":process_stamp(process.pid)},output)
                    os.chmod(directory/"driver.json",0o600)
            except AttributeError:pass
            driver.get(self.origin+"/health/ready")
            first=urlsplit(driver.current_url)
            if urlunsplit((first.scheme,first.netloc,"","",""))!=self.origin:raise Rejected("Initial browser origin mismatch")
            driver.add_cookie({"name":"v3_session","value":state.cookie,"path":"/","httpOnly":True,
                "secure":self.origin.startswith("https:"),"sameSite":"Strict","expiry":int(state.expires)})
            driver.get(self.origin+"/api/v3/auth/me")
            if driver.current_url!=self.origin+"/api/v3/auth/me":raise Rejected("Identity verification redirected")
            identity=json.loads(driver.find_element("tag name","body").text[:16384])
            if identity.get("user_id")!=state.user:raise Rejected("Authenticated browser identity mismatch")
            driver.get(self.origin+validate_path("/app",trusted))
            self.safe_url(state)
            self.states[sid]=state
            return state
        except Exception:
            self.kill(state)
            raise Rejected("Browser authentication failed without exposing credentials") from None
    def artifact(self,state,data):
        meta=png_metadata(data);folder=state.directory/"artifacts"
        existing=list(folder.iterdir())
        if len(existing)>=MAX_ARTIFACTS*2:raise Rejected("Private screenshot quota reached")
        total=0
        for image in self.root.glob("*/artifacts/*.png"):
            if image.is_symlink():raise Rejected("Invalid private artifact")
            total+=image.stat().st_size
        if total+len(data)>MAX_ARTIFACT_BYTES:raise Rejected("Private screenshot byte quota reached")
        artifact=str(uuid.uuid4());image=folder/(artifact+".png");manifest=folder/(artifact+".json")
        fd=os.open(image,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:
            with os.fdopen(fd,"wb",closefd=False) as output:output.write(data);output.flush()
        finally:os.close(fd)
        record={**meta,"artifact_id":artifact,"session_id":state.sid,"user_id":state.user,"created_at":int(time.time())}
        with manifest.open("x") as output:json.dump(record,output)
        os.chmod(manifest,0o600)
        return {**meta,"artifact_id":artifact,
            "path":"/api/v3/sessions/"+state.sid+"/administrator/artifacts/"+artifact}
    def screenshot(self,state):
        try:
            state.driver.execute_script(MASK_SCRIPT,state.cookie)
            return self.artifact(state,state.driver.get_screenshot_as_png())
        finally:
            with contextlib.suppress(Exception):state.driver.execute_script(UNMASK_SCRIPT)
            state.observed=None
    def fresh_target(self,state,selector):
        if state.observed is None or time.monotonic()-state.observed_at>READ_FRESH_SECONDS or state.driver.current_url!=state.observed_url:
            raise Rejected("Read the current page before each input action")
        current=state.driver.execute_script(OBSERVE_SCRIPT)
        if current!=state.observed:state.observed=None;raise Rejected("Page changed; read it again before input")
        target=state.driver.execute_script(TARGET_SCRIPT,selector,state.observed_selector)
        if not target.get("allowed") or target.get("secret"):raise Rejected("Target is unavailable or contains private credentials")
        if target.get("href"):
            parsed=urlsplit(target["href"])
            if urlunsplit((parsed.scheme,parsed.netloc,"","",""))!=self.origin:raise Rejected("External navigation is unavailable")
            validate_path(urlunsplit(("","",parsed.path,parsed.query,"")),{"session_id":state.sid,"agent_id":state.agent})
        element=state.driver.find_element("css selector",selector)
        if not element.is_displayed() or not element.is_enabled():raise Rejected("Input target is not visible and enabled")
        return element
    def run(self,name,args,base,trusted):
        trusted=validate_trusted(base,trusted);action=validate_args(name,args)
        with self.guard:
            self.purge()
            if action=="close":
                record=self.recordings.get(trusted["session_id"])
                if record:record["stop"].set()
                self.drop(trusted["session_id"]);return {"status":"success","closed":True}
            state=self.open(trusted)
        with state.lock:
            if state.cancelled.is_set() or state.expires<=time.time():raise Rejected("Browser authorization expired")
            state.touched=time.monotonic();self.safe_url(state)
            if action in {"login","navigate"}:
                route=validate_path(args.get("path","/app"),trusted)
                state.observed=None;state.driver.get(self.origin+route);self.safe_url(state)
                result={"path":route,"identity_verified":True,"user_id":state.user}
            elif action=="read":
                before=state.driver.execute_script(OBSERVE_SCRIPT)
                result=state.driver.execute_script(READ_SCRIPT,args.get("selector","body"),state.cookie,MAX_TEXT)
                if not isinstance(result,dict) or not isinstance(result.get("text"),str):raise Rejected("Invalid bounded DOM result")
                result["text"]=result["text"][:MAX_TEXT].replace(state.cookie,"[redacted]")
                after=state.driver.execute_script(OBSERVE_SCRIPT)
                if before==after and not result.get("truncated") and not result.get("missing"):
                    state.observed=after;state.observed_url=state.driver.current_url
                    state.observed_selector=args.get("selector","body");state.observed_at=time.monotonic()
                else:state.observed=None
                result["fresh_for_input"]=state.observed is not None
            elif action in {"click","type"}:
                target=self.fresh_target(state,args["selector"]);state.observed=None
                try:
                    if action=="click":target.click()
                    else:target.send_keys(args["text"])
                    self.safe_url(state)
                except Exception:raise Uncertain("Input outcome is uncertain; inspect the page before another action") from None
                result={"action":action,"applied":True,"read_required":True}
            elif action=="screenshot":result=self.screenshot(state)
            else:raise Rejected("Recorder actions use the recorder interface")
            if state.cancelled.is_set():raise Uncertain("Browser action cancelled; inspect the outcome before retrying")
            return {"status":"success","application":"v3","backend":"private_chromium","session_id":state.sid,
                "source_kind":"untrusted_browser_observation",**result}
    async def call(self,name,args,base,trusted):
        sid=trusted.get("session_id") if isinstance(trusted,dict) else None
        operation=asyncio.create_task(asyncio.to_thread(self.run,name,args,base,trusted))
        try:return await asyncio.wait_for(asyncio.shield(operation),CALL_TIMEOUT)
        except asyncio.CancelledError:
            if isinstance(sid,str):await asyncio.to_thread(self.drop,sid)
            raise
        except asyncio.TimeoutError:
            if isinstance(sid,str):await asyncio.to_thread(self.drop,sid)
            return {"status":"uncertain","error_kind":"browser_timeout","output":"Browser operation timed out; inspect before retrying"}
        except Uncertain:
            if isinstance(sid,str):await asyncio.to_thread(self.drop,sid)
            return {"status":"uncertain","error_kind":"browser_uncertain","output":"Browser changed origin or input outcome is uncertain; no automatic replay"}
        except Exception:
            return {"status":"error","error_kind":"browser_rejected","output":"Browser request rejected or backend unavailable; credentials were not returned"}
        finally:
            if not operation.done():
                operation.add_done_callback(lambda task:task.exception() if not task.cancelled() else None)
    async def record_loop(self,base,trusted,record):
        try:
            for _ in range(30):
                if record["stop"].is_set() or time.time()>=trusted["expires_at"]:break
                await asyncio.sleep(1)
                if record["stop"].is_set():break
                result=await self.call("dev_browser_action",{"action":"screenshot"},base,trusted)
                if result.get("status")!="success":
                    record["status"]="stopped";break
                record["artifacts"].append({k:result[k] for k in ("artifact_id","path","media_type","bytes","width","height","sha256")})
            record["status"]="stopped"
        except asyncio.CancelledError:record["status"]="stopped"
        finally:record["completed_at"]=int(time.time())
    async def record(self,args,base,trusted):
        sid=trusted.get("session_id") if isinstance(trusted,dict) else None
        if not canonical_id(sid):
            return {"status":"error","error_kind":"browser_record_rejected","output":"Recorder request rejected"}
        lock=self.record_guards.setdefault(sid,asyncio.Lock())
        async with lock:return await self._record(args,base,trusted)
    async def _record(self,args,base,trusted):
        try:
            trusted=validate_trusted(base,trusted);action=validate_args("dev_browser_record",args);sid=trusted["session_id"]
            record=self.recordings.get(sid)
            if record and record["user"]!=trusted["user_id"]:raise Rejected("Recorder caller mismatch")
            if record and (record["cookie"]!=trusted["cookie"] or record["generation"]!=trusted["generation"]):
                record["stop"].set();record["task"].cancel()
                with contextlib.suppress(asyncio.CancelledError):await record["task"]
                await asyncio.to_thread(self.drop,sid)
                record["status"]="stopped"
            if action=="start":
                if record and record["status"]=="recording":raise Rejected("Recorder already running")
                opened=await self.call("login_as_current_user",{},base,trusted)
                if opened.get("status")!="success":return opened
                record={"user":trusted["user_id"],"generation":trusted["generation"],"cookie":trusted["cookie"],
                    "status":"recording","artifacts":[],"started_at":int(time.time()),
                    "stop":asyncio.Event()}
                self.recordings[sid]=record
                record["task"]=asyncio.create_task(self.record_loop(base,trusted,record))
            elif action=="stop" and record:
                record["stop"].set()
                await record["task"]
            if record is None:return {"status":"success","recording":False,"frames":0,"artifacts":[]}
            return {"status":"success","recording":record["status"]=="recording","frames":len(record["artifacts"]),
                "artifacts":list(record["artifacts"]),"started_at":record["started_at"],
                "completed_at":record.get("completed_at"),"format":"bounded_png_frames",
                "max_frames":30,"max_seconds":30,"video":False}
        except Exception:return {"status":"error","error_kind":"browser_record_rejected","output":"Recorder request rejected"}
    def read_artifact(self,base,trusted,artifact):
        trusted=validate_trusted(base,trusted)
        if not canonical_id(artifact):raise Rejected("Invalid artifact ID")
        folder=self.directory(trusted["session_id"],trusted["user_id"])/"artifacts"
        meta=folder/(artifact+".json");image=folder/(artifact+".png")
        if meta.is_symlink() or meta.stat().st_size>2048:raise Rejected("Invalid artifact record")
        record=json.loads(meta.read_text())
        if record.get("user_id")!=trusted["user_id"] or record.get("session_id")!=trusted["session_id"]:
            raise Rejected("Artifact ownership mismatch")
        fd=os.open(image,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>MAX_PNG:raise Rejected("Invalid private artifact")
            data=os.read(fd,MAX_PNG+1)
        finally:os.close(fd)
        actual=png_metadata(data)
        if any(record.get(k)!=actual[k] for k in actual):raise Rejected("Artifact digest changed")
        return data
    async def close_all(self,base):
        sid=Path(base).name
        if not canonical_id(sid):return
        record=self.recordings.get(sid)
        if record:
            record["stop"].set();record["task"].cancel()
            with contextlib.suppress(asyncio.CancelledError):await record["task"]
        await asyncio.to_thread(self.drop,sid)

_SERVICE=BrowserService()
async def execute(name,args,base,trusted):
    if name=="dev_browser_record":return await _SERVICE.record(args,base,trusted)
    return await _SERVICE.call(name,args,base,trusted)
def read_artifact(base,trusted,artifact):
    return _SERVICE.read_artifact(base,trusted,artifact)
async def close_all(base):
    await _SERVICE.close_all(base)
