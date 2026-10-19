from enum import Enum
from pydantic import BaseModel, Field


class HandlingMode(str, Enum):
    DIRECT_DOWNLOAD = "direct_download"
    PLAIN_HTTP = "plain_http"
    ARTICLE_EXTRACTOR = "article_extractor"
    PLATFORM_EXTRACTOR = "platform_extractor"
    BROWSER = "browser"
    API_CONNECTOR = "api_connector"
    UNSUPPORTED = "unsupported"


class Capabilities(BaseModel):
    plain_http: bool = False
    browser: bool = False
    direct_download: bool = False
    oembed: bool = False
    platform_api: bool = False
    connector: bool = False


class Evidence(BaseModel):
    type: str
    value: str
    weight: float | None = None


class ProbeResult(BaseModel):
    performed: bool = False
    status_code: int | None = None
    content_type: str | None = None
    content_disposition: str | None = None
    content_length: int | None = None
    redirect_count: int = 0
    magic_type: str | None = None
    error: str | None = None


class ClassificationOptions(BaseModel):
    probe: bool = True
    follow_redirects: bool = True
    max_redirects: int = 5
    timeout_ms: int = 5000
    allow_private_network: bool = False


class LinkClassificationResponse(BaseModel):
    url: str
    normalized_url: str
    final_url: str | None = None

    classification: str
    resource_type: str
    handling_mode: HandlingMode

    crawlable: bool
    capabilities: Capabilities

    preferred_handler: str
    fallback_chain: list[str] = Field(default_factory=list)

    confidence: float = Field(ge=0.0, le=1.0)
    matched_rule: str
    rule_version: str

    evidence: list[Evidence] = Field(default_factory=list)
    probe: ProbeResult
