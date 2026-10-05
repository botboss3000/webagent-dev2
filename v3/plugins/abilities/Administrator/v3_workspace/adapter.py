"""Fixed session source actions and pinned Cargo jobs; no host paths or receipts."""
import asyncio,hashlib,json,os,signal,stat
from pathlib import Path
WORKER=None
LIMIT=65536
CARGO_LOG_LIMIT=8192
MAX_FILES=10000
MAX_BYTES=64*1024*1024
EXCLUDED={".git","target",".cache"}
READ_EXCLUDED=EXCLUDED|{"data","workspaces","runtime","gate-runtime","deployment-secrets"}
def configure(module):
 global WORKER
 WORKER=module
class Rejected(ValueError):pass
def exact(args,allowed,required=()):
 if not isinstance(args,dict) or set(args)-set(allowed) or any(k not in args for k in required):raise Rejected("Invalid workspace action arguments")
def digest(value):
 if not isinstance(value,str) or len(value)!=64 or any(c not in "0123456789abcdef" for c in value):raise Rejected("Invalid review digest")
 return value
def source(base):
 base=Path(base).resolve(strict=True);root=base/"webagent-v3"
 if not root.is_dir() or root.is_symlink() or root.resolve().parent!=base:raise Rejected("Session source snapshot unavailable")
 if not (root/".git").is_dir() or (root/".git").is_symlink():raise Rejected("Session Git baseline unavailable")
 return root
def capture(root):
 entries=[];total=0
 def walk(fd,parts):
  nonlocal total
  for name in sorted(os.listdir(fd)):
   rel=parts+[name];meta=os.stat(name,dir_fd=fd,follow_symlinks=False)
   if stat.S_ISLNK(meta.st_mode):raise Rejected("Symlink build inputs unavailable")
   if len(rel)==1 and name in EXCLUDED:continue
   if stat.S_ISDIR(meta.st_mode):
    child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
    try:walk(child,rel)
    finally:os.close(child)
   elif stat.S_ISREG(meta.st_mode):
    if len(entries)>=MAX_FILES or meta.st_size>4*1024*1024 or total+meta.st_size>MAX_BYTES:raise Rejected("Source input capture exceeds limit")
    child=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
    try:
     opened=os.fstat(child)
     if not stat.S_ISREG(opened.st_mode) or opened.st_ino!=meta.st_ino or opened.st_dev!=meta.st_dev:raise Rejected("Build input changed during capture")
     with os.fdopen(child,"rb",closefd=False) as stream:data=stream.read(4*1024*1024+1)
     after=os.fstat(child)
    finally:os.close(child)
    if len(data)!=meta.st_size or after.st_mtime_ns!=meta.st_mtime_ns:raise Rejected("Build input changed during capture")
    total+=len(data);entries.append(("/".join(rel),meta.st_mode&0o777,meta.st_mtime_ns,hashlib.sha256(data).hexdigest()))
   else:raise Rejected("Nonregular build input unavailable")
 rootfd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
 try:walk(rootfd,[])
 finally:os.close(rootfd)
 return {"sha256":hashlib.sha256(json.dumps(entries,separators=(",",":")).encode()).hexdigest(),"files":len(entries),"bytes":total}
async def review(base):
 if WORKER is None:raise Rejected("Trusted workspace worker unavailable")
 result=await asyncio.to_thread(WORKER.source_review,Path(base))
 if result.get("truncated") or not result.get("review_sha256"):raise Rejected("Complete bounded source review required")
 return result
async def status(base):
 root=Path(base)/"webagent-v3"
 if not root.exists() and not root.is_symlink():return {"status":"success","output":{"seeded":False}}
 source(base);result=await review(base)
 return {"status":"success","output":{"seeded":True,**result,"input_capture":capture(root)}}
