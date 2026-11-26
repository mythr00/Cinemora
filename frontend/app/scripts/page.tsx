"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { EmptyState, ErrorState, LoadingState } from "@/components/states";
import {
  attachScriptToProject,
  createScriptDraft,
  deleteScriptDraft,
  expandScriptSections,
  generateScriptDraft,
  getScriptDraft,
  listProjects,
  listScriptDrafts,
  listScriptRevisions,
  restoreScriptRevision,
  runScriptAnalysis,
  runScriptFactCheck,
  sendScriptToProduction,
  tightenScriptPassage,
  updateScriptDraft,
  type ProjectSummary,
  type ScriptDraft,
  type ScriptExpandProposal,
  type ScriptRevision,
  type ScriptTightenProposal,
} from "@/lib/api";
import {
  Clapperboard,
  FileText,
  History,
  Link2,
  Plus,
  Save,
  ShieldCheck,
  Sparkles,
  Trash2,
  Wand2,
} from "lucide-react";

/**
 * YouTube Script Studio (founder priority #12) — the writing
 * workspace that feeds productions. Drafts are sectioned documents
 * (heading + narration body); the production script is the bodies
 * joined by blank lines, headings are structure and are never
 * narrated. Metrics are arithmetic at the platform's recorded
 * ~145 words per minute. AI actions return proposals: nothing the
 * model writes replaces the author's words until the author
 * applies it. Analysis is the production pipeline's own script
 * analysis; fact check is its own fact extraction + KB
 * corroboration, with every fact's basis shown.
 */

type SectionDraft = { id?: string; heading: string; body: string };

function toSectionDrafts(draft: ScriptDraft): SectionDraft[] {
  return (draft.sections || []).map((s) => ({
    id: s.id,
    heading: s.heading,
    body: s.body,
  }));
}

/** Duration presets (founder directive): the standard lengths
 * offered before generation; Custom accepts any 5–60 value. */
const DURATION_PRESETS = [5, 10, 15, 20, 25, 30, 40, 45, 60];

