"use client";

import { useEffect, useRef, useState } from "react";
import { Pause, Play } from "lucide-react";

/**
 * Landing product showcase (presentation redesign, founder
 * directive 2026-11-28). A designed demonstration, not a
 * walkthrough: an interface-inspired stage where a story is
 * assembled into a film — labelled clip blocks settle onto a
 * picture track, a narration waveform runs beneath, caption
 * words light up in sync with the playhead. Nothing here is
 * footage and nothing calls the API; the panel says
 * DEMONSTRATION on its face, and the caption below it points
 * to the one real production on this page (Finished Results).
 *
 * The loop is rAF-driven so the playhead, the clips and the
 * caption words share one clock. It pauses when the stage
 * scrolls out of view, and under prefers-reduced-motion it
 * rests in a mid-assembly still.
 */

const LOOP_MS = 12000;

const CLIPS = [
  { label: "HARBOUR", from: 0.0, to: 0.22 },
  { label: "ARCHIVE", from: 0.22, to: 0.4 },
  { label: "ROUTE MAP", from: 0.4, to: 0.58 },
  { label: "CHART", from: 0.58, to: 0.76 },
  { label: "THE YARD", from: 0.76, to: 1.0 },
];

const CAPTION = "Fifty-eight steel boxes left Newark — and the world got smaller.";
const WORDS = CAPTION.split(" ");

const BARS = Array.from({ length: 56 }, (_, i) =>
  Math.round(22 + 62 * Math.abs(Math.sin(i * 1.35) * 0.7 + Math.sin(i * 0.5) * 0.3)),
);

export function Showcase() {
  const [progress, setProgress] = useState(0);
  const [playing, setPlaying] = useState(true);
  const figureRef = useRef<HTMLElement | null>(null);
  const stateRef = useRef({ progress: 0, playing: true, inView: true });

  useEffect(() => {
    stateRef.current.playing = playing;
  }, [playing]);

  useEffect(() => {
    const el = figureRef.current;
    let io: IntersectionObserver | undefined;
    if (el && "IntersectionObserver" in window) {
      io = new IntersectionObserver(
        (entries) => {
          for (const entry of entries) stateRef.current.inView = entry.isIntersecting;
        },
        { threshold: 0.08 },
      );
      io.observe(el);
    }
    let raf = 0;
    let last = performance.now();
    let motionChecked = false;
    const tick = (now: number) => {
      if (!motionChecked) {
        motionChecked = true;
        if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
          stateRef.current = { progress: 0.62, playing: false, inView: stateRef.current.inView };
          setProgress(0.62);
          setPlaying(false);
        }
      }
      const s = stateRef.current;
      if (s.playing && s.inView) {
        const next = s.progress + (now - last) / LOOP_MS;
        const wrapped = next >= 1 ? next - 1 : next;
        stateRef.current = { ...s, progress: wrapped };
        setProgress(wrapped);
      }
      last = now;
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(raf);
      io?.disconnect();
    };
  }, []);

  const sungCount = Math.ceil(progress * WORDS.length);

  return (
    <figure
      className="showcase"
      aria-label="Demonstration: a story being assembled into a film"
      ref={figureRef}
    >
      <div className="showcase-head" aria-hidden>
        <span>A STORY, ASSEMBLED</span>
        <span>DEMONSTRATION</span>
      </div>
      <div className="demo-frame">
        <div className="demo-tracks">
          <div className="demo-track">
            <span className="demo-track-label" aria-hidden>V1</span>
            <div className="demo-lane">
              {CLIPS.map((clip) => {
                const state = progress >= clip.to ? "is-in" : progress >= clip.from ? "is-in is-now" : "";
                return (
                  <span
                    key={clip.label}
                    className={`demo-clip ${state}`}
                    style={{ left: `${clip.from * 100}%`, width: `${(clip.to - clip.from) * 100}%` }}
                  >
                    {clip.label}
                  </span>
                );
              })}
            </div>
          </div>
          <div className="demo-track">
            <span className="demo-track-label" aria-hidden>A1</span>
            <div className="demo-lane demo-wave" aria-hidden>
              {BARS.map((height, i) => (
                <i
                  key={i}
                  className={i / BARS.length <= progress ? "is-on" : ""}
                  style={{ height: `${height}%` }}
                />
              ))}
            </div>
          </div>
          <div className="demo-track">
            <span className="demo-track-label" aria-hidden>FX</span>
            <div className="demo-lane">
              <span className={`demo-chip demo-chip--cap ${progress >= 0.06 ? "is-in" : ""}`}>CAPTIONS</span>
              <span className={`demo-chip demo-chip--mus ${progress >= 0.3 ? "is-in" : ""}`}>MUSIC</span>
              <span className={`demo-chip demo-chip--gfx ${progress >= 0.58 ? "is-in" : ""}`}>GRAPHICS</span>
            </div>
          </div>
          <span
            className="demo-playhead"
            style={{ left: `calc(38px + (100% - 38px) * ${progress})` }}
            aria-hidden
          />
        </div>
        <p className="demo-caption" aria-hidden>
          {WORDS.map((word, index) => (
            <span key={index} className={index < sungCount ? "is-sung" : ""}>
              {word}
              {index < WORDS.length - 1 ? " " : ""}
            </span>
          ))}
        </p>
      </div>
      <div className="demo-controls">
        <button
          type="button"
          className="showcase-play"
          aria-label={playing ? "Pause the demonstration" : "Play the demonstration"}
          onClick={() => setPlaying((current) => !current)}
        >
          {playing ? <Pause size={15} strokeWidth={2.2} /> : <Play size={15} strokeWidth={2.2} />}
        </button>
        <span className="demo-progress" aria-hidden>
          <i style={{ width: `${Math.round(progress * 100)}%` }} />
        </span>
        <span className="demo-controls-note">A story becomes a film</span>
      </div>
      <figcaption className="showcase-caption">
        A demonstration of the studio at work — drawn, not filmed. The finished
        film further down this page is a real Cinemora production.
      </figcaption>
    </figure>
  );
}
