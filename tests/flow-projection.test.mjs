import test from 'node:test';
import assert from 'node:assert/strict';
import {analysisTrackRows, deriveAgentFlow} from '../src/flowProjection.ts';
const now = 1_800_000_000_000;
function fixture() {
  return {dataSource: 'live', connected: true, autoArmEnabled: false, performanceRevision: 3, queueRevision: 2,
    decks: [{deck: 1, playing: true, track: {id: 'a', title: 'Synthetic A'}, beatIndex: 40}, {deck: 2, playing: false, track: null}],
    events: [], queue: [], transitions: [],
    plan: {id: 'p1', status: 'ready', selectedBy: 'meta', reason: 'Synthetic plan', metaCalls: [], futurePlanStatus: 'idle'},
    flowTelemetry: {sessionId: 'synthetic', observerUpdatedAt: now, ownedPlanStatus: null, ownedPlanId: null,
      executorRunning: false, completedTransitions: 0, lastExecution: null, continuousSetActive: false, rotationInProgress: false},
    setBuilder: {status: 'ready', catalogCount: 2, orderedTrackIds: ['a','b'], postAnalysisPlanning: 'pending', activationReady: true,
      queue: [{id:'a', title:'Synthetic A', analysisStatus:'ready', analysisOrigin:'cache'},
              {id:'b', title:'Synthetic B', analysisStatus:'ready', analysisOrigin:'computed'}],
      transitions: [{status:'analyzed', candidateCount:5, reason:null}], curatorCalls: []}};
}
const find = (flow, lane, id) => flow[lane].find(n => n.id === id);
test('DSP analysis and its cache never claim neural inference or confirmed downbeats', () => {
  const rows = analysisTrackRows([
    {id:'new', analysisStatus:'ready', analysisOrigin:'computed',
     analysisRun:{engine:'essentia_dsp', status:'completed', neuralNet:'not_used'}},
    {id:'cached', analysisStatus:'ready', analysisOrigin:'cache',
     analysisSummary:{analysisBackend:'essentia_dsp'}},
  ]);
  assert.deepEqual(rows.map(r=>r.route), ['pipeline', 'metadata']);
  assert.ok(rows.every(r=>r.source === 'Essentia + local DSP'));
  assert.match(rows[0].note, /No neural model used/);
  assert.match(rows[1].note, /Cached DSP analysis/);
  assert.ok(rows.every(r=>r.note.includes('downbeats estimated')));
});
test('library analysis precedes selection with explicit preparation and start nodes', () => {
  const s=fixture();s.setBuilder.selectionOnly=true;s.setBuilder.buildPhase='library_analysis';
  s.setBuilder.library=s.setBuilder.queue;s.setBuilder.queue=[];
  s.setBuilder.library[1].analysisStatus='analyzing';
  const flow=deriveAgentFlow(s,null,false,now);
  assert.deepEqual(flow.preparation.map(n=>n.id),['library','analysis','music','curation','pairs','export','performance','start']);
  assert.equal(find(flow,'preparation','analysis').status,'running');
  assert.equal(find(flow,'preparation','curation').status,'waiting');
  assert.match(find(flow,'preparation','analysis').label,/1\/2 ready/);
});
test('track rows distinguish complete metadata reuse, neural cache and confirmed fresh inference', () => {
  const tracks = [
    {id:'cache',analysisStatus:'ready',analysisOrigin:'cache'},
    {id:'old',analysisStatus:'ready',analysisOrigin:'available'},
    {id:'neural',analysisStatus:'ready',analysisOrigin:'computed',analysisRun:{status:'completed',neuralNet:'computed'}},
    {id:'dsp',analysisStatus:'ready',analysisOrigin:'computed',analysisRun:{status:'completed',neuralNet:'cache'}},
    {id:'running',analysisStatus:'analyzing',analysisRun:{status:'running',neuralNet:'unverified',engine:'all_in_one_plus_dsp'}},
    {id:'unknown',analysisStatus:'ready'},
    {id:'failed',analysisStatus:'failed',analysisRun:{status:'failed',neuralNet:'unverified'}},
  ];
  const rows=analysisTrackRows(tracks);
  assert.deepEqual(rows.map(r=>r.track.id), tracks.map(t=>t.id));
  assert.deepEqual(rows.map(r=>r.route), ['metadata','metadata','neural','pipeline','pipeline','unknown','pipeline']);
  assert.match(rows[1].note,/original run unrecorded/);
  assert.equal(rows[3].source,'Cached neural result + DSP');
  assert.match(rows[4].note,/not confirmed/);
  assert.match(rows[5].source,/origin unrecorded/);
});
test('cached analyses are available without pretending the analyzer is running', () => {
  const flow = deriveAgentFlow(fixture(), null, false, now);
  assert.equal(find(flow,'preparation','analysis').status, 'done');
  assert.match(find(flow,'preparation','analysis').label, /1 cached/);
  assert.equal(find(flow,'preparation','refine').status, 'skipped');
  assert.equal(find(flow,'preparation','export').status, 'waiting');
});
test('ready model output and ordinary playback never imply transition execution', () => {
  const s=fixture(); let flow=deriveAgentFlow(s,null,false,now);
  assert.equal(find(flow,'liveNodes','execute').status,'idle');
  assert.equal(find(flow,'liveNodes','verify').status,'idle');
  assert.equal(flow.completed,0);
  s.flowTelemetry.ownedPlanStatus='armed'; s.flowTelemetry.executorRunning=true;
  flow=deriveAgentFlow(s,null,false,now);
  assert.equal(find(flow,'liveNodes','execute').status,'waiting');
  s.flowTelemetry.ownedPlanStatus='executing';
  assert.equal(find(deriveAgentFlow(s,null,false,now),'liveNodes','execute').status,'running');
});
test('restored plans, missing telemetry, stale observers and disconnects cannot display live execution', () => {
  for(const mode of ['restored','missing','stale','disconnected','simulation']) {
    const s=fixture(); s.plan.status='completed';
    if(mode==='missing') delete s.flowTelemetry;
    if(mode==='stale') s.flowTelemetry.observerUpdatedAt=now-11000;
    if(mode==='simulation') s.dataSource='simulation';
    const flow=deriveAgentFlow(s,null,mode==='disconnected',now);
    if(mode==='restored') {assert.equal(flow.completed,0); assert.equal(find(flow,'liveNodes','verify').status,'idle');}
    else {assert.equal(flow.completed,null); assert.ok(flow.liveNodes.every(n=>n.status==='stale'));}
  }
});
test('a failed future request can coexist with an actual active executor', () => {
  const s=fixture(); s.flowTelemetry.ownedPlanStatus='executing'; s.flowTelemetry.executorRunning=true;
  s.plan.futurePlanStatus='degraded';
  s.setBuilder.postAnalysisPlanning='failed';s.setBuilder.status='error';s.setBuilder.error='Synthetic failure';
  const flow=deriveAgentFlow(s,null,false,now);
  assert.equal(find(flow,'liveNodes','execute').status,'running');
  assert.equal(find(flow,'liveNodes','plan').status,'error');
  assert.equal(find(flow,'preparation','refine').status,'error');
});
test('crowd staging and queue commit are not reported as audio execution', () => {
  const s=fixture(); const camera={running:true, starting:false, stage:'Recording', error:null, job:null,
    adaptive:{enabled:true,status:'staged',reason:'Waiting for boundary',decision:{planId:'future'}}};
  let flow=deriveAgentFlow(s,camera,false,now);
  assert.equal(find(flow,'crowd','stage').status,'waiting');
  assert.equal(find(flow,'liveNodes','execute').status,'idle');
  camera.adaptive.status='committed';camera.adaptive.lastApplied={planId:'future'};
  flow=deriveAgentFlow(s,camera,false,now);
  assert.match(find(flow,'crowd','stage').label,/queue committed/);
  assert.equal(find(flow,'liveNodes','execute').status,'idle');
  camera.adaptive.lastApplied.executionStatus='rotation_failed';
  assert.equal(find(deriveAgentFlow(s,camera,false,now),'crowd','stage').status,'error');
});
test('projection preserves source state and shows only real recorded model calls', () => {
  const s=fixture(); s.setBuilder.curatorCalls=[{id:'real-record',stage:'post_analysis',model:'synthetic',status:'completed',attempt:1,
    startedAt:new Date(now-10000).toISOString(),finishedAt:new Date(now-1000).toISOString(),responseSeconds:9,completionTokens:600}];
  const original=JSON.stringify(s);const flow=deriveAgentFlow(s,null,false,now);
  assert.equal(flow.records.length,1);assert.equal(flow.records[0].id,'real-record');
  assert.equal(flow.inFlight,0);assert.equal(JSON.stringify(s),original);
});

