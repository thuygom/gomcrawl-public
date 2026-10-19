from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier, Event
import unittest
from unittest.mock import Mock, patch

from core import article_sentiment, news_result_cache
from core.article_cluster import cluster_news_items
from scripts import serve_crawl_dashboard as dashboard


def complete_result() -> dict[str, object]:
    return {
        "count": 1,
        "items": [{"title": "original", "cluster_id": "news-example-cluster-001"}],
        "article_cluster": {"status": "ok"},
        "article_sentiment": {"status": "ok"},
    }


class TemporaryCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "news.sqlite3"
        self.clock = 1_000.0
        for patcher in (
            patch.object(news_result_cache, "_news_cache_path", return_value=self.database),
            patch.object(news_result_cache, "_cache_now", side_effect=lambda: self.clock),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


class NewsResultCacheTest(TemporaryCacheTest):
    def test_result_expires_at_exactly_twenty_four_hours(self) -> None:
        compute = Mock(side_effect=[complete_result(), {**complete_result(), "generation": 2}])
        first, hit = news_result_cache.cached_news_result("company:example", compute)
        self.assertFalse(hit)
        self.assertEqual(news_result_cache.NEWS_RESULT_CACHE_TTL_SECONDS, 86_400)

        self.clock = 1_000.0 + 86_399.0
        cached, hit = news_result_cache.cached_news_result("company:example", compute)
        self.assertTrue(hit)
        self.assertEqual(cached, first)
        compute.assert_called_once()

        self.clock = 1_000.0 + 86_400.0
        refreshed, hit = news_result_cache.cached_news_result("company:example", compute)
        self.assertFalse(hit)
        self.assertEqual(refreshed["generation"], 2)
        self.assertEqual(compute.call_count, 2)

    def test_concurrent_misses_compute_once(self) -> None:
        ready = Barrier(4)
        started = Event()
        release = Event()

        def compute() -> dict[str, object]:
            started.set()
            if not release.wait(timeout=2):
                raise RuntimeError("test did not release computation")
            return complete_result()

        counted_compute = Mock(side_effect=compute)

        def request():
            ready.wait(timeout=2)
            return news_result_cache.cached_news_result("company:concurrent", counted_compute)

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(request) for _ in range(4)]
            try:
                self.assertTrue(started.wait(timeout=2))
            finally:
                release.set()
            results = [future.result(timeout=2) for future in futures]

        counted_compute.assert_called_once()
        self.assertEqual(sorted(hit for _, hit in results), [False, True, True, True])
        self.assertTrue(all(result == complete_result() for result, _ in results))

    def test_mutating_returned_results_does_not_change_stored_response(self) -> None:
        compute = Mock(return_value=complete_result())
        first, _ = news_result_cache.cached_news_result("company:copy", compute)
        first["items"][0]["title"] = "mutated miss"
        second, hit = news_result_cache.cached_news_result("company:copy", compute)
        self.assertTrue(hit)
        self.assertEqual(second["items"][0]["title"], "original")
        second["items"][0]["title"] = "mutated hit"
        third, hit = news_result_cache.cached_news_result("company:copy", compute)
        self.assertTrue(hit)
        self.assertEqual(third["items"][0]["title"], "original")
        compute.assert_called_once()

    def test_failed_analysis_and_failed_empty_search_are_not_cached(self) -> None:
        failures = [
            {**complete_result(), "article_sentiment": {"status": "error"}},
            {**complete_result(), "article_cluster": {"status": "failed"}},
            {
                "count": 0,
                "items": [],
                "article_sentiment": {"status": "skipped"},
                "article_cluster": {"status": "ok"},
                "subtasks": [{"status": "failed"}],
            },
        ]
        for index, failed in enumerate(failures):
            with self.subTest(failure=index):
                compute = Mock(return_value=failed)
                for _ in range(2):
                    result, hit = news_result_cache.cached_news_result(f"failed:{index}", compute)
                    self.assertFalse(hit)
                    self.assertEqual(result, failed)
                self.assertEqual(compute.call_count, 2)

    def test_computation_exception_does_not_block_retry(self) -> None:
        compute = Mock(side_effect=[RuntimeError("search failed"), complete_result()])
        with self.assertRaisesRegex(RuntimeError, "search failed"):
            news_result_cache.cached_news_result("company:retry", compute)
        result, hit = news_result_cache.cached_news_result("company:retry", compute)
        self.assertFalse(hit)
        self.assertEqual(result, complete_result())
        _, hit = news_result_cache.cached_news_result("company:retry", compute)
        self.assertTrue(hit)
        self.assertEqual(compute.call_count, 2)


class FakeRuntime:
    model = "test-local-sentiment"

    def __init__(self) -> None:
        self.calls = 0

    def predict(self, masked_texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[0.04, 0.06, 0.9] for _ in masked_texts]


