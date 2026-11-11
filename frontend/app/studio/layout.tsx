import type { Metadata } from "next";
import type { ReactNode } from "react";

export const metadata: Metadata = { title: "Studio overview", robots: { index: false, follow: true } };

export default function StudioLayout({ children }: { children: ReactNode }) {
  return children;
}
