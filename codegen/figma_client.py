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
    def __init__(self, token: str, timeout: float = 60.0):
        if not token:
            raise ValueError("FIGMA_TOKEN is not set (env var or figma.token in codegen.yaml)")
        self._client = httpx.Client(
            base_url=FIGMA_API,
            headers={"X-Figma-Token": token},
            timeout=timeout,
        )

    def get_node(self, file_key: str, node_id: str) -> dict:
        """Returns {"document", "components", "componentSets", "styles"} for one node."""
        r = self._client.get(f"/files/{file_key}/nodes", params={"ids": node_id})
        r.raise_for_status()
        entry = (r.json().get("nodes") or {}).get(node_id)
        if not entry:
            raise LookupError(f"Node {node_id} not found in file {file_key}")
        return entry

    def list_library_components(self, file_key: str) -> list[dict]:
        """Published components of a library file (useful to build the manifest)."""
        r = self._client.get(f"/files/{file_key}/components")
        r.raise_for_status()
        return r.json().get("meta", {}).get("components", [])


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
