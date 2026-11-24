"""Project Intelligence (founder priority #10; gap-audit doc 05
§2 and doc 01 §1.12).

The audit's finding: every project already IS a persistent,
knowledgeable object — Case Bible, decisions ledger, production
history, quality artefacts — but the knowledge is spread across
artefacts and endpoints with no service that assembles it into
answerable context, and entity connections (doc 01 §1.12) exist
only as raw material inside the bibles. This module is that
assembly layer. It invents no store and no statistic:

- RECORDED data passes through as recorded: bible entities (via
  the Research Studio's bible view, corrections overlay included),
  run rows, QA verdicts (via the Phase F assembly — never
  re-banded here), and the provider funnels each production's
  asset report recorded.
- DERIVED data is labelled with its derivation, in the Research
  Studio gaps pattern: an entity "shared" between two projects
  means the same normalised name + recorded entity type appears
  in both projects' Case Bibles; an entity "mentioned" in a
  timeline event means its recorded name or an alias appears in
  the event's recorded text. Derivations are deterministic counts
  over the records — a reader can recount every one of them.
- RECOMMENDATIONS are deterministic rules over the same records,
  each carrying the figures it rests on. The run-quality findings
  are the Assistant's own computation (design 02 §H), moved here
  so the Assistant and this surface share ONE implementation —
  the Assistant tool delegates to run_findings() below. A rule
  whose evidence is absent produces no recommendation; there is
  no "engagement" filler.
- OWNERSHIP: every aggregate is built from the caller's own
  projects only (the routes verify the per-project choke point;
  the account builder filters Project.owner_id). Another user's
  project contributes nothing — not even to a count.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from database import SessionLocal
from models import Project
from services import media_paths, qa_report, research_studio, run_ledger

SCHEMA = "cinemora.project_intelligence/1"

RECORDED = "recorded"
NOT_RECORDED = "not_recorded"
UNAVAILABLE = "unavailable"

# Derivation labels — printed verbatim wherever derived data is
# shown, in the Research Studio gaps pattern.
SHARED_ENTITY_DERIVATION = (
    "derived: the same normalised entity name and recorded entity "
    "type appears in the Case Bibles of both projects; matching is "
    "on the user-corrected name/type where a correction is recorded"
)
TIMELINE_MENTION_DERIVATION = (
    "derived: the entity's recorded name or one of its recorded "
    "aliases appears in the timeline event's recorded text"
)
PROVIDER_AGGREGATE_DERIVATION = (
    "derived: summed from the provider funnel each production "
    "recorded in its asset report, over the latest production run "
    "of each of your projects that recorded one"
)


# ---------------------------------------------------------------------
# Small shared readers
# ---------------------------------------------------------------------


def _iso(value):
    return value.isoformat() if value is not None else None


def _load_artefact(project_id: str, filename: str, run_id: str | None):
    """One artefact as (state, data) with the QA report's
    semantics: absent -> not_recorded, unparseable -> unavailable,
    parsed dict -> recorded."""
    project_dir = media_paths.project_dir(project_id)
    path = media_paths.resolve_artefact(project_dir, filename, run_id)
    if not path.is_file():
        return NOT_RECORDED, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return UNAVAILABLE, None
    if not isinstance(data, dict):
        return UNAVAILABLE, None
    return RECORDED, data


def _owned_project_rows(owner_id) -> list[dict]:
    """The caller's projects as plain dicts, oldest first. This is
    the ONLY project source for account aggregates, so nothing of
    another user's can enter a count."""
    db = SessionLocal()
    try:
        rows = (
            db.query(Project)
            .filter(Project.owner_id == str(owner_id))
            .order_by(Project.created_at.asc())
            .all()
        )
        return [
            {
                "project_id": row.id,
                "title": row.title,
                "category": row.category,
                "status": row.status,
                "created_at": _iso(row.created_at),
            }
            for row in rows
        ]
    finally:
        db.close()


def _project_row(project_id) -> dict | None:
    db = SessionLocal()
    try:
        row = db.get(Project, str(project_id))
        if row is None:
            return None
        return {
            "project_id": row.id,
            "title": row.title,
            "category": row.category,
            "status": row.status,
            "created_at": _iso(row.created_at),
        }
    finally:
        db.close()


