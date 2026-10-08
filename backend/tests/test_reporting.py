from datetime import datetime, timezone
from io import BytesIO

from pypdf import PdfReader

from app.reporting import build_sentiment_pdf


def test_pdf_lists_filtered_posts_by_platform_with_clickable_links_and_poppins():
    stream = BytesIO()
    posts = [
        {
            'platform': 'facebook',
            'author': 'Bihar News',
            'content': 'A critical Facebook post about the campaign plan.',
            'url': 'https://example.org/facebook/negative-1',
            'published_at': datetime(2026, 10, 3, 8, 30, tzinfo=timezone.utc),
            'engagement_score': 42,
            'sentiment': {'label': 'negative', 'confidence': .91},
        },
        {
            'platform': 'youtube',
            'author': 'News Desk',
            'content': 'A critical YouTube title about campaign delivery.',
            'url': 'https://youtube.com/watch?v=negative2',
            'published_at': datetime(2026, 10, 3, 9, 30, tzinfo=timezone.utc),
            'engagement_score': 17,
            'sentiment': {'label': 'negative', 'confidence': .87},
        },
    ]
    build_sentiment_pdf(
        stream,
        start='2026-10-03',
        end='2026-10-03',
        period='daily',
        timezone_name='Asia/Kolkata',
        rows=[{
            'date': '2026-10-03', 'platform': 'facebook', 'positive_count': 2,
            'negative_count': 1, 'neutral_count': 1, 'mixed_count': 0,
            'total_count': 4, 'negativity_index': 25,
        }],
        sentiment_posts=posts,
        sentiment_filter='negative',
    )

    stream.seek(0)
    reader = PdfReader(stream)
    text = '\n'.join((page.extract_text() or '') for page in reader.pages)
    links = {
        str(annotation.get_object().get('/A', {}).get('/URI'))
        for page in reader.pages
        for annotation in (page.get('/Annots') or [])
        if annotation.get_object().get('/Subtype') == '/Link'
    }
    assert 'Facebook negative posts' in text
    assert 'Youtube negative posts' in text
    assert 'A critical Facebook post' in text
    assert 'A critical YouTube title' in text
    assert 'Data source status' not in text
    assert links == {post['url'] for post in posts}
    embedded_fonts = {
        str(font.get_object().get('/BaseFont'))
        for page in reader.pages
        for font in (page['/Resources'].get('/Font') or {}).get_object().values()
    }
    assert any('Poppins' in name for name in embedded_fonts)
