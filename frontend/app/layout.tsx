import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";

/* C-09: Inter is the single typeface for body and headings, loaded via
   next/font (self-hosted at build time, no external request at runtime)
   and exposed as --font-inter; the design tokens in globals.css build
   --font-sans on it. */
const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  display: "swap",
});

export const metadata: Metadata = {
  title: {
    default: "Cinemora — Documentary production studio",
    template: "%s | Cinemora",
  },
  description: "Develop a documentary story, plan its visuals, choose narration and sound, and follow production through to an export in one connected studio.",
  openGraph: {
    title: "Cinemora — Documentary production studio",
    description: "A connected workspace for documentary story development, visual research, narration, sound, and video production.",
    type: "website",
  },
  icons: {
    icon: "/cinemora-mark.svg",
    apple: "/cinemora-mark.svg",
  },
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`h-full antialiased ${inter.variable}`}>
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
