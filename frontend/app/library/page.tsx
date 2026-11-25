"use client";

import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { AdminOnlyGate } from "@/components/admin-only-gate";
import {
  Copy,
  FileAudio,
  Film,
  FolderPlus,
  Import,
  Search,
  Trash2,
  Upload,
} from "lucide-react";
import {
  ApiClientError,
  apiUrl,
  createLibraryFolder,
  deleteLibraryFolder,
  fetchAuthenticatedMediaObjectUrl,
  getLibraryAsset,
  getLibraryStorage,
  importLibraryAsset,
  listLibraryAssets,
  listLibraryDuplicates,
  listLibraryFolders,
  listProjects,
  setLibraryAssetOrganisation,
  streamMediaPathUrl,
  uploadToLibrary,
  type LibraryAsset,
  type LibraryAssetDetail,
  type LibraryDuplicates,
  type LibraryFolder,
  type LibraryStorage,
  type ProjectSummary,
} from "@/lib/api";

/**
 * Asset Library (Phase E, founder priority #7): the account-level
 * library over the asset ledger. Every card is a distinct piece of
 * recorded content (one content hash); its usages are the ledger
 * decisions that reference it, with verification and rights shown
 * exactly as recorded — an unknown licence stays "unknown", a
 * missing file says so, and records that cannot be keyed to a file
 * are counted in the summary rather than silently dropped.
 * Controls exist only where a real endpoint backs them: search and
 * filters query the recorded fields, folders/tags persist in the
 * account preferences, uploads go through the editor's upload
 * machinery into a home project, and "Use in project" performs the
 * provenance-carrying import.
 */

const TYPE_OPTIONS = ["video", "image", "audio", "unknown"];
const LICENCE_OPTIONS = ["recorded", "unknown"];
const VERIFICATION_OPTIONS = [
  "accepted",
  "rejected",
  "passed",
  "failed",
  "not_evaluated",
  "not_recorded",
  "recorded",
];
const DECISION_OPTIONS = [
  "selected",
  "still",
  "swapped_in",
  "rotated",
  "generated",
  "user_supplied",
  "music",
  "library_reuse",
  "verified_not_picked",
  "rejected",
  "upload",
];

function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "not recorded";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function licenceOf(rights: Record<string, unknown> | null): string {
  if (!rights) return "not recorded";
  const licence = rights.license;
  return typeof licence === "string" && licence ? licence : "unknown";
}

function LibraryThumb({
  mediaUrl,
  type,
  alt,
}: {
  mediaUrl: string | null;
  type: string;
  alt: string;
}) {
  const [media, setMedia] = useState<{ url: string; contentType: string } | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!mediaUrl || type === "audio") return;
    const controller = new AbortController();
    let objectUrl: string | null = null;
    const streamUrl = streamMediaPathUrl(mediaUrl);
    if (streamUrl) {
      fetch(apiUrl(mediaUrl), {
        method: "HEAD",
        cache: "no-store",
        credentials: "include",
        signal: controller.signal,
      })
        .then((response) => {
          if (!response.ok) throw new Error("probe failed");
          if (!controller.signal.aborted) {
            setMedia({
              url: streamUrl,
              contentType: response.headers.get("content-type") || "",
            });
          }
        })
        .catch(() => {
          if (!controller.signal.aborted) setFailed(true);
        });
    } else {
      fetchAuthenticatedMediaObjectUrl(mediaUrl, controller.signal)
        .then((result) => {
          objectUrl = result.url;
          setMedia(result);
        })
        .catch(() => {
          if (!controller.signal.aborted) setFailed(true);
        });
    }
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [mediaUrl, type]);

  if (type === "audio") {
    return (
      <span className="media-thumb" role="img" aria-label={`Audio asset ${alt}`}>
        <FileAudio size={18} strokeWidth={1.8} aria-hidden />
        <small>Audio</small>
      </span>
    );
  }
  if (!mediaUrl || failed) {
    return (
      <span className="media-thumb" role="img" aria-label={`Preview unavailable for ${alt}`}>
        <Film size={16} strokeWidth={1.8} aria-hidden />
        <small>{mediaUrl ? "Preview unavailable" : "File not on disk"}</small>
      </span>
    );
  }
  if (!media) {
    return (
      <span className="media-thumb" role="status" aria-label={`Loading preview for ${alt}`}>
        <Film size={16} strokeWidth={1.8} aria-hidden />
        <small>Loading preview…</small>
      </span>
    );
  }
  if (media.contentType.startsWith("video/")) {
    return (
      <video
        className="media-thumb"
        src={media.url}
        preload="metadata"
        muted
        playsInline
        aria-label={alt}
      />
    );
  }
  // eslint-disable-next-line @next/next/no-img-element -- authenticated blob URL; next/image cannot send the session cookie
  return <img className="media-thumb" src={media.url} alt={alt} />;
}

