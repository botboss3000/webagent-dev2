"""Ephemeral authenticated workspace previews; permanent development tunnel untouched."""
import asyncio,ctypes,hmac,inspect,os,re,secrets,shutil,signal,tempfile,time
from pathlib import Path
from http.cookies import SimpleCookie
LIMIT=1024*1024
COOKIE="__Host-v3_preview"
VALIDATOR=None
EXECUTABLE="/usr/local/bin/cloudflared"
LEASES={}
LOCKS={}
BLOCKED={8080,8081,8082}
HOP={"connection","keep-alive","proxy-authenticate","proxy-authorization","te","trailer","transfer-encoding","upgrade"}
def configure(validator,executable=None):
 global VALIDATOR,EXECUTABLE
 VALIDATOR=validator
 if executable is not None:EXECUTABLE=str(executable)
def base_path(base):
 p=Path(base)
 if p.is_symlink():raise ValueError("Symlink workspace unavailable")
 return p.resolve(strict=True)
async def allowed(base,port):
 if VALIDATOR is None or port in BLOCKED:return False
 result=VALIDATOR(base,port)
 return bool(await result if inspect.isawaitable(result) else result)
async def kill(process):
 if process is None or process.returncode is not None:return
 try:os.killpg(process.pid,signal.SIGTERM)
 except ProcessLookupError:pass
 try:await asyncio.wait_for(process.wait(),3)
 except asyncio.TimeoutError:
  try:os.killpg(process.pid,signal.SIGKILL)
  except ProcessLookupError:pass
  await process.wait()
def parent_guard(parent_pid):
 # Linux worker death must also terminate the isolated connector.
 libc=ctypes.CDLL(None,use_errno=True)
 if libc.prctl(1,signal.SIGKILL,0,0,0)!=0:raise OSError("Parent death guard unavailable")
 if os.getppid()!=parent_pid:os._exit(125)
async def headers(reader):
 raw=await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"),10)
 if len(raw)>16384:raise ValueError("Headers exceed limit")
 lines=raw[:-4].decode("latin1").split("\r\n")
 first=lines[0];result={}
 for line in lines[1:]:
  key,sep,value=line.partition(":")
  key=key.lower()
  if not sep or not re.fullmatch("[!#$%&'*+.^_A-Za-z0-9|~-]+",key) or key in result:raise ValueError("Invalid repeated header")
  if any(ord(c)<32 and c!="\t" or ord(c)==127 for c in value):raise ValueError("Invalid header value")
  result[key]=value.strip()
 return first,result
async def response(writer,status,body=b"",extra=()):
 phrase={200:"OK",302:"Found",400:"Bad Request",403:"Forbidden",410:"Gone",413:"Payload Too Large",429:"Too Many Requests",502:"Bad Gateway",503:"Service Unavailable"}.get(status,"Response")
 writer.write(("HTTP/1.1 "+str(status)+" "+phrase+"\r\nContent-Length: "+str(len(body))+"\r\nConnection: close\r\nCache-Control: no-store\r\nReferrer-Policy: no-referrer\r\n"+ "".join(k+": "+v+"\r\n" for k,v in extra)+"\r\n").encode("latin1")+body)
 await asyncio.wait_for(writer.drain(),10)
async def read_body(reader,values):
 if "transfer-encoding" in values:
  if values["transfer-encoding"].lower()!="chunked" or "content-length" in values:raise ValueError("Unsupported response framing")
  out=bytearray()
  while True:
   line=await asyncio.wait_for(reader.readline(),10)
   if len(line)>128:raise ValueError("Chunk framing exceeds limit")
   size=int(line.split(b";",1)[0].strip(),16)
   if size<0 or len(out)+size>LIMIT:raise ValueError("Response exceeds limit")
   if size==0:
    if await asyncio.wait_for(reader.readexactly(2),10)!=b"\r\n":raise ValueError("Response trailers unavailable")
    return bytes(out)
   out.extend(await asyncio.wait_for(reader.readexactly(size),10))
   if await asyncio.wait_for(reader.readexactly(2),10)!=b"\r\n":raise ValueError("Invalid chunk")
 if "content-length" in values:
  size=int(values["content-length"])
  if not 0<=size<=LIMIT:raise ValueError("Response exceeds limit")
  return await asyncio.wait_for(reader.readexactly(size),10)
 out=bytearray()
 while chunk:=await asyncio.wait_for(reader.read(16384),10):
  out.extend(chunk)
  if len(out)>LIMIT:raise ValueError("Response exceeds limit")
 return bytes(out)
