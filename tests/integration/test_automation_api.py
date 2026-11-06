from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from better.api.app import create_app
from better.config import Settings
from better.providers.fake import make_demo_bundle
from better.services.application import make_application
from better.storage.repository import Repository


def test_controls_api_persists_limits_and_toggle_without_financial_movements(monkeypatch):
    monkeypatch.setenv('BETTER_PIPELINE_AUTO_COLLECT','false')
    repo=Repository('sqlite:///:memory:')
    bundle=make_demo_bundle('bet')
    bundle=replace(bundle,fixtures=SimpleNamespace(get_match=bundle.fixtures.get_match),portfolio=None)
    app=make_application(repository=repo,settings=Settings(analysis_requests_per_minute=100),providers=bundle)
    with TestClient(create_app(app)) as client:
        before=client.get('/bank').json()
        request={'request_id':str(uuid4()),'per_match_eur':'0.25','per_day_eur':'5','per_week_eur':'25'}
        response=client.post('/bank/research-budget',json=request)
        assert response.status_code==200,response.text
        assert response.json()['currency']=='EUR'
        for enabled in (True,False):
            payload={'request_id':str(uuid4()),'enabled':enabled}
            response=client.post('/automation',json=payload)
            assert response.status_code==200,response.text
            assert response.json()['enabled'] is enabled
            assert client.post('/automation',json=payload).status_code==200
            assert client.get('/data-pipeline').json()['timing_comparison']['enabled'] is enabled
        assert client.post('/bank/research-budget',json={**request,'request_id':str(uuid4()),'per_day_eur':'-1'}).status_code==422
        assert client.post('/automation',json={'request_id':str(uuid4()),'enabled':True},headers={'Origin':'https://evil.example'}).status_code==403
        after=client.get('/bank').json()
        assert {k:v for k,v in before.items() if k!='research_budget'}=={k:v for k,v in after.items() if k!='research_budget'}
        assert not repo.all_records('bet_slips')
        assert not repo.all_records('annotations',entity_id='bank:EUR:v1')
    repo.close()
