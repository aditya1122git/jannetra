"""Rate-aware connectors for YouTube, Google News RSS and Apify social monitoring."""
import asyncio
import ast
import hashlib
import html
import json
import random
import re
import time
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from xml.etree import ElementTree
import httpx
from .config import (
    APIFY_FACEBOOK_DISCOVERY_ACTOR,
    APIFY_FACEBOOK_DISCOVERY_LIMIT,
    APIFY_MAX_ITEMS,
    APIFY_REDDIT_FALLBACK_ACTOR,
    APIFY_REDDIT_FALLBACK_MAX_CHARGE_USD,
    APIFY_RUN_TIMEOUT_SECONDS,
    APIFY_X_FALLBACK_ACTOR,
    APIFY_X_FALLBACK_MAX_CHARGE_USD,
    DEFAULT_APIFY_ACTORS,
    config,
)

NEWS_CHANNELS = (
    'News18', 'Zee Bihar', 'ABP Bihar', 'News State', 'Sahara Samay',
    'Bihar Tak', 'First Bihar', 'Live Cities', 'News4Nation',
    'Hindustani Media', 'Dainik Jagran Bihar', 'TV9 Bihar/Jharkhand',
    'Dainik Bhaskar Bihar', 'Prabhat Khabar', 'Live Hindustan Bihar',
    'ETV Bharat Bihar', 'Navbharat Times Bihar',
)

NEWS_SEARCH_QUERIES = (
    '"Jan Suraaj" OR "Jan Suraj"',
    '"Prashant Kishore"',
    '"जन सुराज"',
    '"प्रशांत किशोर"',
)

NEWS_SOURCE_ALIASES = {
    'News18': ('news18',),
    'Zee Bihar': ('zeebihar', 'zeebiharjharkhand', 'zeenews', 'zeehindustan'),
    'ABP Bihar': ('abpbihar', 'abplive', 'abpnews'),
    'News State': ('newsstate', 'newsstate24'),
    'Sahara Samay': ('saharasamay', 'samaylive'),
    'Bihar Tak': ('bihartak',),
    'First Bihar': ('firstbihar',),
    'Live Cities': ('livecities',),
    'News4Nation': ('news4nation',),
    'Hindustani Media': ('hindustanimedia',),
    'Dainik Jagran Bihar': ('jagran', 'jagrancom'),
    'TV9 Bihar/Jharkhand': ('tv9hindi', 'tv9bharatvarsh'),
    'Dainik Bhaskar Bihar': ('dainikbhaskar', 'bhaskarhindi'),
    'Prabhat Khabar': ('prabhatkhabar',),
    'Live Hindustan Bihar': ('livehindustan',),
    'ETV Bharat Bihar': ('etvbharat',),
    'Navbharat Times Bihar': ('navbharattimes',),
}

NEWS_SOURCE_EXACT_ALIASES = {'hindustan'}


def _approved_news_source(value):
    normalized = re.sub(r'[^a-z0-9]+', '', str(value or '').lower())
    return normalized in NEWS_SOURCE_EXACT_ALIASES or any(
        alias in normalized for aliases in NEWS_SOURCE_ALIASES.values() for alias in aliases
    )


class ProviderError(RuntimeError):
    pass


class DailyQuotaExhausted(ProviderError):
    pass


