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

const productionRoles = ["Research", "Narration", "Visual planning", "Sound & captions", "Final edit"];

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

// Landing page (copy pass 2026-11-28; composition rebuilt
// 2026-11-28, premium frontend rebuild): the front door shows
// the product working — a labelled showcase of a real
// production inside the hero, a scroll-driven workflow, ten
// demonstrated capabilities as an editorial index, the
// decided pricing, a finished production presented as a film
// programme entry, FAQ, closing CTA. The composition is
// full-bleed and editorial — oversized type, hairline rules,
// numbered sections — with no page-in-a-card shell; the
// balance pass (2026-11-28, founder correction) returns cards
// only where a card is the right container: the pricing
// tiers, the showcase stage and the finished facts. Every claim still maps to a capability that
// exists in this build; the showcase is a controlled
// simulation built from a real production's own artefacts,
// and says so on its face.
export default function Home() {
  return <main className="landing-page">
    <ScrollReveal />
    <MarketingNavigation />
    <section className="hero" aria-labelledby="hero-title">
      <div className="l-wrap">
        <div className="hero-copy"><p className="eyebrow"><span className="eyebrow-mark" /> SCRIPT IN. FILM OUT.</p><h1 id="hero-title"><span className="hero-line">Start with a script.</span> <span className="hero-line">Finish with a film.</span></h1><p className="hero-deck">Develop the story, review the plan sentence by sentence, and follow the cut as it comes together.</p><div className="hero-actions"><Link className="button button-primary" href="/create">Create a video <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link><Link className="button button-quiet" href="/studio">Open the studio</Link></div><p className="hero-proof"><span /> YOUR STORY · YOUR EDITORIAL CHOICES</p></div>
        <Showcase />
      </div>
    </section>

    <section className="signal-strip" aria-label="Production stages">{productionRoles.map((role, index) => <span key={role}><i>0{index + 1}</i>{role}</span>)}</section>

    <section className="l-section" id="workflow" aria-labelledby="workflow-title">
      <div className="l-wrap">
        <SectionHead index="01" eyebrow="A FILM IS A SERIES OF DECISIONS"><Link className="text-link" href="/create">See the production form <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></SectionHead>
        <div className="section-lede" data-reveal><h2 id="workflow-title">Keep each decision close to the story.</h2><p>Move through production in stages, reviewing the work as it develops.</p></div>
        <Workflow />
      </div>
    </section>

    <section className="l-section" id="capabilities" aria-labelledby="capabilities-title">
      <div className="l-wrap">
        <SectionHead index="02" eyebrow="INSIDE EVERY PRODUCTION"><Link className="text-link" href="/studio">Open your workspace <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></SectionHead>
        <h2 className="l-h2" id="capabilities-title" data-reveal>See the work, craft by craft.</h2>
        <LandingFeatures />
      </div>
    </section>

    <section className="l-section pricing-section" id="pricing" aria-labelledby="pricing-title">
      <div className="l-wrap">
        <SectionHead index="03" eyebrow="PRICING" />
        <div className="pricing-heading" data-reveal><h2 id="pricing-title">One studio, three allowances.</h2><p>Every plan runs the same production engine at the same quality. Plans differ only in how much you can produce, how fast it renders, and how your team works.</p></div>
        <LandingPricing />
      </div>
    </section>

    <section className="l-section" aria-labelledby="editorial-title">
      <div className="l-wrap">
        <SectionHead index="04" eyebrow="A WORKFLOW BUILT FOR REVIEW" />
        <div className="editorial-grid">
          <div className="editorial-copy" data-reveal><h2 id="editorial-title">Make the process visible. Keep the editorial choices yours.</h2><p>Research, planning, voice, visuals and rendering in one production path — with the sources visible as you work.</p><Link className="text-link" href="/projects">Explore the project library <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></div>
          <div className="editorial-annotation" data-reveal data-delay="120"><span>EDITORIAL CONTROL</span><p>Search availability is not reuse permission. Review facts, source details, and media rights before publishing.</p></div>
        </div>
      </div>
    </section>

    <FinishedFilm />

    <section className="l-section" id="faq" aria-labelledby="faq-title">
      <div className="l-wrap">
        <SectionHead index="06" eyebrow="A FEW CLEAR ANSWERS" />
        <div className="faq-grid">
          <h2 id="faq-title" data-reveal>Before you begin.</h2>
          <div className="faq-list" data-reveal data-delay="100"><details><summary>What does Cinemora do?</summary><p>It connects script work, visual planning, narration, captions, and rendering in one production workflow — and records the evidence behind every visual it uses.</p></details><details><summary>What do I need to provide?</summary><p>A script. You choose the settings that matter — voice, captions, music, plan review — and Cinemora handles research, visuals, narration and rendering from there.</p></details><details><summary>Can I review a production before it renders?</summary><p>Yes. Choose plan review when you create a project: production pauses after story analysis, and you approve the per-sentence plan before any footage is retrieved or rendered.</p></details><details><summary>Can I edit a documentary after it renders?</summary><p>Yes. Open it in the studio editor and work on its structure — footage, graphics, captions, narration and music. Each edit is saved as a new revision with its own export; the original is never overwritten.</p></details><details><summary>What does a plan include?</summary><p>Monthly credits and a set of included videos, on the same production engine and quality at every tier. Each finished video counts once, whatever its length; longer or additional productions draw on credits. Top-ups are available and stay valid for 12 months.</p></details><details><summary>Does finding footage mean it is cleared to reuse?</summary><p>No. Search availability is not reuse permission. Confirm the source and license allow your intended use before publishing — every production carries a rights manifest to help you check.</p></details></div>
        </div>
      </div>
    </section>

    <section className="closing-cta" aria-labelledby="closing-title"><div data-reveal><p className="eyebrow">BRING YOUR NEXT STORY</p><h2 id="closing-title">Your next film starts as a script.</h2><p>Bring the words — the studio plans, narrates and renders the rest.</p></div><Link className="button button-primary" href="/create">Start a production <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></section>
    <footer className="landing-footer"><Link className="marketing-brand" href="/" aria-label="Cinemora home"><BrandLockup size="marketing" /></Link><p>Research-led filmmaking, from draft to cut.</p><nav aria-label="Footer navigation"><Link href="/studio">Studio</Link><Link href="/projects">Projects</Link><Link href="/create">Create</Link><Link href="#pricing">Pricing</Link><Link href="#faq">FAQ</Link></nav><span className="footer-year">© {new Date().getFullYear()} CINEMORA</span></footer>
  </main>;
}
