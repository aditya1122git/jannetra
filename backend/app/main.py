import asyncio
import csv
import io
import logging
from contextlib import asynccontextmanager
from datetime import timedelta, date
import bcrypt
import httpx
import jwt
from bson import ObjectId
from pymongo.errors import DuplicateKeyError
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field
from typing import Literal
from .config import (config, APIFY_SYNC_INTERVAL_HOURS, DEFAULT_AUTOMATION_START_HOUR,
                     DEFAULT_AUTOMATION_END_HOUR)
from .connectors import _author_name
from .sentiment import classifier_status
from .models import DOCUMENTS, now, Platform, Label
from .services import POST_SCOPE, KEYWORDS, DEFAULT_KEYWORD_VARIANTS, PLATFORMS, today, bounds, settings, encrypt, audit, statuses, seed, sync, scheduled_sync, sync_running
from .reporting import build_sentiment_pdf

logger = logging.getLogger(__name__)


async def background_manual_sync(database, client):
    """Keep the HTTP request fast while the existing sync lock prevents overlap."""
    try:
        await sync(database, client)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception('Manual background sync failed')


def configure_automation_jobs(scheduler, database, client, start_hour: int, end_hour: int):
    """Replace automation jobs using the administrator's active-hour window."""
    job_ids = ('all-sources-4h', 'youtube-news-quarter-hour', 'youtube-news-full-hour')
    for job_id in job_ids:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
    social_hours = list(range(start_hour, end_hour + 1, APIFY_SYNC_INTERVAL_HOURS))
    frequent_full_hours = [hour for hour in range(start_hour + 1, end_hour + 1)
                           if hour not in social_hours]
    common = dict(max_instances=1, coalesce=True, misfire_grace_time=300)
    # At each four-hour boundary one combined run avoids overlapping sync jobs.
    scheduler.add_job(
        scheduled_sync, 'cron', hour=','.join(map(str, social_hours)), minute=0,
        args=[database, client, PLATFORMS], id='all-sources-4h', **common,
    )
    # YouTube and Google News RSS run every 15 minutes inside the window.
    scheduler.add_job(
        scheduled_sync, 'cron', hour=f'{start_hour}-{end_hour - 1}', minute='15,30,45',
        args=[database, client, ['youtube', 'news']], id='youtube-news-quarter-hour', **common,
    )
    if frequent_full_hours:
        scheduler.add_job(
            scheduled_sync, 'cron', hour=','.join(map(str, frequent_full_hours)), minute=0,
            args=[database, client, ['youtube', 'news']], id='youtube-news-full-hour', **common,
        )


def automation_schedule():
    """Expose the scheduler's real next run times for dashboard countdowns."""
    scheduler = getattr(app.state, 'scheduler', None)
    if not scheduler or not config().scheduler_enabled:
        return {'enabled': False, 'frequent': None, 'social': None}

    def next_time(*job_ids):
        times = []
        for job_id in job_ids:
            job = scheduler.get_job(job_id)
            if job and job.next_run_time:
                times.append(job.next_run_time)
        return min(times).isoformat() if times else None

    return {
        'enabled': True,
        'frequent': {
            'label': 'YouTube + News', 'interval_seconds': 15 * 60,
            'next_run_at': next_time('all-sources-4h', 'youtube-news-quarter-hour', 'youtube-news-full-hour'),
        },
        'social': {
            'label': 'Facebook + Instagram + X', 'interval_seconds': 4 * 60 * 60,
            'next_run_at': next_time('all-sources-4h'),
        },
    }


