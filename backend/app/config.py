from functools import lru_cache
from pathlib import Path
from secrets import token_urlsafe
from cryptography.fernet import Fernet
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import model_validator

DEFAULT_APIFY_ACTORS = {
    'facebook': 'apify~facebook-posts-scraper',
    'instagram': 'apify~instagram-scraper',
    'x': 'apidojo~twitter-scraper-lite',
    'reddit': 'fatihtahta~reddit-scraper-search-fast',
}
APIFY_FACEBOOK_DISCOVERY_ACTOR = 'apify~facebook-search-scraper'
APIFY_FACEBOOK_DISCOVERY_LIMIT = 12
APIFY_MAX_ITEMS = 50
APIFY_RUN_TIMEOUT_SECONDS = 240
APIFY_SYNC_INTERVAL_HOURS = 4
DEFAULT_AUTOMATION_START_HOUR = 6
DEFAULT_AUTOMATION_END_HOUR = 22


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=(Path(__file__).resolve().parents[2] / '.env', Path(__file__).resolve().parents[1] / '.env'), extra='ignore')
    mongodb_uri: str = 'mongodb://mongo:27017'
    mongodb_database: str = 'jannetra'
    seed_mock_data: bool = False
    demo_in_memory: bool = False
    jwt_secret: str = ''
    encryption_key: str = ''
    bootstrap_email: str = ''
    bootstrap_password: str = ''
    hf_model: str = 'cardiffnlp/twitter-xlm-roberta-base-sentiment'
    hf_revision: str = 'f2f1202b1bdeb07342385c3f807f9c07cd8f5cf8'
    hf_token: str = ''
    hf_cache_dir: str = '.cache/huggingface'
    hf_device: str = 'cpu'
    hf_batch_size: int = 16
    hf_max_length: int = 256
    hf_cpu_threads: int = 2
    hf_local_files_only: bool = False
    gemini_api_key: str = ''
    gemini_model: str = 'gemini-3.5-flash-lite'
    hf_confidence_threshold: float = 0.80
    gemini_concurrency: int = 2
    enabled_platforms: str = 'facebook,instagram,x,youtube,news,reddit'
    apify_api_token: str = ''
    youtube_api_key: str = ''
    youtube_backup_api_key: str = ''
    youtube_initial_lookback_days: int = 7
    telegram_bot_token: str = ''
    telegram_chat_id: str = ''
    allowed_origins: str = 'http://localhost:5173,http://localhost:8080'
    reporting_timezone: str = 'Asia/Kolkata'
    scheduler_enabled: bool = True
    youtube_sync_interval_minutes: int = 15

    @model_validator(mode='after')
    def secure_defaults(self):
        if self.demo_in_memory and not self.seed_mock_data:
            raise ValueError('DEMO_IN_MEMORY requires SEED_MOCK_DATA=true')
        if self.seed_mock_data:
            self.jwt_secret = self.jwt_secret or token_urlsafe(48)
            self.encryption_key = self.encryption_key or Fernet.generate_key().decode()
        if len(self.jwt_secret) < 32 or not self.encryption_key:
            raise ValueError('Set JWT_SECRET (32+ chars) and ENCRYPTION_KEY (Fernet)')
        self.bootstrap_email = self.bootstrap_email.strip().lower()
        if self.bootstrap_email or self.bootstrap_password:
            if '@' not in self.bootstrap_email:
                raise ValueError('BOOTSTRAP_EMAIL must be a valid administrator email')
            if len(self.bootstrap_password) < 14:
                raise ValueError('BOOTSTRAP_PASSWORD must contain at least 14 characters')
            if len(self.bootstrap_password.encode()) > 72:
                raise ValueError('BOOTSTRAP_PASSWORD must be at most 72 UTF-8 bytes')
        self.telegram_bot_token = self.telegram_bot_token.strip()
        self.telegram_chat_id = self.telegram_chat_id.strip()
        if bool(self.telegram_bot_token) != bool(self.telegram_chat_id):
            raise ValueError('Set both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or leave both empty')
        Fernet(self.encryption_key.encode())
        if not 1 <= self.hf_batch_size <= 32 or not 16 <= self.hf_max_length <= 512:
            raise ValueError('HF_BATCH_SIZE must be 1..32 and HF_MAX_LENGTH must be 16..512')
        if not 1 <= self.hf_cpu_threads <= 32:
            raise ValueError('HF_CPU_THREADS must be 1..32')
        if (not 0 <= self.hf_confidence_threshold <= 1
                or not 1 <= self.gemini_concurrency <= 8):
            raise ValueError('Invalid classifier threshold/concurrency')
        if not 1 <= self.youtube_initial_lookback_days <= 30:
            raise ValueError('Invalid YouTube search coverage settings')
        if set(self.enabled_platforms.split(',')) - {'facebook', 'instagram', 'x', 'youtube', 'news', 'reddit'}:
            raise ValueError('Invalid ENABLED_PLATFORMS')
        if self.youtube_sync_interval_minutes != 15:
            raise ValueError('YouTube sync interval must remain 15 minutes')
        if self.hf_device not in ('cpu', 'cuda', 'mps'):
            raise ValueError('HF_DEVICE must be cpu, cuda, or mps')
        if not all(value.strip() for value in (
            self.hf_model, self.hf_revision, self.gemini_model,
        )):
            raise ValueError('HF and Gemini model names/revisions are required')
        return self


@lru_cache
def config():
    return Config()
