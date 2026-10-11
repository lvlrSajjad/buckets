"""Tests for nest_routes.cjs: a NestJS backend's controllers turned into an OpenAPI document api_contract.cjs can use."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_api_contract import HTTP_TS, make_repo as make_client, run_tool as check_client
from test_rename import TS_PKG

SCRIPT = Path(__file__).with_name("nest_routes.cjs")
TSCONFIG = json.dumps({"compilerOptions": {"strict": False, "noEmit": True, "skipLibCheck": True, "lib": ["es2020"],
                                           "module": "esnext", "moduleResolution": "bundler",
                                           "experimentalDecorators": True, "emitDecoratorMetadata": True},
                       "include": ["src"]})

# Just enough of the framework packages for the fixtures to type-check: nothing is installed.
COMMON_DTS = """\
type P = string | string[]
export declare function Controller(prefix?: P | { path?: P, version?: string | string[] | symbol }): ClassDecorator
export declare const Get: (path?: P) => MethodDecorator
export declare const Post: (path?: P) => MethodDecorator
export declare const Put: (path?: P) => MethodDecorator
export declare const Patch: (path?: P) => MethodDecorator
export declare const Delete: (path?: P) => MethodDecorator
export declare const Head: (path?: P) => MethodDecorator
export declare const Options: (path?: P) => MethodDecorator
export declare const All: (path?: P) => MethodDecorator
export declare const Sse: (path?: P) => MethodDecorator
export declare const Param: (name?: any, ...pipes: any[]) => ParameterDecorator
export declare const Query: (name?: any, ...pipes: any[]) => ParameterDecorator
export declare const Body: (name?: any, ...pipes: any[]) => ParameterDecorator
export declare const Headers: (name?: string) => ParameterDecorator
export declare const Req: () => ParameterDecorator
export declare const Request: () => ParameterDecorator
export declare const Res: () => ParameterDecorator
export declare const UploadedFile: () => ParameterDecorator
export declare const Version: (v: string | string[] | symbol) => MethodDecorator & ClassDecorator
export declare const VERSION_NEUTRAL: unique symbol
export declare enum VersioningType { URI, HEADER, MEDIA_TYPE, CUSTOM }
export declare enum RequestMethod { GET, POST, PUT, DELETE, PATCH, ALL, OPTIONS, HEAD }
export declare class ParseIntPipe {}
export declare class Module {}
"""
CORE_DTS = """\
export declare class NestFactory { static create(module: any): Promise<{
  setGlobalPrefix(prefix: string, options?: any): void
  enableVersioning(options: any): void
  listen(port: number): Promise<void>
}> }
export declare class RouterModule { static register(routes: any[]): any }
"""
SWAGGER_DTS = """\
type Ctor<T> = new (...args: any[]) => T
export declare function ApiProperty(o?: { required?: boolean }): PropertyDecorator
export declare function ApiPropertyOptional(o?: any): PropertyDecorator
export declare function PartialType<T>(c: Ctor<T>): Ctor<Partial<T>>
export declare function PickType<T, K extends keyof T>(c: Ctor<T>, keys: readonly K[]): Ctor<Pick<T, K>>
export declare function OmitType<T, K extends keyof T>(c: Ctor<T>, keys: readonly K[]): Ctor<Omit<T, K>>
export declare function IntersectionType<A, B>(a: Ctor<A>, b: Ctor<B>): Ctor<A & B>
"""
VALIDATOR_DTS = """\
export declare function IsOptional(): PropertyDecorator
export declare function IsString(): PropertyDecorator
export declare function IsNumber(): PropertyDecorator
export declare function ValidateIf(f: (o: any) => boolean): PropertyDecorator
"""


def _pkg(root: Path, name: str, dts: str):
    d = root / "node_modules" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.d.ts").write_text(dts)
    (d / "package.json").write_text(json.dumps({"name": name, "version": "1.0.0", "types": "index.d.ts"}))


def make_backend(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="buckets-nest-"))
    for name, text in {"tsconfig.json": TSCONFIG, **files}.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    (root / "node_modules").mkdir(exist_ok=True)
    (root / "node_modules/typescript").symlink_to(TS_PKG)
    _pkg(root, "@nestjs/common", COMMON_DTS)
    _pkg(root, "@nestjs/core", CORE_DTS)
    _pkg(root, "@nestjs/swagger", SWAGGER_DTS)
    _pkg(root, "class-validator", VALIDATOR_DTS)
    return root


def routes(repo: Path, **payload) -> dict:
    r = subprocess.run(["node", str(SCRIPT)], input=json.dumps({"repo": str(repo), **payload}),
                       cwd=repo, capture_output=True, text=True, timeout=120)
    return json.loads(r.stdout)


def controller(body: str, imports: str = "", head: str = "@Controller('items')") -> str:
    return ('import { Controller, Get, Post, Put, Patch, Delete, Head, Options, All, Sse, Param, Query, Body, Headers, Req, '
            'Res, Request, UploadedFile, Version, VERSION_NEUTRAL, RequestMethod, ParseIntPipe } from "@nestjs/common"\n'
            f"{imports}\n{head}\nexport class ItemsController {{\n{body}\n}}\n")


MAIN_TS = """\
import { NestFactory } from "@nestjs/core"
import { VersioningType, VERSION_NEUTRAL } from "@nestjs/common"
async function bootstrap() {
  const app = await NestFactory.create({})
  %s
  await app.listen(3000)
}
bootstrap()
"""


@unittest.skipUnless(TS_PKG.exists() and shutil.which("node"), "needs node and a typescript package")
class NestRoutesTests(unittest.TestCase):
    def setUp(self):
        self.dirs = []

    def tearDown(self):
        for d in self.dirs:
            shutil.rmtree(d, ignore_errors=True)

    def backend(self, files, **payload):
        repo = make_backend(files)
        self.dirs.append(repo)
        out = routes(repo, **payload)
        self.assertTrue(out["ok"], out)
        return out

    def ctrl(self, body, imports="", head="@Controller('items')", files=None, **payload):
        return self.backend({"src/items.controller.ts": controller(body, imports, head), **(files or {})}, **payload)

    def op(self, out, method, path):
        paths = out["spec"]["paths"]
        self.assertIn(path, paths, sorted(paths))
        self.assertIn(method, paths[path], list(paths[path]))
        return paths[path][method]

    def params(self, op, where):
        return {p["name"]: p for p in op["parameters"] if p["in"] == where}

    def schema(self, out, op):
        s = op["requestBody"]["content"]["application/json"]["schema"]
        if "$ref" in s:
            s = out["spec"]["components"]["schemas"][s["$ref"].rsplit("/", 1)[1]]
        return s

    # ---------- controllers and methods ----------

    def test_controller_forms(self):
        for head, expect in [("@Controller()", ["/"]), ("@Controller('a')", ["/a"]), ("@Controller('/a/')", ["/a"]),
                             ("@Controller(['a', 'b'])", ["/a", "/b"]), ("@Controller({ path: 'a', version: '1' })", ["/a"]),
                             ("@Controller({ path: ['a', 'b'] })", ["/a", "/b"]), ("@Controller({ version: '1' })", ["/"])]:
            out = self.ctrl("@Get()\nlist() {}", head=head)
            self.assertEqual(sorted(out["spec"]["paths"]), expect, head)
            self.assertEqual(out["summary"], {"controllers": 1, "routes": len(expect)})

    def test_method_decorators_and_path_forms(self):
        out = self.ctrl("""\
  @Get() a() {}
  @Get('one') b() {}
  @Post(['two', 'three']) c() {}
  @Put(':id') d() {}
  @Patch(':id/rename') e() {}
  @Delete('/gone/') f() {}
  @Head('h') g() {}
  @Options('o') h() {}
  @Sse('events') i() {}
  @Get(`tpl/${NAME}`) j() {}
  @Get(Paths.CONST) k() {}
  @Get(OBJ.nested.deep) l() {}
  @Get(LIST) m() {}
  @Get(':id(\\\\d+)') n() {}
  @Get('file.:ext') o() {}
