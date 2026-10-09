import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.connectors import youtube_posts
from app.gemini_fallback import GeminiFallback
from app.models import now
from app.sentiment import Classifier, Result, apply_target_context, detect_targets


@pytest.mark.asyncio
async def test_low_confidence_and_all_hf_negatives_route_to_gemini(monkeypatch):
    settings = SimpleNamespace(
        hf_confidence_threshold=.80, hf_model='hf', hf_revision='rev',
        gemini_model='gemini-test', gemini_api_key='test', gemini_concurrency=2,
    )
    engine = Classifier(settings)
    monkeypatch.setattr(engine, '_run', lambda texts: [
        Result(sentiment='neutral', confidence=.80, reason='HF'),
        Result(sentiment='neutral', confidence=.799, reason='HF'),
        Result(sentiment='negative', confidence=.95, reason='HF'),
    ])

    class Fallback:
        last_error = None

        async def classify(self, texts):
            assert texts == ['low', 'hf-negative']
            return [
                Result(sentiment='negative', confidence=.88, reason='Target criticism'),
                Result(sentiment='positive', confidence=.93, reason='Target expressed sympathy'),
            ]

    engine._gemini = Fallback()
    results = await engine.classify(['boundary', 'low', 'hf-negative'])
    assert [r.model_used for r in results] == [
        'hf@rev', 'gemini/gemini-test', 'gemini/gemini-test',
    ]
    assert results[1].hf_confidence == .799
    assert results[1].sentiment == 'negative'
    assert results[2].hf_confidence == .95
    assert results[2].sentiment == 'positive'


@pytest.mark.parametrize('title', [
    'अपराध पर दुख जताने के बजाय मजाक? अशोक चौधरी के बयान पर भड़के पीके! #JanSuraj',
    'जमुई कांड पर अशोक चौधरी के बेतुके बयान पर भड़के पीके! #JanSuraj #JamuiIncident',
    'राजधानी पटना में दिनदहाड़े गोलियां, डीजीपी तक नहीं! #JanSuraaj #PatnaFiring',
    'बिहार में 50 लाख बाढ़ पीड़ितों पर मोदी-शाह चुप क्यों? #JanSuraj #BiharFlood',
])
def test_negative_event_with_campaign_hashtag_routes_to_target_verifier(title):
    result = apply_target_context(
        title, Result(sentiment='negative', confidence=.95, reason='Overall negative'), .80,
    )
    assert result.confidence == .79
    assert result.hf_confidence is None
    assert 'jan_suraaj' in result.targets
    assert 'Gemini verification' in result.reason


def test_direct_target_criticism_keeps_hf_candidate_confidence_before_verification():
    result = apply_target_context(
        'Jan Suraaj ने जनता को निराश किया',
        Result(sentiment='negative', confidence=.94, reason='Overall negative'), .80,
    )
    assert result.confidence == .94


def test_devanagari_target_detection():
    text = 'जन सुराज और प्रशांत किशोर'
    assert detect_targets(text) == ['jan_suraaj', 'prashant_kishore']
    assert detect_targets('भड़के पीके') == ['prashant_kishore']


@pytest.mark.asyncio
async def test_unavailable_fallback_keeps_high_results_and_flags_low(monkeypatch):
    settings = SimpleNamespace(
        hf_confidence_threshold=.80, hf_model='hf', hf_revision='rev',
        gemini_model='gemini-test', gemini_api_key='', gemini_concurrency=2,
    )
    engine = Classifier(settings)
    monkeypatch.setattr(engine, '_run', lambda texts: [
        Result(sentiment='neutral', confidence=c, reason='HF') for c in [.79, .9]])
    results = await engine.classify(['low', 'high'])
    assert results[0].pending and not results[1].pending
    assert engine.status == 'partial'


