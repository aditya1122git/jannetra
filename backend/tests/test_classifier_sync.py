import pytest
from mongomock_motor import AsyncMongoMockClient
from app import services
from app.models import now
from app.sentiment import Result


@pytest.mark.asyncio
@pytest.mark.parametrize('fails', [False, True])
async def test_local_classifier_only_processes_pending_live_posts(monkeypatch, fails):
    db = AsyncMongoMockClient(tz_aware=True).test
    await db.posts.insert_many([
        {'platform': 'x', 'external_id': 'pending', 'sentiment': None, 'demo': False,
         'content': 'A new post', 'published_at': now()},
        {'external_id': 'classified', 'sentiment': {'label': 'positive'}, 'demo': False, 'content': 'Old post'},
        {'external_id': 'demo', 'sentiment': None, 'demo': True, 'content': 'Synthetic post'},
    ])
    class Settings:
        seed_mock_data = False
        hf_batch_size = 16
        reporting_timezone = 'Asia/Kolkata'
    class Engine:
        provenance = 'public/model@pinned-commit'
        async def classify(self, texts):
            assert texts == ['A new post']
            if fails:
                raise RuntimeError('Model unavailable')
            return [Result(sentiment='negative', confidence=.8, reason='Test inference')]
    async def no_op(*args):
        pass
    monkeypatch.setattr(services, 'config', lambda: Settings())
    monkeypatch.setattr(services, 'classifier', Engine)
    monkeypatch.setattr(services, 'rollup', no_op)
    monkeypatch.setattr(services, 'notify_negative_posts', no_op)
    await services.sync(db, None)
    pending = await db.posts.find_one({'platform': 'x', 'external_id': 'pending'})
    if fails:
        assert pending['sentiment'] is None
    else:
        assert pending['sentiment']['label'] == 'negative'
        assert pending['sentiment']['model_used'] == Engine.provenance
        assert pending['telegram_notification']['status'] == 'pending'
    assert (await db.posts.find_one({'external_id': 'classified'}))['sentiment'] == {'label': 'positive'}
    assert (await db.posts.find_one({'external_id': 'demo'}))['sentiment'] is None


@pytest.mark.asyncio
async def test_completed_post_is_not_classified_again_on_later_sync(monkeypatch):
    db = AsyncMongoMockClient(tz_aware=True).test
    await db.posts.insert_one({
        'platform': 'x', 'external_id': 'once', 'sentiment': None, 'demo': False,
        'content': 'Only classify this once', 'published_at': now(),
    })

    class Settings:
        seed_mock_data = False
        hf_batch_size = 16
        reporting_timezone = 'Asia/Kolkata'
        enabled_platforms = 'x'

    class Engine:
        provenance = 'public/model@pinned-commit'
        calls = 0

        async def classify(self, texts):
            self.calls += 1
            assert self.calls == 1
            assert texts == ['Only classify this once']
            return [Result(sentiment='positive', confidence=.9, reason='Test inference')]

    engine = Engine()

    async def no_op(*args):
        pass

    monkeypatch.setattr(services, 'config', lambda: Settings())
    monkeypatch.setattr(services, 'classifier', lambda: engine)
    monkeypatch.setattr(services, 'rollup', no_op)
    monkeypatch.setattr(services, 'notify_negative_posts', no_op)

    await services.sync(db, None)
    await services.sync(db, None)

    stored = await db.posts.find_one({'external_id': 'once'})
    assert stored['sentiment']['label'] == 'positive'
    assert engine.calls == 1


@pytest.mark.asyncio
async def test_schema_v6_reverifies_existing_hf_negative_once(monkeypatch):
    db = AsyncMongoMockClient(tz_aware=True).test
    await db.posts.insert_one({
        'platform': 'news', 'external_id': 'condolence', 'demo': False,
        'content': 'Nana Patekar ke nidhan par Jan Suraaj ne jataya shok',
        'published_at': now(),
        'sentiment': {
            'label': 'negative', 'confidence': .80,
            'model_used': 'cardiffnlp/model@old',
        },
        'sentiment_schema_version': 5,
    })

    class Settings:
        seed_mock_data = False
        hf_batch_size = 16
        reporting_timezone = 'Asia/Kolkata'
        enabled_platforms = 'news'

    class Engine:
        provenance = 'public/model@pinned-commit'
        calls = 0

        async def classify(self, texts):
            self.calls += 1
            assert texts == ['Nana Patekar ke nidhan par Jan Suraaj ne jataya shok']
            return [Result(
                sentiment='positive', confidence=.94,
                reason='Jan Suraaj is framed expressing sympathy.',
                model_used='gemini/gemini-test', hf_confidence=.80,
            )]

    engine = Engine()

    async def no_op(*args):
        pass

    monkeypatch.setattr(services, 'config', lambda: Settings())
    monkeypatch.setattr(services, 'classifier', lambda: engine)
    monkeypatch.setattr(services, 'rollup', no_op)
    monkeypatch.setattr(services, 'notify_negative_posts', no_op)

    await services.sync(db, None)
    await services.sync(db, None)

    stored = await db.posts.find_one({'external_id': 'condolence'})
    assert stored['sentiment']['label'] == 'positive'
    assert stored['sentiment']['model_used'] == 'gemini/gemini-test'
    assert stored['sentiment_schema_version'] == 6
    assert engine.calls == 1
