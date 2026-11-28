/**
 * Cinemora web render engine — headless Chromium harness.
 *
 * Fixed production code (Animation Studio Expansion, mission
 * §5). The Python engine module writes a job JSON and invokes:
 *   node harness.mjs <job.json>
 * Job: {scene, duration, fps, width, height, colors, data, outDir}
 * The harness serves this directory over a loopback HTTP server
 * (module resolution for three/gsap/lottie-web), opens the scene
 * page, and captures frames DETERMINISTICALLY: every frame is
 * produced by seek(t) on the scene's own timeline — never by
 * wall-clock playback — so renders are reproducible.
 * Frames land as frame_0001.png ... in outDir; the Python module
 * conforms them to the house format.
 *
 * Scenes:
 *   globe   — Three.js: a rotating globe with REAL coastline
 *             geometry (world-atlas Natural Earth data), markers
 *             at the recorded places, and great-circle arcs that
 *             draw on between consecutive recorded places.
 *   kinetic — GSAP: kinetic typography for a recorded SHORT
 *             quoted passage (word-staggered reveal + rule +
 *             attribution). Short payloads only — the Python
 *             side enforces the same gate as text_highlight.
 *   lottie  — lottie-web rendering a Lottie document the Python
 *             side generated from the recorded route (draw-on
 *             path, endpoint markers, travelling pulse). The
 *             JSON is data, generated from recorded coordinates;
 *             this scene only plays it.
 */
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Serve root = the engine home carrying node_modules. The
// harness file itself is canonical in the repo
// (backend/web_engine/); the dependency install may live in a
// separate engine home (CINEMORA_WEB_ENGINE_HOME). Puppeteer —
// the only Node-side dependency — is required FROM that home;
// the scene libraries load in the browser over HTTP below.
const HOME = process.env.CINEMORA_WEB_ENGINE_HOME
  || path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(path.join(HOME, 'package.json'));
const puppeteer = require('puppeteer');
const job = JSON.parse(await readFile(process.argv[2], 'utf8'));
const { scene, duration, fps, width, height, colors, data, outDir } = job;
const frames = Math.max(1, Math.round(duration * fps));

const MIME = { '.js': 'text/javascript', '.mjs': 'text/javascript',
               '.json': 'application/json', '.html': 'text/html' };
let pageHtml = '';
const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://127.0.0.1');
    if (url.pathname === '/__page') {
      // The generated scene page, served from memory: import
      // maps need a real http(s) base URL (about:blank blocks
      // them), and the page carries this job's data, so it is
      // never written into the shared engine home.
      res.writeHead(200, { 'Content-Type': 'text/html' });
      res.end(pageHtml);
      return;
    }
    const file = path.join(HOME, decodeURIComponent(url.pathname));
    if (!file.startsWith(HOME) || !existsSync(file)) {
      res.writeHead(404); res.end('not found'); return;
    }
    const body = await readFile(file);
    res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' });
    res.end(body);
  } catch (err) {
    res.writeHead(500); res.end(String(err));
  }
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const port = server.address().port;