""", imports='const NAME = "named"\nconst LIST = ["l1", "l2"] as const\n'
                              'const OBJ = { nested: { deep: "deep-path" } } as const\n'
                              'enum Paths { CONST = "from-enum" }\n')
        paths = out["spec"]["paths"]
        for p, m in [("/items", "get"), ("/items/one", "get"), ("/items/two", "post"), ("/items/three", "post"),
                     ("/items/{id}", "put"), ("/items/{id}/rename", "patch"), ("/items/gone", "delete"), ("/items/h", "head"),
                     ("/items/o", "options"), ("/items/events", "get"), ("/items/tpl/named", "get"),
                     ("/items/from-enum", "get"), ("/items/deep-path", "get"), ("/items/l1", "get"), ("/items/l2", "get"), ("/items/{id}", "get"),
                     ("/items/file.{ext}", "get")]:
            self.assertIn(m, paths.get(p, {}), (p, m, sorted(paths)))
        self.assertEqual(self.params(self.op(out, "put", "/items/{id}"), "path")["id"]["required"], True)

    def test_constants_from_other_files_and_objects(self):
        out = self.ctrl("""\
  @Get(ROUTES.list) a() {}
  @Get(ROUTES.detail) b() {}
  @Get(Segment.Search) c() {}
""", imports='import { ROUTES, Segment } from "./routes"\n',
                        files={"src/routes.ts": 'export const ROUTES = { list: "all", detail: ":id/detail" } as const\n'
                                                'export enum Segment { Search = "search" }\n'},
                        )
        self.assertEqual(sorted(out["spec"]["paths"]), ["/items/all", "/items/search", "/items/{id}/detail"])
        out = self.backend({"src/a.controller.ts": controller("@Get() x() {}", 'import { BASE } from "./base"\n', "@Controller(BASE)"),
                            "src/base.ts": 'export const BASE = "base" + "-path"\n'})
        self.assertEqual(list(out["spec"]["paths"]), ["/base-path"])

    def test_all_means_every_method(self):
        out = self.ctrl("@All('any') x() {}")
        self.assertEqual(sorted(out["spec"]["paths"]["/items/any"]), ["delete", "get", "head", "options", "patch", "post", "put"])
        self.assertEqual(out["summary"]["routes"], 7)

    def test_optional_path_parts(self):
        out = self.ctrl("@Get(':a{/:b}') x() {}\n@Get('opt/:c?') y() {}")
        self.assertEqual(sorted(out["spec"]["paths"]), ["/items/opt", "/items/opt/{c}", "/items/{a}", "/items/{a}/{b}"])

    def test_wildcards(self):
        out = self.ctrl("@Get('files/*') x() {}\n@Get('docs/*rest') y() {}")
        self.assertEqual(sorted(out["spec"]["paths"]), ["/items/docs/{rest}", "/items/files/{wildcard}"])

    def test_routes_of_a_base_class_come_with_a_controller(self):
        out = self.backend({
            "src/base.ts": 'import { Get, Delete } from "@nestjs/common"\n'
                           "export abstract class CrudBase {\n  @Get(':id') one() {}\n  @Get() list() {}\n  @Delete(':id') remove() {}\n}\n",
            "src/items.controller.ts": 'import { Controller, Get } from "@nestjs/common"\nimport { CrudBase } from "./base"\n'
                                       "@Controller('items')\nexport class ItemsController extends CrudBase {\n  @Get() list() {}\n  @Get('extra') extra() {}\n}\n"})
        paths = out["spec"]["paths"]
        self.assertEqual(sorted(paths), ["/items", "/items/extra", "/items/{id}"])
        self.assertEqual(sorted(paths["/items/{id}"]), ["delete", "get"])
        self.assertEqual(out["summary"]["controllers"], 1)   # the abstract base is not a controller

    def test_classes_without_controller_and_tests_are_skipped(self):
        body = "@Get() x() {}"
        out = self.backend({
            "src/items.controller.ts": controller(body),
            "src/plain.ts": 'import { Get } from "@nestjs/common"\nexport class Plain {\n  @Get("plain") x() {}\n}\n',
            "src/items.controller.spec.ts": controller(body, head="@Controller('from-spec')"),
            "src/items.test.ts": controller(body, head="@Controller('from-test')"),
            "src/__tests__/t.ts": controller(body, head="@Controller('from-tests-dir')"),
            "src/dist/x.ts": controller(body, head="@Controller('from-dist')"),
            "test/e2e.controller.ts": controller(body, head="@Controller('from-test-folder')"),
            "node_modules/pkg/x.ts": controller(body, head="@Controller('from-node-modules')"),
        })
        self.assertEqual(list(out["spec"]["paths"]), ["/items"])

    def test_include_limits_the_folders_and_the_default_is_src(self):
        files = {"src/a/a.controller.ts": controller("@Get() x() {}", head="@Controller('a')"),
                 "src/b/b.controller.ts": controller("@Get() x() {}", head="@Controller('b')"),
                 "tools/t.controller.ts": controller("@Get() x() {}", head="@Controller('tools')")}
        self.assertEqual(sorted(self.backend(files)["spec"]["paths"]), ["/a", "/b"])
        self.assertEqual(list(self.backend(files, include=["src/b"])["spec"]["paths"]), ["/b"])
        files = {"lib/a.controller.ts": controller("@Get() x() {}", head="@Controller('lib')")}
        self.assertEqual(list(self.backend(files)["spec"]["paths"]), ["/lib"])   # no src folder: the whole repo

    def test_out_writes_the_spec_and_a_bad_repo_is_an_error(self):
        repo = make_backend({"src/items.controller.ts": controller("@Get() x() {}")})
        self.dirs.append(repo)
        target = repo / "out" / "spec.json"
        target.parent.mkdir()
        out = routes(repo, out=str(target))
        self.assertEqual(json.loads(target.read_text()), out["spec"])
        self.assertEqual(out["spec"]["openapi"], "3.0.0")
        r = subprocess.run(["node", str(SCRIPT)], input=json.dumps({"repo": "/no/such/repo"}), capture_output=True, text=True)
        d = json.loads(r.stdout)
        self.assertFalse(d["ok"])
        self.assertIn("error", d)
        self.assertFalse(json.loads(subprocess.run(["node", str(SCRIPT)], input="{", capture_output=True, text=True).stdout)["ok"])

    # ---------- app settings ----------

    def test_global_prefix_is_added_and_named_in_servers(self):
        out = self.ctrl("@Get(':id') x() {}", files={"src/main.ts": MAIN_TS % "app.setGlobalPrefix('api')"})
        self.assertEqual(list(out["spec"]["paths"]), ["/api/items/{id}"])
        self.assertEqual(out["spec"]["servers"], [{"url": "/api"}])
        out = self.ctrl("@Get() x() {}", files={"src/main.ts": MAIN_TS % "app.setGlobalPrefix(PREFIX)\n"}, imports="")
        self.assertEqual(out["unresolved"][0]["why"], "the global prefix is not a constant")
        self.assertEqual(list(out["spec"]["paths"]), ["/items"])

    def test_global_prefix_exclude(self):
        out = self.backend({
            "src/items.controller.ts": controller("@Get() x() {}"),
            "src/health.controller.ts": controller("@Get() x() {}", head="@Controller('health')"),
            "src/main.ts": MAIN_TS % "app.setGlobalPrefix('api', { exclude: ['health'] })"})
        self.assertEqual(sorted(out["spec"]["paths"]), ["/api/items", "/health"])

    def test_uri_versioning_with_default_controller_and_method_versions(self):
        main = MAIN_TS % "app.enableVersioning({ type: VersioningType.URI, defaultVersion: '1' })"
        out = self.backend({
            "src/main.ts": main,
            "src/items.controller.ts": controller("""\
  @Get() plain() {}
  @Get('newer') @Version('2') newer() {}
  @Get('both') @Version(['1', '3']) both() {}
  @Get('neutral') @Version(VERSION_NEUTRAL) neutral() {}