async def execute(name,args,base):
 try:
  if WORKER is None:raise Rejected("Trusted workspace worker unavailable")
  if name=="v3_workspace_status":exact(args,());return await status(base)
  if name!="v3_workspace_action":raise Rejected("Workspace receipts and proposals are Rust-owned")
  exact(args,{"action","path","offset","limit","patch","expected_review_sha256"},{"action"})
  action=args["action"]
  if action=="seed":
   exact(args,{"action"});result=await WORKER.seed_workspace_async(Path(base))
   return result
  if action=="review":exact(args,{"action"});return await review(base)
  if action=="patch":
   exact(args,{"action","patch","expected_review_sha256"},{"patch","expected_review_sha256"})
   source(base);digest(args["expected_review_sha256"])
   if not isinstance(args["patch"],str) or not 0<len(args["patch"].encode())<=LIMIT:raise Rejected("Patch exceeds limit")
   return await asyncio.to_thread(WORKER.source_patch,Path(base),{"patch":args["patch"],"expected_review_sha256":args["expected_review_sha256"]})
  if action=="read":
   exact(args,{"action","path","offset","limit"},{"path"});source(base)
   raw=args["path"]
   if not isinstance(raw,str) or not raw or len(raw.encode())>512 or "\\" in raw or raw.startswith("/") or any(p in ("",".","..") for p in raw.split("/")) or raw.split("/")[0] in READ_EXCLUDED:raise Rejected("Read must name a source-relative file")
   read_args={"path":"webagent-v3/"+raw}
   for key in ("offset","limit"):
    if key in args:read_args[key]=args[key]
   return await asyncio.to_thread(WORKER.file_operation,{"workspace":Path(base).name},Path(base),read_args,"file_read")
  raise Rejected("Unknown fixed workspace action")
 except asyncio.CancelledError:raise
 except Exception as error:return {"status":"error","error_kind":"workspace_rejected","output":str(error) if isinstance(error,Rejected) else "Session source action rejected"}
async def stop(process):
 if process.returncode is not None:return
 try:os.killpg(process.pid,signal.SIGTERM)
 except ProcessLookupError:pass
 try:await asyncio.wait_for(process.wait(),5)
 except asyncio.TimeoutError:
  try:os.killpg(process.pid,signal.SIGKILL)
  except ProcessLookupError:pass
  await process.wait()
def append_cargo_tail(output,chunk):
 output.extend(chunk)
 if len(output)<=CARGO_LOG_LIMIT:return False
 del output[:-CARGO_LOG_LIMIT]
 return True
async def run_cargo(root,argv,timeout):
 worker=Path(__file__).resolve().parents[4]/"worker.py"
 if not worker.is_file() or worker.is_symlink():raise Rejected("Trusted Cargo supervisor unavailable")
 env={"PATH":"/opt/rust/bin:/usr/bin:/bin","LANG":"C.UTF-8","HOME":str(root),"TMPDIR":str(root),
      "CARGO_HOME":str(root/".cache/.cargo"),"CARGO_TARGET_DIR":str(root/"target/.v3-target"),
      "CARGO_BUILD_JOBS":"1","CARGO_INCREMENTAL":"0","RUSTFLAGS":"-C debuginfo=0"}
 process=await asyncio.create_subprocess_exec("/usr/bin/python3","-I",str(worker),"--runner",json.dumps(argv),
  cwd=root,env=env,start_new_session=True,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
 output=bytearray();truncated=False
 async def drain():
  nonlocal truncated
  while chunk:=await process.stdout.read(8192):
   truncated|=append_cargo_tail(output,chunk)
  await process.wait()
 try:await asyncio.wait_for(drain(),timeout)
 except asyncio.CancelledError:await stop(process);raise
 except asyncio.TimeoutError:
  await stop(process)
  return {"exit_code":process.returncode,"timeout":True,"truncated":truncated,"text":output.decode(errors="replace")}
 finally:
  if process.returncode is None:await stop(process)
 return {"exit_code":process.returncode,"timeout":False,"truncated":truncated,"text":output.decode(errors="replace")}
async def cargo(base,args):
 try:
  exact(args,{"task","expected_review_sha256","timeout_seconds"},{"task","expected_review_sha256"})
  task=args["task"]
  if task not in ("check","test","release"):raise Rejected("Unknown fixed Cargo job")
  timeout=args.get("timeout_seconds",600)
  if type(timeout) is not int or not 1<=timeout<=600:raise Rejected("Cargo timeout must be 1 to 600 seconds")
  expected=digest(args["expected_review_sha256"]);root=source(base)
  before_review=await review(base)
  if before_review["review_sha256"]!=expected:raise Rejected("Source review changed before Cargo")
  before=await asyncio.to_thread(capture,root)
  argv={"check":["cargo","check","--locked"],"test":["cargo","test","--locked"],"release":["cargo","build","--release","--locked"]}[task]
  result=await run_cargo(root,argv,timeout)
  after=await asyncio.to_thread(capture,root);after_review=await review(base)
  stable=before==after and after_review["review_sha256"]==expected
  # Never call a raced compilation current, including a successful exit code.
  return {"status":"success" if stable and result["exit_code"]==0 and not result["timeout"] else "error",
          "error_kind":None if stable and result["exit_code"]==0 and not result["timeout"] else "cargo_stale_inputs" if not stable else "cargo_failed",
          "output":{"task":task,**result,"stable_inputs":stable,"before":before,"after":after,"review_sha256":expected}}
 except asyncio.CancelledError:raise
 except Exception as error:return {"status":"error","error_kind":"cargo_rejected","output":str(error) if isinstance(error,Rejected) else "Fixed session Cargo job rejected"}