def _verdict_summary(project_id: str, run: dict) -> dict:
    """The run's QA verdicts via the Phase F assembly (the same
    pass-through versioning uses) — summarised to the verdict
    strings; the QA Report tab carries the full recorded detail."""
    try:
        report = qa_report.build_qa_report(project_id, run)
    except Exception:
        return {
            "state": UNAVAILABLE,
            "integrity": None,
            "quality": None,
            "reliability": None,
            "delivery_status": None,
        }
    verdicts = report["verdicts"].get("verdicts") or {}
    return {
        "state": report["verdicts"]["state"],
        "integrity": (verdicts.get("integrity") or {}).get("verdict"),
        "quality": (verdicts.get("quality") or {}).get("verdict"),
        "reliability": (verdicts.get("reliability") or {}).get("verdict"),
        "delivery_status": report["delivery"].get("status"),
    }


# ---------------------------------------------------------------------
# Entities: normalisation, indexing, connections
# ---------------------------------------------------------------------


def _normalise(value) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    if text.startswith("the "):
        text = text[4:]
    return text


def _entity_key(entity: dict) -> tuple[str, str] | None:
    """The cross-project identity of a bible entity: its effective
    (user-corrected where corrected) name + type, normalised. An
    entity with no name cannot be matched and returns None."""
    name = _normalise(entity.get("effective_canonical") or entity.get("canonical"))
    if not name:
        return None
    return name, _normalise(entity.get("effective_type") or entity.get("type"))


def _entity_evidence(entity: dict, project: dict) -> dict:
    """One project's recorded evidence for an entity: the bible's
    own values (recorded name/type stay visible even where a user
    correction supplied the effective ones used for matching)."""
    return {
        "project_id": project["project_id"],
        "project_title": project["title"],
        "entity_id": entity.get("id"),
        "canonical": entity.get("canonical"),
        "type": entity.get("type"),
        "effective_canonical": entity.get("effective_canonical"),
        "effective_type": entity.get("effective_type"),
    }


def _bible_entities(project_id: str) -> tuple[bool, list]:
    """(recorded, entities) through the Research Studio's bible
    view — the corrections overlay and recorded KB checks ride
    along, exactly as the Research tab shows them."""
    view = research_studio.bible_view(project_id)
    if not view.get("recorded"):
        return False, []
    return True, [e for e in view.get("entities") or [] if isinstance(e, dict)]


def _entity_index(projects: list[dict]) -> dict:
    """key -> {"canonical", "type", "appearances": [evidence]} over
    the given projects' bibles. Projects without a recorded bible
    contribute nothing."""
    index: dict = {}
    for project in projects:
        recorded, entities = _bible_entities(project["project_id"])
        if not recorded:
            continue
        seen_in_project = set()
        for entity in entities:
            key = _entity_key(entity)
            if key is None or key in seen_in_project:
                continue
            seen_in_project.add(key)
            entry = index.setdefault(
                key,
                {
                    "canonical": entity.get("effective_canonical")
                    or entity.get("canonical"),
                    "type": entity.get("effective_type") or entity.get("type"),
                    "appearances": [],
                },
            )
            entry["appearances"].append(_entity_evidence(entity, project))
    return index


def _shared_entities(index: dict, *, exclude_project_id=None) -> list:
    """Entities appearing in at least two projects (or, with
    exclude_project_id, in at least one OTHER project and present
    in the excluded one — the caller filters). Sorted by reach,
    then name, so the ordering is deterministic."""
    shared = []
    for entry in index.values():
        project_ids = {a["project_id"] for a in entry["appearances"]}
        if len(project_ids) < 2:
            continue
        shared.append(
            {
                "canonical": entry["canonical"],
                "type": entry["type"],
                "project_count": len(project_ids),
                "appearances": entry["appearances"],
                "derivation": SHARED_ENTITY_DERIVATION,
            }
        )
    shared.sort(key=lambda e: (-e["project_count"], e["canonical"] or ""))
    return shared


