from datetime import datetime, timezone


PRODUCTION_STAGES = [
    "voice_actor",
    "script_analysis",
    "asset_manager",
    "timeline_builder",
    "renderer",
    "sound_designer",
    "video_editor",
    "motion_designer",
]


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def empty_stage():
    return {
        "status": "pending",
        "started_at": None,
        "completed_at": None,
        "error": None,
    }


def create_production_state():
    return {
        "status": "running",
        "current_stage": None,
        "started_at": utc_now(),
        "completed_at": None,
        "error": None,
        "stages": {stage: empty_stage() for stage in PRODUCTION_STAGES},
    }


def ensure_stage(state, stage):
    if "stages" not in state or not isinstance(state["stages"], dict):
        state["stages"] = {name: empty_stage() for name in PRODUCTION_STAGES}
    if stage not in state["stages"]:
        state["stages"][stage] = empty_stage()
    return state["stages"][stage]


def start_stage(state, stage):
    record = ensure_stage(state, stage)
    record["status"] = "running"
    record["started_at"] = utc_now()
    record["completed_at"] = None
    record["error"] = None
    state["status"] = "running"
    state["current_stage"] = stage
    state["error"] = None
    return state


def complete_stage(state, stage):
    record = ensure_stage(state, stage)
    record["status"] = "completed"
    record["completed_at"] = utc_now()
    record["error"] = None
    if state.get("current_stage") == stage:
        state["current_stage"] = None
    return state


def fail_stage(state, stage, error):
    record = ensure_stage(state, stage)
    record["status"] = "failed"
    record["completed_at"] = utc_now()
    record["error"] = str(error)
    state["status"] = "failed"
    state["current_stage"] = stage
    state["error"] = str(error)
    return state