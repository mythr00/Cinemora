import Link from "next/link";
import {
  Archive,
  ArrowUpRight,
  AudioLines,
  Captions,
  Clapperboard,
  Download,
  History,
  Layers,
  LineChart,
  ListChecks,
  Mic,
  Music,
  Search,
} from "lucide-react";

/**
 * Landing feature demonstrations (redesign 2026-11-26,
 * directive Phase 4). Ten capabilities, ten different
 * compositions — no icon-heading-paragraph grid. Every visual
 * is built from the sample production's real artefacts
 * (public/landing/*, from project "The Shipping Container") or
 * from the product's real interface vocabulary (plan rows,
 * coverage chips, revision trail). Copy names only capabilities
 * that exist in this build.
 */

function Kicker({ icon, children }: { icon: React.ReactNode; children: React.ReactNode }) {
  return <p className="feat-kicker">{icon}{children}</p>;
}

export function LandingFeatures() {
  return (
    <div className="feat-grid">
      {/* 1 — Production, wide film strip */}
      <article className="feat-card feat-card--wide feat-production">
        <div className="feat-copy">
          <Kicker icon={<Clapperboard size={14} strokeWidth={2} />}>AI DOCUMENTARY PRODUCTION</Kicker>
          <h3>From script to finished film, one production path</h3>
          <p>Bring a finished script. Cinemora analyses the story, plans every sentence&apos;s visuals, narrates, captions and renders — and shows you the plan before it spends a frame.</p>
          <Link className="text-link" href="/create">Start with your script <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link>
        </div>
        <div className="film-strip" aria-hidden>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/landing/film-frame.jpg" alt="" />
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/landing/terminal.jpg" alt="" />
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/landing/containers.jpg" alt="" />
          <span className="film-strip-label">FRAMES FROM “THE SHIPPING CONTAINER” · A REAL CINEMORA PRODUCTION</span>
        </div>
      </article>

      {/* 2 — Discovery */}
      <article className="feat-card feat-discovery">
        <Kicker icon={<Search size={14} strokeWidth={2} />}>INTELLIGENT DISCOVERY</Kicker>
        <h3>Footage and images, searched per sentence</h3>
        <p>Every narration sentence becomes its own search — across moving footage and stills from every source Cinemora can reach, not one library.</p>
        <div className="query-stack" aria-hidden>
          <span className="query-chip"><Search size={12} strokeWidth={2} /> port of newark 1956</span>
          <span className="query-chip"><Search size={12} strokeWidth={2} /> container terminal aerial</span>
          <span className="query-chip"><Search size={12} strokeWidth={2} /> straddle carrier yard</span>
        </div>
        <div className="thumb-row" aria-hidden>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/landing/terminal.jpg" alt="" />
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/landing/carrier.jpg" alt="" />
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/landing/containers.jpg" alt="" />
        </div>
      </article>

      {/* 3 — Archival & stock */}
      <article className="feat-card feat-archive">
        <Kicker icon={<Archive size={14} strokeWidth={2} />}>ARCHIVAL &amp; STOCK MEDIA</Kicker>
        <h3>History, from the archives — with its papers</h3>
        <p>Archival photographs, historical footage and stock media are all in reach. Every asset keeps its source and licence in the production&apos;s rights manifest; unknown rights stay labelled unknown.</p>
        <figure className="archive-frame">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/landing/archival-1956.jpg" alt="Archival photograph sourced for the sample production: Newark in the 1950s" />
          <figcaption><span>SOURCE RECORDED</span><span>LICENCE IN RIGHTS MANIFEST</span></figcaption>
        </figure>
      </article>

      {/* 4 — Visual selection / plan review */}
      <article className="feat-card feat-card--wide feat-selection">
        <div className="feat-copy">
          <Kicker icon={<ListChecks size={14} strokeWidth={2} />}>INTELLIGENT VISUAL SELECTION</Kicker>
          <h3>Review the plan sentence by sentence — before rendering</h3>
          <p>The strongest visual wins each beat: footage where it communicates, a photograph where it is the evidence, a graphic where an explanation is needed. You approve the plan, or send it back, before anything renders.</p>
        </div>
        <div className="plan-rows" aria-hidden>
          <div className="plan-row"><p>“…carrying 58 steel boxes, and sailed for Houston, Texas.”</p><span className="plan-chip plan-chip--still">ARCHIVAL STILL</span></div>
          <div className="plan-row"><p>“McLean&apos;s boxes brought that cost down to 16 cents per ton.”</p><span className="plan-chip plan-chip--graphic">MOTION GRAPHIC</span></div>
          <div className="plan-row"><p>“…straddle carriers carry each box to its assigned stack in the yard…”</p><span className="plan-chip plan-chip--footage">FOOTAGE</span></div>
        </div>
      </article>

      {/* 5 — Motion graphics */}
      <article className="feat-card feat-graphics">
        <Kicker icon={<LineChart size={14} strokeWidth={2} />}>MOTION GRAPHICS</Kicker>
        <h3>Explanations that animate the point</h3>
        <p>Charts, timelines, maps, evidence boards and diagrams are generated from the narration itself and placed where the script asks for them — never pasted over footage that already tells the story.</p>
        <figure className="graphics-frame">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/landing/chart-frame.jpg" alt="A line-chart motion graphic rendered by Cinemora for the sample production" />
          <figcaption>RENDERED BY CINEMORA · LINE CHART, “THE SHIPPING CONTAINER”</figcaption>
        </figure>
      </article>

      {/* 6 — Narration */}
      <article className="feat-card feat-narration">
        <Kicker icon={<Mic size={14} strokeWidth={2} />}>PROFESSIONAL NARRATION</Kicker>
        <h3>A studio voice that reads it like a narrator</h3>
        <p>Your script is narrated by the studio&apos;s built-in English voice, timed sentence by sentence so every visual and caption lands on the words it belongs to.</p>
        <div className="waveform" aria-hidden>
          {Array.from({ length: 36 }, (_, i) => (
            <i key={i} style={{ height: `${18 + Math.round(30 * Math.abs(Math.sin(i * 1.7)))}px`, animationDelay: `${(i % 9) * 0.12}s` }} />
          ))}
        </div>
        <p className="narration-quote">“The plain steel box … is the machine the modern world is built on.”</p>
      </article>

      {/* 7 — Captions */}
      <article className="feat-card feat-captions">
        <Kicker icon={<Captions size={14} strokeWidth={2} />}>WORD-SYNCHRONISED CAPTIONS</Kicker>
        <h3>Karaoke captions, locked to the voice</h3>
        <p>Captions are timed to the narration word by word and exported with the film — burned in, and as caption files alongside the MP4.</p>
        <p className="karaoke-line" aria-hidden>
          <span className="is-sung">A ship could spend </span><span className="is-now">more time in port </span><span>being loaded than it spent at sea.</span>
        </p>
      </article>

      {/* 8 — Music & sound */}
      <article className="feat-card feat-music">
        <Kicker icon={<Music size={14} strokeWidth={2} />}>BACKGROUND MUSIC &amp; SOUND</Kicker>
        <h3>A soundtrack that stays under the story</h3>
        <p>Where music is enabled, a bed is selected for the film&apos;s mood and mixed beneath the narration — present in the quiet, never competing with the voice.</p>
        <div className="meters" aria-hidden>
          <div className="meter"><span>NARRATION</span><span className="meter-track"><i className="meter-fill meter-fill--voice" /></span></div>
          <div className="meter"><span>MUSIC BED</span><span className="meter-track"><i className="meter-fill meter-fill--music" /></span></div>
        </div>
      </article>

      {/* 9 — Editing & revisions */}
      <article className="feat-card feat-editing">
        <Kicker icon={<History size={14} strokeWidth={2} />}>EDITING &amp; REVISIONS</Kicker>
        <h3>Finished is not final — revise without losing the original</h3>
        <p>Open a completed production in the studio editor and adjust footage, graphics, captions, narration and music. Every edit renders as a new revision; the original export never changes.</p>
        <div className="revision-trail" aria-hidden>
          <span className="revision-chip">REVISION 1 · ORIGINAL</span>
          <span className="revision-arrow">→</span>
          <span className="revision-chip">REVISION 2 · NEW GRAPHIC</span>
          <span className="revision-arrow">→</span>
          <span className="revision-chip revision-chip--current">REVISION 3 · CURRENT</span>
        </div>
      </article>

      {/* 10 — Render & export, wide */}
      <article className="feat-card feat-card--wide feat-export">
        <div className="feat-copy">
          <Kicker icon={<Download size={14} strokeWidth={2} />}>RENDERING &amp; EXPORT</Kicker>
          <h3>The final video, delivered as a real file</h3>
          <p>A full-HD MP4 with narration, captions and music baked in — plus its provenance certificate, asset report and rights manifest, so you can show where every second came from.</p>
          <Link className="text-link" href="/projects">See how projects are delivered <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link>
        </div>
        <div className="export-card" aria-hidden>
          <span className="export-icon"><AudioLines size={20} strokeWidth={1.8} /></span>
          <div>
            <strong>documentary.mp4</strong>
            <span>1920×1080 · MP4 · CAPTIONS + MUSIC BAKED IN</span>
          </div>
          <span className="export-chips"><Layers size={13} strokeWidth={2} /> + CERTIFICATE · ASSET REPORT · RIGHTS MANIFEST</span>
        </div>
      </article>
    </div>
  );
}