@asynccontextmanager
async def lifespan(app):
    c = config()
    if c.demo_in_memory:
        from mongomock_motor import AsyncMongoMockClient
        mongo = AsyncMongoMockClient(tz_aware=True)
    else:
        mongo = AsyncIOMotorClient(c.mongodb_uri, tz_aware=True, serverSelectionTimeoutMS=10000)
    # Separate demo database prevents fixture contamination of live collections.
    db = mongo[c.mongodb_database + ('_demo' if c.seed_mock_data else '')]
    if c.demo_in_memory:
        # Build disposable fixtures before mongomock indexes to avoid quadratic insert checks.
        await seed(db)
    await init_beanie(database=db, document_models=DOCUMENTS)
    app.state.db = db
    app.state.http = httpx.AsyncClient(timeout=30, follow_redirects=False)
    app.state.manual_sync_task = None
    if c.bootstrap_email:
        if not await db.users.find_one({'email': c.bootstrap_email}):
            hashed = await asyncio.to_thread(bcrypt.hashpw, c.bootstrap_password.encode(), bcrypt.gensalt())
            await db.users.insert_one(dict(name='Administrator', email=c.bootstrap_email,
                                           hashed_password=hashed.decode(), role='admin'))
    elif not await db.users.find_one({}):
        raise RuntimeError('No users exist. Set BOOTSTRAP_EMAIL and BOOTSTRAP_PASSWORD for the first startup only.')
    for term in dict.fromkeys(KEYWORDS + DEFAULT_KEYWORD_VARIANTS):
        await db.tracked_keywords.update_one({'keyword': term}, {'$setOnInsert': {'keyword': term, 'is_active': True}}, upsert=True)
    await db.settings.update_one({'_id': 'main'}, {
        '$set': {'threshold': 500},
        '$setOnInsert': {
            'automation_start_hour': DEFAULT_AUTOMATION_START_HOUR,
            'automation_end_hour': DEFAULT_AUTOMATION_END_HOUR,
        },
        '$unset': {'email': '', 'email_enabled': '', 'webhook_enabled': '', 'webhook_secret': ''},
    }, upsert=True)
    # Backfill the defaults for databases created before editable scheduling;
    # later startups preserve the administrator's saved values.
    await db.settings.update_one(
        {'_id': 'main', 'automation_start_hour': {'$exists': False}},
        {'$set': {'automation_start_hour': DEFAULT_AUTOMATION_START_HOUR}},
    )
    await db.settings.update_one(
        {'_id': 'main', 'automation_end_hour': {'$exists': False}},
        {'$set': {'automation_end_hour': DEFAULT_AUTOMATION_END_HOUR}},
    )
    # Repair historical Facebook rows created when an Actor serialized its
    # author object into a string. This keeps the UI and exports readable.
    async for stored in db.posts.find({'platform': 'facebook', 'author': {'$regex': r'^\s*\{'}}, {'author': 1}):
        cleaned_author = _author_name(stored.get('author'))
        if cleaned_author != stored.get('author'):
            await db.posts.update_one({'_id': stored['_id']}, {'$set': {'author': cleaned_author}})
    if c.youtube_api_key and not c.seed_mock_data and 'youtube' in c.enabled_platforms.split(','):
        # YOUTUBE_API_KEY is the source of truth. Using $setOnInsert here left a
        # stale encrypted key in MongoDB whenever an operator rotated the key in
        # .env, while the UI continued to report the old credential's failure.
        await db.platform_credentials.update_one({'platform': 'youtube'}, {
            '$set': dict(platform='youtube', mode='official',
                         encrypted_api_key=encrypt({
                             'api_key': c.youtube_api_key,
                             'backup_api_key': c.youtube_backup_api_key,
                         })),
            '$setOnInsert': dict(status='pending', last_synced_at=None),
        }, upsert=True)
    for p in ['facebook', 'instagram', 'x']:
        if c.apify_api_token and not c.seed_mock_data and p in c.enabled_platforms.split(','):
            secret = {'api_key': c.apify_api_token}
            await db.platform_credentials.update_one({'platform': p}, {'$set': dict(platform=p, mode='apify',
                encrypted_api_key=encrypt(secret), status='pending', last_synced_at=None)}, upsert=True)
        elif not c.seed_mock_data:
            await db.platform_credentials.delete_one({'platform': p, 'mode': {'$ne': 'apify'}})
    if not c.seed_mock_data and 'news' in c.enabled_platforms.split(','):
        # News is always credential-free Google RSS. Repair any stale
        # credential/error left by older deployments before the next run.
        await db.platform_credentials.update_one({'platform': 'news'}, {'$set': dict(
            platform='news', mode='official', encrypted_api_key=encrypt({}), status='pending',
            error=None,
        ), '$unset': {'last_attempt_at': ''}}, upsert=True)
        # The previous channel-name query was not a real publisher filter and
        # could repeatedly store zero rows. Give the corrected entity-query
        # strategy one bounded backfill, including on existing deployments.
        await db.platform_credentials.update_one({
            'platform': 'news', 'search_strategy': {'$ne': 'entity-rss-v3'},
        }, {'$set': {'last_synced_at': None, 'status': 'pending'},
            '$unset': {'last_attempt_at': ''}})
    if not c.seed_mock_data and 'youtube' in c.enabled_platforms.split(','):
        # One-time scope upgrade: refresh the last day using complete video snippets.
        await db.platform_credentials.update_one({'platform': 'youtube',
            'content_scope': {'$ne': 'youtube-title-only-v2'}},
            {'$set': {'content_scope': 'youtube-title-only-v2', 'last_synced_at': None,
                      'status': 'pending'}, '$unset': {'last_attempt_at': ''}})
        await db.platform_credentials.update_one({'platform': 'youtube',
            'search_strategy': {'$ne': 'keyword-and-short-v3'}},
            {'$set': {'status': 'pending'}, '$unset': {'last_attempt_at': ''}})
        # The active YouTube scope is deliberately title-only.
        await db.posts.update_many({'platform': 'youtube', 'content_scope': 'youtube-title-only-v2'},
                                   {'$unset': {'description': ''}})
    # Classified posts are immutable during normal startup and ingestion. Model
    # upgrades must use an explicit maintenance migration; silently clearing
    # sentiment here caused historical posts to join the new-post queue again.
    if c.seed_mock_data:
        await seed(db)
        await sync(db, app.state.http)
    scheduler = AsyncIOScheduler(timezone=c.reporting_timezone)
    app.state.scheduler = scheduler
    if c.scheduler_enabled:
        prefs = await settings(db)
        configure_automation_jobs(
            scheduler, db, app.state.http,
            prefs.get('automation_start_hour', DEFAULT_AUTOMATION_START_HOUR),
            prefs.get('automation_end_hour', DEFAULT_AUTOMATION_END_HOUR),
        )
        scheduler.start()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)
    manual_sync_task = getattr(app.state, 'manual_sync_task', None)
    if manual_sync_task and not manual_sync_task.done():
        manual_sync_task.cancel()
        await asyncio.gather(manual_sync_task, return_exceptions=True)
    await app.state.http.aclose()
    mongo.close()


