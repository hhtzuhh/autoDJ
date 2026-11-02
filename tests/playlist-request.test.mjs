import test from 'node:test';
import assert from 'node:assert/strict';
import {requestedTrackCount} from '../src/playlistRequest.ts';

test('explicit counts work in Chinese and English, without interpreting BPM as song counts', () => {
  for(const [text,count] of [['幫我排 3 首 house',3],['十二首歌',12],['兩首歌',2],['二十首',20],['Build ten tracks at 125 BPM',10],['3 songs, 20 minutes',3],['house at 128 BPM',null],['1.5 songs',null]]) {
    assert.equal(requestedTrackCount(text),count,text);
  }
});
test('out-of-range explicit counts remain visible for validation instead of silently using a different count', () => {
  assert.equal(requestedTrackCount('build 51 tracks'),51);
  assert.equal(requestedTrackCount('0 首'),0);
});
