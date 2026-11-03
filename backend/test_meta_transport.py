"""Production request controls and error reporting, without network access."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from meta_planner import MetaConfig, OpenAICompatibleTransport


class MetaTransportTest(unittest.TestCase):
    def test_audit_keeps_rejected_raw_response_without_credentials(self):
        with TemporaryDirectory() as raw:
            folder=Path(raw)
            with patch('meta_planner.urlopen',return_value=self.response(content='invalid output',finish='length')):
                with self.assertRaises(RuntimeError):
                    OpenAICompatibleTransport(MetaConfig('private-test-key'),artifact_dir=folder)('system','user')
            self.assertEqual(json.loads((folder/'response.json').read_text())['choices'][0]['message']['content'],'invalid output')
            saved=''.join(path.read_text() for path in folder.iterdir())
            self.assertNotIn('private-test-key',saved)
            self.assertNotIn('Authorization',saved)

    def response(self, content='{"selected_candidate_id":"local"}', finish='stop'):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({
            'choices': [{'message': {'content': content}, 'finish_reason': finish}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 50,
                      'completion_tokens_details': {'reasoning_tokens': 20}},
        }).encode()
        return response

    def test_request_has_bounded_reasoning_and_exposes_only_safe_metrics(self):
        events = []
        with patch('meta_planner.urlopen', return_value=self.response()) as send:
            result = OpenAICompatibleTransport(MetaConfig('private-test-key'), on_event=events.append)('system', 'user')
        self.assertIn('selected_candidate_id', result)
        body = json.loads(send.call_args.args[0].data)
        self.assertEqual(body['reasoning_effort'], 'low')
        self.assertEqual(body['max_tokens'], 2048)
        self.assertEqual(send.call_args.kwargs['timeout'], 30)
        self.assertEqual([event['phase'] for event in events],
                         ['waiting_for_response', 'reading_response', 'validating_response'])
        self.assertEqual(events[-1]['reasoningTokens'], 20)
        self.assertNotIn('private-test-key', json.dumps(events))
        self.assertNotIn('selected_candidate_id', json.dumps(events))

    def test_truncated_output_is_rejected_even_when_json_is_valid(self):
        with patch('meta_planner.urlopen', return_value=self.response(finish='length')):
            with self.assertRaisesRegex(RuntimeError, 'token limit'):
                OpenAICompatibleTransport(MetaConfig('test'))('system', 'user')

    def test_action_window_gets_its_own_budget_in_one_request(self):
        request = json.dumps({'required_output_fields': ['transition_actions']})
        for budget in (4096, 5000):
            events = []
            with patch('meta_planner.urlopen', return_value=self.response()) as send:
                OpenAICompatibleTransport(MetaConfig('test', action_max_tokens=budget), on_event=events.append)('system', request)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(json.loads(send.call_args.args[0].data)['max_tokens'], budget)
            self.assertEqual(events[0]['maxTokens'], budget)

    def test_connection_timeout_is_classified_as_timeout(self):
        with patch('meta_planner.urlopen', side_effect=URLError(TimeoutError('socket'))):
            with self.assertRaises(TimeoutError):
                OpenAICompatibleTransport(MetaConfig('test'))('system', 'user')

    def test_invalid_settings_fail_before_sending_request(self):
        for kwargs in ({'timeout_seconds': float('nan')}, {'timeout_seconds': 0},
                       {'reasoning_effort': 'none'}, {'max_tokens': 0}, {'action_max_tokens': 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                MetaConfig('test', **kwargs)
