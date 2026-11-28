"use client";

import { useEffect, useRef } from "react";
import {
  Captions,
  Film,
  Image as ImageIcon,
  LineChart,
  Mic,
  Music,
  Scissors,
  ScrollText,
  TrendingUp,
  Users,
} from "lucide-react";

/**
 * Landing feature demonstrations (presentation redesign,
 * founder directive 2026-11-28). Six capabilities, each
 * DEMONSTRATED by a designed motion panel instead of a
 * photograph, and each stated as one headline + one sentence
 * of customer benefit. No internal mechanics: nothing here
 * describes how the engine searches, selects or renders.
 * The animations are CSS-only, transform/opacity, flattened
 * by the global reduced-motion rule, and paused while the
 * grid is off-screen. Four further studio capabilities ride
 * below as a compact row — present, but not demonstrated.
 */

function Kicker({ icon, children }: { icon: React.ReactNode; children: React.ReactNode }) {
  return <p className="feat-kicker">{icon}{children}</p>;
}

const CAPTION_WORDS = "A ship could spend more time in port than it spent at sea.".split(" ");

const PLAN_SLOTS = [
  { label: "FOOTAGE", tone: "a" },
  { label: "ARCHIVAL", tone: "b" },
  { label: "GRAPHIC", tone: "c" },
  { label: "FOOTAGE", tone: "a" },
];