""", head="@Controller('items')"),
            "src/orders.controller.ts": controller("@Get() x() {}\n@Get('legacy') @Version('1') y() {}",
                                                   head="@Controller({ path: 'orders', version: '5' })")})
        self.assertEqual(sorted(out["spec"]["paths"]), sorted([
            "/items/neutral", "/v1/items", "/v1/items/both", "/v2/items/newer", "/v3/items/both", "/v1/orders/legacy", "/v5/orders"]))

    def test_versioning_goes_after_the_global_prefix_and_takes_a_prefix_option(self):
        out = self.ctrl("@Get() x() {}", files={"src/main.ts": MAIN_TS % (
            "app.setGlobalPrefix('api')\n  app.enableVersioning({ type: VersioningType.URI, defaultVersion: ['1', '2'], prefix: 'ver' })")})
        self.assertEqual(sorted(out["spec"]["paths"]), ["/api/ver1/items", "/api/ver2/items"])
        out = self.ctrl("@Get() x() {}", files={"src/main.ts": MAIN_TS % (
            "app.enableVersioning({ type: VersioningType.URI, defaultVersion: '3', prefix: false })")})
        self.assertEqual(list(out["spec"]["paths"]), ["/3/items"])

    def test_versions_do_nothing_without_uri_versioning(self):
        head = "@Controller({ path: 'items', version: '2' })"
        self.assertEqual(list(self.ctrl("@Get() x() {}", head=head)["spec"]["paths"]), ["/items"])
        out = self.ctrl("@Get() x() {}", head=head, files={"src/main.ts": MAIN_TS % "app.enableVersioning({ type: VersioningType.HEADER, header: 'v' })"})
        self.assertEqual(list(out["spec"]["paths"]), ["/items"])

    def test_settings_outside_the_nest_factory_file_and_router_module_are_reported(self):
        out = self.ctrl("@Get() x() {}", files={
            "src/main.ts": MAIN_TS % "",
            "src/setup.ts": "export function setup(app: any) {\n  app.setGlobalPrefix('late')\n}\n",
            "src/app.module.ts": 'import { RouterModule } from "@nestjs/core"\n'
                                 "export const routing = RouterModule.register([{ path: 'admin', module: class {} }])\n"})
        self.assertEqual(list(out["spec"]["paths"]), ["/items"])    # neither is applied
        why = {(u["file"], u["why"].split(":")[0].split(" is ")[0]) for u in out["unresolved"]}
        self.assertIn(("src/setup.ts", "setGlobalPrefix"), why)
        self.assertIn(("src/app.module.ts", "RouterModule.register"), why)
        router = [u for u in out["unresolved"] if u["file"] == "src/app.module.ts"][0]
        self.assertEqual(router["line"], 2)
        self.assertIn("not applied", router["why"])

    def test_a_path_that_is_not_a_constant_is_reported_and_skipped(self):
        out = self.ctrl("@Get(fn()) x() {}\n@Get('ok') y() {}", imports="declare function fn(): string\n")
        self.assertEqual(list(out["spec"]["paths"]), ["/items/ok"])
        self.assertEqual(out["unresolved"], [{"file": "src/items.controller.ts", "line": 6, "why": "the route path is not a constant"}])

    # ---------- parameters ----------

    def test_path_params(self):
        out = self.ctrl("""\
  @Get(':id') a(@Param('id', ParseIntPipe) id: number) {}
  @Get('by/:slug/:other') b(@Param('other') other: string) {}
  @Get('named') c(@Param('ghost') g: string) {}
