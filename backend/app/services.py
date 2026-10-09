import asyncio
import html
import json
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from cryptography.fernet import Fernet
from pymongo.errors import DuplicateKeyError
from .config import config, DEFAULT_AUTOMATION_START_HOUR, DEFAULT_AUTOMATION_END_HOUR
from .models import now
from .connectors import apify_posts, google_news_posts, youtube_posts, request
from .sentiment import classifier

POST_SCOPE = {'$or': [{'platform': {'$ne': 'youtube'}}, {'demo': True}, {'content_scope': 'youtube-title-only-v2'}]}

PLATFORMS = ['facebook', 'instagram', 'x', 'youtube', 'news', 'reddit']
DEFAULT_KEYWORD_VARIANTS = ['Jan Suraj Party', 'Jan Suraaj Party', 'Jan Suraj', 'Jan Suraaj',
                            '#JanSuraj', '#JanSuraaj']
KEYWORDS = ['Jan Suraaj Party', 'Prashant Kishore', 'PK', 'जन सुराज', 'प्रशांत किशोर', '#JanSuraaj', '#PrashantKishore']

def today():
    return now().astimezone(ZoneInfo(config().reporting_timezone)).date().isoformat()

def bounds(day):
    start = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(config().reporting_timezone))
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def within_automation_window(value=None, start_hour=None, end_hour=None):
    """Return true during the inclusive operating hours."""
    local = (value or now()).astimezone(ZoneInfo(config().reporting_timezone))
    start = DEFAULT_AUTOMATION_START_HOUR if start_hour is None else start_hour
    end = DEFAULT_AUTOMATION_END_HOUR if end_hour is None else end_hour
    return start <= local.hour <= end

def encrypt(value):
    return Fernet(config().encryption_key.encode()).encrypt(json.dumps(value).encode()).decode()

def decrypt(value):
    return json.loads(Fernet(config().encryption_key.encode()).decrypt(value.encode()))

async def settings(db):
    return await db.settings.find_one({'_id': 'main'}) or {
        'threshold': 500,
        'automation_start_hour': DEFAULT_AUTOMATION_START_HOUR,
        'automation_end_hour': DEFAULT_AUTOMATION_END_HOUR,
    }

async def audit(db, user, action, meta=None):
    await db.audit_logs.insert_one(dict(user_id=str(user['_id']), action=action, timestamp=now(), meta=meta or {}))

async def statuses(db):
    credentials = {c['platform']: c async for c in db.platform_credentials.find({}, {'encrypted_api_key': 0})}
    result = []
    for p in PLATFORMS:
        c = credentials.get(p) if p in config().enabled_platforms.split(',') else None
        if config().seed_mock_data and p in ['x', 'youtube']:
            result.append(dict(platform=p, source='Demo', status='demo', last_synced_at=now()))
        elif c:
            source = 'Google News RSS' if p == 'news' else 'Apify Actor' if c['mode'] == 'apify' else 'Live API'
            result.append(dict(platform=p, source=source,
                               status=c.get('status', 'pending'), last_synced_at=c.get('last_synced_at'), error=c.get('error')))
        else:
            result.append(dict(platform=p, source='Not connected', status='disconnected', last_synced_at=None))
    return result

