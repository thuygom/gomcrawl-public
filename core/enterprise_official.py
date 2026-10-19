"""Authoritative company enrichment used only by ``/api/enterpriseDB``.

OpenDART is called directly with the user's key.  The FSC and NTS contracts
follow NomaDamas/k-skill (MIT) and use its narrow hosted/self-hosted HTTP proxy.
All upstream failures are returned as data so the existing web fallback can run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from io import BytesIO
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any, Mapping
from urllib.parse import urlencode
import xml.etree.ElementTree as ET
import zipfile

import httpx

from .credentials import PROJECT_ROOT, secret_for_env


OPEN_DART_BASE_URL = "https://opendart.fss.or.kr/api"
FSC_SOURCE_URL = "https://www.data.go.kr/data/15043184/openapi.do"
NTS_SOURCE_URL = "https://www.data.go.kr/tcs/dss/selectApiDataDetailView.do?publicDataPk=15081808"
DEFAULT_KSKILL_PROXY_BASE_URL = "https://k-skill-proxy.nomadamas.org"
DEFAULT_DART_CACHE = PROJECT_ROOT / ".run" / "cache" / "opendart" / "CORPCODE.xml"

_DART_CACHE_LOCK = threading.Lock()
_PROXY_CIRCUIT_LOCK = threading.Lock()
_PROXY_DISABLED_UNTIL = 0.0


class OfficialSourceError(RuntimeError):
    pass


class ProxyCircuitOpen(OfficialSourceError):
    pass


def empty_official_result() -> dict[str, Any]:
    return {
        "fields": {},
        "field_sources": {},
        "sources": [],
        "errors": [],
        "subtasks": [],
        "identity_confirmed": False,
    }


def _digits(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


def canonical_company_name(value: object) -> str:
    text = str(value or "").casefold()
    text = re.sub(
        r"주식회사|유한책임회사|유한회사|합자회사|합명회사|사단법인|재단법인|"
        r"[\(\[]\s*(?:주|사|재)\s*[\)\]]|\(\s*유\s*\)|㈜",
        " ",
        text,
    )
    text = re.sub(r"\b(?:co|ltd|inc|corp|corporation|company|limited)\b", " ", text)
    return re.sub(r"[^0-9a-z가-힣]+", "", text)


def company_names_equal(left: object, right: object) -> bool:
    left_key = canonical_company_name(left)
    return bool(left_key and left_key == canonical_company_name(right))


def _first(source: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = re.sub(r"\s+", " ", str(source.get(name) or "")).strip()
        if value:
            return value
    return ""


def _format_business_number(value: object) -> str:
    value = _digits(value)
    return f"{value[:3]}-{value[3:5]}-{value[5:]}" if len(value) == 10 else value


def _format_corporate_number(value: object) -> str:
    value = _digits(value)
    return f"{value[:6]}-{value[6:]}" if len(value) == 13 else value


def _format_date(value: object) -> str:
    value = _digits(value)
    return f"{value[:4]}-{value[4:6]}-{value[6:]}" if len(value) == 8 else value


def _homepage(value: object) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    return value if re.match(r"https?://", value, re.I) else f"https://{value.lstrip('/')}"


@lru_cache(maxsize=2)
def _read_dart_index(path_string: str, modified_ns: int) -> dict[str, tuple[dict[str, str], ...]]:
    del modified_ns  # part of the cache key
    grouped: dict[str, list[dict[str, str]]] = {}
    for node in ET.parse(path_string).getroot().findall("list"):
        record = {
            "corp_code": (node.findtext("corp_code") or "").strip(),
            "corp_name": (node.findtext("corp_name") or "").strip(),
            "corp_eng_name": (node.findtext("corp_eng_name") or "").strip(),
            "stock_code": (node.findtext("stock_code") or "").strip(),
            "modify_date": (node.findtext("modify_date") or "").strip(),
        }
        if not record["corp_code"]:
            continue
        for name in (record["corp_name"], record["corp_eng_name"], record["stock_code"]):
            key = canonical_company_name(name)
            if key and record not in grouped.setdefault(key, []):
                grouped[key].append(record)
    return {key: tuple(values) for key, values in grouped.items()}


def _dart_candidates(index: Mapping[str, tuple[dict[str, str], ...]], names: list[str]) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    for name in names:
        for candidate in index.get(canonical_company_name(name), ()):
            code = candidate["corp_code"]
            if code not in seen:
                seen.add(code)
                candidates.append(candidate)
    return candidates


def dart_profile_fields(profile: Mapping[str, Any]) -> dict[str, str]:
    fields = {
        "company": _first(profile, "corp_name"),
        "english_company": _first(profile, "corp_name_eng"),
        "business_number": _format_business_number(profile.get("bizr_no")),
        "corporate_number": _format_corporate_number(profile.get("jurir_no")),
        "representative": _first(profile, "ceo_nm"),
        "address": _first(profile, "adres"),
        "homepage": _homepage(profile.get("hm_url")),
        "founded": _format_date(profile.get("est_dt")),
        "stock_code": _first(profile, "stock_code"),
        "industry_code": _first(profile, "induty_code"),
        "phone": _first(profile, "phn_no"),
        "fax": _first(profile, "fax_no"),
        "settlement_month": _first(profile, "acc_mt"),
    }
    listing = {"Y": "유가증권시장", "K": "코스닥", "N": "코넥스", "E": "기타법인"}.get(
        _first(profile, "corp_cls")
    )
    if listing:
        fields["listing"] = listing
    return {key: value for key, value in fields.items() if value}


def dart_financial_fields(rows: list[Mapping[str, Any]], year: int) -> dict[str, str]:
    preferred = sorted(rows, key=lambda row: 0 if row.get("fs_div") == "CFS" else 1)
    wanted = {
        "revenue": ("매출액", "영업수익", "수익(매출액)"),
        "operating_profit": ("영업이익", "영업이익(손실)"),
        "net_income": ("당기순이익", "당기순이익(손실)", "분기순이익", "반기순이익"),
    }
    found: dict[str, str] = {}
    for row in preferred:
        account_name = re.sub(r"\s+", "", str(row.get("account_nm") or ""))
        amount = str(row.get("thstrm_amount") or "").strip()
        if not amount or not re.fullmatch(r"-?[\d,]+", amount):
            continue
        for field, names in wanted.items():
            if field not in found and account_name in names:
                statement = "연결" if row.get("fs_div") == "CFS" else "별도"
                found[field] = f"{amount}원 ({year} 사업연도, {statement})"
    return found


def dart_employee_fields(rows: list[Mapping[str, Any]], year: int) -> dict[str, str]:
    totals = [row for row in rows if "합계" in str(row.get("fo_bbm") or "")]
    selected = totals or rows
    count = sum(int(_digits(row.get("sm")) or 0) for row in selected)
    return {"scale": f"{count:,}명 ({year} 사업보고서)"} if count else {}


class OpenDartCompanyClient:
    def __init__(self, api_key: str | None = None, *, include_reports: bool = True) -> None:
        self.api_key = api_key or secret_for_env("API_K_DART") or ""
        self.include_reports = include_reports
        self.cache_path = Path(os.getenv("OPEN_DART_CORP_CODE_CACHE", str(DEFAULT_DART_CACHE))).expanduser()
        try:
            self.timeout = max(2.0, min(float(os.getenv("OPEN_DART_TIMEOUT_SECONDS", "20")), 60.0))
        except ValueError:
            self.timeout = 20.0
        try:
            self.cache_ttl = max(3600, int(os.getenv("OPEN_DART_CORP_CODE_TTL_SECONDS", "86400")))
        except ValueError:
            self.cache_ttl = 86400

    def _json(self, client: httpx.Client, endpoint: str, **params: object) -> dict[str, Any]:
        try:
            response = client.get(
                f"{OPEN_DART_BASE_URL}/{endpoint}",
                params={"crtfc_key": self.api_key, **params},
            )
        except httpx.HTTPError as exc:
            raise OfficialSourceError(f"OpenDART transport error ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise OfficialSourceError(f"OpenDART HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise OfficialSourceError("OpenDART returned invalid JSON") from None
        status = str(payload.get("status") or "")
        if status not in {"000", "013"}:
            raise OfficialSourceError(f"OpenDART status {status or 'unknown'}: {payload.get('message') or 'error'}")
        return payload

    def _corp_code_file(self, client: httpx.Client) -> Path:
        fresh = self.cache_path.is_file() and time.time() - self.cache_path.stat().st_mtime < self.cache_ttl
        if fresh:
            return self.cache_path
        with _DART_CACHE_LOCK:
            fresh = self.cache_path.is_file() and time.time() - self.cache_path.stat().st_mtime < self.cache_ttl
            if fresh:
                return self.cache_path
            try:
                response = client.get(
                    f"{OPEN_DART_BASE_URL}/corpCode.xml",
                    params={"crtfc_key": self.api_key},
                )
                if response.status_code != 200:
                    raise OfficialSourceError(f"OpenDART corp-code HTTP {response.status_code}")
                with zipfile.ZipFile(BytesIO(response.content)) as archive:
                    member = next((name for name in archive.namelist() if name.upper().endswith("CORPCODE.XML")), "")
                    if not member:
                        raise OfficialSourceError("OpenDART corp-code archive is missing CORPCODE.xml")
                    xml_bytes = archive.read(member)
                if len(xml_bytes) > 256 * 1024 * 1024:
                    raise OfficialSourceError("OpenDART corp-code XML exceeded the safety limit")
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=self.cache_path.parent, delete=False) as handle:
                    handle.write(xml_bytes)
                    temporary = Path(handle.name)
                os.replace(temporary, self.cache_path)
                _read_dart_index.cache_clear()
            except (OfficialSourceError, httpx.HTTPError, zipfile.BadZipFile) as exc:
                if self.cache_path.is_file():
                    return self.cache_path
                raise OfficialSourceError(f"OpenDART corp-code download failed ({type(exc).__name__})") from None
            return self.cache_path

    def _latest_report_fields(
        self, client: httpx.Client, corp_code: str
    ) -> tuple[dict[str, str], dict[str, str]]:
        current_year = datetime.now(timezone.utc).year
        fields: dict[str, str] = {}
        sources: dict[str, str] = {}
        for year in (current_year - 1, current_year - 2):
            financial = self._json(
                client,
                "fnlttSinglAcnt.json",
                corp_code=corp_code,
                bsns_year=year,
                reprt_code="11011",
            )
            if financial.get("status") == "000":
                fields.update(dart_financial_fields(financial.get("list") or [], year))
                financial_url = f"{OPEN_DART_BASE_URL}/fnlttSinglAcnt.json?{urlencode({'corp_code': corp_code, 'bsns_year': year, 'reprt_code': '11011'})}"
                sources.update({field: financial_url for field in fields if field in {"revenue", "operating_profit", "net_income"}})
            employees = self._json(
                client,
                "empSttus.json",
                corp_code=corp_code,
                bsns_year=year,
                reprt_code="11011",
            )
            if employees.get("status") == "000":
                employee_fields = dart_employee_fields(employees.get("list") or [], year)
                fields.update(employee_fields)
                employee_url = f"{OPEN_DART_BASE_URL}/empSttus.json?{urlencode({'corp_code': corp_code, 'bsns_year': year, 'reprt_code': '11011'})}"
                sources.update({field: employee_url for field in employee_fields})
            if fields:
                break
        return fields, sources

    def lookup(self, profile: Mapping[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        if not self.api_key:
            return {"status": "skipped", "provider": "opendart", "fields": {}, "field_sources": {}, "sources": [], "reason": "API_K_DART is not configured", "elapsed_seconds": 0.0}
        names = [str(value) for value in profile.get("names") or [] if str(value).strip()]
        business_no = _digits(profile.get("business_no"))
        corporate_no = _digits(profile.get("corporate_no"))
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True, headers={"User-Agent": "webSearchAgent-enterpriseDB/1.0"}) as client:
                path = self._corp_code_file(client)
                index = _read_dart_index(str(path), path.stat().st_mtime_ns)
                candidates = _dart_candidates(index, names)
                if len(candidates) > 1 and not (business_no or corporate_no):
                    return {"status": "ambiguous", "provider": "opendart", "fields": {}, "field_sources": {}, "sources": [], "candidate_count": len(candidates), "elapsed_seconds": round(time.monotonic() - started, 3)}
                profiles: list[tuple[dict[str, str], dict[str, Any]]] = []
                for candidate in candidates[:10]:
                    payload = self._json(client, "company.json", corp_code=candidate["corp_code"])
                    if payload.get("status") == "000":
                        profiles.append((candidate, payload))
                    if len(candidates) == 1:
                        break
                matched = [
                    (candidate, item)
                    for candidate, item in profiles
                    if any(
                        company_names_equal(candidate.get("corp_name"), name)
                        or company_names_equal(candidate.get("corp_eng_name"), name)
                        or company_names_equal(item.get("corp_name"), name)
                        or company_names_equal(item.get("corp_name_eng"), name)
                        for name in names
                    )
                    and (not business_no or _digits(item.get("bizr_no")) == business_no)
                    and (not corporate_no or _digits(item.get("jurir_no")) == corporate_no)
                ]
                if len(matched) != 1:
                    return {"status": "no_match" if not matched else "ambiguous", "provider": "opendart", "fields": {}, "field_sources": {}, "sources": [], "candidate_count": len(candidates), "elapsed_seconds": round(time.monotonic() - started, 3)}
                index_record, company = matched[0]
                corp_code = str(company["corp_code"])
                profile_name_match = any(
                    company_names_equal(company.get("corp_name"), name)
                    or company_names_equal(company.get("corp_name_eng"), name)
                    for name in names
                )
                fields = dart_profile_fields(company)
                company_url = f"{OPEN_DART_BASE_URL}/company.json?{urlencode({'corp_code': corp_code})}"
                field_sources = {field: company_url for field in fields}
                report_error = ""
                if self.include_reports:
                    try:
                        report_fields, report_sources = self._latest_report_fields(client, corp_code)
                    except OfficialSourceError as exc:
                        report_fields, report_sources = {}, {}
                        report_error = f"OpenDART report enrichment: {exc}"
                else:
                    report_fields, report_sources = {}, {}
                fields.update(report_fields)
                field_sources.update(report_sources)
                grouped_sources: dict[str, list[str]] = {}
                for field, url in field_sources.items():
                    grouped_sources.setdefault(url, []).append(field)
                source_records = []
                for url, source_fields in grouped_sources.items():
                    if "fnlttSinglAcnt" in url:
                        title = "OpenDART 단일회사 주요계정"
                    elif "empSttus" in url:
                        title = "OpenDART 직원 현황"
                    else:
                        title = "OpenDART 기업개황"
                    source_records.append({
                        "url": url,
                        "title": title,
                        "provider": "opendart",
                        "crawler": "official_api",
                        "status_code": 200,
                        "identity_reason": (
                            "official_registry_exact_name"
                            if url == company_url and profile_name_match
                            else (
                                "official_registry_exact_index_name_same_corp_code"
                                if url == company_url
                                else "confirmed_dart_corp_code"
                            )
                        ),
                        "corp_code": corp_code,
                        "index_company_name": index_record.get("corp_name", ""),
                        "fields": source_fields,
                    })
                return {
                    "status": "matched",
                    "provider": "opendart",
                    "fields": fields,
                    "field_sources": field_sources,
                    "sources": source_records,
                    "identity_confirmed": True,
                    **({"error": report_error} if report_error else {}),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
        except (OfficialSourceError, ET.ParseError, OSError, ValueError) as exc:
            return {"status": "failed", "provider": "opendart", "fields": {}, "field_sources": {}, "sources": [], "error": str(exc), "elapsed_seconds": round(time.monotonic() - started, 3)}


def _trip_proxy_circuit(retry_after: object = None) -> None:
    global _PROXY_DISABLED_UNTIL
    try:
        seconds = float(retry_after or 120)
    except (TypeError, ValueError):
        seconds = 120
    with _PROXY_CIRCUIT_LOCK:
        _PROXY_DISABLED_UNTIL = max(_PROXY_DISABLED_UNTIL, time.monotonic() + max(30, min(seconds, 600)))


def _proxy_json(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    base_url = os.getenv("KSKILL_PROXY_BASE_URL", DEFAULT_KSKILL_PROXY_BASE_URL).strip().rstrip("/")
    if base_url.casefold() in {"", "off", "false", "0", "disabled", "none"}:
        raise ProxyCircuitOpen("k-skill-proxy is disabled")
    with _PROXY_CIRCUIT_LOCK:
        if time.monotonic() < _PROXY_DISABLED_UNTIL:
            raise ProxyCircuitOpen("k-skill-proxy circuit is open")
    try:
        timeout = max(0.5, min(float(os.getenv("KSKILL_PROXY_TIMEOUT_SECONDS", "3")), 10.0))
    except ValueError:
        timeout = 3.0
    try:
        response = httpx.request(
            method,
            f"{base_url}{path}",
            timeout=timeout,
            follow_redirects=True,
            headers={"Accept": "application/json", "User-Agent": "webSearchAgent-enterpriseDB/1.0"},
            **kwargs,
        )
    except httpx.HTTPError as exc:
        _trip_proxy_circuit()
        raise OfficialSourceError(f"k-skill-proxy transport error ({type(exc).__name__})") from None
    if response.status_code == 429 or response.status_code >= 500:
        _trip_proxy_circuit(response.headers.get("retry-after"))
        raise OfficialSourceError(f"k-skill-proxy HTTP {response.status_code}")
    if response.status_code != 200:
        raise OfficialSourceError(f"k-skill-proxy HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        raise OfficialSourceError("k-skill-proxy returned invalid JSON") from None
    if not isinstance(payload, dict) or payload.get("error"):
        raise OfficialSourceError(f"k-skill-proxy error: {payload.get('error') if isinstance(payload, dict) else 'invalid payload'}")
    return payload


def _fsc_fields(candidate: Mapping[str, Any]) -> dict[str, str]:
    fields = {
        "company": _first(candidate, "corpNm"),
        "english_company": _first(candidate, "corpEnsnNm", "enpPbanCmpyNm"),
        "business_number": _format_business_number(candidate.get("bzno")),
        "corporate_number": _format_corporate_number(candidate.get("crno")),
        "representative": _first(candidate, "enpRprFnm"),
        "address": " ".join(filter(None, (_first(candidate, "enpBsadr"), _first(candidate, "enpDtadr")))),
        "homepage": _homepage(candidate.get("enpHmpgUrl")),
        "industry": _first(candidate, "sicNm", "enpMainBizNm"),
        "products": _first(candidate, "enpMainBizNm"),
        "founded": _format_date(candidate.get("enpEstbDt")),
        "phone": _first(candidate, "enpTlno"),
        "fax": _first(candidate, "enpFxno"),
        "listing": _first(candidate, "corpRegMrktDcdNm"),
    }
    return {key: value for key, value in fields.items() if value}


def _lookup_fsc(profile: Mapping[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    names = [str(value) for value in profile.get("names") or [] if str(value).strip()]
    if not names:
        return {"status": "skipped", "provider": "k_skill_fsc", "fields": {}, "field_sources": {}, "sources": [], "elapsed_seconds": 0.0}
    business_no = _digits(profile.get("business_no"))
    try:
        payload = _proxy_json("GET", "/v1/fsc/corp-outline", params={"name": names[0], **({"b_no": business_no} if business_no else {})})
    except ProxyCircuitOpen as exc:
        return {"status": "skipped", "provider": "k_skill_fsc", "fields": {}, "field_sources": {}, "sources": [], "reason": str(exc), "elapsed_seconds": round(time.monotonic() - started, 3)}
    except OfficialSourceError as exc:
        return {"status": "failed", "provider": "k_skill_fsc", "fields": {}, "field_sources": {}, "sources": [], "error": str(exc), "elapsed_seconds": round(time.monotonic() - started, 3)}
    candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
    if business_no:
        checked = payload.get("b_no_cross_check") if isinstance(payload.get("b_no_cross_check"), dict) else {}
        matched = checked.get("matched_candidates") if isinstance(checked.get("matched_candidates"), list) else []
    else:
        matched = [candidate for candidate in candidates if isinstance(candidate, Mapping) and any(company_names_equal(candidate.get("corpNm"), name) for name in names)]
    if len(matched) != 1:
        return {"status": "no_match" if not matched else "ambiguous", "provider": "k_skill_fsc", "fields": {}, "field_sources": {}, "sources": [], "candidate_count": len(candidates), "elapsed_seconds": round(time.monotonic() - started, 3)}
    fields = _fsc_fields(matched[0])
    return {
        "status": "matched",
        "provider": "k_skill_fsc",
        "fields": fields,
        "field_sources": {field: FSC_SOURCE_URL for field in fields},
        "sources": [{
            "url": FSC_SOURCE_URL,
            "title": "금융위원회 기업기본정보",
            "provider": "k_skill_proxy",
            "crawler": "official_api",
            "status_code": 200,
            "identity_reason": "official_registry_exact_name" if not business_no else "official_business_number",
            "fields": list(fields),
        }],
        "identity_confirmed": True,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def _lookup_nts_status(profile: Mapping[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    business_no = _digits(profile.get("business_no"))
    if len(business_no) != 10:
        return {"status": "skipped", "provider": "k_skill_nts", "fields": {}, "field_sources": {}, "sources": [], "reason": "business number is missing", "elapsed_seconds": 0.0}
    try:
        payload = _proxy_json("POST", "/v1/nts-business/status", json={"b_no": [business_no]})
    except ProxyCircuitOpen as exc:
        return {"status": "skipped", "provider": "k_skill_nts", "fields": {}, "field_sources": {}, "sources": [], "reason": str(exc), "elapsed_seconds": round(time.monotonic() - started, 3)}
    except OfficialSourceError as exc:
        return {"status": "failed", "provider": "k_skill_nts", "fields": {}, "field_sources": {}, "sources": [], "error": str(exc), "elapsed_seconds": round(time.monotonic() - started, 3)}
    rows = payload.get("data") if isinstance(payload.get("data"), list) else []
    matches = [row for row in rows if isinstance(row, Mapping) and _digits(row.get("b_no")) == business_no]
    if len(matches) != 1:
        return {"status": "no_match", "provider": "k_skill_nts", "fields": {}, "field_sources": {}, "sources": [], "elapsed_seconds": round(time.monotonic() - started, 3)}
    row = matches[0]
    fields = {
        "business_status": _first(row, "b_stt"),
        "tax_type": _first(row, "tax_type"),
        "business_closed_date": _format_date(row.get("end_dt")),
    }
    fields = {key: value for key, value in fields.items() if value}
    return {
        "status": "matched",
        "provider": "k_skill_nts",
        "fields": fields,
        "field_sources": {field: NTS_SOURCE_URL for field in fields},
        "sources": [{
            "url": NTS_SOURCE_URL,
            "title": "국세청 사업자등록 상태조회",
            "provider": "k_skill_proxy",
            "crawler": "official_api",
            "status_code": 200,
            "identity_reason": "official_business_number_status",
            "fields": list(fields),
        }],
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def enrich_official_company(profile: Mapping[str, Any], _known: Mapping[str, str]) -> dict[str, Any]:
    result = empty_official_result()
    effective = dict(profile)
    effective["names"] = list(profile.get("names") or [])

    for lookup in (OpenDartCompanyClient().lookup, _lookup_fsc):
        try:
            source = lookup(effective)
        except Exception as exc:
            provider = "opendart" if getattr(lookup, "__self__", None) else "k_skill_fsc"
            source = {
                "status": "failed",
                "provider": provider,
                "fields": {},
                "field_sources": {},
                "sources": [],
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_seconds": 0.0,
            }
        result["subtasks"].append({
            "name": source.get("provider"),
            "provider": source.get("provider"),
            "operation": "company_lookup",
            "status": source.get("status"),
            "count": len(source.get("fields") or {}),
            "elapsed_seconds": source.get("elapsed_seconds", 0.0),
            **({"reason": source["reason"]} if source.get("reason") else {}),
        })
        if source.get("error"):
            result["errors"].append(f"{source.get('provider')}: {source['error']}")
        for field, value in (source.get("fields") or {}).items():
            if field not in result["fields"]:
                result["fields"][field] = value
                result["field_sources"][field] = (source.get("field_sources") or {}).get(field, "")
        result["sources"].extend(source.get("sources") or [])
        result["identity_confirmed"] = bool(result["identity_confirmed"] or source.get("identity_confirmed"))
        if source.get("identity_confirmed"):
            for name_field in ("company", "english_company"):
                value = (source.get("fields") or {}).get(name_field)
                if value and value not in effective["names"]:
                    if name_field == "company":
                        effective["names"].insert(0, value)
                    else:
                        effective["names"].append(value)
            if not effective.get("business_no"):
                effective["business_no"] = _digits((source.get("fields") or {}).get("business_number"))
            if not effective.get("corporate_no"):
                effective["corporate_no"] = _digits((source.get("fields") or {}).get("corporate_number"))

    if result["identity_confirmed"] and effective.get("business_no"):
        nts = _lookup_nts_status(effective)
        result["subtasks"].append({
            "name": nts.get("provider"),
            "provider": nts.get("provider"),
            "operation": "business_status",
            "status": nts.get("status"),
            "count": len(nts.get("fields") or {}),
            "elapsed_seconds": nts.get("elapsed_seconds", 0.0),
            **({"reason": nts["reason"]} if nts.get("reason") else {}),
        })
        if nts.get("error"):
            result["errors"].append(f"{nts.get('provider')}: {nts['error']}")
        for field, value in (nts.get("fields") or {}).items():
            if field not in result["fields"]:
                result["fields"][field] = value
                result["field_sources"][field] = (nts.get("field_sources") or {}).get(field, "")
        result["sources"].extend(nts.get("sources") or [])

    return result
