"""The assistant tool registry (design assistant/06).

One declarative spec per tool: name, family, action class, a
pydantic input model, a plain-language label for the UI, and the
handler. The registry is the single source the orchestrator prompt,
the validator, the admin M9 inspection view and the docs are all
generated from — and it is the allowlist: the orchestrator can only
ever invoke what is registered here, with the action class the
registry declares. The model cannot widen any of this.

Handler contract: `handler(principal, args) -> dict`, where
`principal` is a services.auth.Principal and `args` is the
validated pydantic input model. Handlers verify ownership of every
project/run they touch (via api_v1's owner choke points) and raise
api_v1.ApiError for domain failures, which the orchestrator records
and surfaces verbatim in substance.

Admin M9 overrides live in `assistant_tool_overrides`: a row with
scope "*" disables the whole assistant; a row with a tool's name
disables that tool. Overrides are read fresh on every turn, so an
admin change takes effect on the next request (M9 acceptance).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

from pydantic import BaseModel

READ = "read"
WRITE = "write"
CONSEQUENTIAL = "consequential"

ACTION_CLASSES = (READ, WRITE, CONSEQUENTIAL)

GLOBAL_SCOPE = "*"


class EmptyArgs(BaseModel):
    """Input model for tools that take no arguments."""

    model_config = {"extra": "forbid"}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    family: str
    action_class: str
    label: str  # plain language, for provenance strips and cards
    summary: str  # one line for the model-facing catalogue
    input_model: type[BaseModel]
    handler: Callable
    # Optional: builds the pending-action card summary from the
    # validated arguments. Defaults to the label.
    describe: Callable | None = field(default=None)
    # Where this tool's results come from, for the injection feed:
    # tool_result | search_result | fetched_page | project_script.
    scan_source: str = "tool_result"

    def describe_action(self, args: BaseModel) -> str:
        if self.describe is not None:
            return self.describe(args)
        return self.label


_REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    if spec.name in _REGISTRY:
        raise ValueError(f"Duplicate assistant tool: {spec.name}")
    if spec.action_class not in ACTION_CLASSES:
        raise ValueError(f"Bad action class for {spec.name}")
    _REGISTRY[spec.name] = spec
    return spec


def get(name: str) -> ToolSpec | None:
    return _REGISTRY.get(name)


def all_tools() -> list[ToolSpec]:
    return [_REGISTRY[name] for name in sorted(_REGISTRY)]


def catalogue_for_prompt() -> list[dict]:
    """The model-facing tool catalogue, generated from the specs."""
    catalogue = []
    for spec in all_tools():
        catalogue.append(
            {
                "name": spec.name,
                "class": spec.action_class,
                "summary": spec.summary,
                "arguments": spec.input_model.model_json_schema(),
            }
        )
    return catalogue


# ---------------------------------------------------------------------
# Enablement: env kill switch + admin overrides (M9)
# ---------------------------------------------------------------------


def env_enabled() -> bool:
    return os.environ.get("CINEMORA_ASSISTANT_ENABLED", "true").lower() not in (
        "0",
        "false",
        "no",
    )


def _disabled_scopes(db) -> set[str]:
    from models import AssistantToolOverride

    own_session = False
    if db is None:
        from database import SessionLocal

        db = SessionLocal()
        own_session = True
    try:
        rows = (
            db.query(AssistantToolOverride)
            .filter(AssistantToolOverride.disabled.is_(True))
            .all()
        )
        return {row.scope for row in rows}
    except Exception:
        # A missing overrides table (legacy fixture DBs) must not
        # take the assistant down; absence of rows = nothing disabled.
        try:
            db.rollback()
        except Exception:
            pass
        return set()
    finally:
        if own_session:
            db.close()


def assistant_enabled(db=None) -> bool:
    """Env kill switch AND no global admin override."""
    if not env_enabled():
        return False
    return GLOBAL_SCOPE not in _disabled_scopes(db)


def tool_disabled(db, name: str) -> bool:
    scopes = _disabled_scopes(db)
    return GLOBAL_SCOPE in scopes or name in scopes


def tool_states(db=None) -> list[dict]:
    """Registry + live enablement, for the admin M9 inspection view
    and the assistant's own /tools endpoint."""
    scopes = _disabled_scopes(db)
    states = []
    for spec in all_tools():
        states.append(
            {
                "name": spec.name,
                "family": spec.family,
                "action_class": spec.action_class,
                "label": spec.label,
                "enabled": not (
                    spec.name in scopes or GLOBAL_SCOPE in scopes
                ),
                "disabled": spec.name in scopes or GLOBAL_SCOPE in scopes,
                "disabled_scope": (
                    GLOBAL_SCOPE
                    if GLOBAL_SCOPE in scopes
                    else (spec.name if spec.name in scopes else None)
                ),
            }
        )
    return states