async def rollup(db):
    connected = [s['platform'] for s in await statuses(db) if s['status'] != 'disconnected']
    start = now() - timedelta(days=35)
    match = {**POST_SCOPE, 'published_at': {'$gte': start}, 'platform': {'$in': connected}, 'sentiment': {'$ne': None}, 'demo': config().seed_mock_data}
    if config().demo_in_memory:
        # mongomock has no $dateTrunc. Only the explicit disposable demo uses this branch.
        grouped = defaultdict(lambda: dict(positive_count=0, negative_count=0, neutral_count=0, mixed_count=0, total_count=0))
        async for p in db.posts.find(match):
            day = p['published_at'].astimezone(ZoneInfo(config().reporting_timezone)).date().isoformat()
            g = grouped[(day, p['platform'])]
            g[p['sentiment']['label'] + '_count'] += 1; g['total_count'] += 1
        rows = [dict(date=k[0], platform=k[1], **v) for k, v in grouped.items()]
    else:
        pipeline = [{'$match': match}, {'$group': {
            '_id': {'date': {'$dateTrunc': {'date': '$published_at', 'unit': 'day', 'timezone': config().reporting_timezone}}, 'platform': '$platform'},
            **{label + '_count': {'$sum': {'$cond': [{'$eq': ['$sentiment.label', label]}, 1, 0]}} for label in ['positive', 'negative', 'neutral', 'mixed']},
            'total_count': {'$sum': 1}}}]
        raw = await db.posts.aggregate(pipeline).to_list(None)
        rows = [dict(date=r['_id']['date'].astimezone(ZoneInfo(config().reporting_timezone)).date().isoformat(),
                     platform=r['_id']['platform'], **{k: v for k, v in r.items() if k != '_id'}) for r in raw]
    overall = defaultdict(lambda: dict(positive_count=0, negative_count=0, neutral_count=0, mixed_count=0, total_count=0))
    for row in rows:
        for k in overall[row['date']]:
            overall[row['date']][k] += row[k]
    rows += [dict(date=d, platform='overall', **v) for d, v in overall.items()]
    for row in rows:
        row['negativity_index'] = round(row['negative_count'] / row['total_count'] * 100, 1) if row['total_count'] else 0
        await db.daily_aggregates.update_one({'date': row['date'], 'platform': row['platform']}, {'$set': {**row, 'schema_version': 1, 'content_scope': 'youtube-title-only-v2'}}, upsert=True)

async def create_alert(db, day):
    prefs = await settings(db)
    row = await db.daily_aggregates.find_one({'date': day, 'platform': 'overall'})
    if not row or row['negative_count'] <= prefs['threshold']:
        return
    start, end = bounds(day)
    connected = [s['platform'] for s in await statuses(db) if s['status'] != 'disconnected']
    top = await db.posts.find({**POST_SCOPE, 'published_at': {'$gte': start, '$lt': end}, 'sentiment.label': 'negative',
                              'platform': {'$in': connected}, 'demo': config().seed_mock_data}).sort('engagement_score', -1).limit(5).to_list(5)
    for p in top:
        p['_id'] = str(p['_id'])
    breakdown = {r['platform']: r['negative_count'] async for r in db.daily_aggregates.find({'date': day, 'platform': {'$in': connected}})}
    try:
        await db.alerts.update_one({'date': day}, {'$setOnInsert': dict(schema_version=1, date=day, threshold=prefs['threshold'],
                                  triggered_at=now(), notified_channels=[], resolved=False),
                                  '$set': dict(negative_count=row['negative_count'], top_negative_posts=top, platform_breakdown=breakdown,
                                               content_scope='youtube-title-only-v2')}, upsert=True)
    except DuplicateKeyError:
        pass

