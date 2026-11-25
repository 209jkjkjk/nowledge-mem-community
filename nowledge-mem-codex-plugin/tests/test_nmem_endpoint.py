import json
import sys
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
from nmem_endpoint import EndpointError, api_base_from_endpoint, resolve_endpoint


@contextmanager
def mcp_server(responses):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, payload["method"], self.headers.get("Authorization")))
            status, headers, body = responses.get(self.path, (404, {}, b""))
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", requests
        finally:
            server.shutdown()
            worker.join()


INITIALIZED = json.dumps({
    "jsonrpc": "2.0", "id": 1,
    "result": {"serverInfo": {"name": "nowledge-mem"}, "protocolVersion": "2025-03-26"},
}).encode("utf-8")


class EndpointTests(unittest.TestCase):
    def test_discovers_reverse_proxy_sse_and_preserves_api_prefix(self):
        response = (200, {"Content-Type": "text/event-stream"}, b"event: message\ndata: " + INITIALIZED + b"\n\n")
        with mcp_server({"/remote-api/mcp/": response}) as (origin, seen):
            endpoint = resolve_endpoint(origin, "test-key")
            self.assertEqual(endpoint, origin + "/remote-api/mcp/")
            self.assertEqual(api_base_from_endpoint(endpoint), origin + "/remote-api")
            self.assertEqual(seen, [("/remote-api/mcp/", "initialize", "Bearer test-key")])

    def test_falls_back_to_direct_mcp_after_html(self):
        with mcp_server({
            "/remote-api/mcp/": (200, {"Content-Type": "text/html"}, b"<html>"),
            "/mcp": (200, {"Content-Type": "application/json"}, INITIALIZED),
        }) as (origin, seen):
            endpoint = resolve_endpoint(origin, "test-key")
            self.assertEqual(endpoint, origin + "/mcp")
            self.assertEqual(api_base_from_endpoint(endpoint), origin)
            self.assertEqual([request[0] for request in seen], ["/remote-api/mcp/", "/mcp"])

    def test_explicit_endpoint_skips_discovery(self):
        with patch("nmem_endpoint.build_opener") as opener:
            self.assertEqual(
                resolve_endpoint("https://mem.example.com/custom/mcp/", "test-key"),
                "https://mem.example.com/custom/mcp/",
            )
            opener.assert_not_called()

    def test_ignores_redirect_to_another_origin_and_reports_auth(self):
        with mcp_server({}) as (other, unexpected):
            with mcp_server({
                "/remote-api/mcp/": (307, {"Location": other + "/mcp"}, b""),
                "/mcp": (401, {}, b""),
            }) as (origin, _):
                with self.assertRaisesRegex(EndpointError, "Check NMEM_API_KEY"):
                    resolve_endpoint(origin, "test-key")
                self.assertFalse(unexpected)