export default function ScriptStudioPage() {
  const [drafts, setDrafts] = useState<ScriptDraft[]>([]);
  const [draft, setDraft] = useState<ScriptDraft | null>(null);
  const [sections, setSections] = useState<SectionDraft[]>([]);
  const [revisions, setRevisions] = useState<ScriptRevision[]>([]);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [attachTarget, setAttachTarget] = useState("");
  const [busy, setBusy] = useState(true);
  const [working, setWorking] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState("");
  const [expandProposal, setExpandProposal] = useState<ScriptExpandProposal | null>(null);
  const [tightenProposal, setTightenProposal] = useState<ScriptTightenProposal | null>(null);
  const [brief, setBrief] = useState({ topic: "", angle: "", category: "", target: "10" });

  const loadDrafts = useCallback(async () => {
    const data = await listScriptDrafts();
    setDrafts(data.scripts || []);
    return data.scripts || [];
  }, []);

  const openDraft = useCallback(async (scriptId: string) => {
    const full = await getScriptDraft(scriptId);
    setDraft(full);
    setSections(toSectionDrafts(full));
    setExpandProposal(null);
    setTightenProposal(null);
    const revs = await listScriptRevisions(scriptId);
    setRevisions(revs.revisions || []);
  }, []);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [list, projectList] = await Promise.all([
        loadDrafts(),
        listProjects().catch(() => ({ projects: [] as ProjectSummary[] })),
      ]);
      setProjects(projectList.projects || []);
      if (list.length > 0) await openDraft(list[0].id);
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }, [loadDrafts, openDraft]);

  useEffect(() => {
    const timer = setTimeout(() => { void load(); }, 0);
    return () => clearTimeout(timer);
  }, [load]);

  const run = async (label: string, action: () => Promise<void>) => {
    setWorking(label);
    setError(null);
    setNotice("");
    try {
      await action();
    } catch (caught) {
      setError(caught);
    } finally {
      setWorking("");
    }
  };

  const createBlank = () =>
    run("create", async () => {
      const created = await createScriptDraft({
        title: "Untitled script",
        sections: [{ heading: "Opening", body: "" }],
      });
      await loadDrafts();
      await openDraft(created.id);
      setNotice("New draft created.");
    });

  const save = () =>
    run("save", async () => {
      if (!draft) return;
      const saved = await updateScriptDraft(draft.id, {
        title: draft.title,
        video_title: draft.video_title || undefined,
        clear_video_title: !draft.video_title,
        category: draft.category,
        target_minutes: draft.target_minutes,
        sections,
      });
      setDraft(saved);
      setSections(toSectionDrafts(saved));
      const revs = await listScriptRevisions(draft.id);
      setRevisions(revs.revisions || []);
      await loadDrafts();
      setNotice("Draft saved. The previous state was snapshotted under Revisions.");
    });

  const remove = () =>
    run("delete", async () => {
      if (!draft) return;
      await deleteScriptDraft(draft.id);
      setDraft(null);
      setSections([]);
      const list = await loadDrafts();
      if (list.length > 0) await openDraft(list[0].id);
      setNotice("Draft deleted.");
    });

  const generate = () =>
    run("generate", async () => {
      const created = await generateScriptDraft({
        topic: brief.topic,
        angle: brief.angle || undefined,
        category: brief.category || undefined,
        target_minutes: brief.target ? Number(brief.target) : undefined,
      });
      await loadDrafts();
      await openDraft(created.id);
      setNotice(
        "Draft generated from your brief — an outline first, then one writing pass per section. Edit freely; nothing here is final until you send it to production.",
      );
    });

  const expand = () =>
    run("expand", async () => {
      if (!draft) return;
      const proposal = await expandScriptSections(draft.id);
      setExpandProposal(proposal);
    });

  const applyExpand = () =>
    run("apply-expand", async () => {
      if (!draft || !expandProposal) return;
      const mapped = expandProposal.proposal.sections.map((s) => ({
        id: s.id,
        heading: s.heading,
        body: s.body,
      }));
      const saved = await updateScriptDraft(draft.id, { sections: mapped });
      setDraft(saved);
      setSections(toSectionDrafts(saved));
      setExpandProposal(null);
      await loadDrafts();
      setNotice("Expansion applied. The previous text was snapshotted under Revisions.");
    });

  const tighten = (section: SectionDraft) =>
    run(`tighten-${section.id}`, async () => {
      if (!draft || !section.id) return;
      const proposal = await tightenScriptPassage(draft.id, {
        section_id: section.id,
        text: section.body,
      });
      setTightenProposal(proposal);
    });

  const applyTighten = () =>
    run("apply-tighten", async () => {
      if (!draft || !tightenProposal) return;
      const next = sections.map((s) =>
        s.id === tightenProposal.proposal.section_id
          ? { ...s, body: tightenProposal.proposal.tightened }
          : s,
      );
      const saved = await updateScriptDraft(draft.id, { sections: next });
      setDraft(saved);
      setSections(toSectionDrafts(saved));
      setTightenProposal(null);
      await loadDrafts();
      setNotice("Tightened text applied. The previous text was snapshotted under Revisions.");
    });

  const analyse = () =>
    run("analysis", async () => {
      if (!draft) return;
      const result = await runScriptAnalysis(draft.id);
      setDraft({ ...draft, analysis: result.analysis, has_analysis: true });
      setNotice("Analysis complete — this is the same analysis a production runs.");
    });

  const factCheck = () =>
    run("fact-check", async () => {
      if (!draft) return;
      const result = await runScriptFactCheck(draft.id);
      setDraft({ ...draft, fact_check: result.fact_check, has_fact_check: true });
      setNotice("Fact check complete. Facts stay 'script claim' until a verified source supports them.");
    });

  const restore = (revision: ScriptRevision) =>
    run(`restore-${revision.id}`, async () => {
      if (!draft) return;
      const restored = await restoreScriptRevision(draft.id, revision.id);
      setDraft(restored);
      setSections(toSectionDrafts(restored));
      const revs = await listScriptRevisions(draft.id);
      setRevisions(revs.revisions || []);
      setNotice("Revision restored. The state before restoring was snapshotted too.");
    });

  const sendToProduction = () =>
    run("send", async () => {
      if (!draft) return;
      const result = await sendScriptToProduction(draft.id);
      setDraft(result.draft);
      await loadDrafts();
      setNotice(`Project created. Open it from the production panel below.`);
    });

  const attach = () =>
    run("attach", async () => {
      if (!draft || !attachTarget) return;
      const result = await attachScriptToProject(draft.id, attachTarget);
      setDraft(result.draft);
      setNotice("Script written onto the selected project (snapshotted in its Versions).");
    });

  const metrics = draft?.metrics;
  const liveWords = sections.reduce(
    (sum, s) => sum + (s.body.trim() ? s.body.trim().split(/\s+/).length : 0),
    0,
  );

  return (
    <main className="app-page script-page">
      <AppNavigation />
      <section className="script-content">
        <p className="eyebrow">YOUTUBE SCRIPT STUDIO</p>
        <h1>Script Studio</h1>
        <p className="script-intro">
          Write the film before you make it — drafts are sections, produced in order.
        </p>

        {busy && <LoadingState className="script-message" label="Loading your drafts…" />}
        {error ? <ErrorState error={error} onRetry={() => void load()} /> : null}
        {notice && <p className="script-message">{notice}</p>}

        <div className="script-layout">
          <aside className="script-sidebar">
            <div className="script-sidebar-head">
              <h2>Drafts</h2>
              <button type="button" className="button button-primary" onClick={() => void createBlank()} disabled={working === "create"}>
                <Plus size={15} /> New draft
              </button>
            </div>
            {drafts.length === 0 && !busy && (
              <EmptyState
                eyebrow="No drafts"
                title="No script drafts yet"
                description="Create a blank draft, or generate one from a brief below."
              />
            )}
            <div className="script-draft-list">
              {drafts.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className={`script-draft-row${draft?.id === item.id ? " script-draft-row--active" : ""}`}
                  onClick={() => void run("open", () => openDraft(item.id))}
                >
                  <strong>{item.title}</strong>
                  <span>
                    {item.metrics.word_count} words · ~{item.metrics.estimated_minutes} min
                    {item.project_id ? " · in production" : ""}
                  </span>
                </button>
              ))}
            </div>

            <div className="panel script-panel">
              <h2><Sparkles size={16} /> Draft from a brief</h2>
              <p className="script-note">
                The AI designs an outline with per-section word budgets for
                your target length (5–60 minutes), then writes one section
                at a time. The result is a new draft — your existing drafts
                are never touched. If no AI provider is configured on this
                deployment, this reports unavailable instead of inventing a
                script.
              </p>
              <div className="script-form script-form-stack">
                <label className="script-field">
                  Topic or brief
                  <input
                    value={brief.topic}
                    onChange={(e) => setBrief({ ...brief, topic: e.target.value })}
                    placeholder="The 1981 Hyatt Regency walkway collapse"
                  />
                </label>
                <label className="script-field">
                  Angle (optional)
                  <input
                    value={brief.angle}
                    onChange={(e) => setBrief({ ...brief, angle: e.target.value })}
                    placeholder="The design change that doubled the load"
                  />
                </label>
                <label className="script-field">
                  Category (optional)
                  <input
                    value={brief.category}
                    onChange={(e) => setBrief({ ...brief, category: e.target.value })}
                    placeholder="engineering"
                  />
                </label>
                <div className="script-field">
                  <span>Duration</span>
                  <div className="script-duration-presets" role="group" aria-label="Duration presets">
                    {DURATION_PRESETS.map((minutes) => (
                      <button
                        key={minutes}
                        type="button"
                        className={
                          brief.target === String(minutes)
                            ? "script-duration-chip is-active"
                            : "script-duration-chip"
                        }
                        onClick={() => setBrief({ ...brief, target: String(minutes) })}
                      >
                        {minutes} min
                      </button>
                    ))}
                  </div>
                  <input
                    type="number"
                    min={5}
                    max={60}
                    aria-label="Custom duration in minutes"
                    placeholder="Custom minutes (5–60)"
                    value={
                      DURATION_PRESETS.includes(Number(brief.target))
                        ? ""
                        : brief.target
                    }
                    onChange={(e) => setBrief({ ...brief, target: e.target.value })}
                  />
                </div>
                <button
                  type="button"
                  className="button button-primary"
                  onClick={() => void generate()}
                  disabled={working === "generate" || !brief.topic.trim()}
                >
                  {working === "generate" ? "Generating…" : "Generate draft"}
                </button>
              </div>
            </div>
          </aside>

          <div className="script-main">
            {!draft && !busy && (
              <EmptyState
                eyebrow="Script Studio"
                title="Pick a draft to start writing"
                description="Your drafts live here, versioned on every save."
              />
            )}
            {draft && (
              <>
                <div className="script-summary">
                  <div><span>Words</span><strong>{liveWords}</strong></div>
                  <div><span>Est. runtime</span><strong>~{Math.round((liveWords / 145) * 10) / 10} min</strong></div>
                  <div><span>Sentences</span><strong>{metrics?.sentence_count ?? 0}</strong></div>
                  <div>
                    <span>Target</span>
                    <strong>
                      {metrics?.target_words
                        ? `${metrics.target_words} words (${metrics.words_vs_target && metrics.words_vs_target > 0 ? "+" : ""}${metrics.words_vs_target ?? 0})`
                        : "Not set"}
                    </strong>
                  </div>
                </div>

                <div className="panel script-panel">
                  <h2><FileText size={16} /> The draft</h2>
                  <div className="script-form script-form-stack">
                    <label className="script-field">
                      Working title
                      <input
                        value={draft.title}
                        onChange={(e) => setDraft({ ...draft, title: e.target.value })}
                      />
                    </label>
                    <label className="script-field">
                      Video title
                      <input
                        value={draft.video_title ?? ""}
                        placeholder="Generated with the script — edit freely"
                        onChange={(e) =>
                          setDraft({ ...draft, video_title: e.target.value })
                        }
                      />
                      <span className="script-note">
                        The documentary&apos;s public title. It is saved with the
                        draft and becomes the production&apos;s title — it is
                        never part of the narration.
                      </span>
                    </label>
                    <div className="script-form">
                      <label className="script-field">
                        Category
                        <input
                          value={draft.category}
                          onChange={(e) => setDraft({ ...draft, category: e.target.value })}
                        />
                      </label>
                      <label className="script-field">
                        Target minutes (optional)
                        <input
                          type="number"
                          min={1}
                          value={draft.target_minutes ?? ""}
                          onChange={(e) =>
                            setDraft({
                              ...draft,
                              target_minutes: e.target.value ? Number(e.target.value) : null,
                            })
                          }
                        />
                      </label>
                    </div>
                  </div>

                  {sections.map((section, index) => (
                    <div key={section.id || index} className="script-section">
                      <div className="script-section-head">
                        <input
                          className="script-section-heading"
                          value={section.heading}
                          placeholder={`Section ${index + 1} heading`}
                          onChange={(e) => {
                            const next = [...sections];
                            next[index] = { ...next[index], heading: e.target.value };
                            setSections(next);
                          }}
                        />
                        <span className="script-section-meta">
                          {section.body.trim() ? section.body.trim().split(/\s+/).length : 0} words
                        </span>
                        <button
                          type="button"
                          className="button button-quiet"
                          onClick={() => void tighten(section)}
                          disabled={!section.id || working === `tighten-${section.id}` || !section.body.trim()}
                        >
                          <Wand2 size={14} /> Tighten
                        </button>
                        <button
                          type="button"
                          className="button button-quiet"
                          onClick={() => setSections(sections.filter((_, i) => i !== index))}
                          disabled={sections.length <= 1}
                        >
                          <Trash2 size={14} /> Remove
                        </button>
                      </div>
                      <textarea
                        className="script-section-body"
                        value={section.body}
                        rows={8}
                        placeholder="Narration for this section…"
                        onChange={(e) => {
                          const next = [...sections];
                          next[index] = { ...next[index], body: e.target.value };
                          setSections(next);
                        }}
                      />
                      {tightenProposal && tightenProposal.proposal.section_id === section.id && (
                        <div className="script-proposal">
                          <p className="script-note">
                            Tightened proposal ({tightenProposal.proposal.original_words} →{" "}
                            {tightenProposal.proposal.tightened_words} words). Your text is
                            unchanged until you apply it.
                          </p>
                          <p className="script-proposal-text">{tightenProposal.proposal.tightened}</p>
                          <div className="script-form">
                            <button type="button" className="button button-primary" onClick={() => void applyTighten()}>
                              Apply tightened text
                            </button>
                            <button type="button" className="button button-secondary" onClick={() => setTightenProposal(null)}>
                              Discard
                            </button>
                          </div>
                        </div>
                      )}
                    </div>
                  ))}

                  <div className="script-form">
                    <button
                      type="button"
                      className="button button-secondary"
                      onClick={() =>
                        setSections([...sections, { heading: "", body: "" }])
                      }
                    >
                      <Plus size={14} /> Add section
                    </button>
                    <button
                      type="button"
                      className="button button-secondary"
                      onClick={() => void expand()}
                      disabled={working === "expand"}
                    >
                      <Sparkles size={14} /> {working === "expand" ? "Expanding…" : "Expand sections with AI"}
                    </button>
                    <button type="button" className="button button-primary" onClick={() => void save()} disabled={working === "save"}>
                      <Save size={14} /> {working === "save" ? "Saving…" : "Save draft"}
                    </button>
                    <button type="button" className="button button-danger" onClick={() => void remove()} disabled={working === "delete"}>
                      Delete draft
                    </button>
                  </div>

                  {expandProposal && (
                    <div className="script-proposal">
                      <h3>Expansion proposal</h3>
                      <p className="script-note">
                        Generated per section toward the draft&apos;s target. Your saved
                        draft is unchanged until you apply this.
                      </p>
                      {expandProposal.proposal.sections.map((s) => (
                        <div key={s.id} className="script-proposal-section">
                          <strong>{s.heading || "Untitled section"}</strong>
                          <span className="script-section-meta">budget {s.word_budget} words</span>
                          <p className="script-proposal-text">{s.body}</p>
                        </div>
                      ))}
                      <div className="script-form">
                        <button type="button" className="button button-primary" onClick={() => void applyExpand()}>
                          Apply expansion
                        </button>
                        <button type="button" className="button button-secondary" onClick={() => setExpandProposal(null)}>
                          Discard
                        </button>
                      </div>
                    </div>
                  )}
                </div>

                <div className="panel script-panel">
                  <h2><ShieldCheck size={16} /> Analysis &amp; fact check</h2>
                  <p className="script-note">
                    Analysis runs the production pipeline&apos;s own script
                    analysis — sentence roles, derived scenes and the Case
                    Bible entities. Fact check extracts the script&apos;s
                    dated events and quantities with the pipeline&apos;s fact
                    machinery: every fact starts as a <em>script claim</em>{" "}
                    and is only upgraded when a verified knowledge-base
                    record supports it.
                  </p>
                  <div className="script-form">
                    <button type="button" className="button button-secondary" onClick={() => void analyse()} disabled={working === "analysis"}>
                      {working === "analysis" ? "Analysing…" : "Run analysis"}
                    </button>
                    <button type="button" className="button button-secondary" onClick={() => void factCheck()} disabled={working === "fact-check"}>
                      {working === "fact-check" ? "Checking…" : "Run fact check"}
                    </button>
                  </div>
                  {draft.analysis && (
                    <div className="script-analysis">
                      <p className="script-meta">
                        Mode: <strong>{draft.analysis.mode}</strong>
                        {draft.analysis.mode === "degraded"
                          ? " (no AI provider configured — the pipeline's heuristic planner; a production with a provider configured runs the full planner)"
                          : ""}{" "}
                        · {draft.analysis.sentence_count} sentences ·{" "}
                        {draft.analysis.scenes.length} scenes · analysed{" "}
                        {new Date(draft.analysis.at).toLocaleString()}
                      </p>
                      <h3>Scenes the pipeline derives</h3>
                      <ul className="script-list">
                        {draft.analysis.scenes.map((scene) => (
                          <li key={scene.scene_number}>
                            Scene {scene.scene_number}: sentences{" "}
                            {scene.sentence_numbers.filter(Boolean).join(", ") || "—"}
                          </li>
                        ))}
                      </ul>
                      <h3>Entities (Case Bible)</h3>
                      <p className="script-meta">
                        {draft.analysis.bible.entities.length
                          ? draft.analysis.bible.entities
                              .map((e) => `${e.canonical} (${e.type})`)
                              .join(" · ")
                          : "None recorded."}
                      </p>
                      <h3>Sentences &amp; roles</h3>
                      <ul className="script-list">
                        {draft.analysis.sentences.map((s) => (
                          <li key={s.sentence_number}>
                            <strong>{s.sentence_number}.</strong>{" "}
                            <span className="script-role">{s.role || "unclassified"}</span>{" "}
                            {s.text}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {draft.fact_check && (
                    <div className="script-analysis">
                      <p className="script-meta">
                        {draft.fact_check.record.facts} facts extracted (
                        {draft.fact_check.record.dated_events} dated events,{" "}
                        {draft.fact_check.record.quantities} quantities) ·{" "}
                        {draft.fact_check.corroboration.checked} checked against the
                        knowledge base · {draft.fact_check.corroboration.corroborated}{" "}
                        corroborated · checked{" "}
                        {new Date(draft.fact_check.at).toLocaleString()}
                      </p>
                      <ul className="script-list">
                        {draft.fact_check.facts.map((fact) => (
                          <li key={fact.fact_id}>
                            <span className={`status-badge ${fact.basis === "research_supported" ? "status-badge--success" : "status-badge--neutral"}`}>
                              {fact.basis === "research_supported" ? "research supported" : "script claim"}
                            </span>{" "}
                            {fact.kind === "dated_event"
                              ? `${fact.when}: ${fact.what}`
                              : `${fact.kind} — ${fact.display || fact.what || ""}`}{" "}
                            <span className="script-meta">(sentence {fact.sentence_number})</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>

                <div className="panel script-panel">
                  <h2><History size={16} /> Revisions</h2>
                  <p className="script-note">
                    Every save snapshots the previous state of the draft.
                    Restoring a revision snapshots the current state first,
                    so a restore can itself be undone.
                  </p>
                  {revisions.length === 0 && (
                    <p className="script-note">No revisions yet — the first save creates one.</p>
                  )}
                  <div className="script-list-rows">
                    {revisions.map((revision) => (
                      <div key={revision.id} className="script-row">
                        <div className="script-row-main">
                          <strong>{new Date(revision.at).toLocaleString()}</strong>
                          <span>
                            {revision.metrics.word_count} words · via {revision.source}
                          </span>
                        </div>
                        <button
                          type="button"
                          className="button button-secondary"
                          onClick={() => void restore(revision)}
                          disabled={working === `restore-${revision.id}`}
                        >
                          Restore
                        </button>
                      </div>
                    ))}
                  </div>
                </div>

                <div className="panel script-panel">
                  <h2><Clapperboard size={16} /> Production</h2>
                  <p className="script-note">
                    Sending to production creates a real project with this
                    script (section headings are structure and are not
                    narrated) through the normal project creation path.
                    Attaching writes the script onto one of your existing
                    projects instead, snapshotted in that project&apos;s
                    Versions.
                  </p>
                  {draft.project_id && (
                    <p className="script-meta">
                      Linked project:{" "}
                      <Link className="text-link" href={`/projects/${draft.project_id}`}>
                        Open project
                      </Link>{" "}
                      · {draft.productions.length} production link(s) recorded
                    </p>
                  )}
                  <div className="script-form">
                    <button type="button" className="button button-primary" onClick={() => void sendToProduction()} disabled={working === "send"}>
                      {working === "send" ? "Creating project…" : "Send to production"}
                    </button>
                    <label className="script-field script-attach-field">
                      Attach to an existing project
                      <select
                        value={attachTarget}
                        onChange={(e) => setAttachTarget(e.target.value)}
                      >
                        <option value="">Choose a project…</option>
                        {projects.map((p) => (
                          <option key={p.id} value={p.id}>
                            {p.title || p.id}
                          </option>
                        ))}
                      </select>
                    </label>
                    <button
                      type="button"
                      className="button button-secondary"
                      onClick={() => void attach()}
                      disabled={working === "attach" || !attachTarget}
                    >
                      <Link2 size={14} /> Attach script
                    </button>
                  </div>
                </div>
              </>
            )}
          </div>
        </div>
      </section>
    </main>
  );
}
