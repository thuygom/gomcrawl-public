from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from core.connectors.naver import NaverSearchClient


class NaverSearchClientTest(unittest.TestCase):
    def test_noteapp_news_search_requests_latest_first(self) -> None:
        client = object.__new__(NaverSearchClient)
        client.provider = "naver_news"
        client.client_id = "client-id"
        client.client_secret = "client-secret"
        client.timeout = 4.5
        response = MagicMock(status_code=200, headers={})
        response.json.return_value = {"items": []}
        http = MagicMock()
        http.__enter__.return_value = http
        http.get.return_value = response

        with patch("core.connectors.naver.httpx.Client", return_value=http), patch("core.connectors.naver.record_api_usage"):
            client.search('"이즈파크"', limit=10, query_intent="noteapp_complete:news")

        self.assertEqual(http.get.call_args.kwargs["params"]["sort"], "date")


if __name__ == "__main__":
    unittest.main()
