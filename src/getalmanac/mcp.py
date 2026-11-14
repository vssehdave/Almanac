"""`almanac mcp`: the memory as tools, over stdio (Model Context Protocol, JSON-RPC 2.0).

Claude Code starts this next to a session. It only reads the local SQLite file.
"""
import json
import os
import sys
import time

from . import __version__, config
from .memory import Memory
from .store import Store

TOOLS = [
    {"name": "memory_search",
     "description": "Search earlier conversations stored by Almanac on this machine. Returns matching exchanges "
                    "(what was asked and how it ended) with an id for memory_read. Searches the current project "
                    "unless `project` names another one, or is \"all\".",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string", "description": "Words to look for"},
         "project": {"type": "string", "description": "A project name or key, or \"all\". Default: this folder's project"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 20}}}},
    {"name": "memory_read",
     "description": "Read one stored item in full by id: an exchange (the question, the final answer, tools and "
                    "files), or a topic with the exchanges under it.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {
         "id": {"type": "integer"},
         "full": {"type": "boolean", "description": "Also return the exchange's transcript: every reply, tool call and result that was kept"}}}},
    {"name": "memory_projects",
     "description": "List the projects Almanac has memory for, and each one's most recent conversations.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _day(ts) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def render_hits(hits) -> str:
    if not hits:
        return "Nothing stored matches."
    return "\n".join(f"[{h['id']}] {_day(h['ts'])} {h['title']}\n    {h['snippet']}" for h in hits)


def render_node(memory, node) -> str:
    if node is None:
        return "No stored item has that id."
    head = f"[{node['id']}] {node['kind']} from \"{node['ctitle']}\" ({_day(node['ts'])}, project {memory.projects.name(node['project'])})"
    if node["kind"] == "topic":
        kids = "\n".join(f"  [{c['id']}] {c['title']}" for c in node["children"])
        return f"{head}\n{node['text']}\n\nExchanges under this topic:\n{kids}"
    return f"{head}\n\n{node['text']}" + (f"\n\nPart of topic [{node['parent']}]." if node.get("parent") else "")


def render_projects(memory) -> str:
    lines = []
    for row in memory.projects.listing():
        convos = memory.recent(project=row["key"], limit=3)
        if not convos:
            continue
        lines.append(f"{row['name']}  ({row['key']})")
        lines += [f"    {_day(c['updated'])} {c['title']} ({c['turns']} exchange{'s' if c['turns'] != 1 else ''})" for c in convos]
    return "\n".join(lines) or "No conversations are stored yet."


def call(memory, name, args, cwd) -> str:
    if name == "memory_search":
        wanted = str(args.get("project") or "")
        if wanted.lower() == "all":
            project = None
        elif wanted:
            project = memory.projects.find(wanted)
            if not project:
                return f"No project is called {wanted!r}. memory_projects lists them."
        else:
            project = memory.projects.see(cwd, touch=False)
        return render_hits(memory.search(str(args.get("query") or ""), project=project,
                                         limit=max(1, min(20, int(args.get("limit") or 8)))))
    if name == "memory_read":
        node_id = int(args.get("id") or 0)
        text = render_node(memory, memory.read(node_id))
        if args.get("full") and memory.detail(node_id):
            text += "\n\nTranscript:\n" + memory.detail(node_id)
        return text
    if name == "memory_projects":
        return render_projects(memory)
    raise KeyError(name)


def handle(memory, message, cwd):
    """Answer one JSON-RPC message. Returns the reply, or None for a notification."""
    method, ident = message.get("method"), message.get("id")
    if ident is None:
        return None
    if method == "initialize":
        asked = (message.get("params") or {}).get("protocolVersion") or "2024-11-05"
        result = {"protocolVersion": asked, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "almanac", "version": __version__}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params") or {}
        try:
            text, failed = call(memory, params.get("name"), params.get("arguments") or {}, cwd), False
        except KeyError:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": f"unknown tool {params.get('name')}"}}
        except Exception as exc:  # a bad argument or a locked file: say so, keep serving
            text, failed = f"almanac memory error: {type(exc).__name__}: {exc}", True
        result = {"content": [{"type": "text", "text": text}], "isError": failed}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def serve(stdin=None, stdout=None) -> int:
    stdin, stdout = stdin or sys.stdin.buffer, stdout or sys.stdout.buffer
    memory = Memory(Store(config.home() / "almanac.db"))
    cwd = os.getcwd()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = handle(memory, message, cwd) if isinstance(message, dict) else None
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
            stdout.flush()
    return 0


def client_config() -> dict:
    """The `--mcp-config` entry that makes Claude Code start this server."""
    env = {"ALMANAC_HOME": str(config.home()), "PYTHONIOENCODING": "utf-8"}
    return {"mcpServers": {"almanac": {"command": sys.executable, "args": ["-m", "getalmanac", "mcp"], "env": env}}}