app = FastAPI(title='JanNetra', version='1.0.0', lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=config().allowed_origins.split(','), allow_methods=['GET', 'POST', 'PUT'],
                   allow_headers=['Authorization', 'Content-Type'])
oauth = OAuth2PasswordBearer(tokenUrl='/api/auth/token')

@app.middleware('http')
async def headers(request, call_next):
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'DENY'
    return response

def db():
    return app.state.db

async def current_user(token: str = Depends(oauth)):
    try:
        claims = jwt.decode(token, config().jwt_secret, algorithms=['HS256'], audience='jannetra', issuer='jannetra')
        user = await db().users.find_one({'_id': ObjectId(claims['sub'])})
        if not user:
            raise ValueError()
        return user
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(401, 'Invalid or expired session', headers={'WWW-Authenticate': 'Bearer'})

async def admin(user=Depends(current_user)):
    if user['role'] != 'admin':
        raise HTTPException(403, 'Administrator access required')
    return user

def serialize(value):
    if isinstance(value, list):
        return [serialize(v) for v in value]
    if isinstance(value, dict):
        return {k: serialize(v) for k, v in value.items()}
    return str(value) if isinstance(value, ObjectId) else value

@app.get('/api/health')
async def health():
    await db().command('ping')
    return {'status': 'ok', 'demo': config().seed_mock_data}

