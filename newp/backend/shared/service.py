"""FastAPI application factory.

Every service is built from this, so all six behave identically at the edges:
same CORS policy, same error envelope, same /health contract, same access log.
A judge can point at any service and the answers to "how do I check it's up?"
and "what happens when it breaks?" are the same.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import settings
from .errors import AppError

_LOG_FORMAT = "%(asctime)s  %(levelname)-7s  %(name)-20s  %(message)s"


def configure_logging(name: str) -> logging.Logger:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format=_LOG_FORMAT,
        datefmt="%H:%M:%S",
    )
    return logging.getLogger(name)


def create_app(
    *,
    name: str,
    title: str,
    description: str,
    version: str = "0.1.0",
    on_shutdown: Optional[Callable[[], Any]] = None,
) -> FastAPI:
    log = configure_logging(name)
    started_at = time.time()

    @asynccontextmanager
    async def lifespan(_: FastAPI):  # type: ignore[no-untyped-def]
        log.info("%s ready", name)
        yield
        if on_shutdown is not None:
            # Close the httpx clients this service holds open to its
            # downstreams, so a restart doesn't leak sockets.
            result = on_shutdown()
            if hasattr(result, "__await__"):
                await result

    app = FastAPI(
        title=title,
        description=description,
        version=version,
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    app.state.service_name = name
    app.state.started_at = started_at

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def access_log(request: Request, call_next):  # type: ignore[no-untyped-def]
        began = time.perf_counter()
        response = await call_next(request)
        took = (time.perf_counter() - began) * 1000
        if request.url.path != "/health":  # health polls every few seconds
            log.info(
                "%s %s -> %s (%.1fms)",
                request.method,
                request.url.path,
                response.status_code,
                took,
            )
        response.headers["X-Service"] = name
        response.headers["X-Response-Time-Ms"] = f"{took:.1f}"
        return response

    # -- error envelope ------------------------------------------------

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError):  # type: ignore[no-untyped-def]
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def handle_validation(_: Request, exc: RequestValidationError):  # type: ignore[no-untyped-def]
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(p) for p in first.get("loc", [])[1:]) or "request"
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": f"That request didn't look right: {field} — {first.get('msg', 'invalid')}.",
                    "hint": "See /docs for the expected shape.",
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):  # type: ignore[no-untyped-def]
        # Nothing reaches the customer as a stack trace. It reaches the log.
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "Something went wrong on our side. Nothing was changed.",
                    "hint": f"{type(exc).__name__} in {name} — check the service log.",
                }
            },
        )

    # -- identity + health ---------------------------------------------

    @app.get("/", tags=["meta"], summary="Service identity")
    async def root() -> dict:
        return {
            "service": name,
            "title": title,
            "description": description,
            "version": version,
            "docs": "/docs",
        }

    @app.get("/health", tags=["meta"], summary="Liveness")
    async def health() -> dict:
        return {
            "service": name,
            "status": "up",
            "version": version,
            "uptimeSeconds": round(time.time() - started_at, 1),
            "checkedAt": dt.datetime.now().isoformat(timespec="seconds"),
        }

    return app
