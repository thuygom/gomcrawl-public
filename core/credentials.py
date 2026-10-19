from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from .utils import read_secret_file


PROJECT_ROOT = Path(os.getenv("WEB_SEARCH_PROJECT_ROOT", Path(__file__).resolve().parents[1])).resolve()
WORKSPACE_ROOT = Path(
    os.getenv(
        "WORKSPACE_ROOT",
        str(PROJECT_ROOT),
    )
).resolve()
KEY_DIRS = (
    PROJECT_ROOT / "key",
    WORKSPACE_ROOT / "key",
)

KEY_FILE_NAMES: dict[str, tuple[str, ...]] = {
    "NAVER_CLIENT_ID": (
        "naver_client_id.key",
        "naver_client_id.txt",
        "naverClientID.key",
        "naverClientID.txt",
        "naver.id",
    ),
    "NAVER_CLIENT_SECRET": (
        "naver_client_secret.key",
        "naver_client_secret.txt",
        "naverClientSecret.key",
        "naverClientSecret.txt",
        "naver.secret",
    ),
    "YOUTUBE_API_KEY": (
        "youtube_api_key.key",
        "youtube_api_key.txt",
        "youtube_v3.key",
        "youtube_v3.txt",
        "youtube.key",
    ),
    "FIRECRAWL_API_KEY": (
        "firecrawl_api_key.key",
        "firecrawl_api_key.txt",
        "firecrawl.key",
    ),
    "WATERCRAWL_API_URL": (
        "watercrawl_api_url.txt",
        "watercrawl_url.txt",
        "watercrawl.url",
    ),
    "WATERCRAWL_API_KEY": (
        "watercrawl_api_key.key",
        "watercrawl_api_key.txt",
        "watercrawl.key",
    ),
    "SERPER_API_KEY": (
        "serper_api_key.key",
        "serper_api_key.txt",
        "serper.key",
        "serper.jey",
        "serper.txt",
    ),
    "APIFY_API_TOKEN": (
        "apify.key",
        "apify2.key",
        "apify.txt",
        "apify2.txt",
        "apify_api_token.key",
        "apify_api_token.txt",
        "apify_token.key",
    ),
    "APIFY_LINKEDIN_ACTOR_ID": (
        "apify_linkedin_actor_id.key",
        "apify_linkedin_actor_id.txt",
        "linkedin_actor_id.key",
        "linkedin_actor_id.txt",
        "apify.linkedin.actor",
    ),
    "APIFY_LINKEDIN_COMPANY_ACTOR_ID": (
        "apify_linkedin_company_actor_id.key",
        "apify_linkedin_company_actor_id.txt",
        "linkedin_company_actor_id.key",
        "linkedin_company_actor_id.txt",
    ),
    "APIFY_LINKEDIN_INPUT_TEMPLATE": (
        "apify_linkedin_input_template.json",
        "apify_linkedin_input_template.key",
    ),
    "APIFY_LINKEDIN_COMPANY_INPUT_TEMPLATE": (
        "apify_linkedin_company_input_template.json",
        "apify_linkedin_company_input_template.key",
    ),
    "APIFY_LINKEDIN_PROFILE_SEARCH_ACTOR_ID": (
        "apify_linkedin_profile_search_actor_id.key",
        "apify_linkedin_profile_search_actor_id.txt",
        "linkedin_profile_search_actor_id.key",
        "linkedin_profile_search_actor_id.txt",
    ),
    "OPENAI_API_KEY": (
        "openai.key",
        "openai.txt",
        "openai_api_key.key",
        "openai_api_key.txt",
    ),
    "ENTERPRISE_DB_API_KEY": (
        "enterprise_db_api.key",
        "enterpriseDB.key",
    ),
    "API_K_DART": (
        "opendart.key",
        "open_dart.key",
        "dart.key",
    ),
}

BINARY_CANDIDATES: dict[str, tuple[Path, ...]] = {
    "firecrawl": (
        Path.home() / ".hermes/node/bin/firecrawl",
        Path.home() / ".local/node/current/bin/firecrawl",
        PROJECT_ROOT / "node_modules/.bin/firecrawl",
        PROJECT_ROOT / "crawler/node_modules/.bin/firecrawl",
    ),
}


@dataclass(frozen=True, slots=True)
class CredentialResolution:
    env: str
    file_env: str
    present: bool
    source: str
    path: str | None = None
    candidate_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def key_file_candidates(env_name: str) -> tuple[Path, ...]:
    names = KEY_FILE_NAMES.get(env_name, ())
    return tuple(root / name for root in KEY_DIRS for name in names)


def key_file_candidate_strings(env_name: str) -> tuple[str, ...]:
    return tuple(str(path) for path in key_file_candidates(env_name))


def resolve_credential(env_name: str) -> CredentialResolution:
    file_env_name = f"{env_name}_FILE"
    candidates = key_file_candidates(env_name)
    candidate_strings = tuple(str(path) for path in candidates)

    if os.getenv(env_name):
        return CredentialResolution(
            env=env_name,
            file_env=file_env_name,
            present=True,
            source="env",
            candidate_paths=candidate_strings,
        )

    file_env_path = os.getenv(file_env_name)
    if file_env_path and read_secret_file(file_env_path):
        return CredentialResolution(
            env=env_name,
            file_env=file_env_name,
            present=True,
            source="file_env",
            path=str(Path(file_env_path).expanduser()),
            candidate_paths=candidate_strings,
        )

    for candidate in candidates:
        if read_secret_file(candidate):
            return CredentialResolution(
                env=env_name,
                file_env=file_env_name,
                present=True,
                source="candidate_file",
                path=str(candidate),
                candidate_paths=candidate_strings,
            )

    return CredentialResolution(
        env=env_name,
        file_env=file_env_name,
        present=False,
        source="missing",
        candidate_paths=candidate_strings,
    )


def secret_for_env(env_name: str) -> str | None:
    file_env_name = f"{env_name}_FILE"
    value = os.getenv(env_name)
    if value:
        return value

    value = read_secret_file(os.getenv(file_env_name))
    if value:
        return value

    for candidate in key_file_candidates(env_name):
        value = read_secret_file(candidate)
        if value:
            return value
    return None


def binary_candidates(name: str) -> tuple[str, ...]:
    return tuple(str(path) for path in BINARY_CANDIDATES.get(name, ()))


def resolve_binary(name: str) -> str | None:
    path = shutil.which(name)
    if path:
        return path

    for candidate in BINARY_CANDIDATES.get(name, ()):
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None