function AssetLibraryPageContent() {
  const [items, setItems] = useState<LibraryAsset[]>([]);
  const [total, setTotal] = useState(0);
  const [summary, setSummary] = useState<{
    library_items: number;
    library_usages: number;
    projects_covered: number;
    unkeyed_records: { decisions: number; uploads: number };
  } | null>(null);
  const [folders, setFolders] = useState<LibraryFolder[]>([]);
  const [duplicates, setDuplicates] = useState<LibraryDuplicates | null>(null);
  const [storage, setStorage] = useState<LibraryStorage | null>(null);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [detail, setDetail] = useState<LibraryAssetDetail | null>(null);

  const [busy, setBusy] = useState(true);
  const [acting, setActing] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const [query, setQuery] = useState("");
  const [projectFilter, setProjectFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [licenceFilter, setLicenceFilter] = useState("");
  const [verificationFilter, setVerificationFilter] = useState("");
  const [decisionFilter, setDecisionFilter] = useState("");
  const [folderFilter, setFolderFilter] = useState("");
  const [tagFilter, setTagFilter] = useState("");
  const [duplicatesOnly, setDuplicatesOnly] = useState(false);
  const [offset, setOffset] = useState(0);

  const [newFolderName, setNewFolderName] = useState("");
  const [uploadProject, setUploadProject] = useState("");
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [importProject, setImportProject] = useState("");
  const [tagDraft, setTagDraft] = useState("");
  const [folderDraft, setFolderDraft] = useState("");

  const PAGE_SIZE = 60;

  const load = useCallback(
    async (nextOffset: number, append: boolean) => {
      setError("");
      const [listR, foldersR, duplicatesR, storageR, projectsR] =
        await Promise.allSettled([
          listLibraryAssets({
            q: query || undefined,
            project_id: projectFilter || undefined,
            type: typeFilter || undefined,
            licence: licenceFilter || undefined,
            verification: verificationFilter || undefined,
            decision_type: decisionFilter || undefined,
            folder: folderFilter || undefined,
            tag: tagFilter || undefined,
            duplicates_only: duplicatesOnly || undefined,
            limit: PAGE_SIZE,
            offset: nextOffset,
          }),
          listLibraryFolders(),
          listLibraryDuplicates(),
          getLibraryStorage(),
          listProjects(),
        ]);
      if (listR.status === "fulfilled") {
        setItems((previous) =>
          append ? [...previous, ...listR.value.items] : listR.value.items,
        );
        setTotal(listR.value.total);
        setSummary(listR.value.summary);
        setOffset(nextOffset);
      } else {
        setError(
          listR.reason instanceof Error
            ? listR.reason.message
            : "The asset library could not be loaded.",
        );
      }
      if (foldersR.status === "fulfilled") setFolders(foldersR.value.folders);
      if (duplicatesR.status === "fulfilled") setDuplicates(duplicatesR.value);
      if (storageR.status === "fulfilled") setStorage(storageR.value);
      if (projectsR.status === "fulfilled") {
        setProjects(projectsR.value.projects || []);
      }
      setBusy(false);
    },
    [
      query,
      projectFilter,
      typeFilter,
      licenceFilter,
      verificationFilter,
      decisionFilter,
      folderFilter,
      tagFilter,
      duplicatesOnly,
    ],
  );

  useEffect(() => {
    const timer = setTimeout(() => {
      void load(0, false);
    }, 0);
    return () => clearTimeout(timer);
  }, [load]);

  async function act(key: string, work: () => Promise<string>) {
    setActing(key);
    setMessage("");
    try {
      const note = await work();
      setMessage(note);
      await load(0, false);
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

  async function openDetail(asset: LibraryAsset) {
    setDetail(null);
    setTagDraft(asset.tags.join(", "));
    setFolderDraft(asset.folder_id || "");
    try {
      const loaded = await getLibraryAsset(asset.content_hash);
      setDetail(loaded);
      setTagDraft(loaded.tags.join(", "));
      setFolderDraft(loaded.folder_id || "");
    } catch (cause) {
      setMessage(
        cause instanceof Error ? cause.message : "The asset could not be loaded.",
      );
    }
  }

  const unkeyed = summary
    ? summary.unkeyed_records.decisions + summary.unkeyed_records.uploads
    : 0;

  return (
    <main className="app-page library-page">
      <AppNavigation />
      <section className="research-content">
        <p className="eyebrow">ASSET LIBRARY</p>
        <h1>Asset Library</h1>
        <p className="research-intro">
          Every recorded asset across your projects, with its verdicts and rights exactly as recorded.
        </p>
        {message && <p className="research-message">{message}</p>}
        {error && <p className="research-message research-error">{error}</p>}

        <div className="research-summary">
          <div>
            <span>Library items</span>
            <strong>{summary ? summary.library_items : "—"}</strong>
          </div>
          <div>
            <span>Recorded usages</span>
            <strong>{summary ? summary.library_usages : "—"}</strong>
          </div>
          <div>
            <span>Projects covered</span>
            <strong>{summary ? summary.projects_covered : "—"}</strong>
          </div>
          <div>
            <span>Media on disk</span>
            <strong>{storage ? formatBytes(storage.total_bytes) : "—"}</strong>
          </div>
          <div>
            <span>Duplicate copies</span>
            <strong>
              {duplicates ? formatBytes(duplicates.total_reclaimable_bytes) : "—"}
            </strong>
          </div>
        </div>
        {summary && unkeyed > 0 && (
          <p className="research-note">
            {unkeyed} ledger/upload record{unkeyed === 1 ? "" : "s"} (
            {summary.unkeyed_records.decisions} decisions,{" "}
            {summary.unkeyed_records.uploads} uploads) have no resolvable media
            file and no recorded content hash, so they cannot be keyed into the
            library; they remain visible on their projects&apos; asset pages.
          </p>
        )}

        <section className="research-panel">
          <h2>
            <Search size={17} aria-hidden /> Browse and search
          </h2>
          <form
            className="research-form"
            onSubmit={(event) => {
              event.preventDefault();
              void load(0, false);
            }}
          >
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search title, file name, provider, source URL, tag…"
              aria-label="Search the library"
            />
            <select
              value={projectFilter}
              onChange={(event) => setProjectFilter(event.target.value)}
              aria-label="Filter by project"
            >
              <option value="">All projects</option>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.title || project.id}
                </option>
              ))}
            </select>
            <select
              value={typeFilter}
              onChange={(event) => setTypeFilter(event.target.value)}
              aria-label="Filter by type"
            >
              <option value="">Any type</option>
              {TYPE_OPTIONS.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
            <select
              value={licenceFilter}
              onChange={(event) => setLicenceFilter(event.target.value)}
              aria-label="Filter by licence state"
            >
              <option value="">Any licence state</option>
              {LICENCE_OPTIONS.map((option) => (
                <option key={option} value={option}>
                  licence {option}
                </option>
              ))}
            </select>
            <select
              value={verificationFilter}
              onChange={(event) => setVerificationFilter(event.target.value)}
              aria-label="Filter by verification state"
            >
              <option value="">Any verification</option>
              {VERIFICATION_OPTIONS.map((option) => (
                <option key={option} value={option}>
                  {option.replaceAll("_", " ")}
                </option>
              ))}
            </select>
            <select
              value={decisionFilter}
              onChange={(event) => setDecisionFilter(event.target.value)}
              aria-label="Filter by decision type"
            >
              <option value="">Any decision</option>
              {DECISION_OPTIONS.map((option) => (
                <option key={option} value={option}>
                  {option.replaceAll("_", " ")}
                </option>
              ))}
            </select>
            <select
              value={folderFilter}
              onChange={(event) => setFolderFilter(event.target.value)}
              aria-label="Filter by folder"
            >
              <option value="">All folders</option>
              <option value="unfiled">Unfiled</option>
              {folders.map((folder) => (
                <option key={folder.id} value={folder.id}>
                  {folder.name}
                </option>
              ))}
            </select>
            <label className="research-note">
              <input
                type="checkbox"
                checked={duplicatesOnly}
                onChange={(event) => setDuplicatesOnly(event.target.checked)}
              />{" "}
              Duplicates only
            </label>
            {tagFilter && (
              <button
                type="button"
                className="button button-quiet"
                onClick={() => setTagFilter("")}
              >
                Clear tag filter: {tagFilter}
              </button>
            )}
            <button type="submit" className="button button-primary" disabled={busy}>
              Search
            </button>
          </form>

          {busy ? (
            <p className="research-note">Loading the library…</p>
          ) : items.length === 0 ? (
            <p className="research-note">
              No assets match. Assets appear here as your productions record them
              in the ledger, and when you upload media.
            </p>
          ) : (
            <>
              <div className="library-grid">
                {items.map((asset) => (
                  <button
                    key={asset.content_hash}
                    type="button"
                    className="library-card"
                    aria-pressed={detail?.content_hash === asset.content_hash}
                    onClick={() => void openDetail(asset)}
                  >
                    <LibraryThumb
                      mediaUrl={asset.media_url}
                      type={asset.type}
                      alt={asset.title}
                    />
                    <span className="library-card-title">{asset.title}</span>
                    {!asset.title_recorded && (
                      <span className="research-note">title from file name</span>
                    )}
                    <span className="research-meta">
                      <span className="status-badge status-badge--neutral">
                        {asset.type}
                      </span>
                      <span
                        className={`status-badge ${
                          asset.licence_state === "recorded"
                            ? "status-badge--success"
                            : "status-badge--attention"
                        }`}
                      >
                        licence: {licenceOf(asset.rights)}
                      </span>
                      {asset.verification_states.map((state) => (
                        <span key={state} className="status-badge status-badge--neutral">
                          {state.replaceAll("_", " ")}
                        </span>
                      ))}
                      {asset.duplicate && (
                        <span className="status-badge status-badge--attention">
                          <Copy size={11} aria-hidden /> {asset.copies} copies
                        </span>
                      )}
                      {!asset.available && (
                        <span className="status-badge status-badge--danger">
                          file missing
                        </span>
                      )}
                    </span>
                    <span className="research-meta">
                      <span>{formatBytes(asset.size_bytes)}</span>
                      <span>
                        {asset.usage_count} usage{asset.usage_count === 1 ? "" : "s"}
                      </span>
                      <span>
                        {asset.projects.map((p) => p.title || p.id).join(", ")}
                      </span>
                    </span>
                    {asset.tags.length > 0 && (
                      <span className="research-meta">
                        {asset.tags.map((tag) => (
                          <span key={tag} className="status-badge status-badge--neutral">
                            {tag}
                          </span>
                        ))}
                      </span>
                    )}
                  </button>
                ))}
              </div>
              {items.length < total && (
                <button
                  type="button"
                  className="button button-secondary"
                  onClick={() => void load(offset + PAGE_SIZE, true)}
                >
                  Load more ({total - items.length} remaining)
                </button>
              )}
            </>
          )}
        </section>

        {detail && (
          <section className="research-panel">
            <h2>Provenance — {detail.title}</h2>
            <p className="research-note">
              Content hash {detail.content_hash} · {formatBytes(detail.size_bytes)}{" "}
              · home project {detail.home_project?.title || "—"}
              {detail.home_media_key
                ? ` · also served by the project media index (${detail.home_media_key})`
                : ""}
            </p>

            <h3>Recorded usages</h3>
            {detail.usages.length === 0 ? (
              <p className="research-note">
                No ledger usages — this item entered the library through an upload.
              </p>
            ) : (
              <div className="research-list">
                {detail.usages.map((usage) => (
                  <article key={usage.decision_id} className="research-row">
                    <div className="research-row-main">
                      <strong>
                        {usage.project.title || usage.project.id} —{" "}
                        {usage.decision_type.replaceAll("_", " ")}
                      </strong>
                      <div className="research-meta">
                        <span>run {usage.run_id.slice(0, 8)}…</span>
                        {usage.scene_number !== null && (
                          <span>scene {usage.scene_number}</span>
                        )}
                        {usage.sentence_number !== null && (
                          <span>sentence {usage.sentence_number}</span>
                        )}
                        {usage.evidence_class && (
                          <span>evidence: {usage.evidence_class}</span>
                        )}
                        <span
                          className={`status-badge ${
                            usage.verification_state === "accepted" ||
                            usage.verification_state === "passed"
                              ? "status-badge--success"
                              : usage.verification_state === "rejected" ||
                                  usage.verification_state === "failed"
                                ? "status-badge--danger"
                                : "status-badge--neutral"
                          }`}
                        >
                          verification: {usage.verification_state.replaceAll("_", " ")}
                        </span>
                        <span>licence: {licenceOf(usage.rights)}</span>
                        {usage.provider && <span>via {usage.provider}</span>}
                        {!usage.file_present && <span>file not on disk</span>}
                      </div>
                      {usage.source_url && (
                        <p className="research-snippet">
                          Source:{" "}
                          <a href={usage.source_url} target="_blank" rel="noreferrer">
                            {usage.source_url}
                          </a>
                        </p>
                      )}
                      {usage.previous_decision_id && (
                        <p className="research-snippet">
                          Linked to earlier decision {usage.previous_decision_id} —
                          the ledger&apos;s recorded provenance chain.
                        </p>
                      )}
                      {usage.verification && (
                        <details className="library-verbatim">
                          <summary>Recorded verification verdict</summary>
                          <pre>{JSON.stringify(usage.verification, null, 1)}</pre>
                        </details>
                      )}
                      {usage.rights && (
                        <details className="library-verbatim">
                          <summary>Recorded rights</summary>
                          <pre>{JSON.stringify(usage.rights, null, 1)}</pre>
                        </details>
                      )}
                    </div>
                  </article>
                ))}
              </div>
            )}

            {detail.upload_sightings.length > 0 && (
              <>
                <h3>Upload records</h3>
                <div className="research-list">
                  {detail.upload_sightings.map((sighting, index) => (
                    <article
                      key={`${sighting.project_id}-${sighting.upload_id || index}`}
                      className="research-row"
                    >
                      <div className="research-row-main">
                        <strong>
                          {sighting.title || sighting.file_name || "Upload"} —{" "}
                          {sighting.project.title || sighting.project.id}
                        </strong>
                        <div className="research-meta">
                          <span>licence: {licenceOf(sighting.rights)}</span>
                          {sighting.library_provenance && (
                            <span>
                              imported from project{" "}
                              {String(
                                sighting.library_provenance.source_project_id || "—",
                              )}
                            </span>
                          )}
                        </div>
                      </div>
                    </article>
                  ))}
                </div>
              </>
            )}

            <h3>Organise</h3>
            <form
              className="research-form"
              onSubmit={(event) => {
                event.preventDefault();
                void act("organise", async () => {
                  await setLibraryAssetOrganisation(detail.content_hash, {
                    tags: tagDraft
                      .split(",")
                      .map((tag) => tag.trim())
                      .filter(Boolean),
                    folder_id: folderDraft || null,
                    folder_set: true,
                  });
                  const fresh = await getLibraryAsset(detail.content_hash);
                  setDetail(fresh);
                  return "Organisation saved.";
                });
              }}
            >
              <input
                value={tagDraft}
                onChange={(event) => setTagDraft(event.target.value)}
                placeholder="Tags, comma separated"
                aria-label="Tags"
              />
              <select
                value={folderDraft}
                onChange={(event) => setFolderDraft(event.target.value)}
                aria-label="Folder"
              >
                <option value="">Unfiled</option>
                {folders.map((folder) => (
                  <option key={folder.id} value={folder.id}>
                    {folder.name}
                  </option>
                ))}
              </select>
              <button
                type="submit"
                className="button button-secondary"
                disabled={acting !== ""}
              >
                Save
              </button>
            </form>

            <h3>Use in another project</h3>
            <p className="research-note">
              The import copies the file into the target project&apos;s uploads and
              records a library-reuse decision there, linked to the source decision
              above with its rights and verification carried across. The target
              needs a completed production or a plan awaiting approval — the same
              contexts uploads attach to.
            </p>
            <form
              className="research-form"
              onSubmit={(event) => {
                event.preventDefault();
                if (!importProject) return;
                void act("import", async () => {
                  const result = await importLibraryAsset(
                    detail.content_hash,
                    importProject,
                  );
                  const target = projects.find((p) => p.id === importProject);
                  return result.pre_run
                    ? `Imported into ${target?.title || importProject} for its plan review; it will be recorded in the ledger when the production runs.`
                    : `Imported into ${target?.title || importProject}; the reuse is recorded in that project's ledger and certificate.`;
                });
              }}
            >
              <select
                value={importProject}
                onChange={(event) => setImportProject(event.target.value)}
                aria-label="Target project"
              >
                <option value="">Choose a project…</option>
                {projects
                  .filter((project) => project.id !== detail.home_project?.id)
                  .map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.title || project.id}
                    </option>
                  ))}
              </select>
              <button
                type="submit"
                className="button button-primary"
                disabled={acting !== "" || !importProject || !detail.available}
              >
                <Import size={14} aria-hidden /> Use in project
              </button>
              {!detail.available && (
                <span className="research-note">
                  This asset&apos;s file is not on disk, so it cannot be imported.
                </span>
              )}
            </form>
          </section>
        )}

        <section className="research-panel">
          <h2>
            <Upload size={17} aria-hidden /> Upload once, use everywhere
          </h2>
          <p className="research-note">
            An upload is stored in a home project through the editor&apos;s upload
            machinery (probed, size-capped, recorded as user-supplied with licence
            unknown) and becomes a library item immediately — import it into other
            projects from its card above.
          </p>
          <form
            className="research-form"
            onSubmit={(event) => {
              event.preventDefault();
              if (!uploadProject || !uploadFile) return;
              void act("upload", async () => {
                await uploadToLibrary(uploadProject, uploadFile);
                setUploadFile(null);
                return "Uploaded — it is now in the library.";
              });
            }}
          >
            <select
              value={uploadProject}
              onChange={(event) => setUploadProject(event.target.value)}
              aria-label="Home project for the upload"
            >
              <option value="">Choose the home project…</option>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.title || project.id}
                </option>
              ))}
            </select>
            <input
              type="file"
              accept="video/*,image/*,audio/*"
              onChange={(event) => setUploadFile(event.target.files?.[0] || null)}
              aria-label="Media file"
            />
            <button
              type="submit"
              className="button button-primary"
              disabled={acting !== "" || !uploadProject || !uploadFile}
            >
              Upload to library
            </button>
          </form>
        </section>

        <section className="research-panel">
          <h2>
            <FolderPlus size={17} aria-hidden /> Folders
          </h2>
          <form
            className="research-form"
            onSubmit={(event) => {
              event.preventDefault();
              if (!newFolderName.trim()) return;
              void act("folder", async () => {
                await createLibraryFolder(newFolderName.trim());
                setNewFolderName("");
                return "Folder created.";
              });
            }}
          >
            <input
              value={newFolderName}
              onChange={(event) => setNewFolderName(event.target.value)}
              placeholder="New folder name"
              aria-label="New folder name"
            />
            <button
              type="submit"
              className="button button-secondary"
              disabled={acting !== "" || !newFolderName.trim()}
            >
              Create folder
            </button>
          </form>
          {folders.length === 0 ? (
            <p className="research-note">No folders yet.</p>
          ) : (
            <div className="research-list">
              {folders.map((folder) => (
                <article key={folder.id} className="research-row">
                  <div className="research-row-main">
                    <strong>{folder.name}</strong>
                    <div className="research-meta">
                      <span>
                        {folder.item_count} item{folder.item_count === 1 ? "" : "s"}
                      </span>
                    </div>
                  </div>
                  <div className="research-row-actions">
                    <button
                      type="button"
                      className="button button-quiet"
                      disabled={acting !== ""}
                      onClick={() =>
                        void act("folder-delete", async () => {
                          await deleteLibraryFolder(folder.id);
                          return `Folder “${folder.name}” deleted; its items are unfiled, not deleted.`;
                        })
                      }
                    >
                      <Trash2 size={13} aria-hidden /> Delete folder
                    </button>
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>

        <section className="research-panel">
          <h2>
            <Copy size={17} aria-hidden /> Duplicates
          </h2>
          <p className="research-note">
            The same content (identical sha256) stored at more than one path.
            Reclaimable bytes assume one copy is kept; nothing here deletes
            anything.
          </p>
          {!duplicates || duplicates.duplicates.length === 0 ? (
            <p className="research-note">No duplicated content in your library.</p>
          ) : (
            <div className="research-list">
              {duplicates.duplicates.map((entry) => (
                <article key={entry.content_hash} className="research-row">
                  <div className="research-row-main">
                    <strong>{entry.title}</strong>
                    <div className="research-meta">
                      <span>{entry.copies.length} copies</span>
                      <span>{formatBytes(entry.size_bytes)} each</span>
                      <span>
                        {formatBytes(entry.reclaimable_bytes)} reclaimable
                      </span>
                    </div>
                    {entry.copies.map((copy) => (
                      <p key={copy.file_path} className="research-snippet">
                        {copy.project.title || copy.project.id}: {copy.file_path}
                      </p>
                    ))}
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>

        <section className="research-panel">
          <h2>Storage</h2>
          <p className="research-note">
            Per-project figures are the media tree&apos;s own accounting (the same
            numbers the admin storage view reports), restricted to your projects.
            The by-type figures cover library items&apos; files, counted once per
            distinct content.
          </p>
          {storage && (
            <>
              <div className="research-list">
                {storage.projects.map((project) => (
                  <article key={project.project_id} className="research-row">
                    <div className="research-row-main">
                      <strong>{project.title || project.project_id}</strong>
                      <div className="research-meta">
                        <span>{formatBytes(project.bytes)}</span>
                        <span>{project.files} files</span>
                        {!project.in_media_tree && <span>no media directory</span>}
                      </div>
                    </div>
                  </article>
                ))}
              </div>
              <h3>Library content by type</h3>
              <div className="research-meta">
                {Object.entries(storage.library_by_type).map(([type, bucket]) => (
                  <span key={type} className="status-badge status-badge--neutral">
                    {type}: {bucket.items} items · {formatBytes(bucket.bytes)}
                  </span>
                ))}
              </div>
            </>
          )}
        </section>
      </section>
    </main>
  );
}

/**
 * FS-1: the Library is an admin-only presentational surface (FS-C).
 * The page renders only behind AdminOnlyGate (role from
 * /auth/me, the AdminShell pattern); a non-admin direct URL gets
 * the plain not-found view and the content never mounts. The
 * backend endpoints enforce the same rule server-side.
 */
export default function AssetLibraryPage() {
  return (
    <AdminOnlyGate>
      <AssetLibraryPageContent />
    </AdminOnlyGate>
  );
}
