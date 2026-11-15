"""Thread-safe per-render progress for the single-process media worker."""
from functools import wraps
from threading import Lock
from time import monotonic
from uuid import UUID

from flask import current_app, g, jsonify, request
from proglog import ProgressBarLogger


class RenderProgress:
    def __init__(self):
        self.records = {}
        self.lock = Lock()

    def update(self, render_id, *, progress=None, phase=None, status=None, error=None):
        with self.lock:
            record = self.records.get(render_id)
            if record is None:
                return
            if progress is not None:
                record['progress'] = max(record['progress'], min(100, progress))
            for key, value in (('phase', phase), ('status', status), ('error', error)):
                if value is not None:
                    record[key] = value
            record['updated_at'] = monotonic()

    def snapshot(self, render_id):
        with self.lock:
            record = self.records.get(render_id)
            return {key: value for key, value in record.items() if key != 'updated_at'} if record else None

    def track(self, function):
        @wraps(function)
        def tracked(*args, **kwargs):
            data = request.get_json(silent=True) or {}
            raw_id = data.get('render_id')
            if raw_id is None:
                return function(*args, **kwargs)
            try:
                render_id = str(UUID(str(raw_id)))
            except ValueError:
                return jsonify(status='error', error='Invalid render ID.'), 400
            with self.lock:
                now = monotonic()
                self.records = {key: value for key, value in self.records.items()
                                if now - value['updated_at'] < 3600}
                if render_id in self.records:
                    return jsonify(status='error', error='This render ID has already been used.'), 409
                if len(self.records) >= 256:
                    return jsonify(status='error', error='Render service is busy. Try again later.'), 503
                self.records[render_id] = dict(status='rendering', progress=0,
                                               phase='Preparing materials', updated_at=now)
            g.render_progress_id = render_id
            try:
                response = current_app.make_response(function(*args, **kwargs))
                payload = response.get_json(silent=True) or {}
                if response.status_code < 400 and payload.get('status') == 'success':
                    self.update(render_id, status='success', progress=100, phase='Complete')
                else:
                    self.update(render_id, status='error', phase='Render failed',
                                error=payload.get('error') or 'Video render failed.')
                return response
            except Exception:
                self.update(render_id, status='error', phase='Render failed', error='Video render failed.')
                raise
        return tracked

    def logger(self):
        render_id = getattr(g, 'render_progress_id', None)
        if render_id is None:
            return None
        tracker = self

        class Logger(ProgressBarLogger):
            def bars_callback(self, bar, attr, value, old_value=None):
                if attr != 'index':
                    return
                total = self.bars[bar].get('total') or 0
                if total <= 0:
                    return
                fraction = max(0, min(1, value / total))
                if bar == 'chunk':
                    tracker.update(render_id, progress=round(5 + 10 * fraction), phase='Encoding audio')
                elif bar == 'frame_index':
                    tracker.update(render_id, progress=round(15 + 80 * fraction),
                                   phase='Finalizing video' if fraction == 1 else 'Encoding video')

        return Logger()
