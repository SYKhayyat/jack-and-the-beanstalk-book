"""Gemini at gemini.google.com in a local Chrome, as a free backend.

`cdp` speaks the DevTools protocol, `client` drives the Gemini page, and
`factory.client_for` is the one client every gemini_web backend shares.
Deliberately empty: the image providers import `attachment`, and the client
imports the image providers' errors, so nothing here may import either.
"""
