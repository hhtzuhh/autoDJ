"""Read-only projection of planning attempts, including unaccepted responses.

Artifacts are evidence only: never compile, promote or execute a response here.
"""
from copy import deepcopy
import json
import re


class PlanningHistory:
    def __init__(self, root):
        self.root = root
        self.cache = {}

    def _read(self, path):
        try:
            stamp = path.stat().st_mtime_ns
            cached = self.cache.get(path)
            if cached and cached[0] == stamp:
                return cached[1]
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                return {}
            self.cache[path] = (stamp, value)
            return value
        except (OSError, ValueError):
            return {}

    def decorate_decisions(self, rows):
        """Recover trigger scope for older retained decisions from saved provenance."""
        rows = deepcopy(rows)
        for row in rows:
            request = row.get('requestId')
            if row.get('origin') == 'rolling' and isinstance(request, str) and re.fullmatch(r'meta-\d+', request):
                trace = self._read(self.root / request / 'set-run.json')
                row.setdefault('planningScope', trace.get('scope'))
        return rows

    def attempts(self, events):
        rows, accepted = {}, set()
        for event in sorted(events, key=lambda e: e['at']):
            data = event['data']
            request = data.get('requestId')
            if not isinstance(request, str) or not re.fullmatch(r'meta-\d+', request):
                continue
            key = (event.get('setRunId'), request)
            if event['kind'] == 'decision' and data.get('status') == 'selected_not_executed':
                accepted.add(key)
            if event['kind'] not in ('planning_attempt', 'planning_failed'):
                continue
            previous = rows.get(key, {})
            rows[key] = {**previous, 'id': request, 'setRunId': event.get('setRunId'),
                'sessionId': event['sessionId'], 'startedAt': previous.get('startedAt', data.get('startedAt')),
                'updatedAt': event['at'], 'status': data.get('status', 'failed'),
                'detail': data.get('detail') or data.get('error'),
                'scope': data.get('scope', previous.get('scope')), 'model': data.get('model', previous.get('model'))}
        result = []
        for key, row in rows.items():
            if key in accepted and row['status'] not in ('failed', 'timeout', 'discarded'):
                continue  # The accepted decision already has its own lifecycle row.
            directory = self.root / row['id']
            trace = self._read(directory / 'set-run.json')
            request = self._read(directory / 'request.json')
            response = self._read(directory / 'response.json')
            row['scope'] = row['scope'] or trace.get('scope')
            row['model'] = row['model'] or request.get('model') or 'Meta'
            row['requestSaved'] = bool(request)
            try:
                user = next(m for m in request.get('messages', []) if m.get('role') == 'user')
                context = json.loads(user['content']).get('crowd_context') or {}
                row.update(goal=context.get('goal'), signalSource=context.get('source'), observationId=context.get('jobId'))
            except (StopIteration, ValueError, TypeError, KeyError, AttributeError):
                pass
            row['responseReceived'] = bool(response)
            try:
                choice = response['choices'][0]
                content = choice['message']['content']
                row['rawText'] = content if isinstance(content, str) else json.dumps(content)
                row['finishReason'] = choice.get('finish_reason')
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    row['modelReason'] = parsed.get('reason') if isinstance(parsed.get('reason'), str) else None
                    actions = parsed.get('transition_actions')
                    row['transitionFunctions'] = [[a['function'] for a in group if isinstance(a, dict) and isinstance(a.get('function'), str)]
                        for group in actions if isinstance(group, list)] if isinstance(actions, list) else None
                    in_track = parsed.get('in_track_actions')
                    row['inTrackCount'] = len(in_track) if isinstance(in_track, list) else None
            except (KeyError, IndexError, TypeError, ValueError):
                pass
            result.append(row)
        return deepcopy(sorted(result, key=lambda r: r['updatedAt'], reverse=True))