def _timeline_mentions(project_id: str) -> list:
    """DERIVED entity↔event links: the entity's recorded name or an
    alias appears in a bible timeline event's recorded text (the
    mention-matching derivation the Research Studio's gaps already
    use for sources, applied to the bible's own timeline)."""
    view = research_studio.bible_view(project_id)
    if not view.get("recorded"):
        return []
    events = [
        e
        for e in view.get("timeline") or []
        if isinstance(e, dict) and e.get("origin") == "case_bible" and e.get("what")
    ]
    if not events:
        return []
    mentions = []
    for entity in view.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        names = [entity.get("effective_canonical") or entity.get("canonical")]
        names += list(entity.get("aliases") or [])
        names = [n for n in names if n and len(str(n).strip()) >= 3]
        if not names:
            continue
        hits = []
        for event in events:
            text = str(event["what"]).lower()
            matched = next((n for n in names if str(n).lower() in text), None)
            if matched is not None:
                hits.append(
                    {
                        "when": event.get("when"),
                        "what": event.get("what"),
                        "support": event.get("support"),
                        "fact_basis": event.get("fact_basis"),
                        "matched_name": matched,
                    }
                )
        if hits:
            mentions.append(
                {
                    "entity_id": entity.get("id"),
                    "canonical": entity.get("effective_canonical")
                    or entity.get("canonical"),
                    "type": entity.get("effective_type") or entity.get("type"),
                    "events": hits,
                    "derivation": TIMELINE_MENTION_DERIVATION,
                }
            )
    return mentions


# ---------------------------------------------------------------------
# Provider patterns (recorded funnels, aggregated)
# ---------------------------------------------------------------------


def _latest_run_funnel(project_id: str):
    """(run, state, funnel, providers_block) for the project's
    latest production run's recorded asset report."""
    run = run_ledger.latest_production_run_for(project_id)
    if run is None:
        return None, NOT_RECORDED, None, None
    state, report = _load_artefact(project_id, "asset_report.json", run["run_id"])
    if state != RECORDED:
        return run, state, None, None
    providers = report.get("providers") or {}
    funnel = providers.get("funnel")
    if not isinstance(funnel, dict):
        return run, NOT_RECORDED, None, providers
    return run, RECORDED, funnel, providers


_FUNNEL_FIELDS = ("discovered", "verified", "rejected", "downloaded", "download_failed")


def _aggregate_providers(projects: list[dict], *, exclude_project_id=None) -> dict:
    """Sum the recorded provider funnels of the latest production
    run of each project. Every figure is a sum of recorded counts;
    the base (how many projects/runs recorded a funnel) is stated
    beside the totals, and a project whose run recorded no funnel
    contributes nothing and is counted as not recorded."""
    totals: dict = {}
    projects_with_funnel = 0
    runs_included = []
    for project in projects:
        if project["project_id"] == exclude_project_id:
            continue
        run, state, funnel, _block = _latest_run_funnel(project["project_id"])
        if state != RECORDED or funnel is None:
            continue
        projects_with_funnel += 1
        runs_included.append(
            {"project_id": project["project_id"], "run_id": run["run_id"]}
        )
        for provider, counts in funnel.items():
            if not isinstance(counts, dict):
                continue
            entry = totals.setdefault(
                provider,
                {field: 0 for field in _FUNNEL_FIELDS} | {"projects": set()},
            )
            for field in _FUNNEL_FIELDS:
                value = counts.get(field)
                if isinstance(value, int):
                    entry[field] += value
            entry["projects"].add(project["project_id"])
    providers = [
        {
            "provider": provider,
            **{field: entry[field] for field in _FUNNEL_FIELDS},
            "project_count": len(entry["projects"]),
            "project_ids": sorted(entry["projects"]),
        }
        for provider, entry in totals.items()
    ]
    providers.sort(key=lambda p: (-p["verified"], -p["discovered"], p["provider"]))
    return {
        "state": RECORDED if projects_with_funnel else NOT_RECORDED,
        "projects_with_funnel": projects_with_funnel,
        "runs_included": runs_included,
        "providers": providers,
        "derivation": PROVIDER_AGGREGATE_DERIVATION,
    }


