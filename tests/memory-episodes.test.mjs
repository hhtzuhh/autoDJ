import test from 'node:test';
import assert from 'node:assert/strict';
import {inTrackAtCapture, transitionAtCapture} from '../src/memoryEpisodes.ts';

test('executor lifecycle does not claim that a handoff has started', () => {
  assert.match(transitionAtCapture({lifecycleStatus:'executing',transitionStatus:'execution_unknown'}), /unconfirmed/);
  assert.equal(transitionAtCapture({lifecycleStatus:'executing',transitionStatus:'waiting_for_handoff'}), 'Handoff had not started');
  assert.equal(transitionAtCapture({transitionStatus:'handoff_started_during_clip'}), 'Handoff started during this clip');
});

test('missing, optional, planned and observed in-track actions remain distinct', () => {
  const moment = {inTrackActiveDuringClip:[]};
  assert.match(inTrackAtCapture(moment), /Not recorded/);
  assert.equal(inTrackAtCapture({...moment,inTrackActions:[]}), 'None selected for this song');
  const action = {function:'bass_pulse'};
  assert.match(inTrackAtCapture({...moment,inTrackActions:[action]}), /1 planned.*no action execution confirmed/);
  assert.equal(inTrackAtCapture({...moment,inTrackActiveDuringClip:[action]}), 'Observed execution: bass pulse');
});
