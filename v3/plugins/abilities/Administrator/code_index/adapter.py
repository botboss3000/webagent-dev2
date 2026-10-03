"""Derived source index for one Rust-authorized caller/session workspace.

No plugin import appoints a tool or grants access. Rust owns policy and dispatch.
Only the derived index is written; source files are never changed by scanning.
"""
from __future__ import annotations
import ast
import fcntl
import hashlib
import heapq
import json
import os
import re
import sqlite3
import stat
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
try:
    from tree_sitter import Language, Parser
    import tree_sitter_javascript
except ImportError:
    Language = Parser = tree_sitter_javascript = None

MAX_FILES = 5000
MAX_ENTRIES = 20000
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_LINES = 20000
MAX_TOTAL_LINES = 300000
MAX_OUTPUT_BYTES = 64 * 1024
MAX_DB_BYTES = 128 * 1024 * 1024
SUFFIXES = {".js", ".mjs", ".cjs", ".css", ".html", ".json", ".py", ".rs", ".toml"}
EXCLUDED = {".git", "target", "data", "workspaces", "runtime", "rollback", ".code-index",
    "v2_ref", "node_modules", "vendor", "backups", "__pycache__", ".venv", ".cache"}
FUNCTION_RE = re.compile(r"(?:^|[;{}\s])(?:async\s+)?function\s*\*?\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)|(?:^|[;{}\s])(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>")
CLASS_RE = re.compile(r"(?:^|[;{}\s])class\s+([A-Za-z_$][\w$]*)")
IMPORT_RE = re.compile(r"(?:import\s+(?:[^;]+?\s+from\s+)?|import\s*\()\s*['\"]([^'\"]+)['\"]")
SELECTOR_RE = re.compile(r"(?:querySelector(?:All)?|closest|matches)\s*\(\s*['\"]([^'\"]+)['\"]")
EVENT_RE = re.compile(r"(?:addEventListener|on)\s*\(\s*['\"]([^'\"]+)['\"]")
DATA_RE = re.compile(r"\b(data-[A-Za-z0-9_-]+)\b")
ENDPOINT_RE = re.compile(r"['\"\x60]((?:/api/|/session-|/chat/)[^'\"\x60\s?]*)")
CSS_RULE_RE = re.compile(r"^\s*([^@/][^{]+)\{")
HTML_TAG_RE = re.compile(r"<\s*([A-Za-z][\w:-]*)\b")
HTML_ASSET_RE = re.compile(r"(?:src|href)\s*=\s*['\"]([^'\"]+)['\"]", re.I)

DDL = """
CREATE TABLE IF NOT EXISTS meta(id INTEGER PRIMARY KEY CHECK(id=1),schema_version INTEGER NOT NULL,revision INTEGER NOT NULL);
INSERT OR IGNORE INTO meta VALUES(1,1,0);
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY,language TEXT NOT NULL,sha256 TEXT NOT NULL,line_count INTEGER NOT NULL,scanned_at TEXT NOT NULL,parser_status TEXT NOT NULL,parser_notes TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lines(file_path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,line_no INTEGER NOT NULL,kind TEXT NOT NULL,owner_symbol TEXT,text_digest TEXT NOT NULL,note TEXT NOT NULL,PRIMARY KEY(file_path,line_no));
CREATE TABLE IF NOT EXISTS symbols(file_path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,name TEXT NOT NULL,kind TEXT NOT NULL,start_line INTEGER NOT NULL,end_line INTEGER NOT NULL,signature TEXT NOT NULL,parent_symbol TEXT,uncertain INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name);
CREATE TABLE IF NOT EXISTS edges(file_path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,line_no INTEGER NOT NULL,edge_kind TEXT NOT NULL,value TEXT NOT NULL,owner_symbol TEXT);
CREATE INDEX IF NOT EXISTS edges_value ON edges(value);
CREATE TABLE IF NOT EXISTS batches(batch_id TEXT PRIMARY KEY,scope TEXT NOT NULL,status TEXT NOT NULL,requested_files INTEGER NOT NULL,indexed_files INTEGER NOT NULL,started_at TEXT NOT NULL,completed_at TEXT,error TEXT NOT NULL,revision INTEGER);
"""
def sha(data):
    return hashlib.sha256(data).hexdigest()
def now():
    return datetime.now(timezone.utc).isoformat()
def relative_parts(value, allow_empty=False):
    if not isinstance(value,str) or len(value.encode())>512 or "\0" in value or "\\" in value:
        raise ValueError("Invalid relative source path")
    if allow_empty and value in ("", "."):
        return []
    pieces=value.split("/")
    if not pieces or value.startswith("/") or any(p in ("",".","..") for p in pieces):
        raise ValueError("Only confined relative paths are supported")
    return pieces
def open_child_dir(fd,name):
    return os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
