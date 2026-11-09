#!/usr/bin/env python3
"""OpenAPI spec tests: completeness, validity, serving, MCP single source.

The completeness test is the important one: it extracts every /api/v2 route
from server.py's real dispatch code and fails if the committed spec is missing
one (or documents a route that no longer exists).

Run with: python3 tests/test_openapi.py
"""

import json
import sys
import threading
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import api_spec  # noqa: E402
import mcp_tools  # noqa: E402


def _load_extractor():
    import importlib.util
    path = ROOT / "scripts" / "extract_api_routes.py"
    spec = importlib.util.spec_from_file_location("extract_api_routes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


extract_routes = _load_extractor().extract_routes

DOC = api_spec.build_openapi()


class TestSpecShape(unittest.TestCase):
    def test_openapi_3_1(self):
        self.assertEqual(DOC["openapi"], "3.1.0")
        self.assertIn("info", DOC)
        self.assertIn("title", DOC["info"])
        self.assertIn("version", DOC["info"])

    def test_security_schemes(self):
        schemes = DOC["components"]["securitySchemes"]
        self.assertIn("bearerAuth", schemes)
        self.assertEqual(schemes["bearerAuth"]["scheme"], "bearer")
        self.assertIn("sessionCookie", schemes)

    def test_every_operation_is_documented(self):
        for path, methods in DOC["paths"].items():
            self.assertTrue(path.startswith("/api/v2/"), path)
            for method, op in methods.items():
                with self.subTest(path=path, method=method):
                    self.assertIn(method, {"get", "post", "put", "patch", "delete"})
                    self.assertTrue(op.get("summary"), "missing summary")
                    self.assertTrue(op.get("operationId"), "missing operationId")
                    self.assertTrue(op.get("tags"), "missing tags")
                    self.assertTrue(op.get("security"), "missing security")
                    self.assertIn("responses", op)
                    self.assertIn("401", op["responses"])

    def test_operation_ids_are_unique(self):
        ids = [op["operationId"] for methods in DOC["paths"].values() for op in methods.values()]
        self.assertEqual(len(ids), len(set(ids)), "duplicate operationId")

    def test_path_parameters_are_declared(self):
        for path, methods in DOC["paths"].items():
            expected = {p.strip("{}") for p in path.split("/") if p.startswith("{")}
            for op in methods.values():
                declared = {p["name"] for p in op.get("parameters", []) if p.get("in") == "path"}
                self.assertEqual(expected, declared, f"{path}: path params not declared")


class TestCompleteness(unittest.TestCase):
    """THE guard: a new route must be added to the spec (or this fails)."""

    def test_no_route_is_missing_from_the_spec(self):
        extracted = {(r["method"], r["path"]) for r in extract_routes()
                     if r["source"] != "startswith"}
        documented = {(m.upper(), p) for p, methods in DOC["paths"].items() for m in methods}
        missing = sorted(extracted - documented)
        self.assertEqual(missing, [], "routes in server.py but not in the spec")
        extra = sorted(documented - extracted)
        self.assertEqual(extra, [], "routes in the spec but not in server.py")

    def test_route_count_is_reported(self):
        ops = sum(len(m) for m in DOC["paths"].values())
        self.assertGreater(ops, 250, f"expected a full API surface, got {ops}")


class TestEnrichedEndpoints(unittest.TestCase):
    def _op(self, method, path):
        return DOC["paths"][path][method.lower()]

    def test_projects_list_has_pagination_and_search(self):
        op = self._op("GET", "/api/v2/projects")
        names = {p["name"] for p in op["parameters"]}
        self.assertTrue({"search", "count", "startIndex"} <= names)
        self.assertEqual(op["x-pagination"]["style"], "limit-offset")
        self.assertEqual(op["responses"]["200"]["content"]["application/json"]["schema"]["type"], "array")

    def test_add_note_body(self):
        op = self._op("POST", "/api/v2/projects/{id}/history")
        schema = op["requestBody"]["content"]["application/json"]["schema"]
        self.assertIn("content", schema["properties"])
        self.assertIn("notifyUserList", schema["properties"])
        self.assertIn("content", schema["required"])

    def test_link_email_body(self):
        op = self._op("POST", "/api/v2/mail/messages/{id}/link")
        schema = op["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(schema["required"], ["opportunityId"])

    def test_draft_body(self):
        op = self._op("POST", "/api/v2/mail/drafts")
        schema = op["requestBody"]["content"]["application/json"]["schema"]
        self.assertTrue({"to", "subject", "body"} <= set(schema["required"]))

    def test_send_endpoint_is_documented_but_not_exposed_to_mcp(self):
        # The REST API does have /mail/send (the dashboard uses it); the MCP
        # surface must not. tests/test_mcp_server.py pins the MCP side.
        self.assertIn("/api/v2/mail/send", DOC["paths"])
        for tool, (method, path) in api_spec.MCP_TOOL_OPERATIONS.items():
            self.assertNotIn("/mail/send", path, tool)

    def test_error_envelope_schema(self):
        err = DOC["components"]["schemas"]["Error"]
        self.assertEqual(err["required"], ["error"])


class TestMcpSingleSource(unittest.TestCase):
    def test_every_tool_has_an_operation(self):
        self.assertEqual(set(mcp_tools.TOOL_NAMES), set(api_spec.MCP_TOOL_OPERATIONS))

    def test_every_mapped_operation_exists_in_the_spec(self):
        for tool, (method, path) in api_spec.MCP_TOOL_OPERATIONS.items():
            if tool == "get_carrier_estimate_lines":
                # documented as unavailable (route removed in 2.4.2)
                self.assertNotIn(path, DOC["paths"])
                continue
            with self.subTest(tool=tool):
                self.assertIn(path, DOC["paths"])
                self.assertIn(method.lower(), DOC["paths"][path])

    def test_tool_descriptions_come_from_the_spec(self):
        for tool in mcp_tools.TOOLS:
            if tool.name == "get_carrier_estimate_lines":
                continue
            method, path = api_spec.MCP_TOOL_OPERATIONS[tool.name]
            summary = api_spec.operation_summary(method, path)
            with self.subTest(tool=tool.name):
                self.assertTrue(summary)
                self.assertIn(summary.lower(), tool.description.lower())

    def test_no_tool_maps_to_a_send_endpoint(self):
        for tool, (method, path) in api_spec.MCP_TOOL_OPERATIONS.items():
            self.assertNotIn("/mail/send", path, tool)


class TestServed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import db
            import auth
            import api_tokens
            import server
        except Exception as exc:
            raise unittest.SkipTest(f"no database: {exc}")
        if db.query_one("SELECT 1 AS ok FROM information_schema.tables WHERE table_name='api_tokens'") is None:
            raise unittest.SkipTest("CRM schema not present")
        from http.server import ThreadingHTTPServer
        cls.suffix = uuid.uuid4().hex[:8]
        cls.user = db.query_one(
            """INSERT INTO users (email, display_name, password_hash, password_salt,
                                  is_admin, is_active, role)
               VALUES (%s, 'OpenAPI Test', 'x', 'x', FALSE, TRUE, 'member') RETURNING id""",
            (f"openapi-test-{cls.suffix}@example.invalid",),
        )
        cls.token = api_tokens.create_api_token(f"openapi-test-{cls.suffix}", cls.user["id"], "read")["token"]
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.KanbanHandler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.httpd.shutdown()
            cls.httpd.server_close()
        except Exception:
            pass
        try:
            import db
            db.execute("DELETE FROM api_tokens WHERE name = %s", (f"openapi-test-{cls.suffix}",))
            db.execute("DELETE FROM users WHERE id = %s", (cls.user["id"],))
        except Exception:
            pass

    def test_openapi_json_requires_auth(self):
        import httpx
        r = httpx.get(f"http://127.0.0.1:{self.port}/api/v2/openapi.json", timeout=10)
        self.assertEqual(r.status_code, 401)

    def test_openapi_json_is_served(self):
        import httpx
        r = httpx.get(f"http://127.0.0.1:{self.port}/api/v2/openapi.json",
                      headers={"Authorization": f"Bearer {self.token}"}, timeout=15)
        self.assertEqual(r.status_code, 200, r.text)
        doc = r.json()
        self.assertEqual(doc["openapi"], "3.1.0")
        self.assertGreater(len(doc["paths"]), 200)
        self.assertIn("/api/v2/projects", doc["paths"])


class TestDocs(unittest.TestCase):
    def test_api_md_covers_the_required_workflows(self):
        text = (ROOT / "docs" / "API.md").read_text(encoding="utf-8")
        for needle in ("Authorization: Bearer", "/api/v2/projects",
                       "/api/v2/projects/{id}/history", "/api/v2/mail/messages/{id}/link",
                       "/api/v2/tasks", "/api/v2/mail/drafts", "openapi.json",
                       "x-pagination"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
