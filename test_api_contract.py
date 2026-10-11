"""Tests for api_contract.cjs: the HTTP calls a client makes, checked against an OpenAPI/Swagger spec."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("api_contract.cjs")


def _find_typescript() -> Path:
    if os.environ.get("BUCKETS_TEST_TYPESCRIPT"):
        return Path(os.environ["BUCKETS_TEST_TYPESCRIPT"])
    if shutil.which("npm"):
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
        if root and (Path(root) / "typescript").exists():
            return Path(root) / "typescript"
    return Path.home() / "Coding/ME/castellan/node_modules/typescript"


TS_PKG = _find_typescript()
TSCONFIG = json.dumps({"compilerOptions": {"strict": True, "noEmit": True, "skipLibCheck": True, "lib": ["es2020"],
                                           "module": "esnext", "moduleResolution": "bundler"},
                       "include": ["src"]})

HTTP_TS = """\
export interface Client {
  get(url: string, params?: any, config?: any): Promise<any>
  post(url: string, body?: any, config?: any): Promise<any>
  put(url: string, body?: any, config?: any): Promise<any>
  delete(url: string, params?: any, config?: any): Promise<any>
}
export declare const api: Client
export declare function makeRequest(o: {
  method: string, uriTemplate: string, data?: any, uriParams?: { key: string, value: string }[]
}): Promise<any>
"""

URLS_TS = """\
export const GET_ORDER = "orders/:ORDER_ID"
export const LIST_ORDERS = "/orders"
export const SEARCH_ORDERS = "orders/search"
export const ITEMS = { list: "/orders/:id/items" } as const
export const NOT_THERE = "/legacy/things"
"""

CALLS_TS = """\
import { api, makeRequest } from "./http"
import { GET_ORDER, LIST_ORDERS, SEARCH_ORDERS, ITEMS, NOT_THERE } from "./urls"

declare const id: string
declare const flag: boolean
declare const someParam: string
declare const extra: Record<string, unknown>
declare const Dimensions: { get(name: string): { width: number } }

export function direct() {
  api.get("/orders", { status: "open", bogus: 1 })
  api.post("/orders", { customerId: 1, note: "n" })
  api.get(`/orders/${id}`)
  api.delete("/orders/5")
  api.get("/ghosts")
  api.put("/orders", {})
  api.get(SEARCH_ORDERS)
  api.get("/orders?status=open&page=2")
  api.get(ITEMS.list)
  api.get("/v1/customers")
}

export function wrapped() {
  makeRequest({ method: "get", uriTemplate: GET_ORDER, uriParams: [{ key: ":ORDER_ID", value: "1" }] })
  makeRequest({ method: "POST", uriTemplate: LIST_ORDERS, data: { ...extra, customerId: 1 } })
  makeRequest({ method: "post", uriTemplate: LIST_ORDERS, data: { customerId: 2, lines: [] } })
  makeRequest({ method: "get", uriTemplate: NOT_THERE })
}

export function conditional() {
  api.get(flag ? "/orders" : "/customers")
}

export function mutable(kind: string) {
  let uri = "/orders"
  if (kind) uri = uri + "/search"
  api.get(uri)
}

const itemUrl = (order: string) => `/orders/${order}/items`
export function builder() {
  api.get(itemUrl("7"))
}

export function unresolved() {
  api.get(someParam)
}

