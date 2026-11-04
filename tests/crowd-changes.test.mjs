import test from 'node:test';
import assert from 'node:assert/strict';
import {changeStatus, reorderedIds} from '../src/crowdChanges.ts';

test('proposal, applied queue and completed handoff have distinct claims', () => {
  const decision = {planId:'crowd-1'};
  assert.match(changeStatus({status:'staged'},decision),/^Proposed/);
  assert.match(changeStatus({status:'off'},decision),/^Not applied/);
  assert.match(changeStatus({lastApplied:{planId:'old',executionStatus:'completed'}},decision),/^Not applied/);
  assert.match(changeStatus({lastApplied:{planId:'crowd-1'}},decision),/^Applied to queue/);
  assert.equal(changeStatus({lastApplied:{planId:'crowd-1',executionStatus:'completed'}},decision),'First handoff completed');
  assert.match(changeStatus({}, {...decision,applicationStatus:'failed'}),/^Not applied/);
});

test('animate only reordered identities; steady polling and another set do not count', () => {
  assert.deepEqual(reorderedIds(['a','b','c','d'],['a','b','d','c']),['d','c']);
  assert.deepEqual(reorderedIds(['a','b'],['a','b']),[]);
  assert.deepEqual(reorderedIds([],['a','b']),[]);
  assert.deepEqual(reorderedIds(['a','b'],['a','x']),[]);
  assert.deepEqual(reorderedIds(['a','b'],['a','b','c']),[]);
});

import {songTitle, decisionRows, decisionLogStatus} from '../src/crowdChanges.ts';
test('song labels remove known source/artist suffixes without cutting hyphenated titles', () => {
  assert.equal(songTitle({title:'Lets Go - FesliyanStudios.com - David Renda'}),'Lets Go');
  assert.equal(songTitle({title:'Song - Someone',artist:'Someone'}),'Song');
  assert.equal(songTitle({title:'A - B',artist:'Someone'}),'A - B');
});
test('one row per decision survives another decision and status update', () => {
  const first={planId:'first',recordedAt:1,historyStatus:'completed'};
  const second={planId:'second',recordedAt:2,historyStatus:'staged'};
  const state={status:'staged',decisionHistory:[second,first],decision:second,lastApplied:{...first,executionStatus:'completed'}};
  const rows=decisionRows(state);
  assert.deepEqual(rows.map(r=>r.planId),['second','first']);
  assert.equal(decisionLogStatus(state,rows[0]),'Pending application');
  assert.equal(decisionLogStatus(state,rows[1]),'First handoff completed');
  assert.match(decisionLogStatus({status:'off'},second),/^Not applied/);
  assert.match(decisionLogStatus({status:'off',memory:{sessionId:'new'}},{...first,sessionId:'old',executionStatus:'executing'}),/final result unavailable/);
});

import {belongsToRun, decisionChanges} from '../src/crowdChanges.ts';
test('run views never infer an older or unassigned record belongs to the current set', () => {
  assert.equal(belongsToRun({},'set-000002'),false);
  assert.equal(belongsToRun({setRunId:'set-000001'},'set-000002'),false);
  assert.equal(belongsToRun({setRunId:'set-000002'},'set-000002'),true);
  assert.equal(belongsToRun({},''),true);
  assert.equal(belongsToRun({},'all'),true);
  assert.equal(decisionRows({decisionHistory:[{planId:'p',setRunId:'set-000001'},{planId:'p',setRunId:'set-000002'}]}).length,2);
});
test('same technique with different BPM or duration is a transition change, not an in-track action', () => {
  const before={sourceTrack:'a',targetTrack:'b',technique:'crossfade',sourceExitBeat:240,targetEntryBeat:64,
    transitionActions:[{function:'crossfade',start_beat:0,duration_beats:16,target_bpm:110}],inTrackActions:[]};
  const after={...before,transitionActions:[{...before.transitionActions[0],duration_beats:8,target_bpm:115}]};
  const row={changeSummary:{beforeWindow:[before],afterWindow:[after]}};
  assert.match(decisionChanges(row).transition,/1 future handoff changed/);
  assert.equal(decisionChanges(row).inTrack,'In-track · none selected');
  assert.match(decisionChanges({changeSummary:{beforeWindow:[before],afterWindow:[before]}}).transition,/unchanged/);
});