export function LandingFeatures() {
  const gridRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = gridRef.current;
    if (!el || !("IntersectionObserver" in window)) return;
    const io = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) el.removeAttribute("data-anim-off");
          else el.setAttribute("data-anim-off", "");
        }
      },
      { threshold: 0.02 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);

  return (
    <>
      <div className="feat-grid" ref={gridRef}>
        {/* 1 — Visuals: elements settling into a timeline */}
        <article className="feat-card" data-reveal>
          <div className="feat-demo feat-demo--plan" aria-hidden>
            <div className="plan-stage">
              {PLAN_SLOTS.map((slot, i) => (
                <div className="plan-slot" key={i}>
                  <span className={`plan-chip plan-chip--${slot.tone}`} style={{ animationDelay: `${i * 0.45}s` }}>
                    {slot.label}
                  </span>
                  <span className="plan-slot-box" />
                </div>
              ))}
            </div>
            <span className="plan-track" />
          </div>
          <div className="feat-card-copy">
            <Kicker icon={<Film size={14} strokeWidth={2} />}>VISUALS</Kicker>
            <h3>The right visuals for every story</h3>
            <p>Footage, archival imagery and custom graphics — brought together for the story you are telling.</p>
          </div>
        </article>

        {/* 2 — Motion graphics: a chart and a route, drawing themselves */}
        <article className="feat-card" data-reveal data-delay="80">
          <div className="feat-demo feat-demo--graphics" aria-hidden>
            <svg className="graphics-svg" viewBox="0 0 320 176">
              <defs>
                <pattern id="feat-dots" width="18" height="18" patternUnits="userSpaceOnUse">
                  <circle cx="2" cy="2" r="1.6" className="dot" />
                </pattern>
              </defs>
              <rect width="320" height="176" fill="url(#feat-dots)" />
              <path className="g-axis" d="M28 148 H300" />
              <path className="g-line" pathLength={1} d="M28 128 L92 104 L156 114 L220 62 L292 40" />
              <path className="g-route" pathLength={1} d="M36 156 Q 160 18 284 96" />
              <circle className="g-pin" cx="36" cy="156" r="4.5" />
              <circle className="g-pin" cx="284" cy="96" r="4.5" />
            </svg>
          </div>
          <div className="feat-card-copy">
            <Kicker icon={<LineChart size={14} strokeWidth={2} />}>MOTION GRAPHICS</Kicker>
            <h3>Ideas, drawn in motion</h3>
            <p>Charts, maps and diagrams that move with the narration.</p>
          </div>
        </article>

        {/* 3 — Narration: a living waveform */}
        <article className="feat-card" data-reveal>
          <div className="feat-demo feat-demo--voice" aria-hidden>
            <div className="narr-wave">
              {Array.from({ length: 44 }, (_, i) => (
                <i
                  key={i}
                  style={{
                    height: `${18 + Math.round(64 * Math.abs(Math.sin(i * 1.55)))}%`,
                    animationDelay: `${(i % 11) * 0.11}s`,
                  }}
                />
              ))}
            </div>
            <div className="narr-chips">
              <span>54 VOICES</span>
              <span>8 LANGUAGES</span>
            </div>
          </div>
          <div className="feat-card-copy">
            <Kicker icon={<Mic size={14} strokeWidth={2} />}>NARRATION</Kicker>
            <h3>A voice that reads like a narrator</h3>
            <p>54 studio voices across eight languages. Pick the one that sounds like your channel.</p>
          </div>
        </article>

        {/* 4 — Captions: words lighting in sync */}
        <article className="feat-card" data-reveal data-delay="80">
          <div className="feat-demo feat-demo--captions" aria-hidden>
            <p className="cap-line">
              {CAPTION_WORDS.map((word, i) => (
                <span key={i} style={{ animationDelay: `${i * 0.42}s` }}>
                  {word}
                  {i < CAPTION_WORDS.length - 1 ? " " : ""}
                </span>
              ))}
            </p>
            <span className="cap-tag">WORD-SYNCED CAPTIONS</span>
          </div>
          <div className="feat-card-copy">
            <Kicker icon={<Captions size={14} strokeWidth={2} />}>CAPTIONS</Kicker>
            <h3>Every word, perfectly in sync</h3>
            <p>Word-timed captions, burned into the film and delivered as files alongside it.</p>
          </div>
        </article>

        {/* 5 — Music: mood and levels */}
        <article className="feat-card" data-reveal>
          <div className="feat-demo feat-demo--music" aria-hidden>
            <div className="music-chips">
              <span>CALM</span>
              <span className="is-on">DRIVING</span>
              <span>FINALE</span>
            </div>
            <div className="music-levels">
              {Array.from({ length: 18 }, (_, i) => (
                <i
                  key={i}
                  style={{
                    height: `${24 + Math.round(58 * Math.abs(Math.sin(i * 0.9)))}%`,
                    animationDelay: `${(i % 7) * 0.16}s`,
                  }}
                />
              ))}
            </div>
          </div>
          <div className="feat-card-copy">
            <Kicker icon={<Music size={14} strokeWidth={2} />}>MUSIC</Kicker>
            <h3>A soundtrack that follows the story</h3>
            <p>A cinematic bed mixed beneath the voice — present, never competing.</p>
          </div>
        </article>

        {/* 6 — Editing: clips rearranging on a timeline */}
        <article className="feat-card" data-reveal data-delay="80">
          <div className="feat-demo feat-demo--edit" aria-hidden>
            <div className="edit-track">
              <span className="edit-block edit-block--a">OPENING</span>
              <span className="edit-block edit-block--b">THE TURN</span>
              <span className="edit-block edit-block--c">FINALE</span>
            </div>
            <span className="edit-playhead" />
            <div className="edit-track edit-track--audio">
              <span className="edit-block edit-block--voice">NARRATION</span>
              <span className="edit-block edit-block--score">SCORE</span>
            </div>
          </div>
          <div className="feat-card-copy">
            <Kicker icon={<Scissors size={14} strokeWidth={2} />}>EDITING</Kicker>
            <h3>Finished isn&apos;t final</h3>
            <p>Reopen any film, adjust it, and export a new revision. The original never changes.</p>
          </div>
        </article>
      </div>

      <div className="also-row" data-reveal>
        <p className="also-label">ALSO IN THE STUDIO</p>
        <div className="also-items">
          <div className="also-item">
            <ScrollText size={17} strokeWidth={2} aria-hidden />
            <div><strong>Script studio</strong><span>Develop the story before you produce it.</span></div>
          </div>
          <div className="also-item">
            <ImageIcon size={17} strokeWidth={2} aria-hidden />
            <div><strong>Thumbnail studio</strong><span>Thumbnails made for every film.</span></div>
          </div>
          <div className="also-item">
            <TrendingUp size={17} strokeWidth={2} aria-hidden />
            <div><strong>Channel research</strong><span>See what is working in your niche.</span></div>
          </div>
          <div className="also-item">
            <Users size={17} strokeWidth={2} aria-hidden />
            <div><strong>Team workspace</strong><span>Produce together, across channels.</span></div>
          </div>
        </div>
      </div>
    </>
  );
}
