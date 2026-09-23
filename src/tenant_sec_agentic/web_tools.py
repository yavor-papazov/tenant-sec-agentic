"""ADK FunctionTools: web_search, web_fetch, list_provider_services."""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlparse

import httpx
import trafilatura
from google.adk.tools import FunctionTool
from google.adk.tools.tool_context import ToolContext
from tenant_sec_agentic.usage import BudgetExceeded, consume_tool_call

logger = logging.getLogger(__name__)

STATE_DEEP_SEARCH_COUNT = "_deep_research_search_count"
STATE_DEEP_FETCH_COUNT = "_deep_research_fetch_count"
STATE_DOC_SEARCH_COUNT = "_doc_fetch_search_count"
STATE_DOC_FETCH_COUNT = "_doc_fetch_fetch_count"
STATE_FETCHED_DOCUMENTS = "_fetched_documents"


def _tavily_search(query: str, api_key: str) -> list[dict[str, str]]:
    with httpx.Client(timeout=30.0) as client:
        r = client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "search_depth": "basic",
                "max_results": 8,
            },
        )
        r.raise_for_status()
        data = r.json()
    results = []
    for item in data.get("results") or []:
        results.append(
            {
                "url": str(item.get("url", "")),
                "title": str(item.get("title", "")),
                "snippet": str(item.get("content", item.get("snippet", ""))),
            }
        )
    return results


def web_search(query: str, tool_context: ToolContext) -> dict[str, Any]:
    """Search the web for documentation. Returns top results as {url, title, snippet}."""
    mode = tool_context.state.get("pipeline_mode", "doc_fetch")
    if mode == "doc_fetch":
        n = int(tool_context.state.get(STATE_DOC_SEARCH_COUNT, 0))
        lim = int(tool_context.state.get("doc_fetch_max_search", 2))
        if n >= lim:
            return {
                "error": (
                    "Per-control search budget exhausted. Return the final "
                    "structured response now; do not call another tool."
                ),
                "results": [],
            }
        tool_context.state[STATE_DOC_SEARCH_COUNT] = n + 1
    elif mode == "deep_research":
        n = int(tool_context.state.get(STATE_DEEP_SEARCH_COUNT, 0))
        lim = int(
            tool_context.state.get("deep_research_max_search", 10)
        )
        if n >= lim:
            return {"error": "Budget exhausted", "results": []}
        tool_context.state[STATE_DEEP_SEARCH_COUNT] = n + 1
    try:
        consume_tool_call(
            tool_context.state,
            kind="search",
            role=mode,
            control_id=str(tool_context.state.get("current_control_id", "")),
        )
    except BudgetExceeded as exc:
        return {"error": str(exc), "results": []}

    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        logger.warning("TAVILY_API_KEY not set; web_search returns no results.")
        return {"results": [], "error": "TAVILY_API_KEY not configured"}

    try:
        results = _tavily_search(query, api_key)
    except Exception as e:
        logger.exception("web_search failed: %s", e)
        return {"results": [], "error": str(e)}
    return {"results": results}


def web_fetch(
    url: str,
    tool_context: ToolContext,
    max_chars: int = 15000,
) -> dict[str, Any]:
    """Fetch URL and extract readable text (trafilatura)."""
    mode = tool_context.state.get("pipeline_mode", "doc_fetch")
    if mode == "doc_fetch":
        n = int(tool_context.state.get(STATE_DOC_FETCH_COUNT, 0))
        lim = int(tool_context.state.get("doc_fetch_max_fetch", 3))
        if n >= lim:
            return {
                "url": url,
                "content": "",
                "status": "error",
                "error_detail": (
                    "Per-control fetch budget exhausted. Return the final "
                    "structured response now; do not call another tool."
                ),
            }
        tool_context.state[STATE_DOC_FETCH_COUNT] = n + 1
    elif mode == "deep_research":
        n = int(tool_context.state.get(STATE_DEEP_FETCH_COUNT, 0))
        lim = int(tool_context.state.get("deep_research_max_fetch", 5))
        if n >= lim:
            return {
                "url": url,
                "content": "",
                "status": "error",
                "error_detail": "Budget exhausted",
            }
        tool_context.state[STATE_DEEP_FETCH_COUNT] = n + 1
    try:
        consume_tool_call(
            tool_context.state,
            kind="fetch",
            role=mode,
            control_id=str(tool_context.state.get("current_control_id", "")),
        )
    except BudgetExceeded as exc:
        return {
            "url": url,
            "content": "",
            "status": "error",
            "error_detail": str(exc),
        }

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return {
            "url": url,
            "content": "",
            "status": "error",
            "error_detail": "Not a text page",
        }

    headers = {
        "User-Agent": "tenant-sec-agentic/0.1 (+https://github.com/ypapazov/tenant-sec)"
    }
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            r = client.get(url, headers=headers)
    except httpx.TimeoutException:
        return {
            "url": url,
            "content": "",
            "status": "error",
            "error_detail": "Timeout",
        }
    except Exception as e:
        return {
            "url": url,
            "content": "",
            "status": "error",
            "error_detail": str(e),
        }

    if r.status_code == 404:
        return {
            "url": url,
            "content": "",
            "status": "error",
            "error_detail": "Page not found",
        }
    if r.status_code == 403:
        return {
            "url": url,
            "content": "",
            "status": "error",
            "error_detail": "Access denied",
        }
    if r.status_code >= 400:
        return {
            "url": url,
            "content": "",
            "status": "error",
            "error_detail": f"HTTP {r.status_code}",
        }

    ctype = (r.headers.get("content-type") or "").lower()
    if "text/html" not in ctype and "text/plain" not in ctype and ctype:
        if "application/json" in ctype or "application/" in ctype:
            return {
                "url": url,
                "content": "",
                "status": "error",
                "error_detail": "Not a text page",
            }

    try:
        text = trafilatura.extract(
            r.text,
            url=url,
            include_comments=False,
            include_tables=True,
        )
    except Exception:
        text = None
    if not text:
        text = r.text[:max_chars] if r.text else ""

    text = (text or "")[:max_chars]
    result = {
        "url": url,
        "content": text,
        "status": "ok",
        "error_detail": None,
    }
    fetched = dict(tool_context.state.get(STATE_FETCHED_DOCUMENTS) or {})
    fetched[url] = result
    tool_context.state[STATE_FETCHED_DOCUMENTS] = fetched
    return result


def list_provider_services(tool_context: ToolContext) -> dict[str, Any]:
    """Return services_in_scope from session state."""
    cfg = tool_context.state.get("assessment_config")
    if not cfg:
        return {"services": []}
    services = (cfg.get("provider") or {}).get("services_in_scope") or []
    return {"services": services}


def doc_fetch_tools() -> list[FunctionTool]:
    return [
        FunctionTool(web_search),
        FunctionTool(web_fetch),
        FunctionTool(list_provider_services),
    ]
