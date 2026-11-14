"""Regressions found while checking long-lived server deployments."""
import base64
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize('value', [123, ['https://example.test'], '--exec=bad',
                                  'file:///etc/passwd', 'https://[bad', 'https:///missing-host'])
def test_download_rejects_non_http_urls_before_running_process(worker, monkeypatch, value):
    run = Mock()
    monkeypatch.setattr(worker.subprocess, 'run', run)
    response = worker.app.test_client().post('/api/music_download', json={'url': value})
    assert response.status_code == 400
    run.assert_not_called()


def test_download_timeout_releases_temporary_storage(worker, monkeypatch, tmp_path):
    folder = tmp_path / 'download'
    folder.mkdir()
    monkeypatch.setattr(worker.tempfile, 'mkdtemp', lambda **_: str(folder))
    run = Mock(side_effect=subprocess.TimeoutExpired('yt-dlp', 120))
    monkeypatch.setattr(worker.subprocess, 'run', run)
    response = worker.app.test_client().post('/api/music_download', json={'url': 'https://example.test/song'})
    assert response.status_code == 504
    assert not folder.exists()
    assert run.call_args.kwargs['timeout'] == 120
    assert run.call_args.args[0][-2:] == ['--', 'https://example.test/song']


@pytest.mark.parametrize('title', ['a' * 1000, 'Найкрасивіші країни Європи ' * 20, '😀' * 1000])
def test_long_titles_fit_download_and_render_filenames(worker, monkeypatch, tmp_path, title):
    folder = tmp_path / 'download'
    folder.mkdir()
    monkeypatch.setattr(worker.tempfile, 'mkdtemp', lambda **_: str(folder))
    monkeypatch.setattr(worker.subprocess, 'run', Mock(
        side_effect=lambda *args, **kwargs: (folder / 'track.mp3').write_bytes(b'mp3')))
    monkeypatch.setattr(worker, '_get_song_title', lambda _: title)
    response = worker.app.test_client().post('/api/music_download', json={'url': 'https://example.test/song'})
    assert response.status_code == 200, response.json
    assert base64.b64decode(response.json['file']) == b'mp3'
    assert (Path(worker.DOWNLOADS) / (response.json['title'] + '.mp3')).exists()
    # The exact same stem is used for output filenames in make_video.
    rendered = Path(worker.RENDER_DIR) / (worker._filename_stem(title) + '_0123456789ab_final.mp4')
    rendered.write_bytes(b'video')
    assert rendered.is_file()


def test_render_requests_remove_expired_output_without_requiring_music_download(worker, monkeypatch):
    import os
    # Mirror production: the general worker fixture keeps these folders as siblings.
    render_dir = Path(worker.STORAGE_DIR) / 'render'
    render_dir.mkdir()
    monkeypatch.setattr(worker, 'RENDER_DIR', str(render_dir))
    output = render_dir / 'old.mp4'
    output.write_bytes(b'expired')
    os.utime(output, (0, 0))
    assert worker.app.test_client().post('/api/render_video', json={}).status_code == 400
    assert not output.exists()


def test_download_accepts_storage_relative_filename(worker):
    output = Path(worker.RENDER_DIR) / 'video.mp4'
    output.write_bytes(b'video')
    response = worker.app.test_client().get('/api/download_video', query_string={'path': 'video.mp4'})
    assert response.status_code == 200
    assert response.data == b'video'
    response.close()


def test_download_rejects_null_byte_path(worker):
    response = worker.app.test_client().get('/api/download_video', query_string={'path': '\0'})
    assert response.status_code == 400


def test_render_rejects_garbage_after_base64_material(worker):
    response = worker.app.test_client().post('/api/render_video', json={
        'map_file': 'eA==!', 'background_video': 'eA==', 'video_title': 'Title',
    })
    assert response.status_code == 400
    assert 'not valid base64' in response.json['error']
