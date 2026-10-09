import os
os.environ['SEED_MOCK_DATA'] = 'true'
os.environ['DEMO_IN_MEMORY'] = 'true'
os.environ['SCHEDULER_ENABLED'] = 'false'
import json
from datetime import timedelta, timezone
import pytest
import httpx
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient
from app.config import Config, DEFAULT_APIFY_ACTORS, APIFY_X_FALLBACK_ACTOR
from app.models import now
from app.services import bounds, today, create_alert, statuses, encrypt, decrypt, notify_negative_posts, within_automation_window
from app.sentiment import Classifier
from app.connectors import NEWS_CHANNELS, NEWS_SEARCH_QUERIES, matches, apify_posts, google_news_posts, _actor_input, _approved_news_source, ProviderError, request


def test_timezone_boundary():
    start, end = bounds('2026-09-28')
    assert start.isoformat() == '2026-09-27T18:30:00+00:00'
    assert end - start == timedelta(days=1)


def test_bootstrap_credentials_are_optional_for_existing_database():
    settings = Config(_env_file=None, seed_mock_data=False, demo_in_memory=False,
                      jwt_secret='x' * 32, encryption_key=Fernet.generate_key().decode(),
                      bootstrap_email='', bootstrap_password='')
    assert settings.bootstrap_email == '' and settings.bootstrap_password == ''


def test_encryption():
    secret = {'api_key': 'private-key-value'}
    ciphertext = encrypt(secret)
    assert 'private-key' not in ciphertext
    assert decrypt(ciphertext) == secret


def test_keyword_matching_hindi_and_pk():
    assert matches('जन सुराज पर चर्चा', ['जन सुराज'])
    assert matches('PK ki meeting', ['PK'])
    assert not matches('APK file download', ['PK'])


def test_token_only_apify_inputs_are_platform_specific():
    since = now() - timedelta(hours=2)
    keywords = ['Jan Suraaj', 'Prashant Kishore']
    instagram = _actor_input('instagram', keywords, since, 20)
    assert instagram['resultsType'] == 'posts'
    assert instagram['searchType'] == 'hashtag'
    assert instagram['search'] == 'JanSuraaj,PrashantKishore'
    assert instagram['onlyPostsNewerThan'].endswith('Z')
    x_input = _actor_input('x', keywords + ['PK', '#JanSuraaj'], since, 20)
    assert len(x_input['searchTerms']) == 1
    assert '"Jan Suraaj" OR "Prashant Kishore"' in x_input['searchTerms'][0]
    assert '("PK" Bihar)' in x_input['searchTerms'][0]
    assert '#JanSuraaj' in x_input['searchTerms'][0]
    assert f"since:{since.date().isoformat()}" in x_input['searchTerms'][0]
    assert x_input['start'] == since.date().isoformat()
    assert x_input['maxItems'] == 50
    assert x_input['sort'] == 'Latest + Top'
    assert DEFAULT_APIFY_ACTORS['x'] == 'apidojo~tweet-scraper'
    assert APIFY_X_FALLBACK_ACTOR == 'apidojo~twitter-scraper-lite'

    reddit = _actor_input('reddit', keywords + ['PK'], since, 20)
    assert reddit['queries'] == ['"Jan Suraaj" OR "Prashant Kishore"']
    assert reddit['sort'] == 'new'
    assert reddit['scrapeComments'] is False
    assert reddit['maxPosts'] == 20
    assert reddit['dateFrom'] == since.astimezone(timezone.utc).date().isoformat()
    assert DEFAULT_APIFY_ACTORS['reddit'] == 'fatihtahta~reddit-scraper-search-fast'


