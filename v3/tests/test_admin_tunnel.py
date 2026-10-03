"""Real private HTTP proxy tests with an injected fake CLI; no public tunnel."""
import asyncio,importlib.util,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("preview_tunnel",ROOT/"plugins/abilities/Administrator/remote_tunnel/adapter.py")
tunnel=importlib.util.module_from_spec(spec);spec.loader.exec_module(tunnel)
class Tests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)/"workspace";self.base.mkdir()
  self.executable=Path(self.tmp.name)/"fake-cloudflared"
  self.executable.write_text('#!/usr/bin/python3\nimport time\nprint("https://real-shaped-preview.trycloudflare.com",flush=True)\ntime.sleep(300)\n');self.executable.chmod(0o755)
  self.owned=True;self.requests=[];self.large=False
  async def upstream(reader,writer):
   try:
    raw=await reader.readuntil(b"\r\n\r\n");self.requests.append(raw)
    if self.large:writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1048577\r\nConnection: close\r\n\r\n")
    else:writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\nSet-Cookie: upstream=private\r\nConnection: close\r\n\r\npreview")
    await writer.drain()
   finally:writer.close();await writer.wait_closed()
  self.server=await asyncio.start_server(upstream,"127.0.0.1",0);self.port=self.server.sockets[0].getsockname()[1]
  tunnel.configure(lambda base,port:self.owned and base==self.base and port==self.port,self.executable)
 async def asyncTearDown(self):
  await tunnel.close_workspace(self.base);self.server.close();await self.server.wait_closed();self.tmp.cleanup();tunnel.configure(None)
 async def start(self):
  result=await tunnel.execute("start_tunnel",{"port":self.port,"duration_seconds":30},self.base)
  self.assertEqual(result["status"],"success",result);return tunnel.LEASES[self.base]
 async def request(self,lease,path="/",headers=""):
  port=lease.server.sockets[0].getsockname()[1];reader,writer=await asyncio.open_connection("127.0.0.1",port)
  writer.write(("GET "+path+" HTTP/1.1\r\nHost: preview.test\r\n"+headers+"\r\n").encode());await writer.drain()
  result=await reader.read();writer.close();await writer.wait_closed();return result
 async def test_cookie_auth_bootstrap_and_header_strip(self):
  lease=await self.start()
  self.assertTrue(lease.public()["url"].startswith("https://real-shaped-preview.trycloudflare.com/"))
  denied=await self.request(lease);self.assertIn(b"403",denied);self.assertEqual(self.requests,[])
  bootstrap=await self.request(lease,"/"+lease.token+"/")
  self.assertIn(b"302",bootstrap);self.assertIn(b"Secure; HttpOnly; SameSite=Strict",bootstrap);self.assertEqual(self.requests,[])
  result=await self.request(lease,headers="Cookie: "+tunnel.COOKIE+"="+lease.token+"; v3_session=never-forward\r\nAuthorization: Bearer never-forward\r\n")
  self.assertIn(b"preview",result);self.assertNotIn(b"set-cookie",result.lower())
  self.assertNotIn(b"cookie:",self.requests[-1].lower());self.assertNotIn(b"authorization:",self.requests[-1].lower());self.assertNotIn(b"never-forward",self.requests[-1])
 async def test_revocation_rechecked_before_request(self):
  lease=await self.start();self.owned=False
  denied=await self.request(lease,headers="Cookie: "+tunnel.COOKIE+"="+lease.token+"\r\n")
  self.assertIn(b"410",denied);self.assertEqual(self.requests,[]);self.assertTrue(lease.closed)
 async def test_bounds_default_deny_app_ports_and_args(self):
  for args in [{"port":8080},{"port":self.port,"duration_seconds":1801},{"port":self.port,"host":"127.0.0.1"},{"port":True},{"port":self.port,"duration_seconds":True}]:
   self.assertEqual((await tunnel.execute("start_tunnel",args,self.base))["status"],"error")
  tunnel.configure(None,self.executable)
  self.assertEqual((await tunnel.execute("start_tunnel",{"port":self.port},self.base))["status"],"error")
  self.assertEqual((await tunnel.execute("get_tunnel_url",{"session_id":"other"},self.base))["status"],"error")
 async def test_oversized_response_rejected_and_one_lease(self):
  lease=await self.start();self.large=True
  result=await self.request(lease,headers="Cookie: "+tunnel.COOKIE+"="+lease.token+"\r\n")
  self.assertIn(b"502",result);self.assertNotIn(b"preview\r\n",result)
  self.assertEqual((await tunnel.execute("start_tunnel",{"port":self.port},self.base))["status"],"error")
 async def test_missing_cli_ttl_and_close_reaps_process(self):
  tunnel.configure(lambda base,port:True,Path(self.tmp.name)/"missing")
  self.assertEqual((await tunnel.execute("start_tunnel",{"port":self.port},self.base))["status"],"error")
  tunnel.configure(lambda base,port:True,self.executable)
  lease=await self.start();lease.expires=0
  status=await tunnel.execute("get_tunnel_url",{},self.base)
  self.assertEqual(status["output"]["status"],"absent");self.assertIsNotNone(lease.process.returncode);self.assertFalse(Path(lease.profile).exists())
 async def test_cancelled_start_reaps_connector(self):
  self.executable.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(300)\n')
  before=set(Path(tempfile.gettempdir()).glob("v3-private-preview-*"))
  task=asyncio.create_task(tunnel.execute("start_tunnel",{"port":self.port},self.base))
  await asyncio.sleep(0.03);task.cancel()
  with self.assertRaises(asyncio.CancelledError):await task
  self.assertNotIn(self.base,tunnel.LEASES)
  self.assertEqual(set(Path(tempfile.gettempdir()).glob("v3-private-preview-*")),before)
 async def test_failed_cli_never_fabricates_url(self):
  self.executable.write_text('#!/usr/bin/python3\nprint("no tunnel URL",flush=True)\n')
  result=await tunnel.execute("start_tunnel",{"port":self.port},self.base)
  self.assertEqual(result["status"],"error");self.assertNotIn(self.base,tunnel.LEASES)
if __name__=="__main__":unittest.main()
