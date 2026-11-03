import type {CopilotState} from './types';

export function usePreparedQueue(state: CopilotState): boolean {
  return state.dataSource === 'live' && Boolean(state.setBuilder?.queue.length)
    && !state.flowTelemetry?.continuousSetActive;
}
