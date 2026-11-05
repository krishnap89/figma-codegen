"""Visual check: Figma export + sandbox render -> side-by-side page. No network, no LLM, no Gradle."""
import struct
import sys
import zlib
from pathlib import Path

import httpx
import pytest

from codegen.cli import main
from codegen.compare import png_size, write_compare_page
from codegen.config import PlatformConfig, load_config
from codegen.design_system import DesignSystem
from codegen.figma_client import FigmaClient, default_image_name, load_node_from_file
from codegen.render import (RenderConfigError, RenderError, ScreenShape, entry_code, entry_file,
                            render)
from codegen.simplify import simplify
from codegen.validate import compile_code

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/login_nodes.json"
COPY = "import shutil, sys; shutil.copyfile(sys.argv[1], sys.argv[2])"


def make_png(path: Path, w: int, h: int) -> Path:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\x00" + b"\xff\xff\xff" * w for _ in range(h))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return path


@pytest.fixture
def shape():
    ds = DesignSystem.load(ROOT / "manifest/components.json", ROOT / "manifest/tokens.json")
    spec = simplify(ds, load_node_from_file(str(FIXTURE))).spec
    return ScreenShape.from_spec(ds, spec["root"], "compose", "LoginScreen")


def sandbox(tmp_path, platform="compose", **kw) -> PlatformConfig:
    ext = ".kt" if platform == "compose" else ".swift"
    return PlatformConfig(sandbox_file=str(tmp_path / "sb" / f"Generated{ext}"), compile_cwd=str(tmp_path), **kw)


def test_png_size(tmp_path):
    assert png_size(make_png(tmp_path / "a.png", 3, 5).read_bytes()) == (3, 5)
    with pytest.raises(ValueError):
        png_size(b"GIF89a" + b"\0" * 30)


def test_compose_entry_calls_screen_with_defaults(shape):
    code = entry_code("compose", shape, "com.company.login")
    assert "package codegen.render" in code
    assert "import com.company.login.LoginScreen\n" in code
    assert "import com.company.login.LoginScreenState\n" in code
    assert "fun CodegenRenderTarget() {" in code
    for arg in ("state = LoginScreenState()", "onEmailChange = {}", "onPasswordChange = {}",
                "onForgotPassword = {}", "onLogin = {}"):
        assert arg in code


def test_swiftui_entry_uses_state_binding(shape):
    code = entry_code("swiftui", shape)
    assert "@State private var state = LoginScreenState()" in code
    assert "LoginScreen(state: $state, onForgotPassword: {}, onLogin: {})" in code


def test_compile_keeps_sandbox_compiling_with_stub_entry(tmp_path):
    cfg = sandbox(tmp_path, compile_cmd=[sys.executable, "-c", "pass"], render_cmd=["true"])
    ok, _, skipped = compile_code("// screen", cfg, "compose")
    assert ok and not skipped
    stub = entry_file(cfg, "compose").read_text()
    assert stub.endswith("fun CodegenRenderTarget() {}\n")
    assert entry_file(cfg, "compose") == tmp_path / "sb" / "CodegenRenderTarget.kt"


def test_compile_without_render_writes_no_entry(tmp_path):
    cfg = sandbox(tmp_path, compile_cmd=[sys.executable, "-c", "pass"])
    compile_code("// screen", cfg, "compose")
    assert not (tmp_path / "sb" / "CodegenRenderTarget.kt").exists()


def test_render_passes_out_and_size_and_copies_png(tmp_path, shape):
    src = make_png(tmp_path / "shot.png", 4, 8)
    check = ("import os, shutil, sys; "
             "assert os.environ['CODEGEN_RENDER_WIDTH'] == '360' == os.environ['TEST_RUNNER_CODEGEN_RENDER_WIDTH']; "
             "assert sys.argv[3] == 'LoginScreen'; shutil.copyfile(sys.argv[1], sys.argv[2])")
    cfg = sandbox(tmp_path, render_cmd=[sys.executable, "-c", check, str(src), "{out}", "{name}"])
    out = render("fun LoginScreen() {}\n", "compose", cfg, shape, tmp_path / "out" / "render.png", (360, 800))
    assert png_size(out.read_bytes()) == (4, 8)
    # A package-less screen is moved next to the entry, since Kotlin can't import the default package.
    assert Path(cfg.sandbox_file).read_text().startswith("package codegen.render\n")
    assert "import " + "codegen.render.LoginScreen" not in entry_file(cfg, "compose").read_text()


def test_render_finds_output_by_glob(tmp_path, shape):
    src = make_png(tmp_path / "shot.png", 2, 2)
    cfg = sandbox(tmp_path, render_cmd=[sys.executable, "-c", COPY, str(src), "build/shots/LoginScreen_1.png"],
                  render_output="build/shots/*.png")
    (tmp_path / "build/shots").mkdir(parents=True)
    out = render("package a.b\n", "compose", cfg, shape, tmp_path / "render.png")
    assert out.exists()
    assert "import a.b.LoginScreen\n" in entry_file(cfg, "compose").read_text()


