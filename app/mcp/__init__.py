"""MCP bridge — exposes WebAgent's native tool registry to external CLI engines
(Claude Code, Codex, etc.) through the Model Context Protocol over stdio.

One single JSON-RPC server process per CLI invocation; tools are resolved fresh
from the app's own loader on every run so ability/tool/permission changes take
effect immediately with no restart and no manifest to update.
"""
