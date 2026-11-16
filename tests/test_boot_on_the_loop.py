"""`uvicorn --factory` builds the app on the event loop's own thread.

The migration and the seed run there before any request is served. That is
the boot work the loop guard allows; without the allowance every boot logged
one `event_loop.blocking` warning per seed query, live and locally.
"""

from __future__ import annotations

import asyncio

from app.config import Settings
from app.main import create_app
from tests.helpers import RecordingProvider


def test_building_the_app_on_the_loop_is_allowed_boot_work(
    settings: Settings, provider: RecordingProvider
) -> None:
    async def boot() -> None:
        # The suite's guard is strict: a blocked query would raise here.
        create_app(settings, provider=provider)

    asyncio.run(boot())
