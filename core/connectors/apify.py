from __future__ import annotations

import re
import time
from typing import Any

import httpx

from ..credentials import secret_for_env
from ..schema import EvidenceRecord
from ..usage import record_api_usage
from ..utils import detect_pii, strip_html
from .base import SearchAgentError, require_secret


class ApifyLinkedInProfileSearchClient:
    provider = "apify_linkedin_profile_search"
    api_base = "https://api.apify.com/v2"
    default_actor_id = "harvestapi/linkedin-profile-search-by-name"
    school_aliases = (
        ("Hansung University", "Hansung University"),
        ("한성대학교", "Hansung University"),
        ("한성대", "Hansung University"),
    )

    email_re = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.I)

    def __init__(self) -> None:
        self.api_token = require_secret(secret_for_env("APIFY_API_TOKEN"), "APIFY_API_TOKEN")
        self.actor_id = (
            secret_for_env("APIFY_LINKEDIN_PROFILE_SEARCH_ACTOR_ID")
            or self.default_actor_id
        )

    @staticmethod
    def _actor_path_id(actor_id: str) -> str:
        normalized = actor_id.strip()
        if "/" in normalized and "~" not in normalized:
            return normalized.replace("/", "~")
        return normalized

    def _clean_query(self, query: str) -> str:
        return re.sub(r"\s+", " ", self.email_re.sub(" ", query or "")).strip()

    def _schools_from_query(self, query: str) -> list[str]:
        clean = self._clean_query(query)
        lowered = clean.lower()
        schools: list[str] = []
        for alias, normalized in self.school_aliases:
            if alias.lower() in lowered and normalized not in schools:
                schools.append(normalized)
        return schools

    def _query_without_filter_terms(self, query: str) -> str:
        clean = self._clean_query(query)
        for alias, _normalized in sorted(self.school_aliases, key=lambda item: len(item[0]), reverse=True):
            clean = re.sub(re.escape(alias), " ", clean, flags=re.I)
        return re.sub(r"\s+", " ", clean).strip()

    def _schools_from_filters(self, filters: dict[str, object] | None) -> list[str]:
        if not filters:
            return []
        raw = filters.get("schools")
        values = raw if isinstance(raw, list) else [raw]
        schools: list[str] = []
        for value in values:
            text = str(value or "").strip()
            if not text:
                continue
            normalized = text
            for alias, school in self.school_aliases:
                if alias.lower() in text.lower():
                    normalized = school
                    break
            if normalized not in schools:
                schools.append(normalized)
        return schools

    def _linkedin_company_urls_from_filters(self, filters: dict[str, object] | None) -> list[str]:
        if not filters:
            return []
        raw = (
            filters.get("currentCompanies")
            or filters.get("current_companies")
            or filters.get("company_urls")
            or filters.get("companies")
        )
        values = raw if isinstance(raw, list) else [raw]
        urls: list[str] = []
        for value in values:
            text = str(value or "").strip()
            if "linkedin.com/company/" not in text.lower():
                continue
            if text not in urls:
                urls.append(text)
        return urls

    def _name_parts(self, query: str) -> tuple[str, str]:
        clean = self._query_without_filter_terms(query)
        tokens = [token for token in re.split(r"[\s,/|]+", clean) if token]
        if not tokens:
            return "", ""
        name = tokens[0]
        if re.fullmatch(r"[가-힣]{2,5}", name):
            return name[1:], name[:1]
        if len(tokens) >= 2:
            return tokens[0], tokens[1]
        return name, ""

    def _request_payload(
        self,
        query: str,
        *,
        limit: int,
        filters: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        first_name, last_name = self._name_parts(query)
        payload: dict[str, Any] = {
            "profileScraperMode": "Short",
            "strictSearch": True,
            "maxPages": 1,
            "maxItems": max(1, min(limit, 10)),
        }
        if first_name:
            payload["firstName"] = first_name
        if last_name:
            payload["lastName"] = last_name
        schools = self._schools_from_filters(filters) or self._schools_from_query(query)
        if schools:
            payload["schools"] = schools[:10]
        company_urls = self._linkedin_company_urls_from_filters(filters)
        if company_urls:
            payload["currentCompanies"] = company_urls[:10]
        return payload

    def search_with_filters(
        self,
        query: str,
        *,
        filters: dict[str, object] | None = None,
        limit: int = 10,
        start: int = 1,
        query_intent: str | None = None,
    ) -> list[EvidenceRecord]:
        return self._search(
            query,
            filters=filters,
            limit=limit,
            start=start,
            query_intent=query_intent,
        )

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        start: int = 1,
        query_intent: str | None = None,
    ) -> list[EvidenceRecord]:
        return self._search(
            query,
            filters=None,
            limit=limit,
            start=start,
            query_intent=query_intent,
        )

    def enrich_profile(
        self,
        url: str,
        *,
        query: str,
        query_intent: str | None = None,
    ) -> EvidenceRecord:
        from ..crawlers import ApifyLinkedInCrawler

        result = ApifyLinkedInCrawler(max_items=1).crawl(url)
        if result.error:
            raise SearchAgentError(result.error)
        if not result.text:
            raise SearchAgentError("Apify LinkedIn profile actor returned no profile data")
        return EvidenceRecord.build(
            source=self.provider,
            source_type="social_profile_detail",
            title=result.title,
            url=result.url,
            snippet=result.text[:2000],
            raw_payload_json=result.raw_payload,
            query=query,
            query_intent=query_intent,
            confidence=0.8,
            confidence_reason="Apify full profile for the selected LinkedIn candidate",
            processing_status="enriched",
        )

    def _search(
        self,
        query: str,
        *,
        filters: dict[str, object] | None,
        limit: int = 10,
        start: int = 1,
        query_intent: str | None = None,
    ) -> list[EvidenceRecord]:
        started = time.monotonic()
        requested_limit = max(1, min(limit, 10))
        payload = self._request_payload(query, limit=limit, filters=filters)
        actor_path = self._actor_path_id(self.actor_id)
        endpoint = f"{self.api_base}/actors/{actor_path}/run-sync-get-dataset-items"
        params: dict[str, object] = {
            "format": "json",
            "clean": "true",
            "limit": requested_limit,
            "maxItems": requested_limit,
            "maxTotalChargeUsd": 0.5,
        }
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        response: httpx.Response | None = None
        empty_result_retried = False
        try:
            with httpx.Client(timeout=180, follow_redirects=True, headers=headers) as client:
                response = client.post(endpoint, params=params, json=payload)
                response.raise_for_status()
                raw = response.json()
                if isinstance(raw, list) and not raw:
                    empty_result_retried = True
                    relaxed_payload = {**payload, "strictSearch": False}
                    response = client.post(endpoint, params=params, json=relaxed_payload)
                    response.raise_for_status()
                    relaxed_raw = response.json()
                    raw = self._matching_name_items(relaxed_raw, payload)
            records = self._records_from_payload(
                raw,
                query=query,
                query_intent=query_intent,
                matched_filters=self._matched_filters_from_payload(payload),
            )
        except Exception as exc:
            record_api_usage(
                self.provider,
                "search",
                ok=False,
                status_code=response.status_code if response is not None else None,
                response_headers=response.headers if response is not None else None,
                metadata={
                    "error_type": type(exc).__name__,
                    "managed_service": "apify",
                    "actor_id": self.actor_id,
                    "email_sent_to_actor": False,
                    "empty_result_retried": empty_result_retried,
                },
                search_audit={
                    "query": query,
                    "query_intent": query_intent,
                    "requested_limit": requested_limit,
                    "start": start,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                },
            )
            raise

        offset = max(0, start - 1)
        sliced = records[offset : offset + requested_limit]
        record_api_usage(
            self.provider,
            "search",
            ok=True,
            status_code=response.status_code if response is not None else None,
            response_headers=response.headers if response is not None else None,
            result_count=len(sliced),
            metadata={
                "managed_service": "apify",
                "actor_id": self.actor_id,
                "email_sent_to_actor": False,
                "empty_result_retried": empty_result_retried,
            },
            search_audit={
                "query": query,
                "query_intent": query_intent,
                "requested_limit": requested_limit,
                "start": start,
                "result_urls": [record.url for record in sliced],
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return sliced

    def _records_from_payload(
        self,
        payload: Any,
        *,
        query: str,
        query_intent: str | None,
        matched_filters: dict[str, Any] | None = None,
    ) -> list[EvidenceRecord]:
        items = payload if isinstance(payload, list) else []
        records: list[EvidenceRecord] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = self._first_string(
                item,
                ("linkedinUrl", "profileUrl", "url", "link", "publicProfileUrl"),
            )
            if not url:
                public_id = self._first_string(item, ("publicIdentifier", "username", "linkedinSlug"))
                if public_id:
                    url = f"https://www.linkedin.com/in/{public_id}/"
            if not url:
                continue
            name = self._profile_name(item) or url
            headline = self._profile_headline(item)
            location = self._profile_location(item)
            snippet = " · ".join(part for part in (headline, location) if part)
            safe_payload = self._safe_payload(item, matched_filters=matched_filters)
            safe_payload["normalized_profile"] = {
                "name": name,
                "linkedin_url": url,
                "headline": headline,
                "location": location,
            }
            records.append(
                EvidenceRecord.build(
                    source=self.provider,
                    source_type="social_profile_result",
                    title=strip_html(name),
                    url=url,
                    snippet=strip_html(snippet),
                    raw_payload_json=safe_payload,
                    query=query,
                    query_intent=query_intent,
                    confidence=0.25,
                    confidence_reason="Apify LinkedIn profile search candidate; verify identity before use",
                )
            )
        return records

    @classmethod
    def _matching_name_items(cls, payload: Any, request_payload: dict[str, Any]) -> list[dict[str, Any]]:
        items = payload if isinstance(payload, list) else []
        first_name = str(request_payload.get("firstName") or "")
        last_name = str(request_payload.get("lastName") or "")
        expected = {
            cls._normalized_name(first_name + last_name),
            cls._normalized_name(last_name + first_name),
        }
        expected.discard("")
        return [
            item
            for item in items
            if isinstance(item, dict)
            and cls._normalized_name(cls._profile_name(item)) in expected
        ]

    @staticmethod
    def _normalized_name(value: str) -> str:
        return "".join(char for char in value.casefold() if char.isalnum())

    @staticmethod
    def _first_string(item: dict[str, Any], keys: tuple[str, ...]) -> str:
        for key in keys:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @classmethod
    def _profile_name(cls, item: dict[str, Any]) -> str:
        name = cls._first_string(item, ("name", "fullName", "title"))
        if name:
            return name
        return " ".join(
            part for part in (
                cls._first_string(item, ("firstName",)),
                cls._first_string(item, ("lastName",)),
            ) if part
        )

    @classmethod
    def _profile_headline(cls, item: dict[str, Any]) -> str:
        headline = cls._first_string(item, ("headline", "occupation", "position"))
        if headline:
            return headline
        current = item.get("currentPosition")
        positions = current if isinstance(current, list) else [current]
        for position in positions:
            if not isinstance(position, dict):
                continue
            text = " · ".join(
                part for part in (
                    cls._first_string(position, ("position", "title")),
                    cls._first_string(position, ("companyName", "company")),
                ) if part
            )
            if text:
                return text
        return ""

    @classmethod
    def _profile_location(cls, item: dict[str, Any]) -> str:
        location = cls._first_string(item, ("location", "geoLocationName"))
        if location:
            return location
        raw = item.get("location")
        if not isinstance(raw, dict):
            return ""
        location = cls._first_string(raw, ("linkedinText", "text", "name"))
        if location:
            return location
        parsed = raw.get("parsed")
        return cls._first_string(parsed, ("text", "city", "country")) if isinstance(parsed, dict) else ""

    @staticmethod
    def _matched_filters_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
        matched: dict[str, Any] = {}
        for key in ("schools", "currentCompanies"):
            value = payload.get(key)
            if isinstance(value, list) and value:
                matched[key] = value[:10]
        return matched

    def _safe_payload(self, item: dict[str, Any], *, matched_filters: dict[str, Any] | None = None) -> dict[str, Any]:
        blocked = ("email", "phone", "mobile", "telephone", "contact", "address", "birthday", "birthdate")
        clean: dict[str, Any] = {}
        for key, value in item.items():
            lowered = key.lower()
            if any(token in lowered for token in blocked):
                continue
            if isinstance(value, str):
                if "url" in lowered and value.startswith(("http://", "https://")):
                    clean[key] = value[:2000]
                    continue
                flags = detect_pii(value)
                clean[key] = f"[redacted:{','.join(flags)}]" if flags else value[:2000]
            elif isinstance(value, (int, float, bool)) or value is None:
                clean[key] = value
            elif isinstance(value, list):
                clean[key] = f"[list:{len(value)}]"
            elif isinstance(value, dict):
                clean[key] = f"[object:{len(value)}]"
            else:
                clean[key] = str(value)[:500]
        if matched_filters:
            clean["matched_filters"] = matched_filters
        return clean