@app.get('/api/auth/mode')
async def mode():
    return {'demo': config().seed_mock_data}

@app.post('/api/auth/token')
async def login(request: Request, form: OAuth2PasswordRequestForm = Depends()):
    # Shared DB buckets survive restarts. Bound both account and client attempts.
    minute = now().strftime('%Y%m%d%H%M')
    for identity in [f'ip:{request.client.host}', f'account:{form.username.lower()}']:
        key = identity + ':' + minute
        from pymongo import ReturnDocument
        bucket = await db().login_limits.find_one_and_update({'_id': key}, {'$inc': {'count': 1}, '$set': {'expires_at': now() + timedelta(minutes=2)}},
            upsert=True, return_document=ReturnDocument.AFTER)
        if bucket['count'] > 10:
            raise HTTPException(429, 'Too many login attempts. Try again in a minute.')
    await db().login_limits.create_index('expires_at', expireAfterSeconds=0)
    user = await db().users.find_one({'email': form.username.lower()})
    valid = user and len(form.password.encode()) <= 72 and await asyncio.to_thread(bcrypt.checkpw, form.password.encode(), user['hashed_password'].encode())
    if not valid:
        raise HTTPException(401, 'Invalid email or password')
    token = jwt.encode({'sub': str(user['_id']), 'exp': now() + timedelta(minutes=60), 'iat': now(), 'aud': 'jannetra', 'iss': 'jannetra'},
                       config().jwt_secret, algorithm='HS256')
    await audit(db(), user, 'login')
    return {'access_token': token, 'token_type': 'bearer', 'role': user['role'],
            'name': user.get('name') or 'Administrator', 'email': user['email']}

@app.get('/api/overview')
async def overview(platform: Platform | None = None, user=Depends(current_user)):
    sources = await statuses(db())
    usable = [s['platform'] for s in sources if s['status'] != 'disconnected']
    selected = [platform] if platform in usable else usable if platform is None else []
    rows = await db().daily_aggregates.find({'content_scope': 'youtube-title-only-v2', 'date': {'$gte': (date.fromisoformat(today()) - timedelta(days=29)).isoformat()},
                                          'platform': {'$in': selected}}, {'_id': 0}).to_list(None)
    grouped = {}
    for r in rows:
        out = grouped.setdefault(r['date'], dict(date=r['date'], positive_count=0, negative_count=0, neutral_count=0, mixed_count=0, total_count=0))
        for k in ['positive_count', 'negative_count', 'neutral_count', 'mixed_count', 'total_count']:
            out[k] += r.get(k, 0)
    for v in grouped.values():
        v['negativity_index'] = round(v['negative_count'] / v['total_count'] * 100, 1) if v['total_count'] else 0
    prefs = await settings(db())
    pending = await db().posts.count_documents({**POST_SCOPE, 'sentiment': None, 'platform': {'$in': selected}, 'demo': config().seed_mock_data})
    await audit(db(), user, 'view.overview', {'platform': platform})
    manual_sync_task = getattr(app.state, 'manual_sync_task', None)
    is_syncing = sync_running() or bool(manual_sync_task and not manual_sync_task.done())
    return serialize(dict(demo=config().seed_mock_data, date=today(), timezone=config().reporting_timezone,
        sources=sources, today=grouped.get(today()), trend=sorted(grouped.values(), key=lambda x: x['date']),
        platform_totals=[r for r in rows if r['date'] == today()], pending=pending, threshold=prefs['threshold'],
        sync_running=is_syncing,
        schedule=automation_schedule(),
        classifier=classifier_status(),
        partial=pending > 0 or any(s['status'] in ['unavailable', 'pending'] for s in sources if s['platform'] in selected),
        alerts=await db().alerts.find({'resolved': False, 'content_scope': 'youtube-title-only-v2'}).sort('date', -1).limit(30).to_list(30)))

