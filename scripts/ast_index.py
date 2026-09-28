# /// script
# requires-python = ">=3.11"
# ///
"""Persistent, repo-local AST index for Python, JavaScript, and TypeScript."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

VERSION = 1
EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
}
SKIP_PARTS = {".git", ".ast-index", ".venv", "node_modules", "dist", "build"}
MAX_SOURCE_BYTES = 2_000_000


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def db_path(root: Path) -> Path:
    return root / ".ast-index" / "index.sqlite"


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def stable_id(path: str, kind: str, qualified_name: str) -> str:
    raw = f"{path}\0{kind}\0{qualified_name}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def connect(root: Path) -> sqlite3.Connection:
    target = db_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
          key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS files (
          path TEXT PRIMARY KEY, language TEXT NOT NULL, sha256 TEXT NOT NULL,
          size INTEGER NOT NULL, indexed_at REAL NOT NULL, parse_errors INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS symbols (
          id TEXT PRIMARY KEY, path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
          name TEXT NOT NULL, qualified_name TEXT NOT NULL, kind TEXT NOT NULL,
          start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
          parent_id TEXT, signature TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
        CREATE INDEX IF NOT EXISTS idx_symbols_qualified ON symbols(qualified_name);
        CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(path);
        CREATE TABLE IF NOT EXISTS imports (
          id INTEGER PRIMARY KEY, path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
          module TEXT NOT NULL, imported_name TEXT, alias TEXT, line INTEGER NOT NULL,
          resolved_path TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_imports_path ON imports(path);
        CREATE TABLE IF NOT EXISTS calls (
          id INTEGER PRIMARY KEY, path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
          caller_symbol_id TEXT, callee_name TEXT NOT NULL, line INTEGER NOT NULL,
          column_no INTEGER NOT NULL, resolved_symbol_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee_name);
        CREATE INDEX IF NOT EXISTS idx_calls_resolved ON calls(resolved_symbol_id);
        """
    )
    conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('version',?)", (str(VERSION),))
    conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('root',?)", (str(root),))
    return conn


def source_files(root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard"],
            cwd=root,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=True,
        )
        candidates = [root / line for line in result.stdout.splitlines() if line]
    except (OSError, subprocess.CalledProcessError):
        candidates = list(root.rglob("*"))
    return sorted(
        path
        for path in candidates
        if path.is_file()
        and path.suffix.lower() in EXTENSIONS
        and not any(part in SKIP_PARTS for part in path.relative_to(root).parts)
        and "vendor" not in path.relative_to(root).parts
        and not path.name.endswith(".min.js")
        and path.stat().st_size <= MAX_SOURCE_BYTES
    )


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class SymbolRecord:
    id: str
    path: str
    name: str
    qualified_name: str
    kind: str
    start_line: int
    end_line: int
    parent_id: str | None = None
    signature: str | None = None


class PythonIndexer(ast.NodeVisitor):
    def __init__(self, relative_path: str, source: str) -> None:
        self.path = relative_path
        self.source = source
        self.symbols: list[SymbolRecord] = []
        self.imports: list[tuple[str, str | None, str | None, int]] = []
        self.calls: list[tuple[str | None, str, int, int]] = []
        self.stack: list[SymbolRecord] = []

    def add_symbol(self, node: ast.AST, name: str, kind: str) -> SymbolRecord:
        qualified = ".".join([*(item.name for item in self.stack), name])
        record = SymbolRecord(
            stable_id(self.path, kind, qualified),
            self.path,
            name,
            qualified,
            kind,
            getattr(node, "lineno", 1),
            getattr(node, "end_lineno", getattr(node, "lineno", 1)),
            self.stack[-1].id if self.stack else None,
            ast.get_source_segment(self.source, node).splitlines()[0][:500]
            if ast.get_source_segment(self.source, node)
            else None,
        )
        if any(existing.id == record.id for existing in self.symbols):
            record.id = stable_id(self.path, kind, f"{qualified}@{record.start_line}")
        self.symbols.append(record)
        return record

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        record = self.add_symbol(node, node.name, "class")
        self.stack.append(record)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        kind = "method" if self.stack and self.stack[-1].kind == "class" else "function"
        record = self.add_symbol(node, node.name, kind)
        self.stack.append(record)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append((alias.name, None, alias.asname, node.lineno))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = "." * node.level + (node.module or "")
        for alias in node.names:
            self.imports.append((module, alias.name, alias.asname, node.lineno))

    def visit_Call(self, node: ast.Call) -> None:
        name = dotted_python_name(node.func)
        if name:
            self.calls.append(
                (self.stack[-1].id if self.stack else None, name, node.lineno, node.col_offset)
            )
        self.generic_visit(node)


def dotted_python_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_python_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