def open_path(fd,parts,directory=False):
    current=os.dup(fd)
    try:
        for name in parts[:-1] if parts else []:
            following=open_child_dir(current,name);os.close(current);current=following
        if not parts:
            return os.dup(current)
        return os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|(os.O_DIRECTORY if directory else os.O_NONBLOCK),dir_fd=current)
    finally:
        os.close(current)
def read_source(rootfd,path):
    parts=relative_parts(path)
    if any(p in EXCLUDED for p in parts):
        raise ValueError("Excluded source path")
    fd=open_path(rootfd,parts)
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size>MAX_FILE_BYTES:
            raise ValueError("Source file type/size exceeds limit")
        data=bytearray()
        while len(data)<=MAX_FILE_BYTES:
            chunk=os.read(fd,min(65536,MAX_FILE_BYTES+1-len(data)))
            if not chunk:break
            data.extend(chunk)
        after=os.fstat(fd)
        if len(data)>MAX_FILE_BYTES or (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):
            raise ValueError("Source changed during bounded read")
        return bytes(data)
    finally:os.close(fd)
def source_files(rootfd,scope,deadline):
    parts=relative_parts(scope,True)
    if any(p in EXCLUDED for p in parts):
        raise ValueError("Excluded source scope")
    start=open_path(rootfd,parts)
    found=[];entries=0
    def walk(fd,prefix,depth):
        nonlocal entries
        if depth>32 or time.monotonic()>deadline:raise ValueError("Source scan bound exceeded")
        children=[]
        with os.scandir(fd) as iterator:
            for entry in iterator:
                entries+=1
                if entries>MAX_ENTRIES:raise ValueError("Source enumeration limit exceeded")
                children.append(entry)
        children.sort(key=lambda e:e.name)
        for entry in children:
            if time.monotonic()>deadline:raise ValueError("Source scan deadline exceeded")
            if entry.name in EXCLUDED:continue
            path="/".join(prefix+[entry.name])
            if len(path.encode())>512:raise ValueError("Source path exceeds limit")
            if entry.is_symlink():raise ValueError("Symlink source entries are unsupported")
            if entry.is_dir(follow_symlinks=False):
                child=open_child_dir(fd,entry.name)
                try:walk(child,prefix+[entry.name],depth+1)
                finally:os.close(child)
            elif entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in SUFFIXES:
                if entry.name.endswith((".min.js",".min.css")):continue
                found.append(path)
                if len(found)>MAX_FILES:raise ValueError("Source file count exceeds limit")
            elif not entry.is_file(follow_symlinks=False):
                raise ValueError("Unsupported source filesystem entry")
    try:
        meta=os.fstat(start)
        if stat.S_ISREG(meta.st_mode):
            path="/".join(parts)
            if Path(path).suffix.lower() not in SUFFIXES:raise ValueError("Unsupported source suffix")
            found=[path]
        elif stat.S_ISDIR(meta.st_mode):walk(start,parts,0)
        else:raise ValueError("Invalid source scope")
    finally:os.close(start)
    return sorted(found)
def owners(lines,known):
    # Sweep spans once: line completeness must not require an O(lines*symbols) walk.
    starts=sorted(enumerate(known),key=lambda pair:pair[1]["start"]);pending=[];at=0;result={}
    for number in range(1,len(lines)+1):
        while at<len(starts) and starts[at][1]["start"]<=number:
            serial,item=starts[at];at+=1
            heapq.heappush(pending,(item["end"]-item["start"],item["end"],serial,item["name"]))
        while pending and pending[0][1]<number:heapq.heappop(pending)
        result[number]=pending[0][3] if pending else None
    return result

def brace_delta(line: str, in_comment: bool) -> tuple[int, bool]:
    """Count JS braces while avoiding ordinary quotes and comments."""
    delta, index, quote, escaped = 0, 0, None, False
    while index < len(line):
        char, pair = line[index], line[index:index + 2]
        if in_comment:
            if pair == "*/":
                in_comment, index = False, index + 2
                continue
            index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if pair == "/*":
            in_comment, index = True, index + 2
            continue
        if pair == "//":
            break
        if char in "'\"`":
            quote = char
        elif char == "{":
            delta += 1
        elif char == "}":
            delta -= 1
        index += 1
    return delta, in_comment

def python_symbols(source: str) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    result: list[dict[str, Any]] = []
    parents: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            result.append({"name": node.name, "kind": "class", "start": node.lineno, "end": node.end_lineno or node.lineno, "signature": f"class {node.name}", "uncertain": False, "parent": parents[-1] if parents else None})
            parents.append(node.name)
            self.generic_visit(node)
            parents.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._function(node)

        def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            args = [arg.arg for arg in node.args.args]
            result.append({"name": node.name, "kind": "function", "start": node.lineno, "end": node.end_lineno or node.lineno, "signature": f"{node.name}({', '.join(args)})", "uncertain": False, "parent": parents[-1] if parents else None})
            parents.append(node.name)
            self.generic_visit(node)
            parents.pop()

    Visitor().visit(tree)
    return sorted(result, key=lambda item: (item["start"], item["end"], item["name"]))

