# SPDX-License-Identifier: GPL-3.0-or-later
"""RFC 9457 problem details for API errors (ADR 0011 §3)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

PROBLEM_JSON = "application/problem+json"


class ApiProblem(Exception):
    def __init__(
        self,
        status: int,
        title: str,
        detail: str | None = None,
        headers: dict[str, str] | None = None,
        **extensions: Any,
    ):
        super().__init__(detail or title)
        self.status, self.title, self.detail, self.headers = status, title, detail, headers or {}
        # RFC 9457 extension members, e.g. the offset an upload should continue from.
        self.extensions = extensions


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiProblem)
    async def _problem(_: Request, exc: ApiProblem) -> JSONResponse:
        body: dict[str, Any] = {
            **exc.extensions,
            "type": "about:blank",
            "title": exc.title,
            "status": exc.status,
        }
        if exc.detail:
            body["detail"] = exc.detail
        return JSONResponse(
            body, status_code=exc.status, headers=exc.headers, media_type=PROBLEM_JSON
        )