class JavaScriptIndexer:
    """Deterministic structural indexer for JS/TS without native dependencies."""

    DEF_PATTERNS = (
        ("class", re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)")),
        ("function", re.compile(r"\b(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(")),
        ("function", re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>")),
        ("function", re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?function\b")),
    )
    CALL_RE = re.compile(r"(?<![\w$])([A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*)\s*\(")
    IMPORT_RE = re.compile(
        r"^\s*import\s+(?:(.*?)\s+from\s+)?['\"]([^'\"]+)['\"]",
        re.MULTILINE,
    )
    REQUIRE_RE = re.compile(r"\brequire\s*\(\s*['\"]([^'\"]+)['\"]\s*\)")
    CALL_KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "typeof", "delete"}

    def __init__(self, relative_path: str, data: bytes, language: str) -> None:
        self.path = relative_path
        self.text = data.decode("utf-8", "replace")
        self.language = language
        self.symbols: list[SymbolRecord] = []
        self.imports: list[tuple[str, str | None, str | None, int]] = []
        self.calls: list[tuple[str | None, str, int, int]] = []

    def line_col(self, offset: int) -> tuple[int, int]:
        line = self.text.count("\n", 0, offset) + 1
        last = self.text.rfind("\n", 0, offset)
        return line, offset if last < 0 else offset - last - 1

    def run(self) -> int:
        masked = mask_js_literals_and_comments(self.text)
        for kind, pattern in self.DEF_PATTERNS:
            for match in pattern.finditer(masked):
                name = match.group(1)
                line, _ = self.line_col(match.start())
                signature = self.text[match.start() : self.text.find("\n", match.start()) if "\n" in self.text[match.start():] else len(self.text)][:500]
                record = SymbolRecord(
                    stable_id(self.path, kind, name), self.path, name, name, kind,
                    line, line, None, signature,
                )
                if not any(item.id == record.id for item in self.symbols):
                    self.symbols.append(record)
        for match in self.IMPORT_RE.finditer(self.text):
            line, _ = self.line_col(match.start())
            self.imports.append((match.group(2), match.group(1), None, line))
        for match in self.REQUIRE_RE.finditer(self.text):
            line, _ = self.line_col(match.start())
            self.imports.append((match.group(1), None, None, line))
        for match in self.CALL_RE.finditer(masked):
            name = re.sub(r"\s+", "", match.group(1))
            if name in self.CALL_KEYWORDS:
                continue
            line, column = self.line_col(match.start())
            owner = next((item.id for item in reversed(self.symbols) if item.start_line <= line), None)
            self.calls.append((owner, name, line, column))
        return 0


def mask_js_literals_and_comments(text: str) -> str:
    """Replace comments and string contents with spaces while preserving newlines."""
    pattern = re.compile(
        r"//[^\n]*|/\*[\s\S]*?\*/|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`"
    )
    return pattern.sub(lambda match: re.sub(r"[^\n]", " ", match.group(0)), text)


def resolve_module(root: Path, source_path: str, module: str, language: str) -> str | None:
    base = (root / source_path).parent
    if language == "python":
        dots = len(module) - len(module.lstrip("."))
        clean = module.lstrip(".")
        if dots:
            for _ in range(max(0, dots - 1)):
                base = base.parent
        else:
            base = root
        candidate = base.joinpath(*clean.split(".")) if clean else base
        options = [candidate.with_suffix(".py"), candidate / "__init__.py"]
    elif module.startswith("."):
        candidate = (base / module).resolve()
        options = [candidate.with_suffix(ext) for ext in EXTENSIONS if ext != ".py"]
        options += [candidate / f"index{ext}" for ext in EXTENSIONS if ext != ".py"]
    else:
        return None
    for option in options:
        try:
            return option.relative_to(root).as_posix() if option.is_file() else None
        except ValueError:
            continue
    return None


def insert_records(
    conn: sqlite3.Connection,
    relative: str,
    symbols: Iterable[SymbolRecord],
    imports: Iterable[tuple[str, str | None, str | None, int]],
    calls: Iterable[tuple[str | None, str, int, int]],
) -> None:
    conn.executemany(
        "INSERT INTO symbols VALUES(?,?,?,?,?,?,?,?,?)",
        [tuple(record.__dict__.values()) for record in symbols],
    )
    conn.executemany(
        "INSERT INTO imports(path,module,imported_name,alias,line) VALUES(?,?,?,?,?)",
        [(relative, *item) for item in imports],
    )
    conn.executemany(
        "INSERT INTO calls(path,caller_symbol_id,callee_name,line,column_no) VALUES(?,?,?,?,?)",
        [(relative, *item) for item in calls],
    )


def index_file(conn: sqlite3.Connection, root: Path, path: Path, data: bytes, sha: str) -> tuple[int, int, int]:
    relative = path.relative_to(root).as_posix()
    language = EXTENSIONS[path.suffix.lower()]
    conn.execute("DELETE FROM files WHERE path=?", (relative,))
    parse_errors = 0
    if language == "python":
        text = data.decode("utf-8", "replace")
        visitor = PythonIndexer(relative, text)
        try:
            visitor.visit(ast.parse(text, filename=relative, type_comments=True))
        except SyntaxError:
            parse_errors = 1
        symbols, imports, calls = visitor.symbols, visitor.imports, visitor.calls
    else:
        visitor = JavaScriptIndexer(relative, data, language)
        parse_errors = visitor.run()
        symbols, imports, calls = visitor.symbols, visitor.imports, visitor.calls
    conn.execute(
        "INSERT INTO files VALUES(?,?,?,?,?,?)",
        (relative, language, sha, len(data), time.time(), parse_errors),
    )
    insert_records(conn, relative, symbols, imports, calls)
    return len(symbols), len(imports), len(calls)


def resolve_edges(conn: sqlite3.Connection, root: Path) -> None:
    rows = conn.execute("SELECT id,path,module FROM imports").fetchall()
    for row in rows:
        language_row = conn.execute("SELECT language FROM files WHERE path=?", (row["path"],)).fetchone()
        resolved = resolve_module(root, row["path"], row["module"], language_row["language"])
        conn.execute("UPDATE imports SET resolved_path=? WHERE id=?", (resolved, row["id"]))
    symbols = conn.execute("SELECT id,name,qualified_name,path FROM symbols").fetchall()
    by_name: dict[str, list[sqlite3.Row]] = {}
    for symbol in symbols:
        by_name.setdefault(symbol["name"], []).append(symbol)
        by_name.setdefault(symbol["qualified_name"], []).append(symbol)
    calls = conn.execute("SELECT id,path,callee_name FROM calls").fetchall()
    for call in calls:
        leaf = call["callee_name"].split(".")[-1]
        candidates = by_name.get(call["callee_name"], []) or by_name.get(leaf, [])
        resolved = None
        if candidates:
            same_file = [item for item in candidates if item["path"] == call["path"]]
            resolved = (same_file or candidates)[0]["id"]
        conn.execute("UPDATE calls SET resolved_symbol_id=? WHERE id=?", (resolved, call["id"]))


def refresh(root: Path, full: bool = False, verbose: bool = False) -> None:
    conn = connect(root)
    started = time.time()
    paths = source_files(root)
    current = {path.relative_to(root).as_posix() for path in paths}
    known = {row["path"]: row["sha256"] for row in conn.execute("SELECT path,sha256 FROM files")}
    removed = set(known) - current
    for relative in removed:
        conn.execute("DELETE FROM files WHERE path=?", (relative,))
    changed = 0
    totals = [0, 0, 0]
    errors: list[dict[str, str]] = []
    for path in paths:
        data = path.read_bytes()
        sha = digest(data)
        relative = path.relative_to(root).as_posix()
        if not full and known.get(relative) == sha:
            continue
        if verbose:
            print(f"indexing {relative}", file=sys.stderr, flush=True)
        try:
            counts = index_file(conn, root, path, data, sha)
            totals = [a + b for a, b in zip(totals, counts)]
            changed += 1
            conn.commit()
        except Exception as exc:  # keep the rest of the index usable
            errors.append({"path": relative, "error": f"{type(exc).__name__}: {exc}"})
    resolve_edges(conn, root)
    conn.execute("INSERT OR REPLACE INTO metadata VALUES('refreshed_at',?)", (str(time.time()),))
    conn.commit()
    status = status_data(conn, root)
    status.update(
        {
            "changed_files": changed,
            "removed_files": len(removed),
            "new_symbols": totals[0],
            "new_imports": totals[1],
            "new_calls": totals[2],
            "duration_seconds": round(time.time() - started, 3),
            "errors": errors,
        }
    )
    emit(status)


def status_data(conn: sqlite3.Connection, root: Path) -> dict[str, Any]:
    counts = {}
    for table in ("files", "symbols", "imports", "calls"):
        counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    refreshed = conn.execute("SELECT value FROM metadata WHERE key='refreshed_at'").fetchone()
    return {
        "root": str(root),
        "database": str(db_path(root)),
        **counts,
        "refreshed_at": float(refreshed[0]) if refreshed else None,
    }


def symbol_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def find_symbols(conn: sqlite3.Connection, query: str, limit: int) -> list[dict[str, Any]]:
    exact = conn.execute(
        """SELECT *, CASE WHEN name=? THEN 0 WHEN qualified_name=? THEN 1
              WHEN name LIKE ? THEN 2 ELSE 3 END AS rank
           FROM symbols WHERE name LIKE ? OR qualified_name LIKE ?
           ORDER BY rank, length(qualified_name), path, start_line LIMIT ?""",
        (query, query, f"{query}%", f"%{query}%", f"%{query}%", limit),
    ).fetchall()
    return [symbol_dict(row) for row in exact]


def require_symbol(conn: sqlite3.Connection, value: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM symbols WHERE id=?", (value,)).fetchone()
    if row:
        return row
    matches = conn.execute(
        "SELECT * FROM symbols WHERE name=? OR qualified_name=? ORDER BY path,start_line LIMIT 2",
        (value, value),
    ).fetchall()
    if len(matches) != 1:
        raise SystemExit(f"symbol must resolve uniquely; found {len(matches)} matches for {value!r}")
    return matches[0]


def query_command(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    if args.command == "status":
        emit(status_data(conn, Path(args.root)))
    elif args.command == "search":
        emit(find_symbols(conn, args.query, args.limit))
    elif args.command == "symbol":
        emit(symbol_dict(require_symbol(conn, args.symbol)))
    elif args.command == "callers":
        symbol = require_symbol(conn, args.symbol)
        rows = conn.execute(
            """SELECT calls.path,calls.line,calls.column_no,calls.callee_name,
                      symbols.id AS caller_id,symbols.qualified_name AS caller
               FROM calls LEFT JOIN symbols ON symbols.id=calls.caller_symbol_id
               WHERE calls.resolved_symbol_id=? OR calls.callee_name=?
                  OR calls.callee_name LIKE ? ORDER BY calls.path,calls.line""",
            (symbol["id"], symbol["name"], f"%.{symbol['name']}"),
        ).fetchall()
        emit({"symbol": symbol_dict(symbol), "callers": [dict(row) for row in rows]})
    elif args.command == "calls":
        symbol = require_symbol(conn, args.symbol)
        rows = conn.execute(
            """SELECT calls.*,symbols.qualified_name AS resolved_name,symbols.path AS resolved_path,
                      symbols.start_line AS resolved_line FROM calls
               LEFT JOIN symbols ON symbols.id=calls.resolved_symbol_id
               WHERE caller_symbol_id=? ORDER BY line,column_no""",
            (symbol["id"],),
        ).fetchall()
        emit({"symbol": symbol_dict(symbol), "calls": [dict(row) for row in rows]})
    elif args.command == "deps":
        relative = Path(args.path).as_posix().removeprefix("./")
        imports = conn.execute("SELECT * FROM imports WHERE path=? ORDER BY line", (relative,)).fetchall()
        imported_by = conn.execute(
            "SELECT * FROM imports WHERE resolved_path=? ORDER BY path,line", (relative,)
        ).fetchall()
        emit({"path": relative, "imports": [dict(row) for row in imports], "imported_by": [dict(row) for row in imported_by]})
    elif args.command == "impact":
        symbol = require_symbol(conn, args.symbol)
        callers = conn.execute(
            """SELECT DISTINCT calls.path,calls.line,symbols.qualified_name AS caller
               FROM calls LEFT JOIN symbols ON symbols.id=calls.caller_symbol_id
               WHERE calls.resolved_symbol_id=? OR calls.callee_name=? OR calls.callee_name LIKE ?""",
            (symbol["id"], symbol["name"], f"%.{symbol['name']}"),
        ).fetchall()
        imported_by = conn.execute(
            "SELECT DISTINCT path FROM imports WHERE resolved_path=?", (symbol["path"],)
        ).fetchall()
        files = sorted({symbol["path"], *(row["path"] for row in callers), *(row["path"] for row in imported_by)})
        emit({"symbol": symbol_dict(symbol), "impacted_files": files, "callers": [dict(row) for row in callers]})


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", default=str(project_root()), help="repository root")
    sub = result.add_subparsers(dest="command", required=True)
    refresh_parser = sub.add_parser("refresh", help="incrementally refresh the index")
    refresh_parser.add_argument("--full", action="store_true", help="reparse every source file")
    refresh_parser.add_argument("--verbose", action="store_true", help="print each indexed path")
    search = sub.add_parser("search", help="search symbol names")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    for name in ("symbol", "callers", "calls", "impact"):
        command = sub.add_parser(name)
        command.add_argument("symbol", help="stable symbol id or unique name")
    deps = sub.add_parser("deps", help="show imports and reverse imports for a file")
    deps.add_argument("path", help="repo-relative source path")
    sub.add_parser("status", help="show index statistics")
    return result


def main() -> None:
    args = parser().parse_args()
    root = Path(args.root).resolve()
    if args.command == "refresh":
        refresh(root, args.full, args.verbose)
        return
    target = db_path(root)
    if not target.exists():
        raise SystemExit(f"index not found at {target}; run refresh first")
    conn = connect(root)
    query_command(conn, args)


if __name__ == "__main__":
    main()