class Lease:
 def __init__(self,base,port,duration):
  self.base=base;self.port=port;self.duration=duration;self.id=secrets.token_hex(16);self.token=secrets.token_urlsafe(32)
  self.server=None;self.process=None;self.url=None;self.profile=None;self.started=time.monotonic();self.expires=self.started+duration
  self.closed=False;self.connections=asyncio.Semaphore(8);self.requests=0;self.tasks=[];self.clients=set()
 async def close(self):
  if self.closed:return
  self.closed=True
  current=asyncio.current_task()
  if self.server:self.server.close()
  for task in self.tasks:
   if task is not current:task.cancel()
  for task in list(self.clients):
   if task is not current:task.cancel()
  await kill(self.process)
  # Server.wait_closed includes active clients on newer Python versions.
  # A revoking client closes its transport in finally after this returns.
  if self.server and current not in self.clients:
   try:await asyncio.wait_for(self.server.wait_closed(),3)
   except asyncio.TimeoutError:pass
  if self.profile:shutil.rmtree(self.profile,ignore_errors=True)
  if LEASES.get(self.base) is self:LEASES.pop(self.base,None)
 async def expiry(self):
  await asyncio.sleep(max(0,self.expires-time.monotonic()));await self.close()
 def authenticated(self,cookie):
  parsed=SimpleCookie()
  try:parsed.load(cookie or "")
  except Exception:return False
  return COOKIE in parsed and hmac.compare_digest(parsed[COOKIE].value,self.token)
 async def client(self,reader,writer):
  task=asyncio.current_task();self.clients.add(task);acquired=False;upstream=None
  try:
   try:await asyncio.wait_for(self.connections.acquire(),0.1);acquired=True
   except asyncio.TimeoutError:await response(writer,503);return
   if self.closed or time.monotonic()>=self.expires or self.requests>=10000:await response(writer,410);return
   self.requests+=1
   first,values=await headers(reader)
   parts=first.split(" ")
   if len(parts)!=3 or parts[2] not in ("HTTP/1.0","HTTP/1.1"):raise ValueError("Invalid request")
   method,target,_=parts
   if method not in ("GET","HEAD","POST","PUT","PATCH","DELETE","OPTIONS") or not target.startswith("/") or target.startswith("//") or any(ord(c)<32 for c in target):raise ValueError("Invalid preview request")
   # Target ownership is renewed even for token bootstrap; approval does not freeze it.
   if not await allowed(self.base,self.port):await response(writer,410);await self.close();return
   if target=="/"+self.token+"/" and method=="GET":
    await response(writer,302,extra=[("Location","/"),("Set-Cookie",COOKIE+"="+self.token+"; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age="+str(max(1,int(self.expires-time.monotonic()))))]);return
   if not self.authenticated(values.get("cookie")):await response(writer,403);return
   if "transfer-encoding" in values:raise ValueError("Chunked requests unavailable")
   size=int(values.get("content-length","0"))
   if not 0<=size<=LIMIT:await response(writer,413);return
   body=await asyncio.wait_for(reader.readexactly(size),10)
   if not await allowed(self.base,self.port):await response(writer,410);await self.close();return
   remote,upstream=await asyncio.wait_for(asyncio.open_connection("127.0.0.1",self.port),5)
   # Never forward browser/application authentication or hop-by-hop credentials.
   remove=HOP|{"cookie","authorization","host","content-length"}|{v.strip().lower() for v in values.get("connection","").split(",")}
   forwarded=[(k,v) for k,v in values.items() if k not in remove]
   upstream.write((method+" "+target+" HTTP/1.1\r\nHost: 127.0.0.1:"+str(self.port)+"\r\nConnection: close\r\nContent-Length: "+str(len(body))+"\r\n"+"".join(k+": "+v+"\r\n" for k,v in forwarded)+"\r\n").encode("latin1")+body)
   await upstream.drain()
   line,result=await headers(remote)
   match=re.fullmatch(r"HTTP/1\.[01] ([1-5][0-9]{2})(?: .*)?",line)
   if not match:raise ValueError("Invalid preview response")
   content=b"" if method=="HEAD" or int(match[1]) in (204,304) else await read_body(remote,result)
   await response(writer,int(match[1]),content,[(k,v) for k,v in result.items() if k not in HOP|{"content-length","set-cookie","cache-control"}])
  except asyncio.CancelledError:raise
  except Exception:
   try:await response(writer,502,b"Workspace preview request rejected")
   except Exception:pass
  finally:
   if upstream:upstream.close()
   writer.close()
   try:await writer.wait_closed()
   except Exception:pass
   if acquired:self.connections.release()
   self.clients.discard(task)
 async def drain(self):
  total=0
  while chunk:=await self.process.stdout.read(4096):
   total+=len(chunk)
   if total>65536:
    await self.close();return
 async def watch(self):
  await self.process.wait();await self.close()
 async def start(self):
  if not Path(EXECUTABLE).is_file() or Path(EXECUTABLE).is_symlink():raise ValueError("Installed cloudflared CLI unavailable")
  self.server=await asyncio.start_server(self.client,"127.0.0.1",0,limit=16384)
  proxy_port=self.server.sockets[0].getsockname()[1]
  self.profile=tempfile.mkdtemp(prefix="v3-private-preview-");os.chmod(self.profile,0o700)
  env={"PATH":"/usr/bin:/bin","LANG":"C.UTF-8","HOME":self.profile,"TMPDIR":self.profile}
  parent_pid=os.getpid()
  self.process=await asyncio.create_subprocess_exec(EXECUTABLE,"tunnel","--url","http://127.0.0.1:"+str(proxy_port),"--no-autoupdate",cwd=self.profile,env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT,start_new_session=True,preexec_fn=lambda:parent_guard(parent_pid))
  output=bytearray()
  async def wait_url():
   while True:
    chunk=await self.process.stdout.read(4096)
    if not chunk:raise ValueError("Cloudflare preview failed before URL")
    output.extend(chunk)
    if len(output)>65536:raise ValueError("Cloudflared startup output exceeds limit")
    match=re.search(rb"https://[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com(?=[\s/|]|$)",output)
    if match:return match.group().decode("ascii")
  self.url=await asyncio.wait_for(wait_url(),60)
  await asyncio.sleep(0)
  if self.process.returncode is not None or time.monotonic()>=self.expires:raise ValueError("Preview expired before startup")
  self.tasks=[asyncio.create_task(self.expiry()),asyncio.create_task(self.drain()),asyncio.create_task(self.watch())]
  return self.public()
 def public(self):
  return {"lease_id":self.id,"status":"active" if not self.closed else "closed","url":self.url+"/"+self.token+"/" if self.url and not self.closed else None,"expires_in":max(0,int(self.expires-time.monotonic())),"authenticated_preview":True}