async def notify_negative_posts(db, client):
    """Send one idempotent Telegram message for every newly classified negative post."""
    c = config()
    prefs = await settings(db)
    if (c.seed_mock_data or not c.telegram_bot_token or not c.telegram_chat_id
            or not within_automation_window(
                start_hour=prefs.get('automation_start_hour', DEFAULT_AUTOMATION_START_HOUR),
                end_hour=prefs.get('automation_end_hour', DEFAULT_AUTOMATION_END_HOUR))):
        return
    start, end = bounds(today())
    await db.posts.update_many({
        'telegram_notification.status': {'$in': ['pending', 'failed']},
        '$or': [{'published_at': {'$lt': start}}, {'published_at': {'$gte': end}}],
    }, {'$set': {'telegram_notification.status': 'skipped',
                 'telegram_notification.error': 'Only posts published today are alerted'}})
    query = {'sentiment.label': 'negative', 'demo': False,
             'published_at': {'$gte': start, '$lt': end},
             'telegram_notification.status': {'$in': ['pending', 'failed']}}
    async for post_row in db.posts.find(query).sort('published_at', 1).limit(100):
        claim = await db.posts.update_one({
            '_id': post_row['_id'],
            'telegram_notification.status': {'$in': ['pending', 'failed']},
            '$or': [{'telegram_notification.lease_until': {'$exists': False}},
                    {'telegram_notification.lease_until': {'$lt': now()}}],
        }, {'$set': {'telegram_notification.status': 'sending',
                     'telegram_notification.lease_until': now() + timedelta(minutes=5)}})
        if not claim.modified_count:
            continue
        url = str(post_row.get('url') or '').strip()
        if not url.startswith(('https://', 'http://')):
            await db.posts.update_one({'_id': post_row['_id']}, {'$set': {
                'telegram_notification.status': 'skipped',
                'telegram_notification.error': 'Source post link unavailable'}})
            continue
        sentiment = post_row.get('sentiment') or {}
        confidence = round(float(sentiment.get('confidence', 0)) * 100)
        platform = str(post_row.get('platform') or 'unknown')
        platform_name = {
            'x': 'X', 'youtube': 'YouTube', 'reddit': 'Reddit',
            'facebook': 'Facebook', 'instagram': 'Instagram', 'news': 'News',
        }.get(platform, platform.title())
        content = html.escape(' '.join(str(post_row.get('content') or '').split())[:800])
        author = html.escape(str(post_row.get('author') or 'Unknown'))
        link_label = f'View on {platform_name}'
        link = f'<a href="{html.escape(url, quote=True)}">{link_label}</a>'
        message = (f'?? <b>JanNetra negative post</b>\n\n'
                   f'Platform: {platform_name}\n'
                   f'Author: {author}\n'
                   f'Confidence: {confidence}%\n\n'
                   f'{content}\n\n{link}')
        try:
            response = await request(client, 'POST',
                f'https://api.telegram.org/bot{c.telegram_bot_token}/sendMessage',
                json={'chat_id': c.telegram_chat_id, 'text': message,
                      'parse_mode': 'HTML', 'disable_web_page_preview': True})
            if not isinstance(response, dict) or not response.get('ok'):
                raise RuntimeError('Telegram rejected the message')
            await db.posts.update_one({'_id': post_row['_id']}, {
                '$set': {'telegram_notification.status': 'sent',
                         'telegram_notification.sent_at': now()},
                '$unset': {'telegram_notification.error': '',
                           'telegram_notification.lease_until': ''}})
        except Exception:
            await db.posts.update_one({'_id': post_row['_id']}, {
                '$set': {'telegram_notification.status': 'failed',
                         'telegram_notification.error': 'Delivery failed; retry scheduled'}})

_sync_lock = asyncio.Lock()


def sync_running():
    """Report whether an ingestion/classification cycle currently owns the lock."""
    return _sync_lock.locked()

async def scheduled_sync(db, client, platforms):
    """Run without any logged-in user, but only inside the operating window."""
    prefs = await settings(db)
    if not within_automation_window(
            start_hour=prefs.get('automation_start_hour', DEFAULT_AUTOMATION_START_HOUR),
            end_hour=prefs.get('automation_end_hour', DEFAULT_AUTOMATION_END_HOUR)):
        return
    await sync(db, client, platforms=platforms)


