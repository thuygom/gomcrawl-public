from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.usage import (
    quota_snapshot_from_headers,
    record_api_usage,
    render_prometheus_metrics,
    summarize_search_audit,
    summarize_usage,
)


class UsageTest(unittest.TestCase):
    def test_quota_snapshot_parses_naver_headers(self) -> None:
        quota = quota_snapshot_from_headers(
            {
                "x-rate-limit": "10",
                "x-rate-limit-remaining": "9",
                "x-rate-limit-reset": "1781670353000",
            }
        )
        self.assertEqual(quota["limit"], 10)
        self.assertEqual(quota["remaining"], 9)
        self.assertEqual(quota["reset_raw"], "1781670353000")
        self.assertIn("reset_at", quota)

    def test_usage_summary_does_not_store_query_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {
                    "WEB_SEARCH_USAGE_DIR": tmp,
                    "WEB_SEARCH_DAILY_LIMIT_NAVER_SEARCH_OPENAPI": "20",
                },
                clear=False,
            ):
                event = record_api_usage(
                    "naver_web",
                    "search",
                    ok=True,
                    status_code=200,
                    response_headers={"x-rate-limit": "10", "x-rate-limit-remaining": "8"},
                    result_count=2,
                    metadata={"error_type": "none", "query": "민감한 검색어"},
                )
                self.assertIsNotNone(event)
                summary = summarize_usage(days=1)
                self.assertEqual(summary["query_or_url_values_logged"], False)
                rows = {item["provider"]: item for item in summary["providers"]}
                self.assertEqual(rows["naver_web"]["today_units"], 1)
                self.assertIsNone(rows["naver_web"]["configured_daily_remaining"])
                self.assertEqual(rows["naver_web"]["last_quota"]["remaining"], 8)
                self.assertIn("naver_web", summary["series"])
                self.assertEqual(summary["series"]["naver_web"][-1]["calls"], 1)
                self.assertEqual(summary["series"]["naver_web"][-1]["quota_remaining"], 8)
                self.assertIsNone(summary["series"]["naver_web"][-1]["configured_daily_remaining"])
                self.assertEqual(rows["naver_web"]["quota_scope"], "naver_search_openapi")
                scopes = {item["quota_scope"]: item for item in summary["quota_scopes"]}
                self.assertEqual(scopes["naver_search_openapi"]["today_units"], 1)
                self.assertEqual(scopes["naver_search_openapi"]["last_quota"]["remaining"], 8)
                self.assertEqual(scopes["naver_search_openapi"]["configured_daily_remaining"], 19)
                self.assertEqual(summary["quota_series"]["naver_search_openapi"][-1]["quota_remaining"], 8)
                self.assertEqual(summary["quota_series"]["naver_search_openapi"][-1]["configured_daily_remaining"], 19)

                usage_text = "\n".join(Path(path.path).read_text(encoding="utf-8") for path in os.scandir(tmp) if path.is_file())
                self.assertNotIn("민감한 검색어", usage_text)

    def test_search_audit_keeps_private_detail_out_of_public_summary(self) -> None:
        with tempfile.TemporaryDirectory() as usage_tmp, tempfile.TemporaryDirectory() as audit_tmp:
            with patch.dict(
                os.environ,
                {
                    "WEB_SEARCH_USAGE_DIR": usage_tmp,
                    "WEB_SEARCH_AUDIT_DIR": audit_tmp,
                },
                clear=False,
            ):
                record_api_usage(
                    "serper",
                    "search",
                    ok=True,
                    status_code=200,
                    result_count=1,
                    search_audit={
                        "query": 'site:example.com "민감한 이름"',
                        "query_intent": "person_evidence:publication",
                        "requested_limit": 3,
                        "result_urls": ["https://user:pass@example.com/private?q=secret#fragment"],
                        "duration_ms": 120,
                    },
                )
                private_text = "\n".join(
                    Path(path.path).read_text(encoding="utf-8")
                    for path in os.scandir(audit_tmp)
                    if path.is_file()
                )
                usage_text = "\n".join(
                    Path(path.path).read_text(encoding="utf-8")
                    for path in os.scandir(usage_tmp)
                    if path.is_file()
                )
                summary = summarize_search_audit(days=1)
                public_text = str(summary)

                self.assertIn("민감한 이름", private_text)
                self.assertIn("https://example.com/private", private_text)
                self.assertNotIn("user:pass", private_text)
                self.assertNotIn("?q=secret", private_text)
                self.assertNotIn("민감한 이름", usage_text)
                self.assertNotIn("민감한 이름", public_text)
                self.assertNotIn("/private", public_text)
                self.assertEqual(summary["target_domains"][0]["domain"], "example.com")
                self.assertEqual(summary["recent"][0]["result_domains"], ["example.com"])
                self.assertEqual(summary["sensitive_values_exposed"], False)

                metrics = render_prometheus_metrics(
                    summarize_usage(days=1),
                    summary,
                    [{"provider": "serper", "ready": True}],
                )
                self.assertIn('web_search_agent_provider_calls_total{provider="serper",quota_scope="serper_account"} 1', metrics)
                self.assertIn('web_search_agent_provider_ready{provider="serper"} 1', metrics)
                self.assertIn('web_search_agent_search_duration_milliseconds_sum{provider="serper"} 120', metrics)
                self.assertNotIn("민감한 이름", metrics)
                self.assertNotIn("example.com", metrics)


if __name__ == "__main__":
    unittest.main()