def node_text(source: bytes, node: Any) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", "replace")

def javascript_symbols(source: str) -> tuple[Optional[list[dict[str, Any]]], str]:
    """Return AST-backed JavaScript symbols, or a reason to use the fallback."""
    if not all((Language, Parser, tree_sitter_javascript)):
        return None, "tree_sitter_unavailable"
    try:
        language = Language(tree_sitter_javascript.language())
        parser = Parser(language)
        encoded = source.encode("utf-8", "replace")
        tree = parser.parse(encoded)
    except Exception as exc:
        return None, f"tree_sitter_error:{type(exc).__name__}"
    if tree.root_node.has_error:
        return None, "tree_sitter_syntax_error"

    result: list[dict[str, Any]] = []
    symbol_types = {"function_declaration", "class_declaration", "method_definition", "generator_function_declaration"}
    callable_values = {"arrow_function", "function_expression", "generator_function"}

    def add(node: Any, name_node: Any, kind: str, parents: list[str]) -> Optional[str]:
        if not name_node:
            return None
        name = node_text(encoded, name_node).strip()
        if not name:
            return None
        first_line = node_text(encoded, node).splitlines()[0].strip()
        result.append({
            "name": name,
            "kind": kind,
            "start": node.start_point[0] + 1,
            "end": node.end_point[0] + 1,
            "signature": first_line[:300],
            "uncertain": False,
            "parent": parents[-1] if parents else None,
        })
        return name

    def walk(node: Any, parents: list[str]) -> None:
        own_name: Optional[str] = None
        if node.type in symbol_types:
            kind = "class" if node.type == "class_declaration" else "function"
            own_name = add(node, node.child_by_field_name("name"), kind, parents)
        elif node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            if value and value.type in callable_values:
                own_name = add(value, node.child_by_field_name("name"), "function", parents)
        child_parents = parents + [own_name] if own_name else parents
        for child in node.children:
            walk(child, child_parents)

    walk(tree.root_node, [])
    return sorted(result, key=lambda item: (item["start"], item["end"], item["name"])), ""

def symbols(lines: list[str], suffix: str, source: str) -> list[dict[str, Any]]:
    if suffix == ".py":
        return python_symbols(source)
    if suffix in {".css", ".html", ".json"}:
        return []
    starts: list[dict[str, Any]] = []
    for line_no, line in enumerate(lines, 1):
        match = CLASS_RE.search(line)
        if match:
            starts.append({"name": match.group(1), "kind": "class", "start": line_no, "signature": match.group(0).strip(), "parent": None})
            continue
        match = FUNCTION_RE.search(line)
        if match:
            name, args = match.group(1) or match.group(3), match.group(2) or match.group(4) or ""
            starts.append({"name": name, "kind": "function", "start": line_no, "signature": f"{name}({args})", "parent": None})
    by_line = {item["start"]: item for item in starts}
    result: list[dict[str, Any]] = []
    opened: list[tuple[dict[str, Any], int]] = []
    depth, in_comment = 0, False
    for line_no, line in enumerate(lines, 1):
        before = depth
        delta, in_comment = brace_delta(line, in_comment)
        start = by_line.get(line_no)
        if start:
            start["end"] = line_no
            start["uncertain"] = "{" not in line
            if start["uncertain"]:
                result.append(start)
            else:
                opened.append((start, before + delta))
        depth += delta
        still_open: list[tuple[dict[str, Any], int]] = []
        for item, target_depth in opened:
            if depth < target_depth:
                item["end"], item["uncertain"] = line_no, False
                result.append(item)
            else:
                still_open.append((item, target_depth))
        opened = still_open
    for item, _ in opened:
        item["end"], item["uncertain"] = len(lines), True
        result.append(item)
    return sorted(result, key=lambda item: (item["start"], item["end"], item["name"]))

def line_kind(line: str, suffix: str, in_comment: bool) -> tuple[str, bool]:
    text = line.strip()
    if not text:
        return "blank", in_comment
    if in_comment:
        return "comment", "*/" not in text
    if text.startswith("/*"):
        return "comment", "*/" not in text
    if text.startswith("//") or (suffix == ".css" and text.startswith("*")):
        return "comment", False
    if suffix == ".css":
        return ("css_rule" if CSS_RULE_RE.match(line) else "css_declaration" if ":" in line else "top_level"), False
    if suffix == ".html":
        return ("markup_tag" if HTML_TAG_RE.search(line) else "markup"), False
    if suffix == ".json":
        return ("json_property" if re.match(r'\s*"[^"\\]+"\s*:', line) else "json_syntax"), False
    if suffix == ".py":
        if text.startswith("#"):
            return "comment", False
        if re.match(r"\s*(?:async\s+)?def\s+", line):
            return "function", False
        if re.match(r"\s*class\s+", line):
            return "class", False
        if re.match(r"\s*(?:from|import)\s+", line):
            return "import_export", False
        return "python_statement", False
    if text.startswith(("import ", "export ")):
        return "import_export", False
    if CLASS_RE.search(line):
        return "class", False
    if FUNCTION_RE.search(line):
        return "function", False
    if EVENT_RE.search(line):
        return "event_handler", False
    if re.match(r"\s*(?:const|let|var)\s+", line):
        return "declaration", False
    return "top_level", False

