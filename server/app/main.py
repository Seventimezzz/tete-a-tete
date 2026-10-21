"""Точка входа сигналинга Tete-a-tete. HTTP-API нет, Caddy проксирует только /ws."""

from fastapi import FastAPI

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
