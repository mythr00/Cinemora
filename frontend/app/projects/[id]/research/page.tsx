"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { ProjectTabs } from "@/components/project-tabs";
import {
  AlertTriangle,
  ArrowLeft,
  BookMarked,
  CheckCircle2,
  Library,
  Plus,
  Search,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import {
  ApiClientError,
  checkResearchEntity,
  createResearchCitation,
  createResearchCorrection,
  createResearchSession,
  createResearchSummary,
  createResearchTimelineEvent,
  deleteResearchCitation,
  deleteResearchCorrection,
  deleteResearchTimelineEvent,
  getResearchBibliography,
  getResearchBible,
  getResearchOverview,
  listResearchCitations,
  listResearchSources,
  listResearchSummaries,
  runResearchFactCheck,
  setResearchSourceState,
  type ResearchBible,
  type ResearchCitation,
  type ResearchEntity,
  type ResearchOverview,
  type ResearchSource,
  type ResearchSourceState,
  type ResearchSummary,
} from "@/lib/api";

/**
 * Research Studio (Phase D, founder priority #6): the workspace
 * over the research engine. Everything shown is a recorded
 * artefact — sessions and their sources, the Case Bible exactly as
 * analysis recorded it (user corrections shown as an overlay,
 * never merged silently), script facts with their basis stamps,
 * KB verdicts beside bible types when they disagree, citations
 * tying claims to sources, and gaps derived from the recorded
 * data with the derivation stated. Controls exist only where a
 * real endpoint backs them.
 */

const ENTITY_TYPES = ["person", "place", "organization", "event", "object", "other"];

function tierLabel(tier: string) {
  return tier.replaceAll("_", " ");
}

export default function ResearchStudioPage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;

  const [overview, setOverview] = useState<ResearchOverview | null>(null);
  const [bible, setBible] = useState<ResearchBible | null>(null);
  const [sources, setSources] = useState<ResearchSource[]>([]);
  const [sourceCounts, setSourceCounts] = useState<Record<string, number>>({});
  const [citations, setCitations] = useState<ResearchCitation[]>([]);
  const [bibliography, setBibliography] = useState("");
  const [summaries, setSummaries] = useState<ResearchSummary[]>([]);

  const [busy, setBusy] = useState(true);
  const [acting, setActing] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const [topic, setTopic] = useState("");
  const [sourceFilter, setSourceFilter] = useState<string>("");
  const [sourceQuery, setSourceQuery] = useState("");
  const [typeEdits, setTypeEdits] = useState<Record<string, string>>({});
  const [nameEdits, setNameEdits] = useState<Record<string, string>>({});
  const [eventWhen, setEventWhen] = useState("");
  const [eventWhat, setEventWhat] = useState("");
  const [eventSource, setEventSource] = useState("");
  const [citeKind, setCiteKind] = useState("fact");
  const [citeFact, setCiteFact] = useState("");
  const [citeLabel, setCiteLabel] = useState("");
  const [citeSource, setCiteSource] = useState("");
  const [citeExternalTitle, setCiteExternalTitle] = useState("");
  const [citeExternalUrl, setCiteExternalUrl] = useState("");

  const load = useCallback(async () => {
    setError("");
    const [overviewR, bibleR, sourcesR, citationsR, bibR, summariesR] =
      await Promise.allSettled([
        getResearchOverview(projectId),
        getResearchBible(projectId),
        listResearchSources(projectId, {
          state: sourceFilter || undefined,
          q: sourceQuery || undefined,
        }),
        listResearchCitations(projectId),
        getResearchBibliography(projectId),
        listResearchSummaries(projectId),
      ]);
    if (overviewR.status === "fulfilled") setOverview(overviewR.value);
    if (bibleR.status === "fulfilled") setBible(bibleR.value);
    if (sourcesR.status === "fulfilled") {
      setSources(sourcesR.value.sources);
      setSourceCounts(sourcesR.value.counts);
    }
    if (citationsR.status === "fulfilled") setCitations(citationsR.value.citations);
    if (bibR.status === "fulfilled") setBibliography(bibR.value.text);
    if (summariesR.status === "fulfilled") setSummaries(summariesR.value.summaries);
    if (overviewR.status === "rejected") {
      setError(
        overviewR.reason instanceof Error
          ? overviewR.reason.message
          : "The research workspace could not be loaded.",
      );
    }
    setBusy(false);
  }, [projectId, sourceFilter, sourceQuery]);

  useEffect(() => {
    const timer = setTimeout(() => { void load(); }, 0);
    return () => clearTimeout(timer);
  }, [load]);

  async function act(key: string, work: () => Promise<string>) {
    setActing(key);
    setMessage("");
    try {
      const note = await work();
      setMessage(note);
      await load();
    } catch (cause) {
      setMessage(
        cause instanceof ApiClientError || cause instanceof Error
          ? cause.message
          : "The action failed.",
      );
    } finally {
      setActing("");
    }
  }

  const keptSources = sources.filter((s) => s.state === "kept");
  const facts = bible?.facts ?? [];

  function entityRow(entity: ResearchEntity) {
    const check = entity.kb_check;
    const typeValue = typeEdits[entity.id] ?? entity.effective_type ?? entity.type ?? "other";
    const nameValue = nameEdits[entity.id] ?? entity.effective_canonical ?? "";
    return (
      <article key={entity.id} className="research-row">
        <div className="research-row-main">
          <strong>{entity.effective_canonical || entity.canonical || entity.id}</strong>
          {entity.effective_canonical !== entity.canonical && (
            <span className="research-note"> recorded as: {entity.canonical}</span>
          )}
          <div className="research-meta">
            <span className="status-badge status-badge--neutral">
              type: {entity.effective_type || "unknown"}
              {entity.effective_type !== entity.type ? ` (recorded: ${entity.type})` : ""}
            </span>
            {entity.role ? <span>{entity.role}</span> : null}
            {entity.type_low_confidence ? (
              <span className="status-badge status-badge--attention">low-confidence type</span>
            ) : null}
            {entity.aliases && entity.aliases.length > 0 ? (
              <span>also: {entity.aliases.join(", ")}</span>
            ) : null}
          </div>
          {entity.description ? <p className="research-snippet">{entity.description}</p> : null}
          {check && (
            <p className={check.conflict ? "research-conflict" : "research-note"}>
              {check.conflict ? <AlertTriangle size={13} aria-hidden /> : <ShieldCheck size={13} aria-hidden />}{" "}
              Knowledge base: {check.kb.type || "no type"} ({check.kb.type_source || check.kb.status || "recorded"})
              {check.kb.description ? ` — ${check.kb.description}` : ""}
              {check.conflict
                ? " This disagrees with the bible's recorded type above. Both are shown; neither has been changed."
                : ""}
            </p>
          )}
          {entity.corrections.map((correction) => (
            <p key={correction.id} className="research-note">
              Your correction: {correction.field} “{correction.original}” → “{correction.corrected}”
              {correction.applications > 0
                ? ` · applied to ${correction.applications} plan/production analysis run(s)`
                : " · not yet applied — it takes effect at the next plan generation or production"}
              <button
                type="button"
                className="research-inline-button"
                disabled={acting !== ""}
                onClick={() =>
                  void act(`uncorrect-${correction.id}`, async () => {
                    await deleteResearchCorrection(projectId, correction.id);
                    return "Correction removed. The recorded bible value stands as-is.";
                  })
                }
              >
                remove
              </button>
            </p>
          ))}
        </div>
        <div className="research-row-actions">
          <button
            type="button"
            className="button button-secondary"
            disabled={acting !== ""}
            onClick={() =>
              void act(`check-${entity.id}`, async () => {
                const result = await checkResearchEntity(projectId, entity.id);
                return result.conflict
                  ? `Knowledge base says “${result.kb.type}”, the bible records “${result.bible_type}”. Recorded as a conflict — review both, then correct the bible type here if the KB is right.`
                  : `Knowledge-base check recorded for ${entity.canonical}: ${result.kb.type || result.kb.status || "no verdict"}.`;
              })
            }
          >
            {acting === `check-${entity.id}` ? "Checking…" : "Check against knowledge base"}
          </button>
          <label className="research-field">
            <span>Correct type</span>
            <select
              value={typeValue}
              onChange={(event) =>
                setTypeEdits((current) => ({ ...current, [entity.id]: event.target.value }))
              }
            >
              {ENTITY_TYPES.map((type) => (
                <option key={type} value={type}>{type}</option>
              ))}
            </select>
          </label>
          <button
            type="button"
            className="button button-secondary"
            disabled={acting !== "" || typeValue === entity.type}
            onClick={() =>
              void act(`correct-type-${entity.id}`, async () => {
                await createResearchCorrection(projectId, {
                  entity_id: entity.id,
                  field: "type",
                  value: typeValue,
                });
                return `Type correction recorded for ${entity.canonical}. It will be applied at the next plan generation or production run.`;
              })
            }
          >
            Apply type correction
          </button>
          <label className="research-field">
            <span>Correct name</span>
            <input
              value={nameValue}
              onChange={(event) =>
                setNameEdits((current) => ({ ...current, [entity.id]: event.target.value }))
              }
            />
          </label>
          <button
            type="button"
            className="button button-secondary"
            disabled={acting !== "" || !nameValue || nameValue === entity.canonical}
            onClick={() =>
              void act(`correct-name-${entity.id}`, async () => {
                await createResearchCorrection(projectId, {
                  entity_id: entity.id,
                  field: "canonical",
                  value: nameValue,
                });
                return `Name correction recorded for ${entity.canonical}. It will be applied at the next plan generation or production run.`;
              })
            }
          >
            Apply name correction
          </button>
        </div>
      </article>
    );
  }

  return (
    <main className="app-page research-page">
      <AppNavigation />
      <ProjectTabs projectId={projectId} />
      <section className="research-content">
        <Link href={`/projects/${projectId}`} className="editor-back">
          <ArrowLeft size={14} strokeWidth={2} aria-hidden /> Project
        </Link>
        <p className="eyebrow">RESEARCH STUDIO</p>
        <h1>Research</h1>
        <p className="research-intro">
          Research for this project, as recorded: topic sessions and their sources, the Case
          Bible from story analysis, the script&apos;s facts with their basis stamps, and
          citations tying claims to sources. Nothing here is generated presentation — an
          empty section means the research does not exist yet, and an unverified fact is
          labelled unverified.
        </p>

        {message && <p className="research-message" role="status">{message}</p>}
        {error && <p className="research-message research-error" role="alert">{error}</p>}
        {busy && <p className="research-message" role="status">Loading the research workspace…</p>}

        {overview && (
          <div className="research-summary" aria-label="Research summary">
            <div><span>SESSIONS</span><strong>{overview.counts.sessions}</strong></div>
            <div><span>SOURCES</span><strong>{overview.counts.sources}</strong></div>
            <div><span>KEPT</span><strong>{overview.counts.sources_kept}</strong></div>
            <div><span>ENTITIES</span><strong>{overview.counts.entities}</strong></div>
            <div><span>FACTS</span><strong>{overview.counts.facts}</strong></div>
            <div><span>CORROBORATED</span><strong>{overview.counts.facts_research_supported}</strong></div>
            <div><span>CITATIONS</span><strong>{overview.counts.citations}</strong></div>
          </div>
        )}

        {/* ---------- Sessions ---------- */}
        <section className="panel research-panel" aria-label="Research sessions">
          <h2><Search size={16} aria-hidden /> Research a topic</h2>
          <p className="research-note">
            A session runs the production research machinery — multiple query angles over web
            search plus a knowledge-base lookup — and saves every result to this project&apos;s
            source library. It works before any script or production exists.
          </p>
          <form
            className="research-form"
            onSubmit={(event) => {
              event.preventDefault();
              if (!topic.trim()) return;
              void act("session", async () => {
                const session = await createResearchSession(projectId, topic.trim());
                setTopic("");
                return `Session recorded: ${session.record.results} result(s), ${session.record.new_sources} new source(s) added to the library.`;
              });
            }}
          >
            <input
              value={topic}
              onChange={(event) => setTopic(event.target.value)}
              placeholder="Topic, person, event, place…"
              aria-label="Research topic"
            />
            <button type="submit" className="button button-primary" disabled={acting !== "" || !topic.trim()}>
              {acting === "session" ? "Researching…" : "Run research session"}
            </button>
          </form>
          {overview && overview.sessions.length > 0 && (
            <div className="research-list">
              {overview.sessions.map((session) => (
                <article key={session.id} className="research-row">
                  <div className="research-row-main">
                    <strong>{session.topic}</strong>
                    <div className="research-meta">
                      <span>{new Date(session.created_at).toLocaleString()}</span>
                      <span>{session.record.results} results · {session.kept_count} kept</span>
                      {session.kb_verdict?.status ? (
                        <span>
                          KB: {session.kb_verdict.type || session.kb_verdict.status}
                          {session.kb_verdict.type_source ? ` (${session.kb_verdict.type_source})` : ""}
                        </span>
                      ) : null}
                    </div>
                    {session.record.query_errors.length > 0 && (
                      <p className="research-note">
                        {session.record.query_errors.length} quer
                        {session.record.query_errors.length === 1 ? "y" : "ies"} failed and{" "}
                        {session.record.query_errors.length === 1 ? "was" : "were"} recorded as
                        errors, not results.
                      </p>
                    )}
                  </div>
                  <div className="research-row-actions">
                    <button
                      type="button"
                      className="button button-secondary"
                      disabled={acting !== ""}
                      onClick={() =>
                        void act(`summary-${session.id}`, async () => {
                          await createResearchSummary(projectId, session.id);
                          return "Session summary generated from its kept sources.";
                        })
                      }
                    >
                      Summarise kept sources
                    </button>
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>

        {/* ---------- Source library ---------- */}
        <section className="panel research-panel" aria-label="Source library">
          <h2><Library size={16} aria-hidden /> Source library</h2>
          <p className="research-note">
            Every source a session found, kept as a project artefact. Credibility is a tier
            derived only from the provider and domain — never a score.{" "}
            {overview
              ? Object.entries(overview.tier_rubric)
                  .map(([tier, rule]) => `${tierLabel(tier)}: ${rule}`)
                  .join(" · ")
              : ""}
          </p>
          <div className="research-form">
            <select
              value={sourceFilter}
              onChange={(event) => setSourceFilter(event.target.value)}
              aria-label="Filter by state"
            >
              <option value="">All states ({Object.values(sourceCounts).reduce((a, b) => a + b, 0)})</option>
              <option value="candidate">Candidates ({sourceCounts.candidate ?? 0})</option>
              <option value="kept">Kept ({sourceCounts.kept ?? 0})</option>
              <option value="discarded">Discarded ({sourceCounts.discarded ?? 0})</option>
            </select>
            <input
              value={sourceQuery}
              onChange={(event) => setSourceQuery(event.target.value)}
              placeholder="Search titles, snippets, URLs…"
              aria-label="Search sources"
            />
          </div>
          {sources.length === 0 && !busy && (
            <p className="research-note">No sources match. Run a research session to add some.</p>
          )}
          <div className="research-list">
            {sources.map((source) => (
              <article key={source.id} className="research-row">
                <div className="research-row-main">
                  <a href={source.url} target="_blank" rel="noreferrer" className="text-link">
                    {source.title || source.url}
                  </a>
                  <div className="research-meta">
                    <span className="status-badge status-badge--neutral">{tierLabel(source.tier)}</span>
                    <span>{source.provider}</span>
                    {source.domain ? <span>{source.domain}</span> : null}
                    {source.research_type ? <span>angle: {source.research_type}</span> : null}
                    <span>state: {source.state}</span>
                  </div>
                  {source.snippet ? <p className="research-snippet">{source.snippet}</p> : null}
                </div>
                <div className="research-row-actions">
                  {(["candidate", "kept", "discarded"] as ResearchSourceState[])
                    .filter((state) => state !== source.state)
                    .map((state) => (
                      <button
                        key={state}
                        type="button"
                        className="button button-secondary"
                        disabled={acting !== ""}
                        onClick={() =>
                          void act(`state-${source.id}`, async () => {
                            await setResearchSourceState(projectId, source.id, state);
                            return `Source marked ${state}.`;
                          })
                        }
                      >
                        {state === "kept" ? "Keep" : state === "discarded" ? "Discard" : "Undecide"}
                      </button>
                    ))}
                </div>
              </article>
            ))}
          </div>
        </section>

        {/* ---------- Case Bible: entities ---------- */}
        <section className="panel research-panel" aria-label="Case Bible entities">
          <h2><BookMarked size={16} aria-hidden /> Case Bible — people, places, organisations</h2>
          {!bible?.recorded && !busy && (
            <p className="research-note">
              {bible?.note ||
                "No Case Bible is recorded yet. Story analysis during plan generation or a production run produces it."}
            </p>
          )}
          {bible?.recorded && (
            <>
              <p className="research-note">
                {bible.title ? `${bible.title} · ` : ""}
                {bible.story_type ? `${bible.story_type} · ` : ""}
                {bible.story_era ? `${bible.story_era} · ` : ""}
                analysis mode: {bible.mode || "not recorded"}. Types below are the bible&apos;s
                recorded values; your corrections overlay them here and are applied to the
                bible itself at the next plan generation or production.
              </p>
              <div className="research-list">{bible.entities.map(entityRow)}</div>
            </>
          )}
        </section>

        {/* ---------- Facts ---------- */}
        <section className="panel research-panel" aria-label="Script facts">
          <h2><ShieldCheck size={16} aria-hidden /> Script facts &amp; corroboration</h2>
          <p className="research-note">
            Facts are extracted from the script&apos;s own sentences. Basis{" "}
            <strong>script claim</strong> means the script asserts it and nothing more;{" "}
            <strong>research supported</strong> means a recorded knowledge-base verdict
            genuinely corroborates it. Most facts honestly stay script claims.
          </p>
          <button
            type="button"
            className="button button-primary"
            disabled={acting !== "" || !bible?.recorded}
            onClick={() =>
              void act("fact-check", async () => {
                const result = await runResearchFactCheck(projectId);
                return `Fact check recorded: ${result.run.record.corroborated ?? 0} corroborated, ${result.run.record.not_corroborated ?? 0} not corroborated, ${result.run.record.skipped_no_subject_or_year ?? 0} not checkable (no subject or year).`;
              })
            }
          >
            {acting === "fact-check" ? "Checking…" : "Run fact check"}
          </button>
          {facts.length > 0 && (
            <div className="research-list">
              {facts.map((fact) => (
                <article key={fact.fact_id} className="research-row">
                  <div className="research-row-main">
                    <strong>
                      {fact.kind === "dated_event" ? `${fact.when}: ` : ""}
                      {fact.what || fact.display || fact.fact_id}
                    </strong>
                    <div className="research-meta">
                      <span
                        className={`status-badge ${fact.basis === "research_supported" ? "status-badge--success" : "status-badge--attention"}`}
                      >
                        {fact.basis === "research_supported" ? "research supported" : "script claim"}
                      </span>
                      <span>{fact.kind.replaceAll("_", " ")}</span>
                      {fact.sentence_number ? <span>sentence {fact.sentence_number}</span> : null}
                      {fact.kb_check ? (
                        <span>
                          KB check: {fact.kb_check.status || "recorded"}
                          {fact.kb_check.source ? ` via ${fact.kb_check.source}` : ""}
                        </span>
                      ) : (
                        <span>no KB check recorded</span>
                      )}
                    </div>
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>

        {/* ---------- Timeline ---------- */}
        <section className="panel research-panel" aria-label="Timeline">
          <h2><Plus size={16} aria-hidden /> Timeline</h2>
          <p className="research-note">
            Case Bible events as recorded (with their basis), plus events you add here. Your
            events are stored with the project&apos;s research and labelled as yours; the
            bible itself is never edited from this page.
          </p>
          {bible && bible.timeline.length > 0 && (
            <div className="research-list">
              {bible.timeline.map((event, index) => (
                <article key={`${event.origin}-${event.id || event.fact_id || index}`} className="research-row">
                  <div className="research-row-main">
                    <strong>{event.when}</strong> — {event.what}
                    <div className="research-meta">
                      <span className="status-badge status-badge--neutral">
                        {event.origin === "studio" ? "added by you" : "case bible"}
                      </span>
                      {event.fact_basis ? <span>basis: {event.fact_basis.replaceAll("_", " ")}</span> : null}
                      {event.support ? <span>support: {event.support.replaceAll("_", " ")}</span> : null}
                    </div>
                  </div>
                  {event.origin === "studio" && event.id && (
                    <div className="research-row-actions">
                      <button
                        type="button"
                        className="button button-secondary"
                        disabled={acting !== ""}
                        onClick={() =>
                          void act(`event-del-${event.id}`, async () => {
                            await deleteResearchTimelineEvent(projectId, event.id!);
                            return "Timeline event removed.";
                          })
                        }
                      >
                        <Trash2 size={13} aria-hidden /> Remove
                      </button>
                    </div>
                  )}
                </article>
              ))}
            </div>
          )}
          <form
            className="research-form"
            onSubmit={(event) => {
              event.preventDefault();
              void act("event-add", async () => {
                await createResearchTimelineEvent(projectId, {
                  when: eventWhen.trim(),
                  what: eventWhat.trim(),
                  source_ids: eventSource ? [eventSource] : [],
                });
                setEventWhen("");
                setEventWhat("");
                setEventSource("");
                return "Timeline event added to the project's research.";
              });
            }}
          >
            <input value={eventWhen} onChange={(e) => setEventWhen(e.target.value)} placeholder="When (e.g. May 1956)" aria-label="Event date" />
            <input value={eventWhat} onChange={(e) => setEventWhat(e.target.value)} placeholder="What happened" aria-label="Event description" />
            <select value={eventSource} onChange={(e) => setEventSource(e.target.value)} aria-label="Supporting source">
              <option value="">No source</option>
              {keptSources.map((source) => (
                <option key={source.id} value={source.id}>{source.title || source.url}</option>
              ))}
            </select>
            <button type="submit" className="button button-secondary" disabled={acting !== "" || !eventWhen.trim() || !eventWhat.trim()}>
              Add event
            </button>
          </form>
        </section>

        {/* ---------- Citations ---------- */}
        <section className="panel research-panel" aria-label="Citations">
          <h2><BookMarked size={16} aria-hidden /> Citations &amp; bibliography</h2>
          <p className="research-note">
            A citation ties a claim — a recorded script fact, or a statement you write — to a
            source in the library or an external URL you supply. The bibliography below is
            generated from those records only.
          </p>
          <form
            className="research-form research-form-stack"
            onSubmit={(event) => {
              event.preventDefault();
              void act("cite", async () => {
                const claim =
                  citeKind === "fact"
                    ? { kind: "fact", fact_id: citeFact }
                    : { kind: "statement", label: citeLabel.trim() };
                const body =
                  citeExternalUrl.trim()
                    ? { claim, external: { title: citeExternalTitle.trim() || undefined, url: citeExternalUrl.trim() } }
                    : { claim, source_id: citeSource };
                await createResearchCitation(projectId, body);
                setCiteLabel("");
                setCiteExternalUrl("");
                setCiteExternalTitle("");
                return "Citation recorded.";
              });
            }}
          >
            <select value={citeKind} onChange={(e) => setCiteKind(e.target.value)} aria-label="Claim kind">
              <option value="fact">A recorded script fact</option>
              <option value="statement">A statement I write</option>
            </select>
            {citeKind === "fact" ? (
              <select value={citeFact} onChange={(e) => setCiteFact(e.target.value)} aria-label="Fact">
                <option value="">Choose a fact…</option>
                {facts.map((fact) => (
                  <option key={fact.fact_id} value={fact.fact_id}>
                    {(fact.when ? `${fact.when}: ` : "") + (fact.what || fact.display || fact.fact_id)}
                  </option>
                ))}
              </select>
            ) : (
              <input value={citeLabel} onChange={(e) => setCiteLabel(e.target.value)} placeholder="The statement being cited" aria-label="Statement" />
            )}
            <select value={citeSource} onChange={(e) => setCiteSource(e.target.value)} aria-label="Library source" disabled={!!citeExternalUrl.trim()}>
              <option value="">Choose a library source…</option>
              {sources.map((source) => (
                <option key={source.id} value={source.id}>{source.title || source.url}</option>
              ))}
            </select>
            <span className="research-note">or cite an external source:</span>
            <input value={citeExternalTitle} onChange={(e) => setCiteExternalTitle(e.target.value)} placeholder="External title (optional)" aria-label="External title" />
            <input value={citeExternalUrl} onChange={(e) => setCiteExternalUrl(e.target.value)} placeholder="https://…" aria-label="External URL" />
            <button
              type="submit"
              className="button button-secondary"
              disabled={
                acting !== "" ||
                (citeKind === "fact" ? !citeFact : !citeLabel.trim()) ||
                (!citeSource && !citeExternalUrl.trim())
              }
            >
              Add citation
            </button>
          </form>
          {citations.length > 0 && (
            <div className="research-list">
              {citations.map((citation) => (
                <article key={citation.id} className="research-row">
                  <div className="research-row-main">
                    <strong>{citation.claim.label}</strong>
                    <div className="research-meta">
                      <span>{citation.claim.kind.replaceAll("_", " ")}</span>
                      {citation.source ? (
                        <a href={citation.source.url} target="_blank" rel="noreferrer" className="text-link">
                          {citation.source.title || citation.source.url}
                        </a>
                      ) : (
                        <span>source no longer in library</span>
                      )}
                      {citation.source ? <span>tier: {tierLabel(citation.source.tier)}</span> : null}
                    </div>
                  </div>
                  <div className="research-row-actions">
                    <button
                      type="button"
                      className="button button-secondary"
                      disabled={acting !== ""}
                      onClick={() =>
                        void act(`cite-del-${citation.id}`, async () => {
                          await deleteResearchCitation(projectId, citation.id);
                          return "Citation removed.";
                        })
                      }
                    >
                      <Trash2 size={13} aria-hidden /> Remove
                    </button>
                  </div>
                </article>
              ))}
            </div>
          )}
          {bibliography && (
            <>
              <h3>Bibliography</h3>
              <pre className="research-bibliography">{bibliography}</pre>
            </>
          )}
        </section>

        {/* ---------- Summaries ---------- */}
        <section className="panel research-panel" aria-label="Research summaries">
          <h2><CheckCircle2 size={16} aria-hidden /> Research summaries</h2>
          <p className="research-note">
            A summary is generated only from kept sources, and is stored with the exact
            sources it rests on. With nothing kept, there is nothing honest to summarise.
          </p>
          <button
            type="button"
            className="button button-primary"
            disabled={acting !== "" || keptSources.length === 0}
            onClick={() =>
              void act("summary-library", async () => {
                await createResearchSummary(projectId);
                return "Library summary generated from all kept sources.";
              })
            }
          >
            {acting === "summary-library" ? "Generating…" : "Summarise all kept sources"}
          </button>
          {keptSources.length === 0 && (
            <p className="research-note">No kept sources yet — keep at least one source above.</p>
          )}
          <div className="research-list">
            {summaries.map((summary) => (
              <article key={summary.id} className="research-row">
                <div className="research-row-main">
                  <strong>{summary.topic}</strong>
                  <div className="research-meta">
                    <span>{summary.scope === "session" ? "session summary" : "library summary"}</span>
                    <span>{new Date(summary.created_at).toLocaleString()}</span>
                    <span>resting on {summary.sources.length} source(s)</span>
                  </div>
                  <p className="research-summary-text">{summary.text}</p>
                  <div className="research-meta">
                    {summary.sources.map((source, index) => (
                      <a key={source.id} href={source.url} target="_blank" rel="noreferrer" className="text-link">
                        [{index + 1}] {source.title || source.url}
                      </a>
                    ))}
                  </div>
                </div>
              </article>
            ))}
          </div>
        </section>

        {/* ---------- Gaps ---------- */}
        {overview && (
          <section className="panel research-panel" aria-label="Research gaps">
            <h2><AlertTriangle size={16} aria-hidden /> Gaps &amp; conflicts</h2>
            <p className="research-note">
              Derived from the recorded data only — each item states what it was derived
              from. A gap is a prompt to research more, not a verdict on the script.
            </p>
            <div className="research-list">
              {Object.entries(overview.gaps).map(([key, gap]) => (
                <article key={key} className="research-row">
                  <div className="research-row-main">
                    <strong>
                      {key.replaceAll("_", " ")}: {gap.count}
                    </strong>
                    <p className="research-note">{gap.basis}</p>
                  </div>
                </article>
              ))}
            </div>
          </section>
        )}
      </section>
    </main>
  );
}
