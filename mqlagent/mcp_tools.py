"""Orchestrator-side MCP clients. Claude never connects to a server; this code does."""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager

import httpx
from mcp import ClientSession

try:  # mcp >= 1.2x
    from mcp.client.streamable_http import streamable_http_client
except ImportError:  # older 1.x
    streamable_http_client = None
from mcp.client.streamable_http import streamablehttp_client

# The only Server A tools the scoring model may see. get_lead / list_unconverted_leads are ingest-only.
MODEL_VISIBLE_READ_TOOLS = ("lookup_crm_account", "check_open_opportunities", "get_prior_lead_history", "enrich_firmographics")


def _parse(result) -> dict:
    if getattr(result, "structuredContent", None):
        sc = result.structuredContent
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    for block in result.content or []:
        if getattr(block, "type", None) == "text":
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return {"status": "error", "reason": "non_json_response", "data": {}}
    return {"status": "error", "reason": "empty_response", "data": {}}


class McpTools:
    """One open session to one MCP server, with call logging."""

    def __init__(self, session: ClientSession, server_label: str):
        self.session = session
        self.server = server_label
        self.log: list[dict] = []

    async def model_tool_defs(self) -> list[dict]:
        listed = await self.session.list_tools()
        return [
            {"name": t.name, "description": (t.description or "")[:1000], "input_schema": t.inputSchema}
            for t in listed.tools
            if t.name in MODEL_VISIBLE_READ_TOOLS
        ]

    async def call(self, name: str, args: dict, allowed: tuple[str, ...] | None = None) -> dict:
        t0 = time.perf_counter()
        if allowed is not None and name not in allowed:
            res = {"call_id": f"denied-{len(self.log)}", "status": "error", "reason": "tool_not_allowed", "data": {}}
        else:
            res = _parse(await self.session.call_tool(name, args))
            res.setdefault("call_id", f"{self.server.lower()}-nocallid-{len(self.log)}")
        rec = {
            "call_id": res["call_id"], "server": self.server, "tool": name, "args": args, "result": res,
            "latency_ms": int((time.perf_counter() - t0) * 1000),
        }
        self.log.append(rec)
        return res


@asynccontextmanager
async def open_server(url: str, label: str, token: str | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token else None
    if streamable_http_client is not None:
        async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(10, read=30)) as http:
            async with streamable_http_client(url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield McpTools(session, label)
    else:
        async with streamablehttp_client(url, headers=headers, timeout=10) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield McpTools(session, label)
