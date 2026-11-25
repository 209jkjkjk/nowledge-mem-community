"""Discover a Nowledge Mem endpoint on the configured server."""

from __future__ import annotations

import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


PROBE_TIMEOUT = 2.0
MAX_PROBE_BYTES = 65536


class EndpointError(ValueError):
    """The configured server could not be resolved safely."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _server_parts(value: str):
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise EndpointError("NMEM_API_URL is not a valid server URL.") from None
    if (
        not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or not re.fullmatch(r"[a-zA-Z0-9._:/~\[\]-]+", value)
    ):
        raise EndpointError("NMEM_API_URL must contain a server address and optional API path.")
    if url.scheme != "https" and not (
        url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise EndpointError("NMEM_API_URL must use HTTPS for a remote server.")
    return url, (url.scheme, url.hostname, port or (443 if url.scheme == "https" else 80))


def _candidate_endpoints(value: str) -> tuple[str, ...]:
    url, _ = _server_parts(value)
    path = url.path.rstrip("/")
    origin = f"{url.scheme}://{url.netloc}"
    if path.endswith("/mcp"):
        return (origin + url.path,)
    if path:
        return (origin + path + "/mcp/", origin + path + "/mcp")
    return tuple(origin + path for path in ("/remote-api/mcp/", "/mcp", "/mcp/", "/remote-api/mcp"))


def api_base_from_endpoint(endpoint: str) -> str:
    url, _ = _server_parts(endpoint)
    return f"{url.scheme}://{url.netloc}{url.path.rstrip('/')[:-len('/mcp')]}"


def _is_initialize_result(data: bytes) -> bool:
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeError):
        return False
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" or payload.get("id") != 1:
        return False
    result = payload.get("result")
    return isinstance(result, dict) and isinstance(result.get("protocolVersion"), str) and isinstance(result.get("serverInfo"), dict)


def _valid_mcp_response(response) -> bool:
    mime = response.headers.get_content_type()
    if mime == "application/json":
        return _is_initialize_result(response.read(MAX_PROBE_BYTES))
    if mime != "text/event-stream":
        return False
    remaining = MAX_PROBE_BYTES
    lines = []
    while remaining > 0 and (line := response.readline(remaining)):
        remaining -= len(line)
        if not line.strip():
            if lines and _is_initialize_result(b"\n".join(lines)):
                return True
            lines.clear()
        elif line.startswith(b"data:"):
            lines.append(line[5:].strip())
    return bool(lines) and _is_initialize_result(b"\n".join(lines))


def resolve_endpoint(value: str, key: str) -> str:
    candidates = _candidate_endpoints(value)
    if len(candidates) == 1:
        return candidates[0]
    _, origin = _server_parts(value)
    headers = {
        "APP": "Codex",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "nowledge-codex-discovery", "version": "1"},
        },
    }).encode("utf-8")
    opener = build_opener(_NoRedirect())
    auth_failed = False
    for candidate in candidates:
        endpoint = candidate
        try:
            try:
                response = opener.open(Request(endpoint, body, headers), timeout=PROBE_TIMEOUT)
            except HTTPError as error:
                if error.code not in {301, 302, 307, 308} or not error.headers.get("Location"):
                    raise
                with error:
                    endpoint = urljoin(endpoint, error.headers["Location"])
                if _server_parts(endpoint)[1] != origin or endpoint not in candidates:
                    continue
                response = opener.open(Request(endpoint, body, headers), timeout=PROBE_TIMEOUT)
            with response:
                if not _valid_mcp_response(response):
                    continue
                session = response.headers.get("Mcp-Session-Id")
        except HTTPError as error:
            with error:
                auth_failed |= error.code in {401, 403}
            continue
        except (ValueError, URLError, OSError):
            continue
        if session:
            try:
                with opener.open(Request(
                    endpoint, headers={**headers, "Mcp-Session-Id": session}, method="DELETE",
                ), timeout=PROBE_TIMEOUT):
                    pass
            except (URLError, OSError):
                pass
        return endpoint
    if auth_failed:
        raise EndpointError("Nowledge Mem rejected MCP authentication. Check NMEM_API_KEY.")
    raise EndpointError("No Nowledge Mem MCP endpoint responded. Check NMEM_API_URL or set a full MCP URL.")


def matches_server(value: str, endpoint: str) -> bool:
    return endpoint in _candidate_endpoints(value)
