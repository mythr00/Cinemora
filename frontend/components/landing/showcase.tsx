"use client";

import { useEffect, useRef, useState } from "react";
import { Captions, Music, Pause, Play } from "lucide-react";

/**
 * Landing product showcase (redesign 2026-11-26, directive
 * Phase 3). A controlled, clearly-labelled simulation of the
 * production experience, built entirely from one real Cinemora
 * production — "The Shipping Container" (438 words, 21
 * sentences, 2:30, 1920×1080): the script lines below are the
 * production's own sentences, the stills are its own sourced
 * frames, the chart numbers are its own narration ($5.86 → $0.16
 * per ton). Playback is simulated for demonstration; nothing
 * here calls the API or fabricates a result.
 */

type Scene = {
  id: string;
  tab: string;
  kind: string;
  sentence: string;
};

const SCENES: Scene[] = [
  {
    id: "photo",
    tab: "Archive",
    kind: "ARCHIVAL PHOTO · NEWARK, 1956",
    sentence:
      "On April 26, 1956, a converted oil tanker named the Ideal X left Newark, New Jersey, carrying 58 steel boxes, and sailed for Houston, Texas.",
  },
  {
    id: "map",
    tab: "Map",
    kind: "ROUTE MAP · NEWARK → HOUSTON",
    sentence:
      "The boxes belonged to Malcom McLean, a trucking company owner who had spent years trying to cut the cost of moving cargo.",
  },
  {
    id: "chart",
    tab: "Graphic",
    kind: "MOTION GRAPHIC · COST PER TON",
    sentence: "McLean's boxes brought that cost down to 16 cents per ton.",
  },
  {
    id: "footage",
    tab: "Footage",
    kind: "FOOTAGE · STRADDLE CARRIER, TERMINAL YARD",
    sentence:
      "Automated vehicles and straddle carriers carry each box to its assigned stack in the yard, where rail-mounted cranes store it until a truck or a train takes it inland.",
  },
  {
    id: "board",
    tab: "Evidence",
    kind: "EVIDENCE BOARD · THE CORNER FITTING",
    sentence:
      "Engineer Keith Tantlinger designed the corner fittings that let cranes grip and stack the boxes safely, and he persuaded McLean to release the patent so the design could become a world standard.",
  },
];

const SCENE_MS = 5600;

function SceneVisual({ scene, progress }: { scene: Scene; progress: number }) {
  if (scene.id === "photo") {
    return (
      <div className="showcase-visual">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img className="showcase-media showcase-kenburns" src="/landing/archival-1956.jpg" alt="Archival photograph from the sample production: Newark in the 1950s" />
      </div>
    );
  }
  if (scene.id === "footage") {
    return (
      <div className="showcase-visual">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img className="showcase-media showcase-kenburns" src="/landing/carrier.jpg" alt="A straddle carrier moving a container across a terminal yard, from the sample production" />
      </div>
    );
  }
  if (scene.id === "board") {
    return (
      <div className="showcase-visual">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img className="showcase-media" src="/landing/board-frame.jpg" alt="An evidence-board motion graphic rendered by Cinemora for the sample production" />
      </div>
    );
  }
  if (scene.id === "map") {
    return (
      <div className="showcase-visual showcase-visual--drawn" role="img" aria-label="Animated route map from Newark to Houston">
        <svg className="showcase-map" viewBox="0 0 640 360" aria-hidden>
          <path className="map-coast" d="M470 40 L520 90 L505 140 L545 185 L520 235 L545 300 L500 330 L430 300 L380 320 L300 285 L210 300 L120 260 L90 190 L130 120 L220 95 L330 60 Z" />
          <path className="map-route" d="M492 92 C 430 150, 380 210, 322 262" />
          <circle className="map-pin" cx="492" cy="92" r="7" />
          <circle className="map-pin" cx="322" cy="262" r="7" />
          <text className="map-label" x="470" y="72">NEWARK</text>
          <text className="map-label" x="262" y="292">HOUSTON</text>
          <text className="map-sub" x="330" y="180">1,400 MILES BY SEA</text>
        </svg>
      </div>
    );
  }
  // chart
  const eased = 1 - Math.pow(1 - progress, 3);
  const cost = 5.86 - (5.86 - 0.16) * eased;
  return (
    <div className="showcase-visual showcase-visual--drawn" role="img" aria-label="Animated chart: loading cost per ton falls from 5.86 dollars to 16 cents">
      <div className="showcase-chart">
        <p className="chart-title">LOADING COST · PER TON</p>
        <div className="chart-row">
          <span className="chart-year">1956 · LOOSE CARGO</span>
          <span className="chart-bar chart-bar--before" style={{ width: `${Math.max(4, 100 - eased * 62)}%` }} />
          <strong>$5.86</strong>
        </div>
        <div className="chart-row">
          <span className="chart-year">THE CONTAINER</span>
          <span className="chart-bar chart-bar--after" style={{ width: `${4 + eased * 10}%` }} />
          <strong>${cost.toFixed(2)}</strong>
        </div>
        <p className="chart-note">From $5.86 to $0.16 a ton — the drop the whole film turns on.</p>
      </div>
    </div>
  );
}