test('music feature node tracks actual requests separately from queued songs and local analysis', () => {
  const s=fixture();s.setBuilder.selectionOnly=true;s.setBuilder.library=s.setBuilder.queue;
  s.setBuilder.library[0].musicFeatures={status:'running',model:'muse-spark-1.2',jobId:'music-1',requestStartedAt:new Date(now-1000).toISOString()};
  s.setBuilder.library[1].musicFeatures={status:'queued'};
  let flow=deriveAgentFlow(s,null,false,now);
  assert.equal(find(flow,'preparation','music').status,'running');
  assert.equal(find(flow,'preparation','analysis').status,'done');
  assert.equal(flow.inFlight,1);
  assert.equal(flow.records.filter(r=>r.scope==='Music feature extraction').length,1);
  s.setBuilder.library[0].musicFeatures={status:'ready',origin:'cache',jobId:'music-1',requestStartedAt:new Date(now-1000).toISOString(),elapsedSeconds:1,content:'Synthetic'};
  s.setBuilder.library[1].musicFeatures={status:'failed',error:'Synthetic failure'};
  flow=deriveAgentFlow(s,null,false,now);
  assert.equal(find(flow,'preparation','music').status,'error');
  assert.equal(flow.inFlight,0);
  assert.match(flow.records[0].detail,/cached/);
  assert.equal(find(deriveAgentFlow(s,null,true,now),'preparation','music').status,'stale');
});

