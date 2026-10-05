"""Source fixture checks; no shared Cargo outputs or plaintext receipts."""
import asyncio,importlib.util,subprocess,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def module(name,file):
 spec=importlib.util.spec_from_file_location(name,file);value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value
workspace=module("workspace_adapter",ROOT/"plugins/abilities/Administrator/v3_workspace/adapter.py")
worker=module("fixture_worker",ROOT/"worker.py")
class Tests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.source=self.base/"webagent-v3";self.source.mkdir()
  (self.source/"src").mkdir();(self.source/"src/main.rs").write_text('fn main() { println!("fixture"); }\n')
  (self.source/"Cargo.toml").write_text('[package]\nname="fixture"\nversion="0.1.0"\nedition="2021"\n')
  (self.source/"Cargo.lock").write_text('version = 4\n\n[[package]]\nname = "fixture"\nversion = "0.1.0"\n')
  (self.source/".gitignore").write_text("/target/\n/.cache/\n")
  for args in [("init","-q","--initial-branch=main"),("add","."),("-c","user.name=Fixture","-c","user.email=fixture@local","commit","-qm","baseline")]:
   subprocess.run(["git",*args],cwd=self.source,check=True,capture_output=True)
  self.control=self.base/".control";self.control.mkdir();(self.control/"dispatch.lock").touch();(self.control/"shutoff").write_text("0")
  self.old_control=worker.CONTROL;worker.CONTROL=self.control
  self.old_root=worker.ROOT;worker.ROOT=self.base.parent
  workspace.configure(worker);self.original=workspace.run_cargo
 def tearDown(self):workspace.run_cargo=self.original;worker.CONTROL=self.old_control;worker.ROOT=self.old_root;self.temp.cleanup()
 async def review(self):return await workspace.review(self.base)
 async def test_status_and_review_and_receipt_boundary(self):
  result=await workspace.execute("v3_workspace_status",{},self.base)
  self.assertEqual(result["status"],"success",result);self.assertTrue(result["output"]["seeded"])
  self.assertEqual(result["output"]["review_sha256"],(await self.review())["review_sha256"])
  self.assertEqual((await workspace.execute("v3_workspace_receipt",{},self.base))["status"],"error")
  self.assertEqual((await workspace.execute("v3_workspace_action",{"action":"review","command":"id"},self.base))["status"],"error")
 async def test_read_pagination_uses_bounded_worker_contract(self):
  (self.source/"src/main.rs").write_text('first\nsecond\nthird\n')
  result=await workspace.execute("v3_workspace_action",{"action":"read","path":"src/main.rs","offset":2,"limit":1},self.base)
  self.assertEqual(result["status"],"success",result)
  self.assertEqual(result["output"],'second\n')
  self.assertEqual(result["next_offset"],3)
  self.assertTrue(result["truncated"])
  for extra in [{"limit":2001},{"offset":0},{"offset":True},{"command":"id"}]:
   result=await workspace.execute("v3_workspace_action",{"action":"read","path":"src/main.rs",**extra},self.base)
   self.assertEqual(result["status"],"error",result)
 async def test_cargo_tail_preserves_final_error_with_bounded_output(self):
  output=bytearray()
  self.assertFalse(workspace.append_cargo_tail(output,b"build start\n"))
  self.assertTrue(workspace.append_cargo_tail(output,b"x"*9000+b"\nerror: final failure\n"))
  self.assertLessEqual(len(output),workspace.CARGO_LOG_LIMIT)
  self.assertTrue(output.endswith(b"error: final failure\n"))
 async def test_fixed_jobs_cache_exclusion_and_fences(self):
  calls=[]
  async def fake(root,argv,timeout):
   calls.append((root,argv,timeout))
   (root/"target/.v3-target").mkdir(parents=True,exist_ok=True);(root/"target/.v3-target/artifact").write_text("binary")
   (root/".cache/.cargo").mkdir(parents=True,exist_ok=True);(root/".cache/.cargo/cache").write_text("cached")
   return {"exit_code":0,"timeout":False,"truncated":False,"text":"fixture passed"}
  workspace.run_cargo=fake
  digest=(await self.review())["review_sha256"]
  for task,expected in [("check",["cargo","check","--locked"]),("test",["cargo","test","--locked"]),("release",["cargo","build","--release","--locked"])]:
   result=await workspace.cargo(self.base,{"task":task,"expected_review_sha256":digest,"timeout_seconds":5})
   self.assertEqual(result["status"],"success",result);self.assertTrue(result["output"]["stable_inputs"]);self.assertEqual(calls[-1],(self.source,expected,5))
  self.assertFalse(any(self.base.glob("*receipt*")))
 async def test_raced_even_success_and_ignored_inputs(self):
  async def race(root,argv,timeout):
   (root/"src/main.rs").write_text("fn main() { }\n")
   return {"exit_code":0,"timeout":False,"truncated":False,"text":"passed"}
  workspace.run_cargo=race
  result=await workspace.cargo(self.base,{"task":"check","expected_review_sha256":(await self.review())["review_sha256"]})
  self.assertEqual(result["error_kind"],"cargo_stale_inputs")
  before=workspace.capture(self.source)
  (self.source/"data/config").mkdir(parents=True);(self.source/"data/config/gate.json").write_text("{}")
  self.assertNotEqual(before,workspace.capture(self.source))
 async def test_unknown_flags_symlink_and_stale_review(self):
  initial=(await self.review())["review_sha256"]
  for args in [{"task":"check","expected_review_sha256":initial,"flags":["--all-features"]},{"task":"shell","expected_review_sha256":initial},{"task":"check","expected_review_sha256":initial,"timeout_seconds":601}]:
   self.assertEqual((await workspace.cargo(self.base,args))["status"],"error")
  (self.source/"src/link").symlink_to("/etc/passwd")
  with self.assertRaises(workspace.Rejected):workspace.capture(self.source)
  (self.source/"src/link").unlink();(self.source/"src/main.rs").write_text("changed")
  self.assertEqual((await workspace.cargo(self.base,{"task":"check","expected_review_sha256":initial}))["status"],"error")
 async def test_patch_reuses_exact_review(self):
  patch="--- a/src/main.rs\n+++ b/src/main.rs\n@@ -1 +1 @@\n-fn main() { println!(\"fixture\"); }\n+fn main() { }\n"
  digest=(await self.review())["review_sha256"]
  result=await workspace.execute("v3_workspace_action",{"action":"patch","patch":patch,"expected_review_sha256":digest},self.base)
  self.assertEqual(result["status"],"success",result);self.assertEqual((self.source/"src/main.rs").read_text(),"fn main() { }\n")
  self.assertEqual((await workspace.execute("v3_workspace_action",{"action":"patch","patch":patch,"expected_review_sha256":digest},self.base))["status"],"error")
 @unittest.skipUnless(Path("/opt/rust/bin/cargo").is_file(),"pinned Cargo only available in V3 image")
 async def test_actual_pinned_tiny_cargo(self):
  result=await workspace.cargo(self.base,{"task":"check","expected_review_sha256":(await self.review())["review_sha256"],"timeout_seconds":60})
  self.assertEqual(result["status"],"success",result)
if __name__=="__main__":unittest.main()
