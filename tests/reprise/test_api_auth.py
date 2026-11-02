"""Real SQLite credentials, service and Flask HTTP boundary regressions."""
from dataclasses import asdict

from flask import Flask

from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.reprise.identity import media_version
from media_preview_generator.reprise.models import PlaybackIdentity
from media_preview_generator.reprise.service import RepriseService
from media_preview_generator.servers.registry import ServerRegistry
from media_preview_generator.reprise.store import RepriseStore
from media_preview_generator.web.routes.api_reprise import create_reprise_blueprint


def test_client_auth_identity_rotation_and_no_owner_privilege(tmp_path, monkeypatch):
    monkeypatch.setenv('WEB_AUTH_TOKEN', 'owner-test-only')
    monkeypatch.setattr('media_preview_generator.web.auth.is_auth_external', lambda: True)
    markers = MarkerStore(str(tmp_path / 'markers.db'))
    store = RepriseStore(markers)
    service = RepriseService(store, ServerRegistry(), None)
    app = Flask(__name__)
    app.secret_key = 'throwaway'
    app.register_blueprint(create_reprise_blueprint(service))
    client = app.test_client()
    media = tmp_path / 'episode.mp4'
    media.write_bytes(b'1234567890')
    version = media_version(str(media), 10000, '{}')
    identity = PlaybackIdentity('machine', '11', '12', '13', '/library/parts/13/file', 10, 10000, '14')
    store.register_media(version, identity, server_id='server', metadata={'library_id': 'library'})
    created = client.post('/api/reprise/clients', json={'name': 'Player'}).get_json()
    token = created['secret']
    headers = {'Authorization': 'Bearer ' + token}
    assert client.get('/api/v1/results').status_code == 401
    unresolved = client.get('/api/v1/results', headers=headers)
    assert unresolved.status_code == 200
    assert unresolved.headers['Cache-Control'] == 'no-store'
    assert unresolved.json['resolution'] == 'unresolved'
    assert unresolved.json['eligible_skip_ranges'] == []
    result = client.get('/api/v1/results', headers=headers, query_string=asdict(identity))
    assert result.status_code == 200
    assert result.json['eligible_skip_ranges'] == []
    assert str(media) not in result.get_data(as_text=True)
    assert client.get('/api/reprise/clients', headers=headers).status_code == 403
    assert client.post('/api/v1/analyses', headers=headers, json={'media_version_ids': [version.id]}).status_code == 403
    rotated = client.post('/api/reprise/clients/' + created['id'] + '/rotate').get_json()
    assert token != rotated['secret']
    assert client.get('/api/v1/results', headers=headers).status_code == 401
    assert client.get('/api/reprise/clients', headers=headers).status_code == 403
    assert client.get('/api/v1/results', headers={'Authorization': 'Bearer ' + rotated['secret']}).status_code == 200
    assert client.delete('/api/reprise/clients/' + rotated['id']).status_code == 204
    assert client.get('/api/v1/results', headers={'Authorization': 'Bearer ' + rotated['secret']}).status_code == 401
    markers.close()