def extract_edges(lines: list[str], known: list[dict[str, Any]]) -> list[tuple[int, str, str, Optional[str]]]:
    edges = []
    for line_no, line in enumerate(lines, 1):
        line_owner = known[line_no]
        for matcher, kind in ((IMPORT_RE, "import"), (SELECTOR_RE, "selector"), (EVENT_RE, "event"), (ENDPOINT_RE, "endpoint")):
            edges.extend((line_no, kind, match.group(1), line_owner) for match in matcher.finditer(line))
        edges.extend((line_no, "data_attribute", match.group(1), line_owner) for match in DATA_RE.finditer(line))
        edges.extend((line_no, "asset", match.group(1), line_owner) for match in HTML_ASSET_RE.finditer(line))
    return edges


def scan_data(path,data):
    try:source=data.decode("utf-8")
    except UnicodeDecodeError as exc:raise ValueError("Source must be UTF-8 text") from exc
    lines=source.split("\n")
    if source.endswith("\n"):lines.pop()
    if not source:lines=[]
    if len(lines)>MAX_LINES:raise ValueError("Physical line limit exceeded")
    suffix=Path(path).suffix.lower()
    status,notes="ok",""
    if suffix in {".js",".mjs",".cjs"}:
        try:known,reason=javascript_symbols(source)
        except (RecursionError,MemoryError):known,reason=None,"parser_resource_limit"
        if known is None:
            known=symbols(lines,suffix,source)
            for item in known:item["uncertain"]=True
            status,notes="uncertain",reason
    elif suffix==".py":
        try:
            ast.parse(source)
            known=python_symbols(source)
        except (SyntaxError,RecursionError,MemoryError) as exc:
            known=[];status,notes="invalid","python_parse:"+type(exc).__name__
    elif suffix==".rs":
        known=[]
        for number,line in enumerate(lines,1):
            match=re.search(r"\b(fn|struct|enum|trait|mod)\s+([A-Za-z_]\w*)",line)
            if match:known.append({"name":match[2],"kind":match[1],"start":number,"end":number,
                "signature":line.strip()[:300],"parent":None,"uncertain":True})
        status,notes="uncertain","rust_lexical_only"
    else:
        known=[]
        if suffix==".toml":status,notes="uncertain","toml_lexical_only"
    if len(known)>2000:raise ValueError("Symbol count limit exceeded")
    ownership=owners(lines,known);ledger=[];comment=False
    for number,line in enumerate(lines,1):
        kind,comment=line_kind(line,suffix,comment)
        ledger.append((number,kind,ownership[number],sha(line.encode()),"parser_uncertain" if status!="ok" else ""))
    edges=extract_edges(lines,ownership)
    if len(edges)>20000:raise ValueError("Static edge count exceeds limit")
    if any(len(value.encode())>512 for _,_,value,_ in edges):raise ValueError("Static edge value exceeds limit")
    return {"path":path,"language":{".js":"javascript",".mjs":"javascript",".cjs":"javascript",
        ".py":"python",".rs":"rust"}.get(suffix,suffix[1:]),"sha256":sha(data),"line_count":len(lines),
        "parser_status":status,"parser_notes":notes,"symbols":known,"ledger":ledger,"edges":edges}
def store_scan(conn,scan):
    path=scan["path"]
    conn.execute("DELETE FROM files WHERE path=?",(path,))
    conn.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?)",(path,scan["language"],scan["sha256"],
        scan["line_count"],now(),scan["parser_status"],scan["parser_notes"]))
    conn.executemany("INSERT INTO lines VALUES(?,?,?,?,?,?)",[(path,*row) for row in scan["ledger"]])
    conn.executemany("INSERT INTO symbols VALUES(?,?,?,?,?,?,?,?)",[(path,item["name"],item["kind"],
        item["start"],item["end"],item["signature"][:300],item.get("parent"),int(item["uncertain"])) for item in scan["symbols"]])
    conn.executemany("INSERT INTO edges VALUES(?,?,?,?,?)",[(path,*row) for row in scan["edges"]])
