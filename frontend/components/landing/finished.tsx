import Link from "next/link";
import { ArrowUpRight } from "lucide-react";

/**
 * Finished results (presentation redesign, founder directive
 * 2026-11-28). A real Cinemora production, presented
 * outcome-only: the film's own frame at full bleed, its
 * title, and the facts a viewer cares about — runtime,
 * format, the script it came from. No delivery internals,
 * no evidence bundle, no process. This is the one place on
 * the page a real photograph appears, because here the
 * photograph IS the product: actual Cinemora output.
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
        <h2 id="finished-title" data-reveal>A real film, made with Cinemora.</h2>
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
            <div><dt>TITLE</dt><dd>The Shipping Container</dd></div>
            <div><dt>RUNTIME</dt><dd>2:30</dd></div>
            <div><dt>FORMAT</dt><dd>1920×1080 MP4</dd></div>
            <div><dt>SCRIPT</dt><dd>438 words</dd></div>
          </dl>
        </div>
      </div>
    </section>
  );
}