async def request(client, method, url, **kwargs):
    comments_optional = kwargs.pop('comments_optional', False)
    response_json = kwargs.pop('response_json', True)
    response_text = kwargs.pop('response_text', False)
    for attempt in range(4):
        try:
            response = await client.request(method, url, **kwargs)
            if comments_optional and response.status_code == 403 and any(e.get('reason') == 'commentsDisabled' for e in response.json().get('error', {}).get('errors', [])):
                return {'items': []}
            if response.status_code in (403, 429):
                try:
                    error = response.json().get('error', {})
                    message = error.get('message', '')
                    reasons = {row.get('reason', '') for row in error.get('errors', [])}
                except (TypeError, ValueError):
                    message, reasons = '', set()
                quota_reasons = {
                    'quotaExceeded', 'dailyLimitExceeded', 'dailyLimitExceededUnreg',
                    'rateLimitExceeded', 'userRateLimitExceeded',
                }
                if reasons & quota_reasons or 'quota' in message.lower():
                    raise DailyQuotaExhausted('YouTube daily search quota exhausted; retry after quota reset')
                if response.status_code == 403:
                    response.raise_for_status()
                delay = max(float(response.headers.get('retry-after', 0) or 0),
                            float(response.headers.get('x-rate-limit-reset', 0) or 0) - time.time(),
                            2 ** attempt + random.random())
                if delay > 30 or attempt == 3:
                    raise ProviderError('Provider rate limited or unavailable; retry next scheduled run')
                await asyncio.sleep(delay)
                continue
            if response.status_code >= 500:
                delay = max(float(response.headers.get('retry-after', 0) or 0),
                            float(response.headers.get('x-rate-limit-reset', 0) or 0) - time.time(),
                            2 ** attempt + random.random())
                if delay > 30 or attempt == 3:
                    raise ProviderError('Provider rate limited or unavailable; retry next scheduled run')
                await asyncio.sleep(delay)
                continue
            response.raise_for_status()
            return response.text if response_text else response.json() if response_json else {}
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                raise ProviderError(f'Provider rejected request ({exc.response.status_code})') from None
            if attempt == 3:
                raise ProviderError('Provider unavailable') from None
            await asyncio.sleep(2 ** attempt)
    raise ProviderError('Provider unavailable')


def matches(text, keywords):
    return any(re.search(r'(?<!\w)' + re.escape(k) + r'(?!\w)', text, re.I) for k in keywords)


def post(platform, external_id, author, content, url, timestamp, engagement=None):
    metrics = {k: int((engagement or {}).get(k, 0)) for k in ('likes', 'comments', 'shares', 'views')}
    return dict(schema_version=1, platform=platform, external_id=str(external_id), author=author,
                content=content, url=url, published_at=datetime.fromisoformat(timestamp.replace('Z', '+00:00')),
                engagement=metrics, engagement_score=sum(metrics[k] for k in ('likes', 'comments', 'shares')),
                ingested_at=datetime.now(timezone.utc), sentiment=None, demo=False)


async def youtube_posts(client, secret, keywords, since):
    base = 'https://www.googleapis.com/youtube/v3/'
    keys = list(dict.fromkeys(filter(None, (
        str(secret.get('api_key', '')).strip(),
        str(secret.get('backup_api_key', '')).strip(),
    ))))
    if not keys:
        raise ProviderError('YouTube API key is not configured')
    active_key = 0

    async def youtube_get(endpoint, params):
        nonlocal active_key
        while active_key < len(keys):
            try:
                return await request(client, 'GET', base + endpoint,
                                     params={**params, 'key': keys[active_key]})
            except DailyQuotaExhausted:
                active_key += 1
        raise ProviderError('YouTube daily quota exhausted on all configured API keys; retry after quota reset')

    # One combined official search includes regular videos and Shorts. The old
    # per-group + short-filter approach spent about four times as many quota units.
    rows = []
    videos = {}
    query_terms = []
    for keyword in dict.fromkeys(k.strip() for k in keywords if k.strip()):
        if len('|'.join(query_terms + [keyword])) > 450:
            break
        query_terms.append(keyword)
    data = await youtube_get('search', dict(
        part='snippet', type='video', order='date', maxResults=50,
        q='|'.join(query_terms), publishedAfter=since.isoformat().replace('+00:00', 'Z'),
    ))
    for item in data.get('items', []):
        video_id = item.get('id', {}).get('videoId')
        if video_id:
            videos[video_id] = {'general-search'}
    # Fetch stable video metadata in batches. Sentiment uses the title only.
    ids = list(videos)
    for offset in range(0, len(ids), 50):
        details = await youtube_get('videos', dict(
            part='snippet,statistics,contentDetails', id=','.join(ids[offset:offset + 50])))
        for video in details.get('items', []):
            vid, snippet = video['id'], video['snippet']
            title = snippet['title']
            if not matches(title, keywords):
                continue
            stats = video.get('statistics', {})
            row = post('youtube', vid, snippet['channelTitle'], title,
                       f'https://www.youtube.com/watch?v={vid}', snippet['publishedAt'],
                       dict(likes=stats.get('likeCount', 0), views=stats.get('viewCount', 0)))
            duration = video.get('contentDetails', {}).get('duration', '')
            duration_match = re.fullmatch(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration)
            duration_seconds = (int(duration_match.group(1) or 0) * 3600
                                + int(duration_match.group(2) or 0) * 60
                                + int(duration_match.group(3) or 0)) if duration_match else None
            short_candidate = duration_seconds is not None and duration_seconds < 240
            row.update(content_type='short' if short_candidate else 'video', title=title,
                       discovery=sorted(videos[vid]),
                       content_scope='youtube-title-only-v2')
            rows.append(row)
    secret.pop('watched_videos', None)
    return rows