def snapshot(rootfd,paths,deadline):
    total=0;records=[]
    for path in paths:
        if time.monotonic()>deadline:raise ValueError("Source scan deadline exceeded")
        data=read_source(rootfd,path);total+=len(data)
        if total>MAX_TOTAL_BYTES:raise ValueError("Source byte limit exceeded")
        records.append((path,data))
    return records
def stale_files(conn,rootfd):
    stale=[];deadline=time.monotonic()+30;total=0
    rows=conn.execute("SELECT path,sha256 FROM files ORDER BY path").fetchall()
    if len(rows)>MAX_FILES:raise ValueError("Index file count exceeds limit")
    for row in rows:
        if time.monotonic()>deadline:raise ValueError("Freshness deadline exceeded")
        try:
            data=read_source(rootfd,row["path"]);total+=len(data)
            if sha(data)!=row["sha256"]:stale.append({"path":row["path"],"reason":"digest_changed"})
        except (OSError,ValueError):stale.append({"path":row["path"],"reason":"missing_or_unreadable"})
        if total>MAX_TOTAL_BYTES:raise ValueError("Freshness source byte limit exceeded")
    return stale
def page(rows,offset,limit):
    selected=[];used=0
    for row in rows[offset:offset+limit]:
        size=len(json.dumps(row,ensure_ascii=False).encode())
        if used+size>12*1024:break
        selected.append(row);used+=size
    next_offset=offset+len(selected)
    return {"results":selected,"count":len(selected),"total":len(rows),
        "next_offset":next_offset,"has_more":next_offset<len(rows)}
def coverage(conn,rootfd,args):
    expected=set(source_files(rootfd,"",time.monotonic()+30))
    rows=conn.execute("""SELECT f.path,f.line_count,f.parser_status,
        COUNT(l.line_no) AS ledger_rows,
        SUM(CASE WHEN l.line_no BETWEEN 1 AND f.line_count THEN 1 ELSE 0 END) AS covered_lines
        FROM files f LEFT JOIN lines l ON l.file_path=f.path GROUP BY f.path ORDER BY f.path""").fetchall()
    if len(rows)>MAX_FILES:raise ValueError("Index file count exceeds limit")
    files=[];total=0;covered=0
    for row in rows:
        item=dict(row);item["covered_lines"]=item["covered_lines"] or 0
        item["gaps"]=max(0,item["line_count"]-item["covered_lines"])
        total+=item["line_count"];covered+=item["covered_lines"];files.append(item)
    stale=stale_files(conn,rootfd);unindexed=sorted(expected-{r["path"] for r in rows})
    kind=args["action"]
    if kind=="gaps":result=page([f for f in files if f["gaps"]],args["offset"],args["limit"])
    elif kind=="stale":result=page(stale,args["offset"],args["limit"])
    else:
        result=page(files,args["offset"],args["limit"])
        result.update(file_count=len(files),total_lines=total,covered_lines=covered,
            gaps=total-covered,percent=100 if not total else round(covered*100/total,2),
            stale_count=len(stale),unindexed_count=len(unindexed),
            stale=page(stale,args["offset"],args["limit"]),
            unindexed=page([{"path":p} for p in unindexed],args["offset"],args["limit"]))
    return result
