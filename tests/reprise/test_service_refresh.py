"""Refreshes exercise the real queue, scanner, ffprobe, and SQLite over local HTTP."""
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import requests
import pytest
from flask import Flask

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import PlaybackIdentity
from media_preview_generator.reprise.service import RepriseService, _mapping_fingerprint, set_service
from media_preview_generator.reprise.store import RepriseStore
from media_preview_generator.servers.base import Library, ServerConfig, ServerType
from media_preview_generator.servers.registry import ServerRegistry
from media_preview_generator.web import jobs
from media_preview_generator.web.job_gate import reset_job_gate
from media_preview_generator.web.routes.api_reprise import create_reprise_blueprint
from media_preview_generator.web.settings_manager import get_settings_manager, reset_settings_manager


def wait_until(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError('timed out waiting for background refresh')

@pytest.mark.real_job_async
def test_refresh_queue_pause_cancel_partial_and_reconcile(tmp_path, monkeypatch):
    media = tmp_path / 'episode.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=size=32x32:rate=1',
                    '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '1', '-threads', '1',
                    '-c:v', 'mpeg4', '-c:a', 'aac', str(media)], check=True, timeout=20)
    state = {'keys': ['101', '102'], 'block': True, 'missing': False}
    arrived, release = threading.Event(), threading.Event()

    class PlexFixture(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = urlparse(self.path).path
            if path == '/':
                result = {'machineIdentifier': 'machine'}
            elif path == '/library/sections/1/all':
                result = {'totalSize': len(state['keys']), 'Metadata': [{'ratingKey': k} for k in state['keys']]}
            else:
                key = path.rsplit('/', 1)[-1]
                if key == '102' and state['block']:
                    arrived.set()
                    release.wait(15)
                file = str(tmp_path / 'missing.mp4') if key == '102' and state['missing'] else str(media)
                result = {'Metadata': [{'ratingKey': key, 'title': 'Synthetic episode', 'duration': 1000,
                    'Media': [{'id': int(key) + 1000, 'duration': 1000, 'Part': [{'id': int(key) + 2000,
                        'file': file, 'size': media.stat().st_size, 'key': '/library/parts/' + key + '/file',
                        'Stream': [{'streamType': 2, 'id': int(key) + 3000, 'index': 1}]}]}]}]}
            body = json.dumps({'MediaContainer': result}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    plex = ThreadingHTTPServer(('127.0.0.1', 0), PlexFixture)
    plex_thread = threading.Thread(target=plex.serve_forever, daemon=True)
    plex_thread.start()
    url = f'http://127.0.0.1:{plex.server_port}'
    library = Library('1', 'Synthetic TV', (str(tmp_path),), kind='show')
    config = ServerConfig(id='server', type=ServerType.PLEX, name='Plex', enabled=True,
                          url=url, auth={}, libraries=[library], server_identity='machine')

    class PlexAdapter:
        def list_libraries(self):
            return [library]

        def _plex_http(self, method, path, **kwargs):
            response = requests.request(method, url + path, timeout=20, **kwargs)
            response.raise_for_status()
            return response

    registry = ServerRegistry()
    registry._configs['server'] = config
    registry._servers['server'] = PlexAdapter()
    markers = MarkerStore(str(tmp_path / 'markers.db'))
    store = RepriseStore(markers)
    old = PlaybackIdentity('machine', '999', '1999', '2999', '/library/parts/999/file', media.stat().st_size, 1000, '3999')
    version = media_version(str(media), 1000, '{}')
    store.register_media(version, old, server_id='server', metadata={'library_id': '1', 'server_url': url,
        'remote_part_path': str(media), 'mapping_fingerprint': _mapping_fingerprint(config, library)})
    reset_settings_manager()
    settings = get_settings_manager(str(tmp_path))
    settings.set('worker_groups', [{'id': 'local', 'name': 'Local', 'enabled': True,
        'availability': {'mode': 'always', 'windows': []}, 'members': [{'id': 'cpu', 'resource': 'cpu',
        'device': None, 'count': 1, 'job_types': ['previews']}]}])
    manager = jobs.JobManager(config_dir=str(tmp_path))
    monkeypatch.setattr(jobs, '_job_manager', manager)
    reset_job_gate()
    service = RepriseService(store, registry, manager)
    set_service(service)
    monkeypatch.setattr('media_preview_generator.web.auth.is_auth_external', lambda: True)
    app = Flask(__name__)
    app.secret_key = 'throwaway'
    app.register_blueprint(create_reprise_blueprint(service))
    client = app.test_client()
    body = {'server_id': 'server', 'library_id': '1'}

    def submit():
        response = client.post('/api/reprise/refresh', json=body)
        assert response.status_code == 202
        result = response.get_json()
        assert result['operation'] == 'refresh'
        assert str(tmp_path) not in json.dumps(result)
        return result['id']

    def get(job_id):
        response = client.get('/api/reprise/jobs/' + job_id)
        assert response.status_code == 200
        return response.get_json()

    try:
        job_id = submit()
        assert arrived.wait(15)
        assert submit() == job_id
        paused = client.post('/api/reprise/jobs/' + job_id + '/pause').get_json()
        assert paused['status'] == 'running' and paused['pause_requested'] is True
        resumed = client.post('/api/reprise/jobs/' + job_id + '/resume').get_json()
        assert resumed['pause_requested'] is False
        # Stop the in-flight scan before it can reconcile unseen entries.
        cancelled = client.post('/api/reprise/jobs/' + job_id + '/cancel').get_json()
        assert cancelled['status'] == 'cancelled'
        release.set()
        assert store.resolve_binding(old) is not None
        state.update(block=False, missing=True)
        # Wait for the cancelled runner to release its per-library scan lock.
        partial_id = submit()
        partial = wait_until(lambda: (r if (r := get(partial_id))['status'] in ('completed', 'failed', 'cancelled') else None))
        assert partial['status'] == 'failed' and partial['refresh']['skipped'] == 1
        assert store.resolve_binding(old) is not None
        state.update(keys=['101'], missing=False)
        complete_id = submit()
        complete = wait_until(lambda: (r if (r := get(complete_id))['status'] in ('completed', 'failed', 'cancelled') else None))
        assert complete['status'] == 'completed'
        assert complete['refresh']['registered'] == 1
        assert complete['refresh']['removed'] >= 1
        assert store.resolve_binding(old) is None
        assert str(tmp_path) not in json.dumps(complete)
    finally:
        release.set()
        plex.shutdown()
        plex_thread.join()
        plex.server_close()
        manager.close()
        markers.close()
        reset_job_gate()
        reset_settings_manager()
        set_service(None)


@pytest.mark.real_job_async
def test_pending_refresh_pause_and_resume(tmp_path, monkeypatch):
    from media_preview_generator.web.routes import job_runner

    reset_settings_manager()
    settings = get_settings_manager(str(tmp_path))
    settings.set("worker_groups", [{"id": "local", "enabled": True,
        "members": [{"id": "cpu", "resource": "cpu", "count": 1, "job_types": ["previews"]}]}])
    settings.processing_paused = True
    manager = jobs.JobManager(config_dir=str(tmp_path))
    monkeypatch.setattr(jobs, "_job_manager", manager)
    reset_job_gate()
    markers = MarkerStore(str(tmp_path / "markers.db"))
    registry = ServerRegistry()
    registry._configs["server"] = ServerConfig(id="server", type=ServerType.PLEX, name="Plex", enabled=True,
        url="http://127.0.0.1:1", auth={}, libraries=[Library("1", "TV", (str(tmp_path),))])
    service = RepriseService(RepriseStore(markers), registry, manager)
    set_service(service)
    monkeypatch.setattr("media_preview_generator.web.auth.is_auth_external", lambda: True)
    app = Flask(__name__)
    app.secret_key = "throwaway"
    app.register_blueprint(create_reprise_blueprint(service))
    client = app.test_client()
    job_id = None
    try:
        secret = service.store.create_client("read-only client")["secret"]
        assert client.post("/api/reprise/refresh", json={"server_id": "server", "library_id": "1"},
                           headers={"Authorization": "Bearer " + secret}).status_code == 403
        registry._configs["server"].enabled = False
        assert client.post("/api/reprise/refresh", json={"server_id": "server", "library_id": "1"}).status_code == 400
        registry._configs["server"].enabled = True
        response = client.post("/api/reprise/refresh", json={"server_id": "server", "library_id": "1"})
        assert response.status_code == 202
        job_id = response.get_json()["id"]
        paused = client.post(f"/api/reprise/jobs/{job_id}/pause").get_json()
        assert paused["status"] == "pending" and paused["pause_requested"] is True
        resumed = client.post(f"/api/reprise/jobs/{job_id}/resume").get_json()
        assert resumed["status"] == "pending" and resumed["pause_requested"] is False
        cancelled = client.post(f"/api/reprise/jobs/{job_id}/cancel").get_json()
        assert cancelled["status"] == "cancelled"
        wait_until(lambda: job_id not in job_runner._inflight_jobs)
    finally:
        if job_id:
            manager.cancel_job_if_active(job_id)
            wait_until(lambda: job_id not in job_runner._inflight_jobs)
        manager.close()
        markers.close()
        reset_job_gate()
        reset_settings_manager()
        set_service(None)
