import test from 'node:test';
import assert from 'node:assert/strict';
import {rollingPlannerView} from '../src/rollingPlanner.ts';

function fixture() {
  return {queue: ['a', 'b', 'c', 'd'].map(id => ({id, title: `Synthetic ${id}`})),
    transitions: [{sourceTrackId: 'b', targetTrackId: 'c', selectedBy: 'meta', status: 'ready'}],
    plan: {sourceTrackId: 'a', targetTrackId: 'b', status: 'armed', selectedBy: 'meta',
      planningWindowTrackIds: ['a', 'b', 'c'], metaCalls: []}};
}

test('request failure preserves retained readiness; success alone never creates readiness', () => {
  const state = fixture();
  state.plan.futurePlanningWindowTrackIds = ['b', 'c', 'd'];
  for (const status of ['failed', 'succeeded']) {
    state.plan.metaCalls = [{status, trackIds: ['b', 'c', 'd']}];
    const view = rollingPlannerView(state);
    assert.equal(view.ready, 1);
    assert.equal(view.futureDepth, 2);
    assert.deepEqual(view.edges.map(edge => edge.label), ['Ready', 'Awaiting Meta']);
  }
});

test('buffer readiness requires matching adjacent songs and Meta selection', () => {
  const state = fixture();
  state.transitions.push(
    {sourceTrackId: 'c', targetTrackId: 'a', selectedBy: 'meta', status: 'ready'},
    {sourceTrackId: 'c', targetTrackId: 'd', selectedBy: 'deterministic_fallback', status: 'ready'},
  );
  assert.equal(rollingPlannerView(state).ready, 1);
  state.plan.sourceTrackId = 'c';
  state.plan.targetTrackId = 'd';
  assert.equal(rollingPlannerView(state).futureDepth, 0);
  assert.equal(rollingPlannerView(state).ready, 0);
});

test('cancellation stays visible while a request is pending', () => {
  const state = fixture();
  state.plan.status = 'cancelled';
  state.plan.metaCalls = [{status: 'calling', trackIds: ['a', 'b', 'c']}];
  const view = rollingPlannerView(state);
  assert.equal(view.edges[0].label, 'Cancelled');
  assert.equal(view.edges[0].tone, 'warning');
  assert.equal(view.edges[1].label, 'Ready');
});
