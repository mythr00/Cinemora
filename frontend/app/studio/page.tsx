"use client";

import { useMemo, useState } from "react";

type Clip = {
  id: string;
  name: string;
  track: string;
  start: number;
  duration: number;
  type: "video" | "image" | "graphic" | "audio" | "caption";
};

const initialClips: Clip[] = [
  {
    id: "clip-1",
    name: "Seattle aerial",
    track: "VIDEO",
    start: 0,
    duration: 7,
    type: "video",
  },
  {
    id: "clip-2",
    name: "Viaduct footage",
    track: "B-ROLL",
    start: 7,
    duration: 9,
    type: "video",
  },
  {
    id: "clip-3",
    name: "SR99 timeline",
    track: "GRAPHICS",
    start: 16,
    duration: 6,
    type: "graphic",
  },
  {
    id: "clip-4",
    name: "Bertha machine",
    track: "B-ROLL",
    start: 22,
    duration: 8,
    type: "video",
  },
  {
    id: "clip-5",
    name: "Narration",
    track: "VOICE",
    start: 0,
    duration: 30,
    type: "audio",
  },
  {
    id: "clip-6",
    name: "Documentary score",
    track: "MUSIC",
    start: 0,
    duration: 30,
    type: "audio",
  },
  {
    id: "clip-7",
    name: "Captions",
    track: "CAPTIONS",
    start: 0,
    duration: 30,
    type: "caption",
  },
];

const tracks = [
  "VIDEO",
  "B-ROLL",
  "GRAPHICS",
  "VOICE",
  "MUSIC",
  "SFX",
  "CAPTIONS",
];

const assets = [
  { name: "Seattle aerial", type: "VIDEO", duration: "00:07" },
  { name: "Alaskan Way Viaduct", type: "VIDEO", duration: "00:09" },
  { name: "Bertha TBM", type: "VIDEO", duration: "00:08" },
  { name: "SR99 Timeline", type: "GRAPHIC", duration: "00:06" },
  { name: "Launch Pit", type: "IMAGE", duration: "—" },
  { name: "Narration", type: "AUDIO", duration: "00:30" },
];

