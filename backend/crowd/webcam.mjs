import {crowdPrompt, validateCrowdObservation} from './observation.mjs';

export function webcamPrompt(duration) {
  return `${crowdPrompt(duration)}
WEBCAM PROFILE: The operator has deliberately aimed a fixed webcam at consenting demo participants, possibly just one person. Observe them as one whole-frame audience region with id "audience" (omit it if nobody is visible). Do not require a venue or multiple people. Exclude anyone visibly operating DJ equipment.
Instead of the top-level object above, return exactly {"observation": <the crowd-v1 object above>, "cue": {"value":"build|ease|reorder|none|conflict|unknown", "evidence_ids":[]}}.
These are explicit demo gesture conventions, NOT inferred emotions or wishes: build = both arms held above the head continuously for at least two seconds; ease = both forearms held crossed in an X in front of the chest continuously for at least two seconds. reorder = one clearly visible hand held upright with only the index finger extended (the number 1), other fingers folded, continuously for at least two seconds. Do not mistake a two-finger V, open palm, pointing sideways, or an obscured hand for number 1. Do not count a passing dance gesture, clapping, hands near hair, or obscured arms. If more than one of these control poses appears anywhere in this clip use conflict. If uncertain use unknown; if none appears use none. For build/ease/reorder include evidence_ids pointing to an interval of at least two seconds describing that exact visible pose. For conflict provide evidence for the conflicting poses. Do not infer a cue from movement intensity. Natural movement metrics remain separate. The entire response must be English JSON.`;
}

export function validateWebcam(value, duration) {
  const exact = (obj, keys) => obj && typeof obj === 'object' && !Array.isArray(obj)
    && Object.keys(obj).length === keys.length && keys.every(k => Object.hasOwn(obj, k));
  if (!exact(value, ['observation', 'cue']) || !exact(value.cue, ['value', 'evidence_ids'])) throw new Error('Invalid webcam envelope');
  validateCrowdObservation(value.observation, duration);
  const {regions, evidence} = value.observation;
  if (regions.length > 1 || regions.some(r => r.id !== 'audience')) throw new Error('Fixed audience region required');
  const cue = value.cue;
  if (!['build', 'ease', 'reorder', 'none', 'conflict', 'unknown'].includes(cue.value)
    || !Array.isArray(cue.evidence_ids) || new Set(cue.evidence_ids).size !== cue.evidence_ids.length
    || cue.evidence_ids.some(id => !evidence.some(e => e.id === id))) throw new Error('Invalid gesture evidence');
  if (['build', 'ease', 'reorder', 'conflict'].includes(cue.value)) {
    if (!cue.evidence_ids.length || cue.evidence_ids.some(id => {
      const e = evidence.find(e => e.id === id); return e.end_seconds - e.start_seconds < 2;
    })) throw new Error('Gesture must be held for two seconds');
  }
  return value;
}