@pytest.mark.asyncio
async def test_x_apify_no_results_sentinel_is_not_reported_as_live():
    def handler(request):
        assert request.url.path.endswith('/run-sync-get-dataset-items')
        return httpx.Response(200, json=[{'noResults': True}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match='no tweet rows'):
            await apify_posts(
                client, 'x', {'api_key': 'token'}, ['Jan Suraaj'],
                now() - timedelta(hours=4),
            )


@pytest.mark.asyncio
async def test_x_apify_uses_bounded_lite_fallback_after_primary_failure():
    requests = []

    def handler(request):
        requests.append(request)
        if 'apidojo~tweet-scraper/' in request.url.path:
            return httpx.Response(400, json={'error': {'message': 'temporary rejection'}})
        return httpx.Response(200, json=[{
            'id': 'fallback-tweet', 'text': 'Jan Suraaj update',
            'createdAt': now().isoformat(), 'url': 'https://x.com/example/status/1',
            'author': {'name': 'Reporter'},
        }])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rows = await apify_posts(
            client, 'x', {'api_key': 'token'}, ['Jan Suraaj'],
            now() - timedelta(hours=4),
        )

    assert len(rows) == 1
    assert 'apidojo~tweet-scraper/' in requests[0].url.path
    assert 'apidojo~twitter-scraper-lite/' in requests[1].url.path
    assert requests[1].url.params['maxTotalChargeUsd'] == '0.1'


@pytest.mark.parametrize('source', [
    'Dainik Bhaskar', 'jagran.com', 'tv9hindi.com', 'prabhatkhabar.com',
    'Hindustan', 'ETV Bharat', 'Navbharat Times',
])
def test_expanded_bihar_news_sources_are_approved(source):
    assert _approved_news_source(source)


def test_similarly_named_unapproved_source_is_rejected():
    assert not _approved_news_source('Hindustan Times')


@pytest.mark.asyncio
async def test_webhook_accepts_empty_success_response():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(204))) as client:
        assert await request(client, 'POST', 'https://example.org/hook', response_json=False, json={'date': today()}) == {}


@pytest.mark.asyncio
async def test_negative_post_telegram_alert_is_idempotent(monkeypatch):
    db = AsyncMongoMockClient(tz_aware=True).test
    await db.posts.insert_one({
        'platform': 'facebook', 'external_id': 'negative-1', 'author': 'News Desk',
        'content': 'Jan Suraaj Party plan faces criticism',
        'url': 'https://example.org/post/negative-1', 'published_at': now(), 'demo': False,
        'sentiment': {'label': 'negative', 'confidence': .91},
        'telegram_notification': {'status': 'pending'},
    })
    class Settings:
        seed_mock_data = False
        telegram_bot_token = '123456:test_bot_token_value_long_enough'
        telegram_chat_id = '-1001234567890'
        reporting_timezone = 'Asia/Kolkata'
        automation_start_hour = 6
        automation_end_hour = 22
    monkeypatch.setattr('app.services.config', lambda: Settings())
    monkeypatch.setattr('app.services.within_automation_window', lambda *args, **kwargs: True)
    requests = []
    def handler(req):
        requests.append(req)
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 1}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await notify_negative_posts(db, client)
        await notify_negative_posts(db, client)
    stored = await db.posts.find_one({'external_id': 'negative-1'})
    assert stored['telegram_notification']['status'] == 'sent'
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload['chat_id'] == '-1001234567890'
    assert '<a href="https://example.org/post/negative-1">View on Facebook</a>' in payload['text']
    assert payload['parse_mode'] == 'HTML'
    assert payload['disable_web_page_preview'] is True


@pytest.mark.asyncio
async def test_reddit_apify_maps_post_body_and_skips_comments():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json=[
            {
                'kind': 'post', 'id': 'reddit-post-1',
                'title': 'Jan Suraaj discussion',
                'body': 'A long Prashant Kishore post body.',
                'author': 'bihar_analyst', 'score': 42, 'num_comments': 7,
                'created_utc': now().isoformat(),
                'canonical_url': 'https://www.reddit.com/r/bihar/comments/reddit-post-1/topic/',
            },
            {
                'kind': 'comment', 'id': 'reddit-comment-1',
                'body': 'Prashant Kishore comment', 'author': 'commenter',
                'created_utc': now().isoformat(),
                'url': 'https://www.reddit.com/r/bihar/comments/reddit-post-1/topic/comment/',
            },
        ])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rows = await apify_posts(
            client, 'reddit', {'api_key': 'token'},
            ['Jan Suraaj', 'Prashant Kishore'], now() - timedelta(hours=4),
        )

    assert len(rows) == 1
    assert rows[0]['platform'] == 'reddit'
    assert rows[0]['content'] == 'Jan Suraaj discussion\n\nA long Prashant Kishore post body.'
    assert rows[0]['author'] == 'bihar_analyst'
    assert rows[0]['engagement']['likes'] == 42
    assert rows[0]['engagement']['comments'] == 7
    assert rows[0]['content_scope'] == 'reddit-post-only-v1'
    payload = json.loads(captured[0].content)
    assert payload['scrapeComments'] is False


