from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .utils import (
    as_jsonable,
    canonicalize_url,
    compact_text,
    detect_pii,
    extract_image_page_links,
    make_result_id,
    policy_risk_for_text,
    strip_html,
    utc_now_iso,
)


@dataclass(slots=True)
class EvidenceRecord:
    result_id: str
    source: str
    source_type: str
    title: str
    url: str
    canonical_url: str
    snippet: str = ""
    raw_payload_json: dict[str, Any] = field(default_factory=dict)
    published_at: str | None = None
    retrieved_at: str = field(default_factory=utc_now_iso)
    query: str | None = None
    query_intent: str | None = None
    entity_candidates: list[dict[str, Any]] = field(default_factory=list)
    matched_entity_id: str | None = None
    confidence: float = 0.3
    confidence_reason: str = ""
    policy_risk: str = "low"
    pii_flags: list[str] = field(default_factory=list)
    processing_status: str = "raw"

    @classmethod
    def build(
        cls,
        *,
        source: str,
        source_type: str,
        title: str,
        url: str,
        snippet: str = "",
        raw_payload_json: dict[str, Any] | None = None,
        published_at: str | None = None,
        retrieved_at: str | None = None,
        query: str | None = None,
        query_intent: str | None = None,
        confidence: float = 0.3,
        confidence_reason: str = "",
        processing_status: str = "raw",
    ) -> "EvidenceRecord":
        retrieved = retrieved_at or utc_now_iso()
        canonical = canonicalize_url(url)
        clean_title = strip_html(title)
        clean_snippet = strip_html(snippet)
        text = " ".join([clean_title or "", clean_snippet or ""])
        pii_flags = detect_pii(text)
        policy_risk = policy_risk_for_text(text, url)
        return cls(
            result_id=make_result_id(source, canonical, retrieved),
            source=source,
            source_type=source_type,
            title=clean_title or canonical,
            url=url,
            canonical_url=canonical,
            snippet=compact_text(clean_snippet),
            raw_payload_json=as_jsonable(raw_payload_json or {}),
            published_at=published_at,
            retrieved_at=retrieved,
            query=query,
            query_intent=query_intent,
            confidence=confidence,
            confidence_reason=confidence_reason,
            policy_risk=policy_risk,
            pii_flags=pii_flags,
            processing_status=processing_status,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CrawlResult:
    url: str
    canonical_url: str
    crawler: str
    title: str
    text: str
    status_code: int | None = None
    retrieved_at: str = field(default_factory=utc_now_iso)
    raw_payload: dict[str, Any] = field(default_factory=dict)
    markdown: str | None = None
    html: str | None = None
    screenshot_path: str | None = None
    error: str | None = None
    image_urls: list[str] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        *,
        url: str,
        crawler: str,
        title: str = "",
        text: str = "",
        status_code: int | None = None,
        raw_payload: dict[str, Any] | None = None,
        markdown: str | None = None,
        html: str | None = None,
        screenshot_path: str | None = None,
        error: str | None = None,
        image_urls: list[str] | None = None,
    ) -> "CrawlResult":
        resolved_image_urls = image_urls if image_urls is not None else extract_image_page_links(html, base_url=url)
        return cls(
            url=url,
            canonical_url=canonicalize_url(url),
            crawler=crawler,
            title=compact_text(title),
            text=compact_text(text),
            status_code=status_code,
            raw_payload=as_jsonable(raw_payload or {}),
            markdown=markdown,
            html=html,
            screenshot_path=screenshot_path,
            error=error,
            image_urls=list(resolved_image_urls),
        )

    def to_evidence(self, *, query: str | None = None, query_intent: str | None = None) -> EvidenceRecord:
        snippet = self.text[:2000]
        confidence = 0.7 if self.text and not self.error else 0.1
        reason = f"{self.crawler} crawl"
        if self.error:
            reason = f"{self.crawler} crawl error: {self.error}"
        return EvidenceRecord.build(
            source="crawl_allowlist",
            source_type="crawl_like",
            title=self.title or self.canonical_url,
            url=self.url,
            snippet=snippet,
            raw_payload_json={
                "crawler": self.crawler,
                "status_code": self.status_code,
                "canonical_url": self.canonical_url,
                "text_length": len(self.text or ""),
                "screenshot_path": self.screenshot_path,
                "error": self.error,
                **self.raw_payload,
            },
            retrieved_at=self.retrieved_at,
            query=query,
            query_intent=query_intent,
            confidence=confidence,
            confidence_reason=reason,
            processing_status="raw" if not self.error else "error",
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
