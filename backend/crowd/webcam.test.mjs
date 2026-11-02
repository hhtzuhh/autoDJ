import test from 'node:test';
import assert from 'node:assert/strict';
import {validateWebcam} from './webcam.mjs';
function fixture() {
  const metric = value => ({value, evidence_ids: ['pose']});
  return {observation: {schema_version: 'crowd-v1', quality: {visibility: 'good', camera_motion: 'stable', limitations: []},
    excluded_subjects: [], summary_en: 'Synthetic single participant holds both arms overhead.',
    regions: [{id: 'audience', location: 'Whole frame', audience_basis: 'Designated demo camera', visibility: 'good',
      movement_types: ['arm_raise'], movement_spread: metric('most'), movement_amplitude: metric('moderate'),
      movement_continuity: metric('sustained'), within_clip_trend: {value: 'unknown', evidence_ids: []}, region_flow: metric('no_clear_flow')}],
    evidence: [{id: 'pose', region_id: 'audience', start_seconds: 1, end_seconds: 4, observation: 'Both arms remain overhead.'}]},
    cue: {value: 'build', evidence_ids: ['pose']}};
}
test('one clearly visible participant and held pose are valid webcam evidence', () => {
  assert.equal(validateWebcam(fixture(), 6).cue.value, 'build');
});
test('brief pose, missing evidence and shifting region IDs cannot produce a webcam control cue', () => {
  for (const mutate of [v => v.observation.evidence[0].end_seconds = 2, v => v.cue.evidence_ids = [],
    v => v.cue.evidence_ids = ['invented'], v => v.observation.regions[0].id = 'left', v => v.cue.value = 'skip_track']) {
    const v = fixture(); mutate(v); assert.throws(() => validateWebcam(v, 6));
  }
});
test('empty or uncertain scenes can remain unknown without inventing a command', () => {
  const v = fixture(); v.observation.regions = []; v.observation.evidence = [];
  v.observation.quality.visibility = 'insufficient'; v.cue = {value: 'unknown', evidence_ids: []};
  assert.equal(validateWebcam(v, 6).cue.value, 'unknown');
});