@pytest.mark.asyncio
async def test_old_negative_post_is_skipped_without_telegram_delivery(monkeypatch):
    db = AsyncMongoMockClient(tz_aware=True).test
    await db.posts.insert_one({
        'platform': 'youtube', 'external_id': 'old-negative', 'author': 'Channel',
        'content': 'Old negative title', 'url': 'https://youtu.be/old-negative',
        'published_at': now() - timedelta(days=1), 'demo': False,
        'sentiment': {'label': 'negative', 'confidence': .9},
        'telegram_notification': {'status': 'pending'},
    })
    class Settings:
        seed_mock_data = False
        telegram_bot_token = '123456:test_bot_token_value_long_enough'
        telegram_chat_id = '-1001234567890'
        reporting_timezone = 'Asia/Kolkata'
    monkeypatch.setattr('app.services.config', lambda: Settings())
    monkeypatch.setattr('app.services.within_automation_window', lambda *args, **kwargs: True)
    requests = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: requests.append(req) or httpx.Response(200, json={'ok': True}))) as client:
        await notify_negative_posts(db, client)
    stored = await db.posts.find_one({'external_id': 'old-negative'})
    assert stored['telegram_notification']['status'] == 'skipped'
    assert requests == []


def test_hf_label_mapping_and_low_confidence():
    from app.sentiment import result_from_scores, preprocess
    result = result_from_scores(['Positive', 'Negative', 'Neutral'], [.15, .6, .25], True)
    assert result.sentiment == 'negative' and result.confidence == .6
    assert 'Low model confidence' in result.reason and 'truncated' in result.reason
    assert preprocess('@analyst text https://example.org/a') == '@user text http'


@pytest.mark.parametrize('labels,scores', [
    (['LABEL_0', 'LABEL_1', 'LABEL_2'], [.2, .3, .5]),
    (['negative', 'neutral', 'positive'], [.2, .3, float('nan')]),
    (['negative', 'neutral', 'positive'], [.2, .3, 1.4]),
    (['negative', 'neutral', 'positive'], [.2, .3, .1]),
])
def test_hf_rejects_invalid_model_output(labels, scores):
    from app.sentiment import result_from_scores
    with pytest.raises(ValueError):
        result_from_scores(labels, scores)


@pytest.mark.asyncio
async def test_hf_batch_validation_and_failure_status(monkeypatch):
    engine = Classifier()
    monkeypatch.setattr(engine, '_run', lambda texts: [])
    with pytest.raises(ValueError):
        await engine.classify(['a', 'b'])
    assert engine.status == 'unavailable' and 'pending' in engine.error


@pytest.mark.asyncio
async def test_hf_inference_off_event_loop(monkeypatch):
    import threading
    from app.sentiment import Result
    engine = Classifier()
    main_thread = threading.get_ident()
    def run(texts):
        assert threading.get_ident() != main_thread
        return [Result(sentiment='neutral', confidence=.8, reason='Test classification') for _ in texts]
    monkeypatch.setattr(engine, '_run', run)
    assert len(await engine.classify(['English text', 'Hindi text'])) == 2
    assert engine.status == 'live'
    assert '@' in engine.provenance
    assert await engine.classify([]) == []


@pytest.mark.asyncio
async def test_alert_strict_threshold_and_idempotence():
    db = AsyncMongoMockClient(tz_aware=True).test
    await db.alerts.create_index('date', unique=True)
    await db.settings.insert_one({'_id': 'main', 'threshold': 500})
    await db.daily_aggregates.insert_one({'date': today(), 'platform': 'overall', 'negative_count': 500})
    await create_alert(db, today())
    assert await db.alerts.count_documents({}) == 0
    await db.daily_aggregates.update_one({}, {'$set': {'negative_count': 501}})
    await create_alert(db, today()); await create_alert(db, today())
    assert await db.alerts.count_documents({}) == 1
    await db.alerts.update_one({}, {'$set': {'resolved': True}})
    await create_alert(db, today())
    assert (await db.alerts.find_one({}))['resolved'] is True


@pytest.mark.asyncio
async def test_apify_sources_unconfigured_are_disconnected():
    result = await statuses(AsyncMongoMockClient().test)
    for p in ['facebook', 'instagram']:
        s = next(x for x in result if x['platform'] == p)
        assert s['source'] == 'Not connected' and s['status'] == 'disconnected'


