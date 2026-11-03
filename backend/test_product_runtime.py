"""Exercise product imports and bridge startup from an unrelated directory."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


BACKEND = Path(__file__).resolve().parent


class ProductRuntimeTest(unittest.TestCase):
    def run_isolated(self, code, root, extra_env=None):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('DJ_', 'META_', 'MODEL_', 'PYTHONPATH'))}
        env.update(PYTHONPATH=str(BACKEND), DJ_DATA_ROOT=str(root))
        env.update(extra_env or {})
        result = subprocess.run([sys.executable, '-c', code], cwd=root, env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_executor_and_midi_share_data_root_and_selected_cache(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            for backend, suffix in [('essentia_dsp', 'time-map/essentia-dsp-v1'),
                                    ('all_in_one', 'time-map')]:
                payload = json.loads(self.run_isolated('''
import json, sys
from pathlib import Path
import probe, observer, transition, lifecycle_supervisor, plan_lifecycle, live_replanner
assert probe.CANCEL_FILE == transition.CANCEL_FILE
assert probe.COMMAND_LOG.parent == transition.TAKEOVER_FILE.parent
assert plan_lifecycle.DEFAULT_TAKEOVER_PATH == transition.TAKEOVER_FILE
assert lifecycle_supervisor.DEFAULT_REBASE_PATH == transition.REBASE_FILE
assert lifecycle_supervisor.MIXXX_PYTHON == sys.executable
assert lifecycle_supervisor.TRANSITION_EXECUTOR == Path(transition.__file__)
assert probe.DEFAULT_ANALYSIS_ROOT == transition.DEFAULT_ARTIFACTS
print(json.dumps({'analysis': str(transition.DEFAULT_ARTIFACTS),
                  'runtime': str(probe.CANCEL_FILE.parent)}))
''', root, {'DJ_ANALYSIS_BACKEND': backend}))
                self.assertEqual(Path(payload['analysis']), root / 'artifacts' / suffix)
                self.assertEqual(Path(payload['runtime']), root / 'artifacts/mixxx-runtime')

    def test_bridge_main_uses_explicit_data_root_without_midi_or_meta(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            (root / '.env').write_text('DJ_ANALYSIS_BACKEND=all_in_one\n')
            output = self.run_isolated('''
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
import live_bridge, observer
root = Path.cwd()
def observe(**kwargs):
    assert kwargs['analysis_root'] == root / 'artifacts/time-map'
    assert kwargs['command_log'] == root / 'artifacts/mixxx-runtime/commands.jsonl'
server = MagicMock()
with patch.object(sys, 'argv', ['bridge', '--data-root', str(root)]), \\
     patch.object(observer, 'run_observer', side_effect=observe) as run, \\
     patch.object(live_bridge, 'ThreadingHTTPServer', return_value=server):
    assert live_bridge.main() == 0
    assert run.call_count == 1
    server.shutdown.assert_called_once()
    server.server_close.assert_called_once()
assert (root / 'artifacts/live-performance').is_dir()
''', root)
            self.assertIn('dashboard_bridge_started', output)


if __name__ == '__main__':
    unittest.main()