@app.get('/api/posts')
async def posts(q: str = Query('', max_length=200), platform: Platform | None = None, sentiment: Label | None = None,
                day: date | None = None, sort: Literal['recency', 'engagement'] = 'recency', page: int = Query(1, ge=1, le=10000), user=Depends(current_user)):
    import re
    connected = [s['platform'] for s in await statuses(db()) if s['status'] != 'disconnected']
    query = {**POST_SCOPE, 'platform': platform if platform in connected else {'$in': connected if platform is None else []}, 'demo': config().seed_mock_data}
    if q:
        # Escaped substring supports Hindi and PK consistently; text index available for analytical queries.
        query['content'] = {'$regex': re.escape(q), '$options': 'i'}
    if sentiment:
        query['sentiment.label'] = sentiment
    if day:
        start, end = bounds(day.isoformat()); query['published_at'] = {'$gte': start, '$lt': end}
    rows = await db().posts.find(query).sort([('engagement_score' if sort == 'engagement' else 'published_at', -1), ('_id', -1)]).skip((page - 1) * 20).limit(20).to_list(20)
    await audit(db(), user, 'view.posts', {'platform': platform, 'sentiment': sentiment, 'page': page})
    return serialize({'items': rows, 'total': await db().posts.count_documents(query), 'page': page})

@app.get('/api/settings')
async def read_settings(user=Depends(current_user)):
    prefs = await settings(db())
    return serialize({'keywords': await db().tracked_keywords.find({}, {'_id': 0}).to_list(None),
                      'credentials': await db().platform_credentials.find({'platform': {'$in': PLATFORMS}}, {'encrypted_api_key': 0}).to_list(None),
                      'sources': await statuses(db()), 'model': config().hf_model, 'classifier': classifier_status(),
                      'automation_start_hour': prefs.get('automation_start_hour', DEFAULT_AUTOMATION_START_HOUR),
                      'automation_end_hour': prefs.get('automation_end_hour', DEFAULT_AUTOMATION_END_HOUR),
                      'timezone': config().reporting_timezone})

class Preferences(BaseModel):
    keywords: list[str] = Field(min_length=1, max_length=30)
    automation_start_hour: int = Field(ge=0, le=22)
    automation_end_hour: int = Field(ge=1, le=23)


@app.put('/api/settings')
async def update_settings(value: Preferences, user=Depends(admin)):
    if any(not k.strip() or len(k) > 100 for k in value.keywords):
        raise HTTPException(422, 'Keywords must contain 1-100 characters')
    terms = list(dict.fromkeys(k.strip() for k in value.keywords))
    if value.automation_start_hour >= value.automation_end_hour:
        raise HTTPException(422, 'Automation end time must be after start time')
    for term in terms:
        await db().tracked_keywords.update_one({'keyword': term}, {'$set': {'is_active': True}}, upsert=True)
    await db().tracked_keywords.update_many({'keyword': {'$nin': terms}}, {'$set': {'is_active': False}})
    await db().settings.update_one({'_id': 'main'}, {'$set': {
        'automation_start_hour': value.automation_start_hour,
        'automation_end_hour': value.automation_end_hour,
    }}, upsert=True)
    if config().scheduler_enabled:
        configure_automation_jobs(
            app.state.scheduler, db(), app.state.http,
            value.automation_start_hour, value.automation_end_hour,
        )
    await audit(db(), user, 'settings.update', {
        'automation_start_hour': value.automation_start_hour,
        'automation_end_hour': value.automation_end_hour,
    })
    return {'saved': True, 'automation_start_hour': value.automation_start_hour,
            'automation_end_hour': value.automation_end_hour}