@pytest.mark.asyncio
async def test_apify_actor_normalizes_and_filters_items():
    requests = []
    def handler(req):
        requests.append(req)
        if 'facebook-search-scraper' in str(req.url):
            return httpx.Response(200, json=[
                {'pageName': 'Jan Suraaj', 'facebookUrl': 'https://www.facebook.com/jansuraaj'},
            ])
        return httpx.Response(200, json=[
            {'postId': '42', 'text': 'Jan Suraaj Party rally update', 'time': now().isoformat(),
             'postUrl': 'https://example.org/post/42', 'pageName': 'Reporter', 'reactionCount': '12'},
            {'postId': '43', 'text': 'Unrelated post', 'createdAt': now().isoformat()},
        ])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rows = await apify_posts(client, 'facebook', {'api_key': 'apify-token-value'},
            ['Jan Suraaj'], now() - timedelta(hours=1))
    assert len(rows) == 1 and rows[0]['external_id'] == '42'
    assert rows[0]['engagement']['likes'] == 12 and rows[0]['source_provider'] == 'apify'
    assert requests[0].headers['Authorization'] == 'Bearer apify-token-value'
    assert '/acts/apify~facebook-search-scraper/run-sync-get-dataset-items' in str(requests[0].url)
    assert '/acts/apify~facebook-posts-scraper/run-sync-get-dataset-items' in str(requests[1].url)
    assert json.loads(requests[1].content)['startUrls'] == [{'url': 'https://www.facebook.com/jansuraaj'}]


@pytest.mark.asyncio
async def test_apify_normalizes_serialized_facebook_author():
    def handler(req):
        if 'facebook-search-scraper' in str(req.url):
            return httpx.Response(200, json=[{'facebookUrl': 'https://facebook.com/jagaritbihar'}])
        return httpx.Response(200, json=[{
            'postId': 'fb-1', 'text': 'Jan Suraaj Party teachers update',
            'createdAt': now().isoformat(),
            'author': "{'id': '123', 'name': 'Jagarit Bihar', 'profilePic': 'https://example.org/long.jpg'}",
        }])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rows = await apify_posts(client, 'facebook', {'api_key': 'token'},
                                 ['Jan Suraaj'], now() - timedelta(hours=1))
    assert rows[0]['author'] == 'Jagarit Bihar'


@pytest.mark.asyncio
async def test_google_news_rss_input_and_output_mapping():
    from email.utils import format_datetime
    captured = []
    published = format_datetime(now())
    def handler(req):
        captured.append(req)
        return httpx.Response(200, text=f'''<?xml version="1.0" encoding="UTF-8"?>
          <rss version="2.0"><channel><item>
          <title>Prashant Kishore addresses Bihar rally - News18 Bihar Jharkhand</title>
          <link>https://news.google.com/rss/articles/story</link>
          <guid>rss-story-1</guid><pubDate>{published}</pubDate>
          <source url="https://news18.com">News18 Bihar Jharkhand</source>
          </item></channel></rss>''')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rows = await google_news_posts(client, ['Jan Suraaj', 'Prashant Kishore'], now() - timedelta(days=1))
    assert len(captured) == len(NEWS_SEARCH_QUERIES)
    assert all(req.url.host == 'news.google.com' for req in captured)
    assert all(req.url.params['ceid'] == 'IN:hi' for req in captured)
    assert all(not any(channel in req.url.params['q'] for channel in NEWS_CHANNELS)
               for req in captured)
    assert rows[0]['author'] == 'News18 Bihar Jharkhand'
    assert rows[0]['url'] == 'https://news.google.com/rss/articles/story'
    assert rows[0]['content'] == 'Prashant Kishore addresses Bihar rally'
    assert rows[0]['source_provider'] == 'google-news-rss'


def test_automation_window_uses_ist(monkeypatch):
    from datetime import datetime, timezone
    class Settings:
        reporting_timezone = 'Asia/Kolkata'
        automation_start_hour = 6
        automation_end_hour = 22
    monkeypatch.setattr('app.services.config', lambda: Settings())
    assert not within_automation_window(datetime(2026, 10, 4, 0, 29, tzinfo=timezone.utc))  # 05:59 IST
    assert within_automation_window(datetime(2026, 10, 4, 0, 30, tzinfo=timezone.utc))
    assert within_automation_window(datetime(2026, 10, 4, 16, 30, tzinfo=timezone.utc))
    assert not within_automation_window(datetime(2026, 10, 4, 17, 30, tzinfo=timezone.utc))


@pytest.mark.asyncio
async def test_youtube_daily_quota_error_is_reported_without_retries():
    calls = 0

    def handler(req):
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={'error': {
            'message': "Quota exceeded for quota metric 'Search Queries' and limit 'Search Queries per day'",
            'errors': [{'reason': 'rateLimitExceeded'}],
        }})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match='daily search quota exhausted'):
            await request(client, 'GET', 'https://www.googleapis.com/youtube/v3/search')

    assert calls == 1
