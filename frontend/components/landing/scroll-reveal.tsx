"use client";

import { useEffect } from "react";

/**
 * Scroll reveal system (premium frontend mission, 2026-11-28).
 *
 * One observer for the whole landing page: elements carrying
 * `data-reveal` fade/rise in as they enter the viewport, with an
 * optional `data-delay` stagger (ms). Deliberately dependency-
 * free — the effect is one-shot and fully CSS-expressible, so no
 * animation runtime ships for it.
 *
 * Safety properties:
 * - Content is hidden ONLY under `html.reveal-on`, a class this
 *   component adds after mount. No JS → no hiding → the page is
 *   complete without JavaScript.
 * - `prefers-reduced-motion` (and a missing IntersectionObserver)
 *   reveals everything immediately; the global reduced-motion CSS
 *   rule flattens the transition anyway.
 * - A safety timer reveals anything the observer missed, and the
 *   `data-reveal` attribute is removed once an element's reveal
 *   finishes, handing its transitions back to its own hover rules.
 */
const REVEAL_MS = 700;

export function ScrollReveal() {
  useEffect(() => {
    const els = Array.from(
      document.querySelectorAll<HTMLElement>(".landing-page [data-reveal]"),
    );
    if (els.length === 0) return;
    document.documentElement.classList.add("reveal-on");

    const finish = (el: HTMLElement) => {
      el.classList.add("is-revealed");
      const delay = Number(el.dataset.delay ?? 0) || 0;
      window.setTimeout(() => {
        el.removeAttribute("data-reveal");
        el.style.transitionDelay = "";
      }, REVEAL_MS + delay + 60);
    };

    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced || !("IntersectionObserver" in window)) {
      els.forEach(finish);
      return;
    }

    const io = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            finish(entry.target as HTMLElement);
            io.unobserve(entry.target);
          }
        }
      },
      { threshold: 0.1, rootMargin: "0px 0px -6% 0px" },
    );
    els.forEach((el) => {
      const delay = Number(el.dataset.delay ?? 0) || 0;
      if (delay > 0) el.style.transitionDelay = `${delay}ms`;
      io.observe(el);
    });
    const safety = window.setTimeout(() => els.forEach(finish), 3000);
    return () => {
      io.disconnect();
      window.clearTimeout(safety);
    };
  }, []);

  return null;
}
