/**
 * PRESERVED — quarantined in Brief Stage 3 (C-02), 2026-11-20.
 *
 * This is the original Channel Lab (script research) implementation,
 * kept verbatim for reference. It is NOT routed, imported, or
 * rendered anywhere in the active application: every backend
 * endpoint it calls is absent from the committed backend
 * (all 23 legacy endpoints re-verified 404 under TestClient
 * at HEAD bfc5a06). The active route shows an honest
 * unavailable state instead. Do not resurrect this file
 * without a working backend for it, and do not delete it —
 * removal decisions belong to the founder (register C-14).
 */
"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { legacyRequest } from "@/lib/api";
import { ArrowUpRight } from "lucide-react";

type Video = { id: string; title: string; url: string; duration_seconds?: number | null; view_count?: number | null; upload_date?: string | null };
type Channel = { channel_title: string; channel_url: string; videos: Video[]; topic_signals: string[]; detected_niche: string; niche_confidence: number; result_limit: number };
type ResearchSource = { id: string; title: string; url: string; snippet: string };

export default function ChannelLab() {
  const router = useRouter();
  const [channelUrl, setChannelUrl] = useState("");
  const [channel, setChannel] = useState<Channel | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [audience, setAudience] = useState("General audience");
  const [angle, setAngle] = useState("");
  const [durationMinutes, setDurationMinutes] = useState(10);
  const [script, setScript] = useState("");
  const [scriptTopic, setScriptTopic] = useState("");
  const [sources, setSources] = useState<ResearchSource[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  async function inspect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setMessage("Reading public channel and video metadata…"); setScript(""); setScriptTopic("");
    try {
      const data = await legacyRequest<Channel>("/channel-research", { method: "POST", body: JSON.stringify({ channel_url: channelUrl }) });
      setChannel(data); setSelected([]); setSources([]); setMessage(`${data.videos.length} public video entries found. Select up to three.`);
    } catch (error) { setMessage(error instanceof Error ? error.message : "Script workspace could not inspect that channel."); }
    finally { setBusy(false); }
  }

  async function generateScript() {
    if (!channel) return;
    setBusy(true); setMessage(`Finding a fresh ${channel.detected_niche} topic, researching sources, and drafting about ${durationMinutes} minutes…`);
    try {
      const data = await legacyRequest<{ script?: string; research_sources?: ResearchSource[]; topic?: string; actual_word_count?: number; target_word_count?: number; duration_warning?: string | null }>("/channel-research/script", { method: "POST", body: JSON.stringify({ channel_url: channel.channel_url, video_ids: selected, audience, angle, duration_minutes: durationMinutes }) });
      if (!data.script?.trim()) throw new Error("The research service returned an empty draft. Check the backend API and model configuration, then try again.");
      setScript(data.script); setScriptTopic(data.topic || ""); setSources(data.research_sources || []); setMessage(`Draft ready: ${data.actual_word_count?.toLocaleString() || data.script.trim().split(/\s+/).length.toLocaleString()} narration words (target ~${data.target_word_count?.toLocaleString() || (durationMinutes * 135).toLocaleString()} for ${durationMinutes} min).${data.duration_warning ? ` ${data.duration_warning}` : ""} Search snippets are research leads; review linked sources before publishing.`);
    } catch (error) { setScript(""); setSources([]); setMessage(error instanceof Error ? error.message : "Script generation failed."); }
    finally { setBusy(false); }
  }

  function toggleVideo(id: string) {
    setSelected((current) => current.includes(id) ? current.filter((item) => item !== id) : current.length < 3 ? [...current, id] : current);
  }

  return <main className="lab-shell app-page">
    <AppNavigation active="/channel-lab" />
    <section className="lab-content">
      <p className="lab-eyebrow">SCRIPT STUDIO</p><h1>Develop an original script</h1>
      <p className="lab-intro">Review public video titles and channel metadata, choose up to three topic references, then research an original story angle. This does not download videos or copy transcripts or a creator’s writing.</p>
      <form className="lab-form" onSubmit={(event) => void inspect(event)}><label htmlFor="channel-url">YouTube channel URL</label><div className="lab-url-row"><input id="channel-url" type="url" value={channelUrl} onChange={(event) => setChannelUrl(event.target.value)} placeholder="https://www.youtube.com/@channel" required/><button disabled={busy}>{busy ? "Working…" : "Find videos"}</button></div></form>
      {message && !channel && <p className="lab-message" role="status" aria-live="polite">{message}</p>}
      {channel && <section className="lab-results"><h2>{channel.channel_title}</h2><p>Detected niche: <strong>{channel.detected_niche || "documentary stories"}</strong> · Signals: {channel.topic_signals.join(" · ") || "Not enough title data"}</p><p>Showing up to {channel.result_limit} recent public video results · Optional: select up to 3 as niche references · {selected.length}/3 selected</p><div className="channel-video-grid">{channel.videos.map((video) => <label className={`channel-video ${selected.includes(video.id) ? "selected" : ""}`} key={video.id}><input type="checkbox" checked={selected.includes(video.id)} disabled={!selected.includes(video.id) && selected.length >= 3} onChange={() => toggleVideo(video.id)}/><span><strong>{video.title}</strong><small>{video.view_count ? `${video.view_count.toLocaleString()} views` : "Public video"}{video.upload_date ? ` · ${video.upload_date}` : ""}</small></span><a href={video.url} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()}>Open <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></a></label>)}</div>
        <div className="lab-fields"><label>Script length (minutes)<input type="number" min={1} max={60} step={1} value={durationMinutes} onChange={(event) => setDurationMinutes(Math.max(1, Math.min(60, Number(event.target.value) || 1)))}/><small>Approx. {durationMinutes * 135} spoken words</small></label><label>New topic direction (optional)<input value={angle} maxLength={500} onChange={(event) => setAngle(event.target.value)} placeholder="Leave blank for a fresh topic in this niche; e.g. latest US inflation story"/><small>We research a new story, not a rewrite of the selected videos.</small></label><label>Audience<input value={audience} maxLength={200} onChange={(event) => setAudience(event.target.value)}/></label></div><button type="button" className="lab-generate" disabled={busy} onClick={() => void generateScript()}>{busy ? "Finding a topic, researching, and writing…" : `Research and draft a ${durationMinutes}-minute script`}</button><p className="lab-action-status" role="status" aria-live="polite">{message}</p>
      </section>}
      {script && <section className="lab-script"><div><h2>Clean narration{scriptTopic ? ` · ${scriptTopic}` : ""}</h2><div className="lab-script-actions"><button onClick={() => void navigator.clipboard.writeText(script)}>Copy narration</button><button onClick={() => { sessionStorage.setItem("cinemora-script-draft", script); router.push("/create"); }}>Use in editor</button></div></div><textarea readOnly value={script}/><p>The text above is narration only. Research links are separate and are not copied into your script. Review names, dates, and factual claims before publishing.</p>{sources.length > 0 && <div className="research-source-list"><h3>Separate research leads</h3>{sources.map((source) => <a href={source.url} target="_blank" rel="noreferrer" key={source.id}><strong>[{source.id}] {source.title}</strong><small>{source.url}</small><span>{source.snippet}</span></a>)}</div>}</section>}
      <p className="lab-rights">Use YouTube’s metadata in line with its terms. A channel’s public availability does not grant reuse rights to its video or audio.</p>
    </section>
  </main>;
}
