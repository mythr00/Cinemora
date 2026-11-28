"use client";

import { useEffect, useRef } from "react";

/**
 * Parallax (design philosophy transformation, 2026-11-28).
 * The page's single parallax moment: the finished film's
 * frame drifts gently inside its full-bleed band as the band
 * crosses the viewport. Transform-only, rAF-throttled, active
 * only while the band is near the viewport, and completely
 * inert under prefers-reduced-motion. The child is rendered
 * oversized by CSS so the drift never opens an edge.
 */
export function Parallax({
  children,
  range = 0.035,
  className,
}: {
  children: React.ReactNode;
  range?: number;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    let raf = 0;
    let ticking = false;
    const update = () => {
      ticking = false;
      const rect = el.getBoundingClientRect();
      const vh = window.innerHeight || 1;
      if (rect.bottom < -100 || rect.top > vh + 100) return;
      const centre = rect.top + rect.height / 2;
      const offset = ((centre - vh / 2) / vh) * range * 100;
      el.style.transform = `translateY(${offset.toFixed(2)}%)`;
    };
    const onScroll = () => {
      if (!ticking) {
        ticking = true;
        raf = requestAnimationFrame(update);
      }
    };
    update();
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll, { passive: true });
    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
      cancelAnimationFrame(raf);
    };
  }, [range]);

  return (
    <div ref={ref} className={className} aria-hidden={false}>
      {children}
    </div>
  );
}