def _rss_text(value):
    clean = re.sub(r'<[^>]+>', ' ', html.unescape(value or ''))
    return ' '.join(clean.split())


async def google_news_posts(client, keywords, since):
    """Fetch approved Bihar publishers from Google News public RSS search feeds."""
    lookback_days = min(7, max(1, math.ceil((datetime.now(timezone.utc) - since).total_seconds() / 86400)))

    async def fetch(entity_query):
        # Google News does not treat a quoted publisher name as a reliable
        # source filter. Query each tracked entity and validate the RSS
        # <source> metadata against the approved publisher allow-list instead.
        query = f'({entity_query}) when:{lookback_days}d'
        xml = await request(client, 'GET', 'https://news.google.com/rss/search', params={
            'q': query, 'hl': 'hi', 'gl': 'IN', 'ceid': 'IN:hi',
        }, headers={'Accept': 'application/rss+xml, application/xml;q=0.9'}, response_text=True)
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError:
            raise ProviderError('Google News returned invalid RSS') from None
        found = []
        for item in root.findall('./channel/item'):
            source = _rss_text(item.findtext('source'))
            if not _approved_news_source(source):
                continue
            title = _rss_text(item.findtext('title'))
            suffix = f' - {source}'
            headline = title[:-len(suffix)].strip() if source and title.endswith(suffix) else title
            if not headline or not matches(headline, keywords):
                continue
            try:
                published = parsedate_to_datetime(item.findtext('pubDate') or '').astimezone(timezone.utc)
            except (TypeError, ValueError):
                continue
            if published < since:
                continue
            url = (item.findtext('link') or '').strip()
            guid = (item.findtext('guid') or '').strip()
            external_id = guid or hashlib.sha256(f'{url}\0{headline}\0{published.isoformat()}'.encode()).hexdigest()
            row = post('news', external_id, source, headline, url, published.isoformat())
            row.update(source_provider='google-news-rss', content_scope='google-news-rss-v1', title=headline)
            found.append(row)
        return found

    results = await asyncio.gather(*(fetch(query) for query in NEWS_SEARCH_QUERIES))
    unique = {}
    for rows in results:
        for row in rows:
            unique[row['external_id']] = row
    return list(unique.values())


def _first(item, *paths, default=None):
    """Return the first non-empty value from common Actor output shapes."""
    for path in paths:
        value = item
        for part in path.split('.'):
            if not isinstance(value, dict) or part not in value:
                value = None
                break
            value = value[part]
        if value not in (None, ''):
            return value
    return default


def _number(item, *paths):
    value = _first(item, *paths, default=0)
    try:
        return max(0, int(float(str(value).replace(',', ''))))
    except (TypeError, ValueError):
        return 0


def _author_name(value):
    """Normalize Actor author objects, including Python-repr strings."""
    if isinstance(value, dict):
        nested = _first(value, 'name', 'fullName', 'displayName', 'userName', 'username', default='')
        return str(nested).strip() or 'Unknown'
    if isinstance(value, (list, tuple)):
        return _author_name(value[0]) if value else 'Unknown'
    text = str(value or '').strip()
    if text.startswith('{') and text.endswith('}'):
        parsed = None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            try:
                parsed = ast.literal_eval(text)
            except (SyntaxError, ValueError):
                pass
        if isinstance(parsed, dict):
            return _author_name(parsed)
        # Never expose a provider's serialized metadata blob as an author name.
        return 'Unknown'
    return text[:160] or 'Unknown'


