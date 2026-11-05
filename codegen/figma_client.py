"""Minimal Figma REST API client (read-only)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

FIGMA_API = "https://api.figma.com/v1"


def parse_figma_url(url: str) -> tuple[str, str]:
    """Return (file_key, node_id) from a Figma frame link.

    Accepts https://www.figma.com/design/<key>/<name>?node-id=12-300 (also /file/ and /proto/).
    """
    u = urlparse(url)
    m = re.match(r"^/(?:file|design|proto)/([A-Za-z0-9]+)", u.path)
    if not m:
        raise ValueError(f"Not a Figma file URL: {url}")
    node = parse_qs(u.query).get("node-id", [None])[0]
    if not node:
        raise ValueError("URL has no node-id. In Figma, select the frame and use 'Copy link to selection'.")
    return m.group(1), node.replace("-", ":")


class FigmaClient:
    def __init__(self, token: str, timeout: float = 60.0, transport: httpx.BaseTransport | None = None):
        if not token:
            raise ValueError("FIGMA_TOKEN is not set (env var or figma.token in codegen.yaml)")
        self._client = httpx.Client(
            base_url=FIGMA_API,
            headers={"X-Figma-Token": token},
            timeout=timeout,
            transport=transport,
        )
        # Rendered images are served from a CDN URL; don't send the token there.
        self._cdn = httpx.Client(timeout=timeout, follow_redirects=True, transport=transport)

    def export_image(self, file_key: str, node_id: str, scale: float = 2) -> bytes:
        """Render one node to PNG with Figma's own renderer (the 'Export' image designers see)."""
        r = self._client.get(f"/images/{file_key}",
                             params={"ids": node_id, "format": "png", "scale": scale}, timeout=180)
        r.raise_for_status()
        data = r.json()
        url = (data.get("images") or {}).get(node_id)
        if not url:
            raise LookupError(f"Figma could not render node {node_id}: {data.get('err') or 'no image returned'}")
        img = self._cdn.get(url)
        img.raise_for_status()
        return img.content

    def get_nodes_response(self, file_key: str, node_id: str) -> dict:
        """The raw /v1/files/:key/nodes response for one node (the format --figma-json reads)."""
        r = self._client.get(f"/files/{file_key}/nodes", params={"ids": node_id})
        r.raise_for_status()
        data = r.json()
        if not (data.get("nodes") or {}).get(node_id):
            raise LookupError(f"Node {node_id} not found in file {file_key}")
        return data

    def get_node(self, file_key: str, node_id: str) -> dict:
        """Returns {"document", "components", "componentSets", "styles"} for one node."""
        return self.get_nodes_response(file_key, node_id)["nodes"][node_id]

    def list_library_components(self, file_key: str) -> list[dict]:
        """Published components of a library file (useful to build the manifest)."""
        r = self._client.get(f"/files/{file_key}/components")
        r.raise_for_status()
        return r.json().get("meta", {}).get("components", [])


def default_json_name(file_key: str, node_id: str) -> str:
    """'AbC123', '12:300' -> 'AbC123_12-300.json' (':' is awkward in file names)."""
    return f"{file_key}_{node_id.replace(':', '-')}.json"


def default_image_name(file_key: str, node_id: str, scale: float) -> str:
    """'AbC123', '12:300', 2 -> 'AbC123_12-300@2x.png' (saved next to the JSON)."""
    return f"{file_key}_{node_id.replace(':', '-')}@{scale:g}x.png"


def save_nodes_response(data: dict, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return p


def load_node_from_file(path: str, node_id: str | None = None) -> dict:
    """Load a saved /v1/files/:key/nodes response (offline use, tests, debugging)."""
    data = json.loads(Path(path).read_text())
    if "nodes" in data:
        nodes = data["nodes"]
        if node_id and node_id in nodes:
            return nodes[node_id]
        if len(nodes) == 1:
            return next(iter(nodes.values()))
        raise LookupError(f"File has {len(nodes)} nodes; pass a URL with node-id to pick one")
    if "document" in data:
        return data
    raise ValueError("Unrecognised Figma JSON: expected a /files/:key/nodes response")
