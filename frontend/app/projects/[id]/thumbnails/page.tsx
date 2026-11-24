"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { ProjectTabs } from "@/components/project-tabs";
import { ErrorState, LoadingState } from "@/components/states";
import { AuthenticatedDownloadLink } from "@/components/authenticated-media";
import {
  ArrowLeft,
  Download,
  ImageOff,
  Loader2,
  Sparkles,
  Trash2,
  Upload,
  Wand2,
} from "lucide-react";
import {
  ApiClientError,
  analyseThumbnailBase,
  composeThumbnail,
  deleteThumbnail,
  fetchAuthenticatedMediaObjectUrl,
  generateThumbnailVariants,
  getThumbnails,
  suggestThumbnailHeadlines,
  thumbnailDownloadPath,
  thumbnailImagePath,
  uploadAsset,
  type ThumbnailAnalysis,
  type ThumbnailBase,
  type ThumbnailStudioView,
  type ThumbnailVariant,
} from "@/lib/api";

/**
 * Thumbnail Generator (founder priority #13) — compositional
 * thumbnails rendered by the backend from the project's OWN
 * recorded material (film frames, verified stills, uploads) at
 * YouTube's 1280x720 spec. The numbers on this page are mechanical
 * measurements of the finished files (size, format, headline
 * contrast against its backing region, font height at small
 * scale) — the page says so, because nothing here predicts how a
 * thumbnail will perform, and no control pretends otherwise. The
 * reference analysis is likewise labelled measured properties
 * (OCR text, palette, luminance), not AI understanding.
 */

const LAYOUT_LABELS: Record<string, string> = {
  bottom_band: "Bottom band (subject hero)",
  top_band: "Top band",
  left_panel: "Left panel (split)",
  center_scrim: "Centre scrim",
};

function ThumbImage({ projectId, thumbId, alt, small }: {
  projectId: string;
  thumbId: string;
  alt: string;
  small?: boolean;
}) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let objectUrl: string | null = null;
    const controller = new AbortController();
    fetchAuthenticatedMediaObjectUrl(thumbnailImagePath(projectId, thumbId), controller.signal)
      .then((result) => {
        objectUrl = result.url;
        setUrl(result.url);
      })
      .catch(() => {
        if (!controller.signal.aborted) setFailed(true);
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [projectId, thumbId]);
  if (failed) {
    return (
      <span className="thumb-preview thumb-preview-failed" role="img" aria-label={alt}>
        <ImageOff size={16} strokeWidth={1.8} aria-hidden /> Preview unavailable
      </span>
    );
  }
  if (!url) {
    return (
      <span className="thumb-preview thumb-preview-loading" role="status" aria-label={`Loading ${alt}`}>
        <Loader2 size={16} strokeWidth={1.8} aria-hidden /> Loading…
      </span>
    );
  }
  // eslint-disable-next-line @next/next/no-img-element -- authenticated blob URL; next/image cannot send the API token header
  return <img className={small ? "thumb-img thumb-img-small" : "thumb-img"} src={url} alt={alt} />;
}

function baseKindLabel(base: ThumbnailBase | ThumbnailVariant["base"]): string {
  if (base.kind === "film_frame") {
    return `Film frame${base.timestamp_seconds != null ? ` at ${base.timestamp_seconds.toFixed(1)}s` : ""}`;
  }
  if (base.kind === "asset_still") {
    return `Verified still${base.provider ? ` · ${base.provider}` : ""}${base.file_name ? ` · ${base.file_name}` : ""}`;
  }
  return `Upload${base.file_name ? ` · ${base.file_name}` : ""}`;
}

