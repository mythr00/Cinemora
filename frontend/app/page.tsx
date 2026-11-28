import Link from "next/link";
import { BrandLockup } from "@/components/brand-lockup";
import { MarketingNavigation } from "@/components/marketing-navigation";
import { Showcase } from "@/components/landing/showcase";
import { ScrollReveal } from "@/components/landing/scroll-reveal";
import {
  DiscoveryDemo,
  ScriptDemo,
  SelectionDemo,
  StoryboardDemo,
} from "@/components/landing/studio-demos";
import { LandingFeatures } from "@/components/landing/features";
import { LandingPricing } from "@/components/landing/pricing";
import { FinishedFilm } from "@/components/landing/finished";
import { Workflow } from "@/components/landing/workflow";
import { ArrowUpRight } from "lucide-react";

const niches = ["True crime", "History", "Engineering", "Finance", "Science", "Nature"];

/** Section marker row: index number, eyebrow, hairline, optional link. */
function SectionHead({ index, eyebrow, children }: { index: string; eyebrow: string; children?: React.ReactNode }) {
  return (
    <div className="section-head" data-reveal>
      <span className="section-index" aria-hidden>{index}</span>
      <p className="eyebrow">{eyebrow}</p>
      <span className="section-rule" aria-hidden />
      {children}
    </div>
  );
}

