"""Persistent Start playback identities. A failed start never reuses its number."""
from copy import deepcopy
from datetime import datetime, timezone
import json


class SetRuns:
    def __init__(self, root):
        self.root = root
        self.rows = []
        for path in sorted(root.glob('set-*/run.json')):
            self.rows.append(json.loads(path.read_text()))
        self.current = deepcopy(self.rows[-1]) if self.rows else None

    def start(self, selection, queue):
        self.root.mkdir(parents=True, exist_ok=True)
        numbers = [int(p.name[4:]) for p in self.root.glob('set-*') if p.name[4:].isdigit()]
        number = max(numbers, default=0) + 1
        while True:
            run_id = f'set-{number:06d}'
            directory = self.root / run_id
            try:
                directory.mkdir()
                break
            except FileExistsError:
                number += 1
        row = {'id': run_id, 'number': number, 'startedAt': datetime.now(timezone.utc).isoformat(),
               'status': 'starting', 'planId': selection['next_executable_plan']['plan_id'],
               'requestId': selection.get('meta_origin', {}).get('requestId'),
               'trackIds': list(queue['ordered_track_ids'])}
        review = deepcopy(selection)
        review['set_run_id'] = run_id
        review['next_executable_plan']['set_run_id'] = run_id
        (directory / 'reviewed-plan.json').write_text(json.dumps(review, ensure_ascii=False, indent=2))
        self.current = row
        self.rows.append(row)
        self.update('starting')
        return deepcopy(row)

    def update(self, status):
        if self.current is None:
            return
        self.current['status'] = status
        self.rows[-1] = self.current
        path = self.root / self.current['id'] / 'run.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.current, ensure_ascii=False, indent=2))
        temporary.replace(path)
