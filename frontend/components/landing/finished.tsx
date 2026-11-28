import Link from "next/link";
import { ArrowUpRight } from "lucide-react";
import { ConvergeDemo } from "./studio-demos";
import { Parallax } from "./parallax";

/**
 * Finished results (presentation redesign, founder directive
 * 2026-11-28; design philosophy transformation). The section
 * opens with an illustrative simulation — the parts of a
 * production converging into one finished film — and then
 * presents the real thing: an actual Cinemora production,
 * outcome-only, its own frame at full bleed with a gentle
 * parallax drift, its title, and the facts a viewer cares
 * about. No delivery internals, no evidence bundle, no
 * process. The photograph appears here because here the
 * photograph IS the product.
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
        <p className="programme-lede" data-reveal>Every part, in one finished film.</p>
        <ConvergeDemo />
      </div>
      <figure className="programme-band" data-reveal>
        <Parallax className="programme-parallax">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/landing/containers.jpg" width={960} height={720} alt="A frame from the completed Cinemora production 'The Shipping Container': stacked shipping containers" />
        </Parallax>
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
