"""FastAPI dependency providers.

Thin accessors from the request to the container. Keeping them here means route
functions read as controller code with no plumbing noise.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request

from app.container import Container
from app.services.storage import GenerationStore

__all__ = ["ContainerDep", "StoreDep"]


async def get_container(request: Request) -> Container:
    """The container attached to this app at startup.

    Async so that resolving it never takes a thread-pool slot: `/healthz`
    must answer while image work and database calls fill that pool.
    """
    return cast(Container, request.app.state.container)


ContainerDep = Annotated[Container, Depends(get_container)]


def get_store(container: ContainerDep) -> GenerationStore:
    return container.store


StoreDep = Annotated[GenerationStore, Depends(get_store)]
