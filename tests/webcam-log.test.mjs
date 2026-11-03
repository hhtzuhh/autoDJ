import test from 'node:test';
import assert from 'node:assert/strict';
import {mergeObservationLog, observationStatus} from '../src/webcamLog.ts';

const capture = {id:'local-1',sessionId:'camera-1',capturedAt:'2026-11-03T10:00:00.000Z',phase:'recording'};
const job = {id:'server-1',webcamSession:'camera-1',captureTime:capture.capturedAt,status:'running',stage:'Uploading the prepared clip to Meta'};

test('capture appears before submission; repeated server polls update one stable row', () => {
  assert.equal(observationStatus(capture),'Recording 6 seconds…');
  let rows=mergeObservationLog([capture],[job]);
  rows=mergeObservationLog(rows,[{...job,modelRequestedAt:capture.capturedAt}]);
  assert.equal(rows.length,1);assert.equal(rows[0].id,'local-1');
  assert.equal(observationStatus(rows[0]),'Calling Muse Spark…');
  assert.equal(capture.phase,'recording');
});

test('every reply remains even without adaptive mode; a new capture never replaces older results', () => {
  const completed={...job,status:'completed',observation:{summary_en:'Unknown is a valid observation.'}};
  const failed={...job,id:'server-2',captureTime:'2026-11-03T10:01:00.000Z',status:'failed',error:'Invalid observation'};
  const rows=mergeObservationLog([{...capture,id:'local-3',capturedAt:'2026-11-03T10:02:00.000Z'}],[completed,failed]);
  assert.equal(rows.length,3);
  assert.deepEqual(rows.map(observationStatus),['Recording 6 seconds…','Failed','Completed']);
  assert.equal(rows[2].job.observation.summary_en,completed.observation.summary_en);
});

test('stopping capture does not hide an accepted job or claim the server cancelled it', () => {
  const stopped={...capture,phase:'stopped',note:'Upload interrupted'};
  const rows=mergeObservationLog([stopped],[{...job,status:'completed'}]);
  assert.equal(rows.length,1);assert.equal(observationStatus(rows[0]),'Completed');
  assert.equal(rows[0].note,undefined);
});

test('slow running snapshots cannot overwrite a completed response', () => {
  const rows=mergeObservationLog([{...capture,jobId:job.id,job:{...job,status:'completed'}}],[job]);
  assert.equal(observationStatus(rows[0]),'Completed');
  const responding=mergeObservationLog([capture],[{...job,modelRespondedAt:capture.capturedAt}]);
  assert.equal(observationStatus(responding[0]),'Response received · finishing cleanup');
});
