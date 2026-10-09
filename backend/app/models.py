from datetime import datetime, timezone
from typing import Literal, Any
from beanie import Document
from pydantic import BaseModel, Field
from pymongo import IndexModel, DESCENDING, TEXT

Platform = Literal['facebook', 'instagram', 'x', 'youtube', 'news', 'reddit']
Label = Literal['positive', 'negative', 'neutral', 'mixed']

def now():
    return datetime.now(timezone.utc)

class Engagement(BaseModel):
    likes: int = Field(default=0, ge=0)
    comments: int = Field(default=0, ge=0)
    shares: int = Field(default=0, ge=0)
    views: int = Field(default=0, ge=0)

class Sentiment(BaseModel):
    label: Label
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=300)
    hf_confidence: float | None = None
    language: Literal['hi', 'en', 'hinglish'] = 'en'
    targets: list[str] = Field(default_factory=list)
    review_required: bool = False
    model_used: str
    classified_at: datetime = Field(default_factory=now)

class Post(Document):
    schema_version: int = 1
    platform: Platform
    external_id: str
    author: str
    content: str
    url: str
    published_at: datetime
    engagement: Engagement = Field(default_factory=Engagement)
    engagement_score: int = 0
    ingested_at: datetime = Field(default_factory=now)
    sentiment: Sentiment | None = None
    telegram_notification: dict[str, Any] | None = None
    demo: bool = False
    class Settings:
        name = 'posts'
        indexes = [IndexModel([('platform', 1), ('external_id', 1)], unique=True),
                   IndexModel([('published_at', DESCENDING)]),
                   IndexModel([('platform', 1), ('sentiment.label', 1), ('published_at', 1)]),
                   IndexModel([('telegram_notification.status', 1)]),
                   IndexModel([('content', TEXT)])]

class DailyAggregate(Document):
    schema_version: int = 1
    date: str
    platform: str
    positive_count: int
    negative_count: int
    neutral_count: int
    mixed_count: int = 0
    total_count: int
    negativity_index: float
    class Settings:
        name = 'daily_aggregates'
        indexes = [IndexModel([('date', 1), ('platform', 1)], unique=True)]

class Alert(Document):
    schema_version: int = 1
    date: str
    negative_count: int
    threshold: int
    triggered_at: datetime = Field(default_factory=now)
    notified_channels: list[str] = Field(default_factory=list)
    top_negative_posts: list[dict] = Field(default_factory=list)
    resolved: bool = False
    class Settings:
        name = 'alerts'
        indexes = [IndexModel([('date', 1)], unique=True)]

class TrackedKeyword(Document):
    keyword: str
    is_active: bool = True
    class Settings:
        name = 'tracked_keywords'
        indexes = [IndexModel([('keyword', 1)], unique=True)]

class PlatformCredential(Document):
    schema_version: int = 1
    platform: str
    mode: Literal['official', 'apify'] = 'official'
    encrypted_api_key: str
    status: str = 'pending'
    last_synced_at: datetime | None = None
    class Settings:
        name = 'platform_credentials'
        indexes = [IndexModel([('platform', 1)], unique=True)]

class User(Document):
    name: str = ''
    email: str
    hashed_password: str
    role: Literal['admin', 'viewer']
    class Settings:
        name = 'users'
        indexes = [IndexModel([('email', 1)], unique=True)]

class AuditLog(Document):
    user_id: str
    action: str
    timestamp: datetime = Field(default_factory=now)
    meta: dict[str, Any] = Field(default_factory=dict)
    class Settings:
        name = 'audit_logs'
        indexes = [IndexModel([('timestamp', 1)], expireAfterSeconds=7776000)]

DOCUMENTS = [Post, DailyAggregate, Alert, TrackedKeyword, PlatformCredential, User, AuditLog]