export function Showcase() {
  const [sceneIndex, setSceneIndex] = useState(0);
  const [progress, setProgress] = useState(0);
  const [playing, setPlaying] = useState(true);
  const stateRef = useRef({ sceneIndex: 0, progress: 0, playing: true });

  useEffect(() => {
    stateRef.current = { sceneIndex, progress, playing };
  }, [sceneIndex, progress, playing]);

  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setPlaying(false);
    }
    let raf = 0;
    let last = performance.now();
    const tick = (now: number) => {
      const s = stateRef.current;
      if (s.playing) {
        const next = s.progress + (now - last) / SCENE_MS;
        if (next >= 1) {
          const following = (s.sceneIndex + 1) % SCENES.length;
          stateRef.current = { sceneIndex: following, progress: 0, playing: true };
          setSceneIndex(following);
          setProgress(0);
        } else {
          stateRef.current = { ...s, progress: next };
          setProgress(next);
        }
      }
      last = now;
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);

  const jumpTo = (index: number) => {
    stateRef.current = { sceneIndex: index, progress: 0, playing: stateRef.current.playing };
    setSceneIndex(index);
    setProgress(0);
  };

  const scene = SCENES[sceneIndex];
  const words = scene.sentence.split(" ");
  const sungCount = playing || progress > 0 ? Math.ceil(progress * words.length) : 0;

  return (
    <figure className="showcase" aria-label="Product showcase: a sample Cinemora production">
      <div className="showcase-window">
        <div className="showcase-topbar">
          <span className="showcase-dots" aria-hidden><i /><i /><i /></span>
          <span className="showcase-title">THE SHIPPING CONTAINER — PRODUCTION</span>
          <span className="showcase-badge">PRODUCT SHOWCASE · SAMPLE PROJECT</span>
        </div>
        <div className="showcase-body">
          <div className="showcase-narration">
            <p className="showcase-panel-label">NARRATION</p>
            <ol className="showcase-script">
              {SCENES.map((item, index) => (
                <li key={item.id}>
                  <button
                    type="button"
                    className={index === sceneIndex ? "is-active" : ""}
                    aria-current={index === sceneIndex ? "true" : undefined}
                    onClick={() => jumpTo(index)}
                  >
                    {item.sentence}
                  </button>
                </li>
              ))}
            </ol>
            <dl className="showcase-stats">
              <div><dt>WORDS</dt><dd>438</dd></div>
              <div><dt>SENTENCES</dt><dd>21</dd></div>
              <div><dt>EST. DURATION</dt><dd>2:30</dd></div>
              <div><dt>LANGUAGE</dt><dd>English</dd></div>
              <div><dt>VOICE</dt><dd>Studio narration</dd></div>
            </dl>
          </div>
          <div className="showcase-stage">
            <SceneVisual key={scene.id} scene={scene} progress={progress} />
            <span className="showcase-kind">{scene.kind}</span>
            <span className="showcase-music" aria-hidden>
              <span className="eq"><i /><i /><i /><i /></span>
              <Music size={13} strokeWidth={2} /> MUSIC BED
            </span>
            <div className="showcase-captions" aria-hidden>
              <Captions size={15} strokeWidth={2} className="showcase-captions-icon" />
              <p>
                {words.map((word, index) => (
                  <span key={index} className={index < sungCount ? "is-sung" : ""}>
                    {word}{index < words.length - 1 ? " " : ""}
                  </span>
                ))}
              </p>
            </div>
          </div>
        </div>
        <div className="showcase-controls">
          <button
            type="button"
            className="showcase-play"
            aria-label={playing ? "Pause the showcase" : "Play the showcase"}
            onClick={() => setPlaying((current) => !current)}
          >
            {playing ? <Pause size={15} strokeWidth={2.2} /> : <Play size={15} strokeWidth={2.2} />}
          </button>
          <div className="showcase-tabs" role="tablist" aria-label="Showcase scenes">
            {SCENES.map((item, index) => (
              <button
                key={item.id}
                type="button"
                role="tab"
                aria-selected={index === sceneIndex}
                className={index === sceneIndex ? "is-active" : ""}
                onClick={() => jumpTo(index)}
              >
                <span>{item.tab}</span>
                {index === sceneIndex && <i style={{ width: `${Math.round(progress * 100)}%` }} aria-hidden />}
              </button>
            ))}
          </div>
          <span className="showcase-file">documentary.mp4 · 1920×1080</span>
        </div>
      </div>
      <figcaption className="showcase-caption">
        A walkthrough of a real Cinemora production: the script, stills and graphics are the
        production&apos;s own — playback is simulated here for demonstration.
      </figcaption>
    </figure>
  );
}
