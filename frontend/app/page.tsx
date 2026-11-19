import Link from "next/link";
import { BrandLockup } from "@/components/brand-lockup";
import { MarketingNavigation } from "@/components/marketing-navigation";
import { ProductionTeamShowcase } from "@/components/production-team-showcase";
import { ArrowUpRight } from "lucide-react";

const stages = [
  { no: "01", label: "STORY", title: "Shape the narration", note: "Bring a script into the production workspace and review its planned story beats." },
  { no: "02", label: "VISUALS", title: "Build the visual thread", note: "Review researched media alongside the passage and scene it is meant to support." },
  { no: "03", label: "SOUND", title: "Choose voice and sound", note: "Set narration, captions, and optional music or motion graphics." },
  { no: "04", label: "FINISH", title: "Review the finished cut", note: "Follow production status and open the export when the project is complete." },
];
const capabilities = [
  { href: "/create", no: "01", title: "Documentary production", note: "Script, narration, source-aware visual planning, captions, sound, and rendering." },
  { href: "/projects", no: "02", title: "Projects and editing", note: "Return to productions, review scene visuals, and make supported timeline edits." },
  { href: "/channel-lab", no: "03", title: "Script research", note: "Research a topic and develop a fresh script from a channel’s broad subject area." },
  { href: "/thumbnail", no: "04", title: "Thumbnail studio", note: "Create a separate cover image for a documentary or video project." },
  { href: "/viral-clips", no: "05", title: "Short-clip extraction", note: "Review candidate moments from an accessible video link or a file you provide." },
];
const niches = ["True crime", "History", "Business & finance", "Science & space", "Nature", "Technology", "Sports", "Culture", "Biographies", "Sleep stories"];
const productionRoles = ["Research", "Narration", "Visual planning", "Sound & captions", "Editing"];

export default function Home() {
  return <main className="landing-page">
    <MarketingNavigation />
    <section className="landing-hero" aria-labelledby="hero-title">
      <div className="hero-copy"><p className="eyebrow"><span className="eyebrow-mark" /> DOCUMENTARY PRODUCTION, IN ONE WORKSPACE</p><h1 id="hero-title">A clearer path<br /><em>from script to screen.</em></h1><p className="hero-deck">Develop the story, shape its visual thread, choose the voice and sound, then review the cut in one connected studio.</p><div className="hero-actions"><Link className="button button-primary" href="/create">Create a video <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link><Link className="button button-quiet" href="#workflow">Explore the workflow</Link></div><p className="hero-proof"><span /> YOUR STORY · YOUR EDITORIAL CHOICES</p></div>
      <div className="hero-note"><span className="hero-note-mark" aria-hidden="true">C</span><p><strong>Made for considered storytelling.</strong><br />A working space for the choices behind a documentary.</p></div>
    </section>

    <ProductionTeamShowcase />

    <section className="signal-strip" aria-label="Production stages">{productionRoles.map((role, index) => <span key={role}><i>0{index + 1}</i>{role}</span>)}</section>

    <section className="workflow-section" id="workflow" aria-labelledby="workflow-title">
      <div className="section-intro"><p className="eyebrow">A FILM IS A SERIES OF DECISIONS</p><h2 id="workflow-title">Keep each decision<br />close to the story.</h2><p>Move through production in stages. Review the work as it develops and follow the status reported by your project.</p><Link className="text-link" href="/create">See the production form <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></div>
      <div className="workflow-list">{stages.map((stage) => <article className="workflow-row" key={stage.no}><span className="workflow-number">{stage.no}</span><div><span className="workflow-kicker">{stage.label}</span><h3>{stage.title}</h3><p>{stage.note}</p></div></article>)}</div>
    </section>

    <section className="niche-section" id="stories" aria-labelledby="niche-title"><div className="niche-heading"><p className="eyebrow">A RANGE OF SUBJECTS</p><h2 id="niche-title">Different worlds.<br /><em>The same care for the story.</em></h2><p>Start with the subject you know—or research one you are just beginning to explore.</p></div><div className="niche-grid">{niches.map((niche, index) => <div className="niche-card" key={niche}><span>0{index + 1}</span><strong>{niche}</strong></div>)}</div></section>

    <section className="capability-section" id="capabilities" aria-labelledby="capabilities-title"><div className="capability-heading"><div><p className="eyebrow">TOOLS THAT ARE HERE TODAY</p><h2 id="capabilities-title">One studio.<br /><em>Every part of the process.</em></h2></div><Link className="button button-outline" href="/studio">Open your workspace <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></div><div className="capability-list">{capabilities.map((item) => <Link href={item.href} key={item.no}><span className="capability-number">{item.no}</span><span className="capability-name">{item.title}</span><span className="capability-detail">{item.note}</span><ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link>)}</div></section>

    <section className="editorial-section" aria-labelledby="editorial-title"><div className="editorial-rule" aria-hidden="true"><span>RESEARCH</span><i /><span>STORY</span><i /><span>EDIT</span></div><div className="editorial-copy"><p className="eyebrow">A WORKFLOW BUILT FOR REVIEW</p><h2 id="editorial-title">Make the process visible.<br /><em>Keep the editorial choices yours.</em></h2><p>Cinemora brings research, planning, voice, visuals, and rendering into one production path. Review the sources and scene choices as you work.</p><Link className="text-link" href="/projects">Explore the project library <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></div><div className="editorial-annotation"><span>EDITORIAL CONTROL</span><p>Search availability is not reuse permission. Review facts, source details, and media rights before publishing.</p></div></section>

    <section className="faq-section" aria-labelledby="faq-title"><div><p className="eyebrow">A FEW CLEAR ANSWERS</p><h2 id="faq-title">Before you begin.</h2></div><div className="faq-list"><details><summary>What does Cinemora do?</summary><p>It connects script work, visual planning, narration, optional music and graphics, captions, and rendering in one production workflow.</p></details><details><summary>Can I review and edit a production?</summary><p>The project library shows project status and available exports. Supported projects can be edited by scene, then rendered as a new export.</p></details><details><summary>Does finding footage mean it is cleared to reuse?</summary><p>No. Search availability is not reuse permission. Confirm the source and license allow your intended use before publishing.</p></details></div></section>

    <section className="closing-cta" aria-labelledby="closing-title"><div><p className="eyebrow">BRING YOUR NEXT STORY</p><h2 id="closing-title">Start with a script.<br /><em>Shape the cut as you go.</em></h2><p>Choose your narration, sound, and visual direction in the production form.</p></div><Link className="button button-primary" href="/create">Start a production <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></section>
    <footer className="landing-footer"><Link className="marketing-brand" href="/" aria-label="Cinemora home"><BrandLockup /></Link><p>Research-led filmmaking, from draft to cut.</p><nav aria-label="Footer navigation"><Link href="/studio">Studio</Link><Link href="/projects">Projects</Link><Link href="/create">Create</Link><Link href="/channel-lab">Script</Link><Link href="/thumbnail">Thumbnail</Link><Link href="/viral-clips">Short clips</Link></nav><span className="footer-year">© {new Date().getFullYear()} CINEMORA</span></footer>
  </main>;
}