function VariantCard({ projectId, variant, onDelete }: {
  projectId: string;
  variant: ThumbnailVariant;
  onDelete: (id: string) => void;
}) {
  const checks = variant.checks;
  return (
    <article className="thumb-card">
      {variant.available ? (
        <ThumbImage projectId={projectId} thumbId={variant.id} alt={`Thumbnail: ${variant.headline}`} />
      ) : (
        <span className="thumb-preview thumb-preview-failed" role="img" aria-label="Image file missing">
          <ImageOff size={16} strokeWidth={1.8} aria-hidden />
          Recorded, but the image file is no longer on disk — regenerate it from its composition.
        </span>
      )}
      <div className="thumb-card-body">
        <h3>{variant.headline}</h3>
        <p className="plan-meta">
          {baseKindLabel(variant.base)} · {LAYOUT_LABELS[variant.layout] ?? variant.layout} · accent {variant.accent}
        </p>
        {variant.available && (
          <div className="thumb-small-row">
            <ThumbImage projectId={projectId} thumbId={variant.id} alt="" small />
            <span className="plan-meta">The same file at 160 px wide — the size class it is judged at in a feed.</span>
          </div>
        )}
        <ul className="thumb-checks">
          <li>{checks.dimensions[0]}×{checks.dimensions[1]} · {checks.format}</li>
          <li>{(checks.file_size_bytes / 1000).toFixed(0)} KB — {checks.under_youtube_size_limit ? "under" : "OVER"} YouTube&apos;s 2 MB limit</li>
          <li>Headline contrast ratio {checks.headline_contrast_ratio.toFixed(2)} (measured against its backing region)</li>
          <li>Headline height {checks.headline_font_height_px_at_10_percent_scale.toFixed(1)} px at 10% display scale</li>
        </ul>
        <div className="thumb-card-actions">
          {variant.available && (
            <AuthenticatedDownloadLink
              path={thumbnailDownloadPath(projectId, variant.id)}
              filename={`thumbnail-${variant.id}.${checks.format === "PNG" ? "png" : "jpg"}`}
              className="button button-secondary"
            >
              <Download size={14} strokeWidth={2} aria-hidden /> Download
            </AuthenticatedDownloadLink>
          )}
          <button type="button" className="button button-quiet" onClick={() => onDelete(variant.id)}>
            <Trash2 size={14} strokeWidth={2} aria-hidden /> Delete
          </button>
        </div>
      </div>
    </article>
  );
}

