import test from 'node:test';
import assert from 'node:assert/strict';
import { Readable } from 'node:stream';
import { mkdtemp, readFile, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createCrowdServer, normalizeClip } from './service.mjs';

const empty = {schema_version: 'crowd-v1', quality: {visibility: 'insufficient', camera_motion: 'stable', limitations: ['Synthetic empty image.']},
  excluded_subjects: [], regions: [], evidence: [], summary_en: 'No audience in synthetic fixture.'};
function dispatch(server, url, {method = 'GET', body = '', headers = {}} = {}) {
  return new Promise(resolve => {
    const req = Readable.from(body ? [Buffer.from(body)] : []);
    Object.assign(req, {url, method, headers: {host: '127.0.0.1:8767', ...headers}});
    let status, responseHeaders;
    const res = {writeHead(code, values) {status = code; responseHeaders = values;}, end(value = '') {
      const bytes = Buffer.from(value);
      resolve({status, headers: responseHeaders, bytes, json: () => JSON.parse(bytes.toString())});
    }};
    server.emit('request', req, res);
  });
}
const upload = server => dispatch(server, '/api/crowd/analyze?start=2', {method: 'POST', body: 'synthetic', headers: {'content-type': 'application/octet-stream'}});
async function until(fn) {
  const deadline = Date.now() + 3000;
  while (Date.now() < deadline) { const value = await fn(); if (value) return value; await new Promise(resolve => setTimeout(resolve, 10)); }
  throw new Error('Test deadline exceeded');
}
async function setup(t, {observation = empty, failDelete = false, key = 'synthetic-secret', gate, expectedStart = 2} = {}) {
  const root = await mkdtemp(path.join(tmpdir(), 'crowd-service-'));
  t.after(() => rm(root, {recursive: true, force: true}));
  const calls = [];
  const server = createCrowdServer({storageRoot: root, key,
    normalize: async (body, start, dir) => {
      assert.equal(start, expectedStart); assert.equal(body.toString(), 'synthetic');
      const bytes = Buffer.from('synthetic-clip'); await writeFile(path.join(dir, 'clip.mp4'), bytes);
      return {duration: 3, bytes};
    },
    fetchImpl: async (url, options) => {
      calls.push({url, options});
      assert.equal(options.headers.Authorization, 'Bearer synthetic-secret');
      if (url.endsWith('/files')) return Response.json({id: 'file-synthetic'});
      if (url.endsWith('/responses')) {
        if (gate) await gate;
        return Response.json({status: 'completed', output: [{content: [{type: 'output_text', text: JSON.stringify(observation)}]}]});
      }
      if (options.method === 'DELETE') return new Response('', {status: failDelete ? 500 : 200});
      throw new Error('Unexpected network route');
    }});
  return {root, server, calls};
}

test('manual upload calls only Files and Muse, retains evidence, serves clip ranges, and cleans up', async t => {
  const {server, calls, root} = await setup(t);
  const response = await upload(server); assert.equal(response.status, 202);
  const id = response.json().id;
  const job = await until(async () => {const j = (await dispatch(server, `/api/crowd/jobs/${id}`)).json(); return j.status !== 'running' && j;});
  assert.equal(job.status, 'completed'); assert.equal(job.remoteFileCleanup, 'deleted');
  assert.equal(job.observationOnly, true); assert.equal(job.captureTime, null);
  assert.equal(job.musicEventComparison, 'unavailable'); assert.equal(job.historyComparison, 'unavailable');
  assert.ok(!JSON.stringify(job).includes('synthetic-secret'));
  assert.equal(calls.length, 3);
  const request = JSON.parse(calls[1].options.body);
  assert.equal(request.model, 'muse-spark-1.3');
  assert.equal(request.input[0].content[1].file_id, 'file-synthetic');
  assert.equal((await dispatch(server, '/api/crowd/status')).json().activeJobId, null);
  assert.equal(JSON.parse(await readFile(path.join(root, id, 'result.json'))).status, 'completed');
  assert.ok((await readFile(path.join(root, id, 'muse-prompt.txt'), 'utf8')).includes('No previous clips'));
  const range = await dispatch(server, `/api/crowd/jobs/${id}/clip`, {headers: {range: 'bytes=0-3'}});
  assert.equal(range.status, 206); assert.equal(range.bytes.toString(), 'synt');
  assert.equal((await dispatch(server, `/api/crowd/jobs/${id}/clip`, {headers: {range: 'bytes=999-'}})).status, 416);
  assert.equal((await dispatch(server, '/api/arm', {method: 'POST'})).status, 404);
  assert.equal((await dispatch(server, '/api/queue/reorder', {method: 'POST'})).status, 404);
});

