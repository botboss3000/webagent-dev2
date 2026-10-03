"""Real temporary Git repositories; never runtime data or server repositories."""
import asyncio,hashlib,importlib.util,subprocess,tempfile,unittest
from pathlib import Path
FILE=Path(__file__).resolve().parents[1]/"plugins/abilities/Administrator/git_control/adapter.py"
spec=importlib.util.spec_from_file_location("admin_git",FILE);git=importlib.util.module_from_spec(spec);spec.loader.exec_module(git)
class GitTests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.repo=self.base/"webagent-v3";self.repo.mkdir()
  self.run_git("init","-q","--initial-branch=main")
  self.run_git("config","user.name","Fixture");self.run_git("config","user.email","fixture@localhost")
  (self.repo/"file.txt").write_text("baseline\n");self.run_git("add","file.txt");self.run_git("commit","-qm","baseline")
  self.remote=self.base/"remote.git";subprocess.run(["git","init","--bare","-q",str(self.remote)],check=True)
  self.run_git("remote","add","origin",str(self.remote))
 def tearDown(self):self.tmp.cleanup()
 def run_git(self,*args,allowed=(0,)):
  p=subprocess.run(["git",*args],cwd=self.repo,capture_output=True)
  self.assertIn(p.returncode,allowed,p.stderr.decode());return p.stdout.decode().strip()
 async def call(self,name,args):
  return await git.execute(name,args,self.base)
 async def test_status_commit_push_and_dedupe(self):
  initial=await self.call("git_tool",{"operation":"status"})
  self.assertEqual(initial["status"],"success",initial)
  (self.repo/"file.txt").write_text("new source\n")
  view=(await self.call("git_tool",{"operation":"status"}))["output"]
  result=await self.call("commit_and_push",{"paths":["file.txt"],"message":"change","expected_head":view["head"],"expected_review_sha256":view["review_sha256"]})
  self.assertEqual(result["status"],"success",result)
  pushed=subprocess.run(["git","--git-dir",str(self.remote),"rev-parse","main"],capture_output=True,check=True).stdout.decode().strip()
  self.assertEqual(pushed,result["output"]["commit"])
  notice=git.notice(view);self.assertTrue(notice["notify"]);self.assertFalse(git.notice(view,notice["snapshot_sha256"])["notify"])
 async def test_stale_review_paths_and_symlink(self):
  view=await git.snapshot(self.base);(self.repo/"file.txt").write_text("changed after review")
  rejected=await self.call("commit_and_push",{"paths":["file.txt"],"message":"stale","expected_head":view["head"],"expected_review_sha256":view["review_sha256"]})
  self.assertEqual(rejected["error_kind"],"git_rejected")
  for args in [{"operation":"status","repo":"../other"},{"operation":"add","paths":["../escape"],"expected_head":view["head"]},{"operation":"push","force":True,"expected_head":view["head"]}]:
   self.assertEqual((await self.call("git_tool",args))["status"],"error")
  (self.repo/"link").symlink_to(self.base/"remote.git")
  self.assertEqual((await self.call("git_tool",{"operation":"add","paths":["link"],"expected_head":view["head"]}))["status"],"error")
 async def test_secret_and_external_config_guards(self):
  (self.repo/"key.txt").write_text("api_key=abcdefghijklmnop12345")
  view=await git.snapshot(self.base)
  result=await self.call("commit_and_push",{"paths":["key.txt"],"message":"unsafe","expected_head":view["head"],"expected_review_sha256":view["review_sha256"]})
  self.assertEqual(result["status"],"error")
  self.run_git("config","remote.origin.url","https://user:password@example.invalid/repository")
  result=await self.call("git_tool",{"operation":"push","expected_head":view["head"]})
  self.assertEqual(result["status"],"error");self.assertNotIn("password@",str(result))
  self.run_git("config","remote.origin.url",str(self.remote));self.run_git("config","filter.unsafe.clean","touch escaped")
  self.assertEqual((await self.call("git_tool",{"operation":"status"}))["status"],"error")
 async def test_conflict_sha_both_and_ours(self):
  self.run_git("checkout","-qb","other");(self.repo/"file.txt").write_text("other side\n");self.run_git("commit","-qam","other")
  self.run_git("checkout","-q","main");(self.repo/"file.txt").write_text("our side\n");self.run_git("commit","-qam","ours")
  self.run_git("merge","other",allowed=(1,))
  content=(self.repo/"file.txt").read_bytes()
  bad=await self.call("resolve_conflict",{"path":"file.txt","strategy":"both","expected_sha256":"0"*64})
  self.assertEqual(bad["status"],"error")
  result=await self.call("resolve_conflict",{"path":"file.txt","strategy":"both","expected_sha256":hashlib.sha256(content).hexdigest()})
  self.assertEqual(result["status"],"success",result)
  self.assertEqual((self.repo/"file.txt").read_text(),"our side\nother side\n")
  result=await self.call("resolve_conflict",{"path":"file.txt","strategy":"ours","expected_sha256":result["output"]["sha256"]})
  self.assertEqual(result["status"],"success",result);self.assertEqual((self.repo/"file.txt").read_text(),"our side\n")
 async def test_hooks_alternates_and_remote_hook_refusal(self):
  hook=self.remote/"hooks/pre-receive";hook.write_text("#!/bin/sh\nexit 0\n");hook.chmod(0o755)
  current=await git.head(self.repo)
  self.assertEqual((await self.call("git_tool",{"operation":"push","expected_head":current}))["status"],"error")
  hook.unlink();(self.repo/".git/objects/info/alternates").write_text("/outside")
  self.assertEqual((await self.call("git_tool",{"operation":"status"}))["status"],"error")
 async def test_operation_mutation_fence_and_unknown_name(self):
  current=await git.head(self.repo)
  self.assertEqual((await self.call("git_tool",{"operation":"branch_create","name":"safe","expected_head":current}))["status"],"success")
  self.assertIn("safe",self.run_git("branch","--list"))
  self.assertEqual((await self.call("git_tool",{"operation":"checkout","ref":"safe","expected_head":"0"*40}))["status"],"error")
  self.assertEqual((await self.call("shell",{"command":"pwd"}))["status"],"error")
 async def test_optional_schema_defaults_and_exact_mutation_paths(self):
  defaults={"repo":"","paths":[],"ref":"","name":"","message":"","remote":"","limit":20,"cached":False,"expected_head":"0"*40}
  for operation in ("status","diff","log","show","branch_list","tag_list","stash_list"):
   result=await self.call("git_tool",{"operation":operation,**defaults})
   self.assertEqual(result["status"],"success",(operation,result))
  current=await git.head(self.repo)
  for operation in ("add","reset","checkout","branch_create"):
   result=await self.call("git_tool",{"operation":operation,**defaults,"expected_head":current})
   self.assertEqual(result["status"],"error",(operation,result))
  self.assertEqual((await self.call("git_tool",{"operation":"status",**defaults,"name":"substantive"}))["status"],"error")
  self.assertEqual((await self.call("git_tool",{"operation":"status",**defaults,"unknown":""}))["status"],"error")
  (self.repo/"file.txt").write_text("blank defaults commit\n")
  view=(await self.call("git_tool",{"operation":"status",**defaults}))["output"]
  result=await self.call("commit_and_push",{"repo":"","remote":"","paths":["file.txt"],"message":"defaults","expected_head":view["head"],"expected_review_sha256":view["review_sha256"]})
  self.assertEqual(result["status"],"success",result)
if __name__=="__main__":unittest.main()