test('selection waits during automatic Muse description stage', () => {
  const s=fixture();s.setBuilder.selectionOnly=true;s.setBuilder.status='analyzing';
  s.setBuilder.buildPhase='music_features';s.setBuilder.library=s.setBuilder.queue;s.setBuilder.queue=[];
  s.setBuilder.library[0].musicFeatures={status:'running',requestStartedAt:new Date(now-1000).toISOString()};
  s.setBuilder.library[1].musicFeatures={status:'ready',content:'Synthetic'};
  const flow=deriveAgentFlow(s,null,false,now);
  assert.equal(find(flow,'preparation','curation').status,'waiting');
  assert.equal(find(flow,'preparation','curation').label,'Waiting for Muse descriptions');
  assert.equal(find(flow,'preparation','music').status,'running');
  assert.equal(find(flow,'preparation','analysis').status,'done');
});


test('prepared candidates and export do not imply the selected set is playing', () => {
  const s=fixture();s.setBuilder.preserveSelectedOrder=true;s.setBuilder.activation={status:'playlist_ready'};
  let flow=deriveAgentFlow(s,null,false,now);
  assert.equal(find(flow,'preparation','start').status,'idle');
  assert.equal(find(flow,'preparation','performance').status,'waiting');
  assert.equal(flow.preparation.some(n=>n.id==='refine'),false);
  s.flowTelemetry.continuousSetActive=true;s.queue=[{id:'a'},{id:'b'}];
  flow=deriveAgentFlow(s,null,false,now);
  assert.equal(find(flow,'preparation','start').status,'done');
  s.queue=[{id:'other'}];
  assert.equal(find(deriveAgentFlow(s,null,false,now),'preparation','start').status,'idle');
});


test('reviewable performance window is distinct from playback and exposes failures', () => {
  const s=fixture();s.decks.forEach(d=>d.playing=false);
  s.setBuilder.activation={status:'playlist_ready'};
  for (const [status,expected] of [['building','running'],['ready','done'],['stale','error'],['failed','error']]) {
    s.setBuilder.performancePlan={status};
    s.flowTelemetry.activationInProgress=status==='building';
    const flow=deriveAgentFlow(s,null,false,now);
    assert.equal(find(flow,'preparation','performance').status,expected);
    assert.equal(find(flow,'preparation','start').status,status==='ready'?'waiting':'idle');
    assert.equal(find(flow,'preparation','export').status,'done');
    assert.equal(find(flow,'liveNodes','execute').status,'idle');
  }
  assert.equal(find(deriveAgentFlow(s,null,true,now),'preparation','performance').status,'stale');
});

test('manual playlist import is not attributed to Meta selection', () => {
  const s=fixture();s.setBuilder.selectionSource={kind:'mixxx_playlist',filename:'manual.m3u8'};
  s.setBuilder.selectionOnly=true;
  const flow=deriveAgentFlow(s,null,false,now);
  const selection=find(flow,'preparation','curation');
  assert.equal(selection.owner,'MANUAL · MIXXX PLAYLIST');
  assert.match(selection.label,/imported · order preserved/);
  assert.match(selection.detail,/No Meta selection call/);
  assert.equal(flow.records.length,0);
});
