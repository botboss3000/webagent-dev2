import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest

restrict = runpy.run_path(str(Path(__file__).parents[1] / "sandbox.py"))["restrict"]

class SandboxTests(unittest.TestCase):
    def test_workspace_writes_allowed_other_workspace_and_proc_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            own, other = root / "own", root / "other"
            own.mkdir(); other.mkdir()
            (other / "private").write_text("private")
            code = """import pathlib,sys
own,other=map(pathlib.Path,sys.argv[1:])
(own/'result').write_text('ok')
for path in [other/'private',pathlib.Path('/proc/self/environ')]:
    try: path.read_bytes()
    except PermissionError: pass
    else: raise RuntimeError('sandbox read escaped')
try: (other/'escaped').write_text('bad')
except PermissionError: pass
else: raise RuntimeError('sandbox write escaped')
"""
            result = subprocess.run(['/usr/bin/python3','-I','-c',code,str(own),str(other)],
                                    cwd=own,preexec_fn=lambda: restrict(own),
                                    capture_output=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr.decode())
            self.assertEqual((own/'result').read_text(),'ok')
            self.assertFalse((other/'escaped').exists())

    def test_child_cannot_signal_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            code="import os,sys; \ntry: os.kill(int(sys.argv[1]),0)\nexcept PermissionError: pass\nelse: raise RuntimeError('signal escaped domain')"
            result=subprocess.run(['/usr/bin/python3','-I','-c',code,str(os.getpid())],
                                  cwd=base,preexec_fn=lambda: restrict(base),capture_output=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr.decode())

    def test_tcp_bind_obeys_workspace_port_grants(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            code = """import socket
for port,allowed in [(24567,True),(24568,False),(8081,False),(0,True)]:
    listener=socket.socket()
    try:
        listener.bind(('127.0.0.1',port))
    except PermissionError:
        if allowed: raise
    else:
        if not allowed: raise RuntimeError('ungranted listener permitted')
    finally: listener.close()
"""
            # The preexec sandbox reads only trusted supervisor environment.
            previous=os.environ.get('V3_ALLOWED_LISTEN_PORTS')
            os.environ['V3_ALLOWED_LISTEN_PORTS']='24567'
            try:
                result=subprocess.run(['/usr/bin/python3','-I','-c',code],cwd=base,
                                      preexec_fn=lambda: restrict(base),capture_output=True,timeout=5)
            finally:
                if previous is None: os.environ.pop('V3_ALLOWED_LISTEN_PORTS',None)
                else: os.environ['V3_ALLOWED_LISTEN_PORTS']=previous
            self.assertEqual(result.returncode,0,result.stderr.decode())

if __name__ == '__main__': unittest.main()
