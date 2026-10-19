import os
from urllib.parse import urlparse

from core.url_capability_router.models import (
    LinkClassificationResponse, 
    ClassificationOptions, 
    ProbeResult, 
    HandlingMode,
    Capabilities
)
from core.url_capability_router.normalizer import normalize_url
from core.url_capability_router.security import assert_safe_url, SecurityError
from core.url_capability_router.rules import RuleEngine
from core.url_capability_router.probe import probe_url

# Ensure relative path works regardless of where the script is run
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RULE_PATH = os.path.join(CURRENT_DIR, "platform-rules.yaml")

class Classifier:
    def __init__(self, yaml_path: str = DEFAULT_RULE_PATH):
        self.engine = RuleEngine(yaml_path)

    def classify(self, url: str, options: ClassificationOptions = None) -> LinkClassificationResponse:
        if options is None:
            options = ClassificationOptions(probe=False) # Only Phase 1 for now

        normalized = normalize_url(url)
        
        try:
            assert_safe_url(normalized, allow_private_network=options.allow_private_network)
        except SecurityError as e:
            # For now, let's just return a safe fallback or raise depending on design.
            # Returning a fallback is better for API stability
            return LinkClassificationResponse(
                url=url,
                normalized_url=normalized,
                final_url=normalized,
                classification="unknown",
                resource_type="unknown",
                handling_mode=HandlingMode.UNSUPPORTED,
                crawlable=False,
                capabilities=Capabilities(),
                preferred_handler="unsupported",
                fallback_chain=[],
                confidence=1.0,
                matched_rule="security_blocked",
                rule_version=self.engine.version,
                evidence=[],
                probe=ProbeResult(error=str(e))
            )

        parsed = urlparse(normalized)
        
        static_match = self.engine.match(parsed)
        
        should_probe = False
        if options.probe:
            if not static_match or static_match.confidence < 0.80:
                should_probe = True
                
        probe_result = ProbeResult(performed=False)
        if should_probe:
            probe_result = probe_url(normalized, options)

        classification_res = "webpage"
        resource_type = "webpage"
        handling_mode = HandlingMode.ARTICLE_EXTRACTOR
        crawlable = True
        preferred_handler = "generic_article_extractor"
        fallback_chain = ["plain_http", "browser"]
        confidence = 0.0
        matched_rule = "default_webpage"
        evidence = []
        
        if static_match:
            res = static_match.rule.result
            classification_res = res.classification
            resource_type = res.resource_type
            handling_mode = res.handling_mode
            crawlable = res.crawlable
            preferred_handler = res.preferred_handler
            fallback_chain = res.fallback_chain
            confidence = static_match.confidence
            matched_rule = static_match.rule.id
            evidence = static_match.evidence

        # Override with probe result if it clearly indicates a file
        if probe_result.performed and not probe_result.error:
            if probe_result.magic_type or (probe_result.content_type and "application/pdf" in probe_result.content_type.lower()) or (probe_result.content_disposition and "attachment" in probe_result.content_disposition.lower()):
                classification_res = "file"
                resource_type = "file"
                if probe_result.magic_type == "pdf" or (probe_result.content_type and "pdf" in probe_result.content_type.lower()):
                    resource_type = "document"
                handling_mode = HandlingMode.DIRECT_DOWNLOAD
                crawlable = False
                preferred_handler = "file_downloader"
                fallback_chain = ["direct_download"]
                confidence = max(confidence, 0.9)
                matched_rule = "probe_override"

        # Map handling_mode to basic capabilities
        caps = Capabilities(
            plain_http=(handling_mode in (HandlingMode.PLAIN_HTTP, HandlingMode.ARTICLE_EXTRACTOR)),
            browser=(handling_mode == HandlingMode.BROWSER),
            direct_download=(handling_mode == HandlingMode.DIRECT_DOWNLOAD),
            connector=(handling_mode == HandlingMode.API_CONNECTOR),
            platform_api=False,
            oembed=False
        )
        
        # Add browser fallback to capabilities if it's in the chain
        if "browser" in fallback_chain:
            caps.browser = True

        return LinkClassificationResponse(
            url=url,
            normalized_url=normalized,
            final_url=normalized if not probe_result.redirect_count else url, # Ideally use probe's final_url but we didn't track it cleanly, let's just stick to normalized
            classification=classification_res,
            resource_type=resource_type,
            handling_mode=handling_mode,
            crawlable=crawlable,
            capabilities=caps,
            preferred_handler=preferred_handler,
            fallback_chain=fallback_chain,
            confidence=confidence,
            matched_rule=matched_rule,
            rule_version=self.engine.version,
            evidence=evidence,
            probe=probe_result
        )
