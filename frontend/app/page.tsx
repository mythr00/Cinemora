"use client";

import { useState } from "react";

const categories = [
  "True Crime",
  "Documentary",
  "History",
  "Naval History",
  "War History",
  "Biography",
  "Finance",
  "Science",
  "Custom",
];

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";

export default function Home() {
  const [script, setScript] = useState("");
  const [category, setCategory] = useState("Documentary");

  const [options, setOptions] = useState({
    deepResearch: true,
    realFootage: true,
    archiveImages: true,
    maps: true,
    graphics: true,
    captions: true,
    music: true,
  });

  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");

  function updateOption(
    option: keyof typeof options,
    value: boolean
  ) {
    setOptions((current) => ({
      ...current,
      [option]: value,
    }));
  }

  async function createProject() {
    if (!script.trim()) {
      setMessage("Paste your script first.");
      return;
    }

    setLoading(true);
    setMessage("Creating project...");

    try {
      // STEP 1: Create project
      const response = await fetch(
        `${API_URL}/projects`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            script,
            category,
            options,
          }),
        }
      );

      if (!response.ok) {
        throw new Error("Failed to create project.");
      }

      const data = await response.json();

      // STEP 2: Start the real production pipeline
      setMessage("Project created. Starting production...");

      const analyzeResponse = await fetch(
        `${API_URL}/projects/${data.id}/analyze`,
        {
          method: "POST",
        }
      );

      if (!analyzeResponse.ok) {
        throw new Error("Failed to start production pipeline.");
      }

      const analyzeData = await analyzeResponse.json();

      setMessage(
        `Production started successfully. Job: ${analyzeData.job_id}`
      );
    } catch (error) {
      console.error(error);

      setMessage(
        "Failed to start documentary production."
      );
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="min-h-screen bg-[#09090b] text-white">
      <div className="mx-auto max-w-6xl px-6 py-10">

        <header className="mb-10 flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold">
              Documentary Studio
            </h1>

            <p className="mt-1 text-sm text-zinc-400">
              Turn a script into a researched documentary.
            </p>
          </div>

          <div className="rounded-full border border-zinc-800 px-4 py-2 text-sm text-zinc-400">
            AI Video Studio
          </div>
        </header>

        <section className="rounded-2xl border border-zinc-800 bg-zinc-950 p-6 shadow-2xl">

          <div className="mb-6">
            <h2 className="text-xl font-semibold">
              Create a new video
            </h2>

            <p className="mt-1 text-sm text-zinc-500">
              Paste your script and let the production engine
              handle research, visuals, narration and editing.
            </p>
          </div>

          <textarea
            value={script}
            onChange={(event) =>
              setScript(event.target.value)
            }
            placeholder="Paste your script here..."
            className="min-h-[360px] w-full resize-y rounded-xl border border-zinc-800 bg-zinc-900 p-5 text-sm leading-7 text-white outline-none transition focus:border-zinc-600"
          />

          <div className="mt-6 grid gap-5 md:grid-cols-3">

            <div>
              <label className="mb-2 block text-sm text-zinc-400">
                Category
              </label>

              <select
                value={category}
                onChange={(event) =>
                  setCategory(event.target.value)
                }
                className="w-full rounded-lg border border-zinc-800 bg-zinc-900 px-4 py-3 text-sm outline-none"
              >
                {categories.map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="mb-2 block text-sm text-zinc-400">
                Voice
              </label>

              <select className="w-full rounded-lg border border-zinc-800 bg-zinc-900 px-4 py-3 text-sm outline-none">
                <option>Deep Documentary</option>
                <option>Investigative</option>
                <option>Calm History</option>
                <option>Serious Male</option>
                <option>Serious Female</option>
              </select>
            </div>

            <div>
              <label className="mb-2 block text-sm text-zinc-400">
                Format
              </label>

              <select className="w-full rounded-lg border border-zinc-800 bg-zinc-900 px-4 py-3 text-sm outline-none">
                <option>16:9 YouTube</option>
                <option>9:16 Shorts</option>
                <option>1:1 Square</option>
              </select>
            </div>

          </div>

          <div className="mt-8">

            <h3 className="mb-4 text-sm font-semibold">
              Production options
            </h3>

            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">

              <Toggle
                label="Deep research"
                checked={options.deepResearch}
                onChange={(value) =>
                  updateOption("deepResearch", value)
                }
              />

              <Toggle
                label="Real footage"
                checked={options.realFootage}
                onChange={(value) =>
                  updateOption("realFootage", value)
                }
              />

              <Toggle
                label="Archive images"
                checked={options.archiveImages}
                onChange={(value) =>
                  updateOption("archiveImages", value)
                }
              />

              <Toggle
                label="Maps"
                checked={options.maps}
                onChange={(value) =>
                  updateOption("maps", value)
                }
              />

              <Toggle
                label="Motion graphics"
                checked={options.graphics}
                onChange={(value) =>
                  updateOption("graphics", value)
                }
              />

              <Toggle
                label="Word-synced captions"
                checked={options.captions}
                onChange={(value) =>
                  updateOption("captions", value)
                }
              />

              <Toggle
                label="Music + SFX"
                checked={options.music}
                onChange={(value) =>
                  updateOption("music", value)
                }
              />

            </div>
          </div>

          <button
            onClick={createProject}
            disabled={loading}
            className="mt-8 w-full rounded-xl bg-white px-6 py-4 font-semibold text-black transition hover:bg-zinc-200 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading
              ? "Starting production..."
              : "Create Video"}
          </button>

          {message && (
            <div className="mt-4 rounded-lg border border-zinc-800 bg-zinc-900 p-4 text-sm text-zinc-300">
              {message}
            </div>
          )}

        </section>
      </div>
    </main>
  );
}

function Toggle({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <label className="flex cursor-pointer items-center gap-3 rounded-lg border border-zinc-800 bg-zinc-900 p-4">

      <input
        type="checkbox"
        checked={checked}
        onChange={(event) =>
          onChange(event.target.checked)
        }
        className="h-4 w-4"
      />

      <span className="text-sm text-zinc-300">
        {label}
      </span>

    </label>
  );
}
