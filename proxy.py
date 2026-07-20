"""Per-session reverse proxy — the browser sidecar's backend.

A session's container publishes its app on `127.0.0.1:<app_port>`. The console
can't reach that directly from a page served by the control plane, so every
request is forwarded through `/sessions/{id}/app/{path}` instead. The pane is
then just an iframe at that path.

Served under a subpath, an app's root-relative assets (`/static/app.js`) would
escape the prefix, so HTML responses get a `<base>` tag injected. Assets
requested as absolute-root URLs by *script* (a hardcoded `fetch("/api/x")`)
still escape — that's a real limitation, not an oversight; apps that do it need
to honour a base path.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

# Hop-by-hop headers are connection-scoped and must not be forwarded.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-encoding",
    "content-length",
}

_HEAD_RE = re.compile(rb"<head\b[^>]*>", re.IGNORECASE)


class ProxyError(RuntimeError):
    pass


def target_url(app_port: int, path: str, query: str = "") -> str:
    url = f"http://127.0.0.1:{app_port}/{path.lstrip('/')}"
    return f"{url}?{query}" if query else url


def forwardable(headers: dict[str, str] | Any) -> dict[str, str]:
    items = headers.items() if hasattr(headers, "items") else headers
    return {k: v for k, v in items if k.lower() not in _HOP_BY_HOP and k.lower() != "host"}


def inject_base(body: bytes, base: str) -> bytes:
    """Point relative URLs at the proxy prefix so subpath-served apps load."""
    tag = f'<base href="{base}">'.encode()
    match = _HEAD_RE.search(body)
    if match:
        return body[: match.end()] + tag + body[match.end() :]
    return tag + body


def is_html(content_type: str) -> bool:
    return content_type.split(";")[0].strip().lower() == "text/html"


async def forward(
    client: httpx.AsyncClient,
    method: str,
    app_port: int,
    path: str,
    query: str,
    headers: dict[str, str],
    body: bytes,
    base: str,
) -> tuple[int, dict[str, str], bytes]:
    """Proxy one request to the session's app. Returns (status, headers, body)."""
    try:
        response = await client.request(
            method,
            target_url(app_port, path, query),
            headers=forwardable(headers),
            content=body or None,
        )
    except httpx.HTTPError as exc:
        raise ProxyError(f"session app unreachable: {exc}") from exc

    out_headers = forwardable(response.headers)
    content = response.content
    if is_html(response.headers.get("content-type", "")):
        content = inject_base(content, base)
    return response.status_code, out_headers, content