# ---------------------------------------------------------------------
# Recommendations — deterministic rules over recorded data
# ---------------------------------------------------------------------


def run_findings(project_id: str, run_id: str, report: dict) -> list:
    """The Assistant's run-quality findings (design 02 §H), moved
    here so the Assistant tool and Project Intelligence share one
    implementation: computed from the run's own asset_report
    figures — coverage, missing visuals, repeated footage,
    unverified assets, low-confidence selections, retrieval
    failures, and the recorded single-provider reliance warning.
    A figure that is absent or zero produces no finding."""
    findings = []
    coverage = report.get("coverage") or {}
    total_sentences = coverage.get("total_sentences")
    covered = coverage.get("covered_sentences")
    if (
        isinstance(total_sentences, int)
        and isinstance(covered, int)
        and total_sentences > 0
        and covered < total_sentences
    ):
        findings.append(
            {
                "finding": "Incomplete visual coverage",
                "evidence": {
                    "covered_sentences": covered,
                    "total_sentences": total_sentences,
                    "uncovered_sentences": coverage.get("uncovered_sentences"),
                },
                "suggestion": (
                    "Review the uncovered sentences in the Asset "
                    "Manager and consider a re-run; uncovered beats "
                    "fall back to generated graphics."
                ),
                "link": f"/projects/{project_id}/assets",
            }
        )
    missing = report.get("missing_visual_count")
    if isinstance(missing, int) and missing > 0:
        findings.append(
            {
                "finding": "Sentences with no verified visual",
                "evidence": {"missing_visual_count": missing},
                "suggestion": (
                    "Open the plan view to see which sentences lack "
                    "a verified visual and record decisions for them."
                ),
                "link": f"/projects/{project_id}/plan",
            }
        )
    repeated = report.get("repeated_footage")
    if isinstance(repeated, int) and repeated > 0:
        findings.append(
            {
                "finding": "Repeated footage",
                "evidence": {
                    "repeated_footage": repeated,
                    "source_reuse": report.get("source_reuse"),
                },
                "suggestion": (
                    "The same footage serves multiple sentences; a "
                    "re-run with more retrieval, or plan decisions "
                    "replacing the repeats, would improve variety."
                ),
                "link": f"/projects/{project_id}/assets",
            }
        )
    unverified = report.get("unverified_assets")
    if isinstance(unverified, int) and unverified > 0:
        findings.append(
            {
                "finding": "Unverified assets in the cut",
                "evidence": {"unverified_assets": unverified},
                "suggestion": (
                    "Check the verification verdicts in the Asset "
                    "Manager before publishing."
                ),
                "link": f"/projects/{project_id}/assets",
            }
        )
    low_confidence = report.get("low_confidence_selections")
    if isinstance(low_confidence, int) and low_confidence > 0:
        findings.append(
            {
                "finding": "Low-confidence visual selections",
                "evidence": {"low_confidence_selections": low_confidence},
                "suggestion": (
                    "Review the low-confidence picks in the plan and "
                    "replace the weakest ones."
                ),
                "link": f"/projects/{project_id}/plan",
            }
        )
    download_failures = report.get("download_failures") or []
    discovery_failures = report.get("discovery_failures") or []
    if download_failures or discovery_failures:
        findings.append(
            {
                "finding": "Retrieval failures during production",
                "evidence": {
                    "download_failures": len(download_failures),
                    "discovery_failures": len(discovery_failures),
                },
                "suggestion": (
                    "Some sources could not be retrieved; a re-run "
                    "may succeed now, or the plan can be adjusted "
                    "for the affected sentences."
                ),
                "link": f"/projects/{project_id}/assets",
            }
        )
    reliance = (report.get("providers") or {}).get("single_provider_reliance") or {}
    if reliance.get("warning") is True:
        findings.append(
            {
                "finding": "Single-provider reliance",
                "evidence": {
                    "provider": reliance.get("provider"),
                    "footage_share": reliance.get("footage_share"),
                    "footage_base": reliance.get("footage_base"),
                    "stills_share": reliance.get("stills_share"),
                    "stills_base": reliance.get("stills_base"),
                },
                "suggestion": (
                    "One provider supplied most of this production's "
                    "accepted visuals, as recorded in the run's own "
                    "reliance check; widening retrieval would reduce "
                    "that dependence."
                ),
                "link": f"/projects/{project_id}/assets",
            }
        )
    return findings


