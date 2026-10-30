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

For the current A–B–C window, **Plan with Meta** temporarily moves A→B to
`planning`. Local code first removes candidates whose A exit is no longer in
the future and whose B entry/dwell/exit ordering cannot preserve a B→C draft.
A valid single-call model choice moves the edge to `ready` with
`selectedBy: "meta"`; a request or validation failure returns it to `analyzed`
with a non-secret reason. The model name, confidence, and reason shown in the
plan come from the validated selection artifact rather than UI defaults.