def _timestamp(value):
    if isinstance(value, (int, float)):
        # Accept Unix seconds and milliseconds returned by different Actors.
        value = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(value, timezone.utc)
    if not value:
        raise ProviderError('Apify item has no publication timestamp')
    text = str(value).strip().replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # RFC 2822 is common in RSS/news Actor output.
        from email.utils import parsedate_to_datetime
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            raise ProviderError('Apify item has an invalid publication timestamp') from None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _actor_input(platform, keywords, since, max_items):
    """Build inputs for the fixed, tested Apify Actors."""
    since_iso = since.isoformat().replace('+00:00', 'Z')
    if platform == 'instagram':
        # Instagram's official Actor accepts comma-separated searches. Hashtag
        # search is the compliant public discovery mode for keyword monitoring.
        searches = []
        for keyword in keywords[:10]:
            tag = re.sub(r'[^\w]+', '', keyword.lstrip('#'), flags=re.UNICODE)
            if tag and tag.casefold() not in {value.casefold() for value in searches}:
                searches.append(tag)
        return {
            'resultsType': 'posts',
            'searchType': 'hashtag',
            'search': ','.join(searches),
            'searchLimit': min(max_items, 50),
            'resultsLimit': max_items,
            'onlyPostsNewerThan': since_iso,
        }
    if platform == 'x':
        # Use one OR query so every entity spelling shares the full result
        # budget. The Actor supports only a small batch of searches, while the
        # tracked-keyword list can be much larger and starve later entries.
        terms = []
        for keyword in keywords:
            cleaned = str(keyword).strip()
            if not cleaned:
                continue
            if cleaned.casefold() == 'pk':
                expression = '("PK" Bihar)'
            elif cleaned.startswith('#'):
                expression = cleaned
            else:
                expression = f'"{cleaned.replace(chr(34), "").strip()}"'
            if expression.casefold() not in {term.casefold() for term in terms}:
                terms.append(expression)
        if not terms:
            raise ProviderError('No usable X search terms')
        start_date = since.astimezone(timezone.utc).date().isoformat()
        query = f'({" OR ".join(terms)}) since:{start_date} -filter:replies'
        return {
            'searchTerms': [query],
            'maxItems': max(50, max_items),
            'sort': 'Latest + Top',
            'start': start_date,
            'includeSearchTerms': True,
        }
    if platform == 'reddit':
        terms = []
        for keyword in keywords:
            cleaned = str(keyword).strip()
            if not cleaned or cleaned.casefold() == 'pk':
                continue
            expression = cleaned if cleaned.startswith('#') else f'"{cleaned.replace(chr(34), "").strip()}"'
            if expression.casefold() not in {term.casefold() for term in terms}:
                terms.append(expression)
        if not terms:
            raise ProviderError('No usable Reddit search terms')
        return {
            'queries': [' OR '.join(terms)],
            'sort': 'new',
            'timeframe': 'day',
            # This Actor rejects ISO timestamps here; its schema requires a
            # calendar date in YYYY-MM-DD form.
            'dateFrom': since.astimezone(timezone.utc).date().isoformat(),
            'forceSortNewForTimeFilteredRuns': True,
            'scrapeComments': False,
            'includeNsfw': False,
            'strictSearch': False,
            'strictTokenFilter': False,
            'sentiment_analysis': False,
            'content_analysis': False,
            'maxPosts': max_items,
        }
    raise ProviderError(f'Unsupported direct Apify input for {platform}')


