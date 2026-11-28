import Link from "next/link";
import { ArrowUpRight, BadgeCheck, FileText, ScrollText } from "lucide-react";

/**
 * Finished results (premium frontend mission, 2026-11-28).
 *
 * Presents a completed production as a delivered result: a real
 * frame from "The Shipping Container" and the production's own
 * delivery record — runtime, resolution, sentence and word
 * counts, and the bundle every export ships with (MP4,
 * provenance certificate, asset report, rights manifest). The
 * same facts the showcase and export card already publish; no
 * invented results, no fake player.
 */
export function FinishedFilm() {
  return (
    <section className="finished-section" aria-labelledby="finished-title">
      <div className="capability-heading" data-reveal>
        <div>
          <p className="eyebrow">FINISHED RESULTS</p>
          <h2 id="finished-title">A finished film, with its papers.</h2>
        </div>
        <Link className="button button-outline" href="/projects">
          Browse delivered projects <ArrowUpRight size={14} strokeWidth={2} aria-hidden />
        </Link>
      </div>
      <article className="finished-card" data-reveal>
        <figure className="finished-media">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/landing/containers.jpg" alt="A frame from the completed Cinemora production 'The Shipping Container': stacked shipping containers" />
          <figcaption>“THE SHIPPING CONTAINER” · COMPLETED PRODUCTION</figcaption>
        </figure>
        <div className="finished-detail">
          <dl className="finished-facts">
            <div><dt>RUNTIME</dt><dd>2:30</dd></div>
            <div><dt>FORMAT</dt><dd>1920×1080 MP4</dd></div>
            <div><dt>SCRIPT</dt><dd>438 words · 21 sentences</dd></div>
            <div><dt>DELIVERED AS</dt><dd>Film + evidence bundle</dd></div>
          </dl>
          <p>
            Every production leaves the studio as a full-HD MP4 — and a record of
            where every second came from, so the film can be checked, not just watched.
          </p>
          <ul className="finished-bundle">
            <li><BadgeCheck size={14} strokeWidth={2} aria-hidden /> Provenance certificate</li>
            <li><FileText size={14} strokeWidth={2} aria-hidden /> Asset report</li>
            <li><ScrollText size={14} strokeWidth={2} aria-hidden /> Rights manifest</li>
          </ul>
        </div>
      </article>
    </section>
  );
}
