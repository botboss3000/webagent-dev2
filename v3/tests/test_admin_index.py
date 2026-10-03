"""Public five-tool behavior against temporary source snapshots only."""
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec=importlib.util.spec_from_file_location("v3_admin_index",Path(__file__).parents[1]/"plugins/abilities/Administrator/code_index/adapter.py")
index=importlib.util.module_from_spec(spec)
spec.loader.exec_module(index)

class AdminIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.base=Path(self.temp.name)
        self.repo=self.base/"webagent-v3"
        (self.repo/"ui").mkdir(parents=True)
        (self.repo/"src").mkdir()
        (self.repo/"ui/app.js").write_text("function greet() {\n  document.querySelector('#hello').addEventListener('click', () => {});\n  fetch('/api/v3/sessions');\n}\n// 🙂 é\n",encoding="utf-8")
        (self.repo/"ui/view.html").write_text('<div data-id="x"><script src="app.js"></script></div>\n\n',encoding="utf-8")
        (self.repo/"ui/style.css").write_text("#hello {\n color: red;\n}\n",encoding="utf-8")
        (self.repo/"worker.py").write_text("class Worker:\n    def read(self):\n        return 'é'\n",encoding="utf-8")
        (self.repo/"src/main.rs").write_text("fn main() {}\n",encoding="utf-8")
    def tearDown(self):
        self.temp.cleanup()
    def call(self,name,**args):
        result=index.execute("code_index_"+name,args,self.base)
        self.assertEqual(result["status"],"success",result)
        self.assertLessEqual(len(json.dumps(result,ensure_ascii=False).encode()),index.MAX_OUTPUT_BYTES)
        return result
    def scan(self,**args):
        return self.call("scan",action="scan",**args)
    def test_complete_surface_physical_lines_symbols_edges_and_progress(self):
        discovered=self.call("scan",action="discover")
        self.assertEqual(discovered["total"],5)
        scanned=self.scan(expected_revision=0,batch_id="first")
        self.assertEqual(scanned["indexed_files"],5)
        self.assertEqual(scanned["revision"],1)
        coverage=self.call("coverage")
        self.assertEqual(coverage["gaps"],0)
        self.assertEqual(coverage["total_lines"],14)
        self.assertEqual(coverage["unindexed_count"],0)
        self.assertEqual(coverage["stale_count"],0)
        line=self.call("lookup",action="line",file_path="worker.py",line_no=3)["results"][0]
        self.assertEqual(line["owner_symbol"],"read")
        self.assertFalse(line["stale"])
        symbol=self.call("lookup",action="symbol",query="Worker")["results"][0]
        self.assertEqual(symbol["kind"],"class")
        self.assertEqual(symbol["uncertain"],0)
        node=self.call("trace",kind="selector",value="#hello")["results"][0]
        self.assertEqual(node["file_path"],"ui/app.js")
        self.assertEqual(node["line_no"],2)
        self.assertEqual(self.call("trace",kind="event",value="click")["total"],1)
        self.assertEqual(self.call("trace",kind="endpoint",value="/api/v3/sessions")["total"],1)
        self.assertEqual(self.call("trace",kind="asset",value="app.js")["total"],1)
        progress=self.call("progress",action="get",batch_id="first")["results"][0]
        self.assertEqual(progress["status"],"completed")
        self.assertEqual(progress["indexed_files"],5)
        self.assertEqual(self.call("progress")["total"],1)
    def test_changed_deleted_source_requires_explicit_refresh(self):
        self.scan()
        old=self.call("lookup",action="file",file_path="ui/app.js")["results"][0]["sha256"]
        (self.repo/"ui/app.js").write_text("function changed() {}\n",encoding="utf-8")
        stale=self.call("lookup",action="file",file_path="ui/app.js")
        self.assertTrue(stale["results"][0]["stale"])
        self.assertEqual(stale["results"][0]["sha256"],old)
        self.assertEqual(self.call("coverage",action="stale")["total"],1)
        refreshed=self.call("scan",action="scan_file",file_path="ui/app.js",expected_revision=1)
        self.assertEqual(refreshed["revision"],2)
        self.assertFalse(self.call("lookup",action="file",file_path="ui/app.js")["results"][0]["stale"])
        (self.repo/"ui/view.html").unlink()
        self.assertEqual(self.call("coverage",action="stale")["total"],1)
        removed=self.scan(expected_revision=2)
        self.assertEqual(removed["removed_count"],1)
        self.assertEqual(self.call("lookup",action="file",file_path="ui/view.html")["total"],0)
        self.assertEqual(self.call("coverage")["stale_count"],0)
    def test_source_race_and_stale_revision_preserve_previous_index(self):
        self.scan()
        result=index.execute("code_index_scan",{"action":"scan","expected_revision":0},self.base)
        self.assertEqual(result["status"],"error")
        self.assertIn("revision conflict",result["output"])
        before=self.call("lookup",action="file",file_path="ui/app.js")["results"][0]["sha256"]
        original=index.read_source;reads=0
        def changed(rootfd,path):
            nonlocal reads
            data=original(rootfd,path)
            if path=="ui/app.js":
                reads+=1
                if reads==2:return b"function raced() {}\n"
            return data
        with mock.patch.object(index,"read_source",side_effect=changed):
            result=index.execute("code_index_scan",{"action":"scan","batch_id":"raced"},self.base)
        self.assertEqual(result["status"],"error")
        self.assertIn("before index commit",result["output"])
        self.assertEqual(self.call("lookup",action="file",file_path="ui/app.js")["results"][0]["sha256"],before)
        self.assertEqual(self.call("progress",action="get",batch_id="raced")["results"][0]["status"],"failed")
        self.assertEqual(self.call("coverage")["revision"],1)
    def test_unicode_invalid_python_and_js_fallback_remain_line_complete(self):
        (self.repo/"worker.py").write_text("def broken(:\n# 🙂\n",encoding="utf-8")
        with mock.patch.object(index,"Language",None):
            self.scan()
        coverage=self.call("coverage")
        self.assertEqual(coverage["gaps"],0)
        files={path:self.call("lookup",action="file",file_path=path)["results"][0]
            for path in [item["path"] for item in self.call("scan",action="discover")["results"]]}
        self.assertEqual(files["worker.py"]["parser_status"],"invalid")
        self.assertEqual(files["ui/app.js"]["parser_status"],"uncertain")
        self.assertIn("tree_sitter_unavailable",files["ui/app.js"]["parser_notes"])
        self.assertEqual(self.call("lookup",action="line",file_path="worker.py",line_no=2)["total"],1)
        self.assertEqual(files["src/main.rs"]["parser_notes"],"rust_lexical_only")
    def test_paths_symlinks_derived_index_and_other_workspace_are_isolated(self):
        self.scan()
        for args in [{"action":"scan","root":"../"}, {"action":"scan","scope":"../secret"},
                     {"action":"scan_file","file_path":"/etc/passwd"},
                     {"action":"scan","user_id":"foreign"}]:
            self.assertEqual(index.execute("code_index_scan",args,self.base)["status"],"error")
        outside=self.base/"private.py";outside.write_text("PRIVATE_SECRET = 1\n")
        (self.repo/"ui/escape.py").symlink_to(outside)
        result=index.execute("code_index_scan",{"action":"scan"},self.base)
        self.assertEqual(result["status"],"error")
        (self.repo/"ui/escape.py").unlink()
        (self.base/"alias").symlink_to(self.repo,target_is_directory=True)
        self.assertEqual(index.execute("code_index_coverage",{"root":"alias"},self.base)["status"],"error")
        second=self.base/"second";(second/"webagent-v3/ui").mkdir(parents=True)
        (second/"webagent-v3/ui/other.js").write_text("function other() {}\n")
        self.assertEqual(index.execute("code_index_lookup",{"action":"symbol","query":"greet"},second)["total"],0)
        self.assertEqual(index.execute("code_index_scan",{"action":"scan"},second)["status"],"success")
        self.assertEqual(self.call("lookup",action="symbol",query="other")["total"],0)
        # Derived files never alias a runtime DB or a sibling inode.
        key=index.sha(b"webagent-v3")
        db=self.base/".code-index"/(key+".sqlite")
        db.unlink();db.symlink_to(outside)
        self.assertEqual(index.execute("code_index_coverage",{},self.base)["status"],"error")
        self.assertEqual(outside.read_text(),"PRIVATE_SECRET = 1\n")
    def test_page_bound_and_parser_limits_are_explicit(self):
        for number in range(100):
            (self.repo/"ui"/("item%03d.json"%number)).write_text("{}\n")
        self.scan()
        page=self.call("coverage",action="report",limit=3)
        self.assertEqual(page["count"],3)
        self.assertTrue(page["has_more"])
        more=self.call("coverage",action="report",limit=3,offset=page["next_offset"])
        self.assertNotEqual(page["results"][0]["path"],more["results"][0]["path"])
        (self.repo/"ui/huge.py").write_bytes(b"x"*(index.MAX_FILE_BYTES+1))
        self.assertEqual(index.execute("code_index_scan",{"action":"scan"},self.base)["status"],"error")

    def test_all_optional_fields_are_projected_per_action_and_zero_revision_is_enforced(self):
        common={"root":"","limit":1,"offset":0}
        padded={"scope":"","file_path":"","batch_id":"","expected_revision":0}
        discovered=self.call("scan",action="discover",**common,**padded)
        self.assertEqual(discovered["root"],"webagent-v3")
        self.assertEqual(discovered["total"],5)
        scanned=self.call("scan",action="scan",**common,**padded)
        self.assertEqual(scanned["revision"],1)
        self.assertTrue(scanned["batch_id"])
        # Numeric zero is an explicit optimistic precondition, never an empty optional.
        stale=index.execute("code_index_scan",{"action":"scan",**common,**padded},self.base)
        self.assertEqual(stale["status"],"error");self.assertIn("revision conflict",stale["output"])
        progress=self.call("progress",action="list",batch_id="",**common)
        self.assertEqual(progress["total"],1)
        self.call("progress",action="get",batch_id=scanned["batch_id"],**common)
        symbols=self.call("lookup",action="symbol",query="Worker",file_path="",line_no=1,**common)
        self.assertEqual(symbols["total"],1)
        edges=self.call("lookup",action="edge",query="click",file_path="",line_no=1,**common)
        self.assertEqual(edges["total"],1)
        file=self.call("lookup",action="file",file_path="worker.py",query="",line_no=1,**common)
        self.assertEqual(file["total"],1)
        line=self.call("lookup",action="line",file_path="worker.py",query="",line_no=3,**common)
        self.assertEqual(line["results"][0]["line_no"],3)
        refresh=self.call("scan",action="scan_file",scope="",file_path="worker.py",
            batch_id="",expected_revision=1,**common)
        self.assertEqual(refresh["revision"],2)
        # A nonempty supported filter remains semantic and cannot be silently ignored.
        self.assertEqual(self.call("lookup",action="symbol",query="Worker",
            file_path="ui/app.js",line_no=1)["total"],0)
    def test_required_paths_batches_unknown_fields_and_confined_roots_remain_strict(self):
        self.scan()
        for name,args in [
            ("scan",{"action":"scan_file","file_path":""}),
            ("lookup",{"action":"file","file_path":"","query":"","line_no":1}),
            ("lookup",{"action":"line","file_path":"","line_no":1}),
            ("progress",{"action":"get","batch_id":""}),
            ("progress",{"action":"get"}),
            ("trace",{"kind":"symbol","value":"Worker","action":"symbol"}),
            ("scan",{"action":"discover","undeclared":"ignored?"}),
            ("lookup",{"action":"symbol","root":"../outside","file_path":"","query":""}),
        ]:
            self.assertEqual(index.execute("code_index_"+name,args,self.base)["status"],"error",(name,args))
        # Only known irrelevant properties are discarded, never arbitrary fields.
        result=self.call("scan",action="discover",file_path="../irrelevant",batch_id="not a batch",
            expected_revision=-1)
        self.assertEqual(result["total"],5)

if __name__=="__main__":unittest.main()
