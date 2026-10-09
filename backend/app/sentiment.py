"""Local multilingual sentiment with Gemini target-stance verification."""
import asyncio
import html
import math
import re
import threading
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .config import config


class Result(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sentiment: Literal['positive', 'negative', 'neutral', 'mixed']
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=300)
    model_used: str = ''
    hf_confidence: float | None = None
    language: Literal['hi', 'en', 'hinglish'] = 'en'
    targets: list[str] = Field(default_factory=list)
    pending: bool = False
    review_required: bool = False


def preprocess(text: str) -> str:
    """Normalize social text using the Cardiff model-card conventions."""
    text = html.unescape(text)
    text = re.sub(r'https?://\S+', 'http', text)
    text = re.sub(r'(?<!\w)@\w+', '@user', text)
    # Hashtags aid target detection but can distort the base tone model.
    text = re.sub(r'#\S+', '', text)
    text = re.sub(r'[\s\u200b\u200c\u200d\ufeff]+', ' ', text).strip()
    return text[:400]


_TARGET_PATTERN = re.compile(
    r'jan\s*sura(?:j|aj)|\u091c\u0928\s*\u0938\u0941\u0930\u093e\u091c|'
    r'prashant\s*kishor(?:e)?|\u092a\u094d\u0930\u0936\u093e\u0902\u0924\s*'
    r'\u0915\u093f\u0936\u094b\u0930|\u092a\u0940\u0915\u0947|(?<![a-z])pk(?![a-z])', re.IGNORECASE,
)
_TARGET_HASHTAG_PATTERN = re.compile(
    r'#(?:jan_?sura(?:j|aj)|prashant_?kishor(?:e)?|pk|\u092a\u0940\u0915\u0947|'
    r'\u091c\u0928\u0938\u0941\u0930\u093e\u091c|'
    r'\u092a\u094d\u0930\u0936\u093e\u0902\u0924\u0915\u093f\u0936\u094b\u0930)', re.IGNORECASE,
)
_ATTRIBUTION_RISK_PATTERN = re.compile(
    r'\b(?:said|says|asks|questioned|blamed|accused|attacked|criticised|criticized|'
    r'against|vs\.?|slams?|reacts?|statement)\b|'
    r'\u0915\u0939\u093e|\u092c\u094b\u0932\u0947|\u092c\u092f\u093e\u0928|\u0906\u0930\u094b\u092a|'
    r'\u092d\u0921\u093c\u0915\u0947|\u092d\u0921\u0915\u0947|\u0938\u0935\u093e\u0932|'
    r'\u0939\u092e\u0932\u093e|\u0928\u093f\u0936\u093e\u0928\u093e|\u0935\u093f\u0930\u094b\u0927|'
    r'\u0916\u093f\u0932\u093e\u092b|\u091a\u0941\u092a\s+\u0915\u094d\u092f\u094b\u0902|'
    r'\b(?:bjp|rjd|jdu|jd\(u\)|congress|nda|modi|nitish|lalu|tejashwi)\b|'
    r'\u092d\u093e\u091c\u092a\u093e|\u0915\u093e\u0902\u0917\u094d\u0930\u0947\u0938|'
    r'\u092e\u094b\u0926\u0940|\u0936\u093e\u0939|\u0928\u0940\u0924\u0940\u0936|'
    r'\u0932\u093e\u0932\u0942|\u0924\u0947\u091c\u0938\u094d\u0935\u0940|\u0905\u0936\u094b\u0915\s*\u091a\u094c\u0927\u0930\u0940',
    re.IGNORECASE,
)

_ROMAN_HINDI = {
    'aaj', 'ab', 'accha', 'achha', 'bahut', 'bas', 'bhai', 'bihar', 'hai',
    'hain', 'hoga', 'ka', 'ke', 'ki', 'ko', 'kya', 'mein', 'mera', 'nahi',
    'nhi', 'par', 'se', 'sahi', 'vote', 'wala', 'wali', 'ye', 'yeh',
}


