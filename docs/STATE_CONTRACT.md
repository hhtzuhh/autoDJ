# Frontend state contract

The React UI consumes one authoritative `CopilotState`. Simulation and live
adapters use the same shape and must identify their `dataSource`.

The contract currently lives in `src/types.ts` and separates:

- Deck state observed from Mixxx;
- versioned queue state;
- adjacency-scoped transition readiness and candidate counts;
- bounded plan decision and provenance;
- plan lifecycle status;
- revisioned human, model, supervisor, and executor events.

The live adapter must not derive track identity from BPM alone. It must forward
the observer's uniquely resolved catalog identity, or leave it unresolved.
The UI must never infer execution from a model decision; execution status comes
from the local supervisor/executor event stream.

## Transition edges

`transitions` contains one directional edge for every adjacent pair in the
queue. An edge is separate from a track and separate from the execution
lifecycle:

- `pending` / `analyzing`: candidate preparation has not completed;
- `analyzed`: deterministic candidates exist, but no plan is selected;
- `planning`: the current A–B–C window is awaiting a bounded model decision;
- `ready`: a selection exists for the current window;
- `blocked`: deterministic analysis produced no compatible candidate;
- `invalidated`: a previously selected edge no longer matches current state.

Each edge includes its source and target identities, candidate count, queue
revision, selection provenance, and an optional reason. `ready` does not mean
the transition has executed; the plan lifecycle remains authoritative for
`armed`, `executing`, `completed`, and human takeover states.
