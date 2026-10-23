"""Application services: prompts, image handling, providers, pipeline, storage.

Each module here owns one concern. Routes depend on these; these do not depend
on routes.
"""

from app.services.images import InvalidImage, decode_upload
from app.services.pipeline import GenerationPipeline, PipelineOutcome
from app.services.prompts import build_cartoon_prompt, build_scene_prompt
from app.services.storage import GenerationStore

__all__ = [
    "GenerationPipeline",
    "GenerationStore",
    "InvalidImage",
    "PipelineOutcome",
    "build_cartoon_prompt",
    "build_scene_prompt",
    "decode_upload",
]