def scan(conn,rootfd,args):
    scope=args.get("file_path") if args["action"]=="scan_file" else args.get("scope","")
    if scope is None:raise ValueError("scan_file requires file_path")
    deadline=time.monotonic()+30
    paths=source_files(rootfd,scope,deadline)
    if args["action"]=="scan_file" and len(paths)!=1:raise ValueError("scan_file requires one source file")
    batch=args.get("batch_id") or str(uuid.uuid4())
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}",batch):raise ValueError("Invalid batch_id")
    if conn.execute("SELECT 1 FROM batches WHERE batch_id=?",(batch,)).fetchone():
        raise ValueError("Batch identifier already used; inspect progress")
    revision=conn.execute("SELECT revision FROM meta WHERE id=1").fetchone()[0]
    if args.get("expected_revision",revision)!=revision:raise ValueError("Index revision conflict")
    conn.execute("INSERT INTO batches VALUES(?,?,'in_progress',?,0,?,NULL,'',NULL)",(batch,scope,len(paths),now()))
    conn.commit()
    try:
        records=snapshot(rootfd,paths,deadline);scans=[];lines=0
        for path,data in records:
            if time.monotonic()>deadline:raise ValueError("Parser deadline exceeded")
            item=scan_data(path,data);lines+=item["line_count"]
            if lines>MAX_TOTAL_LINES:raise ValueError("Index line count exceeds limit")
            scans.append(item)
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT revision FROM meta WHERE id=1").fetchone()[0]!=revision:
            raise ValueError("Index revision changed during scan")
        # Recheck exact source bytes and full-scope enumeration before replacing
        # derived rows; a raced scan never claims to index the newer content.
        if paths!=source_files(rootfd,scope,deadline):raise ValueError("Source enumeration changed during scan")
        for path,data in records:
            if sha(read_source(rootfd,path))!=sha(data):raise ValueError("Source changed before index commit")
        removed=[]
        if args["action"]=="scan":
            prefix=scope.rstrip("/")
            known=[row[0] for row in conn.execute("SELECT path FROM files")]
            removed=[p for p in known if (not prefix or p==prefix or p.startswith(prefix+"/")) and p not in paths]
            conn.executemany("DELETE FROM files WHERE path=?",[(p,) for p in removed])
        for item in scans:store_scan(conn,item)
        if conn.execute("SELECT COALESCE(SUM(line_count),0) FROM files").fetchone()[0]>MAX_TOTAL_LINES:
            raise ValueError("Stored index line limit exceeded")
        if conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]>MAX_FILES:raise ValueError("Stored file count exceeds limit")
        conn.execute("UPDATE meta SET revision=revision+1 WHERE id=1")
        conn.execute("UPDATE batches SET status='completed',indexed_files=?,completed_at=?,revision=? WHERE batch_id=?",
            (len(scans),now(),revision+1,batch))
        conn.execute("DELETE FROM batches WHERE batch_id NOT IN (SELECT batch_id FROM batches ORDER BY started_at DESC LIMIT 100)")
        conn.commit()
        result=page([{"path":i["path"],"lines":i["line_count"],"symbols":len(i["symbols"]),
            "edges":len(i["edges"]),"parser_status":i["parser_status"]} for i in scans],0,args["limit"])
        result.update(batch_id=batch,indexed_files=len(scans),removed_count=len(removed),
            removed=page([{"path":p} for p in removed],0,args["limit"]),revision=revision+1)
        return result
    except Exception as exc:
        conn.rollback()
        conn.execute("UPDATE batches SET status='failed',completed_at=?,error=? WHERE batch_id=?",
            (now(),type(exc).__name__+": "+str(exc)[:240],batch));conn.commit()
        raise

ACTIONS={"code_index_scan":{"discover","scan","scan_file"},
    "code_index_lookup":{"file","line","symbol","edge"},
    "code_index_coverage":{"report","gaps","stale"},
    "code_index_trace":{"symbol","selector","event","endpoint","data_attribute","import","asset"},
    "code_index_progress":{"list","get"}}
FIELDS={"code_index_scan":{"scope","file_path","batch_id","expected_revision"},
    "code_index_lookup":{"query","file_path","line_no"},
    "code_index_coverage":set(),"code_index_trace":{"kind","value"},
    "code_index_progress":{"batch_id"}}