// Landing page (design philosophy transformation, founder
// directive 2026-11-28, on the presentation redesign's
// foundation): the front door shows WHAT Cinemora does and
// never HOW the engine does it. Copy is benefit-led and
// brief — one idea per section — and capabilities are
// DEMONSTRATED by designed motion panels rather than
// explained or photographed: script understanding, visual
// discovery, asset selection and visual planning as split
// and full-width studio demonstrations, the craft grid
// below them, and a convergence simulation ahead of the
// one real production on the page (Finished Results).
// Display type is Fraunces, an editorial serif — the
// documentary voice; the hero stays centred; cards appear
// only where a card is the right container. Pricing
// figures are the decided ones, untouched.
export default function Home() {
  return <main className="landing-page">
    <ScrollReveal />
    <MarketingNavigation />
    <section className="hero" aria-labelledby="hero-title">
      <div className="l-wrap">
        <div className="hero-copy"><p className="eyebrow"><span className="eyebrow-mark" /> SCRIPT IN. FILM OUT.</p><h1 id="hero-title"><span className="hero-line mask-line"><span>Start with a script.</span></span> <span className="hero-line mask-line"><span>Finish with a <em>film</em>.</span></span></h1><p className="hero-deck">Cinemora turns your script into a finished, narrated documentary — ready to publish.</p><div className="hero-actions"><Link className="button button-primary" href="/create">Create a video <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link><Link className="button button-quiet" href="/studio">Open the studio</Link></div><p className="hero-proof"><span /> YOUR STORY · YOUR EDITORIAL CHOICES</p></div>
        <Showcase />
      </div>
    </section>

    <section className="l-section" id="workflow" aria-labelledby="workflow-title">
      <div className="l-wrap">
        <SectionHead index="01" eyebrow="HOW IT WORKS"><Link className="text-link" href="/create">Start a production <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></SectionHead>
        <div className="section-lede" data-reveal><h2 id="workflow-title">From first draft to finished film.</h2><p>Three steps. No editing software.</p></div>
        <Workflow />
      </div>
    </section>

    <section className="l-section" id="capabilities" aria-labelledby="capabilities-title">
      <div className="l-wrap">
        <SectionHead index="02" eyebrow="INSIDE THE STUDIO"><Link className="text-link" href="/studio">Open your workspace <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></SectionHead>
        <h2 className="l-h2" id="capabilities-title" data-reveal>Everything a film needs.</h2>

        <div className="split-row">
          <div className="split-copy" data-reveal>
            <p className="split-kicker">SCRIPT UNDERSTANDING</p>
            <h3>First, it understands the story.</h3>
            <p>People, places, turning points — read the way an editor reads.</p>
          </div>
          <div data-reveal data-delay="120"><ScriptDemo /></div>
        </div>

        <div className="split-row split-row--flip">
          <div className="split-copy" data-reveal>
            <p className="split-kicker">VISUAL DISCOVERY</p>
            <h3>The right visuals for every story</h3>
            <p>Footage, archival imagery and custom graphics — brought together for the story you are telling.</p>
          </div>
          <div data-reveal data-delay="120"><DiscoveryDemo /></div>
        </div>

        <div className="split-row">
          <div className="split-copy" data-reveal>
            <p className="split-kicker">ASSET SELECTION</p>
            <h3>Only the right frames make the cut.</h3>
            <p>The film keeps what serves the story; the rest falls away.</p>
          </div>
          <div data-reveal data-delay="120"><SelectionDemo /></div>
        </div>
      </div>

      <div className="board-band">
        <div className="l-wrap">
          <div className="board-copy" data-reveal>
            <p className="split-kicker">VISUAL PLANNING</p>
            <h3>Planned scene by scene.</h3>
            <p>A storyboard forms from the narration, ready to shoot.</p>
          </div>
          <div data-reveal data-delay="120"><StoryboardDemo /></div>
        </div>
      </div>

      <div className="l-wrap">
        <LandingFeatures />
      </div>
    </section>

    <section className="l-section pricing-section" id="pricing" aria-labelledby="pricing-title">
      <div className="l-wrap">
        <SectionHead index="03" eyebrow="PRICING" />
        <div className="pricing-heading" data-reveal><h2 id="pricing-title">One studio, three allowances.</h2><p>The same studio and quality on every plan — plans differ only in how much you can produce.</p></div>
        <LandingPricing />
      </div>
    </section>

    <section className="l-section" aria-labelledby="niches-title">
      <div className="l-wrap">
        <SectionHead index="04" eyebrow="MADE FOR YOUR CHANNEL" />
        <div className="niche-grid">
          <div className="niche-copy" data-reveal><h2 id="niches-title">Any story. Any niche.</h2><p>Documentaries on the subjects your audience loves — in eight languages.</p></div>
          <div className="niche-chips" data-reveal data-delay="120">{niches.map((niche) => <span className="niche-chip" key={niche}>{niche}</span>)}<span className="niche-chip niche-chip--accent">+ yours</span></div>
        </div>
      </div>
    </section>

    <FinishedFilm />

    <section className="l-section" id="faq" aria-labelledby="faq-title">
      <div className="l-wrap">
        <SectionHead index="06" eyebrow="A FEW CLEAR ANSWERS" />
        <div className="faq-grid">
          <h2 id="faq-title" data-reveal>Before you begin.</h2>
          <div className="faq-list" data-reveal data-delay="100"><details><summary>What does Cinemora do?</summary><p>You bring a script; Cinemora returns a finished documentary — narration, visuals, captions and music included.</p></details><details><summary>What do I need to provide?</summary><p>A script, and a few choices — voice, captions, music.</p></details><details><summary>Can I review a film before I publish it?</summary><p>Yes — review the plan and the finished cut, and revise anything.</p></details><details><summary>Can I edit a documentary after it renders?</summary><p>Yes — reopen it in the studio editor, adjust anything, and export a new revision.</p></details><details><summary>What does a plan include?</summary><p>Monthly credits and a set of included videos, the same studio and quality at every tier. Each finished video counts once, whatever its length. Top-ups stay valid for 12 months.</p></details><details><summary>What do I get when a film finishes?</summary><p>A full-HD MP4 with narration, captions and music baked in — ready to publish.</p></details></div>
        </div>
      </div>
    </section>

    <section className="closing-cta" aria-labelledby="closing-title"><div data-reveal><p className="eyebrow">YOUR NEXT STORY</p><h2 id="closing-title">Your next film starts as a script.</h2><p>Bring the words. Leave with a film.</p></div><Link className="button button-primary" href="/create">Start a production <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></section>
    <footer className="landing-footer"><Link className="marketing-brand" href="/" aria-label="Cinemora home"><BrandLockup size="marketing" /></Link><p>Documentary filmmaking, from script to screen.</p><nav aria-label="Footer navigation"><Link href="/studio">Studio</Link><Link href="/projects">Projects</Link><Link href="/create">Create</Link><Link href="#pricing">Pricing</Link><Link href="#faq">FAQ</Link></nav><span className="footer-year">© {new Date().getFullYear()} CINEMORA</span></footer>
  </main>;
}