export function noise() {
  const m = new Map<string, string>()
  m.get("a/b")
  const apiCache = new Map<string, string>()
  apiCache.get("orders/1")
  const w = Dimensions.get("window")
  return w
}
"""

SPEC = {
    "openapi": "3.0.0",
    "servers": [{"url": "https://api.example.com/v1"}],
    "paths": {
        "/orders": {
            "get": {"parameters": [{"name": "status", "in": "query"}, {"name": "page", "in": "query"}]},
            "post": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/CreateOrder"}}}}},
        },
        "/orders/search": {"get": {"parameters": [{"name": "q", "in": "query", "required": True}]}},
        "/orders/{id}": {"get": {}, "delete": {}},
        "/orders/{id}/items": {"get": {}},
        "/customers": {"get": {}},
        "/customers/{id}": {"get": {}, "delete": {}},
    },
    "components": {"schemas": {
        "Base": {"type": "object", "properties": {"customerId": {"type": "integer"}}, "required": ["customerId"]},
        "CreateOrder": {"allOf": [
            {"$ref": "#/components/schemas/Base"},
            {"type": "object", "properties": {"lines": {"type": "array"}, "note": {"type": "string"}}, "required": ["lines"]},
        ]},
    }},
}


def make_repo(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="buckets-contract-"))
    for name, text in {"tsconfig.json": TSCONFIG, **files}.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    (root / "node_modules").mkdir(exist_ok=True)
    (root / "node_modules/typescript").symlink_to(TS_PKG)
    return root


def run_tool(repo: Path, spec: dict, **payload) -> dict:
    spec_path = repo / "spec.json"
    spec_path.write_text(json.dumps(spec))
    r = subprocess.run(["node", str(SCRIPT)], input=json.dumps({"repo": str(repo), "spec": str(spec_path), **payload}),
                       cwd=repo, capture_output=True, text=True, timeout=120)
    return json.loads(r.stdout)


@unittest.skipUnless(TS_PKG.exists() and shutil.which("node"), "needs node and a typescript package")
class ApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = make_repo({"src/http.ts": HTTP_TS, "src/urls.ts": URLS_TS, "src/calls.ts": CALLS_TS,
                              "src/calls.test.ts": 'import { api } from "./http"\napi.get("/only-in-tests")\n'})
        cls.out = run_tool(cls.repo, SPEC)
        assert cls.out["ok"], cls.out
        cls.calls = [c for c in cls.out["calls"] if c["file"] == "src/calls.ts"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.repo, ignore_errors=True)

    def find(self, raw=None, method=None, path=None):
        found = [c for c in self.calls if (raw is None or c["raw"] == raw) and (method is None or c["method"] == method)
                 and (path is None or c["path"] == path)]
        self.assertTrue(found, f"no call raw={raw} method={method} path={path} in {[(c['raw'], c['method'], c['path']) for c in self.calls]}")
        return found[0]

    def test_direct_call_with_unknown_query_key(self):
        c = self.find(raw='"/orders"', method="GET")
        self.assertEqual((c["status"], c["endpoint"], c["path"]), ("ok", "GET /orders", "orders"))
        self.assertEqual(c["keys"], ["status", "bogus"])
        self.assertEqual(c["unknown_keys"], ["bogus"])

    def test_body_keys_missing_required_across_allof_and_ref(self):
        c = self.find(raw='"/orders"', method="POST")
        self.assertEqual(c["status"], "ok")
        self.assertEqual(c["keys"], ["customerId", "note"])
        self.assertEqual(c["unknown_keys"], [])
        self.assertEqual(c["missing_required"], ["lines"])

    def test_template_url_with_an_id_matches_the_param_route(self):
        c = self.find(raw="`/orders/${id}`")
        self.assertEqual((c["path"], c["status"], c["endpoint"]), ("orders/{}", "ok", "GET /orders/{id}"))

    def test_delete_with_a_literal_id(self):
        c = self.find(raw='"/orders/5"', method="DELETE")
        self.assertEqual(c["endpoint"], "DELETE /orders/{id}")

    def test_stale_endpoint_has_none(self):
        c = self.find(raw='"/ghosts"')
        self.assertEqual((c["status"], c["endpoint"]), ("no-endpoint", None))

    def test_wrong_method(self):
        c = self.find(raw='"/orders"', method="PUT")
        self.assertEqual(c["status"], "method-mismatch")
        self.assertEqual(sorted(c["allowed"]), ["GET /orders", "POST /orders"])

    def test_literal_route_beats_the_param_route_and_required_query_is_checked(self):
        c = self.find(raw="SEARCH_ORDERS")
        self.assertEqual(c["endpoint"], "GET /orders/search")
        self.assertEqual(c["missing_required"], ["q"])

    def test_keys_in_the_url_count_as_query_keys(self):
        c = self.find(raw='"/orders?status=open&page=2"')
        self.assertEqual((c["status"], c["keys"], c["unknown_keys"]), ("ok", ["status", "page"], []))

    def test_constant_object_member_from_another_file(self):
        c = self.find(raw="ITEMS.list")
        self.assertEqual((c["path"], c["endpoint"]), ("orders/{}/items", "GET /orders/{id}/items"))

    def test_base_prefix_taken_from_the_spec_servers(self):
        c = self.find(raw='"/v1/customers"')
        self.assertEqual((c["status"], c["endpoint"]), ("ok", "GET /customers"))
        self.assertEqual(self.out["summary"]["base"], "v1")

    def test_object_literal_call_with_constant_and_uri_params(self):
        c = self.find(raw="GET_ORDER")
        self.assertEqual((c["method"], c["path"], c["status"], c["endpoint"]), ("GET", "orders/{}", "ok", "GET /orders/{id}"))

    def test_spread_body_has_unknown_keys_and_is_not_checked(self):
        c = self.find(raw="LIST_ORDERS", method="POST", path="orders")
        spread = [x for x in self.calls if x["raw"] == "LIST_ORDERS" and x["keys"] is None]
        self.assertEqual(len(spread), 1)
        self.assertEqual((spread[0]["status"], spread[0]["unknown_keys"], spread[0]["missing_required"]), ("ok", [], []))
        self.assertIsNotNone(c)

    def test_object_literal_call_with_a_constant_that_names_no_endpoint(self):
        c = self.find(raw="NOT_THERE")
        self.assertEqual((c["status"], c["path"]), ("no-endpoint", "legacy/things"))

    def test_conditional_url_gives_two_calls(self):
        two = [c for c in self.calls if c["raw"] == 'flag ? "/orders" : "/customers"']
        self.assertEqual(sorted(c["path"] for c in two), ["customers", "orders"])
        self.assertTrue(all(c["status"] == "ok" for c in two))

    def test_variable_reassigned_in_its_function(self):
        paths = sorted(c["path"] for c in self.calls if c["raw"] == "uri")
        self.assertEqual(paths, ["orders", "orders/search"])

    def test_small_url_builder_function_is_evaluated(self):
        c = self.find(raw='itemUrl("7")')
        self.assertEqual((c["path"], c["endpoint"]), ("orders/7/items", "GET /orders/{id}/items"))

    def test_unresolved_url_keeps_its_text(self):
        c = self.find(raw="someParam")
        self.assertEqual((c["status"], c["path"], c["endpoint"]), ("unresolved", None, None))

    def test_map_get_and_dimensions_get_are_not_calls(self):
        raws = {c["raw"] for c in self.calls}
        self.assertFalse({'"a/b"', '"window"', '"orders/1"'} & raws, raws)

    def test_test_files_are_skipped(self):
        self.assertFalse([c for c in self.out["calls"] if "test" in c["file"]])

    def test_unused_endpoints_and_summary(self):
        unused = set(self.out["unused_endpoints"])
        self.assertIn("DELETE /customers/{id}", unused)
        self.assertIn("GET /customers/{id}", unused)
        self.assertNotIn("GET /orders", unused)
        self.assertNotIn("GET /orders/search", unused)
        s = self.out["summary"]
        self.assertEqual(s["calls"], len(self.out["calls"]))
        self.assertEqual(s["unused_endpoints"], len(unused))
        self.assertEqual(s["no-endpoint"] + s["ok"] + s["method-mismatch"] + s["unresolved"] + s["external"], s["calls"])

    def test_include_limits_where_calls_are_looked_for(self):
        repo = make_repo({"src/http.ts": HTTP_TS, "src/a/one.ts": 'import { api } from "../http"\napi.get("/orders")\n',
                          "src/b/two.ts": 'import { api } from "../http"\napi.get("/customers")\n'})
        try:
            d = run_tool(repo, SPEC, include=["src/a/**"])
            self.assertEqual([c["file"] for c in d["calls"]], ["src/a/one.ts"])
            d = run_tool(repo, SPEC, include=["src/b"])
            self.assertEqual([c["file"] for c in d["calls"]], ["src/b/two.ts"])
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_explicit_base_and_custom_object_keys(self):
        repo = make_repo({"src/req.ts": "export declare function send(o: any): void\n",
                          "src/use.ts": 'import { send } from "./req"\nsend({ verb: "get", href: "/api/orders", payload: { status: "x", nope: 1 } })\n'})
        spec = {"openapi": "3.0.0", "paths": {"/orders": {"get": {"parameters": [{"name": "status", "in": "query"}]}}}}
        try:
            call = {"object_keys": {"method": ["verb"], "url": ["href"], "params": ["payload"]}}
            d = run_tool(repo, spec, call=call, base="/api")
            self.assertEqual(len(d["calls"]), 1, d)
            c = d["calls"][0]
            self.assertEqual((c["status"], c["unknown_keys"]), ("ok", ["nope"]))
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_swagger2_spec_with_body_ref_and_query(self):
        repo = make_repo({"src/http.ts": HTTP_TS, "src/use.ts": (
            'import { api } from "./http"\n'
            'api.post("/api/items", { title: "t", color: "red" })\n'
            'api.get("/api/items", { limit: 3, offset: 0 })\n'
            'api.get("/api/items/{id}")\n')})
        spec = {
            "swagger": "2.0", "basePath": "/api",
            "paths": {
                "/items": {
                    "post": {"parameters": [{"in": "body", "name": "body", "schema": {"$ref": "#/definitions/Item"}}]},
                    "get": {"parameters": [{"name": "limit", "in": "query", "type": "integer", "required": True}]},
                },
                "/items/{id}": {"parameters": [{"name": "id", "in": "path", "required": True, "type": "string"}], "get": {}},
            },
            "definitions": {"Item": {"type": "object", "required": ["title", "price"],
                                     "properties": {"title": {"type": "string"}, "price": {"type": "number"}}}},
        }
        try:
            d = run_tool(repo, spec)
            by = {(c["method"], c["raw"]): c for c in d["calls"]}
            post = by[("POST", '"/api/items"')]
            self.assertEqual((post["status"], post["unknown_keys"], post["missing_required"]), ("ok", ["color"], ["price"]))
            get = by[("GET", '"/api/items"')]
            self.assertEqual((get["unknown_keys"], get["missing_required"]), (["offset"], []))
            self.assertEqual(by[("GET", '"/api/items/{id}"')]["endpoint"], "GET /items/{id}")
            self.assertEqual(d["unused_endpoints"], [])
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_words_match_without_case_and_an_unresolved_part_may_be_a_word(self):
        repo = make_repo({"src/http.ts": HTTP_TS, "src/use.ts": (
            'import { api } from "./http"\n'
            'export const count = (kind: string) => api.get(`/orders/count/${kind}`)\n'
            'api.get("/lookups/calculationMethods")\n')})
        spec = {"openapi": "3.0.0", "paths": {"/orders/count/past": {"get": {}}, "/lookups/CalculationMethods": {"get": {}}}}
        try:
            d = run_tool(repo, spec)
            by = {c["raw"]: c for c in d["calls"]}
            word = by["`/orders/count/${kind}`"]
            self.assertEqual((word["status"], word["endpoint"], word.get("loose")), ("ok", "GET /orders/count/past", True))
            self.assertEqual(by['"/lookups/calculationMethods"']["status"], "ok")   # routes ignore case by default
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_a_bad_payload_is_an_error(self):
        r = subprocess.run(["node", str(SCRIPT)], input=json.dumps({"repo": str(self.repo), "spec": "/no/such.json"}),
                           capture_output=True, text=True)
        d = json.loads(r.stdout)
        self.assertFalse(d["ok"])
        self.assertIn("error", d)


if __name__ == "__main__":
    unittest.main()
