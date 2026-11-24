"use client";

import { useEffect, useRef } from "react";
import type { StudioWaveform } from "@/lib/api";

/**
 * A real waveform: the peaks are decoded by the backend from the
 * actual audio file (services/editor_studio.waveform) — this
 * component only draws them. `offsetSeconds`/`spanSeconds` select
 * the visible window of the file (a track clip rarely spans the
 * whole timeline at the same scale).
 */
export function Waveform({
  wave,
  offsetSeconds,
  spanSeconds,
}: {
  wave: StudioWaveform;
  offsetSeconds: number;
  spanSeconds: number;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const parent = canvas.parentElement;
    if (!parent) return;
    const width = parent.clientWidth;
    const height = parent.clientHeight;
    if (width <= 0 || height <= 0) return;
    const scale = window.devicePixelRatio || 1;
    canvas.width = Math.floor(width * scale);
    canvas.height = Math.floor(height * scale);
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.scale(scale, scale);
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = "rgba(255, 255, 255, 0.55)";
    const bucketSeconds = wave.bucket_seconds;
    const firstBucket = Math.max(0, Math.floor(offsetSeconds / bucketSeconds));
    const bucketsPerPixel = (spanSeconds / bucketSeconds) / width;
    for (let x = 0; x < width; x += 1) {
      const from = firstBucket + Math.floor(x * bucketsPerPixel);
      const to = Math.max(from + 1, firstBucket + Math.floor((x + 1) * bucketsPerPixel));
      let peak = 0;
      for (let i = from; i < to && i < wave.peaks.length; i += 1) {
        if (wave.peaks[i] > peak) peak = wave.peaks[i];
      }
      const barHeight = Math.max(1, peak * (height - 4));
      ctx.fillRect(x, (height - barHeight) / 2, 1, barHeight);
    }
  }, [wave, offsetSeconds, spanSeconds]);

  return <canvas ref={canvasRef} className="studio-wave" aria-hidden />;
}
