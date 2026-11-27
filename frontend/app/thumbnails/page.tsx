"use client";

import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { AuthenticatedDownloadLink } from "@/components/authenticated-media";
import { ErrorState, LoadingState } from "@/components/states";
import { ImageOff, Loader2, Palette, RefreshCw, Search, Trash2, Upload, Wand2 } from "lucide-react";
import {
  analyseChannelThumbnails,
  composeThumbnail,
  deleteThumbnail,
  fetchAuthenticatedMediaObjectUrl,
  generateThumbnailVariants,
  getThumbnails,
  listProjects,
  regenerateThumbnail,
  suggestThumbnailHeadlines,
  thumbnailDownloadPath,
  thumbnailImagePath,
  uploadThumbnailBase,
  type ChannelStyleVideo,
  type ProjectSummary,
  type ThumbnailBase,
  type ThumbnailStudioView,
} from "@/lib/api";

/**
 * Thumbnail Studio (Product Improvements item 5) — its own
 * page, independent of any completed video.
 *
 * What it honestly is: a compositional studio over the
 * project's OWN material (film frames when a run exists,
 * verified stills, and your uploads — an upload is a base the
 * moment it lands, no finished film required). A channel URL
 * yields REAL channel data with MEASURED thumbnail properties
 * (palette, OCR text, luminance) as style reference only —
 * another channel's artwork is never a base and is never
 * copied. There is no generative imagery and no performance
 * prediction anywhere on this page.
 */

const LAYOUT_LABELS: Record<string, string> = {
  bottom_band: "Bottom band",
  top_band: "Top band",
  left_panel: "Left panel",
  center_scrim: "Centre scrim",
};

function ThumbImage({ projectId, thumbId, alt }: { projectId: string; thumbId: string; alt: string }) {
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
  // eslint-disable-next-line @next/next/no-img-element -- authenticated blob URL
  return <img className="thumb-img" src={url} alt={alt} />;
}

function baseLabel(base: ThumbnailBase): string {
  if (base.kind === "film_frame") {
    return `Film frame${base.timestamp_seconds != null ? ` at ${base.timestamp_seconds.toFixed(1)}s` : ""}`;
  }
  if (base.kind === "asset_still") {
    return `Verified still${base.file_name ? ` · ${base.file_name}` : ""}`;
  }
  return `Your upload${base.file_name ? ` · ${base.file_name}` : ""}`;
}