def _project_recommendations(project: dict, owner_id) -> dict:
    """All recommendation rules for one project, each rule's
    findings carrying their recorded evidence. Rules whose inputs
    were never recorded contribute nothing and say why in the
    rule's state."""
    project_id = project["project_id"]
    rules = []

    # Rule 1 — the run's own quality figures (the Assistant's set).
    run = run_ledger.latest_production_run_for(project_id)
    if run is None:
        rules.append(
            {
                "rule": "run_quality",
                "state": NOT_RECORDED,
                "findings": [],
                "note": "No production run exists yet, so there is no evidence to base recommendations on.",
            }
        )
    else:
        state, report = _load_artefact(project_id, "asset_report.json", run["run_id"])
        if state != RECORDED:
            rules.append(
                {
                    "rule": "run_quality",
                    "state": state,
                    "run_id": run["run_id"],
                    "findings": [],
                    "note": "The latest run recorded no readable asset report, so no evidence-based recommendations can be made.",
                }
            )
        else:
            rules.append(
                {
                    "rule": "run_quality",
                    "state": RECORDED,
                    "run_id": run["run_id"],
                    "findings": run_findings(project_id, run["run_id"], report),
                    "note": "Findings are computed from the run's recorded asset report figures; no figure here is estimated.",
                }
            )

    # Rules 2–3 need the account context: the caller's other
    # projects' bibles and funnels.
    others = [p for p in _owned_project_rows(owner_id) if p["project_id"] != project_id]

    # Rule 2 — entities recorded in at least two other projects'
    # bibles but absent from this project's bible.
    recorded, entities = _bible_entities(project_id)
    if not recorded:
        rules.append(
            {
                "rule": "shared_entity_coverage",
                "state": NOT_RECORDED,
                "findings": [],
                "note": "This project has no recorded Case Bible, so entity coverage cannot be compared.",
            }
        )
    else:
        own_keys = {
            key for key in (_entity_key(e) for e in entities) if key is not None
        }
        other_index = _entity_index(others)
        findings = []
        for entry in _shared_entities(other_index):
            key = (
                _normalise(entry["canonical"]),
                _normalise(entry["type"]),
            )
            if key in own_keys:
                continue
            findings.append(
                {
                    "finding": (
                        f"Entity recorded across your other projects: "
                        f"{entry['canonical']}"
                    ),
                    "evidence": {
                        "canonical": entry["canonical"],
                        "type": entry["type"],
                        "project_count": entry["project_count"],
                        "projects": [
                            {
                                "project_id": a["project_id"],
                                "project_title": a["project_title"],
                            }
                            for a in entry["appearances"]
                        ],
                        "derivation": SHARED_ENTITY_DERIVATION,
                    },
                    "suggestion": (
                        "This entity is part of the recorded story of "
                        "several of your other documentaries but does "
                        "not appear in this project's Case Bible; if "
                        "it belongs to this story, covering it would "
                        "connect the films."
                    ),
                    "link": f"/projects/{project_id}/research",
                }
            )
        rules.append(
            {
                "rule": "shared_entity_coverage",
                "state": RECORDED,
                "findings": findings[:10],
                "findings_total": len(findings),
                "note": "Entities are matched across Case Bibles by normalised name and recorded type; only entities in at least two other projects are suggested.",
            }
        )

    # Rule 3 — providers that delivered verified assets in your
    # other productions but verified nothing in this project's
    # latest recorded run.
    _run2, funnel_state, own_funnel, _block = _latest_run_funnel(project_id)
    if funnel_state != RECORDED or own_funnel is None:
        rules.append(
            {
                "rule": "underused_verified_providers",
                "state": funnel_state,
                "findings": [],
                "note": "This project's latest production run recorded no provider funnel, so provider use cannot be compared.",
            }
        )
    else:
        aggregate = _aggregate_providers(others)
        findings = []
        if aggregate["state"] == RECORDED:
            for provider in aggregate["providers"]:
                if provider["verified"] < 1:
                    continue
                own = own_funnel.get(provider["provider"]) or {}
                if isinstance(own, dict) and (own.get("verified") or 0) >= 1:
                    continue
                findings.append(
                    {
                        "finding": (
                            f"Verified source unused in this production: "
                            f"{provider['provider']}"
                        ),
                        "evidence": {
                            "provider": provider["provider"],
                            "verified_in_other_projects": provider["verified"],
                            "discovered_in_other_projects": provider["discovered"],
                            "other_project_count": provider["project_count"],
                            "verified_in_this_project": (
                                own.get("verified") if isinstance(own, dict) else None
                            ),
                            "derivation": PROVIDER_AGGREGATE_DERIVATION,
                        },
                        "suggestion": (
                            "This source delivered verified assets in "
                            "your other productions but none in this "
                            "project's latest run; it may be worth "
                            "widening retrieval to include it."
                        ),
                        "link": f"/projects/{project_id}/assets",
                    }
                )
        rules.append(
            {
                "rule": "underused_verified_providers",
                "state": RECORDED,
                "findings": findings[:8],
                "findings_total": len(findings),
                "note": "Providers are the keys recorded in the runs' provider funnels, compared exactly as recorded.",
            }
        )

    total = sum(len(rule["findings"]) for rule in rules)
    return {"rules": rules, "findings_total": total}