def detect_language(text: str) -> Literal['hi', 'en', 'hinglish']:
    devanagari = len(re.findall(r'[\u0900-\u097f]', text))
    latin_words = re.findall(r"[a-zA-Z']+", text.lower())
    if devanagari:
        return 'hinglish' if latin_words else 'hi'
    hindi_hits = sum(word in _ROMAN_HINDI for word in latin_words)
    return 'hinglish' if hindi_hits >= 2 else 'en'


def detect_targets(text: str) -> list[str]:
    targets = []
    if re.search(r'jan\s*sura(?:j|aj)|\u091c\u0928\s*\u0938\u0941\u0930\u093e\u091c|'
                 r'#jan_?sura(?:j|aj)|#\u091c\u0928\u0938\u0941\u0930\u093e\u091c', text, re.I):
        targets.append('jan_suraaj')
    if re.search(r'prashant\s*kishor(?:e)?|\u092a\u094d\u0930\u0936\u093e\u0902\u0924\s*'
                 r'\u0915\u093f\u0936\u094b\u0930|#prashant_?kishor(?:e)?|'
                 r'#\u092a\u094d\u0930\u0936\u093e\u0902\u0924\u0915\u093f\u0936\u094b\u0930|'
                 r'\u092a\u0940\u0915\u0947|(?<![a-z])pk(?![a-z])', text, re.I):
        targets.append('prashant_kishore')
    return targets


def apply_target_context(text: str, result: Result, verifier_gate: float = 0.80) -> Result:
    """Route target-ambiguous overall-tone results to Gemini.

    Cardiff predicts overall tweet tone. Bad-news words must not become criticism
    of Jan Suraaj merely because a campaign hashtag is present.
    """
    result.language = detect_language(text)
    result.targets = detect_targets(text)
    without_hashtags = re.sub(r'#\S+', '', text)
    target_only_in_hashtag = (
        bool(_TARGET_HASHTAG_PATTERN.search(text))
        and not _TARGET_PATTERN.search(without_hashtags)
    )
    attribution_risk = bool(_ATTRIBUTION_RISK_PATTERN.search(without_hashtags))
    needs_target_verification = (
        not result.targets
        or (result.sentiment != 'neutral' and (target_only_in_hashtag or attribution_risk))
    )
    if needs_target_verification:
        result.confidence = min(result.confidence, max(0, verifier_gate - 0.01))
        result.reason = 'Overall tone detected, but sentiment target is ambiguous; Gemini verification required.'
    return result


def result_from_scores(labels, scores, truncated=False, raw_text=''):
    labels = [str(label).lower() for label in labels]
    if len(labels) != 3 or set(labels) != {'negative', 'neutral', 'positive'}:
        raise ValueError('Model must expose named negative, neutral and positive labels')
    if len(scores) != 3 or any(not math.isfinite(s) or not 0 <= s <= 1 for s in scores):
        raise ValueError('Invalid model probabilities')
    if abs(sum(scores) - 1) > .001:
        raise ValueError('Model probabilities must sum to one')
    index = max(range(3), key=lambda i: scores[i])
    label, confidence = labels[index], scores[index]
    note = 'Overall text sentiment; target attribution is checked separately.'
    if confidence < .8:
        note += ' Low model confidence; Gemini verification required.'
    if truncated:
        note += ' Long post truncated; review full text.'
    return Result(sentiment=label, confidence=confidence, reason=note[:300])


