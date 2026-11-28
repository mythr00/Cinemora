import type { Metadata, Viewport } from "next";
import { Inter } from "next/font/google";
import "./globals.css";

/* AUD-35: an explicit viewport export. Next's default meta is
   only `width=device-width, initial-scale=1` — without
   `viewport-fit: cover`, iOS Safari reports every
   env(safe-area-inset-*) as 0, so the floating bottom bar and
   the page bottom clearance in globals.css could never grow
   past the home-indicator area on notched iPhones. */
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

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
    default: "Cinemora — Start with a script. Finish with a film.",
    template: "%s | Cinemora",
  },
  description: "Develop a documentary story, plan its visuals, choose narration and sound, and follow production through to an export in one connected studio.",
  openGraph: {
    title: "Cinemora — Start with a script. Finish with a film.",
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