import {planningLogRows, planningAttemptStatus} from '../src/crowdChanges.ts';
test('discarded build response and timeout remain visible beside accepted decisions', () => {
  const discarded={id:'meta-3',updatedAt:3,status:'discarded',responseReceived:true};
  const timeout={id:'meta-1',updatedAt:1,status:'timeout',responseReceived:false};
  const state={decisionHistory:[{planId:'accepted',recordedAt:2}],planningAttempts:[timeout,discarded]};
  assert.deepEqual(planningLogRows(state).map(e=>e.kind),['attempt','decision','attempt']);
  assert.equal(planningAttemptStatus(discarded),'Response discarded · not applied');
  assert.equal(planningAttemptStatus(timeout),'Timed out · not applied');
  assert.equal(planningAttemptStatus({...timeout,recoveredBy:'meta-2'}),'Earlier failure · later background plan ready');
  assert.equal(planningAttemptStatus({status:'calling',sessionId:'old'},'new'),'Interrupted · outcome unconfirmed');
  assert.equal(planningAttemptStatus({status:'calling',sessionId:'new'},'new'),'Calling Meta…');
});

import {decisionTrigger} from '../src/crowdChanges.ts';
import {reorderProgress} from '../src/crowdChanges.ts';
test('reorder receipt and pending order are visible before the live queue changes', () => {
  const before=['a','b','c','d','e'], after=['a','b','c','e','d'];
  const request={jobId:'gesture',setRunId:'run',goal:'reorder'};
  const decision={observationId:'gesture',setRunId:'run',planId:'plan',before,after,queueChanged:true,lockedThrough:2};
  const state={request,reason:'working',enabled:true,observing:true};
  assert.equal(reorderProgress({...state,status:'queued'},'run',before).phase,'received');
  assert.equal(reorderProgress({...state,status:'planning'},'run',before).phase,'planning');
  const pending=reorderProgress({...state,status:'staged',observing:false,decision},'run',before);
  assert.equal(pending.phase,'pending');
  assert.deepEqual(pending.decision.after,after);
  assert.deepEqual(before,['a','b','c','d','e']);
  assert.equal(reorderProgress({...state,status:'committed',lastApplied:decision},'run',after).phase,'applied');
  assert.equal(reorderProgress({...state,status:'staged',decision},'run',['b','a','c','d','e']).phase,'waiting');
  assert.equal(reorderProgress({...state,status:'off',decision:{...decision,historyStatus:'discarded',outcomeReason:'Human takeover.'}},'run',before).phase,'cancelled');
  assert.equal(reorderProgress({...state,status:'staged',decision},'other-run',before),null);
  assert.equal(reorderProgress({...state,request:{...request,goal:'build'}},'run',before),null);
});
test('camera context in rolling calls does not imply another gesture trigger', () => {
  assert.equal(decisionTrigger({origin:'rolling',planningScope:'future_buffer',observationId:'one'}),'Trigger · automatic rolling refresh');
  assert.equal(decisionTrigger({triggerSource:'gesture',triggerGoal:'reorder'}),'Trigger · number 1 gesture (reorder)');
  assert.equal(decisionTrigger({triggerSource:'gesture',triggerGoal:'build'}),'Trigger · arms-up gesture (more in-track effect)');
});

test('no change and removed in-track actions are explicit without claiming execution', () => {
  const action={function:'bass_pulse',track_id:'a',start_beat:80,duration_beats:8,depth:.2};
  const before={sourceTrack:'a',targetTrack:'b',inTrackActions:[action]};
  assert.equal(decisionLogStatus({}, {historyStatus:'unchanged'}), 'No change · existing plan kept');
  assert.equal(decisionChanges({changeSummary:{beforeWindow:[before],afterWindow:[before]}}).inTrack, 'In-track · unchanged (1 planned)');
  assert.equal(decisionChanges({changeSummary:{beforeWindow:[before],afterWindow:[{...before,inTrackActions:[]}]}}).inTrack, 'In-track · removed from the future plan');
});
