import logging
import time

import uvicorn
from fastapi import FastAPI, Request
from prometheus_fastapi_instrumentator import Instrumentator

import config
from f1_web_mock import router as f1_web_router
from proxy import forward

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("eposlovanje-mock")

app = FastAPI()

Instrumentator().instrument(app).expose(app)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = (time.monotonic() - start) * 1000
    logger.info("%s %s -> %s (%.1fms)", request.method, request.url.path, response.status_code, duration_ms)
    return response


app.include_router(f1_web_router, prefix="/f1-web", tags=["f1-web (mock)"])


@app.api_route("/eposlovanje/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def eposlovanje_proxy(path: str, request: Request):
    return await forward(request, config.EPOSLOVANJE_BASE_URL, f"/{path}", "Authorization", config.EPOSLOVANJE_API_KEY)


@app.api_route("/pondi/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def pondi_proxy(path: str, request: Request):
    return await forward(request, config.PONDI_BASE_URL, f"/{path}", "ApiKey", config.PONDI_API_KEY)


@app.get("/")
def read_root():
    return {
        "service": "eposlovanje-mock",
        "routes": {
            "/eposlovanje/*": f"proxied to {config.EPOSLOVANJE_BASE_URL}",
            "/pondi/*": f"proxied to {config.PONDI_BASE_URL}",
            "/f1-web/*": "in-memory mock (not proxied)",
        },
    }


def main():
    uvicorn.run(app, host="0.0.0.0", port=8082)


if __name__ == "__main__":
    main()
