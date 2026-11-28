"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Workflow sequence (premium frontend rebuild, 2026-11-28).
 *
 * The journey from script to finished film as a scroll-driven
 * sequence: a frame from the sample production stays pinned on
 * the left while the four production stages scroll past on the
 * right; the frame crossfades to the stage nearest the middle
 * of the viewport. The stills are the production's own
 * artefacts (public/landing/*). On narrow screens the sticky
 * frame gives way to a still inside each stage. The global
 * reduced-motion rule flattens the crossfade; the stage change
 * itself is a state swap, not an animation.
 */

type Stage = {
  no: string;
  label: string;
  title: string;
  note: string;
  img: string;
  alt: string;
};

const STAGES: Stage[] = [
  {
    no: "01",
    label: "STORY",
    title: "Shape the narration",
    note: "Bring a script into the production workspace and review its planned story beats.",
    img: "/landing/film-frame.jpg",
    alt: "Frames from the sample production, reviewed as the story takes shape",
  },
  {
    no: "02",
    label: "VISUALS",
    title: "Build the visual thread",
    note: "Review researched media alongside the passage and scene it is meant to support.",
    img: "/landing/terminal.jpg",
    alt: "A container terminal still sourced for the sample production",
  },
  {
    no: "03",
    label: "VOICE",
    title: "Narrate the story",
    note: "Choose from 54 studio voices in eight languages, with captions timed to the narration word by word.",
    img: "/landing/archival-1956.jpg",
    alt: "Archival photograph from the sample production: Newark in the 1950s",
  },
  {
    no: "04",
    label: "FINISH",
    title: "Review the finished cut",
    note: "Follow production status and open the export when the project is complete.",
    img: "/landing/carrier.jpg",
    alt: "A straddle carrier moving a container across a terminal yard, from the sample production",
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

  const stage = STAGES[active];

  return (
    <div className="workflow-seq">
      <div className="workflow-visual" aria-hidden>
        <div className="workflow-frame">
          {STAGES.map((item, index) => (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              key={item.no}
              src={item.img}
              alt=""
              className={index === active ? "is-active" : ""}
            />
          ))}
          <span className="workflow-frame-caption">
            {stage.no} · {stage.label} — “THE SHIPPING CONTAINER”
          </span>
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
            <figure className="workflow-step-media">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={item.img} alt={item.alt} />
            </figure>
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