async def sync(db, client, platforms=None):
    if _sync_lock.locked():
        return
    async with _sync_lock:
        if not config().seed_mock_data:
            enabled = getattr(config(), 'enabled_platforms', ','.join(PLATFORMS)).split(',')
            selected = [p for p in (platforms or enabled) if p in enabled]
            keywords = [k['keyword'] async for k in db.tracked_keywords.find({'is_active': True})]
            if keywords:
                async for c in db.platform_credentials.find({'platform': {'$in': selected}}):
                    p = c['platform']
                    started = now()
                    await db.platform_credentials.update_one({'_id': c['_id']}, {'$set': {'last_attempt_at': started}})
                    try:
                        secret = decrypt(c['encrypted_api_key'])
                        if p == 'youtube' and c.get('search_strategy') != 'keyword-and-short-v3':
                            since = now() - timedelta(days=config().youtube_initial_lookback_days)
                        elif p == 'news' and c.get('search_strategy') != 'entity-rss-v3':
                            since = now() - timedelta(days=7)
                        else:
                            since = c.get('last_synced_at') or now() - timedelta(hours=24)
                        since -= timedelta(minutes=5)  # deliberate overlap; compound unique index deduplicates
                        if p == 'youtube':
                            rows = await youtube_posts(client, secret, keywords, since)
                        elif p == 'news':
                            rows = await google_news_posts(client, keywords, since)
                        else:
                            if c['mode'] != 'apify':
                                raise RuntimeError('This source must be configured with Apify')
                            rows = await apify_posts(client, p, secret, keywords, since)
                        for row in rows:
                            if p == 'youtube':
                                # Upgrade legacy search snippets to full video content once.
                                await db.posts.update_one({'platform': p, 'external_id': row['external_id'],
                                    'content_scope': {'$ne': 'youtube-title-only-v2'}},
                                    {'$set': row, '$unset': {'description': ''}})
                            await db.posts.update_one({'platform': p, 'external_id': row['external_id']}, {'$setOnInsert': row}, upsert=True)
                        credential_update = {'last_synced_at': started, 'status': 'live', 'error': None,
                                             'encrypted_api_key': encrypt(secret)}
                        if p == 'youtube':
                            credential_update['search_strategy'] = 'keyword-and-short-v3'
                        elif p == 'news':
                            credential_update['search_strategy'] = 'entity-rss-v3'
                        await db.platform_credentials.update_one({'_id': c['_id']}, {'$set': credential_update})
                    except Exception as exc:
                        await db.platform_credentials.update_one({'_id': c['_id']}, {'$set': {'status': 'unavailable',
                            'error': str(exc) if isinstance(exc, RuntimeError) else 'Connection failed; check configuration'}})
            engine = classifier()
            try:
                # Schema v6 makes Gemini verification mandatory for every HF
                # negative. Re-open older HF-only negatives once so existing
                # overall-tone mistakes are corrected as well.
                await db.posts.update_many({
                    **POST_SCOPE,
                    'demo': False,
                    'sentiment.label': 'negative',
                    'sentiment.model_used': {'$not': {'$regex': '^gemini/'}},
                    'sentiment_schema_version': {'$ne': 6},
                }, {
                    '$set': {'sentiment': None, 'classification_status': 'pending_reverification'},
                    '$unset': {'classification_error': '', 'hf_candidate': ''},
                })
                posts = await db.posts.find({
                    **POST_SCOPE,
                    'platform': {'$in': enabled},
                    'sentiment': None,
                    'demo': False,
                }).sort('published_at', -1).limit(200).to_list(200)
                size = config().hf_batch_size
                for offset in range(0, len(posts), size):
                    batch = posts[offset:offset + size]
                    results = await engine.classify([p['content'] for p in batch])
                    for p, r in zip(batch, results, strict=True):
                        if r.pending:
                            await db.posts.update_one({'_id': p['_id'], 'sentiment': None}, {'$set': {
                                'classification_status': 'awaiting_gemini', 'hf_candidate': r.model_dump(),
                                'classification_error': 'Gemini unavailable; negative or low-confidence result excluded from totals'}})
                            continue
                        start, end = bounds(today())
                        published_today = start <= p['published_at'] < end
                        notification = ({'status': 'pending', 'created_at': now()}
                                        if r.sentiment == 'negative' and published_today
                                        and not p.get('telegram_notification') else None)
                        update_fields = {'sentiment': dict(
                            label=r.sentiment, confidence=r.confidence, reason=r.reason,
                            model_used=r.model_used or engine.provenance, hf_confidence=r.hf_confidence,
                            language=r.language, targets=r.targets,
                            review_required=r.review_required, classified_at=now()), 'classification_status': 'classified',
                            'sentiment_schema_version': 6}
                        if notification:
                            update_fields['telegram_notification'] = notification
                        unset_fields = {'classification_error': '', 'hf_candidate': ''}
                        if r.sentiment != 'negative':
                            unset_fields['telegram_notification'] = ''
                        await db.posts.update_one({'_id': p['_id'], 'sentiment': None}, {'$set': update_fields,
                            '$unset': unset_fields})
                    # Deliver newly classified negatives immediately after each batch.
                    await notify_negative_posts(db, client)
            except Exception:
                # Classifier exposes failure state; pending posts are retried next sync.
                pass
        await rollup(db)
        await notify_negative_posts(db, client)
        # Include late-arriving posts from previous reporting days.
        async for day in db.daily_aggregates.find({'platform': 'overall', 'content_scope': 'youtube-title-only-v2'}):
            await create_alert(db, day['date'])

