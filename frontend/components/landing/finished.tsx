import Link from "next/link";
import { ArrowUpRight, BadgeCheck, FileText, ScrollText } from "lucide-react";

/**
 * Finished results (premium frontend mission, 2026-11-28;
 * composition rebuilt 2026-11-28, premium frontend rebuild).
 *
 * Presents a completed production the way a film programme
 * presents a screening: a full-bleed frame from "The Shipping
 * Container", then the production's own delivery record as
 * credits — runtime, resolution, sentence and word counts,
 * and the bundle every export ships with (MP4, provenance
 * certificate, asset report, rights manifest). The same facts
 * the showcase and export row already publish; no invented
 * results, no fake player, no card.
 */
export function FinishedFilm() {
  return (
    <section className="programme-section" aria-labelledby="finished-title">
      <div className="l-wrap">
        <div className="section-head" data-reveal>
          <span className="section-index" aria-hidden>05</span>
          <p className="eyebrow">FINISHED RESULTS</p>
          <span className="section-rule" aria-hidden />
          <Link className="text-link" href="/projects">
            Browse delivered projects <ArrowUpRight size={14} strokeWidth={2} aria-hidden />
          </Link>
        </div>
        <h2 id="finished-title" data-reveal>A finished film, with its papers.</h2>
      </div>
      <figure className="programme-band" data-reveal>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src="/landing/containers.jpg" width={960} height={720} alt="A frame from the completed Cinemora production 'The Shipping Container': stacked shipping containers" />
        <figcaption>
          <span className="programme-film-title">“The Shipping Container”</span>
          <span className="programme-film-meta">COMPLETED PRODUCTION</span>
        </figcaption>
      </figure>
      <div className="l-wrap">
        <div className="programme-credits" data-reveal>
          <dl className="programme-facts">
            <div><dt>RUNTIME</dt><dd>2:30</dd></div>
            <div><dt>FORMAT</dt><dd>1920×1080 MP4</dd></div>
            <div><dt>SCRIPT</dt><dd>438 words · 21 sentences</dd></div>
            <div><dt>DELIVERED AS</dt><dd>Film + evidence bundle</dd></div>
          </dl>
          <p className="programme-note">
            Every production leaves the studio as a full-HD MP4 — and a record of
            where every second came from, so the film can be checked, not just watched.
          </p>
          <ul className="programme-bundle">
            <li><BadgeCheck size={14} strokeWidth={2} aria-hidden /> Provenance certificate</li>
            <li><FileText size={14} strokeWidth={2} aria-hidden /> Asset report</li>
            <li><ScrollText size={14} strokeWidth={2} aria-hidden /> Rights manifest</li>
          </ul>
        </div>
      </div>
    </section>
  );
}