def test_render_errors(tmp_path, shape):
    with pytest.raises(RenderConfigError, match="render_cmd"):
        render("", "compose", sandbox(tmp_path), shape, tmp_path / "r.png")
    failing = sandbox(tmp_path, render_cmd=[sys.executable, "-c", "import sys; print('error: boom'); sys.exit(1)"])
    with pytest.raises(RenderError, match="boom"):
        render("", "compose", failing, shape, tmp_path / "r.png")
    silent = sandbox(tmp_path, render_cmd=[sys.executable, "-c", "pass"])
    with pytest.raises(RenderError, match="no new PNG"):
        render("", "compose", silent, shape, tmp_path / "r.png")


def test_compare_page(tmp_path):
    fig = make_png(tmp_path / "f.png", 720, 1600)   # 360x800 @2x
    ren = make_png(tmp_path / "r.png", 1080, 2520)  # 360x840 @3x
    page = write_compare_page(fig, ren, tmp_path / "cmp", "LoginScreen", frame_size=(360, 800),
                              meta={"Code": "LoginScreen.kt"})
    html = page.path.read_text()
    assert page.width == 360
    assert html.count("data:image/png;base64,") == 2
    assert "LoginScreen.kt" in html and "@3x" in html
    assert page.notes and "800" in page.notes[0] and "840" in page.notes[0]
    assert (tmp_path / "cmp/figma.png").exists() and (tmp_path / "cmp/render.png").exists()


def test_export_image_skips_token_on_cdn(tmp_path):
    png = make_png(tmp_path / "x.png", 1, 1).read_bytes()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.figma.com":
            assert req.url.params["ids"] == "12:300" and req.url.params["scale"] == "2"
            return httpx.Response(200, json={"err": None, "images": {"12:300": "https://cdn.example/x.png"}})
        assert "x-figma-token" not in req.headers
        return httpx.Response(200, content=png)

    client = FigmaClient("tok", transport=httpx.MockTransport(handler))
    assert client.export_image("AbC", "12:300") == png
    assert default_image_name("AbC", "12:300", 2) == "AbC_12-300@2x.png"


def _config(tmp_path, extra: str = "") -> Path:
    p = tmp_path / "codegen.yaml"
    p.write_text(f"paths:\n  manifest: {ROOT / 'manifest/components.json'}\n  tokens: {ROOT / 'manifest/tokens.json'}\n"
                 f"  figma_json: figma\n  compare: compare\n{extra}")
    return p


def test_cli_compare_with_given_images(tmp_path, capsys):
    fig, shot = make_png(tmp_path / "f.png", 4, 8), make_png(tmp_path / "s.png", 6, 12)
    rc = main(["--config", str(_config(tmp_path)), "compare", "--figma-image", str(fig),
               "--screenshot", str(shot), "--name", "Login"])
    assert rc == 0
    assert (tmp_path / "compare/Login/index.html").exists()
    assert "compare/Login/index.html" in capsys.readouterr().out


def test_cli_compare_renders_in_sandbox(tmp_path):
    shot = make_png(tmp_path / "s.png", 6, 12)
    sb = tmp_path / "sb"
    sb.mkdir()
    extra = (f"platforms:\n  compose:\n    sandbox_file: sb/Generated.kt\n    compile_cwd: sb\n"
             f"    render_cmd: ['{sys.executable}', '-c', '{COPY}', '{shot}', '{{out}}']\n")
    code = tmp_path / "LoginScreen.kt"
    code.write_text("package com.x\n")
    rc = main(["--config", str(_config(tmp_path, extra)), "compare", "--figma-json", str(FIXTURE),
               "--figma-image", str(make_png(tmp_path / "f.png", 4, 8)), "--code", str(code)])
    assert rc == 0
    assert (tmp_path / "compare/LoginScreen/render.png").exists()
    assert "onLogin = {}" in (sb / "CodegenRenderTarget.kt").read_text()


def test_cli_compare_without_render_config_explains(tmp_path, capsys):
    code = tmp_path / "LoginScreen.kt"
    code.write_text("")
    rc = main(["--config", str(_config(tmp_path)), "compare", "--figma-json", str(FIXTURE),
               "--figma-image", str(make_png(tmp_path / "f.png", 4, 8)), "--code", str(code)])
    assert rc == 2
    assert "render_cmd" in capsys.readouterr().out


def test_render_config_parsed(tmp_path):
    cfg = load_config(str(_config(tmp_path, "platforms:\n  swiftui:\n    sandbox_file: s/G.swift\n"
                                            "    render_cmd: xcodebuild test\n    render_output: out/*.png\n")), env={})
    pc = cfg.platform("swiftui")
    assert pc.render_cmd == ["xcodebuild", "test"]
    assert pc.render_output == str(tmp_path / "out/*.png")
    assert pc.render_timeout == 600
    assert cfg.compare_dir == str(tmp_path / "compare")