class CredentialInput(BaseModel):
    platform: Literal['facebook', 'instagram', 'x', 'youtube']
    mode: Literal['official', 'apify'] = 'official'
    api_key: str = Field(min_length=10, max_length=10000)

@app.put('/api/credentials')
async def credentials(value: CredentialInput, user=Depends(admin)):
    if config().seed_mock_data:
        raise HTTPException(409, 'Turn off demo mode before storing real credentials')
    if value.platform == 'youtube' and value.mode != 'official':
        raise HTTPException(422, 'YouTube uses the official Data API')
    if value.platform != 'youtube' and value.mode != 'apify':
        raise HTTPException(422, 'Facebook, Instagram and X use Apify')
    secret = {'api_key': value.api_key}
    await db().platform_credentials.update_one({'platform': value.platform}, {'$set': dict(platform=value.platform, mode=value.mode,
        encrypted_api_key=encrypt(secret), status='pending', error=None)}, upsert=True)
    await audit(db(), user, 'credentials.rotate', {'platform': value.platform})
    return {'saved': True}

@app.post('/api/sync', status_code=202)
async def run_sync(user=Depends(admin)):
    await audit(db(), user, 'ingestion.request')
    task = getattr(app.state, 'manual_sync_task', None)
    if sync_running() or (task and not task.done()):
        return {'accepted': True, 'running': True, 'started': False}
    app.state.manual_sync_task = asyncio.create_task(
        background_manual_sync(db(), app.state.http), name='manual-sync')
    return {'accepted': True, 'running': True, 'started': True}

@app.post('/api/alerts/{day}/resolve')
async def resolve(day: date, user=Depends(admin)):
    result = await db().alerts.update_one({'date': day.isoformat()}, {'$set': {'resolved': True}})
    if not result.matched_count:
        raise HTTPException(404, 'Alert not found')
    await audit(db(), user, 'alert.resolve', {'date': day.isoformat()})
    return {'resolved': True}


@app.get('/api/profile')
async def read_profile(user=Depends(current_user)):
    return {'name': user.get('name') or 'Administrator', 'email': user['email'], 'role': user['role']}


