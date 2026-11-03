import test from 'node:test';
import assert from 'node:assert/strict';
import { crowdPrompt, validateCrowdObservation } from './observation.mjs';

function fixture() {
  const metric = value => ({value, evidence_ids: ['e1']});
  return {schema_version: 'crowd-v1', quality: {visibility: 'good', camera_motion: 'stable', limitations: []},
    excluded_subjects: ['DJ behind table'], summary_en: 'Synthetic validation fixture, not a model result.',
    regions: [{id: 'r1', location: 'Left foreground', audience_basis: 'Facing the stage', visibility: 'good',
      movement_types: ['torso_sway'], movement_spread: metric('few'), movement_amplitude: metric('subtle'),
      movement_continuity: metric('sustained'), within_clip_trend: {value: 'stable', evidence_ids: ['e1', 'e2']},
      region_flow: metric('no_clear_flow')}],
    evidence: [{id: 'e1', region_id: 'r1', start_seconds: 0, end_seconds: 2, observation: 'Small torso sways early.'},
      {id: 'e2', region_id: 'r1', start_seconds: 6, end_seconds: 8, observation: 'Similar small torso sways late.'}]};
}
test('prompt timestamp limit cannot round beyond the normalized clip', () => {
  for (const duration of [6.066667, 5.933333, 6, 8]) {
    const limit = Number(crowdPrompt(duration).match(/end_seconds <= (\d+\.\d+)/)[1]);
    assert.ok(limit <= duration);
    assert.ok(duration - limit < .011);
    const v = fixture();
    v.evidence[1].start_seconds = 3;
    v.evidence[1].end_seconds = limit;
    assert.doesNotThrow(() => validateCrowdObservation(v, duration));
  }
  const v = fixture(); v.evidence[1].start_seconds = 3; v.evidence[1].end_seconds = 6.07;
  assert.throws(() => validateCrowdObservation(v, 6.066667));
});
test('accepts evidenced regional movement and an empty insufficient scene', () => {
  assert.equal(validateCrowdObservation(fixture(), 8).regions.length, 1);
  const v = fixture(); v.quality.visibility = 'insufficient'; v.regions = []; v.evidence = [];
  assert.equal(validateCrowdObservation(v, 8).regions.length, 0);
});
test('rejects ungrounded, wrong-region and out-of-window evidence', () => {
  for (const mutate of [v => v.regions[0].movement_spread.evidence_ids = [],
    v => v.regions[0].movement_spread.evidence_ids = ['missing'],
    v => v.evidence[0].region_id = 'r2', v => v.evidence[0].end_seconds = 8.1,
    v => v.evidence[0].end_seconds = 0]) {
    const v = fixture(); mutate(v); assert.throws(() => validateCrowdObservation(v, 8));
  }
});
test('rejects trends from one interval or overlapping intervals', () => {
  const v = fixture(); v.regions[0].within_clip_trend.evidence_ids = ['e1'];
  assert.throws(() => validateCrowdObservation(v, 8), /separate time windows/);
  v.regions[0].within_clip_trend.evidence_ids = ['e1', 'e2']; v.evidence[0].end_seconds = 7;
  assert.throws(() => validateCrowdObservation(v, 8), /separate time windows/);
});
test('camera motion and insufficient visibility block unsupported comparisons', () => {
  const v = fixture(); v.quality.camera_motion = 'moving';
  assert.throws(() => validateCrowdObservation(v, 8), /camera comparison/);
  v.regions[0].within_clip_trend = {value: 'unknown', evidence_ids: []};
  v.regions[0].region_flow = {value: 'unknown', evidence_ids: []};
  assert.doesNotThrow(() => validateCrowdObservation(v, 8));
  v.regions[0].visibility = 'insufficient'; v.regions[0].movement_types = ['unknown'];
  assert.throws(() => validateCrowdObservation(v, 8), /camera comparison/);
});
test('rejects extra control fields, percentages and legacy evidence aliases', () => {
  for (const mutate of [v => v.queue_action = 'skip', v => v.regions[0].movement_spread.value = 73,
    v => { v.evidence[0].evidence = v.evidence[0].observation; delete v.evidence[0].observation; }]) {
    const v = fixture(); mutate(v); assert.throws(() => validateCrowdObservation(v, 8));
  }
});
