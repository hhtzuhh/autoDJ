import test from 'node:test';
import assert from 'node:assert/strict';
import {usePreparedQueue} from '../src/queuePresentation.ts';

test('live reordered queue takes precedence over original prepared order', () => {
  const state = {dataSource:'live', setBuilder:{queue:[{id:'a'},{id:'b'},{id:'c'}]},
    queue:[{id:'a'},{id:'c'},{id:'b'}], flowTelemetry:{continuousSetActive:true}};
  assert.equal(usePreparedQueue(state),false);
  state.flowTelemetry.continuousSetActive=false;
  assert.equal(usePreparedQueue(state),true);
});
