"""Smoke tests: every production backend module must import cleanly in the
pinned environment with dummy configuration (Stage 1/2 baseline, AUD-01/32).
"""

import importlib

import pytest

CORE_MODULES = ["database", "models", "main", "jobs", "worker"]

SERVICE_MODULES = [
    "services.media_pipeline",
    "services.timeline",
    "services.renderer",
    "services.story_understanding",
    "services.script_analyzer",
    "services.visual_registry",
    "services.source_cache",
    "services.final_video_auditor",
    "services.narration",
    "services.captions",
    "services.caption_renderer",
    "services.music",
    "services.graphics_pipeline",
    "services.production_state",
    "services.search_provider",
    "services.llm_client",
    "services.llm_provider",
]


@pytest.mark.parametrize("module", CORE_MODULES + SERVICE_MODULES)
def test_module_imports(module):
    importlib.import_module(module)
