import Link from "next/link";
import { BrandLockup } from "@/components/brand-lockup";
import { MarketingNavigation } from "@/components/marketing-navigation";
import { Showcase } from "@/components/landing/showcase";
import { ScrollReveal } from "@/components/landing/scroll-reveal";
import { LandingFeatures } from "@/components/landing/features";
import { LandingPricing } from "@/components/landing/pricing";
import { FinishedFilm } from "@/components/landing/finished";
import { Workflow } from "@/components/landing/workflow";
import { ArrowUpRight } from "lucide-react";

const highlights = ["54 studio voices", "8 languages", "Word-synced captions", "Full-HD MP4", "Review before you publish"];

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

// Landing page (presentation redesign, founder directive
// 2026-11-28): the front door shows WHAT Cinemora does and
// never HOW the engine does it. Copy is benefit-led and
// brief — one idea per section — and features are
// DEMONSTRATED by designed motion panels rather than
// explained or photographed. The only real photograph on
// the page is the finished production in Finished Results,
// where the image is the product itself. Pricing figures
// are the decided ones, untouched. The hero stays centred;
// cards appear only where a card is the right container
// (the demonstration stage, the feature panels, the pricing
// tiers, the finished facts).
export default function Home() {
  return <main className="landing-page">
    <ScrollReveal />
    <MarketingNavigation />
    <section className="hero" aria-labelledby="hero-title">
      <div className="l-wrap">
        <div className="hero-copy"><p className="eyebrow"><span className="eyebrow-mark" /> SCRIPT IN. FILM OUT.</p><h1 id="hero-title"><span className="hero-line">Start with a script.</span> <span className="hero-line">Finish with a film.</span></h1><p className="hero-deck">Cinemora turns your script into a finished, narrated documentary — ready to publish.</p><div className="hero-actions"><Link className="button button-primary" href="/create">Create a video <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link><Link className="button button-quiet" href="/studio">Open the studio</Link></div><p className="hero-proof"><span /> YOUR STORY · YOUR EDITORIAL CHOICES</p></div>
        <Showcase />
      </div>
    </section>

    <section className="signal-strip" aria-label="Studio highlights">{highlights.map((item, index) => <span key={item}><i>0{index + 1}</i>{item}</span>)}</section>

    <section className="l-section" id="workflow" aria-labelledby="workflow-title">
      <div className="l-wrap">
        <SectionHead index="01" eyebrow="HOW IT WORKS"><Link className="text-link" href="/create">Start a production <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></SectionHead>
        <div className="section-lede" data-reveal><h2 id="workflow-title">From first draft to finished film.</h2><p>Three steps. No editing software, no timeline wrestling.</p></div>
        <Workflow />
      </div>
    </section>

    <section className="l-section" id="capabilities" aria-labelledby="capabilities-title">
      <div className="l-wrap">
        <SectionHead index="02" eyebrow="WHAT THE STUDIO DOES"><Link className="text-link" href="/studio">Open your workspace <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></SectionHead>
        <h2 className="l-h2" id="capabilities-title" data-reveal>Everything a film needs.</h2>
        <LandingFeatures />
      </div>
    </section>

    <section className="l-section pricing-section" id="pricing" aria-labelledby="pricing-title">
      <div className="l-wrap">
        <SectionHead index="03" eyebrow="PRICING" />
        <div className="pricing-heading" data-reveal><h2 id="pricing-title">One studio, three allowances.</h2><p>The same studio and the same quality on every plan — plans differ only in how much you can produce.</p></div>
        <LandingPricing />
      </div>
    </section>

    <section className="l-section" aria-labelledby="niches-title">
      <div className="l-wrap">
        <SectionHead index="04" eyebrow="MADE FOR YOUR CHANNEL" />
        <div className="niche-grid">
          <div className="niche-copy" data-reveal><h2 id="niches-title">Any story. Any niche.</h2><p>Documentaries on the subjects your audience already loves — in eight languages.</p></div>
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
          <div className="faq-list" data-reveal data-delay="100"><details><summary>What does Cinemora do?</summary><p>You bring a script; Cinemora turns it into a finished documentary — narration, visuals, captions and music included, delivered as a full-HD MP4.</p></details><details><summary>What do I need to provide?</summary><p>A script, and a few choices: a voice, captions, music. Everything else is handled for you.</p></details><details><summary>Can I review a film before I publish it?</summary><p>Yes. Review the plan and the finished cut, and revise anything — the editorial choices stay yours.</p></details><details><summary>Can I edit a documentary after it renders?</summary><p>Yes. Open it in the studio editor, adjust footage, graphics, captions, narration and music, and export a new revision. The original is never overwritten.</p></details><details><summary>What does a plan include?</summary><p>Monthly credits and a set of included videos, on the same studio and quality at every tier. Each finished video counts once, whatever its length; longer or additional productions draw on credits. Top-ups are available and stay valid for 12 months.</p></details><details><summary>What do I get when a film finishes?</summary><p>A full-HD MP4 with narration, captions and music baked in — ready to upload and publish.</p></details></div>
        </div>
      </div>
    </section>

    <section className="closing-cta" aria-labelledby="closing-title"><div data-reveal><p className="eyebrow">BRING YOUR NEXT STORY</p><h2 id="closing-title">Your next film starts as a script.</h2><p>Bring the words. Leave with a film.</p></div><Link className="button button-primary" href="/create">Start a production <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></section>
    <footer className="landing-footer"><Link className="marketing-brand" href="/" aria-label="Cinemora home"><BrandLockup size="marketing" /></Link><p>Documentary filmmaking, from script to screen.</p><nav aria-label="Footer navigation"><Link href="/studio">Studio</Link><Link href="/projects">Projects</Link><Link href="/create">Create</Link><Link href="#pricing">Pricing</Link><Link href="#faq">FAQ</Link></nav><span className="footer-year">© {new Date().getFullYear()} CINEMORA</span></footer>
  </main>;
}
