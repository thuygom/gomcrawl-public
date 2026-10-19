from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.bot_detection import summarize_bot_detections
from core.crawlers import ObservedCrawler
from core.schema import CrawlResult
from core.usage import render_prometheus_metrics, summarize_usage


class BotDetectionTest(unittest.TestCase):
    def test_shared_crawler_hook_logs_and_exports_metrics(self) -> None:
        class FakeCrawler:
            name = "static"

            def crawl(self, url: str) -> CrawlResult:
                return CrawlResult.build(
                    url=url,
                    crawler=self.name,
                    title="Just a moment...",
                    html='<script src="/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1"></script>',
                    status_code=200,
                )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = {
                "WEB_SEARCH_BOT_DETECTION_DIR": str(root / "bot"),
                "WEB_SEARCH_USAGE_DIR": str(root / "usage"),
            }
            with patch.dict(os.environ, env):
                result = ObservedCrawler(FakeCrawler()).crawl("https://example.com/private?q=secret")
                detection = result.raw_payload["bot_detection"]
                self.assertTrue(detection["detected"])
                self.assertEqual(detection["vendor"], "cloudflare")
                self.assertNotIn("notification", detection)
                self.assertIn("bot challenge detected", result.error or "")

                event_path = next((root / "bot").glob("*.jsonl"))
                event_text = event_path.read_text(encoding="utf-8")
                event = json.loads(event_text.splitlines()[-1])
                self.assertEqual(event["host"], "example.com")
                self.assertNotIn("/private", event_text)
                self.assertNotIn("q=secret", event_text)
                self.assertEqual(event_path.stat().st_mode & 0o777, 0o600)

                summary = summarize_bot_detections()
                self.assertEqual(summary["detected_count"], 1)
                self.assertEqual(summary["problem_count"], 1)

                metrics = render_prometheus_metrics(summarize_usage(), {"providers": []})
                self.assertIn('provider="bot_detection"', metrics)
                self.assertIn('operation="blocked_cloudflare_hard"', metrics)


if __name__ == "__main__":
    unittest.main()
