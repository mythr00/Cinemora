import Link from "next/link";
import { BrandLockup } from "@/components/brand-lockup";
import { MarketingNavigation } from "@/components/marketing-navigation";
import { ArrowUpRight } from "lucide-react";

const stages = [
  { no: "01", label: "STORY", title: "Shape the narration", note: "Bring a script into the production workspace and review its planned story beats." },
  { no: "02", label: "VISUALS", title: "Build the visual thread", note: "Review researched media alongside the passage and scene it is meant to support." },
  { no: "03", label: "VOICE", title: "Narrate the story", note: "Your script is narrated with the studio's built-in English voice, with captions prepared for the finished film." },
  { no: "04", label: "FINISH", title: "Review the finished cut", note: "Follow production status and open the export when the project is complete." },
];
// Brief Stage 3 (C-15): this list names only what a user can
// actually do in this build. Script research, thumbnail generation
// and short-clip extraction were removed — their backends do not
// exist (register C-02). Brief Stage 7 (C-03) re-verification: the
// revision editor now has a working screen, so the editing
// capability returns below — written against the real editor
// (revision-based, non-destructive, per-revision certificates),
// not the quarantined legacy one.
const capabilities = [
  { href: "/create", no: "01", title: "Documentary production", note: "Bring a script: story analysis, source-aware visual planning, narration, captions, and rendering." },
  { href: "/projects", no: "02", title: "Plan review and approval", note: "Open a project to inspect its per-sentence plan — proposed visuals, evidence and rights — and approve it before rendering." },
  { href: "/projects", no: "03", title: "Projects and provenance", note: "Status, outputs and downloads for every production, with its asset report, rights manifest and provenance certificate." },
  { href: "/projects", no: "04", title: "Revision editing", note: "Open a finished production in the editor and render a new export. Every edit is a new revision — the original never changes." },
];
const productionRoles = ["Research", "Narration", "Visual planning", "Sound & captions", "Final edit"];

// Redesign F1 (plan §3.1): the landing is a front door, not a
// dashboard. Hero, the production stages, the honest capability
// list and FAQ, footer. The ProductionTeamShowcase theatre, the
// niche grid (subjects live in Create's genre step, from the
// real catalogue) and the hero-note "C" tile (§3.14 L6) are
// removed; no data fetching, no controls beyond navigation.
export default function Home() {
  return <main className="landing-page">
    <MarketingNavigation />
    <section className="landing-hero" aria-labelledby="hero-title">
      <div className="hero-copy"><p className="eyebrow"><span className="eyebrow-mark" /> DOCUMENTARY PRODUCTION, IN ONE WORKSPACE</p><h1 id="hero-title">A clearer path from script to screen.</h1><p className="hero-deck">Develop the story, review the plan sentence by sentence, and follow the cut — in one connected studio.</p><div className="hero-actions"><Link className="button button-primary" href="/create">Create a video <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link><Link className="button button-quiet" href="/studio">Open the studio</Link></div><p className="hero-proof"><span /> YOUR STORY · YOUR EDITORIAL CHOICES</p></div>
    </section>

    <section className="signal-strip" aria-label="Production stages">{productionRoles.map((role, index) => <span key={role}><i>0{index + 1}</i>{role}</span>)}</section>

    <section className="workflow-section" id="workflow" aria-labelledby="workflow-title">
      <div className="section-intro"><p className="eyebrow">A FILM IS A SERIES OF DECISIONS</p><h2 id="workflow-title">Keep each decision close to the story.</h2><p>Move through production in stages, reviewing the work as it develops.</p><Link className="text-link" href="/create">See the production form <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></div>
      <div className="workflow-list">{stages.map((stage) => <article className="workflow-row" key={stage.no}><span className="workflow-number">{stage.no}</span><div><span className="workflow-kicker">{stage.label}</span><h3>{stage.title}</h3><p>{stage.note}</p></div></article>)}</div>
    </section>

    <section className="capability-section" id="capabilities" aria-labelledby="capabilities-title"><div className="capability-heading"><div><p className="eyebrow">TOOLS THAT ARE HERE TODAY</p><h2 id="capabilities-title">One studio. Every part of the process.</h2></div><Link className="button button-outline" href="/studio">Open your workspace <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></div><div className="capability-list">{capabilities.map((item) => <Link href={item.href} key={item.no}><span className="capability-number">{item.no}</span><span className="capability-name">{item.title}</span><span className="capability-detail">{item.note}</span><ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link>)}</div></section>

    <section className="editorial-section" aria-labelledby="editorial-title"><div className="editorial-copy"><p className="eyebrow">A WORKFLOW BUILT FOR REVIEW</p><h2 id="editorial-title">Make the process visible. Keep the editorial choices yours.</h2><p>Research, planning, voice, visuals and rendering in one production path — with the sources visible as you work.</p><Link className="text-link" href="/projects">Explore the project library <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></div><div className="editorial-annotation"><span>EDITORIAL CONTROL</span><p>Search availability is not reuse permission. Review facts, source details, and media rights before publishing.</p></div></section>

    <section className="faq-section" aria-labelledby="faq-title"><div><p className="eyebrow">A FEW CLEAR ANSWERS</p><h2 id="faq-title">Before you begin.</h2></div><div className="faq-list"><details><summary>What does Cinemora do?</summary><p>It connects script work, visual planning, narration, captions, and rendering in one production workflow — and records the evidence behind every visual it uses.</p></details><details><summary>Can I review a production before it renders?</summary><p>Yes. Choose plan review when you create a project: production pauses after story analysis, and you approve the per-sentence plan before any footage is retrieved or rendered.</p></details><details><summary>Can I edit a documentary after it renders?</summary><p>Yes. Open it in the studio editor and work on its structure — footage, graphics, captions, narration and music. Each edit is saved as a new revision with its own export; the original is never overwritten.</p></details><details><summary>Does finding footage mean it is cleared to reuse?</summary><p>No. Search availability is not reuse permission. Confirm the source and license allow your intended use before publishing.</p></details></div></section>

    <section className="closing-cta" aria-labelledby="closing-title"><div><p className="eyebrow">BRING YOUR NEXT STORY</p><h2 id="closing-title">Start with a script. Shape the cut as you go.</h2><p>Bring a script — the studio plans, narrates and renders it.</p></div><Link className="button button-primary" href="/create">Start a production <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></section>
    <footer className="landing-footer"><Link className="marketing-brand" href="/" aria-label="Cinemora home"><BrandLockup size="marketing" /></Link><p>Research-led filmmaking, from draft to cut.</p><nav aria-label="Footer navigation"><Link href="/studio">Studio</Link><Link href="/projects">Projects</Link><Link href="/create">Create</Link></nav><span className="footer-year">© {new Date().getFullYear()} CINEMORA</span></footer>
  </main>;
}