def validated(name,args):
    if name not in ACTIONS or not isinstance(args,dict) or set(args)-({"root","limit","offset"}|FIELDS[name]|({"action"} if name!="code_index_trace" else set())):
        raise ValueError("Invalid Code Index arguments")
    action=args.get("action",{"code_index_coverage":"report","code_index_progress":"list"}.get(name,""))
    if name=="code_index_trace":action=args.get("kind","")
    if action not in ACTIONS[name]:raise ValueError("Unsupported Code Index action")
    # Providers may populate every optional schema property. Only this action's
    # fields carry semantics; absent mutation fields cannot accidentally appoint paths.
    fields={
        "code_index_scan":{"discover":{"scope"},"scan":{"scope","batch_id","expected_revision"},
            "scan_file":{"file_path","batch_id","expected_revision"}},
        "code_index_lookup":{"file":{"file_path"},"line":{"file_path","line_no"},
            "symbol":{"query","file_path"},"edge":{"query","file_path"}},
        "code_index_coverage":{"report":set(),"gaps":set(),"stale":set()},
        "code_index_trace":{kind:{"value"} for kind in ACTIONS["code_index_trace"]},
        "code_index_progress":{"list":set(),"get":{"batch_id"}},
    }[name][action]
    result={key:value for key,value in args.items() if key in fields|{"root","limit","offset"}}
    result["action"]=action
    result.setdefault("root","webagent-v3")
    if result["root"]=="":result["root"]="webagent-v3"
    result.setdefault("limit",50);result.setdefault("offset",0)
    if result.get("batch_id")=="" and name=="code_index_scan":result.pop("batch_id")
    if result.get("file_path")=="" and name=="code_index_lookup" and action in {"symbol","edge"}:
        result.pop("file_path")
    if name=="code_index_scan" and action=="scan_file" or name=="code_index_lookup" and action in {"file","line"}:
        if not result.get("file_path"):raise ValueError("This action requires file_path")
    if name=="code_index_progress" and action=="get" and not result.get("batch_id"):
        raise ValueError("get progress requires batch_id")
    if type(result["limit"]) is not int or not 1<=result["limit"]<=200 or type(result["offset"]) is not int or not 0<=result["offset"]<=MAX_FILES:
        raise ValueError("Invalid result page bounds")
    relative_parts(result["root"],True)
    for field in ("scope","file_path"):
        if field in result:relative_parts(result[field],field=="scope")
    for field in ("query","value"):
        if field in result and (not isinstance(result[field],str) or len(result[field].encode())>512 or "\0" in result[field]):
            raise ValueError("Invalid query bound")
    if "line_no" in result and (type(result["line_no"]) is not int or not 1<=result["line_no"]<=MAX_LINES):
        raise ValueError("Invalid line number")
    if "expected_revision" in result and (type(result["expected_revision"]) is not int or not 0<=result["expected_revision"]<2**63):
        raise ValueError("Invalid index revision")
    if "batch_id" in result and (not isinstance(result["batch_id"],str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}",result["batch_id"])):
        raise ValueError("Invalid batch_id")
    return result
def lookup(conn,args):
    action=args["action"];path=args.get("file_path");query=args.get("query","")
    if action=="file":
        rows=conn.execute("SELECT * FROM files WHERE (? IS NULL OR path=?) ORDER BY path",(path,path)).fetchall()
    elif action=="line":
        if not path or "line_no" not in args:raise ValueError("line lookup needs file_path and line_no")
        rows=conn.execute("SELECT * FROM lines WHERE file_path=? AND line_no=?",(path,args["line_no"])).fetchall()
    elif action=="symbol":
        rows=conn.execute("SELECT * FROM symbols WHERE instr(name,?)>0 AND (? IS NULL OR file_path=?) ORDER BY file_path,start_line,name LIMIT 5001",(query,path,path)).fetchall()
    else:
        rows=conn.execute("SELECT * FROM edges WHERE instr(value,?)>0 AND (? IS NULL OR file_path=?) ORDER BY file_path,line_no,value LIMIT 5001",(query,path,path)).fetchall()
    result=page([dict(row) for row in rows[:MAX_FILES]],args["offset"],args["limit"])
    result["result_cap_reached"]=len(rows)>MAX_FILES
    return result
def trace(conn,args):
    value=args.get("value")
    if value is None:raise ValueError("trace requires value")
    kind=args["action"]
    if kind=="symbol":rows=conn.execute("SELECT * FROM symbols WHERE name=? ORDER BY file_path,start_line LIMIT 5001",(value,)).fetchall()
    else:rows=conn.execute("SELECT * FROM edges WHERE edge_kind=? AND value=? ORDER BY file_path,line_no LIMIT 5001",(kind,value)).fetchall()
    result=page([dict(row) for row in rows[:MAX_FILES]],args["offset"],args["limit"])
    result.update(kind=kind,value=value,result_cap_reached=len(rows)>MAX_FILES)
    return result
def annotate_freshness(conn,rootfd,result):
    paths={row.get("file_path",row.get("path")) for row in result["results"]}
    state={};total=0
    for path in sorted(paths):
        expected=conn.execute("SELECT sha256 FROM files WHERE path=?",(path,)).fetchone()
        try:
            data=read_source(rootfd,path);total+=len(data)
            stale=expected is None or sha(data)!=expected[0]
        except (OSError,ValueError):stale=True
        if total>MAX_TOTAL_BYTES:raise ValueError("Lookup freshness byte limit exceeded")
        state[path]=stale
    for row in result["results"]:
        row["stale"]=state[row.get("file_path",row.get("path"))]
    result.update(stale_count=sum(state.values()),freshness_scope="returned_files")
    return result
def dispatch(conn,rootfd,name,args):
    if name=="code_index_scan":
        if args["action"]!="discover":return scan(conn,rootfd,args)
        paths=source_files(rootfd,args.get("scope",""),time.monotonic()+30)
        known={row["path"]:row["sha256"] for row in conn.execute("SELECT path,sha256 FROM files")}
        records=snapshot(rootfd,paths,time.monotonic()+30)
        return page([{"path":p,"indexed":p in known,"stale":p in known and sha(data)!=known[p]} for p,data in records],args["offset"],args["limit"])
    if name=="code_index_lookup":return annotate_freshness(conn,rootfd,lookup(conn,args))
    if name=="code_index_coverage":return coverage(conn,rootfd,args)
    if name=="code_index_trace":return annotate_freshness(conn,rootfd,trace(conn,args))
    if args["action"]=="get":
        if "batch_id" not in args:raise ValueError("get progress requires batch_id")
        rows=conn.execute("SELECT * FROM batches WHERE batch_id=?",(args["batch_id"],)).fetchall()
    else:rows=conn.execute("SELECT * FROM batches ORDER BY started_at DESC LIMIT 100").fetchall()
    return page([dict(row) for row in rows],args["offset"],args["limit"])
def publish(conn,indexfd,key):
    blob=conn.serialize()
    if len(blob)>MAX_DB_BYTES:raise ValueError("Derived index publication exceeds limit")
    temp=key+"."+uuid.uuid4().hex+".tmp"
    tempfd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=indexfd)
    try:
        with os.fdopen(tempfd,"wb",closefd=False) as output:
            output.write(blob);output.flush();os.fsync(tempfd)
        os.rename(temp,key+".sqlite",src_dir_fd=indexfd,dst_dir_fd=indexfd)
        os.fsync(indexfd)
    finally:
        os.close(tempfd)
        try:os.unlink(temp,dir_fd=indexfd)
        except FileNotFoundError:pass
