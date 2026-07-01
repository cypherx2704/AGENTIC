"""WP02 — env-driven stage-enable flags (STAGE_ENABLE_<NAME>).

``Settings`` carries one ``stage_enable_<name>`` field per registry slot (defaults
mirror the first-cycle registry); ``core.pipeline.apply_stage_flags`` is consulted at
startup (the api lifespan) so future stages enable per environment without code edits.

The registry is module-global state, so every test snapshots + restores it (the same
discipline ``test_pipeline._bound_registry`` uses).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from agent_runtime.core import pipeline as pipeline_mod
from agent_runtime.core.config import Settings
from agent_runtime.core.pipeline import Pipeline, PipelineContext, Stage, StageSpec, apply_stage_flags

FIRST_CYCLE_ENABLED = {"LOAD", "PRE_GUARDRAIL", "PROMPT_BUILD", "LLM", "POST_GUARDRAIL"}


@pytest.fixture(autouse=True)
def _snapshot_registry() -> Iterator[None]:
    original = [StageSpec(s.name, s.enabled, s.stage_cls) for s in pipeline_mod.STAGE_REGISTRY]
    try:
        yield
    finally:
        pipeline_mod.STAGE_REGISTRY[:] = original


def _enabled_names() -> set[str]:
    return {s.name for s in pipeline_mod.STAGE_REGISTRY if s.enabled}


def test_default_settings_keep_first_cycle_shape() -> None:
    apply_stage_flags(Settings())
    assert _enabled_names() == FIRST_CYCLE_ENABLED


def test_disable_flag_turns_a_stage_off() -> None:
    apply_stage_flags(Settings(stage_enable_llm=False))
    assert "LLM" not in _enabled_names()
    assert _enabled_names() == FIRST_CYCLE_ENABLED - {"LLM"}


def test_enable_flag_turns_an_enhancement_stage_on() -> None:
    # Future stages enable via env alone — no code edits (the class still has to be
    # bound for from_registry to RUN it; the flag flips the registry's enabled bit).
    apply_stage_flags(Settings(stage_enable_tool_loop=True))
    assert _enabled_names() == FIRST_CYCLE_ENABLED | {"TOOL_LOOP"}


def test_env_var_spelling_maps_to_settings_field(monkeypatch: Any) -> None:
    monkeypatch.setenv("STAGE_ENABLE_MEMORY_RETRIEVE", "true")
    monkeypatch.setenv("STAGE_ENABLE_POST_GUARDRAIL", "false")
    settings = Settings()  # fresh, uncached — reads the patched env
    assert settings.stage_enable_memory_retrieve is True
    assert settings.stage_enable_post_guardrail is False


async def test_disabled_stage_is_skipped_by_the_runner() -> None:
    """End-to-end through the REAL runner: a disabled stage never executes."""
    ran: list[str] = []

    def _recording_stage(stage_name: str) -> type[Stage]:
        class _S(Stage):
            name = stage_name

            async def run(self, ctx: PipelineContext) -> None:
                ran.append(self.name)

        return _S

    class _Event(Stage):
        name = "EVENT"

        async def run(self, ctx: PipelineContext) -> None:
            ran.append("EVENT")

    for name in FIRST_CYCLE_ENABLED:
        pipeline_mod.bind_stage(name, _recording_stage(name))

    apply_stage_flags(Settings(stage_enable_llm=False))
    ctx: Any = object.__new__(PipelineContext)  # runner only reads terminal_error here
    ctx.terminal_error = None

    await Pipeline.from_registry(_Event()).run(ctx)

    assert "LLM" not in ran
    assert ran == ["LOAD", "PRE_GUARDRAIL", "PROMPT_BUILD", "POST_GUARDRAIL", "EVENT"]
