import http from 'node:http';
import { readFile, writeFile, mkdir, readdir, access } from 'node:fs/promises';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { crowdPrompt, validateCrowdObservation } from './observation.mjs';
import { webcamPrompt, validateWebcam } from './webcam.mjs';

const run = promisify(execFile);
const MAX_BYTES = 40 * 1024 * 1024;
const MODEL = 'muse-spark-1.3';
const ID = '[a-f0-9-]{36}';
const ORIGINS = new Set(['http://127.0.0.1:5173', 'http://localhost:5173',
  'http://127.0.0.1:4173', 'http://localhost:4173', 'http://127.0.0.1:8767', 'http://localhost:8767']);

export async function normalizeClip(body, start, dir) {
  const input = path.join(dir, 'input.bin'), clip = path.join(dir, 'clip.mp4');
  await writeFile(input, body);
  try {
    await run('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-nostdin',
      '-protocol_whitelist', 'file,pipe', '-format_whitelist', 'mov,matroska,webm,avi',
      '-ss', String(start), '-i', input, '-t', '8', '-map', '0:v:0', '-an', '-sn',
      '-map_metadata', '-1', '-map_chapters', '-1', '-vf', 'scale=640:-2,fps=15',
      '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', clip], {timeout: 60_000});
    const probe = await run('ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of', 'json', clip], {timeout: 10_000});
    const duration = Number(JSON.parse(probe.stdout).format?.duration);
    if (!Number.isFinite(duration) || duration < 2 || duration > 8.1) throw new Error('duration');
    return {duration, bytes: await readFile(clip)};
  } catch {
    throw new Error('Could not prepare this video. Use MP4, MOV, WebM or AVI with at least 2 seconds remaining, and check ffmpeg/ffprobe installation.');
  }
}

