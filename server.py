#!/usr/bin/env python3
"""tubedata-kb: zdalny serwer MCP (streamable-http) za Bearer tokenem.

Endpoints:
  /mcp            — protokół MCP (kb_search, kb_doc); wymaga Authorization: Bearer $MCP_TOKEN
  /admin/restore  — jednorazowy upload snapshotu Qdranta (multipart 'snapshot'); też za tokenem
  /healthz        — bez tokenu (healthcheck Coolify)
"""
import hmac
import os
import re

import httpx
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

from mcp.server.fastmcp import FastMCP

QDRANT = os.environ.get("QDRANT_URL", "http://qdrant:6333")
TOKEN = os.environ["MCP_TOKEN"]  # brak tokenu = świadomy crash na starcie
COLL = "tubedata_kb"
MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

_cli = _model = None


def clients():
    global _cli, _model
    if _cli is None:
        from qdrant_client import QdrantClient
        from fastembed import TextEmbedding
        _cli = QdrantClient(url=QDRANT, timeout=30)
        _model = TextEmbedding(MODEL)
    return _cli, _model


def yt_ts_url(url: str, ts: str | None) -> str:
    if not ts or "youtube" not in (url or ""):
        return url or ""
    parts = [int(x) for x in ts.split(":")]
    secs = parts[-1] + parts[-2] * 60 + (parts[-3] * 3600 if len(parts) == 3 else 0)
    return f"{url}&t={secs}s"


mcp = FastMCP("tubedata-kb", stateless_http=True)


@mcp.tool()
def kb_search(query: str, limit: int = 8, min_quality: int = 3,
              channel: str = "", category: str = "", level: str = "") -> str:
    """Szukaj w bazie wiedzy founderów (2200+ transkrypcji YT i artykułów o SaaS,
    marketingu, pricing, GTM, copywritingu, direct response). Query po polsku lub
    angielsku. level: 'summary' (całe materiały) lub 'chunk' (fragmenty z
    timestampami); puste = oba. Zwraca tytuły, linki (YT z timestampem) i fragmenty."""
    cli, model = clients()
    from qdrant_client import models as qm
    vec = list(model.embed([query]))[0].tolist()
    must = [qm.FieldCondition(key="quality", range=qm.Range(gte=min_quality))]
    if channel:
        must.append(qm.FieldCondition(key="channel", match=qm.MatchValue(value=channel)))
    if category:
        must.append(qm.FieldCondition(key="category", match=qm.MatchValue(value=category)))
    if level:
        must.append(qm.FieldCondition(key="level", match=qm.MatchValue(value=level)))
    hits = cli.query_points(COLL, query=vec, using="e5s", limit=limit,
                            query_filter=qm.Filter(must=must), with_payload=True).points
    lines = []
    for h in hits:
        p = h.payload
        ts = f" [{p['ts']}]" if p.get("ts") else ""
        snippet = re.sub(r"\s+", " ", p.get("text", ""))[:400]
        lines.append(f"• ({round(h.score, 3)}) [{p['channel']}] {p['title']}{ts} "
                     f"(q{p['quality']}, {p['level']}, doc:{p['doc_id']})\n"
                     f"  {yt_ts_url(p.get('url', ''), p.get('ts'))}\n  {snippet}")
    return "\n".join(lines) or "brak wyników"


@mcp.tool()
def kb_doc(doc_id: str) -> str:
    """Pobierz pełne streszczenie i metadane dokumentu bazy po doc_id (z wyników kb_search)."""
    cli, _ = clients()
    from qdrant_client import models as qm
    pts, _off = cli.scroll(COLL, limit=50, with_payload=True,
                           scroll_filter=qm.Filter(must=[
                               qm.FieldCondition(key="doc_id", match=qm.MatchValue(value=doc_id))]))
    if not pts:
        return "nie znaleziono"
    summ = next((p.payload for p in pts if p.payload["level"] == "summary"), pts[0].payload)
    nch = sum(1 for p in pts if p.payload["level"] == "chunk")
    return (f"{summ['title']} [{summ['channel']}] q{summ['quality']}, {nch} fragmentów\n"
            f"{summ.get('url', '')}\n\n{summ.get('text', '')}")


class Auth(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        got = request.headers.get("authorization", "")
        if not hmac.compare_digest(got, f"Bearer {TOKEN}"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


async def healthz(request):
    return PlainTextResponse("ok")


async def restore(request):
    """Jednorazowa migracja: PUT raw body → wspólny wolumen → qdrant recover file://.
    Streaming na dysk (bez RAM), bez multiparta."""
    dst = "/exchange/upload.snapshot"
    n = 0
    with open(dst, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
            n += len(chunk)
    async with httpx.AsyncClient(timeout=900) as c:
        r = await c.put(f"{QDRANT}/collections/{COLL}/snapshots/recover?wait=true",
                        json={"location": "file:///exchange/upload.snapshot",
                              "priority": "snapshot"})
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:500]}
    body["uploaded_bytes"] = n
    return JSONResponse(body, status_code=r.status_code)


app = Starlette(
    routes=[
        Route("/healthz", healthz),
        Route("/admin/restore", restore, methods=["PUT"]),
        Mount("/", app=mcp.streamable_http_app()),
    ],
    middleware=[Middleware(Auth)],
)
