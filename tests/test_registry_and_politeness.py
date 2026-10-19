from __future__ import annotations

import unittest

from core.credentials import key_file_candidate_strings
from core.connectors.base import SearchAgentError
from core.connectors.factory import create_search_client
from core.politeness import PoliteRequestGate, PolitenessPolicy, is_government_host, retry_after_seconds
from core.registry import available_crawlers, available_search_providers
from core.tools import available_crawler_tools


class RegistryAndPolitenessTest(unittest.TestCase):
    def test_registry_exposes_policy_fields(self) -> None:
        providers = {item["provider"]: item for item in available_search_providers()}
        self.assertEqual(providers["naver_web"]["priority"], "P1")
        self.assertIn("NAVER_CLIENT_ID", providers["naver_web"]["required_env"])
        self.assertIn(
            "naver_client_id.key",
            " ".join(providers["naver_web"]["credential_file_candidates"]["NAVER_CLIENT_ID"]),
        )
        self.assertTrue(providers["serper"]["bulk_discovery"])
        self.assertIn("SERPER_API_KEY", providers["serper"]["required_env"])
        self.assertFalse(providers["watercrawl_search"]["bulk_discovery"])
        self.assertIn("WATERCRAWL_API_URL", providers["watercrawl_search"]["required_env"])
        self.assertIn("WATERCRAWL_API_KEY", providers["watercrawl_search"]["required_env"])
        self.assertFalse(providers["apify_linkedin_profile_search"]["bulk_discovery"])
        self.assertIn("APIFY_API_TOKEN", providers["apify_linkedin_profile_search"]["required_env"])
        self.assertNotIn("naver_book", providers)
        with self.assertRaisesRegex(SearchAgentError, "unknown search provider"):
            create_search_client("naver_book")

        crawlers = {item["crawler"]: item for item in available_crawlers()}
        self.assertEqual(crawlers["static"]["same_domain_concurrency"], 1)
        self.assertTrue(crawlers["auto"]["obeys_robots"])
        self.assertEqual(crawlers["firecrawl"]["engine_type"], "managed_remote")
        self.assertFalse(crawlers["firecrawl"]["local_target_politeness"])
        self.assertEqual(crawlers["watercrawl"]["engine_type"], "managed_remote")
        self.assertFalse(crawlers["watercrawl"]["local_target_politeness"])
        self.assertIn("WATERCRAWL_API_KEY", crawlers["watercrawl"]["required_env"])
        self.assertEqual(crawlers["stealth_browser"]["engine_type"], "local_direct_stealth_browser")
        self.assertEqual(crawlers["apify_linkedin"]["engine_type"], "managed_remote")
        self.assertIn("APIFY_API_TOKEN", crawlers["apify_linkedin"]["required_env"])
        self.assertIn(
            "apify.key",
            " ".join(crawlers["apify_linkedin"]["credential_file_candidates"]["APIFY_API_TOKEN"]),
        )

    def test_retry_after_seconds_parses_delta(self) -> None:
        self.assertEqual(retry_after_seconds("60"), 60.0)
        self.assertIsNone(retry_after_seconds("not a date"))

    def test_crawler_tools_are_mcp_shaped(self) -> None:
        tools = {item["name"]: item for item in available_crawler_tools()}
        self.assertEqual(
            set(tools),
            {
                "web_search_static_crawl",
                "web_search_browser_crawl",
                "web_search_managed_firecrawl_crawl",
                "web_search_managed_watercrawl_crawl",
                "web_search_stealth_browser_crawl",
                "web_search_managed_apify_linkedin_crawl",
            },
        )
        self.assertEqual(tools["web_search_static_crawl"]["inputSchema"]["required"], ["url"])
        self.assertEqual(
            tools["web_search_static_crawl"]["implementation"]["gateway"]["url"],
            "http://127.0.0.1:9101/api/crawl",
        )
        self.assertIn("capabilities", tools["web_search_browser_crawl"])
        self.assertEqual(
            tools["web_search_managed_firecrawl_crawl"]["required_environment"],
            ("FIRECRAWL_API_KEY",),
        )
        self.assertEqual(
            tools["web_search_managed_watercrawl_crawl"]["required_environment"],
            ("WATERCRAWL_API_URL", "WATERCRAWL_API_KEY"),
        )
        self.assertEqual(
            tools["web_search_stealth_browser_crawl"]["implementation"]["gateway"]["crawler"],
            "stealth_browser",
        )
        self.assertFalse(
            tools["web_search_managed_firecrawl_crawl"]["policy"]["local_target_politeness"]
        )
        self.assertEqual(
            tools["web_search_managed_apify_linkedin_crawl"]["required_environment"],
            ("APIFY_API_TOKEN",),
        )
        self.assertFalse(
            tools["web_search_managed_apify_linkedin_crawl"]["policy"]["broad_people_search"]
        )

        browser_tools = available_crawler_tools("browser")
        self.assertEqual({item["name"] for item in browser_tools}, {"web_search_browser_crawl", "web_search_stealth_browser_crawl"})

    def test_government_host_uses_conservative_delay(self) -> None:
        self.assertTrue(is_government_host("example.go.kr"))
        self.assertTrue(is_government_host("agency.gov"))
        self.assertFalse(is_government_host("example.com"))

        now = [100.0]
        slept: list[float] = []

        def clock() -> float:
            return now[0]

        def sleeper(seconds: float) -> None:
            slept.append(seconds)
            now[0] += seconds

        gate = PoliteRequestGate(
            PolitenessPolicy(
                base_delay_seconds=2.0,
                government_delay_seconds=10.0,
                respect_robots=False,
            ),
            sleeper=sleeper,
            clock=clock,
            jitter=lambda _low, _high: 1.0,
        )

        gate.record_response("https://service.go.kr/page-a", 200, {})
        gate.wait_before_request("https://service.go.kr/page-b")
        self.assertEqual(slept, [10.0])

    def test_retry_after_overrides_base_delay(self) -> None:
        gate = PoliteRequestGate(
            PolitenessPolicy(base_delay_seconds=2.0, respect_robots=False),
            clock=lambda: 0.0,
            sleeper=lambda _seconds: None,
            jitter=lambda _low, _high: 1.0,
        )
        gate.record_response("https://example.com/a", 429, {"Retry-After": "90"})
        report = gate.host_report()["example.com"]
        self.assertEqual(report["last_delay_seconds"], 90.0)
        self.assertEqual(report["consecutive_limited"], 1)

    def test_key_file_candidates_include_project_and_workspace_key_dirs(self) -> None:
        candidates = key_file_candidate_strings("FIRECRAWL_API_KEY")
        self.assertTrue(any(path.endswith("/key/firecrawl.key") for path in candidates))
        self.assertTrue(any(path.endswith("/key/firecrawl.key") for path in candidates))
        youtube_candidates = key_file_candidate_strings("YOUTUBE_API_KEY")
        self.assertTrue(any(path.endswith("/key/youtube_v3.key") for path in youtube_candidates))
        serper_candidates = key_file_candidate_strings("SERPER_API_KEY")
        self.assertTrue(any(path.endswith("/key/serper.key") for path in serper_candidates))
        self.assertTrue(any(path.endswith("/key/serper.jey") for path in serper_candidates))
        watercrawl_candidates = key_file_candidate_strings("WATERCRAWL_API_KEY")
        self.assertTrue(any(path.endswith("/key/watercrawl.key") for path in watercrawl_candidates))
        watercrawl_url_candidates = key_file_candidate_strings("WATERCRAWL_API_URL")
        self.assertTrue(any(path.endswith("/key/watercrawl.url") for path in watercrawl_url_candidates))
        apify_candidates = key_file_candidate_strings("APIFY_API_TOKEN")
        self.assertTrue(any(path.endswith("/key/apify.key") for path in apify_candidates))
        self.assertTrue(any(path.endswith("/key/apify2.key") for path in apify_candidates))
        actor_candidates = key_file_candidate_strings("APIFY_LINKEDIN_ACTOR_ID")
        self.assertTrue(any(path.endswith("/key/apify_linkedin_actor_id.key") for path in actor_candidates))
        company_actor_candidates = key_file_candidate_strings("APIFY_LINKEDIN_COMPANY_ACTOR_ID")
        self.assertTrue(
            any(path.endswith("/key/apify_linkedin_company_actor_id.key") for path in company_actor_candidates)
        )
        profile_search_actor_candidates = key_file_candidate_strings("APIFY_LINKEDIN_PROFILE_SEARCH_ACTOR_ID")
        self.assertTrue(
            any(path.endswith("/key/apify_linkedin_profile_search_actor_id.key") for path in profile_search_actor_candidates)
        )
        openai_candidates = key_file_candidate_strings("OPENAI_API_KEY")
        self.assertTrue(any(path.endswith("/key/openai.key") for path in openai_candidates))
        self.assertTrue(any(path.endswith("/key/openai.txt") for path in openai_candidates))


if __name__ == "__main__":
    unittest.main()
