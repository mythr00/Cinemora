import Link from "next/link";
import { AppNavigation } from "@/components/app-navigation";
import { ArrowUpRight } from "lucide-react";

/**
 * Quarantined in Brief Stage 3 (C-02): the thumbnail endpoints this
 * page called (/thumbnails, /thumbnails/generate) do not exist in
 * the committed backend — re-verified 404 under TestClient at HEAD
 * bfc5a06. The original implementation is preserved verbatim at
 * frontend/preserved/thumbnail-page.tsx. This route stays reachable
 * so old links land somewhere honest, but it is removed from all
 * navigation and offers no dead controls.
 */
export default function ThumbnailUnavailable() {
  return <main className="app-page">
    <AppNavigation />
    <div className="tool-page-content">
      <section className="tool-page-heading">
        <p className="eyebrow">IMAGE WORKSPACE</p>
        <h1>Thumbnail generation isn’t available in this build.</h1>
        <p>The image-generation service this page depended on is not part of the current Cinemora backend, so the tool has been withdrawn rather than left in place to fail. Your projects, finished films and production records — plans, outputs and provenance certificates — are all in the project library.</p>
      </section>
      <p>
        <Link className="button button-primary" href="/projects">View projects <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link>{" "}
        <Link className="button button-secondary" href="/create">Start a production</Link>
      </p>
      <footer className="app-footer"><span>CINEMORA / THUMBNAIL</span><span>This feature is not available in the current build.</span></footer>
    </div>
  </main>;
}
