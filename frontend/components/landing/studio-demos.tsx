"use client";

import { useEffect, useRef } from "react";
import { Check, Play } from "lucide-react";

/**
 * Studio demonstrations (design philosophy transformation,
 * founder directive 2026-11-28). Cinemora's own capabilities
 * shown as illustrative simulations — the experience and the
 * outcome, never the mechanics: no queries, no sources, no
 * criteria, no scores appear in any panel. Each stage says
 * DEMONSTRATION on its face.
 *
 * All motion is CSS keyframes over transform/opacity, looped
 * with intent. Base styles are always the composed, readable
 * state; keyframes depart from it, so the global
 * reduced-motion rule (which flattens every animation) leaves
 * each panel resting in a still that still communicates. An
 * IntersectionObserver pauses every loop while its stage is
 * off-screen.
 */

function useAnimPause<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || !("IntersectionObserver" in window)) return;
    const io = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) el.removeAttribute("data-anim-off");
          else el.setAttribute("data-anim-off", "");
        }
      },
      { threshold: 0.04 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);
  return ref;
}

function StageHead({ label }: { label: string }) {
  return (
    <div className="stage-head" aria-hidden>
      <span>{label}</span>
      <span>DEMONSTRATION</span>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* 1 — Script understanding: entities light up, relationships form     */
/* ------------------------------------------------------------------ */

export function ScriptDemo() {
  const ref = useAnimPause<HTMLElement>();
  return (
    <figure
      className="demo-stage stage-script"
      ref={ref}
      aria-label="Demonstration: a script being read — the people, places and things in it light up, and the connections between them form"
    >
      <StageHead label="THE SCRIPT, READ" />
      <div className="script-doc" aria-hidden>
        <p>
          In <span className="ent ent--d1" data-tag="YEAR">1956</span>, the{" "}
          <span className="ent ent--d2" data-tag="VESSEL">Ideal X</span> left{" "}
          <span className="ent ent--d3" data-tag="PLACE">Newark</span> carrying
          fifty-eight steel boxes.
        </p>
        <p>
          <span className="ent ent--d4" data-tag="PERSON">Malcom McLean</span>{" "}
          had bet everything on one simple idea.
        </p>
        <p>The world never traded the same way again.</p>
      </div>
      <div className="script-links" aria-hidden>
        <span className="sl-node sl-node--d4">McLEAN</span>
        <span className="sl-link sl-link--d5" />
        <span className="sl-node sl-node--d2">IDEAL X</span>
        <span className="sl-link sl-link--d6" />
        <span className="sl-node sl-node--d3">NEWARK</span>
      </div>
    </figure>
  );
}

/* ------------------------------------------------------------------ */
/* 2 — Visual discovery: a line of script, and the visuals arrive      */
/* ------------------------------------------------------------------ */

const DISCOVERY_TILES = [
  { label: "FOOTAGE", tone: "a", dur: "0:07" },
  { label: "ARCHIVAL", tone: "b", dur: "" },
  { label: "PHOTOGRAPH", tone: "c", dur: "" },
  { label: "MAP", tone: "d", dur: "" },
  { label: "DIAGRAM", tone: "e", dur: "" },
  { label: "FOOTAGE", tone: "f", dur: "0:12" },
];

export function DiscoveryDemo() {
  const ref = useAnimPause<HTMLElement>();
  return (
    <figure
      className="demo-stage stage-discover"
      ref={ref}
      aria-label="Demonstration: a line of narration, and footage, archival material and graphics arriving for it"
    >
      <StageHead label="VISUAL DISCOVERY" />
      <div className="disc-line" aria-hidden>
        <span className="disc-quote">“…the harbour at dawn, the cranes still…”</span>
        <span className="disc-scan" />
      </div>
      <div className="disc-grid" aria-hidden>
        {DISCOVERY_TILES.map((tile, i) => (
          <div
            key={i}
            className={`disc-tile disc-tile--${tile.tone}`}
            style={{ animationDelay: `${-(0.3 + i * 0.5)}s` }}
          >
            {tile.dur ? (
              <span className="disc-play">
                <Play size={11} strokeWidth={2.4} aria-hidden /> {tile.dur}
              </span>
            ) : null}
            <span className="disc-tag">{tile.label}</span>
          </div>
        ))}
      </div>
    </figure>
  );
}

/* ------------------------------------------------------------------ */
/* 3 — Asset selection: candidates weighed, the right ones settle     */
/* ------------------------------------------------------------------ */

const CANDIDATES = [
  { tone: "a", kept: true },
  { tone: "b", kept: false },
  { tone: "c", kept: true },
  { tone: "d", kept: false },
  { tone: "e", kept: true },
];

export function SelectionDemo() {
  const ref = useAnimPause<HTMLElement>();
  return (
    <figure
      className="demo-stage stage-select"
      ref={ref}
      aria-label="Demonstration: candidate frames being weighed against the story — the right ones are kept and take their place in the film"
    >
      <StageHead label="THE CUTTING ROOM" />
      <div className="sel-row" aria-hidden>
        {CANDIDATES.map((cand, i) => (
          <div
            key={i}
            className={`sel-tile sel-tile--${cand.tone} ${cand.kept ? "is-kept" : "is-passed"}`}
            style={{ "--d": `${-i * 0.3}s` } as React.CSSProperties}
          >
            {cand.kept ? (
              <span className="sel-check">
                <Check size={12} strokeWidth={3} aria-hidden />
              </span>
            ) : null}
          </div>
        ))}
      </div>
      <div className="sel-strip" aria-hidden>
        <span className="sel-strip-label">THE FILM</span>
        {CANDIDATES.filter((c) => c.kept).map((cand, i) => (
          <span
            key={i}
            className={`sel-slot sel-slot--${cand.tone}`}
            style={{ animationDelay: `${-(2.6 + i * 0.5)}s` }}
          />
        ))}
      </div>
    </figure>
  );
}

/* ------------------------------------------------------------------ */
/* 4 — Visual planning: a storyboard forms from the narration          */
/* ------------------------------------------------------------------ */

const BOARD_LINES = [
  "A converted tanker leaves Newark.",
  "Fifty-eight boxes, one idea.",
  "Ports around the world rebuild.",
  "The box becomes the unit.",
];

const BOARD_TILES = [
  { label: "WIDE", art: "wide" },
  { label: "DETAIL", art: "detail" },
  { label: "ARCHIVE", art: "archive" },
  { label: "MAP", art: "map" },
  { label: "GRAPHIC", art: "graphic" },
  { label: "TITLE", art: "title" },
];

export function StoryboardDemo() {
  const ref = useAnimPause<HTMLElement>();
  return (
    <figure
      className="demo-stage stage-board"
      ref={ref}
      aria-label="Demonstration: narration lines on one side, and a storyboard of scenes forming from them on the other"
    >
      <StageHead label="THE STORYBOARD" />
      <div className="board-wrap" aria-hidden>
        <div className="board-lines">
          {BOARD_LINES.map((line, i) => (
            <p key={i} className="board-line" style={{ animationDelay: `${-i * 1.5}s` }}>
              {line}
            </p>
          ))}
        </div>
        <div className="board-grid">
          {BOARD_TILES.map((tile, i) => (
            <div
              key={tile.label}
              className={`board-tile board-tile--${tile.art}`}
              style={{ animationDelay: `${-(0.8 + i * 1.05)}s` }}
            >
              <span className="board-art" />
              <span className="board-tag">{tile.label}</span>
            </div>
          ))}
        </div>
      </div>
    </figure>
  );
}

/* ------------------------------------------------------------------ */
/* 8 — Final production: the parts converge into one finished film     */
/* ------------------------------------------------------------------ */

const CONVERGE_CHIPS = ["VISUALS", "NARRATION", "GRAPHICS", "MUSIC", "CAPTIONS"];

export function ConvergeDemo() {
  const ref = useAnimPause<HTMLElement>();
  return (
    <figure
      className="demo-stage stage-converge"
      ref={ref}
      aria-label="Demonstration: visuals, narration, graphics, music and captions converging into a single finished film"
    >
      <StageHead label="ONE FINISHED FILM" />
      <div className="conv-field" aria-hidden>
        {CONVERGE_CHIPS.map((chip, i) => (
          <span key={chip} className={`conv-chip conv-chip--${i + 1}`}>
            {chip}
          </span>
        ))}
        <div className="conv-frame">
          <span className="conv-fill" />
          <span className="conv-play">
            <Play size={17} strokeWidth={2.2} aria-hidden />
          </span>
          <span className="conv-title">The Shipping Container</span>
          <span className="conv-meta">A FINISHED FILM · 1080P</span>
        </div>
      </div>
    </figure>
  );
}
