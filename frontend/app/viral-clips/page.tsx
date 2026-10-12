"use client";

import { useState } from "react";
import Image from "next/image";

const API_URL = (process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000").replace(/\/$/, "");

type Clip = {
  clip: number;
  start_seconds: number;
  end_seconds: number;
  duration_seconds?: number;
  potential_score: number;
  transcript: string;
  file: string;
};

function timestamp(seconds: number) {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

export default function ViralClipsPage() {
  const [url, setUrl] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [clips, setClips] = useState<Clip[]>([]);
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  async function run(mode: "url" | "upload") {
    if (mode === "url" && !url.trim()) {
      setMessage("Paste a YouTube video link first.");
      return;
    }
    if (mode === "upload" && !file) {
      setMessage("Choose a video file first.");
      return;
    }

    setBusy(true);
    setMessage("Analyzing video and creating your clips...");
    setClips([]);

    try {
      let response: Response;

      if (mode === "url") {
        response = await fetch(`${API_URL}/viral-clips`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ youtube_url: url.trim() }),
        });
      } else {
        const form = new FormData();
        form.append("file", file!);
        response = await fetch(`${API_URL}/viral-clips/upload`, {
          method: "POST",
          body: form,
        });
      }

      const data = await response.json();

      if (!response.ok) {
        throw new Error(
          typeof data.detail === "string"
            ? data.detail
            : "Video processing failed."
        );
      }

      setClips(data.clips || []);
      setTitle(data.video_title || file?.name || "Your video");
      setMessage(`Finished! ${data.clips_created} clips created.`);
    } catch (error) {
      setMessage(
        error instanceof Error ? error.message : "Something went wrong."
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main style={{
      minHeight: "100vh",
      background: "#e6e7e9",
      color: "#17191c",
      padding: "40px 20px",
      fontFamily: "Arial, sans-serif"
    }}>
      <div style={{ maxWidth: 1000, margin: "0 auto" }}>
        <a href="/" style={{ display: "inline-flex", alignItems: "center", gap: 9, color: "#242629", textDecoration: "none", fontSize: 12 }}>
          <Image src="/cinemora-mark.svg" alt="" width={25} height={25} />
          Back to Cinemora
        </a>

        <header style={{ margin: "32px 0" }}>
          <p style={{ color: "#565a5f", fontWeight: 700, letterSpacing: 1.5, fontSize: 10 }}>
            CINEMORA / VIDEO TOOLS
          </p>
          <h1 style={{ fontSize: "clamp(32px, 5vw, 48px)", margin: "12px 0" }}>
            Viral Clips
          </h1>
          <p style={{ color: "#4f5358", lineHeight: 1.7, maxWidth: 640, fontSize: 14 }}>
            Turn a long video into five short-form clips. Find promising
            moments, review the timestamps, and prepare vertical videos.
          </p>
        </header>

        <section style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))",
          gap: 20
        }}>
          <div style={panel}>
            <h2 style={{ fontSize: 20 }}>Paste a YouTube link</h2>
            <p style={muted}>Use a video accessible through normal authorized access.</p>
            <input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://www.youtube.com/watch?v=..."
              style={inputStyle}
            />
            <button
              onClick={() => run("url")}
              disabled={busy}
              style={buttonStyle}
            >
              Extract 5 clips
            </button>
          </div>

          <div style={panel}>
            <h2 style={{ fontSize: 20 }}>Upload a video</h2>
            <p style={muted}>MP4, MOV, MKV, WEBM or AVI</p>
            <input
              type="file"
              accept="video/mp4,video/quicktime,video/x-matroska,video/webm,video/x-msvideo"
              onChange={(e) => setFile(e.target.files?.[0] || null)}
              style={{ ...inputStyle, padding: 10 }}
            />
            {file && (
              <p style={muted}>
                Selected: {file.name} ({(file.size / 1024 / 1024).toFixed(1)} MB)
              </p>
            )}
            <button
              onClick={() => run("upload")}
              disabled={busy}
              style={buttonStyle}
            >
              {busy ? "Processing..." : "Create 5 clips"}
            </button>
          </div>
        </section>

        {message && (
          <div role="status" style={{
            ...panel,
            marginTop: 24,
            color: "#202226",
            overflowWrap: "anywhere"
          }}>
            {busy && "? "}{message}
          </div>
        )}

        {clips.length > 0 && (
          <section style={{ marginTop: 36 }}>
            <h2 style={{ fontSize: 26 }}>Your clips</h2>
            <p style={muted}>{title}</p>

            <div style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
              gap: 20,
              marginTop: 20
            }}>
              {clips.map((clip) => {
                const videoUrl = new URL(clip.file.replace(/^\/+/, ""), `${API_URL}/`).toString();
                return (
                  <article key={clip.clip} style={panel}>
                    <div style={{
                      display: "flex",
                      justifyContent: "space-between",
                      gap: 12,
                      alignItems: "center"
                    }}>
                      <h3 style={{ margin: 0 }}>Clip {clip.clip}</h3>
                      <span style={{
                        color: "#25272a",
                        background: "#e1e2e4",
                        border: "1px solid #c8cace",
                        padding: "6px 10px",
                        borderRadius: 2,
                        fontSize: 12
                      }}>
                        Score {clip.potential_score}
                      </span>
                    </div>

                    <p style={muted}>
                      {timestamp(clip.start_seconds)} - {timestamp(clip.end_seconds)}
                    </p>

                    <video
                      controls
                      playsInline
                      preload="metadata"
                      src={videoUrl}
                      style={{
                        width: "100%",
                        maxHeight: 420,
                        aspectRatio: "9 / 16",
                        objectFit: "contain",
                        background: "#000",
                        borderRadius: 2
                      }}
                    />

                    <p style={{
                      color: "#34373b",
                      fontSize: 13,
                      lineHeight: 1.6
                    }}>
                      {clip.transcript}
                    </p>

                    <a
                      href={videoUrl}
                      download={`viral-clip-${clip.clip}.mp4`}
                      style={{ color: "#202226" }}
                    >
                      Download MP4
                    </a>
                  </article>
                );
              })}
            </div>
          </section>
        )}
      </div>
    </main>
  );
}

const panel: React.CSSProperties = {
  background: "#f4f4f5",
  border: "1px solid #cfd2d6",
  borderRadius: 4,
  padding: 20
};

const muted: React.CSSProperties = {
  color: "#55595e",
  fontSize: 14,
  lineHeight: 1.6
};

const inputStyle: React.CSSProperties = {
  display: "block",
  width: "100%",
  boxSizing: "border-box",
  background: "#f8f8f9",
  color: "#17191c",
  border: "1px solid #c9cbce",
  borderRadius: 3,
  padding: 14,
  margin: "16px 0",
  fontSize: 14
};

const buttonStyle: React.CSSProperties = {
  width: "100%",
  border: "1px solid #191a1c",
  borderRadius: 3,
  padding: 14,
  background: "#242629",
  color: "white",
  fontSize: 15,
  fontWeight: 700,
  cursor: "pointer"
};