# ---------------------------------------------------------------------
# Per-project intelligence
# ---------------------------------------------------------------------


def build_project_intelligence(project_id: str, owner_id) -> dict:
    """One project's intelligence view. The caller (route) has
    already applied the Phase 8 owner choke point for project_id;
    owner_id scopes the cross-project context to the same user."""
    project = _project_row(project_id)
    runs = run_ledger.list_runs_for(project_id)
    production_runs = [
        r for r in runs if (r.get("options") or {}).get("kind") != "revision_render"
    ]
    run_counts = {
        "total": len(runs),
        "production": len(production_runs),
        "by_status": {},
    }
    for run in runs:
        status = run.get("status") or "unknown"
        run_counts["by_status"][status] = run_counts["by_status"].get(status, 0) + 1

    latest = run_ledger.latest_production_run_for(project_id)
    quality = (
        _verdict_summary(project_id, latest)
        if latest is not None
        else {"state": NOT_RECORDED, "integrity": None, "quality": None,
              "reliability": None, "delivery_status": None}
    )

    bible_recorded, entities = _bible_entities(project_id)
    bible_view = research_studio.bible_view(project_id)
    entity_rows = [
        {
            "entity_id": e.get("id"),
            "canonical": e.get("canonical"),
            "type": e.get("type"),
            "effective_canonical": e.get("effective_canonical"),
            "effective_type": e.get("effective_type"),
            "role": e.get("role"),
            "support": e.get("support"),
            "is_primary_subject": bool(
                bible_view.get("primary_subject_id")
                and e.get("id") == bible_view.get("primary_subject_id")
            ),
            "corrections": len(e.get("corrections") or []),
            "kb_check": (
                {
                    "status": (e.get("kb_check") or {}).get("status"),
                    "conflict": bool((e.get("kb_check") or {}).get("conflict")),
                    "kb_type": (e.get("kb_check") or {}).get("kb_type"),
                }
                if isinstance(e.get("kb_check"), dict)
                else None
            ),
        }
        for e in entities
    ]
    kb_conflicts = [row for row in entity_rows if row["kb_check"] and row["kb_check"]["conflict"]]

    # Cross-project context: this project's entities that the same
    # user recorded in other projects too.
    others = [p for p in _owned_project_rows(owner_id) if p["project_id"] != project_id]
    index = _entity_index([project] + others) if project else {}
    shared_here = []
    if project is not None:
        for entry in _shared_entities(index):
            mine = [
                a for a in entry["appearances"] if a["project_id"] == project_id
            ]
            if not mine:
                continue
            shared_here.append(
                {
                    "canonical": entry["canonical"],
                    "type": entry["type"],
                    "project_count": entry["project_count"],
                    "this_project": mine[0],
                    "other_projects": [
                        {
                            "project_id": a["project_id"],
                            "project_title": a["project_title"],
                            "entity_id": a["entity_id"],
                        }
                        for a in entry["appearances"]
                        if a["project_id"] != project_id
                    ],
                    "derivation": SHARED_ENTITY_DERIVATION,
                }
            )

    research = research_studio.overview(project_id)
    return {
        "schema": SCHEMA,
        "project": project,
        "record": {
            "runs": run_counts,
            "latest_production_run": (
                {
                    "run_id": latest["run_id"],
                    "status": latest.get("status"),
                    "created_at": _iso(latest.get("created_at")),
                }
                if latest is not None
                else None
            ),
            "bible_recorded": bible_recorded,
            "story_type": bible_view.get("story_type"),
            "research_counts": research.get("counts"),
        },
        "quality": quality,
        "entities": entity_rows,
        "connections": {
            "primary_subject_id": bible_view.get("primary_subject_id"),
            "shared_entities": shared_here,
            "timeline_mentions": _timeline_mentions(project_id),
            "kb_conflicts": kb_conflicts,
        },
        "recommendations": _project_recommendations(project, owner_id)
        if project is not None
        else {"rules": [], "findings_total": 0},
    }