@pytest.mark.asyncio
async def test_mismatched_gemini_batch_retries_individually(monkeypatch):
    settings = SimpleNamespace(
        gemini_api_key='test', gemini_concurrency=2, gemini_model='gemini-test',
    )
    engine = GeminiFallback(settings)
    calls = []

    async def fake(client, texts):
        calls.append(texts)
        if len(texts) > 1:
            raise ValueError('Mismatch')
        return [Result(sentiment='neutral', confidence=.85, reason=texts[0])]

    monkeypatch.setattr(engine, '_request', fake)
    results = await engine.classify(['one', 'two'])
    assert [r.reason for r in results] == ['one', 'two']
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_gemini_rejects_wrong_ids():
    engine = GeminiFallback(SimpleNamespace(
        gemini_api_key='test', gemini_concurrency=1, gemini_model='gemini-test',
    ))
    payload = {'results': [
        {'id': 9, 'sentiment': 'neutral', 'confidence': .8, 'reason': 'Fact'},
    ]}

    def handler(request):
        return httpx.Response(200, json={'candidates': [{
            'finishReason': 'STOP',
            'content': {'parts': [{'text': json.dumps(payload)}]},
        }]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await engine._request(client, ['text'])


@pytest.mark.asyncio
async def test_gemini_accepts_mixed_label_and_uses_structured_output():
    engine = GeminiFallback(SimpleNamespace(
        gemini_api_key='test', gemini_concurrency=1, gemini_model='gemini-test',
    ))
    payload = {'results': [{
        'id': 0, 'sentiment': 'mixed', 'confidence': .9,
        'reason': 'Praise and criticism both target the entity.',
    }]}

    def handler(request):
        body = json.loads(request.content)
        assert request.headers['x-goog-api-key'] == 'test'
        assert body['generationConfig']['responseMimeType'] == 'application/json'
        assert body['generationConfig']['responseSchema']['required'] == ['results']
        return httpx.Response(200, json={'candidates': [{
            'finishReason': 'STOP',
            'content': {'parts': [{'text': json.dumps(payload)}]},
        }]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await engine._request(client, ['Good ideas, poor execution'])
    assert result[0].sentiment == 'mixed' and result[0].confidence == .9


@pytest.mark.asyncio
async def test_youtube_classifies_title_only_never_description_or_comments():
    paths = []

    def handler(req):
        paths.append(req.url.path)
        if req.url.path.endswith('/search'):
            return httpx.Response(200, json={'items': [
                {'id': {'videoId': 'abc'}}, {'id': {'videoId': 'description-only'}},
            ]})
        assert req.url.path.endswith('/videos')
        assert req.url.params['part'] == 'snippet,statistics,contentDetails'
        return httpx.Response(200, json={'items': [
            {'id': 'abc', 'snippet': {
                'title': 'Prashant Kishore meeting',
                'description': 'Full description, not a search excerpt.',
                'channelTitle': 'News', 'publishedAt': '2026-10-01T00:00:00Z'},
             'contentDetails': {'duration': 'PT45S'},
             'statistics': {'likeCount': '4', 'viewCount': '90', 'commentCount': '99'}},
            {'id': 'description-only', 'snippet': {
                'title': 'Unrelated daily bulletin',
                'description': 'Prashant Kishore appears only here.',
                'channelTitle': 'News', 'publishedAt': '2026-10-01T00:00:00Z'},
             'statistics': {}},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rows = await youtube_posts(
            client, {'api_key': 'test'}, ['Prashant Kishore'], now() - timedelta(days=1),
        )
    assert paths.count('/youtube/v3/search') == 1 and len(rows) == 1
    assert rows[0]['content'] == 'Prashant Kishore meeting'
    assert 'description' not in rows[0]
    assert rows[0]['engagement']['comments'] == 0
    assert rows[0]['engagement']['views'] == 90
    assert rows[0]['content_scope'] == 'youtube-title-only-v2'
    assert rows[0]['content_type'] == 'short'


@pytest.mark.asyncio
async def test_youtube_rotates_to_backup_key_only_after_quota_exhaustion():
    keys = []

    def handler(req):
        key = req.url.params['key']
        keys.append(key)
        if key == 'primary':
            return httpx.Response(403, json={'error': {
                'message': 'The request cannot be completed because you have exceeded your quota.',
                'errors': [{'reason': 'quotaExceeded'}],
            }})
        if req.url.path.endswith('/search'):
            return httpx.Response(200, json={'items': [{'id': {'videoId': 'backup-video'}}]})
        return httpx.Response(200, json={'items': [{
            'id': 'backup-video',
            'snippet': {'title': 'Jan Suraaj update', 'channelTitle': 'News',
                        'publishedAt': '2026-10-06T06:00:00Z'},
            'statistics': {}, 'contentDetails': {'duration': 'PT5M'},
        }]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rows = await youtube_posts(client, {
            'api_key': 'primary', 'backup_api_key': 'backup',
        }, ['Jan Suraaj'], now() - timedelta(days=1))

    assert keys == ['primary', 'backup', 'backup']
    assert len(rows) == 1 and rows[0]['external_id'] == 'backup-video'
