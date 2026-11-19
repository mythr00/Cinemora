"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { ArrowLeft, Plus, Video } from "lucide-react";
import {
  apiUrl,
  getOutputs,
  getProjectStatus,
  legacyRequest,
  listRuns,
  type ProjectOutputs,
  type ProjectStatus,
  type RunSummary,
} from "@/lib/api";

type Asset = { asset_id: string; title: string; type: "video" | "image"; source: string; preview_url: string; provider?: string; source_url?: string; license_name?: string; license_url?: string; creator?: string; attribution_text?: string; attribution_required?: boolean; attribution_url?: string; rights_status?: string; rights_tier?: string; review_status?: string; provider_asset_id?: string };
type Clip = { editor_id: string; asset_id?: string; preview_url?: string; file_path?: string; type: string; title?: string; sentence?: string; start: number; end: number; duration: number; source?: string; provider?: string; source_url?: string; license_name?: string; license_url?: string; creator?: string; attribution_text?: string; attribution_required?: boolean; attribution_url?: string; rights_status?: string; provider_asset_id?: string };
type Scene = { scene_number: number; start: number; end: number; duration_seconds: number; items: Clip[] };
type EditorData = { project: { id: string; category: string; status: string }; timeline: { scenes: Scene[]; duration_seconds: number; edit_revision?: number } | null; assets: Asset[]; video_url: string | null };
type Status = ProjectStatus;
type Overview = { status: ProjectStatus; outputs: ProjectOutputs | null; runs: RunSummary[] };
type Addition = { scene_index: number; asset_id: string; clip_range_start?: number };
type RenderVersion = { version: number; file: string; created_at: string; url?: string };
type RankedCandidate = { asset_id: string; title: string; type: string; score: number; matched_terms: string[]; visual_relevance_verified: false };
type CandidatePool = { scenes: { scene_index: number; candidates: RankedCandidate[] }[]; message?: string };
type RenderOptions = { schema_version: number; graphics: boolean; music: boolean; music_percent: number; captions: boolean; sfx: boolean; music_track_id: string; voice_id: string; format: string; language: string };

function assetUrl(path?: string) { return path ? apiUrl(path) : ""; }
function timecode(seconds: number) { const safe = Math.max(0, seconds || 0); return `${Math.floor(safe / 60).toString().padStart(2, "0")}:${Math.floor(safe % 60).toString().padStart(2, "0")}`; }

export default function ProjectEditorPage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;
  const [data, setData] = useState<EditorData | null>(null);
  const [removed, setRemoved] = useState<string[]>([]);
  const [additions, setAdditions] = useState<Addition[]>([]);
  const [sceneIndex, setSceneIndex] = useState(0);
