from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from better.api.app import create_app
from better.config import Settings
from better.providers.fake import make_demo_bundle
from better.services.application import make_application
from better.storage.repository import Repository


def test_multiple_automation_requires_reviewed_plan_and_same_origin(monkeypatch):
    monkeypatch.setenv('BETTER_PIPELINE_AUTO_COLLECT', 'false')
    repo = Repository('sqlite:///:memory:')
    bundle = make_demo_bundle('bet')
    bundle = replace(bundle, fixtures=SimpleNamespace(get_match=bundle.fixtures.get_match), portfolio=None)
    runtime = make_application(repository=repo, settings=Settings(analysis_requests_per_minute=100), providers=bundle)
    with TestClient(create_app(runtime)) as client:
        report = client.get('/salabet/automatic-multiples')
        assert report.status_code == 200
        assert report.json()['enabled'] is False
        request = {'request_id': str(uuid4()), 'enabled': True, 'plan_id': 'not-reviewed'}
        assert client.post('/salabet/automatic-multiples', json=request).status_code == 409
        assert client.post('/salabet/automatic-multiples', json=request, headers={'Origin': 'https://evil.example'}).status_code == 403
        assert client.post('/salabet/automatic-multiples', json={'request_id': str(uuid4()), 'enabled': 'yes'}).status_code == 422
        assert client.get('/data-pipeline').json()['automatic_multiples']['enabled'] is False
        assert not repo.all_records('bet_slips')
        assert not repo.all_records('annotations', entity_id='bank:EUR:v1')
    repo.close()