export default function ThumbnailStudioPage() {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [projectId, setProjectId] = useState("");
  const [view, setView] = useState<ThumbnailStudioView | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [working, setWorking] = useState("");

  const [baseId, setBaseId] = useState("");
  const [headline, setHeadline] = useState("");
  const [layout, setLayout] = useState("bottom_band");
  const [accent, setAccent] = useState("amber");

  const [proposals, setProposals] = useState<string[] | null>(null);

  const [channelUrl, setChannelUrl] = useState("");
  const [channelStyles, setChannelStyles] = useState<{
    channel: { title?: string | null; subscriber_count?: number | null };
    videos: ChannelStyleVideo[];
    label: string;
  } | null>(null);

  const loadProjects = useCallback(async () => {
    const data = await listProjects();
    const list = data.projects || [];
    setProjects(list);
    return list;
  }, []);

  const loadView = useCallback(async (id: string) => {
    if (!id) {
      setView(null);
      return;
    }
    const data = await getThumbnails(id);
    setView(data);
    const firstBase =
      data.bases.uploads[0] || data.bases.stills[0] || data.bases.film.frames[0];
    setBaseId((current) => current || firstBase?.base_id || "");
    setHeadline(
      (current) => current || data.brief.headline_suggestions[0] || "",
    );
  }, []);

  const load = useCallback(async () => {
    setError(null);
    try {
      const list = await loadProjects();
      const first = list[0]?.id || "";
      setProjectId((current) => current || first);
      await loadView(first);
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }, [loadProjects, loadView]);

  useEffect(() => {
    void load();
  }, [load]);

  const pickProject = async (id: string) => {
    setProjectId(id);
    setBaseId("");
    setHeadline("");
    setActionError(null);
    try {
      await loadView(id);
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : "Could not load that project.");
    }
  };

  const allBases: ThumbnailBase[] = view
    ? [...view.bases.film.frames, ...view.bases.stills, ...view.bases.uploads]
    : [];

  const compose = async () => {
    if (!projectId || !baseId) return;
    setWorking("compose");
    setActionError(null);
    try {
      await composeThumbnail(projectId, {
        base_id: baseId,
        headline: headline || undefined,
        layout,
        accent,
      });
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : "Composition failed.");
      setWorking("");
      return;
    }
    // The thumbnail exists from here — a refresh failure must
    // not report the composition itself as failed.
    try {
      await loadView(projectId);
      setNotice("Thumbnail composed and saved below.");
    } catch {
      setNotice("Thumbnail composed, but the list could not be refreshed — reload the page to see it.");
    } finally {
      setWorking("");
    }
  };

  const variants = async () => {
    if (!projectId) return;
    setWorking("variants");
    setActionError(null);
    try {
      const result = await generateThumbnailVariants(projectId, headline || undefined);
      setNotice(
        `${result.created.length} variation(s) created.` +
          (result.skipped.length
            ? ` ${result.skipped.length} skipped: ${result.skipped.map((s) => s.message).join(" ")}`
            : ""),
      );
      await loadView(projectId);
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : "Variation generation failed.");
    } finally {
      setWorking("");
    }
  };

  const removeVariant = async (thumbId: string) => {
    if (!projectId) return;
    setWorking("delete");
    setActionError(null);
    try {
      await deleteThumbnail(projectId, thumbId);
      setNotice("Thumbnail deleted.");
      await loadView(projectId);
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : "Delete failed.");
    } finally {
      setWorking("");
    }
  };

  const regenerateVariant = async (thumbId: string) => {
    if (!projectId) return;
    setWorking("regenerate");
    setActionError(null);
    try {
      await regenerateThumbnail(projectId, thumbId);
      setNotice("Thumbnail regenerated from its recorded composition.");
      await loadView(projectId);
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : "Regeneration failed.");
    } finally {
      setWorking("");
    }
  };

  const suggestHeadlines = async () => {
    if (!projectId) return;
    setWorking("suggest");
    setActionError(null);
    try {
      const result = await suggestThumbnailHeadlines(projectId);
      setProposals(result.proposals);
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : "Headline suggestions failed.");
    } finally {
      setWorking("");
    }
  };

  const upload = async (file: File | null) => {
    if (!file || !projectId) return;
    // The server's own limits, enforced before the upload so
    // the refusal is instant and explained (25 MB studio cap).
    if (!file.type.startsWith("image/")) {
      setActionError("That file is not an image — thumbnail bases must be image files.");
      return;
    }
    if (file.size > 25 * 1024 * 1024) {
      setActionError("That image is over the 25 MB upload limit.");
      return;
    }
    setWorking("upload");
    setActionError(null);
    try {
      await uploadThumbnailBase(projectId, file);
      setNotice("Uploaded — it is now available as a base and as a measurable reference.");
      await loadView(projectId);
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : "Upload failed.");
    } finally {
      setWorking("");
    }
  };

  const analyseChannel = async () => {
    if (!channelUrl.trim()) return;
    setWorking("channel");
    setActionError(null);
    try {
      const data = await analyseChannelThumbnails(channelUrl.trim());
      setChannelStyles(data);
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : "Channel analysis failed.");
    } finally {
      setWorking("");
    }
  };

  return (
    <main className="app-page thumbs-page">
      <AppNavigation />
      <section className="plan-content">
        <p className="eyebrow">STUDIO</p>
        <h1>Thumbnail Studio</h1>
        <p className="plan-intro">
          Design thumbnails from your own material — frames, verified stills
          and your uploads — at YouTube&apos;s 1280×720 spec. No finished
          video is required: an uploaded image is a working base immediately.
          Channel analysis below measures a real channel&apos;s thumbnails
          (palette, text, brightness) as style reference only.
        </p>

        {busy && <LoadingState className="plan-message" label="Loading the studio…" />}
        {!busy && error != null && (
          <ErrorState
            error={error}
            onRetry={() => {
              setBusy(true);
              void load();
            }}
          />
        )}
        {actionError && (
          <p className="team-error" role="alert">{actionError}</p>
        )}
        {notice && <p className="plan-meta">{notice}</p>}

        {!busy && !error && (
          <>
            <section className="panel">
              <h2>Project material</h2>
              {projects.length === 0 ? (
                <p className="plan-intro">
                  You have no projects yet. Create a project first — then its
                  stills and your uploads become thumbnail bases here, long
                  before any video exists.
                </p>
              ) : (
                <>
                  <div className="team-invite">
                    <label>
                      Project
                      <select value={projectId} onChange={(e) => void pickProject(e.target.value)}>
                        {projects.map((project) => (
                          <option key={project.id} value={project.id}>
                            {project.title || project.id}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      <Upload size={14} /> Upload a base or reference image (image file, up to 25 MB)
                      <input
                        type="file"
                        accept="image/*"
                        disabled={!projectId || working === "upload"}
                        onChange={(e) => void upload(e.target.files?.[0] || null)}
                      />
                    </label>
                  </div>
                  {view && (
                    <p className="plan-meta">
                      Bases available: {view.bases.film.frames.length} film frame(s) ·{" "}
                      {view.bases.stills.length} verified still(s) ·{" "}
                      {view.bases.uploads.length} upload(s).
                      {view.bases.film.state === "not_recorded"
                        ? " Film frames appear once a production completes; stills and uploads work now."
                        : ""}
                    </p>
                  )}
                </>
              )}
            </section>

            {view && (
              <section className="panel">
                <h2>Compose</h2>
                <div className="team-invite">
                  <label>
                    Base image
                    <select value={baseId} onChange={(e) => setBaseId(e.target.value)}>
                      {allBases.map((base) => (
                        <option key={base.base_id} value={base.base_id}>
                          {baseLabel(base)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Headline (max {view.spec.max_headline_words} words)
                    <input
                      value={headline}
                      onChange={(e) => setHeadline(e.target.value)}
                      placeholder={view.brief.headline_suggestions[0] || "Name the film in a few words"}
                    />
                  </label>
                  <label>
                    Layout
                    <select value={layout} onChange={(e) => setLayout(e.target.value)}>
                      {view.spec.layouts.map((option) => (
                        <option key={option} value={option}>
                          {LAYOUT_LABELS[option] || option}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Accent
                    <select value={accent} onChange={(e) => setAccent(e.target.value)}>
                      {view.spec.accents.map((option) => (
                        <option key={option} value={option}>{option}</option>
                      ))}
                    </select>
                  </label>
                  <span className="team-actions">
                    <button type="button" disabled={!baseId || working === "compose"} onClick={() => void compose()}>
                      {working === "compose" ? "Composing…" : "Compose thumbnail"}
                    </button>
                    <button type="button" disabled={allBases.length === 0 || working === "variants"} onClick={() => void variants()}>
                      {working === "variants" ? "Generating…" : "Generate variations"}
                    </button>
                    <button type="button" disabled={working === "suggest"} onClick={() => void suggestHeadlines()}>
                      <Wand2 size={14} aria-hidden />{" "}
                      {working === "suggest" ? "Drafting…" : "Draft headline ideas"}
                    </button>
                  </span>
                </div>
                {view.brief.headline_suggestions.length > 0 && (
                  <p className="plan-meta">
                    Recorded headline suggestions: {view.brief.headline_suggestions.join(" · ")}
                  </p>
                )}
                {proposals && (
                  proposals.length === 0 ? (
                    <p className="plan-meta">The AI returned no headline proposals for this project.</p>
                  ) : (
                    <div className="thumb-proposals">
                      <span className="plan-meta">AI headline proposals — click to use one:</span>
                      {proposals.map((proposal) => (
                        <button
                          key={proposal}
                          type="button"
                          className="team-chip"
                          onClick={() => setHeadline(proposal)}
                        >
                          {proposal}
                        </button>
                      ))}
                    </div>
                  )
                )}
              </section>
            )}

            {view && (
              <section className="panel">
                <h2>Saved thumbnails</h2>
                {view.variants.length === 0 ? (
                  <p className="plan-intro">Nothing composed yet for this project.</p>
                ) : (
                  <div className="thumb-grid">
                    {view.variants.map((variant) => (
                      <article className="thumb-card" key={variant.id}>
                        {variant.available ? (
                          <ThumbImage projectId={projectId} thumbId={variant.id} alt={variant.headline || "Thumbnail"} />
                        ) : (
                          <span className="thumb-preview thumb-preview-failed" role="img" aria-label="Image file missing">
                            <ImageOff size={16} strokeWidth={1.8} aria-hidden />
                            Recorded, but the image file is no longer on disk — regenerate it from its composition.
                          </span>
                        )}
                        <div className="thumb-card-body">
                          <h3>{variant.headline || "(no headline)"}</h3>
                          <p className="plan-meta">
                            {LAYOUT_LABELS[variant.layout] || variant.layout} · accent {variant.accent}
                            {" "}· {variant.checks.dimensions[0]}×{variant.checks.dimensions[1]}
                            {" "}· contrast {variant.checks.headline_contrast_ratio}
                          </p>
                          <div className="thumb-card-actions">
                            {variant.available && (
                              <AuthenticatedDownloadLink path={thumbnailDownloadPath(projectId, variant.id)} filename={variant.file_name}>
                                Download
                              </AuthenticatedDownloadLink>
                            )}
                            {!variant.available && (
                              <button type="button" className="button button-secondary" disabled={working === "regenerate"} onClick={() => void regenerateVariant(variant.id)}>
                                <RefreshCw size={14} aria-hidden /> Regenerate
                              </button>
                            )}
                            <button type="button" className="button button-quiet" disabled={working === "delete"} onClick={() => void removeVariant(variant.id)}>
                              <Trash2 size={14} aria-hidden /> Delete
                            </button>
                          </div>
                        </div>
                      </article>
                    ))}
                  </div>
                )}
              </section>
            )}

            <section className="panel">
              <h2>
                <Search size={16} /> Channel style analysis
              </h2>
              <p className="plan-intro">
                Paste a YouTube channel URL to measure its recent thumbnails —
                real data, measured properties. Use the patterns to inform
                your own original designs; the channel&apos;s images are
                never bases and are never copied.
              </p>
              <div className="team-invite">
                <label>
                  Channel URL
                  <input
                    value={channelUrl}
                    onChange={(e) => setChannelUrl(e.target.value)}
                    placeholder="https://www.youtube.com/@SomeChannel"
                  />
                </label>
                <span className="team-actions">
                  <button type="button" disabled={!channelUrl.trim() || working === "channel"} onClick={() => void analyseChannel()}>
                    {working === "channel" ? "Measuring…" : "Analyse channel"}
                  </button>
                </span>
              </div>
              {channelStyles && (
                <>
                  <p className="plan-meta">
                    {channelStyles.channel.title || "Channel"}
                    {channelStyles.channel.subscriber_count != null
                      ? ` · ${channelStyles.channel.subscriber_count.toLocaleString()} subscribers (as reported)`
                      : ""}
                  </p>
                  <ul className="team-people">
                    {channelStyles.videos.map((video) => (
                      <li key={video.id}>
                        <span>
                          <strong>{video.title}</strong>
                          <span className="plan-meta">
                            {video.view_count != null
                              ? ` · ${video.view_count.toLocaleString()} views`
                              : ""}
                          </span>
                        </span>
                        <span className="team-role">
                          {video.measurement ? (
                            <>
                              <Palette size={13} />{" "}
                              {video.measurement.palette
                                .slice(0, 4)
                                .map((swatch) => swatch.hex)
                                .join(" ")}
                              {video.measurement.ocr.lines.length > 0
                                ? ` · text: "${video.measurement.ocr.lines.slice(0, 2).join(" / ")}"`
                                : " · no readable text"}
                              {` · brightness ${video.measurement.luminance.mean}`}
                            </>
                          ) : (
                            video.measurement_error || "Not measured"
                          )}
                        </span>
                      </li>
                    ))}
                  </ul>
                  <p className="plan-meta">{channelStyles.label}</p>
                </>
              )}
            </section>
          </>
        )}
      </section>
    </main>
  );
}
