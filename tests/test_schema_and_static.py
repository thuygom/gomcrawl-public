from __future__ import annotations

import unittest

from core.schema import CrawlResult, EvidenceRecord
from core.utils import canonicalize_url, detect_pii, extract_image_page_links, extract_snapshot_image_links, strip_html


class SchemaAndUtilsTest(unittest.TestCase):
    def test_canonicalize_url_removes_tracking_and_fragment(self) -> None:
        self.assertEqual(
            canonicalize_url("HTTPS://Example.COM/path?b=2&utm_source=x&a=1#frag"),
            "https://example.com/path?a=1&b=2",
        )

    def test_strip_html_and_pii_detection(self) -> None:
        self.assertEqual(strip_html("<b>Hello</b>&nbsp;world"), "Hello world")
        self.assertEqual(detect_pii("contact test@example.com"), ["email"])

    def test_evidence_record_builds_stable_shape(self) -> None:
        record = EvidenceRecord.build(
            source="naver_web",
            source_type="search_engine_result",
            title="<b>Acme</b>",
            url="https://example.com/?utm_source=x",
            snippet="hello",
            raw_payload_json={"ok": True},
            retrieved_at="2026-06-15T00:00:00+00:00",
        )
        payload = record.to_dict()
        self.assertEqual(payload["canonical_url"], "https://example.com/")
        self.assertEqual(payload["title"], "Acme")
        self.assertEqual(payload["source"], "naver_web")
        self.assertEqual(payload["processing_status"], "raw")

    def test_representative_images_are_ranked_without_non_http_urls(self) -> None:
        html = """
        <meta property="og:image" content="/share.jpg">
        <header><img src="/logo.svg" width="80" height="40" alt="logo"></header>
        <article><img src="/story.webp" width="900" height="600" alt="story cover"></article>
        <img src="data:image/png;base64,nope">
        """
        self.assertEqual(
            extract_image_page_links(html, "https://example.com/post")[0],
            "https://example.com/share.jpg",
        )
        result = CrawlResult.build(url="https://example.com/post", crawler="static", html=html)
        self.assertNotIn("data:image/png;base64,nope", result.image_urls)

    def test_snapshot_images_prefer_large_content_image(self) -> None:
        images = extract_snapshot_image_links(
            [
                {"tag": "img", "css": "header .logo", "attrs": {"src": "/logo.svg"}, "bbox": {"w": 80, "h": 40}},
                {
                    "tag": "img",
                    "css": "main article img.hero",
                    "attrs": {"src": "/hero.jpg", "alt": "featured story"},
                    "bbox": {"w": 960, "h": 540},
                },
            ],
            "https://example.com/",
        )
        self.assertEqual(images[0], "https://example.com/hero.jpg")


if __name__ == "__main__":
    unittest.main()
