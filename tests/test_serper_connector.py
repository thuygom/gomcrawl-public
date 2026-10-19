from __future__ import annotations

import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from core.connectors.serper import (
    SERPER_COMPANY_CACHE_TTL_SECONDS,
    SERPER_PERSON_CACHE_TTL_SECONDS,
    SerperSearchClient,
)


class SerperConnectorTest(unittest.TestCase):
    def test_builds_records_from_organic_results(self) -> None:
        client = object.__new__(SerperSearchClient)
        payload = {
            "credits": 1,
            "organic": [
                {
                    "title": "<b>OpenAI</b>",
                    "link": "https://openai.com/",
                    "snippet": "Official AI research lab.",
                    "position": 1,
                }
            ],
        }

        records = client._records_from_payload(payload, query="OpenAI official", query_intent=None)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].source, "serper")
        self.assertEqual(records[0].title, "OpenAI")
        self.assertEqual(records[0].url, "https://openai.com/")
        self.assertEqual(client._credit_units(payload), 1)

    def test_noteapp_news_uses_serper_news_search_in_korean(self) -> None:
        client = object.__new__(SerperSearchClient)
        client.api_key = "test-key"
        client.timeout = 4.5
        response = MagicMock(status_code=200, headers={})
        response.json.return_value = {
            "credits": 1,
            "news": [{"title": "이즈파크 기사", "link": "https://news.example/1", "date": "1개월 전"}],
        }
        http = MagicMock()
        http.__enter__.return_value = http
        http.post.return_value = response

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"WEB_SEARCH_USAGE_DIR": f"{tmp}/usage"}
        ), patch("core.connectors.serper.httpx.Client", return_value=http), patch(
            "core.connectors.serper.record_api_usage"
        ):
            records = client.search('"이즈파크" 기사', limit=10, query_intent="noteapp_complete:news")

        self.assertEqual(http.post.call_args.args[0], client.news_endpoint)
        self.assertEqual(http.post.call_args.kwargs["json"]["gl"], "kr")
        self.assertEqual(http.post.call_args.kwargs["json"]["hl"], "ko")
        self.assertEqual(records[0].published_at, "1개월 전")

    def test_company_search_reuses_response_until_twenty_hour_ttl_expires(self) -> None:
        def client() -> SerperSearchClient:
            instance = object.__new__(SerperSearchClient)
            instance.api_key = "test-key"
            instance.timeout = 4.5
            return instance

        response = MagicMock(status_code=200, headers={})
        response.json.return_value = {
            "credits": 1,
            "organic": [{"title": "OpenAI", "link": "https://openai.com/"}],
        }
        http = MagicMock()
        http.__enter__.return_value = http
        http.post.return_value = response
        now = 1_800_000_000.0

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"WEB_SEARCH_USAGE_DIR": f"{tmp}/usage"}
        ), patch("core.connectors.serper.httpx.Client", return_value=http), patch(
            "core.connectors.serper.record_api_usage"
        ) as usage, patch("core.connectors.serper._cache_now", return_value=now) as clock:
            first = client().search("OpenAI official", limit=3)
            clock.return_value = now + SERPER_COMPANY_CACHE_TTL_SECONDS - 1
            cached = client().search("OpenAI official", limit=3)
            clock.return_value = now + SERPER_COMPANY_CACHE_TTL_SECONDS + 1
            refreshed = client().search("OpenAI official", limit=3)

        self.assertEqual(http.post.call_count, 2)
        self.assertEqual(first[0].retrieved_at, cached[0].retrieved_at)
        self.assertNotEqual(cached[0].retrieved_at, refreshed[0].retrieved_at)
        self.assertEqual([call.args[0] for call in usage.call_args_list], ["serper", "serper_cache", "serper"])

    def test_person_search_reuses_response_until_three_day_ttl_expires(self) -> None:
        client = object.__new__(SerperSearchClient)
        client.api_key = "test-key"
        client.timeout = 4.5
        response = MagicMock(status_code=200, headers={})
        response.json.return_value = {
            "credits": 1,
            "organic": [{"title": "김갑산", "link": "https://example.com/profile"}],
        }
        http = MagicMock()
        http.__enter__.return_value = http
        http.post.return_value = response
        now = 1_800_000_000.0

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"WEB_SEARCH_USAGE_DIR": f"{tmp}/usage"}
        ), patch("core.connectors.serper.httpx.Client", return_value=http), patch(
            "core.connectors.serper.record_api_usage"
        ), patch("core.connectors.serper._cache_now", return_value=now) as clock:
            first = client.search(
                "김갑산 이즈파크", limit=3, query_intent="person_evidence:publications:academic_web"
            )
            clock.return_value = now + SERPER_COMPANY_CACHE_TTL_SECONDS + 1
            cached = client.search(
                "김갑산 이즈파크", limit=3, query_intent="person_evidence:publications:academic_web"
            )
            clock.return_value = now + SERPER_PERSON_CACHE_TTL_SECONDS + 1
            refreshed = client.search(
                "김갑산 이즈파크", limit=3, query_intent="person_evidence:publications:academic_web"
            )

        self.assertEqual(http.post.call_count, 2)
        self.assertEqual(first[0].retrieved_at, cached[0].retrieved_at)
        self.assertNotEqual(cached[0].retrieved_at, refreshed[0].retrieved_at)

    def test_refresh_merges_unique_results_instead_of_adding_duplicates(self) -> None:
        client = object.__new__(SerperSearchClient)
        client.api_key = "test-key"
        client.timeout = 4.5
        first_payload = {
            "credits": 1,
            "organic": [
                {"title": f"Result {index}", "link": f"https://example.com/{index}?utm_source=first"}
                for index in range(10)
            ],
        }
        second_payload = {
            "credits": 1,
            "organic": [
                {"title": f"Result {index}", "link": f"https://example.com/{index}"}
                for index in range(1, 11)
            ],
        }
        responses = []
        for payload in (first_payload, second_payload):
            response = MagicMock(status_code=200, headers={})
            response.json.return_value = payload
            responses.append(response)
        http = MagicMock()
        http.__enter__.return_value = http
        http.post.side_effect = responses
        now = 1_800_000_000.0

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"WEB_SEARCH_USAGE_DIR": f"{tmp}/usage"}
        ), patch("core.connectors.serper.httpx.Client", return_value=http), patch(
            "core.connectors.serper.record_api_usage"
        ), patch("core.connectors.serper._cache_now", return_value=now) as clock:
            first = client.search("OpenAI news", limit=10)
            clock.return_value = now + SERPER_COMPANY_CACHE_TTL_SECONDS + 1
            merged = client.search("OpenAI news", limit=10)
            cached = client.search("OpenAI news", limit=10)

        self.assertEqual(len(first), 10)
        self.assertEqual(len(merged), 11)
        self.assertEqual(len(cached), 11)
        self.assertEqual(len({record.canonical_url for record in merged}), 11)
        self.assertEqual(http.post.call_count, 2)

    def test_coalesces_concurrent_identical_queries(self) -> None:
        def client() -> SerperSearchClient:
            instance = object.__new__(SerperSearchClient)
            instance.api_key = "test-key"
            instance.timeout = 4.5
            return instance

        response = MagicMock(status_code=200, headers={})
        response.json.return_value = {
            "credits": 1,
            "organic": [{"title": "OpenAI", "link": "https://openai.com/"}],
        }
        http = MagicMock()
        http.__enter__.return_value = http
        http.post.side_effect = lambda *_args, **_kwargs: (time.sleep(0.05), response)[1]

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"WEB_SEARCH_USAGE_DIR": f"{tmp}/usage"}
        ), patch("core.connectors.serper.httpx.Client", return_value=http), patch(
            "core.connectors.serper.record_api_usage"
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _index: client().search("OpenAI official", limit=3), range(2)))

        self.assertEqual(http.post.call_count, 1)
        self.assertEqual([result[0].url for result in results], ["https://openai.com/", "https://openai.com/"])


if __name__ == "__main__":
    unittest.main()