def _reddit_lite_input(keywords, since, max_items):
    """Build the separate input contract used by trudax/reddit-scraper-lite."""
    terms = []
    for keyword in keywords:
        cleaned = str(keyword).strip()
        if not cleaned or cleaned.casefold() == 'pk':
            continue
        expression = cleaned if cleaned.startswith('#') else f'"{cleaned.replace(chr(34), "").strip()}"'
        if expression.casefold() not in {term.casefold() for term in terms}:
            terms.append(expression)
    if not terms:
        raise ProviderError('No usable Reddit search terms')
    fallback_limit = min(max_items, 20)
    return {
        'searches': [' OR '.join(terms)],
        'ignoreStartUrls': True,
        'skipComments': True,
        'searchPosts': True,
        'searchComments': False,
        'searchCommunities': False,
        'searchUsers': False,
        'searchMedia': False,
        'sort': 'new',
        'time': 'day',
        'includeNSFW': False,
        'maxItems': fallback_limit,
        'maxPostCount': fallback_limit,
        'maxComments': 0,
        'postDateLimit': since.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'),
    }


def _apify_item(platform, item, keywords, since):
    if not isinstance(item, dict):
        return None
    if platform == 'reddit':
        kind = str(_first(item, 'kind', 'dataType', 'type', default='post')).lower()
        if kind not in ('post', 'submission'):
            return None
    title = str(_first(item, 'title', 'headline', 'article.title', default='')).strip()
    text = str(_first(item, 'body', 'selftext', 'text', 'full_text', 'tweetText', 'postText', 'message', 'caption',
                      'description', 'snippet', 'article.description', default='')).strip()
    # Social Actors expose the post body in `text`/`caption`; replies and
    # comments are intentionally excluded.
    content = '\n\n'.join(part for part in (title, text) if part) if platform == 'reddit' else text or title
    if not content or not matches(content, keywords):
        return None
    published = _timestamp(_first(item, 'publishedAt', 'published_at', 'createdAt', 'created_at', 'created_utc', 'takenAt',
                                  'date_utc', 'timestamp', 'date', 'time', 'timeCreated', 'article.publishedAt'))
    if published < since:
        return None
    url = str(_first(item, 'canonical_url', 'url', 'postUrl', 'tweetUrl', 'permalink', 'link', 'article.url', default=''))
    if platform == 'reddit' and url.startswith('/'):
        url = 'https://www.reddit.com' + url
    external_id = _first(item, 'id', 'parsedId', 'postId', 'tweetId', 'shortCode', 'shortcode', 'article.id')
    if not external_id:
        external_id = hashlib.sha256(f'{platform}\0{url}\0{content}\0{published.isoformat()}'.encode()).hexdigest()
    author = _author_name(_first(item, 'authorName', 'pageName', 'author.name', 'author.userName', 'author.username',
                                'author', 'ownerUsername', 'username', 'fullName', 'user.pageName', 'user.name', 'user',
                                'channelName', 'source', 'publisher', default='Unknown'))
    engagement = dict(
        likes=_number(item, 'likesCount', 'likeCount', 'reactionCount', 'likes', 'favoriteCount', 'score', 'ups', 'upVotes', 'stats.likes', 'reactions_count', 'public_metrics.like_count'),
        comments=_number(item, 'commentsCount', 'commentCount', 'comments', 'replyCount', 'num_comments', 'numberOfComments', 'stats.comments', 'comments_count', 'public_metrics.reply_count'),
        shares=_number(item, 'sharesCount', 'shareCount', 'shares', 'retweetCount', 'stats.shares', 'reshare_count', 'public_metrics.retweet_count'),
        views=_number(item, 'viewsCount', 'viewCount', 'videoPostViewCount', 'views', 'impressionCount', 'public_metrics.impression_count'),
    )
    row = post(platform, external_id, author, content, url, published.isoformat(), engagement)
    row.update(source_provider='apify', content_scope=(
        'reddit-post-only-v1' if platform == 'reddit' else 'apify-public-post-v1'
    ))
    return row


async def _run_apify_actor(client, actor_id, api_key, payload, limit, max_total_charge_usd=None):
    actor_path = quote(actor_id.replace('/', '~'), safe='~')
    url = f'https://api.apify.com/v2/acts/{actor_path}/run-sync-get-dataset-items'
    params = {'format': 'json', 'clean': '1', 'limit': limit, 'maxItems': limit,
              'timeout': APIFY_RUN_TIMEOUT_SECONDS}
    if max_total_charge_usd is not None:
        params['maxTotalChargeUsd'] = max_total_charge_usd
    data = await request(client, 'POST', url,
                         params=params,
                         headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                         timeout=APIFY_RUN_TIMEOUT_SECONDS + 15,
                         json=payload)
    if not isinstance(data, list):
        raise ProviderError('Apify Actor did not return a dataset item array')
    return data


