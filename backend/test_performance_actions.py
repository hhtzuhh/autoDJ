"""Parameterized routines, rolling constraints and speed-aware execution."""
from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from performance_actions import ACTION_CATALOG, ACTION_SHAPE_EXAMPLES, compile_routine, minimum_ramp_beats, tempo_bounds, validate_edge_actions, validate_window_actions
from in_track_actions import compile_in_track, validate_in_track_actions
from clean_break import compile_clean_break_plan
from meta_planner import select_with_meta
from live_bridge import LiveStateStore

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "experiments/time-map"))
import transition
import routine_executor


def edge(a=120, b=140, exit=160):
    return {"mode": "beat_sync_crossfade", "pair_rank": 1, "source_exit_beat_index": exit,
            "target_entry_beat_index": 16, "transition_bars": 4, "tempo_bounds": tempo_bounds(a, b),
            "target_beat_count": 600, "earliest_source_action_beat": 32}


def actions(common=130, target=140):
    return [{"function": "ramp_tempo", "deck": "source", "start_beat": -64, "duration_beats": 64, "target_bpm": common},
            {"function": "crossfade", "start_beat": 0, "duration_beats": 16, "curve": "linear", "target_bpm": common},
            {"function": "bass_swap", "start_beat": 3, "duration_beats": 9},
            {"function": "ramp_tempo", "deck": "target", "start_beat": 16, "duration_beats": 64, "target_bpm": target}]


