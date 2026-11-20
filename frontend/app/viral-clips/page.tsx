import Link from "next/link";
import { AppNavigation } from "@/components/app-navigation";
import { ArrowUpRight } from "lucide-react";

/**
 * Quarantined in Brief Stage 3 (C-02): the clip-extraction endpoints
 * this page called (/viral-clips, /viral-clips/upload) do not exist
 * in the committed backend — re-verified 404 under TestClient at
 * HEAD bfc5a06. The original implementation is preserved verbatim
 * at frontend/preserved/viral-clips-page.tsx. This route stays
 * reachable so old links land somewhere honest, but it is removed
 * from all navigation and offers no dead controls.
 */
export default function ViralClipsUnavailable() {
  return <main className="app-page">
    <AppNavigation />
    <div className="tool-page-content">
      <section className="tool-page-heading">
        <p className="eyebrow">SHORT-FORM WORKFLOW</p>
        <h1>Short-clip extraction isn’t available in this build.</h1>
        <p>The clip-extraction service this page depended on is not part of the current Cinemora backend, so the tool has been withdrawn rather than left in place to fail. The current build focuses on full documentary production: script, plan review, narration, source-aware visuals and rendering in one workspace.</p>
      </section>
      <p>
        <Link className="button button-primary" href="/create">Start a production <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link>{" "}
        <Link className="button button-secondary" href="/projects">View projects</Link>
      </p>
      <footer className="app-footer"><span>CINEMORA / SHORT CLIPS</span><span>This feature is not available in the current build.</span></footer>
    </div>
  </main>;
}
