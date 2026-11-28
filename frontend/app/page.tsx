import Link from "next/link";
import { BrandLockup } from "@/components/brand-lockup";
import { MarketingNavigation } from "@/components/marketing-navigation";
import { Showcase } from "@/components/landing/showcase";
import { LandingFeatures } from "@/components/landing/features";
import { LandingPricing } from "@/components/landing/pricing";
import { ArrowUpRight } from "lucide-react";

const stages = [
  { no: "01", label: "STORY", title: "Shape the narration", note: "Bring a script into the production workspace and review its planned story beats." },
  { no: "02", label: "VISUALS", title: "Build the visual thread", note: "Review researched media alongside the passage and scene it is meant to support." },
  { no: "03", label: "VOICE", title: "Narrate the story", note: "Choose from 54 studio voices in eight languages, with captions timed to the narration word by word." },
  { no: "04", label: "FINISH", title: "Review the finished cut", note: "Follow production status and open the export when the project is complete." },
];
const productionRoles = ["Research", "Narration", "Visual planning", "Sound & captions", "Final edit"];

// Landing redesign (2026-11-26, founder directive): the front
// door now shows the product working — a labelled showcase of
// a real production inside the hero, ten demonstrated
// capabilities, the decided pricing, FAQ, closing CTA. Every
// claim still maps to a capability that exists in this build;
// the showcase is a controlled simulation built from a real
// production's own artefacts, and says so on its face.
export default function Home() {
  return <main className="landing-page">
    <MarketingNavigation />
    <section className="landing-hero" aria-labelledby="hero-title">
      <div className="hero-copy"><p className="eyebrow"><span className="eyebrow-mark" /> SCRIPT IN. FILM OUT.</p><h1 id="hero-title">Start with a script. Finish with a film.</h1><p className="hero-deck">Develop the story, review the plan sentence by sentence, and follow the cut as it comes together.</p><div className="hero-actions"><Link className="button button-primary" href="/create">Create a video <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link><Link className="button button-quiet" href="/studio">Open the studio</Link></div><p className="hero-proof"><span /> YOUR STORY · YOUR EDITORIAL CHOICES</p></div>
      <Showcase />
    </section>

    <section className="signal-strip" aria-label="Production stages">{productionRoles.map((role, index) => <span key={role}><i>0{index + 1}</i>{role}</span>)}</section>

    <section className="workflow-section" id="workflow" aria-labelledby="workflow-title">
      <div className="section-intro"><p className="eyebrow">A FILM IS A SERIES OF DECISIONS</p><h2 id="workflow-title">Keep each decision close to the story.</h2><p>Move through production in stages, reviewing the work as it develops.</p><Link className="text-link" href="/create">See the production form <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></div>
      <div className="workflow-list">{stages.map((stage) => <article className="workflow-row" key={stage.no}><span className="workflow-number">{stage.no}</span><div><span className="workflow-kicker">{stage.label}</span><h3>{stage.title}</h3><p>{stage.note}</p></div></article>)}</div>
    </section>

    <section className="capability-section" id="capabilities" aria-labelledby="capabilities-title">
      <div className="capability-heading"><div><p className="eyebrow">INSIDE EVERY PRODUCTION</p><h2 id="capabilities-title">See the work, craft by craft.</h2></div><Link className="button button-outline" href="/studio">Open your workspace <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></div>
      <LandingFeatures />
    </section>

    <section className="pricing-section" id="pricing" aria-labelledby="pricing-title">
      <div className="pricing-heading"><p className="eyebrow">PRICING</p><h2 id="pricing-title">One studio, three allowances.</h2><p>Every plan runs the same production engine at the same quality. Plans differ only in how much you can produce, how fast it renders, and how your team works.</p></div>
      <LandingPricing />
    </section>

    <section className="editorial-section" aria-labelledby="editorial-title"><div className="editorial-copy"><p className="eyebrow">A WORKFLOW BUILT FOR REVIEW</p><h2 id="editorial-title">Make the process visible. Keep the editorial choices yours.</h2><p>Research, planning, voice, visuals and rendering in one production path — with the sources visible as you work.</p><Link className="text-link" href="/projects">Explore the project library <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></div><div className="editorial-annotation"><span>EDITORIAL CONTROL</span><p>Search availability is not reuse permission. Review facts, source details, and media rights before publishing.</p></div></section>

    <section className="faq-section" id="faq" aria-labelledby="faq-title"><div><p className="eyebrow">A FEW CLEAR ANSWERS</p><h2 id="faq-title">Before you begin.</h2></div><div className="faq-list"><details><summary>What does Cinemora do?</summary><p>It connects script work, visual planning, narration, captions, and rendering in one production workflow — and records the evidence behind every visual it uses.</p></details><details><summary>What do I need to provide?</summary><p>A script. You choose the settings that matter — voice, captions, music, plan review — and Cinemora handles research, visuals, narration and rendering from there.</p></details><details><summary>Can I review a production before it renders?</summary><p>Yes. Choose plan review when you create a project: production pauses after story analysis, and you approve the per-sentence plan before any footage is retrieved or rendered.</p></details><details><summary>Can I edit a documentary after it renders?</summary><p>Yes. Open it in the studio editor and work on its structure — footage, graphics, captions, narration and music. Each edit is saved as a new revision with its own export; the original is never overwritten.</p></details><details><summary>What does a plan include?</summary><p>Monthly credits and a set of included videos, on the same production engine and quality at every tier. Each finished video counts once, whatever its length; longer or additional productions draw on credits. Top-ups are available and stay valid for 12 months.</p></details><details><summary>Does finding footage mean it is cleared to reuse?</summary><p>No. Search availability is not reuse permission. Confirm the source and license allow your intended use before publishing — every production carries a rights manifest to help you check.</p></details></div></section>

    <section className="closing-cta" aria-labelledby="closing-title"><div><p className="eyebrow">BRING YOUR NEXT STORY</p><h2 id="closing-title">Your next film starts as a script.</h2><p>Bring the words — the studio plans, narrates and renders the rest.</p></div><Link className="button button-primary" href="/create">Start a production <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></section>
    <footer className="landing-footer"><Link className="marketing-brand" href="/" aria-label="Cinemora home"><BrandLockup size="marketing" /></Link><p>Research-led filmmaking, from draft to cut.</p><nav aria-label="Footer navigation"><Link href="/studio">Studio</Link><Link href="/projects">Projects</Link><Link href="/create">Create</Link><Link href="#pricing">Pricing</Link><Link href="#faq">FAQ</Link></nav><span className="footer-year">© {new Date().getFullYear()} CINEMORA</span></footer>
  </main>;
}