// Dependencies are injectable for offline tests. There are deliberately no Mixxx,
// queue, planner, or experimental-workspace imports in this service.
export function createCrowdServer({storageRoot, key, fetchImpl = fetch, normalize = normalizeClip}) {
  const jobs = new Map();
  let activeJobId = null;
  async function storedJob(id) {
    if (jobs.has(id)) return jobs.get(id);
    try {
      const job = JSON.parse(await readFile(path.join(storageRoot, id, 'result.json'), 'utf8'));
      if (job.id !== id || !['completed', 'failed'].includes(job.status)) return null;
      job.rawResponseAvailable = await access(path.join(storageRoot, id, 'muse-response.json')).then(() => true, () => false);
      if (!jobs.has(id)) jobs.set(id, job);
      return jobs.get(id);
    } catch { return null; }
  }
  let historyLoaded;
  function restoreHistory() {
    return historyLoaded ??= readdir(storageRoot, {withFileTypes: true}).then(entries =>
      Promise.all(entries.filter(e => e.isDirectory() && new RegExp(`^${ID}$`).test(e.name)).map(e => storedJob(e.name)))
    ).catch(error => { if (error.code !== 'ENOENT') { historyLoaded = null; throw error; } });
  }
  async function api(route, options = {}) {
    let response;
    try {
      response = await fetchImpl(`https://api.meta.ai/v1${route}`, {...options,
        headers: {...options.headers, Authorization: `Bearer ${key}`},
        signal: AbortSignal.timeout(180_000)});
    } catch {
      throw new Error('Meta could not be reached or timed out. No automatic retry was made.');
    }
    if (!response.ok) throw new Error(`Meta request failed (HTTP ${response.status}). No automatic retry was made.`);
    return response;
  }
  async function analyze(job, body) {
    const dir = path.join(storageRoot, job.id);
    let fileId, terminal = 'failed';
    try {
      await mkdir(dir, {recursive: true});
      job.stage = 'Preparing up to 8 seconds of silent video';
      const {duration, bytes} = await normalize(body, job.sourceStartSeconds, dir);
      job.durationSeconds = duration;
      job.clipAvailable = true;
      job.stage = 'Uploading the prepared clip to Meta';
      const form = new FormData();
      form.set('purpose', 'user_data');
      form.set('file', new Blob([bytes], {type: 'video/mp4'}), 'crowd-clip.mp4');
      const uploaded = await (await api('/files', {method: 'POST', body: form})).json();
      if (typeof uploaded.id !== 'string' || !/^file-[\w-]+$/.test(uploaded.id)) throw new Error('Meta returned an invalid file identifier.');
      fileId = uploaded.id;
      // Retain cleanup information privately even if this process is interrupted.
      await writeFile(path.join(dir, 'remote-file.json'), JSON.stringify({id: fileId, cleanup: 'pending'}));
      const prompt = job.mode === 'webcam' ? webcamPrompt(duration) : crowdPrompt(duration);
      await writeFile(path.join(dir, 'muse-prompt.txt'), prompt);
      job.stage = 'Muse Spark is observing the clip';
      job.modelRequestedAt = new Date().toISOString();
      const started = performance.now();
      const response = await (await api('/responses', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({model: MODEL, input: [{type: 'message', role: 'user', content: [
          {type: 'input_text', text: prompt}, {type: 'input_file', file_id: fileId}
        ]}]})})).json();
      job.modelSeconds = (performance.now() - started) / 1000;
      job.modelRespondedAt = new Date().toISOString();
      await writeFile(path.join(dir, 'muse-response.json'), JSON.stringify(response, null, 2));
      job.rawResponseAvailable = true;
      if (response.status !== 'completed') throw new Error('Meta did not complete the observation. Raw response retained locally.');
      const output = response.output?.flatMap(item => item.content || [])
        .filter(item => item.type === 'output_text').map(item => item.text).join('') || '';
      try {
        const parsed = JSON.parse(output.replace(/^```(?:json)?\s*|\s*```$/g, '').trim());
        if (job.mode === 'webcam') {
          const accepted = validateWebcam(parsed, duration);
          job.observation = accepted.observation; job.cue = accepted.cue;
        } else job.observation = validateCrowdObservation(parsed, duration);
      } catch {
        throw new Error('The model output failed the observation or timestamp checks. Raw response retained locally; no observation accepted.');
      }
      terminal = 'completed';
    } catch (error) {
      job.error = error instanceof Error && (error.message.startsWith('Meta ') || error.message.startsWith('Could not prepare') || error.message.startsWith('The model output'))
        ? error.message : 'Observation failed. Check the local service and available disk space.';
    } finally {
      job.stage = 'Removing the uploaded file from Meta';
      job.remoteFileCleanup = 'not_uploaded';
      if (fileId) {
        try { await api(`/files/${fileId}`, {method: 'DELETE'}); job.remoteFileCleanup = 'deleted'; }
        catch { job.remoteFileCleanup = 'failed'; }
        await writeFile(path.join(dir, 'remote-file.json'), JSON.stringify({id: fileId, cleanup: job.remoteFileCleanup})).catch(() => {});
      }
      job.finishedAt = new Date().toISOString();
      job.status = terminal;
      job.stage = terminal === 'completed' ? 'Observation ready for review' : 'Observation failed';
      await writeFile(path.join(dir, 'result.json'), JSON.stringify(job, null, 2)).catch(() => {job.storageWarning = 'The result could not be saved locally. Download it before closing.';});
      activeJobId = null;
    }
  }
  const server = http.createServer(async (req, res) => {
    const send = (status, data) => {
      res.writeHead(status, {'Content-Type': 'application/json', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'});
      res.end(JSON.stringify(data));
    };
    const host = req.headers.host?.split(':')[0];
    if (!['127.0.0.1', 'localhost'].includes(host) || (req.headers.origin && !ORIGINS.has(req.headers.origin))) return send(403, {error: 'Local dashboard origin required.'});
    try {
      const url = new URL(req.url, 'http://127.0.0.1');
      if (req.method === 'GET' && url.pathname === '/api/crowd/status') {
        return send(200, {keyConfigured: Boolean(key), model: MODEL, activeJobId, mode: 'uploaded_clip', observationOnly: true});
      }
      if (req.method === 'GET' && url.pathname === '/api/crowd/jobs') {
        await restoreHistory();
        const mode = url.searchParams.get('mode');
        if (mode && !['webcam', 'uploaded_clip'].includes(mode)) return send(400, {error: 'Unknown capture mode.'});
        return send(200, {jobs: [...jobs.values()].filter(job => !mode || job.mode === mode)
          .sort((a, b) => String(b.captureTime ?? b.requestedAt).localeCompare(String(a.captureTime ?? a.requestedAt)))});
      }
      const match = url.pathname.match(new RegExp(`^/api/crowd/jobs/(${ID})(/clip|/response)?$`));
      if (req.method === 'GET' && match) {
        const job = await storedJob(match[1]);
        if (!job) return send(404, {error: 'Observation is not available.'});
        if (!match[2]) return send(200, job);
        if (match[2] === '/response') {
          if (!job.rawResponseAvailable) return send(404, {error: 'No raw Meta response is available yet.'});
          return send(200, JSON.parse(await readFile(path.join(storageRoot, job.id, 'muse-response.json'), 'utf8')));
        }
        if (!job.clipAvailable) return send(404, {error: 'Prepared clip is not available yet.'});
        const data = await readFile(path.join(storageRoot, job.id, 'clip.mp4'));
        const range = req.headers.range;
        const headers = {'Content-Type': 'video/mp4', 'Accept-Ranges': 'bytes', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'};
        if (range) {
          const parts = /^bytes=(\d+)-(\d*)$/.exec(range);
          const start = parts ? Number(parts[1]) : NaN;
          const end = parts?.[2] ? Math.min(Number(parts[2]), data.length - 1) : data.length - 1;
          if (!Number.isSafeInteger(start) || start < 0 || start > end || end >= data.length) {
            res.writeHead(416, {...headers, 'Content-Range': `bytes */${data.length}`}); return res.end();
          }
          res.writeHead(206, {...headers, 'Content-Length': end - start + 1, 'Content-Range': `bytes ${start}-${end}/${data.length}`});
          return res.end(data.subarray(start, end + 1));
        }
        res.writeHead(200, {...headers, 'Content-Length': data.length}); return res.end(data);
      }
      if (req.method === 'POST' && url.pathname === '/api/crowd/analyze') {
        if (!key) return send(503, {error: 'Set MODEL_API_KEY in the observation service environment or .env, then restart it.'});
        if (activeJobId) return send(409, {error: 'An observation is already running.', activeJobId});
        if (req.headers['content-type'] !== 'application/octet-stream') return send(415, {error: 'Send a video file as application/octet-stream.'});
        const start = Number(url.searchParams.get('start') || 0);
        if (!Number.isFinite(start) || start < 0 || start > 86400) return send(400, {error: 'Start time must be between 0 and 86400 seconds.'});
        if (Number(req.headers['content-length']) > MAX_BYTES) return send(413, {error: 'Video exceeds 40 MB. Trim it before uploading.'});
        const mode = url.searchParams.get('mode') || 'uploaded_clip';
        const session = url.searchParams.get('session');
        const captured = url.searchParams.get('capturedAt');
        const setRunId = url.searchParams.get('setRunId');
        if (setRunId && !/^set-[0-9]{6,}$/.test(setRunId)) return send(400, {error: 'Invalid set run ID.'});
        if (!['uploaded_clip', 'webcam'].includes(mode)) return send(400, {error: 'Unknown capture mode.'});
        if (mode === 'webcam' && (start !== 0 || !/^[a-f0-9-]{36}$/.test(session || '')
          || !captured || !Number.isFinite(Date.parse(captured)) || Date.now() - Date.parse(captured) > 20_000
          || Date.parse(captured) > Date.now() + 2000)) return send(400, {error: 'A fresh webcam capture and session are required.'});
        const id = randomUUID(); activeJobId = id;
        try {
          const chunks = []; let size = 0;
          for await (const chunk of req) {
            size += chunk.length;
            if (size > MAX_BYTES) { activeJobId = null; return send(413, {error: 'Video exceeds 40 MB. Trim it before uploading.'}); }
            chunks.push(chunk);
          }
          if (!size) { activeJobId = null; return send(400, {error: 'Choose a nonempty video file.'}); }
          const job = {id, status: 'running', stage: 'Video received', model: MODEL, profile: mode === 'webcam' ? 'webcam-v1' : 'crowd-v1',
            requestedAt: new Date().toISOString(), sourceStartSeconds: start, mode, setRunId: mode === 'webcam' ? setRunId : null, webcamSession: mode === 'webcam' ? session : null,
            observationOnly: true, humanReview: 'pending', captureTime: mode === 'webcam' ? new Date(captured).toISOString() : null,
            historyComparison: 'unavailable', musicEventComparison: 'unavailable'};
          jobs.set(id, job);
          void analyze(job, Buffer.concat(chunks));
          return send(202, {id});
        } catch {
          activeJobId = null;
          return send(400, {error: 'Video upload was interrupted.'});
        }
      }
      send(404, {error: 'Not found.'});
    } catch {
      send(500, {error: 'The observation service could not complete this request.'});
    }
  });
  server.requestTimeout = 60_000;
  return server;
}