const [sourceInPoint, setSourceInPoint] = useState(0);
  const [selectedClipId, setSelectedClipId] = useState("");
  const [assetChoice, setAssetChoice] = useState("");
  const [status, setStatus] = useState<Status | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [versions, setVersions] = useState<RenderVersion[]>([]);
  const [candidatePool, setCandidatePool] = useState<CandidatePool>({ scenes: [] });
  const [currentVersion, setCurrentVersion] = useState(1);
  const [renderOptions, setRenderOptions] = useState<RenderOptions>({ schema_version: 1, graphics: false, music: true, music_percent: 3, captions: true, sfx: true, music_track_id: "auto", voice_id: "en_US-lessac-medium", format: "mp4", language: "en" });
  const [rightsConfirmed, setRightsConfirmed] = useState(false);
  const [safetyReviewed, setSafetyReviewed] = useState(false);
  const [privacyReviewed, setPrivacyReviewed] = useState(false);
  const [pendingAssetId, setPendingAssetId] = useState("");
  const [matchQuality, setMatchQuality] = useState<"direct" | "illustrative">("direct");
  const statusName = status?.status;

  const load = useCallback(async () => {
    const result = await legacyRequest<EditorData>(`/projects/${projectId}/editor`);
    setData(result);
    try {
      setCandidatePool(await legacyRequest<CandidatePool>(`/projects/${projectId}/editor/candidates`));
    } catch { /* candidate pool is optional */ }
    setStatus({ id: projectId, category: result.project.category, created_at: "", status: result.project.status, video_url: result.video_url });
    try {
      const options = await legacyRequest<RenderOptions>(`/projects/${projectId}/render-options`);
      setRenderOptions({ schema_version: options.schema_version || 1, graphics: options.graphics ?? false, music: options.music ?? true, music_percent: options.music_percent ?? 3, captions: options.captions ?? true, sfx: options.sfx ?? true, music_track_id: options.music_track_id || "auto", voice_id: options.voice_id || "en_US-lessac-medium", format: options.format || "mp4", language: options.language || "en" });
    } catch { /* render options are optional */ }
    try {
      const versionData = await legacyRequest<{ current?: number; versions?: RenderVersion[] }>(`/projects/${projectId}/versions`);
      setVersions(versionData.versions || []);
      setCurrentVersion(versionData.current || 1);
    } catch { /* version history is optional */ }
  }, [projectId]);

  // API v1 overview (status, outputs, run history) — independent of
  // the legacy editor payload, so a project always has a useful
  // detail view even before/without editor data.
  const [overview, setOverview] = useState<Overview | null>(null);
  const loadOverview = useCallback(async () => {
    try {
      const [statusResult, outputsResult, runsResult] = await Promise.all([
        getProjectStatus(projectId),
        getOutputs(projectId).catch(() => null),
        listRuns(projectId).catch(() => ({ runs: [] as RunSummary[] })),
      ]);
      setOverview({ status: statusResult, outputs: outputsResult, runs: runsResult.runs || [] });
      setStatus(statusResult);
    } catch { /* overview is best-effort; the editor load reports errors */ }
  }, [projectId]);

  useEffect(() => {
    const timer = setTimeout(() => { void loadOverview(); }, 0);
    return () => clearTimeout(timer);
  }, [loadOverview]);

  useEffect(() => {
    const timer = setTimeout(() => {
      void load().catch((error) => setMessage(error instanceof Error ? error.message : "Could not load project."));
    }, 0);
    return () => clearTimeout(timer);
  }, [load]);

  useEffect(() => {
    if (!statusName || !["queued", "running", "pipeline_queued"].includes(statusName)) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const next = await getProjectStatus(projectId);
        if (stopped) return;
        setStatus(next);
        if (next.status === "completed") {
          setBusy(false); setRemoved([]); setAdditions([]); setMessage("Your edit has rendered. Preview or download the updated MP4.");
          await load();
        } else if (next.status === "failed") {
          setBusy(false); setMessage(next.error || "The edit render failed. Your previous export is preserved.");
        } else setMessage(next.current_stage ? `${next.current_stage.replaceAll("_", " ")} in progress…` : "Waiting for the worker…");
      } catch (error) { if (!stopped) setMessage(error instanceof Error ? error.message : "Could not read render status."); }
      if (!stopped) timer = setTimeout(poll, 2500);
    }
    void poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, [projectId, statusName, load]);

  const pendingScenes = useMemo(() => (data?.timeline?.scenes || []).map((scene, index) => {
    const clips = scene.items.filter((clip) => !removed.includes(clip.editor_id));
    for (const addition of additions.filter((item) => item.scene_index === index)) {
      const asset = data?.assets.find((item) => item.asset_id === addition.asset_id);
      if (asset) clips.push({ ...asset, editor_id: `new:${index}:${asset.asset_id}`, asset_id: asset.asset_id, start: scene.start, end: scene.end, duration: scene.duration_seconds });
    }
    return { ...scene, clips };
  }), [data, removed, additions]);
  const activeScene = pendingScenes[sceneIndex];
  const activeClip = activeScene?.clips.find((clip) => clip.editor_id === selectedClipId) || activeScene?.clips[0];
  const isRendering = status?.status === "queued" || status?.status === "running";

  async function refreshCandidates() {
    setBusy(true); setMessage("Ranking registered project footage against this scene's narration…");
    try {
      const result = await legacyRequest<CandidatePool>(`/projects/${projectId}/editor/candidates/refresh`, { method: "POST" });
      setCandidatePool(result); setMessage("Candidate ranking saved. Scores show recorded-word overlap and do not verify visual relevance.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "Could not rank project footage."); }
    finally { setBusy(false); }
  }
  async function uploadAsset(file?: File) {
    if (!file) return;
    setBusy(true); setMessage("Checking and adding your footage to this project…");
    try {
      const body = new FormData(); body.append("file", file); body.append("rights_confirmed", String(rightsConfirmed)); body.append("match_quality", matchQuality);
      const result = await legacyRequest<{ asset?: Asset }>(`/projects/${projectId}/assets`, { method: "POST", body });
      if (!result.asset) throw new Error("Footage upload failed.");
      setPendingAssetId(result.asset.asset_id); setMessage("Technical checks passed. Complete the source-level safety and privacy review before the clip can be selected.");
      setRightsConfirmed(false); setSafetyReviewed(false); setPrivacyReviewed(false);
    } catch (error) { setMessage(error instanceof Error ? error.message : "Footage upload failed."); }
    finally { setBusy(false); }
  }

  async function reviewOwnerAsset() {
    if (!pendingAssetId || !safetyReviewed || !privacyReviewed) return;
    setBusy(true); setMessage("Recording the source-level safety and privacy decision…");
    try {
      const result = await legacyRequest<{ status?: string }>(`/projects/${projectId}/assets/${pendingAssetId}/review`, { method: "POST", body: JSON.stringify({ safety: "pass", privacy: "pass" }) });
      if (result.status !== "approved") throw new Error("The source did not pass safety and privacy review.");
      await load(); setAssetChoice(pendingAssetId); setPendingAssetId(""); setSafetyReviewed(false); setPrivacyReviewed(false); setMessage("The reviewed owner-supplied source is available in the project media pool.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "Source review failed."); }
    finally { setBusy(false); }
  }

  function removeClip(clip: Clip, targetSceneIndex: number) {
    if (!clip.editor_id || clip.editor_id.startsWith("new:")) {
      setAdditions((current) => current.filter((item) => !(item.scene_index === targetSceneIndex && item.asset_id === clip.asset_id)));
      return;
    }
    if ((pendingScenes[targetSceneIndex]?.clips.length || 0) <= 1) { setMessage("A scene needs at least one visual. Add replacement footage before removing its last shot."); return; }
    setRemoved((current) => [...current, clip.editor_id]);
  }

  async function saveEdit() {
    setBusy(true); setMessage("Saving your edit and rendering the updated video…");
    try {
      const result = await legacyRequest<{ status?: string; job_id?: string }>(`/projects/${projectId}/editor`, { method: "PUT", body: JSON.stringify({ remove_item_ids: removed, add_items: additions }) });
      setStatus((current) => ({ id: projectId, category: current?.category || "", created_at: current?.created_at || "", status: result.status || "queued" })); setMessage("Edit queued. Your previous download remains available while this version renders.");
    } catch (error) { setBusy(false); setMessage(error instanceof Error ? error.message : "Could not save the edit."); }
  }

  async function changeHistory(action: "undo" | "redo" | "revert") {
    setBusy(true); setMessage(action === "revert" ? "Reverting to the generated timeline…" : `${action === "undo" ? "Undoing" : "Redoing"} the last timeline edit…`);
    try {
      const result = await legacyRequest<{ status?: string }>(`/projects/${projectId}/editor/history`, { method: "POST", body: JSON.stringify({ action }) });
      setStatus((current) => ({ id: projectId, category: current?.category || "", created_at: current?.created_at || "", status: result.status || "queued" }));
    } catch (error) { setBusy(false); setMessage(error instanceof Error ? error.message : "Could not change edit history."); }
  }

  async function rerenderOptions() {
    setBusy(true); setMessage("Re-rendering with the selected options from saved project assets…");
    try {
      const result = await legacyRequest<{ status?: string; render_options?: RenderOptions }>(`/projects/${projectId}/rerender`, { method: "POST", body: JSON.stringify(renderOptions) });
      if (result.render_options) setRenderOptions(result.render_options);
      setStatus((current) => ({ id: projectId, category: current?.category || "", created_at: current?.created_at || "", status: result.status || "queued" }));
    } catch (error) { setBusy(false); setMessage(error instanceof Error ? error.message : "Could not queue the re-render."); }
  }

  if (!data) return <main className="project-editor-page"><AppNavigation active="/projects" /><section className="projects-content"><Link href="/projects" className="editor-back"><ArrowLeft size={14} strokeWidth={2} aria-hidden /> Projects</Link>{overview ? <>
    <p className="eyebrow">PROJECT OVERVIEW</p>
    <h1>{overview.status.title || `${overview.status.category} production`}</h1>
    <p>Status: <strong>{overview.status.status.replaceAll("_", " ")}</strong>{overview.status.current_stage ? ` · Stage: ${overview.status.current_stage.replaceAll("_", " ")}` : ""}</p>
    {overview.status.error && <p className="projects-error">{overview.status.error}</p>}
    {message && <p role="status">{message}</p>}
    <p><Link className="button button-primary" href={`/projects/${projectId}/plan`}>Production plan</Link>{" "}<Link className="button button-secondary" href={`/projects/${projectId}/certificate`}>Provenance certificate</Link></p>
    {overview.outputs && <section aria-label="Outputs"><h2>Outputs</h2><ul className="overview-outputs">
      {overview.outputs.film.exists && <li>Final film — <a href={apiUrl(overview.outputs.film.media_url || "")}>download MP4</a></li>}
      {overview.outputs.captions.srt.exists && <li>Captions (SRT) — <a href={apiUrl(overview.outputs.captions.srt.media_url || "")}>download</a></li>}
      {overview.outputs.captions.vtt.exists && <li>Captions (VTT) — <a href={apiUrl(overview.outputs.captions.vtt.media_url || "")}>download</a></li>}
      {overview.outputs.asset_report.exists && <li>Asset report — <a href={apiUrl(overview.outputs.asset_report.media_url || "")}>download JSON</a></li>}
      {overview.outputs.rights_manifest.exists && <li>Rights manifest — <a href={apiUrl(overview.outputs.rights_manifest.media_url || "")}>download JSON</a></li>}
      {overview.outputs.run_manifest.exists && <li>Run manifest — <a href={apiUrl(overview.outputs.run_manifest.media_url || "")}>download JSON</a></li>}
      {!overview.outputs.film.exists && !overview.outputs.asset_report.exists && <li>No outputs recorded yet.</li>}
    </ul></section>}
    {overview.runs.length > 0 && <section aria-label="Production history"><h2>Production history</h2><ul className="overview-runs">{overview.runs.map((run) => <li key={run.run_id}><strong>{run.status.replaceAll("_", " ")}</strong> · started {run.created_at ? new Date(run.created_at).toLocaleString() : "—"}{run.finished_at ? ` · finished ${new Date(run.finished_at).toLocaleString()}` : ""}{run.error ? ` · ${run.error}` : ""}</li>)}</ul></section>}
  </> : <p className="projects-state">{message || "Loading project…"}</p>}</section></main>;
  if (!data.timeline) return <main className="project-editor-page"><AppNavigation active="/projects" /><header className="projects-top"><Link href="/projects" className="projects-brand">CINEMORA <span>/ PROJECT</span></Link><Link href="/create" className="projects-create">New production</Link></header><section className="projects-content"><p className="eyebrow">PROJECT PROGRESS</p><h1>Production status</h1><p>{status?.current_stage ? `${status.current_stage.replaceAll("_", " ")} in progress…` : status?.status || "Waiting for production"}</p>{message && <p role="status">{message}</p>}{status?.status === "failed" && <p className="projects-error">{status.error || "Production failed."}</p>}<p><Link className="button button-primary" href={`/projects/${projectId}/plan`}>Production plan</Link>{" "}<Link className="button button-secondary" href={`/projects/${projectId}/certificate`}>Provenance certificate</Link></p></section></main>;
  const videoUrl = status?.video_url ? assetUrl(status.video_url) : assetUrl(data.video_url || undefined);

  return <main className="project-editor-page">
    <AppNavigation active="/projects" />
    <header className="editor-topbar"><div><Link href="/projects" className="editor-back"><ArrowLeft size={14} strokeWidth={2} aria-hidden /> Projects</Link><span className="editor-project-name">{data.project.category} production</span><small>Scene visuals · {timecode(data.timeline.duration_seconds)}</small></div><div className="editor-top-actions"><Link className="button button-secondary" href={`/projects/${projectId}/plan`}>Plan review</Link><button type="button" disabled={busy || isRendering} onClick={() => void changeHistory("undo")}>Undo</button><button type="button" disabled={busy || isRendering} onClick={() => void changeHistory("redo")}>Redo</button><button type="button" disabled={busy || isRendering} onClick={() => void changeHistory("revert")}>Revert</button>{data.video_url && <a className="editor-download" href={apiUrl(data.video_url)}>Download MP4</a>}<button className="editor-export" disabled={busy || isRendering || (!removed.length && !additions.length)} onClick={() => void saveEdit()}>{isRendering ? "Rendering edit…" : "Save & render export"}</button></div></header>
    {message && <p className="editor-message" role="status">{message}</p>}
    {status?.status === "failed" && <p className="editor-error" role="alert">{status.error || message}</p>}
    <div className="project-editor-layout">
      <aside className="editor-media-panel"><div className="editor-section-head"><div><span>PROJECT MEDIA</span><h2>Footage</h2></div><label className="editor-upload"><Plus size={14} strokeWidth={2} aria-hidden /> Upload<input type="file" accept="video/mp4,video/quicktime,video/webm,video/x-matroska" onChange={(event) => void uploadAsset(event.target.files?.[0])} disabled={busy} /></label></div><p>Project-sourced clips and owner-supplied video.</p><div className="owner-upload-review"><label><input type="checkbox" checked={rightsConfirmed} onChange={(event) => setRightsConfirmed(event.target.checked)} /> I have the right to use this footage.</label><label>Match type<select value={matchQuality} onChange={(event) => setMatchQuality(event.target.value as "direct" | "illustrative")}><option value="direct">This shows the event</option><option value="illustrative">Illustrative</option></select></label><small>Technical validation runs on upload. Vision safety review is not configured; every new source therefore needs manual safety and privacy review before it enters the candidate pool.</small>{pendingAssetId && <div className="owner-upload-review"><strong>Source-level review</strong><label><input type="checkbox" checked={safetyReviewed} onChange={(event) => setSafetyReviewed(event.target.checked)} /> I reviewed this source for graphic content and approve its safety.</label><label><input type="checkbox" checked={privacyReviewed} onChange={(event) => setPrivacyReviewed(event.target.checked)} /> I reviewed this source for minors and privacy concerns.</label><button type="button" disabled={busy || !safetyReviewed || !privacyReviewed} onClick={() => void reviewOwnerAsset()}>Approve this source</button></div>}</div><button type="button" disabled={busy || isRendering || !data.timeline} onClick={() => void refreshCandidates()}>Rank candidates for scene {sceneIndex + 1}</button>{(candidatePool.scenes.find((scene) => scene.scene_index === sceneIndex)?.candidates || []).map((candidate) => <button type="button" key={`ranked:${candidate.asset_id}`} className={`editor-asset ${assetChoice === candidate.asset_id ? "selected" : ""}`} onClick={() => setAssetChoice(candidate.asset_id)}><strong>{candidate.title}</strong><small>Recorded-term overlap {Math.round(candidate.score * 100)}% · {candidate.matched_terms.join(", ")} · relevance not verified</small></button>)}{/* TODO(Brief Stage 5): serve real asset thumbnails via the media endpoint here; the Video icon is a neutral asset-type placeholder, not a thumbnail. */}<div className="editor-assets">{data.assets.map((asset) => <button type="button" key={asset.asset_id} className={`editor-asset ${assetChoice === asset.asset_id ? "selected" : ""}`} onClick={() => setAssetChoice(asset.asset_id)}><span>{asset.type === "image" ? <img src={assetUrl(asset.preview_url)} alt="" /> : <Video size={20} strokeWidth={1.7} aria-hidden />}</span><strong>{asset.title}</strong><small>{asset.type} · {asset.source.replaceAll("_", " ")}{asset.rights_tier ? ` · ${asset.rights_tier}` : ""}</small></button>)}{!data.assets.length && <small>No reusable footage found. Review and upload a video.</small>}</div></aside>
      <section className="editor-main"><div className="editor-preview-wrap">{activeClip?.type === "image" && activeClip.preview_url ? <img className="editor-preview" src={assetUrl(activeClip.preview_url)} alt={activeClip.title || "Selected footage"} /> : activeClip?.preview_url ? <video className="editor-preview" controls preload="metadata" src={assetUrl(activeClip.preview_url)} /> : videoUrl ? <video className="editor-preview" controls preload="metadata" src={videoUrl} /> : <div className="editor-empty-preview"><span className="reel-glyph" /><strong>No preview available yet</strong><small>Choose a scene asset or wait for the first export.</small></div>}<div className="editor-preview-label">{activeClip?.title || `Scene ${sceneIndex + 1}`} · {activeClip?.sentence || "Selected project visual"}</div></div>
<div className="editor-timeline-heading"><div><span>EDIT TIMELINE</span><h2>Visual scenes</h2></div><label>Video source in-point (seconds)<input aria-label="Video source in-point seconds" type="number" min="0" step="0.1" value={sourceInPoint} onChange={(event) => setSourceInPoint(Math.max(0, Number(event.target.value) || 0))} /></label><button disabled={!assetChoice || busy || isRendering} onClick={() => { setAdditions((current) => [...current, { scene_index: sceneIndex, asset_id: assetChoice, clip_range_start: sourceInPoint }]); setMessage("Footage added to the selected scene. Save and render to apply it to your export."); }}>Add selected footage to scene</button></div>
        <div className="editor-scenes">{pendingScenes.map((scene, index) => <section className={`editor-scene ${sceneIndex === index ? "active" : ""}`} key={`${scene.scene_number}-${index}`} onClick={() => setSceneIndex(index)}><div className="editor-scene-head"><button type="button" onClick={(event) => { event.stopPropagation(); setSceneIndex(index); }}><strong>Scene {scene.scene_number}</strong><small>{timecode(scene.start)}–{timecode(scene.end)} · {scene.clips.length} visuals</small></button><button type="button" disabled={!assetChoice || busy || isRendering} onClick={(event) => { event.stopPropagation(); setSceneIndex(index); const asset = data.assets.find((item) => item.asset_id === assetChoice); setAdditions((current) => [...current, { scene_index: index, asset_id: assetChoice, clip_range_start: sourceInPoint }]); if (asset) setSelectedClipId(`new:${index}:${asset.asset_id}`); }}><Plus size={14} strokeWidth={2} aria-hidden /> Add footage</button></div><div className="editor-clip-row">{scene.clips.map((clip) => <article className="editor-clip" key={clip.editor_id}><button type="button" className="editor-clip-preview" onClick={(event) => { event.stopPropagation(); setSceneIndex(index); setSelectedClipId(clip.editor_id); }}>{clip.type === "image" ? <img src={assetUrl(clip.preview_url)} alt="" /> : <span>VIDEO</span>}<strong>{clip.title || clip.sentence || "Footage clip"}</strong></button><small>{timecode(clip.start)}–{timecode(clip.end)}</small><button type="button" className="editor-remove" onClick={(event) => { event.stopPropagation(); setSceneIndex(index); removeClip(clip, index); }}>Remove</button></article>)}</div></section>)}</div>
      </section>
      <aside className="editor-inspector"><span>EXPORT</span><h2>Version {currentVersion}</h2><p>{isRendering ? "Your edits are rendering. The existing export stays available until the new one passes validation." : "Saving changes renders a new MP4 with the existing narration and captions."}</p>{videoUrl ? <><a href={videoUrl} target="_blank" rel="noreferrer" className="editor-preview-link">Preview current MP4</a><a href={apiUrl(data.video_url || "")} className="editor-download wide">Download finished MP4</a></> : <p className="editor-rights-note">A finished MP4 will be available here after production completes.</p>}<section className="editor-source-details" aria-label="Render options"><h3>Re-render options</h3><label><input type="checkbox" checked={renderOptions.graphics} onChange={(event) => setRenderOptions((value) => ({ ...value, graphics: event.target.checked }))} /> Motion graphics</label><label><input type="checkbox" checked={renderOptions.music} onChange={(event) => setRenderOptions((value) => ({ ...value, music: event.target.checked }))} /> Music</label>{renderOptions.music && <label>Music volume {renderOptions.music_percent}%<input type="range" min="0" max="100" value={renderOptions.music_percent} onChange={(event) => setRenderOptions((value) => ({ ...value, music_percent: Number(event.target.value) }))} /></label>}<label><input type="checkbox" checked={renderOptions.captions} onChange={(event) => setRenderOptions((value) => ({ ...value, captions: event.target.checked }))} /> Captions</label><button type="button" disabled={busy || isRendering} onClick={() => void rerenderOptions()}>Save options & re-render</button><small>Uses the saved timeline, source assets and narration; discovery and voice generation are not rerun.</small></section><section className="editor-source-details" aria-label="Render versions"><h3>Render versions</h3>{versions.length ? versions.map((version) => <p key={version.version}><a href={assetUrl(version.url)} target="_blank" rel="noreferrer">v{version.version}{version.version === currentVersion ? " · current" : ""}</a> · {new Date(version.created_at).toLocaleString()}</p>) : <small>No version history is recorded yet.</small>}</section>{activeClip && <section className="editor-source-details" aria-label="Selected footage source"><h3>Selected footage source</h3><p>{(activeClip.provider || activeClip.source || "Source").replaceAll("_", " ")}{activeClip.rights_status ? ` · ${(activeClip.rights_status === "unverified_user_accepted" ? "license not checked" : activeClip.rights_status.replaceAll("_", " "))}` : ""}</p>{activeClip.attribution_required && activeClip.attribution_text && <p>Credit: {activeClip.attribution_text}</p>}{activeClip.creator && !activeClip.attribution_text && <p>Creator: {activeClip.creator}</p>}<div>{activeClip.source_url && <a href={activeClip.source_url} target="_blank" rel="noreferrer">View source</a>}{activeClip.license_url && <a href={activeClip.license_url} target="_blank" rel="noreferrer">{activeClip.license_name || "View license"}</a>}</div></section>}<p className="editor-rights-note">Removing a shot affects the visual timeline only; the narration and caption timing are preserved.</p></aside>
    </div>
  </main>;
}