pageHtml = `<!DOCTYPE html><html><head><meta charset="utf-8">
<style>html,body{margin:0;padding:0;background:${colors.bg};overflow:hidden}
#stage{position:relative;width:${width}px;height:${height}px}
canvas{display:block}</style>
<script type="importmap">{"imports":{
  "three": "/node_modules/three/build/three.module.js"
}}</script>
<script src="/node_modules/gsap/dist/gsap.min.js"></script>
<script src="/node_modules/lottie-web/build/player/lottie.min.js"></script>
<script src="/node_modules/topojson-client/dist/topojson-client.min.js"></script></head>
<body><div id="stage"></div>
<script type="module">
import * as THREE from 'three';
const topojson = window.topojson;
const gsap = window.gsap;
const lottie = window.lottie;
const JOB = ${JSON.stringify({ scene, duration, fps, width, height, colors, data })};
const { scene, duration, fps, width, height, colors, data } = JOB;
const stage = document.getElementById('stage');
const C = (hex) => new THREE.Color(hex);

async function buildGlobe() {
  const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
  renderer.setSize(width, height);
  stage.appendChild(renderer.domElement);
  const scene3 = new THREE.Scene();
  scene3.background = C(colors.bg);
  const camera = new THREE.PerspectiveCamera(38, width / height, 0.1, 100);
  camera.position.set(0, 0.42, 3.1);
  camera.lookAt(0, 0, 0);
  const group = new THREE.Group();
  scene3.add(group);
  const R = 1.0;
  const sphere = new THREE.Mesh(
    new THREE.SphereGeometry(R * 0.995, 48, 32),
    new THREE.MeshBasicMaterial({ color: C(colors.surface) }));
  group.add(sphere);
  // Real coastlines: Natural Earth via world-atlas (bundled
  // dataset, loaded from the engine's own node_modules).
  const topo = await (await fetch('/node_modules/world-atlas/countries-110m.json')).json();
  const geo = topojson.feature(topo, topo.objects.countries);
  const lineMat = new THREE.LineBasicMaterial({ color: C(colors.muted), transparent: true, opacity: 0.85 });
  const toXYZ = (lon, lat, r) => {
    const phi = (90 - lat) * Math.PI / 180;
    const theta = (lon + 180) * Math.PI / 180;
    return new THREE.Vector3(
      -r * Math.sin(phi) * Math.cos(theta), r * Math.cos(phi),
      r * Math.sin(phi) * Math.sin(theta));
  };
  const addRing = (ring) => {
    const pts = ring.map(([lon, lat]) => toXYZ(lon, lat, R));
    const g = new THREE.BufferGeometry().setFromPoints(pts);
    group.add(new THREE.Line(g, lineMat));
  };
  for (const feature of geo.features) {
    const geom = feature.geometry;
    if (!geom) continue;
    const polys = geom.type === 'Polygon' ? [geom.coordinates]
      : geom.type === 'MultiPolygon' ? geom.coordinates : [];
    for (const poly of polys) for (const ring of poly) addRing(ring);
  }
  // Recorded places: markers + great-circle arcs.
  const places = data.places || [];
  const markerMat = new THREE.MeshBasicMaterial({ color: C(colors.accent) });
  const pulses = [];
  for (const p of places) {
    const pos = toXYZ(p.lon, p.lat, R * 1.01);
    const m = new THREE.Mesh(new THREE.SphereGeometry(0.016, 12, 8), markerMat);
    m.position.copy(pos);
    group.add(m);
    const ring = new THREE.Mesh(
      new THREE.RingGeometry(0.02, 0.026, 32),
      new THREE.MeshBasicMaterial({ color: C(colors.accent), transparent: true, side: THREE.DoubleSide }));
    ring.position.copy(pos.clone().multiplyScalar(1.02));
    ring.lookAt(pos.clone().multiplyScalar(2));
    group.add(ring);
    pulses.push(ring);
  }
  const arcs = [];
  const arcMat = new THREE.MeshBasicMaterial({ color: C(colors.accent2) });
  for (let i = 0; i + 1 < places.length; i++) {
    const a = toXYZ(places[i].lon, places[i].lat, R);
    const b = toXYZ(places[i + 1].lon, places[i + 1].lat, R);
    const angle = a.angleTo(b);
    const lift = 1 + 0.22 * Math.sin(Math.min(angle, Math.PI) / 2);
    const pts = [];
    const SEG = 48;
    for (let s = 0; s <= SEG; s++) {
      const t = s / SEG;
      const v = a.clone().multiplyScalar(Math.sin((1 - t) * angle))
        .add(b.clone().multiplyScalar(Math.sin(t * angle)))
        .divideScalar(Math.sin(angle) || 1);
      v.multiplyScalar(R * (1 + (lift - 1) * Math.sin(t * Math.PI)) * 1.005);
      pts.push(v);
    }
    const curve = new THREE.CatmullRomCurve3(pts);
    const tube = new THREE.TubeGeometry(curve, SEG, 0.0055, 6, false);
    const mesh = new THREE.Mesh(tube, arcMat);
    group.add(mesh);
    arcs.push({ mesh, total: tube.index ? tube.index.count : 0 });
  }
  const meanLon = places.length
    ? places.reduce((s, p) => s + p.lon, 0) / places.length : 0;
  const baseRot = -((meanLon + 180) * Math.PI / 180) + Math.PI / 2;
  return (t) => {
    const prog = t / duration;
    group.rotation.y = baseRot + (prog - 0.5) * 0.22;
    group.rotation.x = 0.12 * Math.sin(prog * Math.PI);
    for (const { mesh, total } of arcs) {
      const frac = Math.min(1, Math.max(0, (prog - 0.15) / 0.6));
      mesh.geometry.setDrawRange(0, Math.floor(total * frac));
    }
    pulses.forEach((ring, i) => {
      const s = 1 + 0.55 * (0.5 + 0.5 * Math.sin(t * 2.4 + i * 1.7));
      ring.scale.setScalar(s);
      ring.material.opacity = 0.9 - 0.4 * (s - 1);
    });
    renderer.render(scene3, camera);
  };
}

function buildKinetic() {
  stage.style.background = colors.bg;
  const wrap = document.createElement('div');
  wrap.style.cssText = 'position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;font-family:Georgia,serif;padding:0 90px;box-sizing:border-box';
  const words = String(data.text || '').split(/\\s+/).filter(Boolean);
  const line = document.createElement('div');
  line.style.cssText = 'color:' + colors.text + ';font-size:64px;line-height:1.28;text-align:center;font-weight:600';
  words.forEach((w, i) => {
    const span = document.createElement('span');
    span.textContent = w;
    span.style.display = 'inline-block';
    span.style.opacity = '0';
    line.appendChild(span);
    if (i + 1 < words.length) line.appendChild(document.createTextNode(' '));
  });
  const rule = document.createElement('div');
  rule.style.cssText = 'width:340px;height:5px;background:' + colors.accent + ';margin-top:44px;transform:scaleX(0);transform-origin:center';
  wrap.appendChild(line);
  wrap.appendChild(rule);
  if (data.attribution) {
    const attr = document.createElement('div');
    attr.textContent = data.attribution;
    attr.style.cssText = 'color:' + colors.muted + ';font-size:30px;margin-top:30px;letter-spacing:2px;text-transform:uppercase;opacity:0;font-family:Helvetica,Arial,sans-serif';
    attr.id = 'attr';
    wrap.appendChild(attr);
  }
  stage.appendChild(wrap);
  const tl = gsap.timeline({ paused: true });
  tl.to(line.querySelectorAll('span'),
        { opacity: 1, y: 0, duration: 0.5, stagger: 0.09, ease: 'power3.out',
          startAt: { y: 26 } }, 0.25);
  tl.to(rule, { scaleX: 1, duration: 0.7, ease: 'power2.inOut' },
        Math.min(duration * 0.55, 0.25 + words.length * 0.09 + 0.4));
  const attr = document.getElementById('attr');
  if (attr) tl.to(attr, { opacity: 1, duration: 0.6 }, duration * 0.72);
  return (t) => { tl.time(Math.min(t, duration - 0.001)); };
}

async function buildLottie() {
  const holder = document.createElement('div');
  holder.style.cssText = 'width:' + width + 'px;height:' + height + 'px;background:' + colors.bg;
  stage.appendChild(holder);
  const anim = lottie.loadAnimation({
    container: holder, renderer: 'svg', loop: false, autoplay: false,
    animationData: data.lottie,
  });
  // Seeking before the player has built its DOM silently
  // renders nothing — wait for DOMLoaded (with a bounded
  // fallback so a malformed document fails the render loudly
  // downstream instead of hanging the harness).
  await new Promise((resolve) => {
    let done = false;
    const finish = () => { if (!done) { done = true; resolve(); } };
    anim.addEventListener('DOMLoaded', finish);
    setTimeout(finish, 8000);
  });
  const total = anim.totalFrames || Math.round(duration * fps);
  return (t) => {
    // NB: the clamp must use the animation's own frame count —
    // a bare frames identifier here would resolve to
    // window.frames and silently freeze the animation at
    // frame 0 (the bug this comment replaces).
    const frame = Math.min(total - 1, Math.floor(t * fps));
    anim.goToAndStop(frame, true);
  };
}

const builders = { globe: buildGlobe, kinetic: buildKinetic, lottie: buildLottie };
window.__seek = await builders[scene]();
window.__ready = true;
</script></body></html>`;

const browser = await puppeteer.launch({
  headless: true,
  args: ['--no-sandbox', '--use-angle=swiftshader',
         '--enable-unsafe-swiftshader', '--disable-dev-shm-usage'],
});
try {
  const page = await browser.newPage();
  await page.setViewport({ width, height });
  page.on('pageerror', (err) => console.error('PAGEERROR', String(err)));
  await page.goto(`http://127.0.0.1:${port}/__page`,
                  { waitUntil: 'networkidle0', timeout: 60000 });
  await page.waitForFunction('window.__ready === true', { timeout: 60000 });
  for (let i = 0; i < frames; i++) {
    const t = i / fps;
    await page.evaluate((tt) => window.__seek(tt), t);
    const name = `frame_${String(i + 1).padStart(4, '0')}.png`;
    await page.screenshot({ path: `${outDir}/${name}` });
  }
  console.log(JSON.stringify({ ok: true, frames }));
} finally {
  await browser.close();
  server.close();
}