async def execute(name,args,base):
 lease=None
 try:
  base=base_path(base)
  if not isinstance(args,dict):raise ValueError("Typed tunnel arguments required")
  lock=LOCKS.setdefault(base,asyncio.Lock())
  async with lock:
   current=LEASES.get(base)
   if current and (current.closed or time.monotonic()>=current.expires):await current.close();current=None
   if name=="get_tunnel_url":
    if args:raise ValueError("Tunnel status accepts an empty object")
    return {"status":"success","output":current.public() if current else {"status":"absent","url":None,"reason":"No active lease; worker restart never replays a tunnel"}}
   if name!="start_tunnel" or set(args)-{"port","duration_seconds"} or "port" not in args:raise ValueError("Invalid preview start")
   port=args["port"];duration=args.get("duration_seconds",300)
   if type(port) is not int or not 1024<=port<=65535 or type(duration) is not int or not 30<=duration<=1800:raise ValueError("Preview port or duration exceeds limit")
   if current:raise ValueError("Workspace already has an active preview lease")
   if not await allowed(base,port):raise ValueError("Port is not owned by this workspace terminal")
   lease=Lease(base,port,duration)
   try:result=await lease.start()
   except BaseException:await lease.close();raise
   LEASES[base]=lease
   return {"status":"success","output":result}
 except asyncio.CancelledError:
  if lease:await lease.close()
  raise
 except Exception:return {"status":"error","error_kind":"preview_tunnel_rejected","output":"Authenticated workspace preview unavailable or invalid; no automatic retry"}
async def close_workspace(base):
 base=base_path(base)
 if lease:=LEASES.get(base):await lease.close()
