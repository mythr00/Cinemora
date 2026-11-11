import type { Metadata } from "next";
import type { ReactNode } from "react";

export const metadata: Metadata = { title: "Project library", robots: { index: false, follow: true } };

export default function ProjectsLayout({ children }: { children: ReactNode }) {
  return children;
}
