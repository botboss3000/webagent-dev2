# Code Index — UI and application source map

Use this ability for source-backed investigation of WebAgent's current UI and application code. It maps maintainable source beneath `ui/` and `app/`; it does not map session data or runtime-generated browser execution.

## Freshness contract

The index is a derived snapshot, so its exact file and line totals are intentionally not recorded here. Run `code_index_coverage(action="report")` before relying on it; use the result only when it reports zero ledger gaps, zero stale digests, and no unindexed files. A full scan also removes records for files no longer in its approved source set.

## What is indexed

Every physical line in each approved source file has one ledger record, including blank and comment lines. The approved source set is every non-generated `.js`, `.mjs`, `.cjs`, `.css`, `.html`, `.json`, and `.py` file below `ui/` or `app/`.

- JavaScript functions, classes, methods, and callable variable declarations have Tree-sitter AST spans, parent-symbol context, and line ownership.
- Python functions and classes have Python-AST spans, parent-symbol context, and line ownership.
- CSS, HTML, and JSON have complete line records classified in their native source category. HTML asset links are indexed.
- Static edges include JavaScript imports, DOM selectors, `data-*` attributes, event names, chat/API endpoint strings, and HTML `src`/`href` assets.
- Each file stores a SHA-256 source digest, scan timestamp, line count, parser status, and parser notes.

The index therefore supports questions such as: which function owns an application or UI line; which source files bind an event, selector, attribute, endpoint, import, or asset; and which static references surround a changed implementation.

## Source of truth and freshness

The SQLite database at `data/db/code_index.db` is derived data, never the source of truth. Before a diagnosis or edit:

1. Run `code_index_coverage(action="report")`. Use indexed results only when the relevant files have zero gaps and are not stale.
2. Use `code_index_lookup` for an exact file, line, symbol, or edge. Use `code_index_trace` for an exact symbol, selector, event, endpoint, data attribute, import, or asset.
3. After changing UI source, run `code_index_scan(action="scan_file", file_path="...")` for the changed file, or `scan` for a broader refresh. Recheck stale state afterward.

`code_index_progress` records scan batches only. A completed batch is not proof that a file remains current; the digest check is.

## Interpretation limits

Static references are navigation evidence, not a runtime call graph. Dynamic imports, computed selectors, string-built endpoints, framework rendering, CSS cascade/layout, browser state, and server-provided payloads may affect behavior without a static edge. Use the index to focus source review, then use an appropriate browser journey or API check to prove live behavior.

If Tree-sitter cannot parse a JavaScript file or is unavailable, Code Index falls back to a conservative scanner and marks it `parser_uncertain`; it must not be treated as an exact AST function map. At the current baseline, all indexed UI files have parser status `ok`.

## Exclusions

Generated/minified files, vendored code, backup/original copies, bytecode, documentation, data, binary media, images, archives, and fonts below `ui/` are intentionally excluded from source coverage. They are not silently counted as runnable UI code.