async def seed(db):
    if await db.posts.count_documents({'demo': True}):
        return
    rng = random.Random(20260928)
    texts = {
        'positive': ['जन सुराज की शिक्षा पर चर्चा अच्छी लगी। अब काम होते देखना है।', 'Prashant Kishore is asking the right questions about jobs in Bihar.', 'Jan Suraaj Party ki local meeting mein achhi discussion hui.'],
        'negative': ['Jan Suraaj Party needs a clearer jobs plan. Promises alone are not enough.', 'प्रशांत किशोर की बातों में जमीन पर काम की स्पष्ट योजना कहाँ है?', 'PK ki rally mein sawaal zyada, jawaab kam. Bihar needs specifics.'],
        'neutral': ['Prashant Kishore addressed a public meeting today. Full discussion to follow.', 'जन सुराज की अगली बैठक रविवार को आयोजित होगी।', 'Jan Suraaj Party: a summary of today’s education policy discussion.']}
    texts['mixed'] = [
        'Prashant Kishore raised strong education points, but his jobs plan remains weak.',
        'Jan Suraaj campaign praised for outreach but criticised for unclear candidates.',
    ]
    docs = []
    day0 = datetime.fromisoformat(today()).date()
    for offset in range(30):
        day = (day0 - timedelta(days=offset)).isoformat(); start, end = bounds(day)
        for platform in ['x', 'youtube']:
            count = (1080 if platform == 'x' else 570) if offset == 0 else rng.randint(260, 560)
            for i in range(count):
                label = rng.choices(['positive', 'negative', 'neutral', 'mixed'], [32, 40 if offset == 0 else 24 + offset % 9, 23, 5])[0]
                eng = dict(likes=rng.randint(0, 980), comments=rng.randint(0, 180), shares=rng.randint(0, 240), views=rng.randint(300, 18000))
                available_seconds = max(1, int((min(end, now()) - start).total_seconds()))
                docs.append(dict(schema_version=1, platform=platform, external_id=f'demo-{day}-{platform}-{i}',
                                 author=f'Sample analyst {i % 80 + 1:02d}', content=rng.choice(texts[label]), url='',
                                 published_at=start + timedelta(seconds=rng.randrange(available_seconds)), engagement=eng,
                                 engagement_score=eng['likes'] + eng['comments'] + eng['shares'], ingested_at=now(), demo=True,
                                 sentiment=dict(label=label, confidence=round(rng.uniform(.59, .98), 2), reason='Synthetic demonstration label', model_used='demo-fixture', classified_at=now())))
    await db.posts.insert_many(docs)
