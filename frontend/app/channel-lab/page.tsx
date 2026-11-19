import Link from "next/link";
import { AppNavigation } from "@/components/app-navigation";
import { ArrowUpRight } from "lucide-react";

/**
 * Quarantined in Brief Stage 3 (C-02): the channel-research endpoints
 * this page called (/channel-research, /channel-research/script) do
 * not exist in the committed backend — re-verified 404 under
 * TestClient at HEAD bfc5a06. The original implementation is
 * preserved verbatim at frontend/preserved/channel-lab-page.tsx.
 * This route stays reachable so old links land somewhere honest,
 * but it is removed from all navigation and offers no dead controls.
 */
export default function ChannelLabUnavailable() {
  return <main className="app-page">
    <AppNavigation active="" />
    <div className="tool-page-content">
      <section className="tool-page-heading">
        <p className="eyebrow">SCRIPT STUDIO</p>
        <h1>Script research<br /><em>isn't available in this build.</em></h1>
        <p>The channel-research service this page depended on is not part of the current Cinemora backend, so the tool has been withdrawn rather than left in place to fail. Scriptwriting itself is unaffected: productions start from a script you paste into the production form, and story analysis and visual research run inside every production.</p>
      </section>
      <p>
        <Link className="button button-primary" href="/create">Start a production <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link>{" "}
        <Link className="button button-secondary" href="/projects">View projects</Link>
      </p>
      <footer className="app-footer"><span>CINEMORA / SCRIPT</span><span>This feature is not available in the current build.</span></footer>
    </div>
  </main>;
}
