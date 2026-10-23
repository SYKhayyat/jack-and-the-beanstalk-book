"""HTTP layer. Controllers only: parse input, call services, render a response."""

from app.routes import api, auth, book, media, web

__all__ = ["api", "auth", "book", "media", "web"]
