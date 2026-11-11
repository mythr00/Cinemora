"use client";

import Image from "next/image";
import Link from "next/link";
import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";

const API_URL = (process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
const categories = [
  "Documentary",
  "History",
  "Science",
  "Biography",
  "True Crime",
  "Naval History",
  "War History",
  "Finance",
  "Custom",
];

const stageOrder = [
  ["voice_actor", "Narration"],
  ["script_analysis", "Story analysis"],
  ["asset_manager", "Visual research"],
  ["motion_designer", "Motion design"],
  ["sound_designer", "Sound mix"],
  ["timeline_builder", "Timeline"],
  ["renderer", "Render"],
  ["video_editor", "Final edit"],
] as const;

type MusicTrack = {
  id: string;
  title: string;
  mood: string;
  preview_url: string;
};

type BackgroundLook = {
  id: string;
  title: string;
  description: string;
  swatch: string;
  preview: string;
};

type NarrationVoice = { id: string; name: string; installed: boolean; locale?: string; quality?: string; license_url?: string; voice_type?: string; provider?: string };
type NarrationLanguage = { code: string; name: string };

type ProjectStatus = {
  status: string;
  current_stage?: string | null;
  error?: string | null;
  video_url?: string | null;
  stages?: Record<string, { status?: string; error?: string | null }>;
};

export default function Home() {
  const [script, setScript] = useState("");
  const [category, setCategory] = useState("Documentary");
  const [loading, setLoading] = useState(false);
  const [projectId, setProjectId] = useState("");
  const [jobId, setJobId] = useState("");
  const [status, setStatus] = useState<ProjectStatus | null>(null);
  const [message, setMessage] = useState("");
  const [apiOnline, setApiOnline] = useState<boolean | null>(null);
  const [musicTracks, setMusicTracks] = useState<MusicTrack[]>([]);
  const [selectedMusic, setSelectedMusic] = useState("auto");
  const [includeMusic, setIncludeMusic] = useState(true);
  const [includeMotion, setIncludeMotion] = useState(false);
  const [musicPercent, setMusicPercent] = useState(3);
  const [includeCaptions, setIncludeCaptions] = useState(true);
  const [includeSfx, setIncludeSfx] = useState(true);
  const [voiceFile, setVoiceFile] = useState<File | null>(null);
  const [voiceTranscribed, setVoiceTranscribed] = useState(false);
  const [useUploadedVoice, setUseUploadedVoice] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [musicLoadError, setMusicLoadError] = useState("");
  const [backgrounds, setBackgrounds] = useState<BackgroundLook[]>([]);
  const [backgroundId, setBackgroundId] = useState("midnight");
  const [voiceCatalog, setVoiceCatalog] = useState<NarrationVoice[]>([]);
  const [narrationLanguages, setNarrationLanguages] = useState<NarrationLanguage[]>([]);
  const [voiceId, setVoiceId] = useState("en_US-lessac-medium");
  const [selectedLanguage, setSelectedLanguage] = useState("auto");
  const [useCustomLanguage, setUseCustomLanguage] = useState(false);
  const [customLanguage, setCustomLanguage] = useState("");
  const [voiceSearch, setVoiceSearch] = useState("");
  const [voicePreviewUrl, setVoicePreviewUrl] = useState("");
  const [thumbnailTitle, setThumbnailTitle] = useState("");
  const [thumbnailUrl, setThumbnailUrl] = useState("");
  const [thumbnailBusy, setThumbnailBusy] = useState(false);

  const words = useMemo(() => script.trim().split(/\s+/).filter(Boolean).length, [script]);
  const duration = words ? Math.max(1, Math.round(words / 145)) : 0;
  const isTerminal = status?.status === "completed" || status?.status === "failed";

  useEffect(() => {
    const controller = new AbortController();
    fetch(`${API_URL}/render-options/defaults`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) return;
        const defaults = await response.json() as { graphics?: boolean; music?: boolean; music_percent?: number; captions?: boolean; sfx?: boolean };
        setIncludeMotion(defaults.graphics ?? false);
        setIncludeMusic(defaults.music ?? true);
        setMusicPercent(Math.min(100, Math.max(0, defaults.music_percent ?? 3)));
        setIncludeCaptions(defaults.captions ?? true);
        setIncludeSfx(defaults.sfx ?? true);
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const draftTimer = setTimeout(() => {
      const channelDraft = sessionStorage.getItem("cinemora-script-draft");
      if (channelDraft) {
        setScript(channelDraft.slice(0, 50000));
        sessionStorage.removeItem("cinemora-script-draft");
        setMessage("Script draft loaded. Review it before starting production.");
      }
    }, 0);
    fetch(`${API_URL}/health`, { signal: controller.signal })
      .then((response) => setApiOnline(response.ok))
      .catch(() => setApiOnline(false));
    return () => { controller.abort(); clearTimeout(draftTimer); };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    fetch(`${API_URL}/voices`, { signal: controller.signal })
      .then(async (response) => {
        const data = await response.json() as { voices?: NarrationVoice[]; catalog?: NarrationVoice[]; languages?: NarrationLanguage[] };
        if (!response.ok) throw new Error("Could not load installed voices.");
        setVoiceCatalog(data.catalog || []);
        setNarrationLanguages(data.languages || []);
        if (data.voices?.length) setVoiceId((current) => data.voices!.some((voice) => voice.id === current) ? current : data.voices![0].id);
      })
      .catch(() => { if (!controller.signal.aborted) setMessage("Installed narration voices could not be loaded."); });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    fetch(`${API_URL}/backgrounds`, { signal: controller.signal })
      .then(async (response) => {
        const data = await response.json() as { backgrounds?: BackgroundLook[] };
        if (!response.ok) throw new Error("Could not load background looks.");
        setBackgrounds(data.backgrounds || []);
      })
      .catch(() => {
        if (!controller.signal.aborted) setMessage("Background looks could not be loaded.");
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    fetch(`${API_URL}/music`, { signal: controller.signal })
      .then(async (response) => {
        const data = await response.json() as { detail?: string; tracks?: MusicTrack[] };
        if (!response.ok) throw new Error(data.detail || "Could not load music tracks.");
        setMusicTracks(data.tracks || []);
      })
      .catch(() => {
        if (!controller.signal.aborted) setMusicLoadError("Music previews are unavailable. Generation can continue with narration only unless you select an available track.");
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    document.querySelectorAll<HTMLAudioElement>(".music-card audio").forEach((audio) => { audio.volume = musicPercent / 100; });
  }, [musicPercent, musicTracks]);

  useEffect(() => {
    if (!projectId || isTerminal) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;

    async function pollStatus() {
      try {
        const response = await fetch(`${API_URL}/projects/${projectId}/status`, { cache: "no-store" });
        const data = (await response.json()) as ProjectStatus;
        if (!response.ok) throw new Error((data as ProjectStatus & { detail?: string }).detail || "Could not read project status.");
        if (stopped) return;
        setStatus(data);
        if (data.status === "completed" && data.video_url) {
          setMessage("Your documentary is ready to review.");
        } else if (data.status === "failed") {
          setMessage(data.error || "Production stopped before completion.");
        } else {
          setMessage(data.current_stage ? `${stageLabel(data.current_stage)} in progress` : "Waiting for a worker to start production");
        }
      } catch (error) {
        if (!stopped) setMessage(error instanceof Error ? error.message : "Could not read project status.");
      }
      if (!stopped) timer = setTimeout(pollStatus, 3000);
    }

    void pollStatus();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [projectId, isTerminal]);

  async function createProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!script.trim()) {
      setMessage("Add a script before starting production.");
      return;
    }

    const outputLanguage = useCustomLanguage ? customLanguage.trim() : selectedLanguage;
    if (useCustomLanguage && !/^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/.test(outputLanguage)) {
      setMessage("Enter a valid BCP-47 language code, such as sw, am, or zh-Hant.");
      return;
    }

    setLoading(true);
    setProjectId("");
    setJobId("");
    setStatus(null);
    setMessage("Creating your project…");

    try {
      const createResponse = await fetch(`${API_URL}/projects`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ script: script.trim(), category, render_options: { schema_version: 1, graphics: includeMotion, music: includeMusic, music_percent: musicPercent, captions: includeCaptions, sfx: includeSfx, music_track_id: selectedMusic, voice_id: voiceId, format: "mp4", language: outputLanguage } }),
      });
      const created = await createResponse.json();
      if (!createResponse.ok) throw new Error(created.detail || "Could not create the project.");

      setProjectId(created.id);
      setMessage("Project created. Adding it to the production queue…");
      if (useUploadedVoice && voiceFile && voiceTranscribed) {
        const voiceBody = new FormData();
        voiceBody.append("file", voiceFile);
        const voiceResponse = await fetch(`${API_URL}/projects/${created.id}/voiceover`, { method: "POST", body: voiceBody });
        const voiceResult = await voiceResponse.json();
        if (!voiceResponse.ok) throw new Error(voiceResult.detail || "Could not attach your voice recording to the project.");
      }
      const renderOptions = { schema_version: 1, graphics: includeMotion, music: includeMusic, music_percent: musicPercent, captions: includeCaptions, sfx: includeSfx, music_track_id: selectedMusic, voice_id: voiceId, format: "mp4", language: outputLanguage };
      const startResponse = await fetch(`${API_URL}/projects/${created.id}/analyze?background_id=${encodeURIComponent(backgroundId)}&use_uploaded_voice=${useUploadedVoice && voiceTranscribed}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(renderOptions) });
      const started = await startResponse.json();
      if (!startResponse.ok) throw new Error(started.detail || "The project was created, but production could not be started.");

      setJobId(started.job_id || "");
      setMessage("Project created and added to the production queue.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not start documentary production.");
    } finally {
      setLoading(false);
    }
  }

  async function transcribeVoiceFile() {
    if (!voiceFile) return;
    setTranscribing(true);
    setMessage("Transcribing your voice recording…");
    try {
      const body = new FormData();
      body.append("file", voiceFile);
      const response = await fetch(`${API_URL}/transcribe-upload`, { method: "POST", body });
      const data = await response.json() as { transcript?: string; language?: string; detail?: string };
      if (!response.ok) throw new Error(data.detail || "Transcription failed.");
      setScript(data.transcript || "");
      setVoiceTranscribed(true);
      setUseUploadedVoice(true);
      setMessage(`Transcript ready${data.language ? ` (${data.language})` : ""}. Review it, then create your documentary.`);
      document.getElementById("documentary-script")?.scrollIntoView({ behavior: "smooth", block: "center" });
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Transcription failed.");
    } finally {
      setTranscribing(false);
    }
  }

  async function createThumbnail() {
    if (!projectId || !thumbnailTitle.trim()) return;
    setThumbnailBusy(true);
    setMessage("Creating a thumbnail from a frame in your finished video…");
    try {
      const response = await fetch(`${API_URL}/projects/${projectId}/thumbnail`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: thumbnailTitle.trim(), background_id: backgroundId }),
      });
      const data = await response.json() as { detail?: string; thumbnail_url?: string };
      if (!response.ok) throw new Error(data.detail || "Could not create thumbnail.");
      setThumbnailUrl(`${API_URL}${data.thumbnail_url}?v=${Date.now()}`);
      setMessage("Thumbnail created from your selected video frame.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not create thumbnail.");
    } finally {
      setThumbnailBusy(false);
    }
  }

  function previewVoice(id: string) {
    setVoiceId(id);
    setVoicePreviewUrl(`${API_URL}/voices/preview?voice_id=${encodeURIComponent(id)}`);
  }

  const effectiveLanguage = useCustomLanguage ? (customLanguage.trim().split(/[-_]/, 1)[0].toLowerCase() || "auto") : selectedLanguage;
  const filteredVoices = voiceCatalog.filter((voice) =>
    (effectiveLanguage === "auto" || voice.locale === effectiveLanguage || voice.locale?.split("_", 1)[0] === effectiveLanguage || voice.locale === "multilingual")
    && `${voice.name} ${voice.id}`.toLowerCase().includes(voiceSearch.toLowerCase())
  ).slice(0, 12);

  const videoUrl = status?.video_url ? new URL(status.video_url, `${API_URL}/`).toString() : "";

  return (
    <main className="studio-shell">
      <aside className="sidebar">
        <Link className="brand" href="/" aria-label="Cinemora home">
          <Image className="cinemora-wordmark" src="/cinemora-logo.png" alt="CINEMORA" width={190} height={50} priority />
        </Link>

        <div className="side-label">WORKSPACE</div>
        <nav className="side-nav" aria-label="Main navigation">
          <Link className="nav-item active" href="/create"><Icon name="spark" /><span>Create</span><span className="nav-active-dot" /></Link>
          <Link className="nav-item" href="/studio"><Icon name="grid" /><span>Overview</span></Link>
          <Link className="nav-item" href="/projects"><Icon name="grid" /><span>Projects</span></Link>
          <a className="nav-item" href="#thumbnail-generator"><Icon name="image" /><span>Thumbnail generator</span></a>
          <a className="nav-item" href="/channel-lab"><Icon name="wand" /><span>Script</span></a>
          <a className="nav-item" href="/thumbnail"><Icon name="image" /><span>Thumbnail</span></a>
          <Link className="nav-item" href="/viral-clips"><Icon name="film" /><span>Short clips</span></Link>
        </nav>

        <div className="sidebar-spacer" />
        <div className="sidebar-note">
          <span className="note-icon"><Icon name="wand" /></span>
          <div><strong>Production tools</strong><p>Script, research, narration, and edit.</p></div>
        </div>
        <div className="connection"><span className={`connection-dot ${apiOnline === false ? "offline" : ""}`} /><span>{apiOnline === null ? "Connecting to studio" : apiOnline ? "Studio API connected" : "Studio API unavailable"}</span></div>
        <div className="sidebar-footer"><span className="avatar"><Image src="/cinemora-symbol.png" alt="" width={25} height={25} /></span><span><strong>Cinemora</strong><small>Production workspace</small></span></div>
      </aside>

      <section className="workspace" id="create">
        <header className="topbar">
          <div className="breadcrumbs"><span>Cinemora</span><span className="crumb-separator">/</span><strong>New documentary</strong></div>
          <div className="topbar-right"><span className="draft-status"><span /> New project</span><span className="edition-label">STUDIO EDITION</span></div>
        </header>

        <div className="content-wrap">
          <div className="page-heading">
          <div><div className="eyebrow">DOCUMENTARY STUDIO <span className="eyebrow-line" /></div><h1>New documentary</h1><p>Script, visual research, narration, and final edit.</p></div>
          </div>

          <form className="create-layout" onSubmit={createProject}>
            <div className="main-column">
              <section className="panel script-panel" aria-labelledby="script-title">
                <div className="panel-heading">
                  <div className="step-marker">01</div>
                  <div className="panel-title"><h2 id="script-title">Script</h2><p>Paste the narration you want produced.</p></div>
                  <span className="required-tag">{words.toLocaleString()} / 50,000</span>
                </div>
                <label className="sr-only" htmlFor="documentary-script">Documentary script</label>
                <textarea id="documentary-script" value={script} onChange={(event) => setScript(event.target.value)} maxLength={50000} placeholder="Paste script…" />
                <div className="editor-footer"><span className="editor-hint">{words.toLocaleString()} words</span><span className="count">{script.length.toLocaleString()} / 50,000 characters</span></div>
                <div className="voice-upload-row"><label className="field-label" htmlFor="voice-upload">OR UPLOAD YOUR NARRATION<input id="voice-upload" type="file" accept="audio/*,video/mp4,video/quicktime,video/webm,video/x-matroska" onChange={(event) => { setVoiceFile(event.target.files?.[0] || null); setVoiceTranscribed(false); setUseUploadedVoice(false); }} /></label><button type="button" className="thumbnail-button" disabled={!voiceFile || transcribing} onClick={() => void transcribeVoiceFile()}>{transcribing ? "Transcribing…" : voiceTranscribed ? "Transcribe again" : "Transcribe into script"}</button><small>Transcribe, then choose whether the final video uses this original recording or generated narration.</small>{voiceTranscribed && <label className="motion-toggle"><input type="checkbox" checked={useUploadedVoice} onChange={(event) => setUseUploadedVoice(event.target.checked)} /><span><strong>Use my original voice recording</strong><small>Use your uploaded audio as the narration track.</small></span></label>}</div>
              </section>

              <section className="panel details-panel">
                <div className="panel-heading compact-heading">
                  <div className="step-marker">02</div>
                  <div className="panel-title"><h2>Project details</h2><p>Choose a category and check the estimated runtime.</p></div>
                </div>
                <div className="form-fields">
                  <label className="field-label" htmlFor="category">STORY CATEGORY
                    <span className="select-wrap"><select id="category" value={category} onChange={(event) => setCategory(event.target.value)}>{categories.map((item) => <option key={item}>{item}</option>)}</select><Icon name="chevron" /></span>
                  </label>
                  <div className="field-label length-field"><span>ESTIMATED RUNTIME</span><div className="runtime-value">{duration ? `~${duration} min` : "Add a script"}</div><small>Based on about 145 spoken words per minute.</small></div>
                </div>
                <div className="workflow-includes"><span className="include-label">INCLUDED IN YOUR WORKFLOW</span><div className="include-chips"><span><Icon name="search" /> Story research</span><span><Icon name="film" /> Matched visuals</span><span><Icon name="mic" /> Narration + captions</span></div></div>
              </section>

              <section className="panel voice-panel" aria-labelledby="voice-title">
                <div className="panel-heading compact-heading"><div className="step-marker">03</div><div className="panel-title"><h2 id="voice-title">Narration voice</h2><p>Choose a voice now; if its model is not present yet, the worker fetches it automatically when production starts.</p></div></div>
                <label className="field-label" htmlFor="narration-voice">SELECTED VOICE
                  <span className="select-wrap"><select id="narration-voice" value={voiceId} onChange={(event) => { const voice = voiceCatalog.find((item) => item.id === event.target.value); setVoiceId(event.target.value); if (voice?.locale && voice.locale !== "multilingual") setSelectedLanguage(voice.locale.split("_", 1)[0]); }} disabled={!voiceCatalog.length}>{filteredVoices.map((voice) => <option key={voice.id} value={voice.id}>{voice.name}{voice.voice_type === "masculine" ? " · masculine" : ""}</option>)}</select><Icon name="chevron" /></span>
                </label>
                <label className="field-label" htmlFor="narration-language">DOCUMENTARY LANGUAGE
                  <span className="select-wrap"><select id="narration-language" value={useCustomLanguage ? "__custom__" : selectedLanguage} onChange={(event) => { const nextLanguage = event.target.value; if (nextLanguage === "__custom__") { setUseCustomLanguage(true); return; } setUseCustomLanguage(false); setSelectedLanguage(nextLanguage); if (nextLanguage !== "auto") { const nextVoice = voiceCatalog.find((voice) => voice.locale?.split("_", 1)[0] === nextLanguage) || voiceCatalog.find((voice) => voice.locale === "multilingual"); if (nextVoice) setVoiceId(nextVoice.id); } }}>{[<option key="auto" value="auto">Automatic from script</option>, ...narrationLanguages.map((language) => <option key={language.code} value={language.code}>{language.name}</option>), <option key="custom" value="__custom__">Other language · enter BCP-47</option>]}</select><Icon name="chevron" /></span>
                </label>
                {useCustomLanguage && <label className="field-label" htmlFor="custom-language-code">LANGUAGE CODE
                  <input id="custom-language-code" className="thumbnail-input" value={customLanguage} onChange={(event) => setCustomLanguage(event.target.value)} placeholder="e.g. sw, am, zh-Hant" maxLength={16} />
                  <small>Uses a matching installed Piper voice or the configured multilingual speech provider. If neither supports this language, production reports the limitation instead of speaking in another language.</small>
                </label>}
                <div className="voice-browser">
                  <label className="field-label" htmlFor="voice-search">BROWSE MULTILINGUAL PIPER VOICES<input id="voice-search" className="thumbnail-input" value={voiceSearch} onChange={(event) => setVoiceSearch(event.target.value)} placeholder="Search accent or voice name" /></label>
                  <div className="voice-catalog-list">{filteredVoices.map((voice) => <div className="voice-catalog-row" key={voice.id}><div><strong>{voice.name}{voice.voice_type === "masculine" ? " · masculine" : ""}</strong><small>{voice.provider === "openai" ? "Multilingual cloud voice" : voice.installed ? "Model ready" : "Worker downloads on first use"} · {voice.license_url ? <a href={voice.license_url} target="_blank" rel="noreferrer">view voice license</a> : "voice license varies"}</small></div><button type="button" onClick={() => { setVoiceId(voice.id); if (voice.locale && voice.locale !== "multilingual") setSelectedLanguage(voice.locale.split("_", 1)[0]); setMessage(`${voice.name} selected. First-use download happens in the worker if needed.`); }}>{voiceId === voice.id ? "Selected" : "Select"}</button>{voice.installed && <button type="button" onClick={() => previewVoice(voice.id)}>Preview</button>}</div>)}</div>
                  {voiceCatalog.length > 12 && <p className="voice-note">Showing 12 matches. Search to narrow the catalog. Review each model card before commercial use.</p>}
                  {voicePreviewUrl && <audio className="voice-preview-audio" controls preload="none" src={voicePreviewUrl}>Audio preview unavailable in this browser.</audio>}
                </div>
                <p className="voice-note">Automatic language selection detects the script and chooses a matching voice when one is available. If the selected language has no voice model, production reports the limitation instead of speaking in English.</p><p className="voice-note">Language support is based on the configured narration provider. Where available, a matching local Piper voice is used; other languages use the configured multilingual speech provider. Audio from that provider is AI-generated.</p>
              </section>

              <section className="panel background-panel" aria-labelledby="background-title">
                <div className="panel-heading compact-heading">
                  <div className="step-marker">04</div>
                  <div className="panel-title"><h2 id="background-title">Background</h2><p>Choose a visual treatment for motion graphics and your thumbnail.</p></div>
                </div>
                <div className="background-grid">
                  {backgrounds.map((look) => (
                    <label className={`background-option ${backgroundId === look.id ? "selected" : ""}`} key={look.id}>
                      <input type="radio" name="background-look" value={look.id} checked={backgroundId === look.id} onChange={() => setBackgroundId(look.id)} />
                      <span className={`background-preview bg-preview-${look.preview}`} aria-hidden="true"><span>CINEMORA</span></span>
                      <span className="background-card-copy"><strong>{look.title}</strong><small>{look.description}</small></span>
                    </label>
                  ))}
                </div>
              </section>

              <section className="panel music-panel" aria-labelledby="music-title">
                <div className="panel-heading compact-heading">
                  <div className="step-marker">05</div>
                  <div className="panel-title"><h2 id="music-title">Background music</h2><p>Choose whether to include a music bed; narration stays in front.</p></div>
                </div>
                <label className="motion-toggle"><input type="checkbox" checked={includeMusic} onChange={(event) => setIncludeMusic(event.target.checked)} /><span><strong>Include background music</strong><small>Turn off for narration-only audio.</small></span></label>
                {includeMusic && <div className="music-grid">
                  <div className={`music-card ${selectedMusic === "auto" ? "selected" : ""}`}>
                    <label className="music-card-choice">
                      <input type="radio" name="music-track" value="auto" checked={selectedMusic === "auto"} onChange={() => setSelectedMusic("auto")} />
                      <span><strong>Automatic selection</strong><small>Use a configured project track when available.</small></span>
                    </label>
                    <div className="music-auto-note">If no safe music source is configured, production continues with narration only.</div>
                  </div>
                  {musicTracks.map((track) => (
                    <div className={`music-card ${selectedMusic === track.id ? "selected" : ""}`} key={track.id}>
                      <label className="music-card-choice">
                        <input type="radio" name="music-track" value={track.id} checked={selectedMusic === track.id} onChange={() => setSelectedMusic(track.id)} />
                        <span><strong>{track.title}</strong><small>{track.mood}</small></span>
                      </label>
                      <audio controls preload="none" src={`${API_URL}${track.preview_url}`} aria-label={`Preview ${track.title}`} />
                    </div>
                  ))}
                </div>}
                {includeMusic && <>
                  <label className="music-volume-control">Music volume <strong>{musicPercent}% · {musicPercent === 0 ? "muted" : `${(20 * Math.log10(musicPercent / 100)).toFixed(1)} dB`}</strong><input type="range" min={0} max={100} value={musicPercent} onChange={(event) => setMusicPercent(Math.max(0, Math.min(100, Number(event.target.value))))} /><small>Linear gain after track loudness normalization and before voice ducking. Low values are very quiet. A mixed preview is available after narration is created.</small></label>
                  <p className="music-license-note">Use tracks only when your download license permits your intended video use. Keep the original license or receipt.</p>
                </>}
                {includeMusic && musicLoadError && <p className="music-error" role="status">{musicLoadError}</p>}
              </section>

              <section className="panel motion-option-panel"><div className="panel-heading compact-heading"><div className="step-marker">06</div><div className="panel-title"><h2>Generation options</h2><p>Captions are independent of template-based motion graphics.</p></div></div><label className="motion-toggle"><input type="checkbox" checked={includeMotion} onChange={(event) => setIncludeMotion(event.target.checked)} /><span><strong>Motion graphics</strong><small>Off by default. Planned template graphics only; never used to cover an unresolved passage.</small></span></label><label className="motion-toggle"><input type="checkbox" checked={includeCaptions} onChange={(event) => setIncludeCaptions(event.target.checked)} /><span><strong>Word-synced captions</strong><small>Captions remain available when graphics are off.</small></span></label><label className="motion-toggle"><input type="checkbox" checked={includeSfx} onChange={(event) => setIncludeSfx(event.target.checked)} /><span><strong>Sound effects</strong><small>Separate from the background music option.</small></span></label><div className="music-license-note" aria-label="Footage license notice"><strong>Advanced · Rights and sourcing</strong><p>Footage is screened under the configured rights policy. A downloaded asset is not automatically cleared for publication; review its source and license before publishing.</p></div></section>

            </div>

            <aside className="right-column">
              <section className="summary-card">
                <div className="summary-top"><div><span className="eyebrow">PROJECT</span><h2>Summary</h2></div></div>
                <div className="summary-rows"><div><span>Category</span><strong>{category}</strong></div><div><span>Background</span><strong>{backgrounds.find((look) => look.id === backgroundId)?.title || "Blue"}</strong></div><div><span>Music</span><strong>{includeMusic ? selectedMusic === "auto" ? "Automatic selection" : musicTracks.find((track) => track.id === selectedMusic)?.title || "Loading track" : "Off"}</strong></div><div><span>Music level</span><strong>{includeMusic ? `${musicPercent}%` : "—"}</strong></div><div><span>Captions</span><strong>{includeCaptions ? "On" : "Off"}</strong></div><div><span>Motion graphics</span><strong>{includeMotion ? "On" : "Off"}</strong></div><div><span>Script length</span><strong>{words ? `${words.toLocaleString()} words` : "Not added"}</strong></div><div><span>Estimated runtime</span><strong>{duration ? `About ${duration} min` : "—"}</strong></div><div><span>Visual sourcing</span><strong>Research-led</strong></div></div>
                <div className="summary-footnote"><Icon name="info" /><span>Runtime is an estimate. Final length depends on narration and edit.</span></div>

                <button className="generate-button" type="submit" disabled={loading || !script.trim() || apiOnline === false}>
                  {loading ? <><span className="button-spinner" /> Starting…</> : <>Create and start <Icon name="arrow" /></>}
                </button>
                <p className="button-caption">You can follow each stage as your film comes together.</p>
              </section>

              <section className="status-card" aria-live="polite">
                <div className="status-heading"><span className={`status-indicator ${status?.status === "failed" ? "status-error" : status?.status === "completed" ? "status-done" : projectId ? "status-active" : ""}`} /><div><span className="eyebrow">PRODUCTION STATUS</span><h3>{statusTitle(status?.status, !!projectId, loading)}</h3></div></div>
                {projectId ? <>
                  <p className="status-message">{message || "Waiting for production status…"}</p>
                  <div className="stage-list">{stageOrder.map(([key, label]) => {
                    const stageStatus = status?.stages?.[key]?.status || (status?.current_stage === key ? "running" : "pending");
                    return <div className={`stage-row ${stageStatus}`} key={key}><span className="stage-check">{stageStatus === "completed" ? "✓" : stageStatus === "running" ? <span /> : stageStatus === "failed" ? "!" : ""}</span><span>{label}</span>{stageStatus === "running" && <small>NOW</small>}{stageStatus === "skipped" && <small>SKIPPED</small>}{stageStatus === "failed" && <small>FAILED</small>}</div>;
                  })}</div>
                  <div className="project-reference"><span>PROJECT</span><code>{projectId.slice(0, 8)}…</code>{jobId && <><i /><span>JOB</span><code>{jobId.slice(0, 8)}…</code></>}</div>
                </> : <div className="empty-status"><div className="status-skeleton"><span /><span /><span /></div><p>Your production stages will appear here after you start.</p></div>}
              </section>

              <section className="panel thumbnail-panel" id="thumbnail-generator" aria-labelledby="thumbnail-title">
                <div className="panel-heading compact-heading"><div className="step-marker">07</div><div className="panel-title"><h2 id="thumbnail-title">Thumbnail generator</h2><p>{videoUrl ? "Creates a 1280 × 720 cover from a real footage frame in your video." : "Available after your video is finished. Your generated thumbnails will appear here."}</p></div></div>
                <label className="field-label" htmlFor="thumbnail-copy">THUMBNAIL TITLE
                  <input id="thumbnail-copy" className="thumbnail-input" value={thumbnailTitle} onChange={(event) => setThumbnailTitle(event.target.value)} maxLength={100} placeholder="Add a short, clear headline" disabled={!videoUrl} />
                </label>
                <button className="thumbnail-button" type="button" disabled={!videoUrl || thumbnailBusy || !thumbnailTitle.trim()} onClick={() => void createThumbnail()}>{thumbnailBusy ? "Creating…" : thumbnailUrl ? "Regenerate thumbnail" : "Generate thumbnail"}</button>
                {thumbnailUrl && <Image className="thumbnail-preview" src={thumbnailUrl} alt="Generated video thumbnail preview" width={640} height={360} unoptimized />}
              </section>

              {videoUrl && <section className="video-card"><div className="video-heading"><span className="eyebrow">FINAL CUT</span><span className="ready-pill">READY</span></div><video controls preload="metadata" src={videoUrl} /><a href={videoUrl} target="_blank" rel="noreferrer">Open or download MP4 <Icon name="arrow-up-right" /></a></section>}
            </aside>
          </form>
          <footer className="page-footer"><span>CINEMORA</span><span>Production workspace</span></footer>
        </div>
      </section>
    </main>
  );
}

function stageLabel(stage: string) {
  return stageOrder.find(([key]) => key === stage)?.[1] || stage.replaceAll("_", " ");
}

function statusTitle(status: string | undefined, hasProject: boolean, loading: boolean) {
  if (loading) return "Creating your project";
  if (status === "completed") return "Documentary complete";
  if (status === "failed") return "Production needs attention";
  if (status === "running") return "Your film is in production";
  if (status === "queued" || status === "created" || status === "pipeline_queued") return "Waiting in the queue";
  return hasProject ? "Checking production…" : "Ready when you are";
}

function Icon({ name }: { name: string }) {
  const common = { width: 18, height: 18, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.7, strokeLinecap: "round" as const, strokeLinejoin: "round" as const, "aria-hidden": true as const };
  const paths: Record<string, ReactNode> = {
    spark: <><path d="m12 3 1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3Z" /><path d="m19 15 .9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9L19 15Z" /></>,
    grid: <><rect x="3.5" y="3.5" width="7" height="7" rx="1.5" /><rect x="13.5" y="3.5" width="7" height="7" rx="1.5" /><rect x="3.5" y="13.5" width="7" height="7" rx="1.5" /><rect x="13.5" y="13.5" width="7" height="7" rx="1.5" /></>,
    image: <><rect x="3" y="4" width="18" height="16" rx="2" /><circle cx="8.5" cy="9" r="1.5" /><path d="m21 15-5-5L5 20" /></>,
    layers: <><path d="m12 3 9 5-9 5-9-5 9-5Z" /><path d="m3 12 9 5 9-5M3 16l9 5 9-5" /></>,
    wand: <><path d="m15 4 5 5M4 20l12.5-12.5M7 4v4M5 6h4M18 14v4M16 16h4" /><path d="m13 6 5 5" /></>,
    more: <><circle cx="5" cy="12" r="1" /><circle cx="12" cy="12" r="1" /><circle cx="19" cy="12" r="1" /></>,
    chevron: <path d="m7 10 5 5 5-5" />,
    search: <><circle cx="10.8" cy="10.8" r="6.8" /><path d="m16 16 4.5 4.5" /></>,
    film: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4" /></>,
    mic: <><rect x="9" y="3" width="6" height="12" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3M9 21h6" /></>,
    info: <><circle cx="12" cy="12" r="9" /><path d="M12 11v5M12 8h.01" /></>,
    arrow: <><path d="M5 12h14M13 6l6 6-6 6" /></>,
    "arrow-up-right": <><path d="M7 17 17 7M8 7h9v9" /></>,
  };
  return <svg {...common}>{paths[name] || paths.spark}</svg>;
}
