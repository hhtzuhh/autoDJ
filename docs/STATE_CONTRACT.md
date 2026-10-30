# Frontend state contract

The React UI consumes one authoritative `CopilotState`. Simulation and live
adapters use the same shape and must identify their `dataSource`.

The contract currently lives in `src/types.ts` and separates:

- Deck state observed from Mixxx;
- versioned queue state;
- bounded plan decision and provenance;
- plan lifecycle status;
- revisioned human, model, supervisor, and executor events.

The live adapter must not derive track identity from BPM alone. It must forward
the observer's uniquely resolved catalog identity, or leave it unresolved.
The UI must never infer execution from a model decision; execution status comes
from the local supervisor/executor event stream.