# ---------------------------------------------------------------------
# Account intelligence
# ---------------------------------------------------------------------


def build_account_intelligence(owner_id) -> dict:
    """The caller's whole catalogue as recorded patterns: projects
    by category, quality outcomes by category (latest production
    run per project, verdicts via the Phase F assembly), provider
    delivery patterns (recorded funnels, latest run per project),
    and entities shared across projects. Only the caller's own
    projects are read."""
    projects = _owned_project_rows(owner_id)

    by_category: dict = {}
    quality_by_category: dict = {}
    project_rows = []
    for project in projects:
        category = project["category"] or "Documentary"
        by_category[category] = by_category.get(category, 0) + 1
        runs = run_ledger.list_runs_for(project["project_id"])
        latest = run_ledger.latest_production_run_for(project["project_id"])
        quality = (
            _verdict_summary(project["project_id"], latest)
            if latest is not None
            else None
        )
        bucket = quality_by_category.setdefault(
            category,
            {"projects": 0, "with_recorded_verdict": 0, "quality_verdicts": {}},
        )
        bucket["projects"] += 1
        if quality is not None and quality["state"] == RECORDED and quality["quality"]:
            bucket["with_recorded_verdict"] += 1
            verdict = quality["quality"]
            bucket["quality_verdicts"][verdict] = (
                bucket["quality_verdicts"].get(verdict, 0) + 1
            )
        bible_recorded, entities = _bible_entities(project["project_id"])
        project_rows.append(
            {
                **project,
                "run_count": len(runs),
                "latest_production_run": (
                    {
                        "run_id": latest["run_id"],
                        "status": latest.get("status"),
                        "created_at": _iso(latest.get("created_at")),
                    }
                    if latest is not None
                    else None
                ),
                "quality": quality,
                "bible_recorded": bible_recorded,
                "entity_count": len(entities),
            }
        )

    index = _entity_index(projects)
    shared = _shared_entities(index)
    providers = _aggregate_providers(projects)

    return {
        "schema": SCHEMA,
        "totals": {
            "projects": len(projects),
            "runs": sum(row["run_count"] for row in project_rows),
            "projects_with_bible": sum(
                1 for row in project_rows if row["bible_recorded"]
            ),
            "distinct_entities": len(index),
            "shared_entities": len(shared),
        },
        "projects_by_category": [
            {"category": category, "projects": count}
            for category, count in sorted(
                by_category.items(), key=lambda kv: (-kv[1], kv[0])
            )
        ],
        "quality_by_category": [
            {"category": category, **bucket}
            for category, bucket in sorted(quality_by_category.items())
        ],
        "providers": providers,
        "entity_connections": shared,
        "projects": project_rows,
    }