class Classifier:
    def __init__(self, settings=None):
        self.settings = settings or config()
        self._tokenizer = self._model = None
        self._lock = threading.Lock()
        self.status = 'pending'
        self.error = None
        self._gemini = None

    @property
    def provenance(self):
        return f'{self.settings.hf_model}@{self.settings.hf_revision}'

    def _load(self):
        if self._model is not None:
            return
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        c = self.settings
        torch.set_num_threads(c.hf_cpu_threads)
        options = dict(revision=c.hf_revision, cache_dir=c.hf_cache_dir,
                       token=c.hf_token or False, local_files_only=c.hf_local_files_only,
                       trust_remote_code=False)
        tokenizer = AutoTokenizer.from_pretrained(c.hf_model, **options)
        model = AutoModelForSequenceClassification.from_pretrained(
            c.hf_model, weights_only=True, **options,
        )
        labels = [model.config.id2label[i] for i in range(model.config.num_labels)]
        result_from_scores(labels, [1 / 3] * 3)
        model.to(c.hf_device)
        model.eval()
        self._labels = labels
        self._tokenizer, self._model = tokenizer, model

    def _run(self, texts):
        with self._lock:
            self._load()
            import torch
            c = self.settings
            results = []
            for offset in range(0, len(texts), c.hf_batch_size):
                originals = texts[offset:offset + c.hf_batch_size]
                batch = [preprocess(t) for t in originals]
                raw = self._tokenizer(batch, truncation=False, add_special_tokens=True)
                truncated = [len(ids) > c.hf_max_length for ids in raw['input_ids']]
                tokens = self._tokenizer(batch, padding=True, truncation=True,
                                         max_length=c.hf_max_length, return_tensors='pt')
                tokens = {k: v.to(c.hf_device) for k, v in tokens.items()}
                with torch.inference_mode():
                    scores = self._model(**tokens).logits.softmax(dim=-1).cpu().tolist()
                if len(scores) != len(batch):
                    raise ValueError('Classification array length mismatch')
                for row, cut, cleaned, original in zip(
                        scores, truncated, batch, originals, strict=True):
                    result = result_from_scores(self._labels, row, cut, cleaned)
                    result.hf_confidence = result.confidence
                    results.append(apply_target_context(
                        original, result, c.hf_confidence_threshold,
                    ))
            return results

    async def classify(self, texts):
        if not texts:
            return []
        if any(not isinstance(t, str) or not t.strip() for t in texts):
            raise ValueError('Post text must be nonempty')
        self.status, self.error = 'loading', None
        try:
            results = await asyncio.to_thread(self._run, texts)
            if len(results) != len(texts):
                raise ValueError('Classification array length mismatch')
            results = [Result.model_validate(r) for r in results]
            for result in results:
                result.model_used = self.provenance
                if result.hf_confidence is None:
                    result.hf_confidence = result.confidence
            # Cardiff predicts overall text tone rather than stance toward the
            # tracked political entity. Every negative candidate therefore
            # needs Gemini target verification, even when HF is confident.
            # Low-confidence non-negative candidates continue to use the same
            # verifier. The Gemini result is always the final saved result.
            verify = [i for i, result in enumerate(results)
                      if (result.sentiment == 'negative'
                          or result.confidence < self.settings.hf_confidence_threshold)]
            if verify:
                from .gemini_fallback import GeminiFallback
                if self._gemini is None:
                    self._gemini = GeminiFallback(self.settings)
                refined = await self._gemini.classify([texts[i] for i in verify])
                for index, fallback in zip(verify, refined, strict=True):
                    if fallback is None:
                        results[index].pending = True
                        results[index].review_required = True
                    else:
                        fallback.model_used = 'gemini/' + self.settings.gemini_model
                        fallback.hf_confidence = results[index].hf_confidence
                        fallback.language = results[index].language
                        fallback.targets = results[index].targets
                        fallback.review_required = fallback.confidence < self.settings.hf_confidence_threshold
                        results[index] = fallback
            self.status = 'partial' if any(r.pending for r in results) else 'live'
            self.error = ((getattr(self._gemini, 'last_error', None) or
                           'Gemini verifier unavailable; negative or low-confidence posts remain pending.')
                          if self.status == 'partial' else None)
            return results
        except Exception:
            self.status = 'unavailable'
            self.error = 'Local model unavailable; check model cache, dependencies and memory. Posts remain pending.'
            raise


@lru_cache
def classifier():
    return Classifier()


def classifier_status():
    c = config()
    engine = classifier()
    status = 'demo' if c.seed_mock_data else ('ready' if engine.status == 'pending' else engine.status)
    return {
        'provider': 'Hugging Face sentiment + Gemini target verifier',
        'status': status,
        'model': c.hf_model,
        'revision': c.hf_revision,
        'threshold': c.hf_confidence_threshold,
        'fallback_model': c.gemini_model,
        'fallback_configured': bool(c.gemini_api_key),
        'error': None if c.seed_mock_data else engine.error,
    }
