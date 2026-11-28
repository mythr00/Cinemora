"use client";

import { useEffect, useRef, useState } from "react";
import { Play } from "lucide-react";

/**
 * Workflow sequence (presentation redesign, founder directive
 * 2026-11-28). The journey from script to finished film as a
 * designed progression — no production photographs. A panel
 * stays pinned while three short steps scroll past: script
 * lines, then clips assembling on a timeline, then the
 * finished-film panel. The zone matching the step nearest the
 * middle of the viewport lights up. On narrow screens the
 * panel sits above the steps in normal flow.
 */

type Stage = {
  no: string;
  label: string;
  title: string;
  note: string;
};

const STAGES: Stage[] = [
  {
    no: "01",
    label: "WRITE",
    title: "Bring your script",
    note: "Paste it in — that is the only material you need to start.",
  },
  {
    no: "02",
    label: "REVIEW",
    title: "Watch it take shape",
    note: "See the film come together, and revise anything before you publish.",
  },
  {
    no: "03",
    label: "PUBLISH",
    title: "Leave with a film",
    note: "A full-HD MP4 with narration, captions and music — ready for your channel.",
  },
];

export function Workflow() {
  const [active, setActive] = useState(0);
  const stepRefs = useRef<Array<HTMLElement | null>>([]);

  useEffect(() => {
    if (!("IntersectionObserver" in window)) return;
    const io = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            const index = Number((entry.target as HTMLElement).dataset.index ?? 0);
            setActive(index);
          }
        }
      },
      { rootMargin: "-42% 0px -42% 0px", threshold: 0 },
    );
    stepRefs.current.forEach((el) => el && io.observe(el));
    return () => io.disconnect();
  }, []);

  return (
    <div className="workflow-seq">
      <div className="workflow-visual">
        <div className="wf-panel" aria-hidden>
          <div className={`wf-zone wf-zone--script ${active === 0 ? "is-active" : ""}`}>
            <span className="wf-zone-label">THE SCRIPT</span>
            <span className="wf-line wf-line--w92" />
            <span className="wf-line wf-line--w78" />
            <span className="wf-line wf-line--w85" />
            <span className="wf-line wf-line--w60" />
          </div>
          <span className="wf-arrow" aria-hidden>→</span>
          <div className={`wf-zone wf-zone--cut ${active === 1 ? "is-active" : ""}`}>
            <span className="wf-zone-label">THE CUT</span>
            <div className="wf-mini-track">
              <span className="wf-block wf-block--a" />
              <span className="wf-block wf-block--b" />
              <span className="wf-block wf-block--c" />
            </div>
            <div className="wf-mini-wave">
              {Array.from({ length: 24 }, (_, i) => (
                <i key={i} style={{ height: `${20 + Math.round(55 * Math.abs(Math.sin(i * 1.4)))}%` }} />
              ))}
            </div>
          </div>
          <span className="wf-arrow" aria-hidden>→</span>
          <div className={`wf-zone wf-zone--film ${active === 2 ? "is-active" : ""}`}>
            <span className="wf-zone-label">THE FILM</span>
            <div className="wf-film-frame">
              <span className="wf-play"><Play size={16} strokeWidth={2.2} aria-hidden /></span>
              <span className="wf-film-meta">documentary.mp4 · 1080p</span>
            </div>
          </div>
        </div>
        <div className="workflow-progress" aria-hidden>
          {STAGES.map((item, index) => (
            <span key={item.no} className={index <= active ? "is-done" : ""} />
          ))}
        </div>
      </div>
      <div className="workflow-steps">
        {STAGES.map((item, index) => (
          <article
            key={item.no}
            className={`workflow-step${index === active ? " is-active" : ""}`}
            data-index={index}
            ref={(el) => {
              stepRefs.current[index] = el;
            }}
          >
            <p className="workflow-step-no" aria-hidden>{item.no}</p>
            <p className="workflow-kicker">{item.label}</p>
            <h3>{item.title}</h3>
            <p className="workflow-note">{item.note}</p>
          </article>
        ))}
      </div>
    </div>
  );
}
