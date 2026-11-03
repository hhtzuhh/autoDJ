import type {InTrackAction} from "./types";

export const inTrackLabels = {
  bass_pulse: "Bass pulse",
  bass_reveal: "Bass reveal",
  volume_duck: "Volume duck",
};

export function InTrackActions({actions}: {actions?: InTrackAction[] | null}) {
  return <section className="in-track-actions" aria-label="In-track actions">
    <h3>In-track actions · during the song</h3>
    {actions == null ? <p className="panel-details__note">Not included in this earlier plan. Rebuild to let Meta choose optional in-track actions.</p>
      : !actions.length ? <p className="panel-details__note">None selected · leave this song unchanged until transition preparation.</p>
      : <>
        <p className="panel-details__note">Planned on this transition’s outgoing song, before handoff preparation. These are song beat positions, not transition offsets.</p>
        <ul>{actions.map((a, i) => <li key={i}>
          <strong>{inTrackLabels[a.function]}</strong>
          <span>Song beats {a.start_beat}–{a.start_beat + a.duration_beats} · {a.duration_beats / 4} bars · {Math.round(a.depth * 100)}% control reduction</span>
          <small>{a.function === "bass_reveal" ? "Reduce low EQ, then gradually restore it."
            : a.function === "bass_pulse" ? "Lower and restore low EQ." : "Lower and restore channel volume."}</small>
        </li>)}</ul>
      </>}
  </section>;
}