class ProfileInput(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: str = Field(min_length=3, max_length=254)
    current_password: str = Field(min_length=1, max_length=72)
    new_password: str = Field(default='', max_length=14)


@app.put('/api/profile')
async def update_profile(value: ProfileInput, user=Depends(current_user)):
    name = ' '.join(value.name.split())
    email = value.email.strip().lower()
    if len(name) < 2 or '@' not in email or '\n' in email or '\r' in email:
        raise HTTPException(422, 'Valid name and email are required')
    if len(value.current_password.encode()) > 72 or not await asyncio.to_thread(
            bcrypt.checkpw, value.current_password.encode(), user['hashed_password'].encode()):
        raise HTTPException(401, 'Current password is incorrect')
    changes = {'name': name, 'email': email}
    if value.new_password:
        if len(value.new_password) < 8 or len(value.new_password) > 14:
            raise HTTPException(422, 'New password must contain 8-14 characters')
        changes['hashed_password'] = (await asyncio.to_thread(
            bcrypt.hashpw, value.new_password.encode(), bcrypt.gensalt())).decode()
    try:
        await db().users.update_one({'_id': user['_id']}, {'$set': changes})
    except DuplicateKeyError:
        raise HTTPException(409, 'Email address is already in use')
    await audit(db(), user, 'profile.update', {'email': email, 'password_changed': bool(value.new_password)})
    return {'saved': True, 'name': name, 'email': email, 'role': user['role']}

class NewUser(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=14, max_length=72)
    role: Literal['admin', 'viewer'] = 'viewer'

@app.post('/api/users', status_code=201)
async def add_user(value: NewUser, user=Depends(admin)):
    if '@' not in value.email or len(value.password.encode()) > 72:
        raise HTTPException(422, 'Valid email and password at most 72 bytes required')
    hashed = await asyncio.to_thread(bcrypt.hashpw, value.password.encode(), bcrypt.gensalt())
    try:
        await db().users.insert_one(dict(email=value.email.lower(), hashed_password=hashed.decode(), role=value.role))
    except DuplicateKeyError:
        raise HTTPException(409, 'User already exists')
    await audit(db(), user, 'user.create', {'email': value.email, 'role': value.role})
    return {'created': True}

@app.get('/api/export')
async def export(period: Literal['daily', 'weekly', 'monthly'] = 'daily', format: Literal['csv', 'pdf'] = 'csv',
                 sentiment: Literal['all', 'positive', 'negative', 'neutral', 'mixed'] = 'all',
                 user=Depends(current_user)):
    count = {'daily': 1, 'weekly': 7, 'monthly': 30}[period]
    end_day = today()
    start = (date.fromisoformat(end_day) - timedelta(days=count - 1)).isoformat()
    source_rows = await statuses(db())
    connected = [s['platform'] for s in source_rows if s['status'] != 'disconnected']
    rows = await db().daily_aggregates.find({'content_scope': 'youtube-title-only-v2', 'date': {'$gte': start, '$lte': today()}, 'platform': {'$in': connected}}, {'_id': 0}).sort([('date', 1), ('platform', 1)]).to_list(None)
    fields = ['date', 'platform', 'positive_count', 'negative_count', 'neutral_count', 'mixed_count', 'total_count', 'negativity_index']
    source_status = '; '.join(f'{s["platform"]}: {s["source"]}/{s["status"]}' for s in source_rows)
    if format == 'csv':
        selected_field = f'{sentiment}_count' if sentiment != 'all' else None
        csv_fields = fields + ['sentiment_filter', 'selected_count', 'mode', 'source_status']
        stream = io.StringIO(); writer = csv.DictWriter(stream, fieldnames=csv_fields, extrasaction='ignore'); writer.writeheader()
        writer.writerows([{
            **r,
            'sentiment_filter': sentiment,
            'selected_count': r.get(selected_field, 0) if selected_field else r.get('total_count', 0),
            'mode': 'DEMO' if config().seed_mock_data else 'LIVE',
            'source_status': source_status,
        } for r in rows])
        content = stream.getvalue().encode('utf-8-sig'); mime = 'text/csv'
    else:
        range_start, _ = bounds(start)
        _, range_end = bounds(end_day)
        sentiment_match = ({'$in': ['positive', 'negative', 'neutral', 'mixed']}
                           if sentiment == 'all' else sentiment)
        sentiment_posts = await db().posts.find({
            **POST_SCOPE,
            'published_at': {'$gte': range_start, '$lt': range_end},
            'platform': {'$in': connected},
            'sentiment.label': sentiment_match,
            'demo': config().seed_mock_data,
        }, {
            '_id': 0, 'platform': 1, 'author': 1, 'content': 1, 'url': 1,
            'published_at': 1, 'engagement': 1, 'engagement_score': 1,
            'sentiment.label': 1, 'sentiment.confidence': 1,
        }).sort([('platform', 1), ('published_at', -1)]).to_list(None)
        stream = io.BytesIO()
        build_sentiment_pdf(
            stream,
            start=start,
            end=end_day,
            period=period,
            timezone_name=config().reporting_timezone,
            rows=rows,
            sentiment_posts=sentiment_posts,
            sentiment_filter=sentiment,
            demo=config().seed_mock_data,
        )
        content = stream.getvalue(); mime = 'application/pdf'
    await audit(db(), user, 'export.' + format, {'period': period, 'sentiment': sentiment})
    return Response(content, media_type=mime, headers={'Content-Disposition': f'attachment; filename="jannetra-{period}-{sentiment}-{today()}.{format}"'})
