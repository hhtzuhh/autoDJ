from copy import deepcopy
import json
import unittest
from types import SimpleNamespace
from in_track_actions import validate_in_track_actions, compile_in_track
from performance_actions import validate_window_actions
from meta_planner import select_with_meta
from test_performance_actions import edge, actions
from live_bridge import LiveStateStore


def action(track='a', start=48):
    return {'function':'bass_pulse','track_id':track,'start_beat':start,'duration_beats':16,'depth':.4}


class InTrackTest(unittest.TestCase):
    def test_single_call_accepts_empty_or_selected_actions_and_rejects_missing_field(self):
        e=edge(); candidate={'candidate_id':'test','transitions':[e], 'action_planning':True,
                            'in_track_planning':True,'in_track_source_ids':['a']}
        for selected in ([], [action()], None):
            calls=[]
            def transport(system,user):
                request=json.loads(user);calls.append(request)
                self.assertIn('in_track_actions',request['required_output_fields'])
                self.assertEqual(len(request['in_track_catalog']),3)
                self.assertEqual(request['candidate_data'][0]['in_track_windows'][0]['track_id'],'a')
                reply={'selected_candidate_id':'test','intent':'maintain_energy','transition_actions':[actions()],
                       'confidence':.8,'reason':'Synthetic choice including leaving the song unchanged.'}
                if selected is not None:reply['in_track_actions']=selected
                return json.dumps(reply)
            kwargs=dict(queue_revision=1,intent='maintain_energy',track_ids=('a','b'),model='test',transport=transport)
            if selected is None:
                with self.assertRaisesRegex(ValueError,'in_track_actions'):select_with_meta([candidate],**kwargs)
            else:
                result=select_with_meta([candidate],**kwargs)
                self.assertEqual(result.decision.in_track_actions,selected)
            self.assertEqual(len(calls),1)

    def test_invalid_actions_cannot_overlap_transition_or_previous_song_handoff(self):
        edges=[edge(),edge(140,128,240)]
        routines=validate_window_actions(edges,[actions(),actions(134,128)])
        for bad in ([action('unknown')], [action(start=80)], [action('b',48)],
                    [action(),action(start=52)], [{**action(),'depth':.9}],
                    [{**action(),'start_beat':True}], [{**action(),'function':'loop'}],
                    [{**action(),'duration_beats':float('nan')}], [{**action(),'parameters':{}}]):
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                validate_in_track_actions(edges,deepcopy(routines),bad,['a','b'])
        result=validate_in_track_actions(edges,routines,[action(),action('b',128)],['a','b'])
        self.assertEqual(result[1]['in_track_actions'],[action('b',128)])

    def test_compilation_and_future_projection_retain_source_specific_actions(self):
        store=object.__new__(LiveStateStore)
        store.queue={'revision':1}
        store.analysis_catalog=SimpleNamespace(tracks={t:SimpleNamespace(analysis_cache_key=t) for t in ('a','b','c')})
        store.transition_overrides={}
        candidate={'candidate_id':'test','transitions':[edge(),edge(140,128,240)], 'action_planning':True,
                   'in_track_planning':True,'in_track_source_ids':['a','b']}
        reply={'selected_candidate_id':'test','intent':'maintain_energy','transition_actions':[actions(),actions(134,128)],
               'in_track_actions':[action(),action('b',128)],'confidence':.8,'reason':'Synthetic'}
        selected=select_with_meta([candidate],queue_revision=1,intent='maintain_energy',track_ids=('a','b','c'),model='test',transport=lambda *_:json.dumps(reply))
        compiled=store._compile_three_transition_candidate(('a','b','c'),selected,'test',1,2)
        self.assertEqual(compiled['next_executable_plan']['in_track_actions'],[action()])
        self.assertEqual(compiled['next_executable_plan']['automation_start_beat_index'],48)
        self.assertEqual(compiled['next_executable_plan']['deck_a']['play_start_beat_index'],40)
        store.selection=compiled
        store._restore_transition_buffer_overrides()
        self.assertEqual(store.transition_overrides[('b','c')]['inTrackActions'],[action('b',128)])
        promoted=store._plan_from_buffer_edge(compiled['transition_buffer'][1],source_id='b',target_id='c',source_deck=2,target_deck=1,model='test',suffix='next')
        self.assertEqual(promoted['in_track_actions'],[action('b',128)])
        self.assertEqual(promoted['automation_start_beat_index'],128)