export default function StudioPage() {
  const [clips, setClips] = useState(initialClips);
  const [selectedId, setSelectedId] = useState("clip-2");
  const [activePanel, setActivePanel] = useState("effects");
  const [playing, setPlaying] = useState(false);

  const [effects, setEffects] = useState({
    blur: 0,
    opacity: 100,
    scale: 100,
    x: 0,
    y: 0,
    rotation: 0,
    speed: 100,
    exposure: 0,
    contrast: 0,
    saturation: 0,
    temperature: 0,
  });

  const selectedClip = useMemo(
    () => clips.find((clip) => clip.id === selectedId),
    [clips, selectedId]
  );

  function updateEffect(
    key: keyof typeof effects,
    value: number
  ) {
    setEffects((current) => ({
      ...current,
      [key]: value,
    }));
  }

  function splitClip() {
    if (!selectedClip) return;

    const half = selectedClip.duration / 2;

    const first: Clip = {
      ...selectedClip,
      id: `${selectedClip.id}-a-${Date.now()}`,
      duration: half,
    };

    const second: Clip = {
      ...selectedClip,
      id: `${selectedClip.id}-b-${Date.now()}`,
      start: selectedClip.start + half,
      duration: half,
    };

    setClips((current) => [
      ...current.filter((clip) => clip.id !== selectedClip.id),
      first,
      second,
    ]);

    setSelectedId(first.id);
  }

  function deleteClip() {
    if (!selectedClip) return;

    setClips((current) =>
      current.filter((clip) => clip.id !== selectedClip.id)
    );

    setSelectedId("");
  }

  return (
    <main className="min-h-screen bg-[#070708] text-white">
      {/* TOP BAR */}
      <header className="flex h-16 items-center justify-between border-b border-zinc-800 bg-[#0b0b0d] px-5">
        <div className="flex items-center gap-5">
          <div>
            <div className="text-lg font-bold tracking-tight">
              CINEMORA
            </div>
            <div className="text-[10px] uppercase tracking-[0.25em] text-zinc-600">
              Edit Studio
            </div>
          </div>

          <div className="h-7 w-px bg-zinc-800" />

          <div>
            <div className="text-sm font-medium">
              Bertha and the Buried Highway
            </div>
            <div className="text-xs text-zinc-500">
              Seattle SR 99 Tunnel
            </div>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <button className="rounded-lg border border-zinc-800 px-3 py-2 text-xs text-zinc-400 hover:bg-zinc-900">
            Undo
          </button>

          <button className="rounded-lg border border-zinc-800 px-3 py-2 text-xs text-zinc-400 hover:bg-zinc-900">
            Redo
          </button>

          <button className="rounded-lg border border-zinc-700 bg-zinc-900 px-4 py-2 text-xs font-medium hover:bg-zinc-800">
            Save
          </button>

          <button className="rounded-lg bg-white px-5 py-2 text-xs font-semibold text-black hover:bg-zinc-200">
            Export
          </button>
        </div>
      </header>

      {/* MAIN EDITOR */}
      <div className="grid h-[calc(100vh-64px)] grid-cols-[240px_minmax(500px,1fr)_300px]">
        {/* MEDIA PANEL */}
        <aside className="overflow-hidden border-r border-zinc-800 bg-[#0b0b0d]">
          <div className="border-b border-zinc-800 p-4">
            <div className="mb-3 text-xs font-semibold uppercase tracking-widest text-zinc-500">
              Project
            </div>

            <div className="grid grid-cols-2 gap-1 rounded-lg bg-zinc-900 p-1">
              {["Media", "Scenes"].map((item) => (
                <button
                  key={item}
                  className={`rounded-md px-3 py-2 text-xs ${
                    item === "Media"
                      ? "bg-zinc-800 text-white"
                      : "text-zinc-500"
                  }`}
                >
                  {item}
                </button>
              ))}
            </div>
          </div>

          <div className="p-4">
            <div className="mb-3 flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-widest text-zinc-500">
                Assets
              </span>

              <button className="rounded-md border border-zinc-800 px-2 py-1 text-[10px] text-zinc-400 hover:bg-zinc-900">
                + Add
              </button>
            </div>

            <div className="space-y-2">
              {assets.map((asset) => (
                <button
                  key={asset.name}
                  className="group flex w-full items-center gap-3 rounded-lg border border-zinc-900 bg-zinc-950 p-2 text-left hover:border-zinc-700 hover:bg-zinc-900"
                >
                  <div className="flex h-10 w-14 items-center justify-center rounded bg-zinc-900 text-[9px] text-zinc-600">
                    {asset.type}
                  </div>

                  <div className="min-w-0 flex-1">
                    <div className="truncate text-xs text-zinc-300">
                      {asset.name}
                    </div>
                    <div className="mt-1 text-[10px] text-zinc-600">
                      {asset.duration}
                    </div>
                  </div>
                </button>
              ))}
            </div>
          </div>

          <div className="border-t border-zinc-800 p-4">
            <div className="mb-3 text-xs font-semibold uppercase tracking-widest text-zinc-500">
              AI Tools
            </div>

            <div className="space-y-2">
              <button className="w-full rounded-lg border border-zinc-800 bg-zinc-900 p-3 text-left text-xs hover:border-zinc-600">
                ✨ Improve Scene
              </button>

              <button className="w-full rounded-lg border border-zinc-800 bg-zinc-900 p-3 text-left text-xs hover:border-zinc-600">
                🔎 Find Better Footage
              </button>

              <button className="w-full rounded-lg border border-zinc-800 bg-zinc-900 p-3 text-left text-xs hover:border-zinc-600">
                🎬 Make Cinematic
              </button>
            </div>
          </div>
        </aside>

        {/* CENTER */}
        <section className="flex min-w-0 flex-col bg-[#080809]">
          {/* PREVIEW */}
          <div className="flex min-h-0 flex-1 items-center justify-center p-8">
            <div className="relative aspect-video w-full max-w-4xl overflow-hidden rounded-lg border border-zinc-800 bg-black shadow-2xl">
              <div className="absolute inset-0 flex items-center justify-center bg-gradient-to-br from-zinc-900 via-zinc-950 to-black">
                <div className="text-center">
                  <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-full border border-zinc-700 bg-zinc-900 text-2xl">
                    ▶
                  </div>

                  <div className="text-sm font-medium">
                    {selectedClip?.name || "No clip selected"}
                  </div>

                  <div className="mt-1 text-xs text-zinc-600">
                    Preview monitor
                  </div>
                </div>
              </div>

              <div className="absolute left-4 top-4 rounded bg-black/70 px-2 py-1 text-[10px] text-zinc-400">
                1080p • 30 FPS
              </div>

              <div className="absolute bottom-4 right-4 rounded bg-black/70 px-2 py-1 font-mono text-[10px] text-zinc-400">
                00:00:16:12
              </div>
            </div>
          </div>

          {/* PLAYER */}
          <div className="border-t border-zinc-800 bg-[#0b0b0d] px-5 py-3">
            <div className="mb-2 h-1 rounded-full bg-zinc-800">
              <div className="h-full w-[42%] rounded-full bg-white" />
            </div>

            <div className="flex items-center justify-center gap-4">
              <button className="text-zinc-500 hover:text-white">
                ⏮
              </button>

              <button
                onClick={() => setPlaying(!playing)}
                className="flex h-9 w-9 items-center justify-center rounded-full bg-white text-black"
              >
                {playing ? "Ⅱ" : "▶"}
              </button>

              <button className="text-zinc-500 hover:text-white">
                ⏭
              </button>

              <span className="ml-4 font-mono text-xs text-zinc-500">
                00:16.12 / 00:30.00
              </span>
            </div>
          </div>

          {/* TIMELINE */}
          <div className="h-[310px] border-t border-zinc-800 bg-[#09090b]">
            <div className="flex h-10 items-center justify-between border-b border-zinc-800 px-4">
              <div className="flex items-center gap-2">
                <span className="text-xs font-semibold uppercase tracking-widest text-zinc-500">
                  Timeline
                </span>

                <span className="rounded bg-zinc-900 px-2 py-1 text-[10px] text-zinc-600">
                  00:30
                </span>
              </div>

              <div className="flex gap-2">
                <button
                  onClick={splitClip}
                  className="rounded border border-zinc-800 px-2 py-1 text-[10px] text-zinc-400 hover:bg-zinc-900"
                >
                  Split
                </button>

                <button
                  onClick={deleteClip}
                  className="rounded border border-zinc-800 px-2 py-1 text-[10px] text-zinc-400 hover:bg-zinc-900"
                >
                  Delete
                </button>
              </div>
            </div>

            <div className="flex h-[260px] overflow-hidden">
              {/* TRACK LABELS */}
              <div className="w-24 shrink-0 border-r border-zinc-800 bg-[#0b0b0d]">
                {tracks.map((track) => (
                  <div
                    key={track}
                    className="flex h-9 items-center border-b border-zinc-900 px-3 text-[9px] font-semibold tracking-widest text-zinc-600"
                  >
                    {track}
                  </div>
                ))}
              </div>

              {/* TRACK CONTENT */}
              <div className="relative flex-1 overflow-x-auto">
                <div className="absolute left-0 right-0 top-0 min-w-[900px]">
                  <div className="flex h-6 border-b border-zinc-900">
                    {Array.from({ length: 7 }).map((_, index) => (
                      <div
                        key={index}
                        className="w-[130px] shrink-0 border-r border-zinc-900 px-2 text-[9px] text-zinc-700"
                      >
                        {index * 5}s
                      </div>
                    ))}
                  </div>

                  {tracks.map((track) => (
                    <div
                      key={track}
                      className="relative h-9 min-w-[900px] border-b border-zinc-900"
                    >
                      {clips
                        .filter((clip) => clip.track === track)
                        .map((clip) => {
                          const left = clip.start * 6;
                          const width = Math.max(
                            clip.duration * 6,
                            45
                          );

                          const selected =
                            clip.id === selectedId;

                          return (
                            <button
                              key={clip.id}
                              onClick={() =>
                                setSelectedId(clip.id)
                              }
                              style={{
                                left: `${left}px`,
                                width: `${width}px`,
                              }}
                              className={`absolute top-1 h-7 overflow-hidden rounded border px-2 text-left text-[9px] transition ${
                                selected
                                  ? "border-white bg-zinc-600 text-white"
                                  : "border-zinc-700 bg-zinc-800 text-zinc-400 hover:bg-zinc-700"
                              }`}
                            >
                              <span className="block truncate">
                                {clip.name}
                              </span>
                            </button>
                          );
                        })}
                    </div>
                  ))}
                </div>

                {/* PLAYHEAD */}
                <div className="absolute bottom-0 left-[42%] top-0 w-px bg-red-500">
                  <div className="absolute -left-1 top-0 h-2 w-2 rounded-full bg-red-500" />
                </div>
              </div>
            </div>
          </div>
        </section>

        {/* INSPECTOR */}
        <aside className="overflow-y-auto border-l border-zinc-800 bg-[#0b0b0d]">
          <div className="border-b border-zinc-800 p-4">
            <div className="text-xs font-semibold uppercase tracking-widest text-zinc-500">
              Inspector
            </div>

            <div className="mt-2 truncate text-sm font-medium">
              {selectedClip?.name || "Nothing selected"}
            </div>
          </div>

          <div className="flex border-b border-zinc-800">
            {[
              ["effects", "Effects"],
              ["color", "Color"],
              ["motion", "Motion"],
              ["audio", "Audio"],
            ].map(([id, label]) => (
              <button
                key={id}
                onClick={() => setActivePanel(id)}
                className={`flex-1 px-2 py-3 text-[10px] ${
                  activePanel === id
                    ? "border-b border-white text-white"
                    : "text-zinc-600"
                }`}
              >
                {label}
              </button>
            ))}
          </div>

          {activePanel === "effects" && (
            <InspectorSection title="Effects">
              <Slider
                label="Blur"
                value={effects.blur}
                min={0}
                max={100}
                onChange={(value) =>
                  updateEffect("blur", value)
                }
              />

              <Slider
                label="Opacity"
                value={effects.opacity}
                min={0}
                max={100}
                suffix="%"
                onChange={(value) =>
                  updateEffect("opacity", value)
                }
              />

              <div className="mt-5">
                <div className="mb-2 text-[10px] uppercase tracking-widest text-zinc-600">
                  Blur Type
                </div>

                <div className="grid grid-cols-2 gap-2">
                  {[
                    "Gaussian",
                    "Face",
                    "Background",
                    "Selective",
                  ].map((item) => (
                    <button
                      key={item}
                      className="rounded border border-zinc-800 bg-zinc-900 px-2 py-2 text-[10px] text-zinc-400 hover:border-zinc-600"
                    >
                      {item}
                    </button>
                  ))}
                </div>
              </div>

              <div className="mt-5">
                <div className="mb-2 text-[10px] uppercase tracking-widest text-zinc-600">
                  Mask
                </div>

                <div className="grid grid-cols-3 gap-2">
                  {["Rectangle", "Circle", "Freeform"].map(
                    (item) => (
                      <button
                        key={item}
                        className="rounded border border-zinc-800 bg-zinc-900 px-2 py-2 text-[9px] text-zinc-400"
                      >
                        {item}
                      </button>
                    )
                  )}
                </div>
              </div>
            </InspectorSection>
          )}

          {activePanel === "color" && (
            <InspectorSection title="Color">
              <Slider
                label="Exposure"
                value={effects.exposure}
                min={-100}
                max={100}
                onChange={(value) =>
                  updateEffect("exposure", value)
                }
              />

              <Slider
                label="Contrast"
                value={effects.contrast}
                min={-100}
                max={100}
                onChange={(value) =>
                  updateEffect("contrast", value)
                }
              />

              <Slider
                label="Saturation"
                value={effects.saturation}
                min={-100}
                max={100}
                onChange={(value) =>
                  updateEffect("saturation", value)
                }
              />

              <Slider
                label="Temperature"
                value={effects.temperature}
                min={-100}
                max={100}
                onChange={(value) =>
                  updateEffect("temperature", value)
                }
              />

              <div className="mt-5">
                <div className="mb-2 text-[10px] uppercase tracking-widest text-zinc-600">
                  Documentary Preset
                </div>

                <select className="w-full rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-xs text-zinc-300">
                  <option>Clean Documentary</option>
                  <option>Dark Investigation</option>
                  <option>Cold Investigation</option>
                  <option>Warm History</option>
                  <option>Archival</option>
                  <option>Noir</option>
                  <option>News</option>
                </select>
              </div>
            </InspectorSection>
          )}

          {activePanel === "motion" && (
            <InspectorSection title="Motion">
              <Slider
                label="Scale"
                value={effects.scale}
                min={50}
                max={200}
                suffix="%"
                onChange={(value) =>
                  updateEffect("scale", value)
                }
              />

              <Slider
                label="Position X"
                value={effects.x}
                min={-100}
                max={100}
                onChange={(value) =>
                  updateEffect("x", value)
                }
              />

              <Slider
                label="Position Y"
                value={effects.y}
                min={-100}
                max={100}
                onChange={(value) =>
                  updateEffect("y", value)
                }
              />

              <Slider
                label="Rotation"
                value={effects.rotation}
                min={-180}
                max={180}
                suffix="°"
                onChange={(value) =>
                  updateEffect("rotation", value)
                }
              />

              <Slider
                label="Speed"
                value={effects.speed}
                min={25}
                max={400}
                suffix="%"
                onChange={(value) =>
                  updateEffect("speed", value)
                }
              />

              <div className="mt-5 grid grid-cols-2 gap-2">
                {[
                  "Slow Push",
                  "Slow Pull",
                  "Ken Burns",
                  "Punch In",
                ].map((item) => (
                  <button
                    key={item}
                    className="rounded border border-zinc-800 bg-zinc-900 px-2 py-2 text-[10px] text-zinc-400 hover:border-zinc-600"
                  >
                    {item}
                  </button>
                ))}
              </div>
            </InspectorSection>
          )}

          {activePanel === "audio" && (
            <InspectorSection title="Audio">
              <Slider
                label="Volume"
                value={100}
                min={0}
                max={200}
                suffix="%"
                onChange={() => {}}
              />

              <Slider
                label="Fade In"
                value={0}
                min={0}
                max={10}
                suffix="s"
                onChange={() => {}}
              />

              <Slider
                label="Fade Out"
                value={0}
                min={0}
                max={10}
                suffix="s"
                onChange={() => {}}
              />

              <button className="mt-4 w-full rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-3 text-xs text-zinc-400">
                AI Clean Audio
              </button>
            </InspectorSection>
          )}
        </aside>
      </div>

      {/* AI DIRECTOR */}
      <div className="fixed bottom-5 left-1/2 z-20 flex -translate-x-1/2 items-center gap-2 rounded-xl border border-zinc-700 bg-[#101012]/95 p-2 shadow-2xl backdrop-blur">
        <div className="px-3">
          <div className="text-[9px] font-semibold uppercase tracking-widest text-zinc-500">
            AI Director
          </div>
          <div className="text-xs text-white">
            Scene intelligence
          </div>
        </div>

        <button className="rounded-lg border border-zinc-700 px-4 py-2 text-[10px] text-zinc-300 hover:bg-zinc-800">
          Analyze Scene
        </button>

        <button className="rounded-lg border border-zinc-700 px-4 py-2 text-[10px] text-zinc-300 hover:bg-zinc-800">
          Fix Pacing
        </button>

        <button className="rounded-lg border border-zinc-700 px-4 py-2 text-[10px] text-zinc-300 hover:bg-zinc-800">
          Find Better Footage
        </button>

        <button className="rounded-lg bg-white px-4 py-2 text-[10px] font-semibold text-black hover:bg-zinc-200">
          ✨ Improve Scene
        </button>
      </div>
    </main>
  );
}

function InspectorSection({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="border-b border-zinc-800 p-4">
      <div className="mb-5 text-xs font-semibold text-zinc-300">
        {title}
      </div>

      {children}
    </div>
  );
}

function Slider({
  label,
  value,
  min,
  max,
  suffix = "",
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  suffix?: string;
  onChange: (value: number) => void;
}) {
  return (
    <div className="mb-4">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-[10px] text-zinc-500">
          {label}
        </span>

        <span className="font-mono text-[10px] text-zinc-400">
          {value}
          {suffix}
        </span>
      </div>

      <input
        type="range"
        min={min}
        max={max}
        value={value}
        onChange={(event) =>
          onChange(Number(event.target.value))
        }
        className="w-full accent-white"
      />
    </div>
  );
}