async def apify_posts(client, platform, secret, keywords, since):
    """Run JanNetra's fixed Apify Actors and normalize public social posts."""
    api_key = str(secret.get('api_key', '')).strip()
    if not api_key:
        raise ProviderError('Missing Apify API token')
    max_items = APIFY_MAX_ITEMS
    if platform == 'facebook':
        # The official Posts Actor accepts page/profile URLs, not keywords.
        # Discover matching public pages first, then fetch their latest posts.
        discovery = await _run_apify_actor(client, APIFY_FACEBOOK_DISCOVERY_ACTOR, api_key, {
            'categories': keywords[:10],
            'locations': ['Bihar'],
            'resultsLimit': APIFY_FACEBOOK_DISCOVERY_LIMIT,
        }, APIFY_FACEBOOK_DISCOVERY_LIMIT)
        page_urls = []
        for item in discovery:
            candidate = str(_first(item, 'facebookUrl', 'pageUrl', 'url', default='')).strip()
            if candidate.startswith(('https://www.facebook.com/', 'https://facebook.com/')) and candidate not in page_urls:
                page_urls.append(candidate)
        if not page_urls:
            return []
        per_page = max(1, max_items // len(page_urls))
        payload = {
            'startUrls': [{'url': url} for url in page_urls],
            'resultsLimit': per_page,
            'onlyPostsNewerThan': since.isoformat().replace('+00:00', 'Z'),
        }
    else:
        payload = _actor_input(platform, keywords, since, max_items)
    if platform == 'x':
        primary_error = None
        try:
            data = await _run_apify_actor(
                client, DEFAULT_APIFY_ACTORS[platform], api_key, payload, max_items,
            )
            primary_no_results = bool(data) and all(
                isinstance(item, dict) and item.get('noResults') for item in data
            )
            if primary_no_results:
                raise ProviderError('Primary X Actor returned no tweet rows')
        except ProviderError as exc:
            primary_error = exc
            try:
                # Protect FREE-plan credits: on that tier this fallback can
                # cost far more per query than Tweet Scraper V2. Paid plans
                # can execute it inside the same bounded charge ceiling.
                data = await _run_apify_actor(
                    client, APIFY_X_FALLBACK_ACTOR, api_key, payload, max_items,
                    max_total_charge_usd=APIFY_X_FALLBACK_MAX_CHARGE_USD,
                )
            except ProviderError:
                raise ProviderError(
                    f'X Actors unavailable; primary error: {primary_error}'
                ) from None
            if data and all(isinstance(item, dict) and item.get('noResults') for item in data):
                raise ProviderError('Apify X Actors returned no tweet rows')
    elif platform == 'reddit':
        try:
            data = await _run_apify_actor(
                client, DEFAULT_APIFY_ACTORS[platform], api_key, payload, max_items,
            )
        except ProviderError as primary_error:
            try:
                fallback_payload = _reddit_lite_input(keywords, since, max_items)
                data = await _run_apify_actor(
                    client, APIFY_REDDIT_FALLBACK_ACTOR, api_key, fallback_payload,
                    fallback_payload['maxItems'],
                    max_total_charge_usd=APIFY_REDDIT_FALLBACK_MAX_CHARGE_USD,
                )
            except ProviderError:
                raise ProviderError(
                    f'Reddit Actors unavailable; primary error: {primary_error}'
                ) from None
    else:
        data = await _run_apify_actor(
            client, DEFAULT_APIFY_ACTORS[platform], api_key, payload, max_items,
        )
    rows = []
    invalid = 0
    for item in data:
        try:
            row = _apify_item(platform, item, keywords, since)
        except ProviderError:
            invalid += 1
            continue
        if row:
            rows.append(row)
    if data and invalid == len(data):
        raise ProviderError('Apify output has no usable publication timestamps; check Actor mapping')
    return rows
