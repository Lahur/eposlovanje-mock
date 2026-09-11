import logging

import httpx
from fastapi import Request, Response

logger = logging.getLogger("proxy")

client = httpx.AsyncClient(timeout=30.0)

# Headers that must not be forwarded as-is between hops.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


async def forward(request: Request, base_url: str, path: str, auth_header: str, auth_value: str) -> Response:
    logger.info(
        "%s %s -> %s%s (incoming %s=%r)",
        request.method, request.url.path, base_url, path,
        auth_header, request.headers.get(auth_header),
    )

    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    # ASGI headers are lowercase, so a plain `headers[auth_header] = auth_value` (auth_header is
    # mixed-case, e.g. "Authorization") would add a second, differently-cased key rather than
    # replace the caller's own auth header — httpx then sends both on the wire, which upstream
    # (an IIS-fronted API) rejects outright as a malformed request. Drop any case-variant first.
    headers = {k: v for k, v in headers.items() if k.lower() != auth_header.lower()}
    headers[auth_header] = auth_value

    upstream = await client.request(
        request.method,
        f"{base_url}{path}",
        params=request.query_params,
        content=await request.body(),
        headers=headers,
    )

    logger.info("%s %s <- %s", upstream.status_code, request.url.path, base_url)

    response_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=response_headers)