""")
        a = self.params(self.op(out, "get", "/items/{id}"), "path")
        self.assertEqual({k: v["required"] for k, v in a.items()}, {"id": True})
        b = self.params(self.op(out, "get", "/items/by/{slug}/{other}"), "path")
        self.assertEqual(sorted(b), ["other", "slug"])          # a route param with no decorator is still a path param
        self.assertTrue(all(v["required"] for v in b.values()))
        self.assertIn("ghost", self.params(self.op(out, "get", "/items/named"), "path"))

    def test_query_params_named_and_ignored_decorators(self):
        out = self.ctrl("""\
  @Get() a(@Query('status') s: string, @Query('page', ParseIntPipe) p: number, @Headers('x-key') k: string,
           @Res() res: any, @Param() all: any) {}
""")
        q = self.params(self.op(out, "get", "/items"), "query")
        self.assertEqual({k: v["required"] for k, v in q.items()}, {"status": False, "page": False})
        self.assertEqual(q["page"]["schema"], {"type": "number"})
        self.assertNotIn("x-key", [p["name"] for p in self.op(out, "get", "/items")["parameters"]])

    def test_query_dto_becomes_one_param_per_property(self):
        out = self.ctrl("@Get() a(@Query() q: ListDto) {}", imports="""\
import { IsOptional } from "class-validator"
class PageDto { limit?: number; offset: number = 0 }
class ListDto extends PageDto {
  search!: string
  @IsOptional() sort: string
  kind?: 'a' | 'b'
}
""")
        q = self.params(self.op(out, "get", "/items"), "query")
        self.assertEqual({k: v["required"] for k, v in q.items()},
                         {"limit": False, "offset": False, "search": True, "sort": False, "kind": False})

    def test_query_with_an_open_type_or_the_raw_request_has_no_query_params(self):
        for sig in ("@Query() q: any", "@Query() q: Record<string, string>", "@Query('a') a: string, @Query() q: object",
                    "@Query('a') a: string, @Req() req: any", "@Query('a') a: string, @Request() req: any"):
            out = self.ctrl(f"@Get() a({sig}) {{}}")
            self.assertEqual(self.params(self.op(out, "get", "/items"), "query"), {}, sig)

    def test_body_dto_properties_and_required(self):
        out = self.ctrl("@Post() a(@Body() dto: CreateItemDto) {}\n@Put(':id') b(@Param('id') id: string, @Body() dto: CreateItemDto) {}",
                        imports="class CreateItemDto { name: string; note?: string; count: number = 1 }\n")
        a = self.op(out, "post", "/items")
        self.assertEqual(a["requestBody"]["content"]["application/json"]["schema"], {"$ref": "#/components/schemas/CreateItemDto"})
        s = self.schema(out, a)
        self.assertEqual(sorted(s["properties"]), ["count", "name", "note"])
        self.assertEqual(s["required"], ["name"])
        self.assertNotIn("additionalProperties", s)
        self.assertEqual(s["properties"]["name"], {"type": "string"})
        self.assertEqual(self.op(out, "put", "/items/{id}")["requestBody"], a["requestBody"])   # one component, shared

    def test_body_named_fields(self):
        out = self.ctrl("@Post() a(@Body('title') title: string) {}\n"
                        "@Put() b(@Body('title') title: string, @Body('count') count: number) {}\n"
                        "@Patch() c(@Body() dto: Dto, @Body('extra') extra: string) {}",
                        imports="class Dto { name: string }\n")
        a = self.schema(out, self.op(out, "post", "/items"))
        self.assertEqual(a["properties"], {"title": {"type": "string"}})
        self.assertNotIn("required", a)
        b = self.schema(out, self.op(out, "put", "/items"))
        self.assertEqual(sorted(b["properties"]), ["count", "title"])
        c = self.op(out, "patch", "/items")["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(c["allOf"][0], {"$ref": "#/components/schemas/Dto"})
        self.assertEqual(list(c["allOf"][1]["properties"]), ["extra"])

    def test_body_arrays_primitives_and_files(self):
        out = self.ctrl("@Post('many') a(@Body() list: Dto[]) {}\n@Post('text') b(@Body() t: string) {}\n"
                        "@Post('up') c(@UploadedFile() f: any, @Body() dto: Dto) {}\n@Post('up2') d(@UploadedFile() f: any) {}\n@Get() e() {}",
                        imports="class Dto { name: string }\n")
        self.assertEqual(self.op(out, "post", "/items/many")["requestBody"]["content"]["application/json"]["schema"], {"type": "array", "items": {}})
        self.assertEqual(self.op(out, "post", "/items/text")["requestBody"]["content"]["application/json"]["schema"], {"type": "string"})
        up = self.op(out, "post", "/items/up")["requestBody"]["content"]["multipart/form-data"]["schema"]
        self.assertEqual(up["allOf"][0], {"$ref": "#/components/schemas/Dto"})
        self.assertTrue(up["allOf"][1]["additionalProperties"])
        self.assertTrue(self.op(out, "post", "/items/up2")["requestBody"]["content"]["multipart/form-data"]["schema"]["additionalProperties"])
        self.assertNotIn("requestBody", self.op(out, "get", "/items"))

    def test_two_dtos_with_one_name_get_two_components(self):
        out = self.backend({
            "src/a/a.controller.ts": 'import { Controller, Post, Body } from "@nestjs/common"\nclass CreateDto { a: string }\n'
                                     "@Controller('a')\nexport class A {\n  @Post() x(@Body() d: CreateDto) {}\n}\n",
            "src/b/b.controller.ts": 'import { Controller, Post, Body } from "@nestjs/common"\nclass CreateDto { b: string }\n'
                                     "@Controller('b')\nexport class B {\n  @Post() x(@Body() d: CreateDto) {}\n}\n"})
        schemas = out["spec"]["components"]["schemas"]
        self.assertEqual(sorted(schemas), ["CreateDto", "CreateDto_2"])
        self.assertEqual(sorted(list(schemas["CreateDto"]["properties"]) + list(schemas["CreateDto_2"]["properties"])), ["a", "b"])

    # ---------- DTO shapes ----------

    def dto(self, decl, kind="body"):
        """Generate a spec for a controller that takes `Dto` as its body (or its query) and return (spec, schema or query params)."""
        if kind == "body":
            out = self.ctrl("@Post() a(@Body() dto: Dto) {}", imports=decl)
            return out, self.schema(out, self.op(out, "post", "/items"))
        out = self.ctrl("@Get() a(@Query() dto: Dto) {}", imports=decl)
        return out, self.params(self.op(out, "get", "/items"), "query")

    def test_optionality_rules(self):
        _, s = self.dto("""\
