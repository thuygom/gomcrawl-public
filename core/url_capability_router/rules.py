import yaml
import pathlib
from urllib.parse import urlparse, parse_qs
from pydantic import BaseModel, Field

from core.url_capability_router.models import HandlingMode, Capabilities, Evidence

class RuleMatchCondition(BaseModel):
    exact_hosts: list[str] = Field(default_factory=list)
    host_suffixes: list[str] = Field(default_factory=list)
    exact_paths: list[str] = Field(default_factory=list)
    path_prefixes: list[str] = Field(default_factory=list)
    required_query_keys: list[str] = Field(default_factory=list)
    extensions: list[str] = Field(default_factory=list)
    path_segment_count: dict[str, int] | None = None
    any: bool = False

class RuleResult(BaseModel):
    classification: str
    resource_type: str
    handling_mode: HandlingMode
    crawlable: bool
    preferred_handler: str
    fallback_chain: list[str] = Field(default_factory=list)

class Rule(BaseModel):
    id: str
    priority: int
    match: RuleMatchCondition
    result: RuleResult

class RuleMatch(BaseModel):
    rule: Rule
    evidence: list[Evidence]
    confidence: float

class RuleEngine:
    def __init__(self, yaml_path: str):
        self.rules: list[Rule] = []
        self.version = "unknown"
        self.load(yaml_path)

    def load(self, yaml_path: str):
        with open(yaml_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        
        self.version = data.get("version", "unknown")
        rules_data = data.get("rules", [])
        
        for r_data in rules_data:
            self.rules.append(Rule(**r_data))
            
        # Sort by priority descending
        self.rules.sort(key=lambda x: x.priority, reverse=True)

    def match(self, parsed_url) -> RuleMatch | None:
        domain = parsed_url.netloc.lower()
        if ":" in domain:
            domain = domain.split(":")[0]
            
        path = parsed_url.path.lower()
        if not path:
            path = "/"
            
        query = parse_qs(parsed_url.query)
        ext = pathlib.Path(path).suffix.lstrip(".").lower()
        segments = [s for s in path.split("/") if s]

        best_match = None

        for rule in self.rules:
            is_match = False
            evidences = []
            confidence = 0.0
            
            c = rule.match
            
            if c.any:
                is_match = True
            else:
                # Check hosts
                host_matched = False
                if c.exact_hosts and domain in c.exact_hosts:
                    host_matched = True
                    evidences.append(Evidence(type="exact_host", value=domain, weight=0.45))
                    confidence += 0.45
                elif c.host_suffixes:
                    for suffix in c.host_suffixes:
                        if domain.endswith(suffix):
                            host_matched = True
                            evidences.append(Evidence(type="host_suffix", value=suffix, weight=0.30))
                            confidence += 0.30
                            break
                            
                # If host conditions exist but none matched, rule fails
                if (c.exact_hosts or c.host_suffixes) and not host_matched:
                    continue

                # Check paths
                path_matched = False
                if c.exact_paths and path in c.exact_paths:
                    path_matched = True
                    evidences.append(Evidence(type="exact_path", value=path, weight=0.20))
                    confidence += 0.20
                elif c.path_prefixes:
                    for prefix in c.path_prefixes:
                        if path.startswith(prefix):
                            path_matched = True
                            evidences.append(Evidence(type="path_prefix", value=prefix, weight=0.15))
                            confidence += 0.15
                            break
                
                # If path conditions exist but none matched, rule fails
                if (c.exact_paths or c.path_prefixes) and not path_matched:
                    continue

                # Check queries
                query_matched = True
                if c.required_query_keys:
                    for key in c.required_query_keys:
                        if key not in query:
                            query_matched = False
                            break
                        else:
                            evidences.append(Evidence(type="query_parameter", value=key, weight=0.10))
                            confidence += 0.10
                if not query_matched:
                    continue

                # Check extensions
                ext_matched = True
                if c.extensions:
                    if ext not in c.extensions:
                        ext_matched = False
                    else:
                        evidences.append(Evidence(type="extension", value=ext, weight=0.10))
                        confidence += 0.10
                if not ext_matched:
                    continue

                # Check segments
                segment_matched = True
                if c.path_segment_count:
                    min_c = c.path_segment_count.get("min", 0)
                    max_c = c.path_segment_count.get("max", 999)
                    if not (min_c <= len(segments) <= max_c):
                        segment_matched = False
                if not segment_matched:
                    continue
                    
                # If we passed all conditions, it's a match!
                # Wait, if rule had NO conditions (except 'any'), it shouldn't be matched unless 'any' is true,
                # but pydantic empty lists mean no conditions, so it acts like a catch-all if we aren't careful.
                # A rule must have at least ONE condition specified to match unless any=True.
                if not (c.exact_hosts or c.host_suffixes or c.exact_paths or c.path_prefixes or c.required_query_keys or c.extensions or c.path_segment_count):
                    continue
                    
                is_match = True

            if is_match:
                confidence = min(confidence, 1.0)
                best_match = RuleMatch(rule=rule, evidence=evidences, confidence=confidence)
                break # Since rules are sorted by priority

        return best_match
