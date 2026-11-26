"""Re-use the pipeline app fixtures (fake providers, scripted LLM) for end-to-end fal tests."""

from tests.pipeline.conftest import (  # noqa: F401
    papp,
    phone,
    pipeline_settings,
    services,
    smart_llm,
)