class PerformanceActionsTest(unittest.TestCase):
    def test_examples_match_contract_and_wrong_fields_are_not_silently_repaired(self):
        for example in ACTION_SHAPE_EXAMPLES:
            self.assertEqual(set(example),{'function',*ACTION_CATALOG[example['function']]['parameters']})
        bad=actions()
        bad[0]={'function':'ramp_tempo','parameters':{k:v for k,v in bad[0].items() if k!='function'}}
        with self.assertRaisesRegex(ValueError,"missing=.*unexpected=.*parameters"):
            validate_edge_actions(edge(),bad)
        bad=actions();bad[0]['curve']=None
        with self.assertRaisesRegex(ValueError,"unexpected=.*curve"):
            validate_edge_actions(edge(),bad)

    def test_one_bounded_correction_revalidates_complete_window(self):
        candidate={'candidate_id':'test','transitions':[edge()],'action_planning':True}
        valid={'selected_candidate_id':'test','intent':'maintain_energy','transition_actions':[actions()], 'confidence':.8,'reason':'synthetic'}
        invalid=deepcopy(valid);invalid['transition_actions'][0][0]['curve']='linear'
        for repair_ok in (True,False):
            with self.subTest(repair_ok=repair_ok):
                initial=Mock(return_value=json.dumps(invalid))
                correction=Mock(return_value=json.dumps(valid if repair_ok else invalid))
                kwargs=dict(queue_revision=1,intent='maintain_energy',track_ids=('a','b'),model='test',transport=initial,repair_transport=correction)
                if repair_ok:
                    result=select_with_meta([candidate],**kwargs)
                    self.assertEqual(result.decision.transition_actions,valid['transition_actions'])
                else:
                    with self.assertRaisesRegex(ValueError,'unexpected'):
                        select_with_meta([candidate],**kwargs)
                initial.assert_called_once();correction.assert_called_once()
                request=json.loads(correction.call_args.args[1])
                self.assertIn('curve',request['validation_error'])
                self.assertEqual(json.loads(request['invalid_output']),invalid)
        correction=Mock()
        select_with_meta([candidate],queue_revision=1,intent='maintain_energy',track_ids=('a','b'),model='test',transport=lambda *_:json.dumps(valid),repair_transport=correction)
        correction.assert_not_called()

    def test_ramp_minimum_is_directional_and_tolerates_only_float_noise(self):
        self.assertEqual(minimum_ramp_beats(112,140),40)
        self.assertEqual(minimum_ramp_beats(140,112),32)
        self.assertEqual(minimum_ramp_beats(100,110),16)
        self.assertEqual(minimum_ramp_beats(100,110.0001),20)
        e=edge(100,110)
        valid=[{'function':'ramp_tempo','deck':'source','start_beat':-16,'duration_beats':16,'target_bpm':110},
               {'function':'crossfade','start_beat':0,'duration_beats':16,'curve':'linear','target_bpm':110}]
        self.assertEqual(validate_edge_actions(e,valid)['pre_beats'],16)
        valid[0].update(start_beat=-12,duration_beats=12)
        with self.assertRaisesRegex(ValueError,'100 -> 110 BPM.*requires at least 16 beats'):
            validate_edge_actions(e,valid)

    def test_120_to_140_has_shared_tempo_without_source_led_six_percent(self):
        bounds = tempo_bounds(120, 140)
        self.assertLessEqual(bounds["minimum_bpm"], 130)
        self.assertGreaterEqual(bounds["maximum_bpm"], 130)
        self.assertIsNone(tempo_bounds(60, 180))
        self.assertEqual(validate_edge_actions(edge(), actions())["common_bpm"], 130)

    def test_playback_embellishments_optional_handoff_mandatory(self):
        plain = [actions(120)[1]]
        result = validate_edge_actions(edge(120, 120), plain)
        self.assertEqual((result["pre_beats"], result["post_beats"]), (0, 0))
        for invalid in ([], [actions()[0]], [*plain, *plain]):
            with self.assertRaises(ValueError): validate_edge_actions(edge(), invalid)

    def test_one_meta_call_decides_all_function_parameters(self):
        edges = [edge(), edge(140, 128, 240)]
        candidate = {"candidate_id": "routine-1", "transitions": edges, "action_planning": True}
        expected = [actions(), actions(134,128)]
        requests = []
        def transport(system, user):
            request = json.loads(user); requests.append(request)
            self.assertEqual(request["action_catalog"], ACTION_CATALOG)
            self.assertIn("transition_actions", request["required_output_fields"])
            options=request['candidate_data'][0]['transitions'][0]['tempo_ramp_options']
            self.assertTrue(options)
            for option in options:
                self.assertEqual(option['minimum_target_return_beats'],minimum_ramp_beats(option['common_bpm'],140))
            self.assertNotIn('tempo_ramp_options',candidate['transitions'][0])
            return json.dumps({"selected_candidate_id": "routine-1", "intent": "maintain_energy", "transition_actions": expected,
                               "confidence": .8, "reason": "Gradual shared tempo with a measured low-EQ exchange."})
        selection = select_with_meta([candidate], queue_revision=1, intent="maintain_energy", track_ids=tuple("abc"), model="test", transport=transport)
        self.assertEqual(selection.decision.transition_actions, expected)
        self.assertEqual(len(requests), 1)

    def test_invalid_tempo_eq_and_timeline_are_rejected_without_substitution(self):
        bad = []
        a=actions();a[1]["target_bpm"]=190;bad.append(a)
        a=actions();a[0]["duration_beats"]=4;bad.append(a)
        a=actions();a[2]["duration_beats"]=30;bad.append(a)
        a=actions();a[1]["function"]="echo_out";bad.append(a)
        a=actions();a[3]["start_beat"]=2;bad.append(a)
        a=actions();a[0]["target_bpm"]=float("nan");bad.append(a)
        for a in bad:
            with self.subTest(actions=a), self.assertRaises(ValueError): validate_edge_actions(edge(), a)
        with self.assertRaisesRegex(ValueError, "overlap"):
            validate_window_actions([edge(), edge(140,128,150)], [actions(), actions(134,128)])
        with self.assertRaisesRegex(ValueError, "runway"):
            validate_edge_actions({**edge(), "earliest_source_action_beat": 120}, actions())

    def build_plan(self, root, source_deck=1, in_track=None, clean=False):
        store=object.__new__(LiveStateStore)
        ids=("a"*64,"b"*64)
        store.queue={"revision": 1}
        store.analysis_catalog=SimpleNamespace(tracks={t:SimpleNamespace(analysis_cache_key=t) for t in ids})
        plan=store._build_executable_plan(source_id=ids[0],target_id=ids[1],source_exit_beat=160,target_entry_beat=16,
             transition_bars=4,model="synthetic",source_deck=source_deck,target_deck=3-source_deck,plan_suffix="routine",use_bass_swap=False)
        compile_routine(plan,edge(),validate_edge_actions(edge(),actions()))
        if clean:
            plan = compile_clean_break_plan(source_id=ids[0], target_id=ids[1],
                pair={'transition_mode':'clean_break','a_exit':{'beat_index':160},'b_entry':{'beat_index':16}},
                catalog=store.analysis_catalog, queue_revision=1, source_deck=source_deck, target_deck=3-source_deck, model='test')
            e={**edge(), 'mode':'clean_break', 'transition_bars':0}
            a=[{'function':'cut_to_next','start_beat':0,'fade_out_seconds':.5}]
            compile_routine(plan,e,validate_edge_actions(e,a))
        if in_track is not None:
            compile_in_track(plan, in_track)
        for t,bpm in zip(ids,(120,140)):
            folder=root/t;folder.mkdir(exist_ok=True)
            (folder/"track-analysis-v1.json").write_text(json.dumps({"audio_sha256":t,"analysis_cache_key":t,
                "audio":{"sample_rate":44100},"rhythm":{"bpm":bpm,"beats_seconds":[n*60/bpm for n in range(600)]}}))
        path=root/"plan.json";path.write_text(json.dumps(plan))
        return plan, transition.resolve_plan(path,root)

    def test_compilation_keeps_model_eq_parameters_and_rejects_tampering(self):
        with TemporaryDirectory() as raw:
            root=Path(raw);plan,resolved=self.build_plan(root)
            self.assertEqual(plan["mix"]["bass_swap"], {"start_beat":3,"end_beat":12})
            self.assertEqual(plan["automation_start_beat_index"],96)
            self.assertEqual(plan["deck_a"]["play_start_beat_index"],88)
            self.assertEqual(resolved["target_bpm"],130)
            plan["tempo_routine"]["common_bpm"]=150
            (root/"plan.json").write_text(json.dumps(plan))
            with self.assertRaisesRegex(ValueError,"does not match"):
                transition.resolve_plan(root/"plan.json",root)

    def execute_fake(self, root, *, source=1, takeover_at=None, cancel_at=None, missing_mapping=False, in_track=None, clean=False):
        plan,resolved=self.build_plan(root,source,in_track,clean)
        target=3-source
        clock=[0.0];last=[0.0];calls=[]
        state={source:{"pos":resolved['deck_a_start_seconds'],"rate":1.0,"play":True,"bpm":120},target:{"pos":0.,"rate":1.,"play":False,"bpm":140}}
        def advance():
            dt=clock[0]-last[0]
            for d in state.values():
                if d["play"]:d["pos"]+=dt*d["rate"]
            last[0]=clock[0]
        def ratio(output,deck,value):
            advance();state[deck]["rate"]=value;calls.append(("tempo",deck,value,clock[0]))
        def send(output,deck,control,value):
            advance();calls.append((control,deck,value,clock[0]))
            if control=="play":state[int(deck)]["play"]=bool(value)
        def seek(deck,seconds,*args):
            advance();state[deck]["pos"]=seconds;state[deck]["play"]=False
        def position(deck,*args):advance();return state[deck]["pos"]
        def wait(deadline,**kwargs):
            clock[0]=max(clock[0],deadline)
            if takeover_at and clock[0]>=takeover_at:
                calls.append(("takeover",));raise transition.TransitionTakenOver()
            if cancel_at and clock[0]>=cancel_at:
                raise transition.TransitionCancelled()
        def protocol(output):
            if missing_mapping:raise RuntimeError("mapping unavailable")
        probe=SimpleNamespace(mido=SimpleNamespace(open_output=lambda name:nullcontext(object())),port_name=lambda:"test",
             feedback_session=lambda name:nullcontext(),
             require_routine_protocol=protocol,read_position=position,precise_seek=seek,send_tempo_ratio=ratio,send_value=send,
             deck_status=lambda deck,**kw:{"sync_mode":0,"bpm":state[deck]["bpm"]*state[deck]["rate"],"file_bpm":state[deck]["bpm"]},
             TransitionCancelled=type("ProbeCancelled",(Exception,),{}))
        with patch.dict(sys.modules,{"probe":probe}),patch.object(transition,"wait_until",side_effect=wait), \
             patch.object(routine_executor.time,"monotonic",side_effect=lambda:clock[0]), \
             patch.object(transition,"CANCEL_FILE",root/"cancel"):
            try: routine_executor.execute(resolved,root,False)
            except RuntimeError:
                if not (takeover_at or cancel_at or missing_mapping):raise
        return state,calls

    def test_actual_position_drives_ramps_handoff_and_native_recovery_both_decks(self):
        for source in (1,2):
            with TemporaryDirectory() as raw:
                state,calls=self.execute_fake(Path(raw),source=source)
                target=3-source
                self.assertFalse(state[source]["play"])
                self.assertTrue(state[target]["play"])
                self.assertAlmostEqual(state[target]["rate"],1.0)
                self.assertTrue(any(c[:2]==("tempo",source) and abs(c[2]-130/120)<.001 for c in calls))
                self.assertTrue(any(c[:2]==("tempo",target) and abs(c[2]-130/140)<.001 for c in calls))
                start=next(c for c in calls if c[:3]==("play",str(target),1))
                # Native-speed wall time would be 36 s. Acceleration brings the
                # same file cue earlier, and execution follows that position.
                self.assertLess(start[3],36)
                self.assertGreater(start[3],32)

    def test_takeover_stops_all_controls_and_missing_mapping_touches_nothing(self):
        with TemporaryDirectory() as raw:
            _,calls=self.execute_fake(Path(raw),takeover_at=12)
            self.assertEqual(calls[-1],("takeover",))
        with TemporaryDirectory() as raw:
            state,calls=self.execute_fake(Path(raw),missing_mapping=True)
            self.assertEqual(calls,[])
            self.assertTrue(state[1]["play"])

    def test_cancel_preserves_running_song_at_its_current_speed(self):
        with TemporaryDirectory() as raw:
            state,calls=self.execute_fake(Path(raw),cancel_at=12)
            self.assertTrue(state[1]["play"])
            self.assertFalse(state[2]["play"])
            self.assertGreater(state[1]["rate"],1.0)
            self.assertLess(state[1]["rate"],130/120)

    def test_in_track_actions_execute_before_transition_on_both_decks_and_cut(self):
        for fn in ('bass_pulse','bass_reveal','volume_duck'):
            for source in (1,2):
                for clean in (False,True):
                    with self.subTest(fn=fn,source=source,clean=clean), TemporaryDirectory() as raw:
                        action={'function':fn,'track_id':'a'*64,'start_beat':48,'duration_beats':16,'depth':.25}
                        state,calls=self.execute_fake(Path(raw),source=source,in_track=[action],clean=clean)
                        control,baseline=('volume',.75) if fn=='volume_duck' else ('bass',.5)
                        shaping=[c for c in calls if c[:2]==(control,str(source)) and c[3] <= 12.1]
                        self.assertTrue(any(c[2] < baseline-.05 for c in shaping))
                        self.assertAlmostEqual(shaping[-1][2],baseline)
                        self.assertTrue(state[3-source]['play'])
                        self.assertFalse(state[source]['play'])
                        log=next(Path(raw).glob('runs/*/transition-events.jsonl')).read_text()
                        self.assertIn('in_track_action_completed',log)

    def test_in_track_cancel_restores_control_but_takeover_writes_nothing(self):
        action={'function':'volume_duck','track_id':'a'*64,'start_beat':48,'duration_beats':16,'depth':.25}
        for takeover in (False,True):
            with TemporaryDirectory() as raw:
                state,calls=self.execute_fake(Path(raw),in_track=[action],**({'takeover_at':8} if takeover else {'cancel_at':8}))
                self.assertTrue(state[1]['play'])
                self.assertFalse(state[2]['play'])
                if takeover:self.assertEqual(calls[-1],('takeover',))
                else:self.assertEqual(calls[-1][:3],('volume','1',.75))


if __name__=="__main__":unittest.main()