export default function ThumbnailsPage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;
  const [view, setView] = useState<ThumbnailStudioView | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [acting, setActing] = useState(false);

  const [baseId, setBaseId] = useState<string>("");
  const [headline, setHeadline] = useState("");
  const [layout, setLayout] = useState("bottom_band");
  const [accent, setAccent] = useState("amber");
  const [proposals, setProposals] = useState<string[] | null>(null);
  const [analysis, setAnalysis] = useState<ThumbnailAnalysis | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const loaded = await getThumbnails(projectId);
      setView(loaded);
      setBaseId((current) => {
        if (current) return current;
        const first = loaded.bases.stills[0] ?? loaded.bases.film.frames[0] ?? loaded.bases.uploads[0];
        return first?.base_id ?? "";
      });
      setHeadline((current) => current || loaded.brief.headline_suggestions[0] || "");
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }, [projectId]);

  useEffect(() => {
    const timer = setTimeout(() => { void load(); }, 0);
    return () => clearTimeout(timer);
  }, [load]);

  const words = headline.trim() ? headline.trim().split(/\s+/).length : 0;
  const overCap = view != null && (words > view.spec.max_headline_words || headline.length > view.spec.max_headline_characters);
  const allBases: ThumbnailBase[] = view
    ? [...view.bases.stills, ...view.bases.film.frames, ...view.bases.uploads]
    : [];
  const hasBases = allBases.length > 0;

  async function runAction(fn: () => Promise<void>) {
    setActing(true);
    setActionError(null);
    try {
      await fn();
    } catch (err) {
      setActionError(err instanceof ApiClientError ? err.message : "The action failed.");
    } finally {
      setActing(false);
    }
  }

  async function onCompose() {
    await runAction(async () => {
      await composeThumbnail(projectId, {
        base_id: baseId,
        headline: headline || undefined,
        layout,
        accent,
      });
      await load();
    });
  }

  async function onGenerateSet() {
    await runAction(async () => {
      await generateThumbnailVariants(projectId, headline || undefined);
      await load();
    });
  }

  async function onSuggest() {
    await runAction(async () => {
      const result = await suggestThumbnailHeadlines(projectId);
      setProposals(result.proposals);
    });
  }

  async function onAnalyse() {
    await runAction(async () => {
      setAnalysis(await analyseThumbnailBase(projectId, baseId));
    });
  }

  async function onUpload(file: File) {
    await runAction(async () => {
      const record = await uploadAsset(projectId, file);
      await load();
      if (record.upload_id) setBaseId(`upload:${record.upload_id}`);
    });
  }

  async function onDelete(id: string) {
    await runAction(async () => {
      await deleteThumbnail(projectId, id);
      await load();
    });
  }

  return (
    <main className="app-page thumbnails-page">
      <AppNavigation />
      <ProjectTabs projectId={projectId} />
      <section className="plan-content">
        <Link href={`/projects/${projectId}`} className="editor-back">
          <ArrowLeft size={14} strokeWidth={2} aria-hidden /> Project
        </Link>
        <p className="eyebrow">PACKAGING</p>
        <h1>Thumbnails</h1>
        <p className="plan-intro">
          Thumbnails from this film&apos;s own material, at YouTube&apos;s exact 1280×720 — measured, never scored for clicks.
        </p>

        {busy && <LoadingState className="plan-message" label="Loading the thumbnail studio…" />}
        {!busy && error != null && <ErrorState error={error} onRetry={() => { setBusy(true); void load(); }} />}

        {view && (
          <>
            <section className="hub-panel" aria-label="Thumbnail brief">
              <h2>Brief — from the film&apos;s own records</h2>
              <p className="hub-panel-note">
                Derived from the project and its Case Bible, exactly as recorded — nothing here
                is invented copy.
              </p>
              <div className="plan-summary">
                <div>
                  <h3>Title</h3>
                  <p>{view.brief.title || "Not recorded"}</p>
                </div>
                <div>
                  <h3>Primary subject</h3>
                  <p>
                    {view.brief.case_bible_state !== "recorded"
                      ? "No Case Bible recorded for this project yet."
                      : view.brief.primary_subject?.name
                        ? `${view.brief.primary_subject.name}${view.brief.primary_subject.role ? ` — ${view.brief.primary_subject.role}` : ""}`
                        : "Not recorded in the Case Bible."}
                  </p>
                </div>
                <div>
                  <h3>Recorded facts</h3>
                  <p>
                    {view.brief.recorded_facts.length
                      ? view.brief.recorded_facts.map((f) => f.display).join(" · ")
                      : "None recorded."}
                  </p>
                </div>
              </div>
              {view.brief.headline_suggestions.length > 0 && (
                <p className="plan-meta">
                  Recorded headline material:{" "}
                  {view.brief.headline_suggestions.map((suggestion) => (
                    <button
                      key={suggestion}
                      type="button"
                      className="button button-quiet thumb-suggestion"
                      onClick={() => setHeadline(suggestion)}
                    >
                      {suggestion}
                    </button>
                  ))}
                </p>
              )}
              <p className="plan-meta">
                <button type="button" className="button button-secondary" onClick={() => void onSuggest()} disabled={acting}>
                  <Sparkles size={14} strokeWidth={2} aria-hidden /> Draft headlines with AI
                </button>{" "}
                Drafts are proposals from the recorded brief only — they compose nothing until
                you use one.
              </p>
              {proposals && (
                <p className="plan-meta">
                  {proposals.length === 0
                    ? "The provider returned no usable proposals."
                    : proposals.map((proposal) => (
                        <button
                          key={proposal}
                          type="button"
                          className="button button-quiet thumb-suggestion"
                          onClick={() => setHeadline(proposal)}
                        >
                          {proposal}
                        </button>
                      ))}
                </p>
              )}
            </section>

            {!hasBases ? (
              <div className="plan-empty">
                <h2>No material to compose from yet</h2>
                <p>
                  This project has no completed production (so no film frames or verified
                  stills) and no uploaded images. Upload a base image below, or run a
                  production first — thumbnails are composed only from the project&apos;s own
                  recorded material.
                </p>
              </div>
            ) : (
              <section className="hub-panel" aria-label="Compose a thumbnail">
                <h2>Compose</h2>
                <div className="form-fields">
                  <label>
                    Base image
                    <select value={baseId} onChange={(event) => setBaseId(event.target.value)}>
                      {view.bases.stills.length > 0 && (
                        <optgroup label="Verified stills">
                          {view.bases.stills.map((base) => (
                            <option key={base.base_id} value={base.base_id}>{baseKindLabel(base)}</option>
                          ))}
                        </optgroup>
                      )}
                      {view.bases.film.frames.length > 0 && (
                        <optgroup label={`Film frames (${view.bases.film.timestamps_source === "timeline" ? "recorded timeline moments" : "duration fractions"})`}>
                          {view.bases.film.frames.map((base) => (
                            <option key={base.base_id} value={base.base_id}>{baseKindLabel(base)}</option>
                          ))}
                        </optgroup>
                      )}
                      {view.bases.uploads.length > 0 && (
                        <optgroup label="Your uploads">
                          {view.bases.uploads.map((base) => (
                            <option key={base.base_id} value={base.base_id}>{baseKindLabel(base)}</option>
                          ))}
                        </optgroup>
                      )}
                    </select>
                  </label>
                  <label>
                    Headline
                    <input
                      value={headline}
                      onChange={(event) => setHeadline(event.target.value)}
                      placeholder="A few words — the film's name or its hook"
                      maxLength={120}
                    />
                  </label>
                  <label>
                    Layout
                    <select value={layout} onChange={(event) => setLayout(event.target.value)}>
                      {view.spec.layouts.map((name) => (
                        <option key={name} value={name}>{LAYOUT_LABELS[name] ?? name}</option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Accent
                    <select value={accent} onChange={(event) => setAccent(event.target.value)}>
                      {view.spec.accents.map((name) => (
                        <option key={name} value={name}>{name}</option>
                      ))}
                    </select>
                  </label>
                </div>
                <p className="plan-meta">
                  {words} word{words === 1 ? "" : "s"} · {headline.length} characters — the cap is{" "}
                  {view.spec.max_headline_words} words / {view.spec.max_headline_characters} characters,
                  so the text stays legible at small sizes.
                  {overCap && " This headline is over the cap and will be refused."}
                </p>
                <div className="thumb-actions">
                  <button type="button" className="button button-primary" onClick={() => void onCompose()} disabled={acting || !baseId}>
                    <Wand2 size={14} strokeWidth={2} aria-hidden /> Compose thumbnail
                  </button>
                  <button type="button" className="button button-secondary" onClick={() => void onGenerateSet()} disabled={acting}>
                    Generate variant set (2 bases × 4 layouts)
                  </button>
                  <button type="button" className="button button-quiet" onClick={() => void onAnalyse()} disabled={acting || !baseId}>
                    Measure the selected base image
                  </button>
                </div>
              </section>
            )}

            <section className="hub-panel" aria-label="Upload a base or reference image">
              <h2>Upload a base or reference image</h2>
              <p className="hub-panel-note">
                Stored through the project&apos;s normal upload machinery as your own content
                (rights recorded as unknown, as with every upload). Use it as a composition
                base, or measure a reference thumbnail&apos;s properties with the button above.
              </p>
              <input
                ref={fileInput}
                type="file"
                accept="image/*"
                hidden
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  event.target.value = "";
                  if (file) void onUpload(file);
                }}
              />
              <button type="button" className="button button-secondary" onClick={() => fileInput.current?.click()} disabled={acting}>
                <Upload size={14} strokeWidth={2} aria-hidden /> Upload image
              </button>
            </section>

            {analysis && (
              <section className="hub-panel" aria-label="Measured properties">
                <h2>Measured properties</h2>
                <p className="hub-panel-note">{analysis.label}</p>
                <div className="plan-summary">
                  <div>
                    <h3>Dimensions</h3>
                    <p>{analysis.dimensions[0]}×{analysis.dimensions[1]}</p>
                  </div>
                  <div>
                    <h3>Text read by OCR{analysis.ocr.engine ? ` (${analysis.ocr.engine})` : ""}</h3>
                    <p>
                      {analysis.ocr.state === "recorded"
                        ? analysis.ocr.lines.length
                          ? analysis.ocr.lines.join(" · ")
                          : "No text detected."
                        : "OCR unavailable on this deployment."}
                    </p>
                  </div>
                  <div>
                    <h3>Luminance</h3>
                    <p>Mean {analysis.luminance.mean.toFixed(2)} · spread {analysis.luminance.spread.toFixed(2)}</p>
                  </div>
                </div>
                <div className="thumb-palette" aria-label="Dominant colours">
                  {analysis.palette.map((swatch) => (
                    <span key={swatch.hex} className="thumb-swatch">
                      <span className="thumb-swatch-chip" style={{ background: swatch.hex }} />
                      {swatch.hex} · {(swatch.share * 100).toFixed(0)}%
                    </span>
                  ))}
                </div>
              </section>
            )}

            {actionError && <p className="thumb-error" role="alert">{actionError}</p>}

            <section aria-label="Variants">
              <h2 className="thumb-variants-title">Variants ({view.variants.length})</h2>
              {view.variants.length === 0 ? (
                <p className="plan-meta">
                  No thumbnails composed yet. Pick a base and a headline above, or generate the
                  variant set.
                </p>
              ) : (
                <div className="thumb-grid">
                  {view.variants.map((variant) => (
                    <VariantCard key={variant.id} projectId={projectId} variant={variant} onDelete={(id) => void onDelete(id)} />
                  ))}
                </div>
              )}
            </section>
          </>
        )}
      </section>
    </main>
  );
}
