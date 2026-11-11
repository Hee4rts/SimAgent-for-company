"""Web application entry point; requires the optional web dependencies."""

from .app import create_app

__all__ = ["create_app"]