import { IsOptional, IsString, ValidateIf } from "class-validator"
import { ApiProperty, ApiPropertyOptional } from "@nestjs/swagger"
class Dto {
  required: string
  definite!: string
  @IsString() decorated: string
  optional?: string
  @IsOptional() byValidator: string
  @ApiPropertyOptional() bySwaggerOptional: string
  @ApiProperty({ required: false }) bySwaggerFlag: string
  @ApiProperty({ required: true }) bySwaggerTrue: string
  @ApiProperty() bySwagger: string
  @ValidateIf((o) => o.x) conditional: string
  withDefault: number = 5
  orUndefined: string | undefined
  orNull: string | null
  static skipped = 1
  method() {}
}
""")
        self.assertEqual(sorted(s["properties"]), sorted(["required", "definite", "decorated", "optional", "byValidator", "bySwaggerOptional",
                                                           "bySwaggerFlag", "bySwaggerTrue", "bySwagger", "conditional", "withDefault",
                                                           "orUndefined", "orNull"]))
        self.assertEqual(sorted(s["required"]), sorted(["required", "definite", "decorated", "bySwaggerTrue", "bySwagger", "orNull"]))

    def test_extends_chain_and_overrides(self):
        _, s = self.dto("class A { a: string; shared: string }\nclass B extends A { b?: string; shared?: string }\nclass Dto extends B { c: number }\n")
        self.assertEqual(sorted(s["properties"]), ["a", "b", "c", "shared"])
        self.assertEqual(sorted(s["required"]), ["a", "c"])    # the nearest declaration of "shared" wins

    def test_mapped_types(self):
        _, s = self.dto('import { PartialType } from "@nestjs/swagger"\nclass Base { a: string; b: number }\nclass Dto extends PartialType(Base) { own: string }\n')
        self.assertEqual(sorted(s["properties"]), ["a", "b", "own"])
        self.assertEqual(s["required"], ["own"])      # PartialType makes the inherited ones optional only

        _, s = self.dto('import { PickType } from "@nestjs/swagger"\nclass Base { a: string; b: number; c?: string }\n'
                        "class Dto extends PickType(Base, ['a', 'c'] as const) {}\n")
        self.assertEqual((sorted(s["properties"]), s["required"]), (["a", "c"], ["a"]))

        _, s = self.dto('import { OmitType } from "@nestjs/swagger"\nconst DROP = ["a"] as const\nclass Base { a: string; b: number; c?: string }\n'
                        "class Dto extends OmitType(Base, DROP) {}\n")
        self.assertEqual((sorted(s["properties"]), s["required"]), (["b", "c"], ["b"]))

        _, s = self.dto('import { IntersectionType } from "@nestjs/swagger"\nclass A { a: string }\nclass B { b?: string }\n'
                        "class Dto extends IntersectionType(A, B) {}\n")
        self.assertEqual((sorted(s["properties"]), s["required"]), (["a", "b"], ["a"]))

        _, s = self.dto('import { PartialType, OmitType } from "@nestjs/swagger"\nclass Base { a: string; b: number }\n'
                        "class Dto extends PartialType(OmitType(Base, ['a'] as const)) {}\n")
        self.assertEqual((sorted(s["properties"]), s.get("required")), (["b"], None))

    def test_mapped_types_work_without_the_package_installed(self):
        repo = make_backend({"src/items.controller.ts": controller(
            "@Post() a(@Body() dto: Dto) {}", 'import { PartialType } from "@nestjs/mapped-types"\nclass Base { a: string }\n'
                                              "class Dto extends PartialType(Base) {}\n")})
        self.dirs.append(repo)
        out = routes(repo)
        self.assertTrue(out["ok"], out)
        self.assertNotIn("required", self.schema(out, self.op(out, "post", "/items")))

    def test_interfaces_aliases_and_utility_types(self):
        _, s = self.dto("interface Base { a: string }\ninterface Dto extends Base { b?: number; c: boolean }\n")
        self.assertEqual((sorted(s["properties"]), sorted(s["required"])), (["a", "b", "c"], ["a", "c"]))
        self.assertEqual(s["properties"]["c"], {"type": "boolean"})

        _, s = self.dto("type Dto = { a: string; b?: number }\n")
        self.assertEqual((sorted(s["properties"]), s["required"]), (["a", "b"], ["a"]))

        _, s = self.dto("class Base { a: string; b: number }\ntype Dto = Partial<Base>\n")
        self.assertEqual((sorted(s["properties"]), s.get("required")), (["a", "b"], None))

        _, s = self.dto("class Base { a: string; b?: number }\ntype Dto = Pick<Base, 'a'> & { extra: string }\n")
        self.assertEqual((sorted(s["properties"]), sorted(s["required"])), (["a", "extra"], ["a", "extra"]))

        _, q = self.dto("interface Dto { page?: number; q: string }\n", kind="query")
        self.assertEqual({k: v["required"] for k, v in q.items()}, {"page": False, "q": True})

    def test_union_of_dtos_makes_everything_optional(self):
        out = self.ctrl("@Post() a(@Body() dto: One | Two) {}", imports="class One { a: string }\nclass Two { b: string }\n")
        s = out["spec"]["paths"]["/items"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual([x["$ref"].rsplit("/", 1)[1] for x in s["anyOf"]], ["One", "Two"])

    def test_open_schemas_have_no_property_list_to_check_against(self):
        for ty, decl in [("any", ""), ("unknown", ""), ("object", ""), ("Record<string, unknown>", ""), ("{ [k: string]: any }", ""),
                         ("T", ""), ("Dto", "class Dto { [key: string]: any }"), ("Dto", "interface Dto { [key: string]: string }"),
                         ("Dto", 'import { Missing } from "./nowhere"\nclass Dto extends Missing {}'), ("Dto", "class Dto {}"),
                         ("Dto", "type Dto = {}")]:
            out = self.ctrl(f"@Post() a(@Body() dto: {ty}) {{}}", imports=decl + "\n")
            s = self.schema(out, self.op(out, "post", "/items"))
            self.assertTrue(s.get("additionalProperties") is True, (ty, decl, s))
            self.assertNotIn("required", s)
            if ty == "any":
                self.assertEqual(s, {"type": "object", "additionalProperties": True})

    def test_an_index_signature_keeps_the_known_properties_and_stays_open(self):
        _, s = self.dto("class Dto { name: string; [key: string]: any }\n")
        self.assertEqual((list(s["properties"]), s["required"], s["additionalProperties"]), (["name"], ["name"], True))

    def test_a_mixin_base_class_is_read_from_its_type(self):
        _, s = self.dto("function WithId<T extends new (...a: any[]) => {}>(b: T) { return class extends b { id: number } }\n"
                        "class Base { name: string }\nclass Dto extends WithId(Base) { own: string }\n")
        self.assertEqual(sorted(s["properties"]), ["id", "name", "own"])

    # ---------- with api_contract ----------

    def test_the_spec_works_with_api_contract(self):
        backend = make_backend({
            "src/main.ts": MAIN_TS % "app.setGlobalPrefix('api')",
            "src/orders.controller.ts": controller("""\
  @Get() list(@Query('status') status: string) {}
  @Get(':id') one(@Param('id') id: string) {}
  @Post() create(@Body() dto: CreateOrderDto) {}
  @Delete(':id') remove(@Param('id') id: string) {}
