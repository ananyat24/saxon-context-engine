# Shared "connect to an MCP server as a client, call one tool with a
# single natural-language string, get text back" logic -- used by both
# app/retrieval/fabric_iq_ontology_retriever.py and work_iq_retriever.py.
# Factored out because both are the same shape (a bearer-token-authed
# streamable-HTTP MCP server exposing a tool that takes a query/question
# string), even though the two providers' exact tool names and parameter
# names differ.
#
# Deliberately introspects the tool's own input schema (list_tools()) at
# call time rather than hardcoding a parameter name like "query" --
# Microsoft's own docs for both Fabric IQ Ontology and Work IQ explicitly
# warn these are preview APIs whose "tool names and parameters" may change
# (see CLAUDE.md's v7 section). Picking the tool's first required
# string-typed input property is a defensible guess given a tool already
# known (by name) to take one natural-language question, not a blind one --
# and it keeps working across a parameter rename that would otherwise
# silently break a hardcoded key.
import logging
from typing import Optional

import httpx
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 15.0


async def query_mcp_tool(url: str, access_token: str, tool_name: str, query_text: str) -> Optional[str]:
    """Returns the tool's text result, or None on anything that means "this
    retriever found nothing usable" -- unreachable server, tool not found,
    no string parameter to put the query in, or the server reporting an
    error. Never raises: same "a live external dependency being
    unreachable degrades to found-nothing, not a failed query" principle
    app/retrieval/foundry_iq_retriever.py already follows."""
    try:
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {access_token}"}, timeout=_TIMEOUT_SECONDS
        ) as http_client:
            async with Client(streamable_http_client(url, http_client=http_client)) as client:
                tools = await client.list_tools()
                tool = next((t for t in tools.tools if t.name == tool_name), None)
                if tool is None:
                    logger.warning(f"MCP server at '{url}' has no tool named '{tool_name}'.")
                    return None

                param_name = _pick_string_param(tool.input_schema or {})
                if param_name is None:
                    logger.warning(f"MCP tool '{tool_name}' at '{url}' has no string parameter to query with.")
                    return None

                result = await client.call_tool(tool_name, {param_name: query_text})
                if result.is_error:
                    logger.warning(f"MCP tool '{tool_name}' at '{url}' returned an error result.")
                    return None
                texts = [c.text for c in result.content if getattr(c, "type", None) == "text" and c.text]
                return "\n".join(texts) if texts else None
    except Exception as e:
        logger.warning(f"MCP query to '{url}' ('{tool_name}') failed: {e}")
        return None


def _pick_string_param(input_schema: dict) -> Optional[str]:
    properties = input_schema.get("properties") or {}
    required = input_schema.get("required") or []
    for name in required:
        if properties.get(name, {}).get("type") == "string":
            return name
    for name, schema in properties.items():
        if schema.get("type") == "string":
            return name
    return None
