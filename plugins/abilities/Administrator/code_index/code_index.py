"""Source-derived, line-complete UI source index.

Code Index is deliberately independent from the hand-maintained source index.
It reads current source, records one ledger row per physical line, and retains
static symbols and UI edges in ``data/db/code_index.db``.
"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from tree_sitter import Language, Parser
    import tree_sitter_javascript
except ImportError:  # Dependency failure must never make the index unusable.
    Language = Parser = tree_sitter_javascript = None

logger = logging.getLogger(__name__)
TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = set()

DEFAULT_SCOPE = ("ui", "app", "v3/ui")
SOURCE_SUFFIXES = {".js", ".mjs", ".cjs", ".css", ".html", ".json", ".py"}
EXCLUDED_PARTS = {"data", "docs", "vendor", "node_modules", ".git", "backups", "__pycache__"}
EXCLUDED_SUFFIXES = {".bak", ".orig", ".pyc", ".disabled"}
FUNCTION_RE = re.compile(r"(?:^|[;{}\s])(?:async\s+)?function\s*\*?\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)|(?:^|[;{}\s])(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>")
CLASS_RE = re.compile(r"(?:^|[;{}\s])class\s+([A-Za-z_$][\w$]*)")
IMPORT_RE = re.compile(r"(?:import\s+(?:[^;]+?\s+from\s+)?|import\s*\()\s*['\"]([^'\"]+)['\"]")
SELECTOR_RE = re.compile(r"(?:querySelector(?:All)?|closest|matches)\s*\(\s*['\"]([^'\"]+)['\"]")
EVENT_RE = re.compile(r"(?:addEventListener|on)\s*\(\s*['\"]([^'\"]+)['\"]")
DATA_RE = re.compile(r"\b(data-[A-Za-z0-9_-]+)\b")
ENDPOINT_RE = re.compile(r"['\"`]((?:/api/|/session-|/chat/)[^'\"`\s?]*)")
CSS_RULE_RE = re.compile(r"^\s*([^@/][^{]+)\{")
HTML_TAG_RE = re.compile(r"<\s*([A-Za-z][\w:-]*)\b")
HTML_ASSET_RE = re.compile(r"(?:src|href)\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)

DDL = """
CREATE TABLE IF NOT EXISTS ci_files (
 path TEXT PRIMARY KEY, language TEXT NOT NULL, sha256 TEXT NOT NULL,
 line_count INTEGER NOT NULL, scanned_at TEXT NOT NULL, parser_status TEXT NOT NULL,
 parser_notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ci_lines (
 file_path TEXT NOT NULL REFERENCES ci_files(path) ON DELETE CASCADE,
 line_no INTEGER NOT NULL, kind TEXT NOT NULL, owner_symbol TEXT,
 text_digest TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(file_path, line_no)
);
CREATE INDEX IF NOT EXISTS ci_lines_owner ON ci_lines(file_path, owner_symbol);
CREATE TABLE IF NOT EXISTS ci_symbols (
 id INTEGER PRIMARY KEY AUTOINCREMENT, file_path TEXT NOT NULL REFERENCES ci_files(path) ON DELETE CASCADE,
 name TEXT NOT NULL, kind TEXT NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
 signature TEXT NOT NULL DEFAULT '', parent_symbol TEXT
);
CREATE INDEX IF NOT EXISTS ci_symbols_name ON ci_symbols(name);
CREATE TABLE IF NOT EXISTS ci_edges (
 id INTEGER PRIMARY KEY AUTOINCREMENT, file_path TEXT NOT NULL REFERENCES ci_files(path) ON DELETE CASCADE,
 line_no INTEGER NOT NULL, edge_kind TEXT NOT NULL, value TEXT NOT NULL, owner_symbol TEXT
);
CREATE INDEX IF NOT EXISTS ci_edges_value ON ci_edges(value);
CREATE TABLE IF NOT EXISTS ci_batches (
 batch_id TEXT PRIMARY KEY, scope TEXT NOT NULL, status TEXT NOT NULL,
 requested_files INTEGER NOT NULL DEFAULT 0, indexed_files INTEGER NOT NULL DEFAULT 0,
 started_at TEXT NOT NULL, completed_at TEXT, error TEXT NOT NULL DEFAULT ''
);
"""


def root() -> Path:
    return Path(__file__).resolve().parents[4]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def db_path() -> Path:
    return root() / "data" / "db" / "code_index.db"


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DDL)
    conn.commit()
    return conn


def relative(path: Path) -> str:
    return path.resolve().relative_to(root().resolve()).as_posix()


def allowed(path: str) -> bool:
    value = path.strip("/")
    if any(part in EXCLUDED_PARTS for part in value.split("/")):
        return False
    return any(value == scope or value.startswith(scope + "/") for scope in DEFAULT_SCOPE)


def excluded_file(path: Path) -> bool:
    lowered = path.name.lower()
    backup_parts = {"backup", "backups", "original", "originals"}
    path_has_backup_copy = any(
        part.lower() in backup_parts
        or part.lower().endswith((" - backup", " - original"))
        for part in path.parts
    )
    return (
        path_has_backup_copy
        or lowered.endswith(".min.js")
        or any(lowered.endswith(suffix) for suffix in EXCLUDED_SUFFIXES)
    )


def language_for(suffix: str) -> str:
    return {
        ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
        ".css": "css", ".html": "html", ".json": "json", ".py": "python",
    }.get(suffix, suffix.lstrip("."))


def source_files(scope: Optional[str] = None) -> list[Path]:
    project = root().resolve()
    requested = [scope.strip("/")] if scope else list(DEFAULT_SCOPE)
    found: set[Path] = set()
    for item in requested:
        candidate = (project / item).resolve()
        try:
            candidate_relative = relative(candidate)
        except ValueError:
            continue
        if not allowed(candidate_relative) or not candidate.exists():
            continue
        if candidate.is_file() and candidate.suffix.lower() in SOURCE_SUFFIXES and not excluded_file(candidate):
            found.add(candidate)
        elif candidate.is_dir():
            for file in candidate.rglob("*"):
                if file.is_file() and file.suffix.lower() in SOURCE_SUFFIXES and not excluded_file(file) and allowed(relative(file)):
                    found.add(file)
    return sorted(found)


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


def owner(line_no: int, known: list[dict[str, Any]]) -> Optional[str]:
    containing = [item for item in known if item["start"] <= line_no <= item["end"]]
    return min(containing, key=lambda item: item["end"] - item["start"])["name"] if containing else None


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
        line_owner = owner(line_no, known)
        for matcher, kind in ((IMPORT_RE, "import"), (SELECTOR_RE, "selector"), (EVENT_RE, "event"), (ENDPOINT_RE, "endpoint")):
            edges.extend((line_no, kind, match.group(1), line_owner) for match in matcher.finditer(line))
        edges.extend((line_no, "data_attribute", match.group(1), line_owner) for match in DATA_RE.finditer(line))
        edges.extend((line_no, "asset", match.group(1), line_owner) for match in HTML_ASSET_RE.finditer(line))
    return edges


def scan_file(path: Path) -> dict[str, Any]:
    source = path.read_text(encoding="utf-8", errors="replace")
    lines = source.splitlines()
    if source and not lines:
        lines = [source]
    suffix = path.suffix.lower()
    parser_status, parser_notes = "ok", ""
    if suffix in {".js", ".mjs", ".cjs"}:
        parsed, reason = javascript_symbols(source)
        if parsed is None:
            known = symbols(lines, suffix, source)
            parser_status, parser_notes = "uncertain", reason
        else:
            known = parsed
    else:
        known = symbols(lines, suffix, source)
    comment, ledger = False, []
    for line_no, line in enumerate(lines, 1):
        kind, comment = line_kind(line, suffix, comment)
        uncertain = any(item["uncertain"] and item["start"] <= line_no <= item["end"] for item in known)
        ledger.append((line_no, kind, owner(line_no, known), digest(line), "parser_uncertain" if uncertain else ""))
    uncertain_names = [item["name"] for item in known if item["uncertain"]]
    if uncertain_names:
        parser_status = "uncertain"
        parser_notes = ", ".join(filter(None, (parser_notes, *uncertain_names)))
    if suffix == ".py" and not known and source.strip():
        try:
            ast.parse(source)
        except SyntaxError as exc:
            parser_status, uncertain_names = "invalid", [f"syntax_error:{exc.lineno}"]
            parser_notes = ", ".join(uncertain_names)
    return {"path": relative(path), "source": source, "language": language_for(suffix), "line_count": len(lines), "symbols": known, "ledger": ledger, "edges": extract_edges(lines, known), "parser_status": parser_status, "parser_notes": parser_notes}


def store_scan(conn: sqlite3.Connection, scan: dict[str, Any]) -> dict[str, Any]:
    path = scan["path"]
    conn.execute("DELETE FROM ci_files WHERE path = ?", (path,))
    conn.execute("INSERT INTO ci_files(path,language,sha256,line_count,scanned_at,parser_status,parser_notes) VALUES (?,?,?,?,?,?,?)", (path, scan["language"], digest(scan["source"]), scan["line_count"], now(), scan["parser_status"], scan["parser_notes"]))
    conn.executemany("INSERT INTO ci_lines(file_path,line_no,kind,owner_symbol,text_digest,note) VALUES (?,?,?,?,?,?)", [(path, *row) for row in scan["ledger"]])
    conn.executemany("INSERT INTO ci_symbols(file_path,name,kind,start_line,end_line,signature,parent_symbol) VALUES (?,?,?,?,?,?,?)", [(path, item["name"], item["kind"], item["start"], item["end"], item["signature"], item.get("parent")) for item in scan["symbols"]])
    conn.executemany("INSERT INTO ci_edges(file_path,line_no,edge_kind,value,owner_symbol) VALUES (?,?,?,?,?)", [(path, *edge) for edge in scan["edges"]])
    return {"path": path, "lines": scan["line_count"], "symbols": len(scan["symbols"]), "edges": len(scan["edges"]), "parser_status": scan["parser_status"]}


def stale(conn: sqlite3.Connection) -> list[dict[str, str]]:
    result = []
    for row in conn.execute("SELECT path,sha256 FROM ci_files ORDER BY path"):
        path = root() / row["path"]
        if not path.is_file():
            result.append({"path": row["path"], "reason": "missing"})
        elif digest(path.read_text(encoding="utf-8", errors="replace")) != row["sha256"]:
            result.append({"path": row["path"], "reason": "digest_changed"})
    return result


def coverage(conn: sqlite3.Connection) -> dict[str, Any]:
    files, total, covered, gaps = [], 0, 0, 0
    indexed_paths = {row["path"] for row in conn.execute("SELECT path FROM ci_files")}
    expected_paths = {relative(path) for path in source_files()}
    unindexed = sorted(expected_paths - indexed_paths)
    for row in conn.execute("SELECT * FROM ci_files ORDER BY path"):
        expected = row["line_count"]
        distinct = conn.execute("SELECT COUNT(DISTINCT line_no) FROM ci_lines WHERE file_path=?", (row["path"],)).fetchone()[0]
        count = conn.execute("SELECT COUNT(*) FROM ci_lines WHERE file_path=?", (row["path"],)).fetchone()[0]
        missing = max(0, expected - distinct)
        total, covered, gaps = total + expected, covered + min(expected, distinct), gaps + missing
        files.append({"path": row["path"], "line_count": expected, "ledger_rows": count, "covered_lines": distinct, "gaps": missing, "parser_status": row["parser_status"]})
    return {"files": files, "file_count": len(files), "total_lines": total, "covered_lines": covered, "gaps": gaps, "percent": 100 if not total else round(covered * 100 / total, 2), "unindexed": unindexed}


async def index_scan(action: str, scope: Optional[str] = None, file_path: Optional[str] = None, batch_id: Optional[str] = None) -> str:
    conn = connect()
    try:
        if action == "discover":
            current = {row["path"]: row["sha256"] for row in conn.execute("SELECT path,sha256 FROM ci_files")}
            result = []
            for path in source_files(scope):
                path_string, current_digest = relative(path), digest(path.read_text(encoding="utf-8", errors="replace"))
                result.append({"path": path_string, "indexed": path_string in current, "stale": path_string in current and current[path_string] != current_digest})
            return json.dumps({"status": "ok", "scope": scope or "default", "count": len(result), "files": result})
        if action not in {"scan", "scan_file"}:
            return json.dumps({"status": "error", "message": "action must be discover, scan, or scan_file"})
        target = file_path if action == "scan_file" else scope
        if action == "scan_file" and (not file_path or not allowed(file_path)):
            return json.dumps({"status": "error", "message": "file_path must be an approved indexed source path"})
        paths = source_files(target)
        if action == "scan_file" and len(paths) != 1:
            return json.dumps({"status": "error", "message": "approved source file not found"})
        identifier = batch_id or f"scan:{now()}"
        conn.execute("INSERT OR REPLACE INTO ci_batches(batch_id,scope,status,requested_files,indexed_files,started_at,error) VALUES (?,?,'in_progress',?,0,?,'')", (identifier, target or "default", len(paths), now()))
        removed: list[str] = []
        if action == "scan":
            scopes = [target.strip("/")] if target else list(DEFAULT_SCOPE)
            indexed_paths = {relative(path) for path in paths}
            known_paths = [row["path"] for row in conn.execute("SELECT path FROM ci_files") if any(row["path"] == scope or row["path"].startswith(scope + "/") for scope in scopes)]
            removed = [path for path in known_paths if path not in indexed_paths]
            conn.executemany("DELETE FROM ci_files WHERE path = ?", [(path,) for path in removed])

        result = [store_scan(conn, scan_file(path)) for path in paths]
        conn.execute("UPDATE ci_batches SET status='completed',indexed_files=?,completed_at=? WHERE batch_id=?", (len(result), now(), identifier))
        conn.commit()
        return json.dumps({"status": "ok", "batch_id": identifier, "count": len(result), "removed": removed, "files": result, "coverage": coverage(conn)})
    except Exception as exc:
        logger.exception("Code Index scan failed")
        return json.dumps({"status": "error", "message": str(exc)})
    finally:
        conn.close()


async def index_lookup(action: str, query: Optional[str] = None, file_path: Optional[str] = None, line_no: Optional[int] = None, limit: int = 50) -> str:
    conn = connect()
    try:
        limit = max(1, min(int(limit), 500))
        if action == "file":
            rows = conn.execute("SELECT * FROM ci_files WHERE (? IS NULL OR path=?) ORDER BY path LIMIT ?", (file_path, file_path, limit)).fetchall()
        elif action == "line":
            if not file_path or line_no is None:
                return json.dumps({"status": "error", "message": "file_path and line_no required"})
            rows = conn.execute("SELECT * FROM ci_lines WHERE file_path=? AND line_no=?", (file_path, line_no)).fetchall()
        elif action == "symbol":
            rows = conn.execute("SELECT * FROM ci_symbols WHERE name LIKE ? AND (? IS NULL OR file_path=?) ORDER BY file_path,start_line LIMIT ?", (f"%{query or ''}%", file_path, file_path, limit)).fetchall()
        elif action == "edge":
            rows = conn.execute("SELECT * FROM ci_edges WHERE value LIKE ? AND (? IS NULL OR file_path=?) ORDER BY file_path,line_no LIMIT ?", (f"%{query or ''}%", file_path, file_path, limit)).fetchall()
        else:
            return json.dumps({"status": "error", "message": "action must be file, line, symbol, or edge"})
        return json.dumps({"status": "ok", "count": len(rows), "results": [dict(row) for row in rows]})
    finally:
        conn.close()


async def index_coverage(action: str = "report") -> str:
    conn = connect()
    try:
        if action == "report":
            result = coverage(conn)
            result.update({"status": "ok", "stale": stale(conn)})
            return json.dumps(result)
        if action == "gaps":
            return json.dumps({"status": "ok", "gaps": [item for item in coverage(conn)["files"] if item["gaps"]]})
        if action == "stale":
            rows = stale(conn)
            return json.dumps({"status": "ok", "count": len(rows), "files": rows})
        return json.dumps({"status": "error", "message": "action must be report, gaps, or stale"})
    finally:
        conn.close()


async def index_trace(kind: str, value: str, limit: int = 100) -> str:
    conn = connect()
    try:
        limit = max(1, min(int(limit), 500))
        if kind == "symbol":
            rows = conn.execute("SELECT file_path,start_line AS line_no,kind,name AS value,signature FROM ci_symbols WHERE name=? ORDER BY file_path,start_line LIMIT ?", (value, limit)).fetchall()
        elif kind in {"selector", "event", "endpoint", "data_attribute", "import", "asset"}:
            rows = conn.execute("SELECT file_path,line_no,edge_kind AS kind,value,owner_symbol FROM ci_edges WHERE edge_kind=? AND value=? ORDER BY file_path,line_no LIMIT ?", (kind, value, limit)).fetchall()
        else:
            return json.dumps({"status": "error", "message": "unsupported trace kind"})
        return json.dumps({"status": "ok", "kind": kind, "value": value, "count": len(rows), "nodes": [dict(row) for row in rows]})
    finally:
        conn.close()


async def index_progress(action: str = "list", batch_id: Optional[str] = None) -> str:
    conn = connect()
    try:
        if action == "list":
            rows = conn.execute("SELECT * FROM ci_batches ORDER BY started_at DESC LIMIT 100").fetchall()
        elif action == "get" and batch_id:
            rows = conn.execute("SELECT * FROM ci_batches WHERE batch_id=?", (batch_id,)).fetchall()
        else:
            return json.dumps({"status": "error", "message": "use list or get with batch_id"})
        return json.dumps({"status": "ok", "count": len(rows), "batches": [dict(row) for row in rows]})
    finally:
        conn.close()


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "", **_ctx):
    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update({
        "code_index_scan": {"type": "object", "properties": {"action": {"type": "string", "enum": ["discover", "scan", "scan_file"]}, "scope": {"type": "string"}, "file_path": {"type": "string"}, "batch_id": {"type": "string"}}, "required": ["action"]},
        "code_index_lookup": {"type": "object", "properties": {"action": {"type": "string", "enum": ["file", "line", "symbol", "edge"]}, "query": {"type": "string"}, "file_path": {"type": "string"}, "line_no": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["action"]},
        "code_index_coverage": {"type": "object", "properties": {"action": {"type": "string", "enum": ["report", "gaps", "stale"]}}, "required": ["action"]},
        "code_index_trace": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["symbol", "selector", "event", "endpoint", "data_attribute", "import", "asset"]}, "value": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["kind", "value"]},
        "code_index_progress": {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "get"]}, "batch_id": {"type": "string"}}, "required": ["action"]},
    })
    DESTRUCTIVE.clear()
    DESTRUCTIVE.add("code_index_scan")
    return {"code_index_scan": index_scan, "code_index_lookup": index_lookup, "code_index_coverage": index_coverage, "code_index_trace": index_trace, "code_index_progress": index_progress}