test('schema rejection retains raw response and still deletes upload', async t => {
  const {server, root, calls} = await setup(t, {observation: {...empty, queue_action: 'skip'}});
  const id = (await upload(server)).json().id;
  const job = await until(async () => {const j = (await dispatch(server, `/api/crowd/jobs/${id}`)).json(); return j.status !== 'running' && j;});
  assert.equal(job.status, 'failed'); assert.equal(job.remoteFileCleanup, 'deleted');
  assert.equal(job.observation, undefined); assert.match(job.error, /failed the observation/);
  assert.ok((await readFile(path.join(root, id, 'muse-response.json'), 'utf8')).includes('queue_action'));
  assert.equal(calls.filter(c => c.url.endsWith('/responses')).length, 1);
});

test('concurrent submissions are rejected and cleanup failure remains visible without a model retry', async t => {
  let release;
  const gate = new Promise(resolve => {release = resolve;});
  const {server, calls, root} = await setup(t, {gate, failDelete: true});
  const id = (await upload(server)).json().id;
  assert.equal((await upload(server)).status, 409);
  assert.equal((await dispatch(server, '/api/crowd/status')).json().activeJobId, id);
  release();
  const job = await until(async () => {const j = (await dispatch(server, `/api/crowd/jobs/${id}`)).json(); return j.status !== 'running' && j;});
  assert.equal(job.status, 'completed'); assert.equal(job.remoteFileCleanup, 'failed');
  assert.equal(JSON.parse(await readFile(path.join(root, id, 'remote-file.json'))).cleanup, 'failed');
  assert.equal(calls.length, 3);
});

test('rejects invalid uploads, external origins and missing configuration before model access', async t => {
  const {server, calls} = await setup(t);
  assert.equal((await dispatch(server, '/api/crowd/status', {headers: {origin: 'https://untrusted.example'}})).status, 403);
  assert.equal((await dispatch(server, '/api/crowd/status', {headers: {host: 'untrusted.example'}})).status, 403);
  assert.equal((await dispatch(server, '/api/crowd/analyze', {method: 'POST'})).status, 415);
  for (const start of ['NaN', '-1', '90000']) {
    assert.equal((await dispatch(server, `/api/crowd/analyze?start=${start}`, {method: 'POST', headers: {'content-type': 'application/octet-stream'}})).status, 400);
  }
  assert.equal((await dispatch(server, '/api/crowd/analyze', {method: 'POST', headers: {'content-type': 'application/octet-stream', 'content-length': String(41 * 1024 * 1024)}})).status, 413);
  assert.equal((await dispatch(server, '/api/crowd/analyze', {method: 'POST', headers: {'content-type': 'application/octet-stream'}})).status, 400);
  assert.equal((await dispatch(server, '/api/crowd/status')).json().activeJobId, null);
  const unconfigured = await setup(t, {key: ''}); assert.equal((await upload(unconfigured.server)).status, 503);
  assert.equal(calls.length, 0); assert.equal(unconfigured.calls.length, 0);
});

test('real ffmpeg normalization removes audio, fixes timing and rejects undecodable input', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'crowd-media-'));
  t.after(() => rm(root, {recursive: true, force: true}));
  const input = path.join(root, 'synthetic.mp4');
  const exec = promisify(execFile);
  await exec('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=c=navy:s=128x72:r=30:d=3',
    '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3', '-c:v', 'libx264', '-c:a', 'aac', '-shortest', input]);
  const result = await normalizeClip(await readFile(input), 0, root);
  assert.equal(result.duration, 3);
  const probe = JSON.parse((await exec('ffprobe', ['-v', 'error', '-show_streams', '-of', 'json', path.join(root, 'clip.mp4')])).stdout);
  assert.equal(probe.streams.length, 1); assert.equal(probe.streams[0].width, 640); assert.equal(probe.streams[0].avg_frame_rate, '15/1');
  const invalid = await mkdtemp(path.join(tmpdir(), 'crowd-invalid-'));
  t.after(() => rm(invalid, {recursive: true, force: true}));
  await assert.rejects(normalizeClip(Buffer.from('invalid video'), 0, invalid), /Could not prepare/);
});