""", imports="""\
import { PartialType } from "@nestjs/swagger"
class LineDto { sku: string }
class CreateOrderDto { customerId: number; lines: LineDto[]; note?: string }
""", head="@Controller('orders')"),
            "src/items.controller.ts": controller("@Put(':id') update(@Param('id') id: string, @Body() dto: UpdateItemDto) {}",
                                                  imports='import { PartialType } from "@nestjs/swagger"\n'
                                                          "class CreateItemDto { name: string; price: number }\n"
                                                          "class UpdateItemDto extends PartialType(CreateItemDto) {}\n")})
        self.dirs.append(backend)
        generated = routes(backend)
        self.assertTrue(generated["ok"], generated)
        spec_file = backend / "spec.json"
        spec_file.write_text(json.dumps(generated["spec"]))

        client = make_client({"src/http.ts": HTTP_TS, "src/calls.ts": """\
import { api } from "./http"
declare const id: string

export function calls() {
  api.get("/api/orders", { status: "open" })                         // line 5: fine
  api.get(`/api/orders/${id}`)                                       // line 6: fine
  api.put("/api/orders", {})                                         // line 7: wrong method
  api.get("/api/customers")                                          // line 8: no such route
  api.post("/api/orders", { customerId: 1, lines: [], bogus: true }) // line 9: an undeclared key
  api.post("/api/orders", { customerId: 1 })                         // line 10: a missing required key
  api.post("/api/orders", { customerId: 1, lines: [], note: "n" })   // line 11: fine, optional key sent
  api.put("/api/items/5", { price: 3 })                              // line 12: PartialType: nothing required
  api.get("/orders", { status: "open" })                             // line 13: the base prefix may be left out
}
"""})
        self.dirs.append(client)
        result = check_client(client, generated["spec"])
        self.assertTrue(result["ok"], result)
        by_line = {c["line"]: c for c in result["calls"] if c["file"] == "src/calls.ts"}
        self.assertEqual(result["summary"]["base"], "api")
        self.assertEqual((by_line[5]["status"], by_line[5]["unknown_keys"], by_line[5]["missing_required"]), ("ok", [], []))
        self.assertEqual((by_line[6]["status"], by_line[6]["endpoint"]), ("ok", "GET /api/orders/{id}"))
        self.assertEqual(by_line[7]["status"], "method-mismatch")
        self.assertEqual(by_line[8]["status"], "no-endpoint")
        self.assertEqual((by_line[9]["status"], by_line[9]["unknown_keys"], by_line[9]["missing_required"]), ("ok", ["bogus"], []))
        self.assertEqual((by_line[10]["status"], by_line[10]["unknown_keys"], by_line[10]["missing_required"]), ("ok", [], ["lines"]))
        self.assertEqual((by_line[11]["unknown_keys"], by_line[11]["missing_required"]), ([], []))
        self.assertEqual((by_line[12]["status"], by_line[12]["unknown_keys"], by_line[12]["missing_required"]), ("ok", [], []))
        self.assertEqual(by_line[13]["status"], "ok")

    def test_open_shapes_never_give_unknown_key_findings_in_api_contract(self):
        backend = make_backend({"src/items.controller.ts": controller(
            "@Post() a(@Body() dto: any) {}\n@Post('rec') b(@Body() dto: Record<string, unknown>) {}\n"
            "@Post('known') c(@Body() dto: Dto) {}\n@Get() d(@Query() q: any) {}\n@Get('raw') e(@Query('a') a: string, @Req() r: any) {}",
            imports="class Dto { name: string; [k: string]: any }\n")})
        self.dirs.append(backend)
        spec = routes(backend)["spec"]
        client = make_client({"src/http.ts": HTTP_TS, "src/calls.ts": """\
import { api } from "./http"
export function calls() {
  api.post("/items", { anything: 1 })
  api.post("/items/rec", { anything: 1 })
  api.post("/items/known", { anything: 1 })
  api.get("/items", { whatever: 1 })
  api.get("/items/raw", { whatever: 1 })
}
"""})
        self.dirs.append(client)
        calls = check_client(client, spec)["calls"]
        by_line = {c["line"]: c for c in calls if c["file"] == "src/calls.ts"}
        for line in (3, 4, 6, 7):
            self.assertEqual((by_line[line]["status"], by_line[line]["unknown_keys"]), ("ok", []), line)
        self.assertEqual((by_line[5]["unknown_keys"], by_line[5]["missing_required"]), ([], ["name"]))   # still checks what it knows


if __name__ == "__main__":
    unittest.main()