def execute(name,args,base):
    """Rust supplies base after caller/session policy. Never accept a runtime DB path."""
    fds=[];conn=None;indexfd=None;key=None;save=False
    try:
        args=validated(name,args);basefd=os.open(base,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);fds.append(basefd)
        rootfd=open_path(basefd,relative_parts(args["root"],True),True);fds.append(rootfd)
        try:os.mkdir(".code-index",mode=0o700,dir_fd=basefd)
        except FileExistsError:pass
        indexfd=open_child_dir(basefd,".code-index");fds.append(indexfd)
        key=sha(args["root"].encode())
        lockfd=os.open(key+".lock",os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=indexfd);fds.append(lockfd)
        if not stat.S_ISREG(os.fstat(lockfd).st_mode) or os.fstat(lockfd).st_nlink!=1:
            raise ValueError("Invalid index lock file")
        fcntl.flock(lockfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        dbfd=os.open(key+".sqlite",os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=indexfd);fds.append(dbfd)
        meta=os.fstat(dbfd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_nlink!=1 or meta.st_size>MAX_DB_BYTES:raise ValueError("Invalid derived index file")
        # Never give SQLite a caller-writable filename. Deserialize the confined
        # derived inode into memory; atomic publication below uses anchored fds.
        raw=bytearray()
        while len(raw)<=MAX_DB_BYTES:
            chunk=os.read(dbfd,min(65536,MAX_DB_BYTES+1-len(raw)))
            if not chunk:break
            raw.extend(chunk)
        if len(raw)>MAX_DB_BYTES:raise ValueError("Derived index exceeds limit")
        conn=sqlite3.connect(":memory:",timeout=1)
        if raw:conn.deserialize(bytes(raw))
        conn.row_factory=sqlite3.Row
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("PRAGMA journal_mode=MEMORY");conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA max_page_count="+str(MAX_DB_BYTES//4096))
        conn.executescript(DDL)
        if conn.execute("SELECT schema_version FROM meta WHERE id=1").fetchone()[0]!=1:raise ValueError("Unsupported derived index schema")
        save=name=="code_index_scan" and args["action"]!="discover"
        result=dispatch(conn,rootfd,name,args)
        result.update(status="success",root=args["root"],revision=conn.execute("SELECT revision FROM meta WHERE id=1").fetchone()[0],
            source_kind="untrusted_source_index",auto_rescan=False)
        if len(json.dumps(result,ensure_ascii=False).encode())>MAX_OUTPUT_BYTES:raise ValueError("Index result exceeds response limit; request a smaller page")
        if save:publish(conn,indexfd,key);save=False
        return result
    except BlockingIOError:return {"status":"error","error_kind":"index_busy","output":"Index scan already running"}
    except (OSError,ValueError,sqlite3.Error,RecursionError) as exc:
        if save and conn is not None:
            try:publish(conn,indexfd,key)
            except (OSError,ValueError,sqlite3.Error):pass
        return {"status":"error","error_kind":"index_rejected","output":type(exc).__name__+": "+str(exc)[:300]}
    finally:
        if conn is not None:conn.close()
        for fd in reversed(fds):os.close(fd)
def schemas():
    path={"type":"string","maxLength":512}
    text={"type":"string","maxLength":512}
    common={"root":path,"limit":{"type":"integer","minimum":1,"maximum":200},
        "offset":{"type":"integer","minimum":0,"maximum":MAX_FILES}}
    extra={
        "code_index_scan":{"scope":path,"file_path":path,"batch_id":{"type":"string","maxLength":80},
            "expected_revision":{"type":"integer","minimum":0}},
        "code_index_lookup":{"query":text,"file_path":path,"line_no":{"type":"integer","minimum":1,"maximum":MAX_LINES}},
        "code_index_coverage":{},
        "code_index_trace":{"kind":{"type":"string","enum":sorted(ACTIONS["code_index_trace"])},"value":text},
        "code_index_progress":{"batch_id":{"type":"string","maxLength":80}}}
    result={}
    for name in ACTIONS:
        properties={**common,**extra[name]}
        if name!="code_index_trace":properties["action"]={"type":"string","enum":sorted(ACTIONS[name])}
        required={"code_index_scan":["action"],"code_index_lookup":["action"],
            "code_index_trace":["kind","value"]}.get(name,[])
        result[name]={"type":"object","properties":properties,"required":required,"additionalProperties":False}
    return result
