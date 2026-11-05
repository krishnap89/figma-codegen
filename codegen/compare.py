"""Figma export vs. rendered screen, side by side, as one self-contained HTML page.

No model judges anything here: a person looks at both images. The page offers four views:
side by side, onion skin (opacity slider), swipe (reveal slider) and difference (pixels that
match turn black). Both images are drawn at the Figma frame's width in CSS px, so densities
(Figma @2x vs. an xxhdpi / @3x screenshot) don't matter.
"""
from __future__ import annotations

import base64
import html
import json
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path


def png_size(data: bytes) -> tuple[int, int]:
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError("not a PNG file")
    return struct.unpack(">II", data[16:24])


@dataclass
class ComparePage:
    path: Path              # index.html
    figma_size: tuple[int, int]
    render_size: tuple[int, int]
    width: float            # CSS px both images are drawn at
    notes: list[str]


def write_compare_page(figma_png: Path, render_png: Path, out_dir: Path, title: str, *,
                       frame_size: tuple[float, float] | None = None, figma_scale: float = 2,
                       meta: dict[str, str] | None = None) -> ComparePage:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fig_bytes, ren_bytes = Path(figma_png).read_bytes(), Path(render_png).read_bytes()
    fig_size, ren_size = png_size(fig_bytes), png_size(ren_bytes)
    # Keep the inputs next to the page (handy for attaching to a PR); skip if they're already there.
    for src, name in ((figma_png, "figma.png"), (render_png, "render.png")):
        dst = out_dir / name
        if Path(src).resolve() != dst.resolve():
            shutil.copyfile(src, dst)

    width = frame_size[0] if frame_size and frame_size[0] else fig_size[0] / figma_scale
    fig_h = fig_size[1] * width / fig_size[0]
    ren_h = ren_size[1] * width / ren_size[0]
    notes = []
    if abs(fig_h - ren_h) > 2:
        notes.append(f"Heights differ at the same width: Figma {fig_h:.0f}, rendered {ren_h:.0f} "
                     f"(dp/pt). Content is longer or shorter, or the render device size differs from the frame.")
    fig_ratio, ren_ratio = fig_size[0] / width, ren_size[0] / width
    data = {
        "figma": "data:image/png;base64," + base64.b64encode(fig_bytes).decode(),
        "render": "data:image/png;base64," + base64.b64encode(ren_bytes).decode(),
        "width": width,
    }
    rows = dict(meta or {})
    rows["Figma image"] = f"{fig_size[0]}×{fig_size[1]} px (@{fig_ratio:.3g}x)"
    rows["Rendered image"] = f"{ren_size[0]}×{ren_size[1]} px (@{ren_ratio:.3g}x)"
    rows["Drawn at"] = f"{width:g} dp/pt wide"

    page = _TEMPLATE.replace("__TITLE__", html.escape(title))
    page = page.replace("__META__", "".join(
        f"<div><dt>{html.escape(k)}</dt><dd>{html.escape(str(v))}</dd></div>" for k, v in rows.items()))
    page = page.replace("__NOTES__", "".join(f'<p class="note">{html.escape(n)}</p>' for n in notes))
    page = page.replace("__DATA__", json.dumps(data))
    path = out_dir / "index.html"
    path.write_text(page)
    return ComparePage(path, fig_size, ren_size, width, notes)


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ · Figma vs code</title>
<style>
:root {
  --bg: #f6f6f4; --panel: #ffffff; --text: #1d1d1b; --muted: #6b6b66; --line: #deded8;
  --accent: #2f6fe4; --accent-text: #ffffff; --warn-bg: #fff4d6; --warn-text: #6b4e00;
  --check-a: #ffffff; --check-b: #ececec;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #161616; --panel: #1f1f1f; --text: #ededea; --muted: #9a9a94; --line: #333331;
    --accent: #6c9cff; --accent-text: #0b0b0b; --warn-bg: #3a3014; --warn-text: #f3d98a;
    --check-a: #262626; --check-b: #2e2e2e;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
header { padding: 16px; border-bottom: 1px solid var(--line); background: var(--panel); }
h1 { margin: 0 0 6px; font-size: 18px; }
dl { display: flex; flex-wrap: wrap; gap: 2px 18px; margin: 0; color: var(--muted); font-size: 12px; }
dl div { display: flex; gap: 6px; }
dt::after { content: ":"; } dd { margin: 0; color: var(--text); }
.note { margin: 10px 0 0; padding: 6px 10px; border-radius: 6px; background: var(--warn-bg);
  color: var(--warn-text); font-size: 13px; }
.bar { position: sticky; top: 0; z-index: 2; display: flex; flex-wrap: wrap; align-items: center; gap: 10px 16px;
  padding: 10px 16px; background: var(--panel); border-bottom: 1px solid var(--line); }
.seg { display: inline-flex; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
.seg button { border: 0; background: none; color: var(--text); padding: 6px 12px; font: inherit; cursor: pointer; }
.seg button + button { border-left: 1px solid var(--line); }
.seg button[aria-pressed="true"] { background: var(--accent); color: var(--accent-text); }
.seg button kbd { font: 11px ui-monospace, monospace; opacity: .6; margin-left: 4px; }
label.slider { display: inline-flex; align-items: center; gap: 8px; color: var(--muted); }
label.slider[hidden] { display: none; }
input[type=range] { width: 160px; accent-color: var(--accent); }
main { padding: 16px; overflow-x: auto; }
.pair { display: flex; gap: 24px; align-items: flex-start; justify-content: center; }
figure { margin: 0; min-width: 0; }
figcaption { margin-bottom: 6px; color: var(--muted); font-size: 12px; font-weight: 600;
  text-transform: uppercase; letter-spacing: .04em; }
.frame { position: relative; outline: 1px solid var(--line);
  background: repeating-conic-gradient(var(--check-a) 0 25%, var(--check-b) 0 50%) 0 0 / 16px 16px; }
.frame img { display: block; width: 100%; height: auto; image-rendering: auto; }
.stack .top { position: absolute; top: 0; left: 0; }
.stack.diff .frame { background: #000; }
.stack.diff .top { mix-blend-mode: difference; }
.swipe-line { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--accent); pointer-events: none; }
@media (max-width: 640px) { .pair { flex-direction: column; align-items: center; } }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <dl id="meta">__META__</dl>
  __NOTES__
</header>
<div class="bar">
  <div class="seg" role="group" aria-label="View">
    <button data-mode="side">Side by side<kbd>1</kbd></button><button data-mode="onion">Onion skin<kbd>2</kbd></button><button data-mode="swipe">Swipe<kbd>3</kbd></button><button data-mode="diff">Difference<kbd>4</kbd></button>
  </div>
  <label class="slider" id="sliderBox" hidden><span id="sliderLabel">Code</span>
    <input type="range" id="slider" min="0" max="100" value="50"></label>
  <div class="seg" role="group" aria-label="Zoom">
    <button data-zoom="fit">Fit</button><button data-zoom="1">1×</button><button data-zoom="2">2×</button>
  </div>
</div>
<main id="stage"></main>
<script>
const D = __DATA__;
const state = { mode: "side", zoom: "1", slider: 50 };
try { Object.assign(state, JSON.parse(localStorage.getItem("figma-codegen-compare") || "{}")); } catch (e) {}
const stage = document.getElementById("stage");
const slider = document.getElementById("slider");
const sliderBox = document.getElementById("sliderBox");

function frame(src, alt, cls) {
  const f = document.createElement("div"); f.className = "frame";
  const img = new Image(); img.src = src; img.alt = alt; if (cls) img.className = cls;
  f.appendChild(img); return f;
}
function widthFor(n) {
  if (state.zoom !== "fit") return D.width * Number(state.zoom);
  const avail = stage.clientWidth - 32 - (state.mode === "side" && window.innerWidth > 640 ? 24 : 0);
  return Math.min(D.width * 2, avail / n);
}
function render() {
  document.querySelectorAll("[data-mode]").forEach(b => b.setAttribute("aria-pressed", b.dataset.mode === state.mode));
  document.querySelectorAll("[data-zoom]").forEach(b => b.setAttribute("aria-pressed", b.dataset.zoom === state.zoom));
  sliderBox.hidden = !(state.mode === "onion" || state.mode === "swipe");
  document.getElementById("sliderLabel").textContent = state.mode === "onion" ? "Code opacity" : "Reveal code";
  slider.value = state.slider;
  stage.innerHTML = "";
  if (state.mode === "side") {
    const pair = document.createElement("div"); pair.className = "pair";
    const w = widthFor(window.innerWidth > 640 ? 2 : 1);
    for (const [src, cap] of [[D.figma, "Figma"], [D.render, "Code"]]) {
      const fig = document.createElement("figure"); fig.style.width = w + "px";
      const c = document.createElement("figcaption"); c.textContent = cap;
      fig.append(c, frame(src, cap)); pair.appendChild(fig);
    }
    stage.appendChild(pair); return;
  }
  const wrap = document.createElement("div"); wrap.className = "pair stack" + (state.mode === "diff" ? " diff" : "");
  const fig = document.createElement("figure"); fig.style.width = widthFor(1) + "px";
  const c = document.createElement("figcaption");
  c.textContent = { onion: "Code over Figma", swipe: "Figma | Code", diff: "Difference (black = identical)" }[state.mode];
  const f = frame(D.figma, "Figma");
  const top = new Image(); top.src = D.render; top.alt = "Code"; top.className = "top"; f.appendChild(top);
  if (state.mode === "onion") top.style.opacity = state.slider / 100;
  if (state.mode === "swipe") {
    top.style.clipPath = `inset(0 0 0 ${state.slider}%)`;
    const line = document.createElement("div"); line.className = "swipe-line"; line.style.left = state.slider + "%";
    f.appendChild(line);
  }
  fig.append(c, f); wrap.appendChild(fig); stage.appendChild(wrap);
}
function set(patch) {
  Object.assign(state, patch);
  try { localStorage.setItem("figma-codegen-compare", JSON.stringify(state)); } catch (e) {}
  render();
}
document.querySelectorAll("[data-mode]").forEach(b => b.onclick = () => set({ mode: b.dataset.mode }));
document.querySelectorAll("[data-zoom]").forEach(b => b.onclick = () => set({ zoom: b.dataset.zoom }));
slider.oninput = () => set({ slider: Number(slider.value) });
document.addEventListener("keydown", e => {
  const m = { "1": "side", "2": "onion", "3": "swipe", "4": "diff" }[e.key];
  if (m && !e.metaKey && !e.ctrlKey && e.target.tagName !== "INPUT") set({ mode: m });
});
window.addEventListener("resize", () => { if (state.zoom === "fit") render(); });
render();
</script>
</body>
</html>
"""