class JsonHandler(dashboard.DashboardHandler):
    def __init__(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json"}
        self.rfile = BytesIO(body)
        self.wfile = BytesIO()
        self.path = "/v1/complete/news"
        self.status = None

    def send_response(self, status: int, message=None) -> None:
        self.status = status

    def send_header(self, keyword: str, value: str) -> None:
        pass

    def end_headers(self) -> None:
        pass


class NewsGatewayCacheTest(TemporaryCacheTest):
    def setUp(self) -> None:
        super().setUp()
        self.runtime = FakeRuntime()
        for name, patcher in (
            ("search", patch.object(dashboard, "complete_for_purpose", side_effect=self.search_result)),
            ("sentiment", patch.object(dashboard, "evaluate_article_sentiment_batch", wraps=article_sentiment.evaluate_article_sentiment_batch)),
            ("cluster", patch.object(dashboard, "cluster_news_items", wraps=cluster_news_items)),
            ("usage", patch.object(dashboard, "record_api_usage", return_value=None)),
            ("runtime_loader", patch.object(article_sentiment, "get_article_sentiment_runtime", return_value=self.runtime)),
            ("sentiment_disabled", patch.object(article_sentiment, "article_sentiment_disabled", return_value=False)),
        ):
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def search_result(self, purpose: str, payload: dict[str, object]) -> dict[str, object]:
        self.assertEqual(purpose, "news")
        target = str((payload.get("company") or {}).get("회사명") or payload.get("query") or "Example Corp")
        return {
            "schema_version": "noteapp_complete/v1",
            "purpose": "news",
            "endpoint": "/v1/complete/news",
            "completion_status": "completed",
            "query": target,
            "search_keyword": target,
            "request": deepcopy(payload),
            "count": 2,
            "items": [
                {
                    "title": f"{target} 반도체 양산 로드맵 {ending}",
                    "article_url": f"https://news.example/article-{index}",
                    "published_at": f"2026-09-07T0{3 - index}:00:00Z",
                    "thumb_urls": [f"https://images.example/{index}.jpg"],
                }
                for index, ending in enumerate(("발표", "공개"), start=1)
            ],
            "search_metadata": {"searched_at": "2026-09-07T03:00:00Z"},
        }

    def invoke(self, payload: dict[str, object]) -> dict[str, object]:
        handler = JsonHandler(payload)
        handler.do_POST()
        self.assertEqual(handler.status, 200, handler.wfile.getvalue().decode())
        return json.loads(handler.wfile.getvalue())

    def test_same_company_reuses_full_analysis_without_sharing_account_context(self) -> None:
        with patch.object(dashboard, "now", side_effect=[10.0, 10.4, 20.0, 20.9]):
            first = self.invoke({
                "accountId": "PRIVATE_ACCOUNT_A",
                "company": {"회사명": "주식회사  Example   Corp", "홈페이지": "https://private.example"},
                "person": {"이름": "PRIVATE_PERSON_A"},
                "metadata": {"marker": "PRIVATE_METADATA_A"},
                "limit": 3,
                "crawl_business_news": False,
            })
            second = self.invoke({
                "accountId": "PRIVATE_ACCOUNT_B",
                "company": {"회사명": "(주) example corp"},
                "person": {"이름": "PRIVATE_PERSON_B"},
                "limit": 10,
                "crawl_business_news": True,
            })

        self.search.assert_called_once_with("news", {"company": {"회사명": "Example Corp"}, "limit": 3, "crawl_business_news": False})
        self.sentiment.assert_called_once()
        self.cluster.assert_called_once()
        self.assertEqual(self.runtime.calls, 1)
        self.assertEqual(first["cache"], {"hit": False, "ttl_seconds": 86_400})
        self.assertEqual(second["cache"], {"hit": True, "ttl_seconds": 86_400})
        for field in ("items", "article_sentiment", "article_cluster", "search_metadata", "request"):
            self.assertEqual(first[field], second[field], field)
        self.assertEqual(first["accountId"], "PRIVATE_ACCOUNT_A")
        self.assertEqual(second["accountId"], "PRIVATE_ACCOUNT_B")
        self.assertEqual(first["gateway_elapsed_seconds"], 0.4)
        self.assertEqual(second["gateway_elapsed_seconds"], 0.9)
        with sqlite3.connect(self.database) as database:
            stored = database.execute("SELECT payload_json FROM responses").fetchone()[0]
        self.assertNotIn("PRIVATE_", stored)
        self.assertNotIn("private.example", stored)
        self.assertNotIn("accountId", stored)

    def test_other_company_and_query_have_separate_cache_entries(self) -> None:
        first = self.invoke({"company": {"회사명": "Example Corp"}})
        other = self.invoke({"company": {"회사명": "Other Corp"}})
        query = self.invoke({"query": "  Battery   Roadmap  ", "accountId": "PRIVATE_ACCOUNT", "metadata": {"marker": "PRIVATE_METADATA"}})
        query_hit = self.invoke({"query": "battery roadmap", "limit": 1})

        self.assertFalse(first["cache"]["hit"])
        self.assertFalse(other["cache"]["hit"])
        self.assertFalse(query["cache"]["hit"])
        self.assertTrue(query_hit["cache"]["hit"])
        self.assertEqual(self.search.call_count, 3)
        self.assertEqual(self.sentiment.call_count, 3)
        self.assertEqual(self.cluster.call_count, 3)
        self.assertEqual(self.search.call_args.args[1], {"query": "Battery Roadmap"})
        self.assertEqual(query["items"], query_hit["items"])

    def test_explicit_url_bypasses_result_cache(self) -> None:
        payload = {"url": "https://news.example/direct", "company": {"회사명": "Example Corp"}}
        first = self.invoke(payload)
        second = self.invoke(payload)

        self.assertEqual(self.search.call_count, 2)
        self.assertEqual(self.sentiment.call_count, 2)
        self.assertEqual(self.cluster.call_count, 2)
        self.assertNotIn("cache", first)
        self.assertNotIn("cache", second)
        self.assertFalse(self.database.exists())
        self.assertNotEqual(first["items"][0]["cluster_id"], second["items"][0]["cluster_id"])


if __name__ == "__main__":
    unittest.main()