test('webcam route preserves capture provenance and rejects stale sessions before model access', async t => {
  const {server, calls, root} = await setup(t, {observation: {observation: empty, cue: {value: 'unknown', evidence_ids: []}}, expectedStart: 0});
  const session = '12345678-1234-1234-1234-123456789abc';
  const send = capturedAt => dispatch(server, `/api/crowd/analyze?${new URLSearchParams({mode: 'webcam', session, capturedAt, setRunId: 'set-000023'})}`,
    {method: 'POST', body: 'synthetic', headers: {'content-type': 'application/octet-stream'}});
  assert.equal((await send(new Date(Date.now() - 30000).toISOString())).status, 400);
  assert.equal(calls.length, 0);
  const captured = new Date().toISOString();
  const accepted = await send(captured); assert.equal(accepted.status, 202);
  const job = await until(async () => {const j = (await dispatch(server, `/api/crowd/jobs/${accepted.json().id}`)).json(); return j.status !== 'running' && j;});
  assert.equal(job.status, 'completed'); assert.equal(job.profile, 'webcam-v1');
  assert.equal(job.webcamSession, session); assert.equal(job.captureTime, captured);
  assert.equal(job.setRunId, 'set-000023');
  await until(async () => {try {return JSON.parse(await readFile(path.join(root, job.id, 'result.json'))).setRunId === 'set-000023';} catch {return false;}});
  const restored = createCrowdServer({storageRoot: root, key: ''});
  assert.equal((await dispatch(restored, `/api/crowd/jobs/${job.id}`)).json().setRunId, 'set-000023');
  assert.equal(job.cue.value, 'unknown'); assert.equal(job.remoteFileCleanup, 'deleted');
  assert.match(JSON.parse(calls[1].options.body).input[0].content[0].text, /WEBCAM PROFILE/);
});

test('history shows an actual in-flight Meta call and its response without resubmission', async t => {
  let release;
  const gate = new Promise(resolve => {release=resolve;});
  const {server,calls}=await setup(t,{gate});
  const id=(await upload(server)).json().id;
  const pending=await until(async()=>{
    const list=(await dispatch(server,'/api/crowd/jobs')).json().jobs;
    return list.find(j=>j.id===id && j.modelRequestedAt);
  });
  assert.equal(pending.status,'running');assert.equal(pending.modelRespondedAt,undefined);
  assert.equal((await dispatch(server,`/api/crowd/jobs/${id}/response`)).status,404);
  release();
  const completed=await until(async()=>{
    const list=(await dispatch(server,'/api/crowd/jobs')).json().jobs;
    return list.find(j=>j.id===id && j.status==='completed');
  });
  assert.ok(completed.modelRespondedAt);assert.equal(completed.rawResponseAvailable,true);
  const raw=await dispatch(server,`/api/crowd/jobs/${id}/response`);
  assert.equal(raw.status,200);assert.equal(raw.json().status,'completed');
  assert.equal(raw.bytes.toString().includes('synthetic-secret'),false);
  assert.equal(calls.filter(c=>c.url.endsWith('/responses')).length,1);
});

test('restart restores failed results, raw response and clips; history reads never call Meta', async t => {
  const {server,root}=await setup(t,{observation:{...empty,unexpected:'synthetic invalid field'}});
  const id=(await upload(server)).json().id;
  await until(async()=> (await dispatch(server,`/api/crowd/jobs/${id}`)).json().status==='failed');
  await until(async()=> {try {return JSON.parse(await readFile(path.join(root,id,'result.json'))).status==='failed';} catch {return false;}});
  const restored=createCrowdServer({storageRoot:root,key:'',fetchImpl:()=>{throw new Error('No network permitted');}});
  const history=(await dispatch(restored,'/api/crowd/jobs?mode=uploaded_clip')).json().jobs;
  assert.equal(history.length,1);assert.equal(history[0].status,'failed');
  assert.equal((await dispatch(restored,'/api/crowd/jobs?mode=webcam')).json().jobs.length,0);
  assert.equal((await dispatch(restored,`/api/crowd/jobs/${id}/response`)).status,200);
  assert.equal((await dispatch(restored,`/api/crowd/jobs/${id}/clip`)).status,200);
  assert.equal((await dispatch(restored,'/api/crowd/jobs',{headers:{origin:'https://untrusted.example'}})).status,403);
  assert.equal((await dispatch(restored,'/api/crowd/jobs?mode=bad')).status,400);
});
