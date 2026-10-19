#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import signal
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.bot_detection import summarize_bot_detections  # noqa: E402
from core.article_sentiment import article_sentiment_readiness, evaluate_article_sentiment_batch  # noqa: E402
from core.article_cluster import MAX_ARTICLES, cluster_news_items  # noqa: E402
from core.biz_no_finder import (  # noqa: E402
    BIZ_NO_FINDER_ENDPOINT,
    BIZ_NO_FINDER_ENDPOINTS,
    find_business_number,
)
from core.crawlers import create_crawler, shutdown_stealth_browser_runtime  # noqa: E402
from core.credentials import secret_for_env  # noqa: E402
from core.doctor import build_doctor_report  # noqa: E402
from core.enterprise_db import (  # noqa: E402
    ENTERPRISE_DB_ENDPOINT,
    bearer_authorized,
    complete_enterprise_db,
)
from core.entity_match import score_entity_match_batch  # noqa: E402
from core.linkedin_finder import find_linkedin_profiles  # noqa: E402
from core.url_capability_router.classifier import Classifier
from core.url_capability_router.models import ClassificationOptions

_URL_CLASSIFIER = Classifier()
from core.llm_confidence import clean_bool, evaluate_confidence_batch  # noqa: E402
from core.noteapp_complete import (  # noqa: E402
    NEWS_RESULT_ANALYSIS_VERSION,
    NOTEAPP_COMPLETE_PURPOSES,
    complete_for_purpose,
    news_company_name,
    prepare_payload,
)
from core.news_execution_log import log_news_execution, orch_news_logged, start_news_execution_worker  # noqa: E402
from core.news_result_cache import NEWS_RESULT_CACHE_TTL_SECONDS, cached_news_result  # noqa: E402
from core.person_evidence_finder import find_person_evidence  # noqa: E402
from core.registry import available_crawlers, available_search_providers  # noqa: E402
from core.search import SearchAgentError, create_search_client  # noqa: E402
from core.usage import (  # noqa: E402
    record_api_usage,
    render_prometheus_metrics,
    summarize_search_audit,
    summarize_usage,
)


MAX_QUERY_LENGTH = 500
MAX_URL_LENGTH = 2048
MAX_RESULTS = 10
MAX_ACCOUNT_ID_LENGTH = 200
SEARCH_ENDPOINT_GROUPS: dict[str, dict[str, object]] = {
    "naver": {
        "default": "naver_web",
        "providers": {
            "web": "naver_web",
            "webkr": "naver_web",
            "naver_web": "naver_web",
            "news": "naver_news",
            "naver_news": "naver_news",
        },
    },
    "google": {
        "default": "serper",
        "providers": {
            "serper": "serper",
        },
    },
    "youtube": {
        "default": "youtube",
        "providers": {
            "youtube": "youtube",
            "youtube_v3": "youtube",
        },
    },
    "linkedin": {
        "default": "apify_linkedin_profile_search",
        "providers": {
            "apify": "apify_linkedin_profile_search",
            "apify_linkedin": "apify_linkedin_profile_search",
            "apify_linkedin_profile_search": "apify_linkedin_profile_search",
            "profile_search": "apify_linkedin_profile_search",
        },
    },
    "watercrawl": {
        "default": "watercrawl_search",
        "providers": {
            "watercrawl": "watercrawl_search",
            "watercrawl_search": "watercrawl_search",
        },
    },
}

CRAWL_ENDPOINTS = {
    "playwright": "playwright",
    "playwright-stealth": "playwright_stealth",
    "playwright_stealth": "playwright_stealth",
    "firecrawl": "firecrawl",
    "watercrawl": "watercrawl",
    "stealth-browser": "stealth_browser",
    "stealth_browser": "stealth_browser",
    "linkedin": "apify_linkedin",
    "apify-linkedin": "apify_linkedin",
    "apify_linkedin": "apify_linkedin",
}
FALLBACK_CRAWL_ENDPOINTS = {"playwright-firecrawl", "playwright_firecrawl"}
APIFY_LINKEDIN_SCHOOL_ALIASES = (
    ("Hansung University", "Hansung University"),
    ("한성대학교", "Hansung University"),
    ("한성대", "Hansung University"),
)
NOTEAPP_COMPLETE_ENDPOINTS = tuple(f"/v1/complete/{purpose}" for purpose in NOTEAPP_COMPLETE_PURPOSES)
ARTICLE_CLUSTER_ENDPOINT = "/articluster"
ARTICLE_CLUSTER_EXAMPLE_ENDPOINT = f"{ARTICLE_CLUSTER_ENDPOINT}/example"
ARTICLE_CLUSTER_EXAMPLE_PATH = ROOT / "examples" / "article_cluster_offline_example.json"
ATTACHMENT_LINK_ENDPOINT = "/v1/complete/attachmentLink"
SCRAP_ENDPOINT_CATEGORIES: dict[str, tuple[str, ...] | None] = {
    "/api/scrap/person-evidence": None,
    "/api/scrap/books": ("books",),
    "/api/scrap/publications": ("publications",),
    "/api/scrap/awards": ("awards", "institutions"),
    "/api/scrap/honors": ("awards",),
    "/api/scrap/institutions": ("institutions",),
    "/api/scrap/career": ("career",),
    "/api/find/person-evidence": None,
    "/api/find/books": ("books",),
    "/api/find/publications": ("publications",),
    "/api/find/awards": ("awards", "institutions"),
    "/api/find/honors": ("awards",),
}


INDEX_HTML = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Web Search Agent Crawl Dashboard</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --text: #18212f;
      --muted: #657184;
      --line: #d9dee8;
      --accent: #0d766e;
      --danger: #b42318;
      --warn: #ad5b00;
      --ok: #157347;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); }
    header { border-bottom: 1px solid var(--line); background: #fff; }
    .wrap { max-width: 1180px; margin: 0 auto; padding: 20px; }
    .top { display: flex; justify-content: space-between; gap: 16px; align-items: center; }
    .top-actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
    .nav-button {
      border: 1px solid var(--line); border-radius: 6px; background: #fff; color: #18212f;
      padding: 9px 11px; text-decoration: none; font-size: 13px; font-weight: 700;
    }
    .nav-button:hover { border-color: var(--accent); color: var(--accent); }
    h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: 0; }
    p { margin: 0; color: var(--muted); }
    .grid { display: grid; grid-template-columns: 360px 1fr; gap: 16px; align-items: start; }
    .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px; }
    h2 { margin: 0 0 12px; font-size: 16px; letter-spacing: 0; }
    label { display: block; margin: 12px 0 6px; font-size: 13px; color: #334155; font-weight: 650; }
    input, select, textarea {
      width: 100%; border: 1px solid var(--line); border-radius: 6px; padding: 10px 11px; font: inherit; background: #fff;
    }
    textarea { min-height: 96px; resize: vertical; }
    button {
      border: 0; border-radius: 6px; background: var(--accent); color: white; font-weight: 700;
      padding: 10px 12px; cursor: pointer; min-height: 40px;
    }
    button.secondary { background: #334155; }
    button:disabled { opacity: .55; cursor: wait; }
    .row { display: flex; gap: 8px; align-items: center; }
    .row > * { flex: 1; }
    .tabs { display: flex; gap: 8px; margin-bottom: 14px; }
    .tabs button { background: #e6ebf2; color: #18212f; }
    .tabs button.active { background: var(--accent); color: white; }
    .status { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
    .pill { border: 1px solid var(--line); border-radius: 999px; padding: 5px 9px; background: #fff; color: #334155; font-size: 12px; }
    .ok { color: var(--ok); border-color: #aad8bf; background: #effaf3; }
    .bad { color: var(--danger); border-color: #fac5c0; background: #fff2f0; }
    .warn { color: var(--warn); border-color: #ffd59a; background: #fff8eb; }
    .hidden { display: none; }
    .result { border-top: 1px solid var(--line); padding: 14px 0; }
    .result:first-child { border-top: 0; padding-top: 0; }
    .result h3 { margin: 0 0 5px; font-size: 15px; }
    .result a { color: #0f5f9e; overflow-wrap: anywhere; }
    .crawl-result { display: grid; grid-template-columns: minmax(220px, 34%) minmax(0, 1fr); gap: 18px; }
    .result-media {
      aspect-ratio: 4 / 3; min-width: 0; overflow: hidden; border: 1px solid var(--line);
      border-radius: 6px; background: #eef1f5; position: relative;
    }
    .result-media a, .result-media img { display: block; width: 100%; height: 100%; }
    .result-media img { object-fit: contain; }
    .image-fallback { display: none; height: 100%; align-items: center; justify-content: center; color: var(--muted); font-size: 13px; }
    .result-media.empty .image-fallback { display: flex; }
    .result-body { min-width: 0; }
    .snippet { color: #334155; margin-top: 8px; line-height: 1.45; white-space: pre-wrap; }
    pre { background: #101828; color: #e6edf7; border-radius: 8px; padding: 12px; overflow: auto; max-height: 560px; }
    .small { font-size: 12px; color: var(--muted); line-height: 1.45; }
    .error { color: var(--danger); white-space: pre-wrap; }
    @media (max-width: 860px) {
      .grid, .crawl-result { grid-template-columns: 1fr; }
      .result-media { max-width: 520px; width: 100%; }
      .top { align-items: flex-start; flex-direction: column; }
    }
  </style>
</head>
<body>
  <header>
    <div class="wrap top">
      <div>
        <h1>Web Search Agent Crawl Dashboard</h1>
        <p>Search API, local crawler, browser crawler, managed crawler smoke surface.</p>
      </div>
      <div class="top-actions">
        <a class="nav-button" href="/docs">Docs</a>
        <a class="nav-button" href="/articluster">ArticleCluster</a>
        <a class="nav-button" href="/observability">Observability</a>
        <div id="healthPills" class="status"></div>
      </div>
    </div>
  </header>
  <main class="wrap grid">
    <section class="panel">
      <div class="tabs">
        <button id="tabSearch" class="active" onclick="switchMode('search')">Search</button>
        <button id="tabCrawl" onclick="switchMode('crawl')">Crawl</button>
        <button id="tabNoteApp" onclick="switchMode('noteapp')">NoteApp</button>
      </div>
      <form id="searchForm">
        <h2>Search API</h2>
        <label for="provider">Provider</label>
        <select id="provider"></select>
        <label for="query">Query</label>
        <textarea id="query" maxlength="500">삼성전자 IR</textarea>
        <div class="row">
          <div>
            <label for="limit">Limit</label>
            <input id="limit" type="number" min="1" max="10" value="3">
          </div>
          <div>
            <label for="intent">Intent</label>
            <input id="intent" value="company_profile">
          </div>
        </div>
        <label class="small"><input id="rawSearch" type="checkbox" style="width:auto"> Raw JSON 보기</label>
        <button id="searchButton" type="submit">Run Search</button>
      </form>
      <form id="crawlForm" class="hidden">
        <h2>Public Page Crawl</h2>
        <label for="url">URL</label>
        <textarea id="url" maxlength="2048">http://127.0.0.1:9101/</textarea>
        <label for="crawler">Crawler</label>
        <select id="crawler">
          <option value="auto">auto</option>
          <option value="static">static</option>
          <option value="playwright">playwright</option>
          <option value="selenium">selenium</option>
          <option value="firecrawl">firecrawl</option>
          <option value="watercrawl">watercrawl</option>
          <option value="stealth_browser">stealth_browser</option>
          <option value="apify_linkedin">apify_linkedin</option>
        </select>
        <div class="row">
          <div>
            <label for="baseDelay">Base delay</label>
            <input id="baseDelay" type="number" min="0" step="0.5" value="2">
          </div>
          <div>
            <label for="govDelay">Gov delay</label>
            <input id="govDelay" type="number" min="0" step="0.5" value="10">
          </div>
        </div>
        <label class="small"><input id="respectRobots" type="checkbox" checked style="width:auto"> robots.txt 확인</label>
        <label class="small"><input id="rawCrawl" type="checkbox" style="width:auto"> Raw JSON 보기</label>
        <button id="crawlButton" type="submit">Run Crawl</button>
      </form>
      <form id="noteappForm" class="hidden">
        <h2>NoteApp Complete</h2>
        <label for="completePath">API</label>
        <select id="completePath">
          <option value="/v1/complete/news">news</option>
          <option value="/v1/complete/publication">publication</option>
          <option value="/v1/complete/award">award</option>
          <option value="/v1/complete/social">social</option>
          <option value="/v1/complete/profile">profile</option>
          <option value="/v1/complete/company-homepage">company-homepage</option>
        </select>
        <label for="completePayload">JSON Payload</label>
        <textarea id="completePayload" maxlength="20000">{
  "person": {"이름": "김갑산", "직책": "대표"},
  "company": {"회사명": "(주)Example Company"},
  "limit": 2,
  "llm_confidence": false
}</textarea>
        <label class="small"><input id="rawComplete" type="checkbox" style="width:auto"> Raw JSON 보기</label>
        <button id="completeButton" type="submit">Run NoteApp</button>
      </form>
      <p class="small" style="margin-top:14px">로그인, CAPTCHA, 계정 페이지 우회 자동화는 막아둔다. Firecrawl/WaterCrawl/Apify는 원격 관리형이라 로컬 delay가 target throttle이 아니다.</p>
    </section>
    <section class="panel">
      <h2>Result</h2>
      <div id="summary" class="status"></div>
      <div id="results"></div>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    let mode = 'search';

    function pill(text, kind='') {
      return `<span class="pill ${kind}">${text}</span>`;
    }

    function switchMode(next) {
      mode = next;
      $('tabSearch').classList.toggle('active', next === 'search');
      $('tabCrawl').classList.toggle('active', next === 'crawl');
      $('tabNoteApp').classList.toggle('active', next === 'noteapp');
      $('searchForm').classList.toggle('hidden', next !== 'search');
      $('crawlForm').classList.toggle('hidden', next !== 'crawl');
      $('noteappForm').classList.toggle('hidden', next !== 'noteapp');
      $('results').innerHTML = '';
      $('summary').innerHTML = '';
    }

    async function loadProviders() {
      const res = await fetch('/api/providers');
      const data = await res.json();
      $('provider').innerHTML = data.search.map((item) => {
        const ready = item.ready ? 'key present' : 'missing key';
        return `<option value="${item.provider}">${item.provider} (${ready})</option>`;
      }).join('');
      $('crawler').innerHTML = data.crawlers.map((item) => {
        const ready = item.ready ? 'ready' : 'missing key/actor';
        return `<option value="${item.crawler}">${item.crawler} (${ready})</option>`;
      }).join('');
      const readyProviders = data.search.filter((item) => item.ready).length;
      const readyCrawlers = data.crawlers.filter((item) => item.ready).length;
      $('healthPills').innerHTML = [
        pill(`key-present providers ${readyProviders}`, readyProviders ? 'ok' : 'bad'),
        pill(`ready crawlers ${readyCrawlers}`, readyCrawlers ? 'ok' : 'bad'),
        pill('dashboard')
      ].join('');
    }

    function renderError(err) {
      $('summary').innerHTML = pill('failed', 'bad');
      $('results').innerHTML = `<div class="error">${escapeHtml(err)}</div>`;
    }

    function escapeHtml(value) {
      return String(value || '').replace(/[&<>"']/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }

    function renderSearch(payload, raw) {
      $('summary').innerHTML = [
        pill(payload.provider),
        pill(`${payload.count} records`, payload.count ? 'ok' : 'warn'),
        payload.elapsed_seconds != null ? pill(`${payload.elapsed_seconds}s`) : ''
      ].join('');
      if (raw) {
        $('results').innerHTML = `<pre>${escapeHtml(JSON.stringify(payload, null, 2))}</pre>`;
        return;
      }
      $('results').innerHTML = payload.records.map((item) => `
        <article class="result">
          <h3>${escapeHtml(item.title)}</h3>
          <a href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer">${escapeHtml(item.url)}</a>
          <div class="snippet">${escapeHtml(item.snippet || '')}</div>
          <div class="status">${pill(item.source)}${pill(`confidence ${item.confidence}`)}${pill(item.policy_risk, item.policy_risk === 'low' ? 'ok' : 'warn')}</div>
        </article>
      `).join('') || '<p>No records.</p>';
    }

    function renderCrawl(payload, raw) {
      const result = payload.result || {};
      $('summary').innerHTML = [
        pill(payload.crawler),
        result.error ? pill('error', 'bad') : pill('ok', 'ok'),
        pill(`${result.text_length || 0} chars`),
        result.status_code ? pill(`HTTP ${result.status_code}`) : ''
      ].join('');
      if (raw) {
        $('results').innerHTML = `<pre>${escapeHtml(JSON.stringify(payload, null, 2))}</pre>`;
        return;
      }
      const primaryImage = result.primary_thumb_url || '';
      $('results').innerHTML = `
        <article class="result crawl-result">
          <div class="result-media ${primaryImage ? '' : 'empty'}">
            ${primaryImage ? `<a href="${escapeHtml(primaryImage)}" target="_blank" rel="noreferrer"><img src="${escapeHtml(primaryImage)}" alt="${escapeHtml(result.title || 'Crawled page image')}" referrerpolicy="no-referrer" decoding="async" onerror="this.closest('.result-media').classList.add('empty');this.parentElement.remove()"></a>` : ''}
            <span class="image-fallback">No preview image</span>
          </div>
          <div class="result-body">
            <h3>${escapeHtml(result.title || result.canonical_url || '')}</h3>
            <a href="${escapeHtml(result.url || '')}" target="_blank" rel="noreferrer">${escapeHtml(result.url || '')}</a>
            ${result.error ? `<div class="error">${escapeHtml(result.error)}</div>` : ''}
            <div class="snippet">${escapeHtml(result.text_preview || '')}</div>
          </div>
        </article>`;
    }

    function renderComplete(payload, path, raw) {
      const items = payload.items || [];
      $('summary').innerHTML = [
        pill(path),
        pill(payload.completion_status || 'completed', payload.completion_status === 'completed' ? 'ok' : 'warn'),
        pill(`${items.length} items`, items.length ? 'ok' : 'warn'),
        payload.gateway_elapsed_seconds != null ? pill(`${payload.gateway_elapsed_seconds}s`) : ''
      ].join('');
      if (raw) {
        $('results').innerHTML = `<pre>${escapeHtml(JSON.stringify(payload, null, 2))}</pre>`;
        return;
      }
      $('results').innerHTML = items.map((item) => {
        const title = item.title || item.award_name || item.platform || item.name || item.url || item.article_url || 'Untitled';
        const url = item.url || item.article_url || '';
        const snippet = item.snippet || item.summary || item.description || '';
        const scores = item.scores || {};
        return `
          <article class="result">
            <h3>${escapeHtml(title)}</h3>
            ${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${escapeHtml(url)}</a>` : ''}
            <div class="snippet">${escapeHtml(snippet)}</div>
            <div class="status">
              ${item.identity_label ? pill(`identity ${item.identity_label}`) : ''}
              ${scores.workflow_score != null ? pill(`workflow ${scores.workflow_score}`) : ''}
              ${scores.identity_match_score != null ? pill(`match ${scores.identity_match_score}`) : ''}
            </div>
          </article>`;
      }).join('') || '<p>No items.</p>';
    }

    async function postJSON(path, payload) {
      const res = await fetch(path, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      return data;
    }

    $('searchForm').addEventListener('submit', async (event) => {
      event.preventDefault();
      $('searchButton').disabled = true;
      try {
        const data = await postJSON('/api/search', {
          provider: $('provider').value,
          query: $('query').value,
          limit: Number($('limit').value),
          intent: $('intent').value
        });
        renderSearch(data, $('rawSearch').checked);
      } catch (err) {
        renderError(err.message);
      } finally {
        $('searchButton').disabled = false;
      }
    });

    $('crawlForm').addEventListener('submit', async (event) => {
      event.preventDefault();
      $('crawlButton').disabled = true;
      try {
        const data = await postJSON('/dashboard/api/crawl', {
          url: $('url').value,
          crawler: $('crawler').value,
          respect_robots: $('respectRobots').checked,
          base_delay_seconds: Number($('baseDelay').value),
          government_delay_seconds: Number($('govDelay').value)
        });
        renderCrawl(data, $('rawCrawl').checked);
      } catch (err) {
        renderError(err.message);
      } finally {
        $('crawlButton').disabled = false;
      }
    });

    $('noteappForm').addEventListener('submit', async (event) => {
      event.preventDefault();
      $('completeButton').disabled = true;
      const path = $('completePath').value;
      try {
        const payload = JSON.parse($('completePayload').value || '{}');
        const data = await postJSON(path, payload);
        renderComplete(data, path, $('rawComplete').checked);
      } catch (err) {
        renderError(err.message);
      } finally {
        $('completeButton').disabled = false;
      }
    });

    loadProviders().catch((err) => renderError(err.message));
  </script>
</body>
</html>
"""


ARTICLUSTER_HTML = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ArticleCluster PoC</title>
  <style>
    :root { color-scheme:light; --bg:#f7f8fa; --panel:#fff; --text:#18212f; --muted:#657184; --line:#d9dee8; --accent:#0d766e; --accent-soft:#eaf7f4; --warn:#ad5b00; --sentiment-negative:#c62828; --sentiment-positive:#2563eb; --sentiment-neutral:#6b7280; --sentiment-uncertain:#9a6700; font-family:Inter,ui-sans-serif,system-ui,sans-serif; }
    * { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--text); }
    .wrap { max-width:1180px; margin:0 auto; padding:20px; } header { background:#fff; border-bottom:1px solid var(--line); }
    .top { display:flex; justify-content:space-between; gap:16px; align-items:center; } h1 { margin:0 0 4px; font-size:22px; }
    h2 { margin:0; font-size:17px; } h3 { margin:0; font-size:14px; }
    p { color:var(--muted); line-height:1.5; } .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; margin-top:16px; }
    .section-head { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; flex-wrap:wrap; margin-bottom:12px; }
    .eyebrow { display:block; color:var(--accent); font-size:11px; font-weight:850; letter-spacing:.08em; margin-bottom:4px; }
    .badge { display:inline-flex; border:1px solid #9bd5cd; border-radius:999px; padding:4px 8px; background:var(--accent-soft); color:#075f57; font-size:11px; font-weight:800; }
    .badge.warn { border-color:#ffd59a; background:#fff8eb; color:var(--warn); }
    .run-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; }
    label { display:block; margin:0 0 6px; font-size:13px; font-weight:650; } input { width:100%; border:1px solid var(--line); border-radius:6px; padding:10px; font:inherit; }
    .check { display:flex; align-items:center; gap:8px; margin-top:12px; color:var(--muted); font-size:13px; }
    .check input { width:auto; } button { margin-top:14px; border:0; border-radius:6px; padding:10px 14px; background:var(--accent); color:white; font-weight:700; cursor:pointer; }
    button.secondary { margin-top:0; border:1px solid var(--line); background:#fff; color:var(--text); } button:disabled { opacity:.55; cursor:wait; }
    pre { white-space:pre-wrap; overflow:auto; background:#0f172a; color:#e2e8f0; border-radius:6px; padding:14px; font-size:12px; line-height:1.45; }
    .response { margin:0; max-height:480px; } code { color:var(--accent); } a { color:var(--accent); }
    .example-intro { display:flex; justify-content:space-between; gap:12px; align-items:center; flex-wrap:wrap; }
    .example-status { color:var(--muted); font-size:12px; }
    .stats { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; margin:14px 0; }
    .stat { border:1px solid var(--line); border-radius:7px; background:#fbfcfe; padding:10px; } .stat-label { color:var(--muted); font-size:11px; } .stat-value { margin-top:3px; font-weight:850; font-size:14px; }
    .groups { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }
    .group { border:1px solid var(--line); border-radius:8px; padding:12px; background:#fff; } .group-head { display:flex; justify-content:space-between; gap:8px; align-items:flex-start; }
    .group-title { font-weight:850; line-height:1.4; } .group-meta { color:var(--muted); font-size:11px; white-space:nowrap; } .terms { color:var(--muted); font-size:11px; margin:7px 0; }
    .articles { display:grid; gap:5px; margin:8px 0 0; padding-left:18px; } .articles li { font-size:12px; line-height:1.4; } .article-meta { color:var(--muted); font-size:11px; }
    .article-title { display:inline; } .article-link { color:inherit; } .sentiment-mark { display:inline-block; width:1em; font-weight:900; } .sentiment-negative, .sentiment-negative a { color:var(--sentiment-negative); } .sentiment-positive, .sentiment-positive a { color:var(--sentiment-positive); } .sentiment-neutral, .sentiment-neutral a { color:var(--sentiment-neutral); } .sentiment-uncertain, .sentiment-uncertain a { color:var(--sentiment-uncertain); }
    details { margin-top:14px; } summary { cursor:pointer; color:var(--accent); font-size:13px; font-weight:750; }
    @media (max-width:760px) { .run-grid, .groups { grid-template-columns:1fr; } .stats { grid-template-columns:repeat(2,minmax(0,1fr)); } .top { align-items:flex-start; flex-direction:column; } }
  </style>
</head>
<body>
  <header><div class="wrap top"><div><h1>ArticleCluster PoC</h1><p>실시간 실행과 저장 데이터 예시를 분리해 확인합니다.</p></div><a href="/docs">API Docs</a></div></header>
  <main class="wrap">
    <section class="panel">
      <div class="section-head"><div><span class="eyebrow">LIVE REQUEST</span><h2>1. 실시간 군집화 실행</h2></div><span class="badge">POST /articluster</span></div>
      <p>보관한 기사 목록을 붙여 넣으면 해당 목록만 군집화합니다. 감성분석을 선택하면 지정한 회사을 기준으로 기사별 감성을 함께 표시합니다. 검색이나 본문 수집은 실행하지 않습니다.</p>
      <form id="cluster-form">
        <label for="target">분석 대상 회사<input id="target" placeholder="예: Example Company"></label>
        <label for="articles">기사 목록 JSON (1~50개)</label>
        <textarea id="articles" rows="12" style="width:100%" spellcheck="false">[{"title":"Example Company, 신규 서비스 공개","article_url":"https://example.com/news/1","snippet":"Example Company가 고객 지원 서비스를 출시했다."},{"title":"Example Company의 고객 지원 서비스 출시","article_url":"https://example.com/news/2","snippet":"고객 지원 신규 서비스가 공개됐다."}]</textarea>
        <label class="check"><input id="sentiment" type="checkbox"> 기사별 감성분석 함께 실행 (대상 입력 필요)</label>
        <button type="submit">군집화 실행</button>
      </form>
    </section>
    <section class="panel">
      <div class="section-head"><div><span class="eyebrow">LIVE RESPONSE</span><h2>실시간 실행 결과</h2></div></div>
      <div id="live-summary" class="stats"></div>
      <div id="live-groups" class="groups"></div>
      <details><summary>실시간 응답 JSON 보기</summary><pre id="output" class="response">아직 실행하지 않았습니다.</pre></details>
    </section>
    <section class="panel">
      <div class="section-head"><div><span class="eyebrow">OFFLINE FIXTURE</span><h2>2. 저장 데이터 예시</h2></div><span class="badge">GET /articluster/example</span></div>
      <div class="example-intro"><p>방금 Tieming dump에서 추출한 실제 기사 13건을 로컬 군집기로 묶은 표시용 예시입니다. 이 영역은 검색·crawl을 실행하지 않습니다.</p><button id="example-button" class="secondary" type="button">예시 다시 불러오기</button></div>
      <div id="example-status" class="example-status">예시를 불러오는 중...</div>
      <div id="example-summary" class="stats"></div>
      <div id="example-groups" class="groups"></div>
      <details><summary>offline 예시 JSON 보기</summary><pre id="example-output" class="response">아직 불러오지 않았습니다.</pre></details>
    </section>
  </main>
  <script>
    const form = document.getElementById('cluster-form');
    const output = document.getElementById('output');
    const liveSummary = document.getElementById('live-summary');
    const liveGroups = document.getElementById('live-groups');
    const exampleButton = document.getElementById('example-button');
    const exampleStatus = document.getElementById('example-status');
    const exampleSummary = document.getElementById('example-summary');
    const exampleGroups = document.getElementById('example-groups');
    const exampleOutput = document.getElementById('example-output');
    const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
    const safeHref = (value) => /^https?:\/\//i.test(String(value || '').trim()) ? escapeHtml(String(value).trim()) : '';
    const stat = (label, value) => `<div class="stat"><div class="stat-label">${escapeHtml(label)}</div><div class="stat-value">${escapeHtml(value)}</div></div>`;
    function sentimentInfo(article) {
      const sentiment = article?.article_sentiment || {};
      const label = sentiment.label || (sentiment.status === 'uncertain' ? 'uncertain' : null);
      if (!['negative', 'neutral', 'positive', 'uncertain'].includes(label)) return {className: '', mark: ''};
      return {
        className: `sentiment-${label}`,
        mark: label === 'positive' ? '+' : label === 'negative' ? '-' : ''
      };
    }
    function sentimentStat(value) {
      const articles = Array.isArray(value.items) && value.items.length
        ? value.items
        : (Array.isArray(value.clusters) ? value.clusters.flatMap((cluster) => Array.isArray(cluster.articles) ? cluster.articles : []) : []);
      const counts = {positive: 0, negative: 0, neutral: 0, uncertain: 0};
      articles.forEach((article) => {
        const sentiment = article?.article_sentiment || {};
        const label = sentiment.label || (sentiment.status === 'uncertain' ? 'uncertain' : null);
        if (counts[label] !== undefined) counts[label] += 1;
      });
      if (!articles.length || !Object.values(counts).some(Boolean)) return stat('기사 감성', value.article_sentiment ? '미분류' : '미실행');
      return `<div class="stat"><div class="stat-label">기사 감성</div><div class="stat-value"><span class="sentiment-positive">+${counts.positive}</span> <span class="sentiment-negative">-${counts.negative}</span> <span class="sentiment-neutral">중립 ${counts.neutral}</span>${counts.uncertain ? ` <span class="sentiment-uncertain">보류 ${counts.uncertain}</span>` : ''}</div></div>`;
    }
    function renderSummary(value, target, proposal = {}) {
      target.innerHTML = [
        stat('기사', `${value.clustered_count ?? value.count ?? 0}건`),
        stat('로컬 군집', `${value.cluster_count ?? 0}개`),
        sentimentStat(value),
        proposal.model ? stat('라벨 보조', proposal.model) : stat('상태', value.article_sentiment?.status || '완료')
      ].join('');
    }
    function articlesForCluster(value, cluster) {
      if (Array.isArray(cluster.articles)) return cluster.articles;
      const byId = new Map((Array.isArray(value.items) ? value.items : []).map((item) => [item.article_id, item]));
      return (Array.isArray(cluster.article_ids) ? cluster.article_ids : []).map((id) => byId.get(id)).filter(Boolean);
    }
    function renderGroups(value, target) {
      const clusters = Array.isArray(value.clusters) ? value.clusters : [];
      target.innerHTML = clusters.map((cluster) => {
        const articles = articlesForCluster(value, cluster);
        const articleHtml = articles.map((article) => {
          const title = escapeHtml(article.title || '(제목 없음)');
          const href = safeHref(article.article_url);
          const sentiment = sentimentInfo(article);
          const titleHtml = href ? `<a class="article-link" href="${href}" target="_blank" rel="noreferrer">${title}</a>` : title;
          const sentimentLabel = article.article_sentiment?.label || (article.article_sentiment?.status === 'uncertain' ? 'uncertain' : '');
          const markedTitle = `<span class="article-title ${sentiment.className}" title="${escapeHtml(sentimentLabel)}">${sentiment.mark ? `<span class="sentiment-mark">${sentiment.mark}</span>` : ''}${titleHtml}</span>`;
          const meta = [article.source, article.published_at].filter(Boolean).map(escapeHtml).join(' · ');
          return `<li>${markedTitle}${meta ? ` <span class="article-meta">(${meta})</span>` : ''}</li>`;
        }).join('');
        const terms = Array.isArray(cluster.top_terms) ? cluster.top_terms.map(escapeHtml).join(' · ') : '';
        return `<article class="group"><div class="group-head"><div class="group-title">${escapeHtml(cluster.display_event_summary || cluster.proposed_event_summary || cluster.cluster_id || '군집')}</div><div class="group-meta">${escapeHtml(cluster.article_count ?? articles.length)}건</div></div><div class="terms">${terms ? `핵심어: ${terms}` : ''}</div><ol class="articles">${articleHtml}</ol></article>`;
      }).join('') || '<p>표시할 예시 군집이 없습니다.</p>';
    }
    function renderExample(value) {
      const source = value.source || {};
      const proposal = value.label_proposal || {};
      renderSummary(value, exampleSummary, proposal);
      renderGroups(value, exampleGroups);
      exampleOutput.textContent = JSON.stringify(value, null, 2);
      exampleStatus.textContent = `${source.description || '저장 fixture'} · live search: ${source.live_search_called ? '실행됨' : '실행하지 않음'}`;
    }
    function renderLive(value) {
      renderSummary(value, liveSummary);
      renderGroups(value, liveGroups);
      output.textContent = JSON.stringify(value, null, 2);
    }
    async function loadExample() {
      exampleButton.disabled = true; exampleStatus.textContent = 'offline 예시를 불러오는 중...';
      try {
        const response = await fetch('/articluster/example', {cache:'no-store'});
        const value = await response.json();
        if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
        renderExample(value);
      } catch (error) {
        exampleStatus.textContent = `예시를 불러오지 못했습니다: ${error}`;
        exampleGroups.innerHTML = '';
        exampleOutput.textContent = String(error);
      } finally { exampleButton.disabled = false; }
    }
    form.addEventListener('submit', async (event) => {
      event.preventDefault(); output.textContent = '전달한 기사 목록 군집화 중...'; liveSummary.innerHTML = ''; liveGroups.innerHTML = '';
      try {
        const body = {items: JSON.parse(document.getElementById('articles').value), target: document.getElementById('target').value.trim(), article_sentiment: document.getElementById('sentiment').checked};
        const response = await fetch('/articluster', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
        const value = await response.json(); if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`); renderLive(value);
      } catch (error) { liveSummary.innerHTML = ''; liveGroups.innerHTML = ''; output.textContent = String(error); }
    });
    exampleButton.addEventListener('click', loadExample);
    loadExample();
  </script>
</body>
</html>
"""


DOCS_HTML = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Web Search Agent API Docs</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --text: #18212f;
      --muted: #657184;
      --line: #d9dee8;
      --accent: #0d766e;
      --warn: #ad5b00;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); }
    header { border-bottom: 1px solid var(--line); background: #fff; }
    .wrap { max-width: 1180px; margin: 0 auto; padding: 20px; }
    .top { display: flex; justify-content: space-between; gap: 16px; align-items: center; }
    .top-actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
    .nav-button {
      border: 1px solid var(--line); border-radius: 6px; background: #fff; color: #18212f;
      padding: 9px 11px; text-decoration: none; font-size: 13px; font-weight: 700;
    }
    .nav-button:hover { border-color: var(--accent); color: var(--accent); }
    h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: 0; }
    h2 { font-size: 17px; margin: 0 0 12px; letter-spacing: 0; }
    h3 { font-size: 14px; margin: 0 0 8px; letter-spacing: 0; }
    p { margin: 0; color: var(--muted); line-height: 1.5; }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; align-items: start; }
    .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px; }
    .full { grid-column: 1 / -1; }
    .endpoint { border-top: 1px solid var(--line); padding-top: 14px; margin-top: 14px; }
    .endpoint:first-of-type { border-top: 0; padding-top: 0; margin-top: 0; }
    .contract { border: 1px solid var(--line); border-radius: 8px; background: #fbfcfe; padding: 12px; margin-top: 14px; display: grid; gap: 12px; }
    .endpoint-line { display: flex; gap: 9px; align-items: center; flex-wrap: wrap; }
    .method {
      display: inline-flex; align-items: center; justify-content: center; min-width: 52px;
      border-radius: 5px; background: #0d766e; color: #fff; padding: 4px 7px;
      font-size: 12px; font-weight: 900; letter-spacing: 0;
    }
    .code-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; align-items: start; }
    .code-title { color: var(--text); font-size: 12px; font-weight: 850; margin-bottom: 6px; }
    .field-list { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; }
    .field-list code { display: block; width: 100%; }
    code { background: #eef2f7; border: 1px solid var(--line); border-radius: 4px; padding: 1px 4px; }
    pre { background: #101828; color: #e6edf7; border-radius: 8px; padding: 12px; overflow: auto; margin: 9px 0 0; font-size: 12px; line-height: 1.45; }
    .pill { display: inline-block; border: 1px solid var(--line); border-radius: 999px; padding: 4px 8px; background: #fff; color: #334155; font-size: 12px; margin: 0 5px 6px 0; }
    .warn { color: var(--warn); border-color: #ffd59a; background: #fff8eb; }
    .small { font-size: 12px; color: var(--muted); }
    details.tester { border: 1px solid var(--line); border-radius: 8px; margin-top: 12px; background: #fbfcfe; }
    details.tester summary { cursor: pointer; padding: 10px 12px; font-weight: 800; font-size: 13px; color: var(--text); }
    .test-body { border-top: 1px solid var(--line); padding: 12px; display: grid; gap: 10px; }
    .form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .form-grid.three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .field-section { display: grid; gap: 10px; }
    .field-title { color: var(--text); font-size: 13px; font-weight: 850; }
    .tester-shell { display: grid; grid-template-columns: 310px minmax(0, 1fr); gap: 16px; margin-top: 14px; align-items: start; }
    .endpoint-rail { display: grid; gap: 10px; min-width: 0; }
    .endpoint-tabs { display: flex; gap: 6px; flex-wrap: wrap; }
    .endpoint-tab {
      border: 1px solid var(--line); border-radius: 999px; background: #fff; color: #334155;
      padding: 7px 10px; font-size: 12px; font-weight: 800;
    }
    .endpoint-tab.active { border-color: #7cc9bd; background: #eaf7f4; color: #075f57; }
    .endpoint-list {
      display: grid; gap: 6px; max-height: 690px; overflow: auto; padding-right: 4px;
      border-top: 1px solid var(--line); padding-top: 10px;
    }
    .endpoint-choice {
      width: 100%; border: 1px solid transparent; border-radius: 7px; background: transparent;
      color: var(--text); padding: 9px 10px; display: grid; gap: 3px; text-align: left;
    }
    .endpoint-choice:hover { border-color: var(--line); background: #f8fafc; }
    .endpoint-choice.active { border-color: #91d5cc; background: #eef9f6; }
    .endpoint-choice-title { font-size: 13px; font-weight: 850; }
    .endpoint-choice-meta { display: flex; justify-content: space-between; gap: 8px; align-items: center; color: var(--muted); font-size: 11px; }
    .endpoint-choice-path { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .tester-panel {
      border: 1px solid var(--line); border-radius: 8px; background: #fff;
      padding: 16px; display: grid; gap: 14px; min-width: 0;
    }
    .tester-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; border-bottom: 1px solid var(--line); padding-bottom: 12px; }
    .tester-title { font-size: 18px; font-weight: 900; margin-bottom: 4px; }
    .tester-path { font-size: 12px; color: var(--muted); }
    .keyword-layout { display: grid; grid-template-columns: minmax(300px, 0.82fr) minmax(0, 1.18fr); gap: 16px; align-items: start; }
    .search-field-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; }
    .search-field-toggle {
      border: 1px solid var(--line); border-radius: 7px; padding: 7px 8px; display: flex;
      align-items: center; gap: 7px; color: var(--text); font-size: 12px; font-weight: 800; background: #fbfcfe;
    }
    .search-field-toggle input { width: auto; }
    .search-field-toggle span { white-space: nowrap; }
    .search-query-preview {
      min-height: 38px; border: 1px dashed #b7c3d4; border-radius: 7px; background: #f8fafc;
      padding: 9px 10px; color: #1f2937; font-size: 12px; line-height: 1.45; word-break: keep-all;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    .eval-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
    .option-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
    .checkbox-row { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12px; font-weight: 750; }
    .checkbox-row input { width: auto; }
    .tester-actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    label { display: grid; gap: 5px; color: var(--muted); font-size: 12px; font-weight: 700; }
    input, textarea, select {
      width: 100%; border: 1px solid var(--line); border-radius: 6px; padding: 9px 10px;
      font: inherit; font-size: 13px; background: #fff; color: var(--text);
    }
    textarea { min-height: 92px; resize: vertical; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    button {
      border: 0; border-radius: 6px; background: var(--accent); color: #fff;
      padding: 10px 12px; font-weight: 800; cursor: pointer; width: fit-content;
    }
    button:disabled { opacity: 0.55; cursor: wait; }
    .result-pre { max-height: 460px; }
    @media (max-width: 900px) {
      .grid, .tester-shell, .keyword-layout, .code-grid, .field-list { grid-template-columns: 1fr; }
      .top { align-items: flex-start; flex-direction: column; }
      .option-grid, .eval-grid, .search-field-list { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div class="wrap top">
      <div>
        <h1>API Docs</h1>
        <p>Search, Crawl, Scrap 워크플로를 게이트웨이 형태로 제공하는 API 문서입니다.</p>
      </div>
      <div class="top-actions">
        <a class="nav-button" href="/">Dashboard</a>
        <a class="nav-button" href="/observability">Observability</a>
      </div>
    </div>
  </header>
  <main class="wrap grid">
    <section class="panel full">
      <h2>Base</h2>
      <p><code>http://127.0.0.1:9101</code></p>
      <p class="small">모든 엔드포인트는 JSON POST 본문을 받습니다. Query 값과 URL 값은 사용량 원장에 기록하지 않습니다.</p>
      <p class="small"><code>/api/search</code>는 질의어를 기반으로 URL과 검색 결과를 찾습니다. <code>/api/crawl</code>은 이미 알고 있는 단일 URL에서 본문을 추출합니다. <code>/api/scrap</code>은 교보문고, KoreaScience, eArticle, 대한민국 상훈, 기관/커리어 페이지 등 출처별 워크플로를 감싼 래퍼입니다.</p>
      <p class="small">루트 엔드포인트인 <code>/api/search</code>와 <code>/api/crawl</code>은 GPT를 호출하거나 기사 감성분류를 수행하지 않습니다. 묶음형 Search/Crawl 엔드포인트와 Scrap 워크플로는 결정적 식별 점수를 수행할 수 있으며, 요청하신 경우에만 선택적으로 GPT confidence를 추가합니다.</p>
      <p class="small">점수화가 가능한 엔드포인트를 호출하실 때에는 원본 소스 데이터를 담은 nullable <code>person</code> 및 <code>company</code> 객체를 함께 보내 주십시오. 각 결과의 마지막에는 <code>scores</code>가 추가되며, 그 안에 <code>confidence</code>, <code>llm_confidence</code>, <code>identity_match_score</code>, <code>identity_match</code>가 정리됩니다. 결정적 식별 점수만 유지하고 GPT를 생략하시려면 <code>{"llm_confidence":false}</code>를 보내시면 됩니다.</p>
    </section>

    <section class="panel full">
      <h2>Endpoint Testers</h2>
      <p class="small"><b>Search keywords</b>는 17개 평가 항목 중 실제 검색어 생성에 사용할 필드를 선택하는 영역입니다. <b>Evaluation keywords</b>는 결과가 대상 인물 또는 회사와 일치하는지 판단하기 위한 기준값 전체를 입력하는 영역입니다.</p>
      <div class="tester-shell">
        <aside class="endpoint-rail">
          <div id="endpointTabs" class="endpoint-tabs"></div>
          <div id="endpointList" class="endpoint-list"></div>
        </aside>
        <section id="endpointTester" class="tester-panel"></section>
      </div>
    </section>

    <section class="panel full">
      <h2>NoteApp Complete API</h2>
      <p class="small">NoteApp용 목적별 완성형 엔드포인트입니다. 입력으로는 기존 17개 <code>person</code>/<code>company</code> 필드를 그대로 전달하시면 되며, 서버가 목적에 맞는 검색 질의를 구성하고 결과를 정리하여 반환합니다.</p>
      <p class="small">여섯 엔드포인트는 모두 JSON 입력을 사용합니다. <code>complete</code> 응답은 동기 집계 방식으로 동작하므로 포함된 하위 검색·스크랩 작업이 완료된 뒤 반환되며, 응답에는 <code>response_mode</code>, <code>completion_status</code>, <code>subtasks</code>가 포함됩니다.</p>
      <p class="small">모든 POST API의 신규 앱 연동은 <code>accountId</code>를 반드시 전송합니다. 최상위 또는 <code>inputs</code>·<code>params</code>·<code>payload</code>에 있는 계정을 읽고 JSON 응답 최상위에도 보존합니다. <code>account_id</code>는 호환 이름입니다. <code>user_id</code>·<code>userId</code> 등 내부 owner는 application 계정으로 변환하지 않습니다. 명시 계정이 없으면 계정은 미확정이며 개인 맥락조회에 사용하지 않습니다. 문자열은 trim 후 최대 200자입니다. 계정 없는 기존 공개 검색은 유지되며 계정 값 자체로 인증하거나 RDB 조회를 실행하지 않습니다.</p>
      <p class="small"><code>news</code>, <code>publication</code>, <code>award</code>, <code>social</code>, <code>profile</code> 요청에 top-level <code>url</code>을 전달하면 검색 API를 건너뛰고 해당 URL만 수집합니다. <code>company-homepage</code>는 <code>company.홈페이지</code> 또는 <code>url</code>을 <code>auto</code>로 수집하고 실패하면 Firecrawl scrape를 사용합니다.</p>
      <p class="small">각 <code>items</code> 항목에는 결과 키워드 일치 조합에 따른 <code>identity_label</code>이 포함됩니다. 기존 <code>scores</code> 및 <code>raw_payload_json</code>은 유지되며, 뉴스 항목에만 <code>thumb_urls</code>와 <code>primary_thumb_url</code>이 항상 포함됩니다.</p>
      <p class="small"><code>news</code>는 이번 응답의 최대 10개 기사에 군집화와 감성분석을 항상 실행합니다. <code>items[].cluster_id</code>, <code>items[].article_sentiment</code>와 최상위 처리 요약을 함께 반환합니다. 기사 목록은 최신순을 유지하며, 이전에 저장한 기사와의 재군집화는 수행하지 않습니다. 감성은 회사 <code>search_keyword</code>에 대한 기사별 긍정·부정·중립을 한 번 분류하고, 확신이 낮은 방향성은 보류합니다. 저장 기사 중 선택한 목록을 별도로 분석하려면 <code>/articluster</code>를 사용합니다.</p>
      <p><code>POST /v1/complete/linkClassification</code> · <code>POST /v1/complete/news</code> · <code>POST /articluster</code> · <code>POST /v1/complete/publication</code> · <code>POST /v1/complete/award</code> · <code>POST /v1/complete/social</code> · <code>POST /v1/complete/profile</code> · <code>POST /v1/complete/company-homepage</code></p>
      <div class="endpoint">
        <h3><code>POST /v1/complete/linkClassification</code></h3>
        <p class="small">주어진 URL을 분석하여 <code>google_drive</code>, <code>notion</code>, <code>file</code>, <code>webpage</code> 타입으로 분류하고 크롤링 가능 여부를 반환합니다.</p>
        <div class="code-title">curl (Windows CMD)</div>
        <pre>curl -sS -X POST "http://127.0.0.1:9101/v1/complete/linkClassification" -H "Content-Type: application/json" -d "{\\\"url\\\":\\\"https://drive.google.com/file/d/1abc/view\\\"}"</pre>
        <div class="contract">
          <div class="endpoint-line"><span class="method">POST</span><code>/v1/complete/linkClassification</code></div>
          <div class="code-grid">
            <div>
              <div class="code-title">Request body</div>
              <pre>{
  "url": "https://drive.google.com/file/d/1abc/view"
}</pre>
            </div>
            <div>
              <div class="code-title">Response shape</div>
              <pre>{
  "url": "https://drive.google.com/file/d/1abc/view",
  "classification": "google_drive",
  "crawlable": false
}</pre>
            </div>
          </div>
        </div>
      </div>
      <div class="endpoint">
        <h3><code>POST /articluster</code> · ArticleCluster PoC</h3>
        <p class="small">요청의 <code>items</code> 기사 목록(1~50개)만 사건·서비스별로 군집화합니다. 검색·crawl은 실행하지 않습니다. <code>target</code>으로 분석할 회사을 지정하고 <code>article_sentiment=true</code>이면 기사별 감성을 함께 분류합니다. 감성은 군집 생성 기준이 아닙니다. 기본 벡터화는 로컬 CPU <code>tfidf_word_char_v1</code>입니다.</p>
        <p class="small"><code>GET /articluster/example</code>은 검색을 실행하지 않고 저장 기사 fixture의 offline 예시를 반환합니다.</p>
        <div class="code-title">curl (Linux/macOS)</div>
        <pre>curl -sS -X POST "http://127.0.0.1:9101/articluster" -H "Content-Type: application/json" -d '{"target":"Example Company","items":[{"title":"Example Company, 신규 서비스 출시","article_url":"https://example.com/news/1"}],"article_sentiment":true}'</pre>
        <div class="contract">
          <div class="endpoint-line"><span class="method">POST</span><code>/articluster</code></div>
          <div class="code-grid">
            <div>
              <div class="code-title">Request body</div>
              <pre>{
  "target": "Example Company",
  "items": [{"title":"Example Company, 신규 서비스 출시","article_url":"https://example.com/news/1","published_at":"2026-09-07"}],
  "article_sentiment": true,
  "similarity_threshold": 0.30,
  "time_window_days": 90,
  "embedding_model": ""
}</pre>
            </div>
            <div>
              <div class="code-title">Response shape</div>
              <pre>{
  "schema_version": "article_cluster_poc/v1",
  "method": "centroid_threshold_agglomerative/v1",
  "cluster_count": 2,
  "clusters": [{
    "cluster_id": "cluster-001",
    "article_count": 3,
    "top_terms": ["hbm6", "양산"],
    "representative_article_id": "article-...",
    "representative_article": {"title": "..."}
  }],
  "unclustered": [],
  "items": [{"article_id": "article-...", "cluster_id": "cluster-001", "cluster_role": "representative", "article_sentiment": {"label": "positive", "score": 0.81}}],
  "article_sentiment": {"status": "ok", "target": "SK하이닉스", "items_scored": 3},
  "parameters": {"embedding_backend": "tfidf_word_char_v1", "ann_index": "not_used_in_poc_exact_scan; HNSW-compatible vector boundary"}
}</pre>
            </div>
          </div>
        </div>
        <p class="small">viewer는 긍정 기사를 파란색 <code>+</code>, 부정 기사를 빨간색 <code>-</code>, 중립 기사를 회색, 확신이 낮은 방향성은 황색 보류로 표시합니다. 감성분류는 타깃 기업에 대한 방향성 신호이며, gold label이나 재무적 영향 예측값이 아닙니다.</p>
      </div>
      <div class="endpoint">
        <h3><code>POST /v1/complete/news</code></h3>
        <p class="small">Naver News와 Serper를 병렬 검색한 뒤 최종 기사 메타데이터를 <code>static</code>, <code>respect_robots=false</code>로 응답 예산 안에서 수집합니다. <code>crawl_business_news=true</code>이면 한국 기업뉴스 상위 결과만 <code>auto</code>로 본문을 확인하고, 모두 실패할 때 Firecrawl Search를 1회 사용합니다. 최종 검색 결과에는 기준 검색어에 대한 감성분류를 항상 실행합니다. News 요청에는 군집·감성 활성화 옵션이 없습니다. 호출하면 검색·군집화·긍정·부정·중립 감성분석까지 수행합니다. 회사와의 관계 가치(성과·안부/주의·대화 소재)를 먼저 기록하고, 고정밀 제목 노이즈는 <code>search_metadata.quality</code>로 제거·기록합니다. 근거 부족·모델 오류·저확신 방향성은 감성 status로 구분하며 중립으로 바꾸지 않습니다. 가치 신호와 보류 정책은 추가 provider/LLM 호출을 만들지 않습니다.</p>
        <p class="small">군집화는 로컬 CPU <code>tfidf_word_char_v1</code>으로 실행하며 추가 검색·LLM 호출이 없습니다. <code>article_cluster.scope=response_items</code>이며 새 분석마다 군집 ID를 만듭니다. 같은 회사명·검색어의 완성 결과는 24시간 캐시하여 기사·군집 ID·감성·검색 시각을 그대로 반환합니다. 캐시 적중 시 검색·수집·분석을 다시 실행하지 않으며 <code>cache.hit=true</code>입니다. <code>limit</code>·본문 수집 옵션은 최초 생성 때 적용되고 캐시 유효기간에는 목록이 동일합니다. 검색 0건도 정상 응답이며, 군집화 실패 시 검색 결과를 그대로 반환하고 <code>article_cluster.status=failed</code>를 표시합니다.</p>
        <div class="code-title">curl (Windows CMD)</div>
        <pre>curl -sS -X POST "http://127.0.0.1:9101/v1/complete/news" -H "Content-Type: application/json" -d "{\\\"company\\\":{\\\"회사명\\\":\\\"예시회사\\\"},\\\"limit\\\":3}"</pre>
        <div class="contract">
          <div class="endpoint-line"><span class="method">POST</span><code>/v1/complete/news</code></div>
          <div class="code-grid">
            <div>
              <div class="code-title">Request body</div>
              <pre>{
  "company": {"회사명": "예시회사", "업종": "AI 소프트웨어"},
  "limit": 5,
  "llm_confidence": false
}</pre>
            </div>
            <div>
              <div class="code-title">Response shape</div>
              <pre>{
  "schema_version": "noteapp_complete/v1",
  "purpose": "news",
  "response_mode": "synchronous",
  "completion_status": "completed",
  "subtasks": [{"name": "naver_news_search", "status": "completed"}],
  "cache": {"hit": false, "ttl_seconds": 86400},
  "search_keyword": "예시회사",
  "search_metadata": {"searched_at":"2026-09-07T00:00:00+00:00","queries":[{"provider":"naver_news","query":"예시회사"}]},
  "items": [
    {
      "title": "홍길동 예시회사 관련 뉴스",
      "search_keyword": "예시회사",
      "source": "news.example",
      "article_url": "https://news.example/article",
      "snippet": "뉴스 요약",
      "published_at": "2026-06-25T00:00:00Z",
      "article_id": "article-example",
      "cluster_id": "news-0123456789abcdef0123456789abcdef-cluster-001",
      "cluster_role": "representative",
      "cluster_similarity": 1.0,
      "identity_label": "C",
      "thumb_urls": [],
      "primary_thumb_url": null,
      "article_sentiment": {
        "status": "ok",
        "label": "positive",
        "score": 0.81,
        "intensity": 5,
        "confidence": 0.88,
        "probabilities": {"negative": 0.07, "neutral": 0.05, "positive": 0.88},
        "evidence_scope": "title_snippet"
      },
      "scores": {"confidence": 0.35},
      "raw_payload_json": {"record": {}, "scores": {}}
    }
  ],
  "article_sentiment": {"status": "ok", "target": "예시회사", "items_scored": 1},
  "article_cluster": {
    "status": "ok",
    "scope": "response_items",
    "schema_version": "article_cluster_poc/v1",
    "count": 1,
    "cluster_count": 1,
    "clustered_count": 1,
    "unclustered_count": 0,
    "clusters": [{
      "cluster_id": "news-0123456789abcdef0123456789abcdef-cluster-001",
      "article_count": 1,
      "article_ids": ["article-example"],
      "representative_article_id": "article-example"
    }],
    "unclustered": [],
    "warnings": [],
    "parameters": {"embedding_backend": "tfidf_word_char_v1", "embedding_model": null}
  }
}</pre>
            </div>
          </div>
        </div>
      </div>
      <div class="endpoint">
        <h3><code>POST /v1/complete/company-homepage</code></h3>
        <p class="small">회사 공식 홈페이지를 별도로 <code>auto</code> 크롤링하여 본문 미리보기와 이미지 URL을 반환합니다. 일반 오류나 빈 본문이면 Firecrawl scrape를 한 번 사용하고, 봇 챌린지는 우회하지 않습니다.</p>
        <div class="code-title">curl (Windows CMD)</div>
        <pre>curl -sS -X POST "http://127.0.0.1:9101/v1/complete/company-homepage" -H "Content-Type: application/json" -d "{\"company\":{\"회사명\":\"Example Company\",\"홈페이지\":\"https://example.com/\"}}"</pre>
        <div class="contract">
          <div class="endpoint-line"><span class="method">POST</span><code>/v1/complete/company-homepage</code></div>
          <div class="code-grid">
            <div>
              <div class="code-title">Request body</div>
              <pre>{
  "company": {"회사명": "Example Company", "홈페이지": "https://example.com/"}
}</pre>
            </div>
            <div>
              <div class="code-title">Response shape</div>
              <pre>{
  "schema_version": "noteapp_complete/v1",
  "purpose": "company-homepage",
  "completion_status": "completed",
  "subtasks": [{"name": "company_homepage_auto_crawl", "status": "completed"}],
  "items": [{
    "company_name": "Example Company",
    "homepage_url": "https://example.com/",
    "title": "Example Company",
    "text_preview": "회사 홈페이지 본문...",
    "crawler": "static",
    "identity_label": "C",
    "scores": {"confidence": 0.7},
    "raw_payload_json": {"homepage_crawl": {}, "attempts": []}
  }]
}</pre>
            </div>
          </div>
        </div>
      </div>
      <div class="endpoint">
        <h3><code>POST /v1/complete/publication</code></h3>
        <p class="small">저서 및 논문 범주를 함께 조회하여 책, 논문, 학술자료 후보를 수집하고, 출판물 중심의 evidence 목록으로 정리하여 반환합니다.</p>
        <div class="contract">
          <div class="endpoint-line"><span class="method">POST</span><code>/v1/complete/publication</code></div>
          <div class="code-grid">
            <div>
              <div class="code-title">Request body</div>
              <pre>{
  "person": {"이름": "홍길동", "영문명": "Gildong Hong"},
  "company": {"회사명": "예시회사"},
  "limit": 5,
  "crawl_top": true,
  "crawler": "firecrawl",
  "llm_confidence": false
}</pre>
            </div>
            <div>
              <div class="code-title">Response shape</div>
              <pre>{
  "schema_version": "noteapp_complete/v1",
  "purpose": "publication",
  "completion_status": "completed",
  "subtasks": [{"name": "person_evidence_publication", "categories": ["books", "publications"]}],
  "items": [
    {
      "title": "홍길동의 출판물",
      "publisher": "출판사 또는 학술 DB",
      "publication_year": "2024",
      "summary": "예시회사에서 축적한 경험을 정리한 책",
      "author_role": "저자 정보",
      "url": "https://source.example/item",
      "category": "books",
      "identity_label": "A",
      "scores": {"workflow_score": 0.82, "identity_match_score": 0.76},
      "raw_payload_json": {"candidate": {}, "primary_record": {}, "scores": {}}
    }
  ]
}</pre>
            </div>
          </div>
        </div>
      </div>
      <div class="endpoint">
        <h3><code>POST /v1/complete/award</code></h3>
        <p class="small">상훈·수상 이력과 기관 단서를 함께 조회하여, 인물 또는 회사와 연결될 수 있는 수훈·수상 관련 evidence를 정리하여 반환합니다.</p>
        <div class="contract">
          <div class="endpoint-line"><span class="method">POST</span><code>/v1/complete/award</code></div>
          <div class="code-grid">
            <div>
              <div class="code-title">Request body</div>
              <pre>{
  "person": {"이름": "홍길동", "영문명": "Gildong Hong"},
  "company": {"회사명": "예시회사"},
  "limit": 5,
  "llm_confidence": false
}</pre>
            </div>
            <div>
              <div class="code-title">Response shape</div>
              <pre>{
  "schema_version": "noteapp_complete/v1",
  "purpose": "award",
  "completion_status": "completed",
  "subtasks": [{"name": "person_evidence_award", "categories": ["awards", "institutions"]}],
  "items": [
    {
      "award_name": "대통령 표창",
      "awarding_organization": "대한민국 상훈",
      "award_year": "2025",
      "description": "홍길동 예시회사 대표의 수상 내역",
      "url": "https://source.example/award",
      "identity_label": "A",
      "scores": {"workflow_score": 0.9, "identity_match_score": 0.88},
      "raw_payload_json": {"candidate": {}, "primary_record": {}, "scores": {}}
    }
  ]
}</pre>
            </div>
          </div>
        </div>
      </div>
      <div class="endpoint">
        <h3><code>POST /v1/complete/social</code></h3>
        <p class="small">LinkedIn 후보 검색과 YouTube 검색을 함께 수행하여, 공개 프로필·채널·영상 단서를 사회적 활동 관점에서 묶어 반환합니다.</p>
        <div class="contract">
          <div class="endpoint-line"><span class="method">POST</span><code>/v1/complete/social</code></div>
          <div class="code-grid">
            <div>
              <div class="code-title">Request body</div>
              <pre>{
  "person": {
    "이름": "홍길동",
    "영문명": "Gildong Hong",
    "직책": "대표",
    "부서": "전략",
    "이메일": "name at example dot com"
  },
  "company": {"회사명": "예시회사", "홈페이지": "https://example.com"},
  "limit": 5,
  "llm_confidence": false
}</pre>
            </div>
            <div>
              <div class="code-title">Response shape</div>
              <pre>{
  "schema_version": "noteapp_complete/v1",
  "purpose": "social",
  "response_mode": "synchronous",
  "completion_status": "completed",
  "subtasks": [
    {"name": "linkedin_profile_finder", "status": "completed"},
    {"name": "youtube_search", "status": "completed"}
  ],
  "items": [
    {
      "platform": "LinkedIn",
      "title": "홍길동 - 예시회사",
      "url": "https://www.linkedin.com/in/example",
      "identity_label": "A",
      "scores": {"workflow_score": 0.91, "identity_match_score": 0.9},
      "raw_payload_json": {"candidate": {}, "primary_record": {}, "scores": {}}
    }
  ]
}</pre>
            </div>
          </div>
        </div>
      </div>
      <div class="endpoint">
        <h3><code>POST /v1/complete/profile</code></h3>
        <p class="small">네이버 인물검색 결과 페이지를 Firecrawl로 수집하여 학력, 경력, 약력 등 프로필 필드를 추출합니다. 현재는 <code>firecrawl</code> crawler만 지원합니다.</p>
        <div class="contract">
          <div class="endpoint-line"><span class="method">POST</span><code>/v1/complete/profile</code></div>
          <div class="code-grid">
            <div>
              <div class="code-title">Request body</div>
              <pre>{
  "person": {"이름": "홍길동", "영문명": "Gildong Hong"},
  "company": {"회사명": "예시회사"},
  "crawler": "firecrawl"
}</pre>
            </div>
            <div>
              <div class="code-title">Response shape</div>
              <pre>{
  "schema_version": "noteapp_complete/v1",
  "purpose": "profile",
  "response_mode": "synchronous",
  "completion_status": "completed",
  "subtasks": [{"name": "naver_profile_crawl", "provider": "firecrawl"}],
  "items": [
    {
      "education": ["한성대학교"],
      "career_history": ["예시회사 대표"],
      "identity_label": "A",
      "scores": {"confidence": 0.7},
      "raw_payload_json": {"profile_crawl": {}, "scores": {}}
    }
  ],
}</pre>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section class="panel full">
      <h2>LinkedIn Profile Finder Workflow</h2>
      <span class="pill">serper</span><span class="pill">apify</span><span class="pill">ranked candidates</span>
      <p><code>POST /api/find/linkedin</code></p>
      <div class="contract">
        <div class="endpoint-line"><span class="method">POST</span><code>/api/find/linkedin</code></div>
        <div class="code-grid">
          <div>
            <div class="code-title">Request body</div>
            <pre>{
  "query": "홍길동 예시회사",
  "person": {"이름": "홍길동", "영문명": "Gildong Hong"},
  "company": {"회사명": "예시회사"},
  "providers": ["serper"],
  "max_serp_queries": 4,
  "max_apify_queries": 6,
  "llm_confidence": false
}</pre>
          </div>
          <div>
            <div class="code-title">Response shape</div>
            <pre>{
  "schema_version": "linkedin_profile_finder/v1",
  "query": "홍길동 예시회사",
  "providers": ["serper"],
  "best_match": {"url": "https://www.linkedin.com/in/example", "workflow_score": 0.91, "identity_label": "A"},
  "candidates": [{"url": "...", "identity_label": "A", "identity_match": {}, "records": []}],
  "operations": [],
  "endpoint": "/api/find/linkedin"
}</pre>
          </div>
        </div>
      </div>
    </section>

    <section class="panel full">
      <h2>Scrap API</h2>
      <span class="pill">Kyobo</span><span class="pill">Scholar/eArticle/KCI/RISS/DBpia/KoreaScience</span><span class="pill">대한민국 상훈</span><span class="pill">훈장/포장/표창</span><span class="pill">기관/커리어</span>
      <p><code>POST /api/scrap/person-evidence</code></p>
      <p><code>POST /api/scrap/books</code> · <code>POST /api/scrap/publications</code> · <code>POST /api/scrap/honors</code> · <code>POST /api/scrap/awards</code> · <code>POST /api/scrap/institutions</code> · <code>POST /api/scrap/career</code></p>
      <p class="small">이 API는 일반 검색 API가 아닙니다. 고정 출처 검색 URL은 직접 crawler로 수집하고 Serper는 보완 탐색에만 사용합니다. identity matching으로 후보를 정렬하며, 필요하실 경우 Firecrawl로 상위 후보 페이지를 추가 수집할 수 있습니다. <code>/api/find/*</code> 하위의 레거시 별칭도 계속 지원합니다.</p>
      <div class="contract">
        <div class="endpoint-line"><span class="method">POST</span><code>/api/scrap/person-evidence</code></div>
        <div class="field-list">
          <code>categories: books | publications | awards | institutions | career</code>
          <code>providers: serper</code>
          <code>direct_crawler: auto | stealth_browser | playwright_stealth | watercrawl</code>
          <code>crawler: firecrawl | playwright | static</code>
        </div>
        <div class="code-grid">
          <div>
            <div class="code-title">Request body</div>
            <pre>{
  "query": "홍길동 예시회사",
  "person": {"이름": "홍길동", "영문명": "Gildong Hong"},
  "company": {"회사명": "예시회사"},
  "providers": ["serper"],
  "categories": ["books", "publications", "awards"],
  "direct_crawler": "auto",
  "crawl_top": true,
  "crawler": "firecrawl",
  "crawl_limit": 3,
  "llm_confidence": false
}</pre>
          </div>
          <div>
            <div class="code-title">Response shape</div>
            <pre>{
  "schema_version": "person_evidence_finder/v1",
  "query": "홍길동 예시회사",
  "categories": ["books", "publications", "awards"],
  "best_by_category": {"books": {}, "publications": {}, "awards": {}},
  "candidates": [{"workflow_score": 0.82, "identity_match": {}, "primary_record": {}}],
  "identity_match": {},
  "llm_confidence": {},
  "operations": []
}</pre>
          </div>
        </div>
      </div>
    </section>

    <section class="panel">
      <h2>Search</h2>
      <div class="endpoint">
        <h3>Naver bundle</h3>
        <span class="pill">web</span><span class="pill">news</span><span class="pill">book</span>
        <p><code>POST /api/search/naver</code></p>
        <pre>{
  "type": "web",
  "query": "삼성전자 IR",
  "limit": 3,
  "intent": "company_profile",
  "target": "삼성전자 IR 공식 정보"
}</pre>
      </div>
      <div class="endpoint">
        <h3>Google SERP bundle</h3>
        <span class="pill">serper</span>
        <p><code>POST /api/search/google</code></p>
        <pre>{
  "provider": "serper",
  "query": "Samsung Electronics IR",
  "limit": 3
}</pre>
      </div>
      <div class="endpoint">
        <h3>YouTube</h3>
        <span class="pill">youtube</span>
        <p><code>POST /api/search/youtube</code></p>
        <pre>{
  "query": "OpenAI keynote",
  "limit": 3
}</pre>
      </div>
      <div class="endpoint">
        <h3>WaterCrawl Search</h3>
        <span class="pill">watercrawl</span><span class="pill">async API</span>
        <p><code>POST /api/search/watercrawl</code></p>
        <pre>{
  "query": "Samsung Electronics IR",
  "limit": 3
}</pre>
      </div>
    </section>

    <section class="panel">
      <h2>Crawl</h2>
      <div class="endpoint">
        <h3>Generic Crawl <span class="pill warn">response updated 2026-07-28</span></h3>
        <p><code>POST /api/crawl</code></p>
        <p class="small">요청 body와 endpoint는 유지됩니다. 변경 전/후 응답은 다음과 같습니다.</p>
        <div class="contract">
          <div>
            <div class="code-title">Request body (변경 없음)</div>
            <pre>{
  "url": "https://example.com/article",
  "crawler": "auto",
  "respect_robots": true
}</pre>
          </div>
          <div class="code-grid">
            <div>
              <div class="code-title">변경 전 response</div>
              <pre>{
  "crawler": "auto",
  "elapsed_seconds": 5.012,
  "result": {
    "url": "https://example.com/article",
    "crawler": "stealth_browser",
    "title": "Example",
    "text": "...",
    "status_code": 200,
    "screenshot_path": null,
    "error": null,
    "text_length": 3,
    "text_preview": "..."
  }
}</pre>
            </div>
            <div>
              <div class="code-title">변경 후 response (2026-07-28)</div>
              <pre>{
  "crawler": "auto",
  "response_mode": "api",
  "elapsed_seconds": 2.107,
  "result": {
    "url": "https://example.com/article",
    "crawler": "stealth_browser",
    "title": "Example",
    "text": "...",
    "status_code": 200,
    "error": null,
    "thumb_urls": [
      "https://cdn.example.com/hero.jpg"
    ],
    "primary_thumb_url":
      "https://cdn.example.com/hero.jpg",
    "text_length": 3,
    "text_preview": "..."
  }
}</pre>
            </div>
          </div>
          <div>
            <div class="code-title">변경 요약</div>
            <p class="small"><b>변경 전:</b> 이미지 URL 필드와 최상위 <code>response_mode</code>가 없었고, 서버 로컬 경로인 <code>result.screenshot_path</code>가 포함됐습니다.</p>
            <p class="small"><b>변경 후:</b> 원격 썸네일 URL 배열인 <code>thumb_urls</code>, 대표 URL인 <code>primary_thumb_url</code>, <code>response_mode=api</code>가 추가되고 <code>screenshot_path</code>는 제거됐습니다.</p>
            <p class="small">이미지 바이너리, multipart, base64는 반환하지 않습니다. 대표 이미지는 Vision Encoder 없이 메타데이터, DOM 위치, 표시 크기, URL 패턴으로 선택합니다.</p>
          </div>
        </div>
      </div>
      <div class="endpoint">
        <h3>Playwright</h3>
        <p><code>POST /api/crawl/playwright</code></p>
        <pre>{
  "url": "http://127.0.0.1:9101/",
  "respect_robots": true,
  "llm_confidence": false
}</pre>
      </div>
      <div class="endpoint">
        <h3>Playwright Stealth</h3>
        <p><code>POST /api/crawl/playwright-stealth</code></p>
        <pre>{
  "url": "http://127.0.0.1:9101/",
  "respect_robots": true,
  "llm_confidence": false
}</pre>
      </div>
      <div class="endpoint">
        <h3>Firecrawl</h3>
        <p><code>POST /api/crawl/firecrawl</code></p>
        <pre>{
  "url": "http://127.0.0.1:9101/",
  "llm_confidence": false
}</pre>
      </div>
      <div class="endpoint">
        <h3>WaterCrawl</h3>
        <p><code>POST /api/crawl/watercrawl</code></p>
        <pre>{
  "url": "http://127.0.0.1:9101/",
  "llm_confidence": false
}</pre>
      </div>
      <div class="endpoint">
        <h3>Stealth Browser</h3>
        <p><code>POST /api/crawl/stealth-browser</code></p>
        <pre>{
  "url": "http://127.0.0.1:9101/",
  "respect_robots": true,
  "llm_confidence": false
}</pre>
      </div>
      <div class="endpoint">
        <h3>Playwright then Firecrawl fallback</h3>
        <p><code>POST /api/crawl/playwright-firecrawl</code></p>
        <pre>{
  "url": "http://127.0.0.1:9101/",
  "respect_robots": true,
  "base_delay_seconds": 2,
  "government_delay_seconds": 10
}</pre>
      </div>
    </section>

    <section class="panel full">
      <h2>LinkedIn via Apify</h2>
      <span class="pill good">endpoint open</span><span class="pill good">profile/company actors configured</span>
      <p><code>POST /api/crawl/linkedin</code></p>
      <p class="small"><code>/in/</code>은 개인 프로필 Actor, <code>/company/</code>는 회사 전용 Actor로 분리합니다. 공개 LinkedIn 프로필·회사·학교·쇼케이스 URL만 명시적으로 받습니다.</p>
      <div class="contract">
        <div class="endpoint-line"><span class="method">POST</span><code>/api/crawl/linkedin</code></div>
        <div class="code-grid">
          <div>
            <div class="code-title">Request body</div>
            <pre>{
  "url": "https://www.linkedin.com/company/openai/",
  "llm_confidence": false
}</pre>
          </div>
          <div>
            <div class="code-title">Response shape</div>
            <pre>{
  "crawler": "apify_linkedin",
  "endpoint": "/api/crawl/linkedin",
  "result": {
    "url": "https://www.linkedin.com/company/openai/",
    "text_preview": "...",
    "scores": {}
  }
}</pre>
          </div>
        </div>
      </div>
    </section>
  </main>
  <script>
    function pretty(value) {
      return JSON.stringify(value, null, 2);
    }

    const EVAL_FIELDS = [
      {scope: 'person', key: '이름', label: '이름'},
      {scope: 'person', key: '영문명', label: '영문명'},
      {scope: 'person', key: '직책', label: '직책'},
      {scope: 'person', key: '부서', label: '부서'},
      {scope: 'person', key: '이메일', label: '이메일'},
      {scope: 'person', key: '전화번호', label: '전화번호', searchDefault: false},
      {scope: 'company', key: '회사명', label: '회사명'},
      {scope: 'company', key: '사업자번호', label: '사업자번호'},
      {scope: 'company', key: '법인번호', label: '법인번호'},
      {scope: 'company', key: '대표자', label: '대표자'},
      {scope: 'company', key: '설립일', label: '설립일', searchDefault: false},
      {scope: 'company', key: '상장여부', label: '상장여부', searchDefault: false},
      {scope: 'company', key: '업종', label: '업종'},
      {scope: 'company', key: '특허/인증', label: '특허/인증', list: true},
      {scope: 'company', key: '주소', label: '주소'},
      {scope: 'company', key: '주요제품/서비스', label: '주요제품/서비스', list: true},
      {scope: 'company', key: '홈페이지', label: '홈페이지'}
    ];

    const ENDPOINTS = [
      {group: 'Search', title: 'Generic Search', path: '/api/search', family: 'search', provider: true, defaultProvider: 'serper'},
      {group: 'Search', title: 'Naver Bundle', path: '/api/search/naver', family: 'search', vertical: true},
      {group: 'Search', title: 'Naver Web', path: '/api/search/naver/web', family: 'search'},
      {group: 'Search', title: 'Naver News', path: '/api/search/naver/news', family: 'search'},
      {group: 'Search', title: 'Google SERP Bundle', path: '/api/search/google', family: 'search', provider: true, defaultProvider: 'serper'},
      {group: 'Search', title: 'Serper', path: '/api/search/google/serper', family: 'search'},
      {group: 'Search', title: 'YouTube', path: '/api/search/youtube', family: 'search'},
      {group: 'Search', title: 'WaterCrawl Search', path: '/api/search/watercrawl', family: 'search'},
      {group: 'NoteApp', title: 'News Complete', path: '/v1/complete/news', family: 'noteapp'},
      {group: 'PoC', title: 'ArticleCluster', path: '/articluster', family: 'article-cluster'},
      {group: 'NoteApp', title: 'Publication Complete', path: '/v1/complete/publication', family: 'noteapp'},
      {group: 'NoteApp', title: 'Award Complete', path: '/v1/complete/award', family: 'noteapp'},
      {group: 'NoteApp', title: 'Social Complete', path: '/v1/complete/social', family: 'noteapp'},
      {group: 'NoteApp', title: 'Profile Complete', path: '/v1/complete/profile', family: 'noteapp'},
      {group: 'NoteApp', title: 'Company Homepage Complete', path: '/v1/complete/company-homepage', family: 'noteapp'},
      {group: 'Scrap', title: 'Person Evidence', path: '/api/scrap/person-evidence', family: 'scrap'},
      {group: 'Scrap', title: 'Books', path: '/api/scrap/books', family: 'scrap'},
      {group: 'Scrap', title: 'Publications', path: '/api/scrap/publications', family: 'scrap'},
      {group: 'Scrap', title: 'Honors', path: '/api/scrap/honors', family: 'scrap'},
      {group: 'Scrap', title: 'Awards + Institutions', path: '/api/scrap/awards', family: 'scrap'},
      {group: 'Scrap', title: 'Institutions', path: '/api/scrap/institutions', family: 'scrap'},
      {group: 'Scrap', title: 'Career', path: '/api/scrap/career', family: 'scrap'},
      {group: 'Workflow', title: 'LinkedIn Finder', path: '/api/find/linkedin', family: 'linkedin'},
      {group: 'Crawl', title: 'Generic Crawl', path: '/api/crawl', family: 'crawl', crawler: true},
      {group: 'Crawl', title: 'Playwright', path: '/api/crawl/playwright', family: 'crawl'},
      {group: 'Crawl', title: 'Playwright Stealth', path: '/api/crawl/playwright-stealth', family: 'crawl'},
      {group: 'Crawl', title: 'Firecrawl', path: '/api/crawl/firecrawl', family: 'crawl'},
      {group: 'Crawl', title: 'WaterCrawl', path: '/api/crawl/watercrawl', family: 'crawl'},
      {group: 'Crawl', title: 'Stealth Browser', path: '/api/crawl/stealth-browser', family: 'crawl'},
      {group: 'Crawl', title: 'Playwright -> Firecrawl', path: '/api/crawl/playwright-firecrawl', family: 'crawl'},
      {group: 'Crawl', title: 'Apify LinkedIn URL Crawl', path: '/api/crawl/linkedin', family: 'crawl'}
    ];

    function esc(value) {
      return String(value ?? '').replace(/[&<>"']/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }

    function splitList(text) {
      return String(text || '')
        .split(/[,\\n]/)
        .map((item) => item.trim())
        .filter(Boolean);
    }

    function addIf(payload, key, value) {
      if (value === null || value === undefined || value === '' || (Array.isArray(value) && !value.length)) return;
      payload[key] = value;
    }

    function numberFrom(card, selector) {
      const raw = card.querySelector(selector)?.value?.trim() || '';
      if (!raw) return null;
      const parsed = Number(raw);
      return Number.isFinite(parsed) ? parsed : null;
    }

    function optionHtml(config) {
      if (config.family === 'crawl') {
        return `
          <div class="option-grid">
            ${config.crawler ? `<label>Crawler<select class="opt-crawler"><option value="auto">auto</option><option value="static">static</option><option value="playwright">playwright</option><option value="selenium">selenium</option><option value="firecrawl">firecrawl</option><option value="watercrawl">watercrawl</option><option value="stealth_browser">stealth_browser</option><option value="apify_linkedin">apify_linkedin</option></select></label>` : ''}
            <label>Browser<select class="opt-browser"><option value="">default</option><option value="chromium">chromium</option><option value="firefox">firefox</option><option value="webkit">webkit</option></select></label>
            <label>Base delay<input class="opt-base-delay" type="number" min="0" max="60" step="0.1" placeholder="2"></label>
            <label>Gov delay<input class="opt-gov-delay" type="number" min="0" max="120" step="0.1" placeholder="10"></label>
            <label class="checkbox-row"><input class="opt-respect-robots" type="checkbox" checked> Respect robots</label>
            <label class="checkbox-row"><input class="opt-llm" type="checkbox"> GPT confidence</label>
          </div>`;
      }
      if (config.family === 'scrap') {
        return `
          <div class="option-grid">
            <label>SERP providers<input class="opt-providers" placeholder="serper"></label>
            <label>Limit<input class="opt-limit" type="number" min="1" max="10" placeholder="3"></label>
            <label>Max queries/source<input class="opt-max-queries" type="number" min="1" max="8" placeholder="2"></label>
            <label>Crawler<select class="opt-crawler"><option value="firecrawl">firecrawl</option><option value="watercrawl">watercrawl</option><option value="playwright">playwright</option><option value="stealth_browser">stealth_browser</option><option value="static">static</option></select></label>
            <label>Crawl top count<input class="opt-crawl-limit" type="number" min="1" max="10" placeholder="3"></label>
            <label class="checkbox-row"><input class="opt-crawl-top" type="checkbox"> Crawl top</label>
            <label class="checkbox-row"><input class="opt-llm" type="checkbox"> GPT confidence</label>
          </div>`;
      }
      if (config.family === 'linkedin') {
        return `
          <div class="option-grid">
            <label>SERP providers<input class="opt-providers" placeholder="serper"></label>
            <label>Limit<input class="opt-limit" type="number" min="1" max="10" placeholder="5"></label>
            <label>Max SERP queries<input class="opt-max-serp" type="number" min="0" max="12" placeholder="4"></label>
            <label>Max Apify queries<input class="opt-max-apify" type="number" min="1" max="12" placeholder="6"></label>
            <label class="checkbox-row"><input class="opt-strict-company" type="checkbox"> Strict company filter</label>
            <label class="checkbox-row"><input class="opt-llm" type="checkbox"> GPT confidence</label>
          </div>`;
      }
      if (config.family === 'noteapp') {
        return `
          <div class="option-grid">
            <label>Limit<input class="opt-limit" type="number" min="1" max="10" placeholder="5"></label>
            <label>Direct URL<input class="opt-url" placeholder="https://example.com/article"></label>
            <label>Crawler<select class="opt-crawler"><option value="">default</option><option value="stealth_browser">stealth_browser</option><option value="playwright_stealth">playwright_stealth</option><option value="watercrawl">watercrawl</option><option value="firecrawl">firecrawl</option><option value="static">static</option></select></label>
            <label>Fallback crawler<select class="opt-fallback-crawler"><option value="">none</option><option value="playwright_stealth">playwright_stealth</option><option value="stealth_browser">stealth_browser</option><option value="watercrawl">watercrawl</option><option value="firecrawl">firecrawl</option><option value="static">static</option></select></label>
            <label>Crawl top count<input class="opt-crawl-limit" type="number" min="1" max="10" placeholder="3"></label>
            <label class="checkbox-row"><input class="opt-crawl-top" type="checkbox"> Crawl top</label>
            <label class="checkbox-row"><input class="opt-llm" type="checkbox"> GPT confidence</label>
          </div>`;
      }
      return `
        <div class="option-grid">
          ${config.provider ? `<label>Provider<select class="opt-provider"><option value="">endpoint default</option><option value="serper">serper</option><option value="watercrawl_search">watercrawl_search</option><option value="naver_web">naver_web</option><option value="naver_news">naver_news</option><option value="youtube">youtube</option><option value="apify_linkedin_profile_search">apify_linkedin_profile_search</option></select></label>` : ''}
          ${config.vertical ? `<label>Naver type<select class="opt-vertical"><option value="">endpoint default</option><option value="web">web</option><option value="news">news</option><option value="book">book</option></select></label>` : ''}
          <label>Limit<input class="opt-limit" type="number" min="1" max="10" placeholder="3"></label>
          <label>Start<input class="opt-start" type="number" min="1" max="1000" placeholder="1"></label>
          <label>Intent<input class="opt-intent" placeholder="company_profile / person_lookup"></label>
          <label>Target<input class="opt-target" placeholder="평가 기준 설명"></label>
          <label class="checkbox-row"><input class="opt-llm" type="checkbox"> GPT confidence</label>
        </div>`;
    }

    function groupList() {
      return ['All', ...Array.from(new Set(ENDPOINTS.map((item) => item.group)))];
    }

    let activeGroup = 'All';
    let selectedEndpointIndex = 0;

    function endpointChoices() {
      return ENDPOINTS
        .map((config, index) => ({config, index}))
        .filter((item) => activeGroup === 'All' || item.config.group === activeGroup);
    }

    function searchFieldHtml(config) {
      if (config.family === 'crawl') return '';
      return EVAL_FIELDS.map((field, index) => {
        const checked = field.searchDefault !== false ? 'checked' : '';
        return `
          <label class="search-field-toggle">
            <input type="checkbox" data-search-index="${index}" ${checked}>
            <span>${esc(field.label)}</span>
          </label>`;
      }).join('');
    }

    function evaluationFieldsHtml() {
      return EVAL_FIELDS.map((field, index) => `
        <label>${esc(field.label)}
          <input data-eval-index="${index}" placeholder="${field.list ? '쉼표/줄바꿈으로 구분' : ''}">
        </label>`).join('');
    }

    function renderEndpointTabs() {
      const tabs = document.getElementById('endpointTabs');
      tabs.innerHTML = groupList().map((group) => `
        <button class="endpoint-tab ${group === activeGroup ? 'active' : ''}" type="button" data-group="${esc(group)}">${esc(group)}</button>
      `).join('');
      for (const tab of tabs.querySelectorAll('.endpoint-tab')) {
        tab.addEventListener('click', () => {
          activeGroup = tab.dataset.group;
          const visible = endpointChoices();
          if (!visible.some((item) => item.index === selectedEndpointIndex)) {
            selectedEndpointIndex = visible[0]?.index ?? 0;
          }
          renderEndpointTabs();
          renderEndpointList();
          renderEndpointTester();
        });
      }
    }

    function renderEndpointList() {
      const list = document.getElementById('endpointList');
      list.innerHTML = endpointChoices().map(({config, index}) => `
        <button class="endpoint-choice ${index === selectedEndpointIndex ? 'active' : ''}" type="button" data-endpoint-index="${index}">
          <span class="endpoint-choice-title">${esc(config.title)}</span>
          <span class="endpoint-choice-meta">
            <span class="endpoint-choice-path">POST ${esc(config.path)}</span>
            <span>${esc(config.group)}</span>
          </span>
        </button>
      `).join('');
      for (const button of list.querySelectorAll('.endpoint-choice')) {
        button.addEventListener('click', () => {
          selectedEndpointIndex = Number(button.dataset.endpointIndex);
          renderEndpointList();
          renderEndpointTester();
        });
      }
    }

    function renderEndpointTester() {
      const panel = document.getElementById('endpointTester');
      const config = ENDPOINTS[selectedEndpointIndex];
      const isCrawl = config.family === 'crawl';
      panel.dataset.endpointIndex = String(selectedEndpointIndex);
      panel.innerHTML = `
        <div class="tester-head">
          <div>
            <div class="tester-title">${esc(config.title)}</div>
            <div class="tester-path">POST ${esc(config.path)}</div>
          </div>
          <span class="pill">${esc(config.group)}</span>
        </div>
        <div class="keyword-layout">
          <div class="field-section">
            <div class="field-title">${isCrawl ? 'Target URL' : 'Search keywords'}</div>
            ${isCrawl
              ? `<input class="target-url" placeholder="https://example.com/">`
              : `<div class="search-field-list">${searchFieldHtml(config)}</div>
                 <div class="search-query-preview"></div>`}
          </div>
          <div class="field-section">
            <div class="field-title">Evaluation keywords</div>
            <div class="eval-grid">${evaluationFieldsHtml()}</div>
          </div>
        </div>
        <div class="field-section">
          <div class="field-title">Endpoint options</div>
          ${optionHtml(config)}
        </div>
        <details class="tester" open>
          <summary>Generated JSON</summary>
          <div class="test-body">
            <textarea class="payload-json">{}</textarea>
          </div>
        </details>
        <div class="tester-actions">
          <button class="build-json" type="button">Rebuild JSON</button>
          <button class="run-endpoint" type="button">Run API</button>
        </div>
        <pre class="result-pre endpoint-result">{}</pre>`;
      wireEndpointTester(panel);
    }

    function valueFromInput(input, field) {
      const raw = input?.value?.trim() || '';
      if (!raw) return null;
      return field.list ? splitList(raw) : raw;
    }

    function collectEvaluation(panel) {
      const person = {};
      const company = {};
      for (const input of panel.querySelectorAll('[data-eval-index]')) {
        const field = EVAL_FIELDS[Number(input.dataset.evalIndex)];
        const value = valueFromInput(input, field);
        if (!value || (Array.isArray(value) && !value.length)) continue;
        if (field.scope === 'person') person[field.key] = value;
        if (field.scope === 'company') company[field.key] = value;
      }
      return {person, company};
    }

    function buildSearchQueryFromFields(panel) {
      const terms = [];
      for (const checkbox of panel.querySelectorAll('[data-search-index]:checked')) {
        const index = Number(checkbox.dataset.searchIndex);
        const field = EVAL_FIELDS[index];
        const input = panel.querySelector(`[data-eval-index="${index}"]`);
        const value = valueFromInput(input, field);
        if (Array.isArray(value)) terms.push(...value);
        else if (value) terms.push(value);
      }
      return Array.from(new Set(terms))
        .join(' ')
        .replace(/\s+/g, ' ')
        .trim();
    }

    function buildPayloadFromTester(panel) {
      const config = ENDPOINTS[Number(panel.dataset.endpointIndex)];
      const payload = {};
      if (config.family === 'crawl') {
        addIf(payload, 'url', panel.querySelector('.target-url')?.value?.trim() || '');
      } else {
        addIf(payload, 'query', buildSearchQueryFromFields(panel));
      }

      const {person, company} = collectEvaluation(panel);
      if (Object.keys(person).length) payload.person = person;
      if (Object.keys(company).length) payload.company = company;

      addIf(payload, 'provider', panel.querySelector('.opt-provider')?.value || config.defaultProvider || '');
      addIf(payload, 'type', panel.querySelector('.opt-vertical')?.value || '');
      addIf(payload, 'vertical', panel.querySelector('.opt-vertical')?.value || '');
      addIf(payload, 'limit', numberFrom(panel, '.opt-limit'));
      addIf(payload, 'start', numberFrom(panel, '.opt-start'));
      addIf(payload, 'intent', panel.querySelector('.opt-intent')?.value?.trim() || '');
      addIf(payload, 'target', panel.querySelector('.opt-target')?.value?.trim() || '');
      addIf(payload, 'providers', splitList(panel.querySelector('.opt-providers')?.value || ''));
      addIf(payload, 'max_queries_per_source', numberFrom(panel, '.opt-max-queries'));
      addIf(payload, 'max_serp_queries', numberFrom(panel, '.opt-max-serp'));
      addIf(payload, 'max_apify_queries', numberFrom(panel, '.opt-max-apify'));
      addIf(payload, 'url', panel.querySelector('.opt-url')?.value?.trim() || '');
      addIf(payload, 'crawl_limit', numberFrom(panel, '.opt-crawl-limit'));
      addIf(payload, 'crawler', panel.querySelector('.opt-crawler')?.value || (config.family === 'crawl' && config.crawler ? 'auto' : ''));
      addIf(payload, 'fallback_crawler', panel.querySelector('.opt-fallback-crawler')?.value || '');
      addIf(payload, 'browser', panel.querySelector('.opt-browser')?.value || '');
      addIf(payload, 'base_delay_seconds', numberFrom(panel, '.opt-base-delay'));
      addIf(payload, 'government_delay_seconds', numberFrom(panel, '.opt-gov-delay'));
      if (panel.querySelector('.opt-crawl-top')?.checked) payload.crawl_top = true;
      if (panel.querySelector('.opt-strict-company')?.checked) payload.strict_company_filter = true;
      payload.llm_confidence = Boolean(panel.querySelector('.opt-llm')?.checked);
      const respectRobots = panel.querySelector('.opt-respect-robots');
      if (respectRobots) payload.respect_robots = respectRobots.checked;
      if (config.family === 'scrap' && !payload.providers) payload.providers = ['serper'];
      if (config.family === 'linkedin' && !payload.providers) payload.providers = ['serper'];
      return payload;
    }

    function updateSearchPreview(panel) {
      const preview = panel.querySelector('.search-query-preview');
      if (!preview) return;
      preview.textContent = buildSearchQueryFromFields(panel) || '검색에 포함하실 항목을 선택하고 값을 입력하시면, 생성된 query가 이곳에 표시됩니다.';
    }

    function rebuildTesterJson(panel) {
      updateSearchPreview(panel);
      panel.querySelector('.payload-json').value = pretty(buildPayloadFromTester(panel));
    }

    async function runEndpointTester(panel) {
      const config = ENDPOINTS[Number(panel.dataset.endpointIndex)];
      let payload;
      try {
        payload = JSON.parse(panel.querySelector('.payload-json').value || '{}');
      } catch (err) {
        panel.querySelector('.endpoint-result').textContent = pretty({ok: false, error: 'Invalid JSON body', detail: String(err)});
        return;
      }
      const button = panel.querySelector('.run-endpoint');
      button.disabled = true;
      panel.querySelector('.endpoint-result').textContent = pretty({status: 'running', endpoint: config.path});
      try {
        const response = await fetch(config.path, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload)
        });
        const text = await response.text();
        let data;
        try {
          data = JSON.parse(text);
        } catch (_err) {
          data = {status: response.status, raw: text};
        }
        panel.querySelector('.endpoint-result').textContent = pretty({status: response.status, ok: response.ok, data});
      } catch (err) {
        panel.querySelector('.endpoint-result').textContent = pretty({ok: false, error: String(err)});
      } finally {
        button.disabled = false;
      }
    }

    function wireEndpointTester(panel) {
      for (const node of panel.querySelectorAll('input, textarea, select')) {
        node.addEventListener('input', () => rebuildTesterJson(panel));
        node.addEventListener('change', () => rebuildTesterJson(panel));
      }
      panel.querySelector('.build-json').addEventListener('click', () => rebuildTesterJson(panel));
      panel.querySelector('.run-endpoint').addEventListener('click', () => runEndpointTester(panel));
      rebuildTesterJson(panel);
    }

    renderEndpointTabs();
    renderEndpointList();
    renderEndpointTester();
  </script>
</body>
</html>
"""


OBSERVABILITY_HTML = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Web Search Agent Observability</title>
  <script src="https://cdn.jsdelivr.net/npm/echarts@6.1.0/dist/echarts.min.js"></script>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --text: #18212f;
      --muted: #657184;
      --line: #d9dee8;
      --accent: #0d766e;
      --danger: #b42318;
      --warn: #ad5b00;
      --ok: #157347;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); }
    header { border-bottom: 1px solid var(--line); background: #fff; }
    .wrap { max-width: 1240px; margin: 0 auto; padding: 20px; }
    .top { display: flex; justify-content: space-between; gap: 16px; align-items: center; }
    .top-actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
    h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: 0; }
    h2 { margin: 0 0 12px; font-size: 16px; letter-spacing: 0; overflow-wrap: anywhere; }
    p { margin: 0; color: var(--muted); }
    .nav-button, button {
      border: 1px solid var(--line); border-radius: 6px; background: #fff; color: #18212f;
      padding: 9px 11px; text-decoration: none; font: inherit; font-size: 13px; font-weight: 700; cursor: pointer;
    }
    button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
    .nav-button:hover, button:hover { border-color: var(--accent); color: var(--accent); }
    button.primary:hover { color: #fff; }
    select {
      border: 1px solid var(--line); border-radius: 6px; padding: 9px 10px; font: inherit; background: #fff; min-width: 160px;
    }
    .toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin: 18px 0; }
    .status { display: flex; gap: 8px; flex-wrap: wrap; }
    #domainSummary { margin-bottom: 12px; }
    .pill { border: 1px solid var(--line); border-radius: 999px; padding: 5px 9px; background: #fff; color: #334155; font-size: 12px; }
    .ok { color: var(--ok); border-color: #aad8bf; background: #effaf3; }
    .bad { color: var(--danger); border-color: #fac5c0; background: #fff2f0; }
    .warn { color: var(--warn); border-color: #ffd59a; background: #fff8eb; }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; align-items: start; }
    .panel { min-width: 0; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px; }
    .panel.full { grid-column: 1 / -1; }
    .chart { height: 340px; width: 100%; }
    .small { font-size: 12px; color: var(--muted); line-height: 1.45; }
    .table-wrap { overflow: auto; border: 1px solid var(--line); border-radius: 8px; }
    table { width: 100%; border-collapse: collapse; min-width: 880px; background: #fff; }
    th, td { padding: 10px 11px; border-bottom: 1px solid var(--line); text-align: left; font-size: 13px; vertical-align: top; }
    th { background: #eef2f7; color: #334155; font-weight: 750; }
    tr:last-child td { border-bottom: 0; }
    code { background: #eef2f7; border: 1px solid var(--line); border-radius: 4px; padding: 1px 4px; }
    .error { color: var(--danger); white-space: pre-wrap; }
    @media (max-width: 900px) {
      .grid { grid-template-columns: 1fr; }
      .top { align-items: flex-start; flex-direction: column; }
      .chart { height: 300px; }
    }
  </style>
</head>
<body>
  <header>
    <div class="wrap top">
      <div>
        <h1>Observability</h1>
        <p>Connector usage, errors, local budgets, and upstream rate-window signals.</p>
      </div>
      <div class="top-actions">
        <a class="nav-button" href="/">Dashboard</a>
        <a class="nav-button" href="/docs">Docs</a>
        <button class="primary" id="refreshButton" type="button">Refresh</button>
      </div>
    </div>
  </header>
  <main class="wrap">
    <div class="toolbar">
      <select id="days">
        <option value="7">Last 7 days</option>
        <option value="14">Last 14 days</option>
        <option value="30" selected>Last 30 days</option>
        <option value="90">Last 90 days</option>
      </select>
      <select id="provider">
        <option value="">All connectors</option>
      </select>
      <div id="summary" class="status"></div>
    </div>
    <section class="grid">
      <article class="panel full">
        <h2>Connector Calls</h2>
        <div id="callsChart" class="chart"></div>
      </article>
      <article class="panel">
        <h2>Remaining Budget / Shared Rate Window</h2>
        <div id="quotaChart" class="chart"></div>
        <p class="small">Daily budget is local and appears only when <code>WEB_SEARCH_DAILY_LIMIT_*</code> is configured. Naver <code>x-rate-limit-remaining</code> is a short upstream window, not today's remaining quota.</p>
      </article>
      <article class="panel">
        <h2>Errors</h2>
        <div id="errorChart" class="chart"></div>
      </article>
      <article class="panel full">
        <h2>Connector Table</h2>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>connector</th>
                <th>credential</th>
                <th>quota scope</th>
                <th>today</th>
                <th>total</th>
                <th>success</th>
                <th>errors</th>
                <th>last status</th>
                <th>last upstream rate window</th>
                <th>local daily budget</th>
              </tr>
            </thead>
            <tbody id="providerRows"></tbody>
          </table>
        </div>
      </article>
      <article class="panel full">
        <h2>Bot Detection</h2>
        <div id="botSummary" class="status"></div>
        <div class="table-wrap" style="margin-top:12px">
          <table>
            <thead>
              <tr>
                <th>time</th>
                <th>status</th>
                <th>host</th>
                <th>vendor</th>
                <th>severity</th>
                <th>crawler</th>
                <th>HTTP</th>
              </tr>
            </thead>
            <tbody id="botRows"></tbody>
          </table>
        </div>
        <p class="small">전체 URL과 검색어는 저장하지 않습니다.</p>
      </article>
      <article class="panel full">
        <h2>Recent Search Activity</h2>
        <div id="domainSummary" class="status"></div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>time</th>
                <th>provider</th>
                <th>query id</th>
                <th>intent</th>
                <th>target domains</th>
                <th>result domains</th>
                <th>results</th>
                <th>duration</th>
                <th>status</th>
              </tr>
            </thead>
            <tbody id="auditRows"></tbody>
          </table>
        </div>
        <p class="small">검색어와 전체 URL은 서버의 비공개 감사 로그에만 보관됩니다. 이 화면은 해시와 도메인 집계만 표시합니다.</p>
      </article>
    </section>
    <p id="error" class="error"></p>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    let callsChart;
    let quotaChart;
    let errorChart;
    let providerOptionsLoaded = false;
    let providerInventory = [];

    function esc(value) {
      return String(value ?? '').replace(/[&<>"']/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }

    function pill(text, kind='') {
      return `<span class="pill ${kind}">${esc(text)}</span>`;
    }

    function providerLabel(value) {
      const labels = {
        firecrawl: 'firecrawl scrape/crawl',
        apify_linkedin: 'apify LinkedIn scrape',
        apify_linkedin_profile_search: 'apify LinkedIn profile search',
        firecrawl_search: 'firecrawl search',
        watercrawl: 'watercrawl scrape/crawl',
        watercrawl_search: 'watercrawl search',
        naver_web: 'naver web',
        naver_news: 'naver news',
        naver_book: 'naver book',
        workflow_linkedin_profile_finder: 'workflow LinkedIn finder',
        workflow_person_evidence_finder: 'workflow Scrap API',
        crawler_auto: 'gateway crawl auto',
        crawler_static: 'gateway crawl static',
        crawler_playwright: 'gateway crawl playwright',
        crawler_selenium: 'gateway crawl selenium',
        crawler_firecrawl: 'gateway crawl firecrawl',
        crawler_watercrawl: 'gateway crawl watercrawl',
        crawler_stealth_browser: 'gateway crawl stealth-browser',
        crawler_playwright_firecrawl: 'gateway crawl playwright-firecrawl',
        crawler_apify_linkedin: 'gateway crawl apify LinkedIn',
        local_article_sentiment: 'local article sentiment classification',
        bot_detection: 'bot detection checks'
      };
      return labels[value] || value || 'unknown';
    }

    function quotaScopeLabel(value) {
      const labels = {
        firecrawl_account: 'firecrawl account credits',
        apify_account: 'apify account usage',
        naver_search_openapi: 'naver search openapi bucket',
        gateway_workflows: 'gateway workflows',
        gateway_crawl_tools: 'gateway crawl tool calls'
      };
      return labels[value] || value || 'unknown';
    }

    function numberOrNull(value) {
      if (value === null || value === undefined || value === '') return null;
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    }

    function chartBase(title) {
      return {
        animationDuration: 250,
        color: ['#0d766e', '#3366cc', '#ad5b00', '#7a5195', '#d45087', '#2f4b7c', '#157347', '#b42318'],
        tooltip: { trigger: 'axis' },
        legend: { type: 'scroll', top: 0 },
        grid: { top: 48, left: 48, right: 24, bottom: 44 },
        xAxis: { type: 'category', boundaryGap: false, data: [] },
        yAxis: { type: 'value', minInterval: 1 },
        series: []
      };
    }

    function ensureCharts() {
      if (!window.echarts) {
        $('error').textContent = 'Apache ECharts failed to load from CDN.';
        return false;
      }
      callsChart ||= echarts.init($('callsChart'));
      quotaChart ||= echarts.init($('quotaChart'));
      errorChart ||= echarts.init($('errorChart'));
      return true;
    }

    function visibleProviders(data) {
      const selected = $('provider').value;
      const names = Object.keys(data.series || {}).sort();
      return selected ? names.filter((name) => name === selected) : names;
    }

    function visibleQuotaScopes(data, providers) {
      const selectedProviders = new Set(providers);
      const scopes = data.quota_scopes || [];
      if (!$('provider').value) return scopes.map((row) => row.quota_scope).sort();
      return scopes
        .filter((row) => (row.providers || []).some((name) => selectedProviders.has(name)))
        .map((row) => row.quota_scope)
        .sort();
    }

    function populateProviderOptions(data) {
      const names = [...new Set([
        ...Object.keys(data.series || {}),
        ...providerInventory.map((row) => row.provider)
      ])].sort();
      const current = $('provider').value;
      $('provider').innerHTML = '<option value="">All connectors</option>' + names.map((name) => `<option value="${esc(name)}">${esc(providerLabel(name))}</option>`).join('');
      if (names.includes(current)) $('provider').value = current;
      providerOptionsLoaded = true;
    }

    function renderCharts(data) {
      if (!ensureCharts()) return;
      const providers = visibleProviders(data);
      const sample = providers[0] ? data.series[providers[0]] : data.overall_series || [];
      const dates = sample.map((point) => point.date);

      const calls = chartBase('calls');
      calls.xAxis.data = dates;
      calls.series = providers.map((name) => ({
        name: providerLabel(name),
        type: 'line',
        smooth: false,
        symbolSize: 6,
        data: (data.series[name] || []).map((point) => Number(point.calls || 0))
      }));
      callsChart.setOption(calls, true);

      const quota = chartBase('budget');
      quota.yAxis.minInterval = undefined;
      quota.xAxis.data = dates;
      const selected = $('provider').value;
      quota.series = [];
      const scopes = visibleQuotaScopes(data, providers);
      for (const name of scopes) {
        const points = (data.quota_series || {})[name] || [];
        const localRemaining = points.map((point) => numberOrNull(point.configured_daily_remaining));
        if (localRemaining.some((value) => value !== null)) {
          quota.series.push({ name: `${quotaScopeLabel(name)} local daily remaining`, type: 'line', smooth: false, symbolSize: 6, data: localRemaining });
        }
        const rateRemaining = points.map((point) => numberOrNull(point.quota_remaining));
        if (rateRemaining.some((value) => value !== null)) {
          quota.series.push({ name: `${quotaScopeLabel(name)} upstream rate-window remaining`, type: 'line', smooth: false, symbolSize: 6, data: rateRemaining });
        }
        if (selected) {
          const localLimits = points.map((point) => numberOrNull(point.configured_daily_limit));
          if (localLimits.some((value) => value !== null)) {
            quota.series.push({ name: `${quotaScopeLabel(name)} local daily limit`, type: 'line', smooth: false, symbolSize: 6, lineStyle: { type: 'dashed' }, data: localLimits });
          }
          const rateLimits = points.map((point) => numberOrNull(point.quota_limit));
          if (rateLimits.some((value) => value !== null)) {
            quota.series.push({ name: `${quotaScopeLabel(name)} upstream rate-window limit`, type: 'line', smooth: false, symbolSize: 6, lineStyle: { type: 'dashed' }, data: rateLimits });
          }
        }
      }
      if (!quota.series.length) {
        quota.series = [{ name: 'no budget or rate-window values', type: 'line', data: dates.map(() => null) }];
      }
      quotaChart.setOption(quota, true);

      const errors = chartBase('errors');
      errors.xAxis.data = dates;
      errors.series = providers.map((name) => ({
        name: providerLabel(name),
        type: 'line',
        smooth: false,
        symbolSize: 6,
        data: (data.series[name] || []).map((point) => Number(point.errors || 0))
      }));
      errorChart.setOption(errors, true);
    }

    function renderTable(data) {
      const scopeByName = Object.fromEntries((data.quota_scopes || []).map((row) => [row.quota_scope, row]));
      const inventoryByName = Object.fromEntries(providerInventory.map((row) => [row.provider, row]));
      const usageByName = Object.fromEntries((data.providers || []).map((row) => [row.provider, row]));
      const names = [...new Set([...Object.keys(usageByName), ...Object.keys(inventoryByName)])].sort();
      const rows = names
        .filter((name) => !$('provider').value || name === $('provider').value)
        .map((name) => usageByName[name] || {
          provider: name, quota_scope: name, today_units: 0, units: 0,
          success_count: 0, error_count: 0, last_status_code: null
        });
      $('providerRows').innerHTML = rows.map((row) => {
        const inventory = inventoryByName[row.provider] || {};
        const hasInventory = Object.hasOwn(inventoryByName, row.provider);
        const scope = scopeByName[row.quota_scope] || {};
        const quota = scope.last_quota || row.last_quota || {};
        const quotaText = quota.remaining != null
          ? `rate-window remaining ${quota.remaining}${quota.limit != null ? ' / ' + quota.limit : ''}${quota.reset_at ? '<br>reset ' + esc(quota.reset_at) : ''}`
          : 'none captured';
        const budget = scope.configured_daily_limit != null
          ? `${scope.configured_daily_remaining} / ${scope.configured_daily_limit}`
          : 'not configured';
        return `<tr>
          <td>${esc(providerLabel(row.provider))}<br><code>${esc(row.provider)}</code></td>
          <td>${hasInventory ? (inventory.ready ? pill('ready', 'ok') : pill('missing', 'warn')) : pill('n/a')}<br>${hasInventory ? esc((inventory.required_env || []).join(', ') || 'none') : 'not in current search registry'}</td>
          <td>${esc(quotaScopeLabel(row.quota_scope || row.provider))}<br><code>${esc(row.quota_scope || row.provider)}</code></td>
          <td>${esc(row.today_units)}</td>
          <td>${esc(row.units)}</td>
          <td>${esc(row.success_count)}</td>
          <td>${esc(row.error_count)}</td>
          <td>${esc(row.last_status_code ?? '')}</td>
          <td>${quotaText}</td>
          <td>${esc(budget)}</td>
        </tr>`;
      }).join('') || '<tr><td colspan="10">No providers found.</td></tr>';
    }

    function renderAudit(data) {
      $('domainSummary').innerHTML = (data.target_domains || []).slice(0, 12)
        .map((row) => pill(`${row.domain} ${row.count}`))
        .join('') || pill('no targeted domains');
      $('auditRows').innerHTML = (data.recent || []).map((row) => {
        const status = row.ok === true ? pill('ok', 'ok') : row.ok === false ? pill('error', 'bad') : pill('unknown', 'warn');
        return `<tr>
          <td>${esc(row.timestamp || '')}</td>
          <td><code>${esc(row.provider || '')}</code></td>
          <td><code>${esc(row.query_fingerprint || '')}</code></td>
          <td>${esc(row.query_intent || '')}</td>
          <td>${esc((row.target_domains || []).join(', '))}</td>
          <td>${esc((row.result_domains || []).join(', '))}</td>
          <td>${esc(row.result_count ?? '')}</td>
          <td>${row.duration_ms != null ? esc(row.duration_ms) + ' ms' : ''}</td>
          <td>${status}</td>
        </tr>`;
      }).join('') || '<tr><td colspan="9">No search audit events for this range.</td></tr>';
    }

    function renderBotDetections(data) {
      $('botSummary').innerHTML = [
        pill(`${data.check_count || 0} checks`),
        pill(`${data.problem_count || 0} problems`, data.problem_count ? 'bad' : 'ok')
      ].join('');
      $('botRows').innerHTML = (data.recent || []).map((row) => {
        const status = row.status === 'blocked' || row.status === 'http_block'
          ? pill(row.status, 'bad')
          : row.status === 'clear' ? pill('clear', 'ok') : pill(row.status || 'unknown', 'warn');
        return `<tr>
          <td>${esc(row.timestamp || '')}</td>
          <td>${status}</td>
          <td><code>${esc(row.host || '')}</code></td>
          <td>${esc(row.vendor || '')}</td>
          <td>${esc(row.severity || '')}</td>
          <td>${esc(row.crawler || '')}${row.actual_crawler && row.actual_crawler !== row.crawler ? ' → ' + esc(row.actual_crawler) : ''}</td>
          <td>${esc(row.status_code ?? '')}</td>
        </tr>`;
      }).join('') || '<tr><td colspan="7">No bot detection checks for this range.</td></tr>';
    }

    function renderSummary(data, audit, botDetections) {
      const providers = data.providers || [];
      const total = providers.reduce((sum, row) => sum + Number(row.today_units || 0), 0);
      const errors = providers.reduce((sum, row) => sum + Number(row.error_count || 0), 0);
      $('summary').innerHTML = [
        pill(`${data.days} days`),
        pill(`${providers.length} connectors`, providers.length ? 'ok' : 'warn'),
        pill(`today ${total} calls`),
        pill(`${errors} errors`, errors ? 'warn' : 'ok'),
        pill(`${botDetections.problem_count || 0} bot blocks`, botDetections.problem_count ? 'bad' : 'ok'),
        pill(audit.private_detail_logged ? 'private search audit on' : 'private search audit off', audit.private_detail_logged ? 'ok' : 'warn')
      ].join('');
    }

    async function loadUsage() {
      $('error').textContent = '';
      const days = Number($('days').value || 30);
      const provider = $('provider').value;
      const suffix = `?days=${encodeURIComponent(days)}${provider ? '&provider=' + encodeURIComponent(provider) : ''}`;
      const [usageResponse, auditResponse, providerResponse, botResponse] = await Promise.all([
        fetch(`/api/usage${suffix}`),
        fetch(`/api/search-audit${suffix}&limit=100`),
        fetch('/api/providers'),
        fetch(`/api/bot-detections?days=${encodeURIComponent(days)}&limit=100`)
      ]);
      const [data, audit, providers, botDetections] = await Promise.all([
        usageResponse.json(),
        auditResponse.json(),
        providerResponse.json(),
        botResponse.json()
      ]);
      if (!usageResponse.ok) throw new Error(data.error || `HTTP ${usageResponse.status}`);
      if (!auditResponse.ok) throw new Error(audit.error || `HTTP ${auditResponse.status}`);
      if (!providerResponse.ok) throw new Error(providers.error || `HTTP ${providerResponse.status}`);
      if (!botResponse.ok) throw new Error(botDetections.error || `HTTP ${botResponse.status}`);
      providerInventory = providers.search || [];
      if (!providerOptionsLoaded || !provider) populateProviderOptions(data);
      renderSummary(data, audit, botDetections);
      renderCharts(data);
      renderTable(data);
      renderBotDetections(botDetections);
      renderAudit(audit);
    }

    $('days').addEventListener('change', () => loadUsage().catch((err) => $('error').textContent = err.message));
    $('provider').addEventListener('change', () => loadUsage().catch((err) => $('error').textContent = err.message));
    $('refreshButton').addEventListener('click', () => loadUsage().catch((err) => $('error').textContent = err.message));
    window.addEventListener('resize', () => {
      callsChart?.resize();
      quotaChart?.resize();
      errorChart?.resize();
    });
    loadUsage().catch((err) => $('error').textContent = err.message);
  </script>
</body>
</html>
"""


def now() -> float:
    return time.monotonic()


def safe_provider_rows() -> list[dict[str, object]]:
    doctor = build_doctor_report()
    ready_by_provider = {
        item["provider"]: bool(item["ready"])
        for item in doctor.get("providers", [])
        if isinstance(item, dict)
    }
    rows = []
    for item in available_search_providers():
        rows.append(
            {
                "provider": item["provider"],
                "priority": item["priority"],
                "source_type": item["source_type"],
                "allowed_targets": item["allowed_targets"],
                "required_env": item["required_env"],
                "ready": ready_by_provider.get(str(item["provider"]), False),
                "policy_note": item["policy_note"],
                "quota_note": item["quota_note"],
            }
        )
    return rows


def safe_crawler_rows() -> list[dict[str, object]]:
    doctor = build_doctor_report()
    ready_by_crawler = {
        item["crawler"]: bool(item["ready"])
        for item in doctor.get("crawler_credentials", [])
        if isinstance(item, dict)
    }
    rows = []
    for item in available_crawlers():
        rows.append(
            {
                "crawler": item["crawler"],
                "use": item["use"],
                "engine_type": item["engine_type"],
                "required_env": item.get("required_env", []),
                "ready": ready_by_crawler.get(str(item["crawler"]), False),
                "policy_note": item["policy_note"],
            }
        )
    return rows


def normalized_token(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def search_provider_for_endpoint(group: str, payload: dict[str, object], explicit_choice: str = "") -> str:
    normalized_group = normalized_token(group)
    spec = SEARCH_ENDPOINT_GROUPS.get(normalized_group)
    if not spec:
        raise ValueError("unsupported search endpoint")
    provider_map = spec["providers"]
    if not isinstance(provider_map, dict):
        raise ValueError("invalid search endpoint configuration")
    requested = (
        explicit_choice
        or str(payload.get("provider") or "")
        or str(payload.get("type") or "")
        or str(payload.get("vertical") or "")
        or str(payload.get("engine") or "")
        or str(payload.get("source") or "")
    )
    choice = normalized_token(requested)
    if not choice:
        return str(spec["default"])
    provider = provider_map.get(choice)
    if not provider:
        allowed = ", ".join(sorted(provider_map))
        raise ValueError(f"unsupported {group} search provider/type; allowed: {allowed}")
    return str(provider)


def crawl_failed(result: object) -> bool:
    error = getattr(result, "error", None)
    status_code = getattr(result, "status_code", None)
    text = getattr(result, "text", "")
    if error:
        return True
    if isinstance(status_code, int) and status_code >= 400:
        return True
    return not bool(str(text or "").strip())


def crawl_result_payload(result: object) -> dict[str, object]:
    payload = result.to_dict()
    text = str(payload.get("text") or "")
    image_urls = payload.pop("image_urls", [])
    if not isinstance(image_urls, list):
        image_urls = []
    payload["thumb_urls"] = image_urls
    payload["primary_thumb_url"] = image_urls[0] if image_urls else None
    payload["text_length"] = len(text)
    payload["text_preview"] = text[:3000]
    payload.pop("html", None)
    payload.pop("screenshot_path", None)
    return payload


def attachment_file_name(result_payload: dict[str, object]) -> str:
    url = str(result_payload.get("url") or "")
    parsed = urlparse(url)
    path_name = unquote(parsed.path.rsplit("/", 1)[-1]).strip()
    title = str(result_payload.get("title") or "").strip()
    candidate = path_name if "." in path_name else (title or parsed.netloc or "attachment") + ".html"
    candidate = " ".join(candidate.replace("/", "_").replace("\\", "_").split())
    return candidate[:255] or "attachment.html"


def attachment_mime_type(result_payload: dict[str, object], file_name: str) -> str | None:
    raw_payload = result_payload.get("raw_payload")
    if isinstance(raw_payload, dict):
        content_type = str(raw_payload.get("content_type") or "").split(";", 1)[0].strip()
        if content_type:
            return content_type
    guessed, _ = mimetypes.guess_type(file_name)
    return guessed


def attachment_link_response(result: object) -> dict[str, object]:
    result_payload = crawl_result_payload(result)
    text = str(result_payload.get("text") or "")
    raw_payload = result_payload.get("raw_payload")
    auto_decision = raw_payload.get("auto_decision") if isinstance(raw_payload, dict) else None
    file_name = attachment_file_name(result_payload)
    return {
        "status": "success",
        "endpoint": ATTACHMENT_LINK_ENDPOINT,
        "requested_crawler": "auto",
        "resolved_crawler": result_payload.get("crawler"),
        "auto_decision": auto_decision,
        "url": result_payload.get("url"),
        "canonical_url": result_payload.get("canonical_url"),
        "title": result_payload.get("title") or result_payload.get("canonical_url") or "",
        "status_code": result_payload.get("status_code"),
        "retrieved_at": result_payload.get("retrieved_at"),
        "attachments": [
            {
                "file_name": file_name,
                "mime_type": attachment_mime_type(result_payload, file_name),
                "parsed_text": text[:20_000] or None,
            }
        ],
        "crawl": {
            "text_length": len(text),
            "text_preview": result_payload.get("text_preview") or "",
            "thumb_urls": result_payload.get("thumb_urls") or [],
            "primary_thumb_url": result_payload.get("primary_thumb_url"),
            "error": result_payload.get("error"),
        },
    }


def public_http_url(value: str) -> str:
    url = (value or "").strip()
    if len(url) > MAX_URL_LENGTH:
        raise ValueError("URL is too long")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Only public http/https URLs are accepted")
    return url


def clean_text(value: object, *, max_length: int) -> str:
    text = str(value or "").strip()
    if len(text) > max_length:
        raise ValueError("Input is too long")
    return text


def llm_confidence_enabled_for_payload(payload: dict[str, object]) -> bool:
    if "llm_confidence" in payload:
        return clean_bool(payload.get("llm_confidence"), default=True)
    if "include_llm_confidence" in payload:
        return clean_bool(payload.get("include_llm_confidence"), default=True)
    return True


def comparison_target_from_payload(payload: dict[str, object], default: str) -> str:
    for key in ("comparison_target", "target", "reference", "subject", "entity", "original_query"):
        text = str(payload.get(key) or "").strip()
        if text:
            return text[:MAX_QUERY_LENGTH]
    return default[:MAX_QUERY_LENGTH]


def payload_mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def normalize_account_id(payload: dict[str, object]) -> str | None:
    """Preserve explicit application accounts without inferring from graph owners."""
    nested = payload.get("payload")
    sources = [payload, payload.get("inputs"), payload.get("params"), nested]
    if isinstance(nested, dict):
        sources.extend((nested.get("inputs"), nested.get("params")))
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("accountId", "account_id"):
            value = source.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string or null")
            text = value.strip()
            if len(text) > MAX_ACCOUNT_ID_LENGTH:
                raise ValueError(f"{key} must be at most {MAX_ACCOUNT_ID_LENGTH} characters")
            if text:
                payload["accountId"] = text
                return text
    return None


def usage_context_from_request(headers: object, payload: dict[str, object], path: str, workflow: str) -> dict[str, object]:
    context = dict(payload)
    context.setdefault("workflow", workflow)
    context.setdefault("mode", workflow)
    context.setdefault("route_path", path)
    for header, key in (
        ("X-Tenant-ID", "tenant_id"),
        ("X-Tenant", "tenant_id"),
        ("X-Workspace-Tenant", "tenant_id"),
        ("X-Tenant-Label", "tenant_label"),
        ("X-Tenant-Name", "tenant_label"),
        ("X-User-ID", "user_id"),
        ("X-User", "user_id"),
        ("X-Workspace-User", "user_id"),
        ("X-User-Label", "user_label"),
        ("X-User-Name", "user_label"),
        ("X-Account-ID", "account_id"),
    ):
        value = headers.get(header) if hasattr(headers, "get") else None
        if value and key not in context:
            context[key] = str(value).strip()
    if account_id := normalize_account_id(context):
        context["account_id"] = account_id
    return context


def normalize_school_filter(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    for alias, normalized in APIFY_LINKEDIN_SCHOOL_ALIASES:
        if alias.lower() in lowered:
            return normalized
    return text


def collect_school_filters(value: object, output: list[str]) -> None:
    if isinstance(value, list):
        for item in value:
            collect_school_filters(item, output)
        return
    if isinstance(value, dict):
        for key in ("school", "schools", "학교", "학력", "university", "대학"):
            if key in value:
                collect_school_filters(value.get(key), output)
        return
    school = normalize_school_filter(value)
    if school and school not in output:
        output.append(school)


def provider_filters_from_payload(provider: str, payload: dict[str, object]) -> dict[str, object]:
    if provider != "apify_linkedin_profile_search":
        return {}
    schools: list[str] = []
    for key in ("school", "schools", "학교", "학력", "university", "대학"):
        if key in payload:
            collect_school_filters(payload.get(key), schools)
    reference_payload = payload_mapping(payload.get("reference"))
    for source in (
        payload_mapping(payload.get("person") or payload.get("Person") or payload.get("person_data")),
        payload_mapping(payload.get("company") or payload.get("Company") or payload.get("company_data")),
        payload_mapping(reference_payload.get("person") or reference_payload.get("Person")),
        payload_mapping(reference_payload.get("company") or reference_payload.get("Company")),
    ):
        collect_school_filters(source, schools)
    return {"schools": schools[:10]} if schools else {}


def identity_reference_from_payload(payload: dict[str, object], *, kind: str, default: str, **metadata: object) -> dict[str, object]:
    reference_payload = payload_mapping(payload.get("reference"))
    person = payload_mapping(payload.get("person") or payload.get("Person") or payload.get("person_data") or reference_payload.get("person") or reference_payload.get("Person"))
    company = payload_mapping(payload.get("company") or payload.get("Company") or payload.get("company_data") or reference_payload.get("company") or reference_payload.get("Company"))
    result: dict[str, object] = {
        "kind": kind,
        "target": comparison_target_from_payload(payload, default),
        "person": person,
        "company": company,
    }
    for key, value in metadata.items():
        if value not in (None, ""):
            result[key] = value
    return result


def search_confidence_items(records_payload: list[dict[str, object]]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for index, record in enumerate(records_payload, start=1):
        item_id = str(record.get("result_id") or record.get("url") or f"search-{index}")
        items.append(
            {
                "item_id": item_id,
                "source": record.get("source"),
                "source_type": record.get("source_type"),
                "title": record.get("title"),
                "url": record.get("url") or record.get("canonical_url"),
                "snippet": record.get("snippet"),
                "published_at": record.get("published_at"),
                "raw_payload_json": record.get("raw_payload_json"),
            }
        )
    return items


def crawl_confidence_item(result_payload: dict[str, object]) -> dict[str, object]:
    item_id = str(result_payload.get("canonical_url") or result_payload.get("url") or "crawl-page")
    return {
        "item_id": item_id,
        "kind": "crawl_result",
        "crawler": result_payload.get("crawler"),
        "title": result_payload.get("title"),
        "url": result_payload.get("url") or result_payload.get("canonical_url"),
        "text_preview": result_payload.get("text_preview") or result_payload.get("text"),
        "raw_payload": {
            "status_code": result_payload.get("status_code"),
            "text_length": result_payload.get("text_length"),
            "error": result_payload.get("error"),
        },
    }


def article_item_id(item: dict[str, object], index: int) -> str:
    return str(
        item.get("result_id")
        or item.get("article_url")
        or item.get("url")
        or item.get("canonical_url")
        or f"article-{index}"
    )


def article_sentiment_items(items_payload: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "item_id": article_item_id(item, index),
            "source": item.get("source"),
            "title": item.get("title"),
            "snippet": item.get("snippet"),
            "article_text_preview": item.get("article_text_preview") or item.get("text_preview") or item.get("content") or item.get("description") or item.get("summary"),
            "published_at": item.get("published_at"),
        }
        for index, item in enumerate(items_payload, start=1)
    ]


def apply_article_sentiment(
    items_payload: list[dict[str, object]],
    annotations: dict[str, dict[str, object]],
) -> None:
    for index, item in enumerate(items_payload, start=1):
        annotation = annotations.get(article_item_id(item, index))
        if annotation:
            item["article_sentiment"] = annotation


def add_complete_news_clusters(result: dict[str, object]) -> None:
    """Annotate this response's articles without changing their search order."""
    try:
        items = result.get("items") or []
        target = str(result.get("search_keyword") or result.get("query") or "")[:MAX_QUERY_LENGTH]
        clustered = cluster_news_items(
            items,
            query=target,
            identity_terms=[target],
            embedding_model="",
            max_articles=len(items),
        )
        # Stored responses can be appended without colliding on cluster-001.
        prefix = f"news-{uuid.uuid4().hex}-"
        for cluster in clustered["clusters"]:
            cluster["cluster_id"] = prefix + cluster["cluster_id"]
        for item in clustered["items"]:
            if item["cluster_id"] is not None:
                item["cluster_id"] = prefix + item["cluster_id"]
        annotated_items = clustered.pop("items")
        clustered.update(status="ok", scope="response_items")
    except Exception as exc:
        result["article_cluster"] = {
            "status": "failed",
            "scope": "response_items",
            "error": {"code": "ARTICLE_CLUSTER_FAILED", "message": str(exc)[:500]},
        }
        return
    result["items"] = annotated_items
    result["article_cluster"] = clustered


def complete_news_with_analysis(payload: dict[str, object]) -> dict[str, object]:
    cache_key = None
    search_payload = payload
    if not payload.get("url"):
        prepared = prepare_payload(payload, "news")
        company = prepared.get("company")
        company = company if isinstance(company, dict) else {}
        company_name = news_company_name(company.get("회사명"))
        if company_name:
            search_payload = {"company": {"회사명": company_name}}
            cache_key = f"complete-news/{NEWS_RESULT_ANALYSIS_VERSION}:company:{company_name.casefold()}"
        else:
            query = str(prepared.get("query") or "")
            search_payload = {"query": query}
            cache_key = f"complete-news/{NEWS_RESULT_ANALYSIS_VERSION}:query:{query.casefold()}"
        # Shared company/query results must not contain another caller's
        # person, account, or request metadata. First-miss options set the batch.
        for option in ("limit", "crawl_business_news"):
            if option in prepared:
                search_payload[option] = prepared[option]

    def compute() -> dict[str, object]:
        result = complete_for_purpose("news", search_payload)
        items = result.get("items")
        news_items = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        target = str(result.get("search_keyword") or result.get("query") or "")[:MAX_QUERY_LENGTH]
        sentiment_summary, sentiment_annotations = evaluate_article_sentiment_batch(
            target, article_sentiment_items(news_items), usage_context=payload,
        )
        apply_article_sentiment(news_items, sentiment_annotations)
        result["article_sentiment"] = sentiment_summary
        add_complete_news_clusters(result)
        return result

    if cache_key is None:
        return compute()
    result, hit = cached_news_result(cache_key, compute)
    result["cache"] = {"hit": hit, "ttl_seconds": NEWS_RESULT_CACHE_TTL_SECONDS}
    return result


def apply_llm_confidence_to_search_records(
    records_payload: list[dict[str, object]],
    annotations: dict[str, dict[str, object]],
) -> None:
    for index, record in enumerate(records_payload, start=1):
        item_id = str(record.get("result_id") or record.get("url") or f"search-{index}")
        annotation = annotations.get(item_id)
        if not annotation:
            continue
        record["base_confidence"] = record.get("confidence")
        record["base_confidence_reason"] = record.get("confidence_reason")
        record["llm_confidence"] = annotation
        if annotation.get("status") == "ok" and annotation.get("confidence") is not None:
            record["confidence"] = annotation["confidence"]
            record["confidence_reason"] = annotation.get("confidence_reason") or record.get("confidence_reason") or ""
            record["processing_status"] = "llm_scored"


def apply_entity_match_to_search_records(
    records_payload: list[dict[str, object]],
    annotations: dict[str, dict[str, object]],
) -> None:
    for index, record in enumerate(records_payload, start=1):
        item_id = str(record.get("result_id") or record.get("url") or f"search-{index}")
        annotation = annotations.get(item_id)
        if not annotation:
            continue
        record["identity_label"] = annotation.get("identity_label")
        record["identity_match_score"] = annotation.get("score")
        record["identity_match"] = annotation


def apply_llm_confidence_to_crawl_result(
    result_payload: dict[str, object],
    annotation: dict[str, object] | None,
) -> None:
    if not annotation:
        return
    result_payload["llm_confidence"] = annotation
    if annotation.get("status") == "ok" and annotation.get("confidence") is not None:
        result_payload["confidence"] = annotation["confidence"]
        result_payload["confidence_reason"] = annotation.get("confidence_reason") or ""
        result_payload["processing_status"] = "llm_scored"


def apply_entity_match_to_crawl_result(
    result_payload: dict[str, object],
    annotation: dict[str, object] | None,
) -> None:
    if not annotation:
        return
    result_payload["identity_label"] = annotation.get("identity_label")
    result_payload["identity_match_score"] = annotation.get("score")
    result_payload["identity_match"] = annotation


def append_search_scores(records_payload: list[dict[str, object]]) -> None:
    for record in records_payload:
        scores = {
            "confidence": record.get("confidence"),
            "confidence_reason": record.get("confidence_reason"),
            "base_confidence": record.get("base_confidence"),
            "base_confidence_reason": record.get("base_confidence_reason"),
            "llm_confidence": record.get("llm_confidence"),
            "identity_match_score": record.get("identity_match_score"),
            "identity_match": record.get("identity_match"),
        }
        record.pop("scores", None)
        record["scores"] = {key: value for key, value in scores.items() if value is not None}


def append_crawl_scores(result_payload: dict[str, object]) -> None:
    scores = {
        "confidence": result_payload.get("confidence"),
        "confidence_reason": result_payload.get("confidence_reason"),
        "llm_confidence": result_payload.get("llm_confidence"),
        "identity_match_score": result_payload.get("identity_match_score"),
        "identity_match": result_payload.get("identity_match"),
    }
    result_payload.pop("scores", None)
    result_payload["scores"] = {key: value for key, value in scores.items() if value is not None}


def usage_snapshot_for_provider(provider: str) -> dict[str, object]:
    usage = summarize_usage(days=1)
    provider_row = next(
        (row for row in usage.get("providers", []) if isinstance(row, dict) and row.get("provider") == provider),
        None,
    )
    quota_scope = provider_row.get("quota_scope") if isinstance(provider_row, dict) else None
    quota_row = next(
        (
            row
            for row in usage.get("quota_scopes", [])
            if isinstance(row, dict) and quota_scope and row.get("quota_scope") == quota_scope
        ),
        None,
    )
    return {
        "query_or_url_values_logged": usage.get("query_or_url_values_logged"),
        "provider": provider_row,
        "quota_scope": quota_row,
        "local_daily_budget": {
            "scope": quota_row.get("quota_scope") if isinstance(quota_row, dict) else None,
            "limit": quota_row.get("configured_daily_limit") if isinstance(quota_row, dict) else None,
            "used_today": quota_row.get("today_units") if isinstance(quota_row, dict) else None,
            "remaining_today": quota_row.get("configured_daily_remaining") if isinstance(quota_row, dict) else None,
            "source": "default_or_env" if isinstance(quota_row, dict) and quota_row.get("configured_daily_limit") is not None else "not_configured",
        },
        "upstream_rate_window": quota_row.get("last_quota") if isinstance(quota_row, dict) else None,
    }


def load_article_cluster_example() -> dict[str, object]:
    with ARTICLE_CLUSTER_EXAMPLE_PATH.open(encoding="utf-8") as handle:
        example = json.load(handle)
    if not isinstance(example, dict):
        raise ValueError("ArticleCluster example must be a JSON object")
    return example


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "WebSearchAgentDashboard/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))

    def send_json(self, payload: object, *, status: int = 200) -> None:
        account_id = getattr(self, "request_account_id", None)
        if account_id and isinstance(payload, dict):
            payload = {**payload, "accountId": account_id}
        news_request = getattr(self, "_news_request_payload", None)
        if news_request is not None:
            log_news_execution(news_request, payload, status)
            self._news_request_payload = None
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            self.close_connection = True

    def send_html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_text(
        self,
        body: str,
        *,
        content_type: str = "text/plain; charset=utf-8",
        status: int = 200,
        download_name: str = "",
    ) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > 20_000:
            raise ValueError("Request body is too large")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def do_GET(self) -> None:
        self.request_account_id = None
        self._news_request_payload = None
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self.send_html(INDEX_HTML)
            return
        if parsed.path in {"/docs", "/docs.html"}:
            self.send_html(DOCS_HTML)
            return
        if parsed.path == ARTICLE_CLUSTER_ENDPOINT:
            self.send_html(ARTICLUSTER_HTML)
            return
        if parsed.path == ARTICLE_CLUSTER_EXAMPLE_ENDPOINT:
            try:
                self.send_json(load_article_cluster_example())
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.send_json({"error": f"ArticleCluster example unavailable: {type(exc).__name__}"}, status=503)
            return
        if parsed.path in {"/observability", "/observability.html"}:
            self.send_html(OBSERVABILITY_HTML)
            return
        if parsed.path == "/health":
            doctor = build_doctor_report()
            llm_confidence = doctor.get("llm_confidence") if isinstance(doctor.get("llm_confidence"), dict) else {}
            sentiment = article_sentiment_readiness()
            ready = [
                item["provider"]
                for item in doctor.get("providers", [])
                if isinstance(item, dict) and item.get("ready")
            ]
            ready_crawlers = [
                item["crawler"]
                for item in doctor.get("crawler_credentials", [])
                if isinstance(item, dict) and item.get("ready")
            ]
            self.send_json(
                {
                    "ok": True,
                    "version": "web-search-agent-dashboard/0.1",
                    "key_present_providers": ready,
                    "ready_providers": ready,
                    "ready_crawlers": ready_crawlers,
                    "llm_confidence_ready": bool(llm_confidence.get("ready")),
                    "article_sentiment_ready": bool(sentiment.get("ready")),
                    "provider_count": len(doctor.get("providers", [])),
                    "crawler_count": len(available_crawlers()),
                    "credential_values_printed": False,
                }
            )
            return
        if parsed.path == "/api/providers":
            self.send_json({"search": safe_provider_rows(), "crawlers": safe_crawler_rows()})
            return
        if parsed.path == "/api/usage":
            query = parse_qs(parsed.query)
            try:
                days = int((query.get("days") or ["1"])[0] or "1")
            except ValueError:
                days = 1
            days = max(1, min(days, 366))
            provider = (query.get("provider") or [None])[0]
            self.send_json(summarize_usage(days=days, provider=provider))
            return
        if parsed.path == "/api/search-audit":
            query = parse_qs(parsed.query)
            try:
                days = int((query.get("days") or ["1"])[0] or "1")
                limit = int((query.get("limit") or ["100"])[0] or "100")
            except ValueError:
                days, limit = 1, 100
            provider = (query.get("provider") or [None])[0]
            self.send_json(summarize_search_audit(days=days, provider=provider, limit=limit))
            return
        if parsed.path == "/api/bot-detections":
            query = parse_qs(parsed.query)
            try:
                days = int((query.get("days") or ["1"])[0] or "1")
                limit = int((query.get("limit") or ["100"])[0] or "100")
            except ValueError:
                days, limit = 1, 100
            self.send_json(summarize_bot_detections(days=days, limit=limit))
            return
        if parsed.path == "/metrics":
            self.send_text(
                render_prometheus_metrics(
                    summarize_usage(days=366),
                    summarize_search_audit(days=366),
                    safe_provider_rows(),
                ),
                content_type="text/plain; version=0.0.4; charset=utf-8",
            )
            return
        self.send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        self.request_account_id = None
        parsed = urlparse(self.path)
        self._news_request_payload = {} if parsed.path == "/v1/complete/news" and not orch_news_logged(self.headers) else None
        try:
            payload = self.read_json()
            if self._news_request_payload is not None:
                self._news_request_payload = payload
            self.request_account_id = normalize_account_id(payload)
            if parsed.path == "/v1/complete/linkClassification":
                self.handle_link_classification(payload)
                return
            if parsed.path == ATTACHMENT_LINK_ENDPOINT:
                self.handle_attachment_link(payload)
                return
            if parsed.path == ENTERPRISE_DB_ENDPOINT:
                self.handle_enterprise_db(payload)
                return
            if parsed.path in BIZ_NO_FINDER_ENDPOINTS:
                self.handle_biz_no_finder(payload)
                return
            if parsed.path == ARTICLE_CLUSTER_ENDPOINT:
                self.handle_article_cluster(payload)
                return
            if parsed.path in NOTEAPP_COMPLETE_ENDPOINTS:
                self.handle_noteapp_complete(parsed.path, payload)
                return
            if parsed.path == "/api/search":
                self.handle_search(payload)
                return
            if parsed.path.startswith("/api/search/"):
                self.handle_search_endpoint(parsed.path, payload)
                return
            if parsed.path in {"/api/find/linkedin", "/api/workflows/linkedin-profile"}:
                self.handle_linkedin_profile_finder(payload)
                return
            if parsed.path in SCRAP_ENDPOINT_CATEGORIES:
                self.handle_person_evidence_finder(parsed.path, payload)
                return
            if parsed.path == "/dashboard/api/crawl":
                self.handle_crawl(payload, response_mode="dashboard")
                return
            if parsed.path == "/api/crawl":
                self.handle_crawl(payload, response_mode="api")
                return
            if parsed.path.startswith("/api/crawl/"):
                self.handle_crawl_endpoint(parsed.path, payload)
                return
            self.send_json({"error": "not found"}, status=404)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, status=400)
        except SearchAgentError as exc:
            self.send_json({"error": str(exc)}, status=422)
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:1200] if exc.response is not None else ""
            self.send_json(
                {
                    "error": str(exc),
                    "upstream_status": exc.response.status_code if exc.response is not None else None,
                    "upstream_body": detail,
                },
                status=422,
            )
        except Exception as exc:
            self.send_json({"error": str(exc)}, status=500)

    def handle_search(self, payload: dict[str, object]) -> None:
        provider = clean_text(payload.get("provider"), max_length=80)
        if not provider:
            raise ValueError("provider and query are required")
        self.send_search_response(provider, payload)

    def handle_enterprise_db(self, payload: dict[str, object]) -> None:
        expected_key = secret_for_env("ENTERPRISE_DB_API_KEY")
        if not expected_key:
            self.send_json({"error": "enterpriseDB API key is not configured"}, status=503)
            return
        authorization = getattr(self, "headers", {}).get("Authorization", "")
        if not bearer_authorized(authorization, expected_key):
            self.send_json({"error": "unauthorized"}, status=401)
            return
        normalize_account_id(payload)
        started = now()
        try:
            result = complete_enterprise_db(payload)
        except Exception as exc:
            record_api_usage(
                "workflow_enterprise_db",
                "enrich",
                ok=False,
                metadata={"error_type": type(exc).__name__, "endpoint": ENTERPRISE_DB_ENDPOINT},
            )
            raise
        record_api_usage(
            "workflow_enterprise_db",
            "enrich",
            ok=True,
            result_count=len(result.get("items") or []),
            metadata={"endpoint": ENTERPRISE_DB_ENDPOINT, "status": result.get("status")},
        )
        result["gateway_elapsed_seconds"] = round(now() - started, 3)
        self.send_json(result)

    def handle_biz_no_finder(self, payload: dict[str, object]) -> None:
        expected_key = secret_for_env("ENTERPRISE_DB_API_KEY")
        if not expected_key:
            self.send_json({"error": "enterpriseDB API key is not configured"}, status=503)
            return
        authorization = getattr(self, "headers", {}).get("Authorization", "")
        if not bearer_authorized(authorization, expected_key):
            self.send_json({"error": "unauthorized"}, status=401)
            return
        normalize_account_id(payload)
        started = now()
        try:
            result = find_business_number(payload)
        except Exception as exc:
            record_api_usage(
                "workflow_biz_no_finder",
                "resolve",
                ok=False,
                metadata={"error_type": type(exc).__name__, "endpoint": BIZ_NO_FINDER_ENDPOINT},
            )
            raise
        record_api_usage(
            "workflow_biz_no_finder",
            "resolve",
            ok=True,
            result_count=len(result.get("candidates") or []),
            metadata={"endpoint": BIZ_NO_FINDER_ENDPOINT, "status": result.get("status")},
        )
        result["gateway_elapsed_seconds"] = round(now() - started, 3)
        self.send_json(result)

    def handle_search_endpoint(self, path: str, payload: dict[str, object]) -> None:
        parts = [part for part in path.split("/") if part]
        if len(parts) < 3:
            raise ValueError("unsupported search endpoint")
        group = parts[2]
        explicit_choice = parts[3] if len(parts) > 3 else ""
        if normalized_token(group) == "linkedin" and not explicit_choice:
            self.handle_linkedin_profile_finder(payload)
            return
        provider = search_provider_for_endpoint(group, payload, explicit_choice)
        self.send_search_response(provider, payload, group=group, endpoint=path, identity_scoring=True)

    def handle_linkedin_profile_finder(self, payload: dict[str, object]) -> None:
        started = now()
        try:
            result = find_linkedin_profiles(payload)
        except Exception as exc:
            record_api_usage(
                "workflow_linkedin_profile_finder",
                "find",
                ok=False,
                metadata={"error_type": type(exc).__name__},
            )
            raise
        best_match = result.get("best_match") if isinstance(result.get("best_match"), dict) else None
        record_api_usage(
            "workflow_linkedin_profile_finder",
            "find",
            ok=True,
            result_count=len(result.get("candidates") or []),
            metadata={
                "best_match_found": bool(best_match),
                "best_match_score": best_match.get("workflow_score") if best_match else None,
            },
        )
        result["endpoint"] = "/api/find/linkedin"
        result["gateway_elapsed_seconds"] = round(now() - started, 3)
        self.send_json(result)


    def handle_article_cluster(self, payload: dict[str, object]) -> None:
        started = now()
        normalize_account_id(payload)
        supplied_items = payload.get("items")
        if not isinstance(supplied_items, list) or not 1 <= len(supplied_items) <= MAX_ARTICLES:
            raise ValueError(f"items must be an array of 1..{MAX_ARTICLES} article objects; this endpoint does not search")
        if any(not isinstance(item, dict) for item in supplied_items):
            raise ValueError("items must contain only article objects")
        target = payload.get("target", "")
        if not isinstance(target, str) or len(target) > MAX_QUERY_LENGTH:
            raise ValueError(f"target must be a string of at most {MAX_QUERY_LENGTH} characters")
        target = target.strip()
        sentiment_enabled = payload.get("article_sentiment", False)
        if not isinstance(sentiment_enabled, bool):
            raise ValueError("article_sentiment must be a boolean")
        if sentiment_enabled and not target:
            raise ValueError("target is required when article_sentiment=true")
        if any(key in payload for key in ("news_request", "query", "limit", "crawl_business_news", "max_articles")):
            raise ValueError("search/limit controls are not accepted; supply the complete items list and optional target")
        cluster_options = payload.get("cluster") if isinstance(payload.get("cluster"), dict) else {}
        identity_terms = cluster_options.get("identity_terms", [])
        if not isinstance(identity_terms, list) or any(not isinstance(term, str) for term in identity_terms):
            raise ValueError("cluster.identity_terms must be an array of strings")
        identity_terms = [*identity_terms, target]
        items = [dict(item) for item in supplied_items]
        # Supplied annotations may describe another target; only return this run's sentiment.
        for item in items:
            item.pop("article_sentiment", None)
        try:
            sentiment_summary = None
            if sentiment_enabled:
                sentiment_summary, sentiment_annotations = evaluate_article_sentiment_batch(
                    target,
                    article_sentiment_items(items),
                    usage_context=payload,
                )
                apply_article_sentiment(items, sentiment_annotations)
            result = cluster_news_items(
                items,
                query=target,
                identity_terms=identity_terms,
                embedding_model=str(
                    (cluster_options.get("embedding_model") if isinstance(cluster_options, dict) else None)
                    or payload.get("embedding_model")
                    or os.getenv("ARTICLE_CLUSTER_EMBEDDING_MODEL", "")
                ),
                similarity_threshold=(cluster_options.get("similarity_threshold") if isinstance(cluster_options, dict) else None)
                or payload.get("similarity_threshold"),
                time_window_days=(cluster_options.get("time_window_days") if isinstance(cluster_options, dict) else None)
                or payload.get("time_window_days"),
                max_articles=len(items),
            )
        except Exception as exc:
            record_api_usage(
                "workflow_article_cluster",
                "cluster",
                ok=False,
                metadata={"endpoint": ARTICLE_CLUSTER_ENDPOINT, "error_type": type(exc).__name__},
            )
            raise
        result["endpoint"] = ARTICLE_CLUSTER_ENDPOINT
        result["response_mode"] = "synchronous"
        result["completion_status"] = "completed"
        result["source"] = {"mode": "supplied_items", "live_search_called": False}
        result["target"] = target
        if sentiment_summary is not None:
            result["article_sentiment"] = sentiment_summary
        record_api_usage(
            "workflow_article_cluster",
            "cluster",
            ok=True,
            result_count=int(result.get("cluster_count") or 0),
            metadata={"endpoint": ARTICLE_CLUSTER_ENDPOINT, "article_count": int(result.get("count") or 0)},
        )
        result["gateway_elapsed_seconds"] = round(now() - started, 3)
        self.send_json(result)

    def handle_person_evidence_finder(self, path: str, payload: dict[str, object]) -> None:
        forced_categories = SCRAP_ENDPOINT_CATEGORIES.get(path)
        started = now()
        operation = "scrap" if path.startswith("/api/scrap/") else "find"
        try:
            result = find_person_evidence(payload, forced_categories=forced_categories)
        except Exception as exc:
            record_api_usage(
                "workflow_person_evidence_finder",
                operation,
                ok=False,
                metadata={"error_type": type(exc).__name__},
            )
            raise
        record_api_usage(
            "workflow_person_evidence_finder",
            operation,
            ok=True,
            result_count=len(result.get("candidates") or []),
            metadata={
                "endpoint": path,
                "category_count": len(result.get("categories") or []),
            },
        )
        result["api_family"] = "scrap"
        result["legacy_alias"] = path.startswith("/api/find/")
        result["endpoint"] = path
        result["gateway_elapsed_seconds"] = round(now() - started, 3)
        self.send_json(result)

    def handle_link_classification(self, payload: dict[str, object]) -> None:
        normalize_account_id(payload)
        url = str(payload.get("url") or "").strip()
        if not url:
            raise ValueError("url is required")
        
        options = ClassificationOptions(probe=True, follow_redirects=True, allow_private_network=False)
        result = _URL_CLASSIFIER.classify(url, options)
        self.send_json(result.model_dump())

    def handle_attachment_link(self, payload: dict[str, object]) -> None:
        normalize_account_id(payload)
        options = self.crawl_request_options({"url": payload.get("url")})
        started = now()
        try:
            result = self.run_crawler_result("auto", options)
        except Exception as exc:
            record_api_usage(
                "crawler_auto",
                "attachment_link",
                ok=False,
                result_count=0,
                metadata={"transport": "gateway", "error_type": type(exc).__name__},
            )
            self.send_json(
                {
                    "status": "error",
                    "endpoint": ATTACHMENT_LINK_ENDPOINT,
                    "requested_crawler": "auto",
                    "resolved_crawler": None,
                    "url": options["url"],
                    "attachments": [],
                    "error": {
                        "code": "CRAWL_FAILED",
                        "message": str(exc),
                        "retryable": False,
                    },
                },
                status=422,
            )
            return

        failed = bool(result.error)
        record_api_usage(
            "crawler_auto",
            "attachment_link",
            ok=not failed,
            status_code=result.status_code,
            result_count=0 if failed else 1,
            metadata={"transport": "gateway"},
        )
        if failed:
            self.send_json(
                {
                    "status": "error",
                    "endpoint": ATTACHMENT_LINK_ENDPOINT,
                    "requested_crawler": "auto",
                    "resolved_crawler": result.crawler,
                    "url": result.url,
                    "attachments": [],
                    "error": {
                        "code": "CRAWL_FAILED",
                        "message": result.error,
                        "retryable": False,
                    },
                },
                status=422,
            )
            return

        response = attachment_link_response(result)
        response["elapsed_seconds"] = round(now() - started, 3)
        self.send_json(response)

    def handle_noteapp_complete(self, path: str, payload: dict[str, object]) -> None:
        normalize_account_id(payload)
        purpose = path.rsplit("/", 1)[-1]
        started = now()
        provider_name = f"workflow_noteapp_complete_{purpose}"
        usage_payload = usage_context_from_request(getattr(self, "headers", {}), payload, path, "cnote_app")
        try:
            result = complete_news_with_analysis(usage_payload) if purpose == "news" else complete_for_purpose(purpose, usage_payload)
        except Exception as exc:
            record_api_usage(
                provider_name,
                "complete",
                ok=False,
                metadata={"error_type": type(exc).__name__, "endpoint": path},
            )
            raise
        record_api_usage(
            provider_name,
            "complete",
            ok=True,
            result_count=len(result.get("items") or []),
            metadata={"endpoint": path, "item_count": len(result.get("items") or [])},
        )
        result.setdefault("response_mode", "synchronous")
        result.setdefault("completion_status", "completed")
        result["gateway_elapsed_seconds"] = round(now() - started, 3)
        self.send_json(result)

    def send_search_response(
        self,
        provider: str,
        payload: dict[str, object],
        *,
        group: str | None = None,
        endpoint: str | None = None,
        identity_scoring: bool = False,
    ) -> None:
        query = clean_text(payload.get("query"), max_length=MAX_QUERY_LENGTH)
        intent = clean_text(payload.get("intent"), max_length=80) or None
        if not provider or not query:
            raise ValueError("provider and query are required")
        limit = int(payload.get("limit") or 3)
        limit = max(1, min(limit, MAX_RESULTS))
        start = int(payload.get("start") or 1)
        start = max(1, min(start, 1000))
        started = now()
        client = create_search_client(provider)
        provider_filters = provider_filters_from_payload(provider, payload)
        search_with_filters = getattr(client, "search_with_filters", None)
        if provider_filters and callable(search_with_filters):
            records = search_with_filters(
                query,
                filters=provider_filters,
                limit=limit,
                start=start,
                query_intent=intent,
            )
        else:
            records = client.search(query, limit=limit, start=start, query_intent=intent)
        records_payload = [record.to_dict() for record in records]
        if identity_scoring:
            usage_context = usage_context_from_request(getattr(self, "headers", {}), payload, endpoint or "/api/search", "web_search")
            match_reference = identity_reference_from_payload(
                payload,
                kind="search",
                default=query,
                query=query,
                intent=intent or "",
                provider=provider,
            )
            search_items = search_confidence_items(records_payload)
            identity_match_summary, identity_match_annotations = score_entity_match_batch(
                match_reference,
                search_items,
            )
            apply_entity_match_to_search_records(records_payload, identity_match_annotations)
            confidence_summary, confidence_annotations = evaluate_confidence_batch(
                match_reference,
                search_items,
                enabled=llm_confidence_enabled_for_payload(payload),
                usage_context=usage_context,
            )
            apply_llm_confidence_to_search_records(records_payload, confidence_annotations)
            append_search_scores(records_payload)
        else:
            identity_match_summary = None
            confidence_summary = None
        response = {
            "provider": provider,
            "query": query,
            "start": start,
            "count": len(records),
            "elapsed_seconds": round(now() - started, 3),
            "usage_snapshot": usage_snapshot_for_provider(provider),
            "records": records_payload,
        }
        if identity_scoring:
            response["identity_match"] = identity_match_summary
            response["llm_confidence"] = confidence_summary
        if group:
            response["group"] = group
        if endpoint:
            response["endpoint"] = endpoint
        self.send_json(response)

    def handle_crawl(self, payload: dict[str, object], *, response_mode: str = "api") -> None:
        crawler_name = clean_text(payload.get("crawler") or "auto", max_length=40)
        if crawler_name not in {"auto", "static", "playwright", "playwright_stealth", "selenium", "firecrawl", "watercrawl", "stealth_browser", "apify_linkedin"}:
            raise ValueError("unsupported crawler")
        self.send_crawl_response(crawler_name, payload, response_mode=response_mode)

    def handle_crawl_endpoint(self, path: str, payload: dict[str, object]) -> None:
        parts = [part for part in path.split("/") if part]
        if len(parts) < 3:
            raise ValueError("unsupported crawl endpoint")
        endpoint_name = normalized_token(parts[2])
        if endpoint_name in FALLBACK_CRAWL_ENDPOINTS:
            self.send_playwright_firecrawl_response(payload, endpoint=path)
            return
        crawler_name = CRAWL_ENDPOINTS.get(endpoint_name)
        if not crawler_name:
            raise ValueError("unsupported crawl endpoint")
        self.send_crawl_response(crawler_name, payload, endpoint=path, strict_failure=True, identity_scoring=True)

    def crawl_request_options(self, payload: dict[str, object]) -> dict[str, object]:
        url = public_http_url(str(payload.get("url") or ""))
        browser = clean_text(payload.get("browser") or "chromium", max_length=20)
        if browser not in {"chromium", "firefox", "webkit"}:
            raise ValueError("unsupported browser")
        base_delay = float(payload.get("base_delay_seconds") or 2.0)
        gov_delay = float(payload.get("government_delay_seconds") or 10.0)
        return {
            "url": url,
            "browser": browser,
            "respect_robots": bool(payload.get("respect_robots", True)),
            "allow_selenium_fallback": bool(payload.get("allow_selenium_fallback", False)),
            "base_delay_seconds": max(0.0, min(base_delay, 60.0)),
            "government_delay_seconds": max(0.0, min(gov_delay, 120.0)),
        }

    def run_crawler_result(self, crawler_name: str, options: dict[str, object]) -> object:
        crawler = create_crawler(
            crawler_name,
            respect_robots=bool(options["respect_robots"]),
            browser=str(options["browser"]),
            allow_selenium_fallback=bool(options["allow_selenium_fallback"]),
            base_delay_seconds=float(options["base_delay_seconds"]),
            government_delay_seconds=float(options["government_delay_seconds"]),
        )
        return crawler.crawl(str(options["url"]))

    def send_crawl_response(
        self,
        crawler_name: str,
        payload: dict[str, object],
        *,
        endpoint: str | None = None,
        identity_scoring: bool = False,
        strict_failure: bool = False,
        response_mode: str = "api",
    ) -> None:
        options = self.crawl_request_options(payload)
        started = now()
        try:
            result = self.run_crawler_result(crawler_name, options)
        except Exception as exc:
            record_api_usage(
                f"crawler_{crawler_name}",
                "crawl",
                ok=False,
                result_count=0,
                metadata={"transport": "gateway", "error_type": type(exc).__name__},
            )
            raise
        failed = crawl_failed(result) if strict_failure else bool(result.error)
        record_api_usage(
            f"crawler_{crawler_name}",
            "crawl",
            ok=not failed,
            status_code=result.status_code,
            result_count=1 if not failed else 0,
            metadata={"transport": "gateway"},
        )
        result_payload = crawl_result_payload(result)
        if identity_scoring:
            usage_context = usage_context_from_request(getattr(self, "headers", {}), payload, endpoint or "/api/crawl", "web_search")
            confidence_item = crawl_confidence_item(result_payload)
            match_reference = identity_reference_from_payload(
                payload,
                kind="crawl",
                default=str(options["url"]),
                url=str(options["url"]),
                crawler=crawler_name,
            )
            identity_match_summary, identity_match_annotations = score_entity_match_batch(
                match_reference,
                [confidence_item],
            )
            apply_entity_match_to_crawl_result(
                result_payload,
                identity_match_annotations.get(str(confidence_item["item_id"])),
            )
            confidence_summary, confidence_annotations = evaluate_confidence_batch(
                match_reference,
                [confidence_item],
                enabled=llm_confidence_enabled_for_payload(payload),
                usage_context=usage_context,
            )
            apply_llm_confidence_to_crawl_result(
                result_payload,
                confidence_annotations.get(str(confidence_item["item_id"])),
            )
            append_crawl_scores(result_payload)
        else:
            identity_match_summary = None
            confidence_summary = None
        response = {
            "crawler": crawler_name,
            "response_mode": response_mode,
            "elapsed_seconds": round(now() - started, 3),
            "result": result_payload,
        }
        if identity_scoring:
            response["identity_match"] = identity_match_summary
            response["llm_confidence"] = confidence_summary
        if endpoint:
            response["endpoint"] = endpoint
        self.send_json(response, status=200 if not failed else 422)

    def send_playwright_firecrawl_response(self, payload: dict[str, object], *, endpoint: str) -> None:
        options = self.crawl_request_options(payload)
        started = now()
        attempts: list[dict[str, object]] = []
        fallback_used = False
        final_result = None
        try:
            playwright_result = self.run_crawler_result("playwright", options)
            attempts.append(
                {
                    "crawler": "playwright",
                    "failed": crawl_failed(playwright_result),
                    "status_code": playwright_result.status_code,
                    "error": playwright_result.error,
                    "text_length": len(playwright_result.text or ""),
                }
            )
            final_result = playwright_result
            if crawl_failed(playwright_result):
                raw_payload = getattr(playwright_result, "raw_payload", {}) or {}
                bot_detection = raw_payload.get("bot_detection") or {}
                if not bot_detection.get("detected"):
                    fallback_used = True
                    firecrawl_result = self.run_crawler_result("firecrawl", options)
                    attempts.append(
                        {
                            "crawler": "firecrawl",
                            "failed": crawl_failed(firecrawl_result),
                            "status_code": firecrawl_result.status_code,
                            "error": firecrawl_result.error,
                            "text_length": len(firecrawl_result.text or ""),
                        }
                    )
                    final_result = firecrawl_result
        except Exception as exc:
            record_api_usage(
                "crawler_playwright_firecrawl",
                "crawl",
                ok=False,
                result_count=0,
                metadata={"transport": "gateway", "error_type": type(exc).__name__},
            )
            raise
        failed = crawl_failed(final_result)
        record_api_usage(
            "crawler_playwright_firecrawl",
            "crawl",
            ok=not failed,
            status_code=final_result.status_code,
            result_count=1 if not failed else 0,
            metadata={"transport": "gateway"},
        )
        result_payload = crawl_result_payload(final_result)
        confidence_item = crawl_confidence_item(result_payload)
        match_reference = identity_reference_from_payload(
            payload,
            kind="crawl",
            default=str(options["url"]),
            url=str(options["url"]),
            crawler="playwright_firecrawl",
            primary_crawler="playwright",
            fallback_crawler="firecrawl",
            fallback_used=str(fallback_used),
        )
        identity_match_summary, identity_match_annotations = score_entity_match_batch(
            match_reference,
            [confidence_item],
        )
        apply_entity_match_to_crawl_result(
            result_payload,
            identity_match_annotations.get(str(confidence_item["item_id"])),
        )
        confidence_summary, confidence_annotations = evaluate_confidence_batch(
            match_reference,
            [confidence_item],
            enabled=llm_confidence_enabled_for_payload(payload),
            usage_context=usage_context_from_request(getattr(self, "headers", {}), payload, endpoint, "web_search"),
        )
        apply_llm_confidence_to_crawl_result(
            result_payload,
            confidence_annotations.get(str(confidence_item["item_id"])),
        )
        append_crawl_scores(result_payload)
        self.send_json(
            {
                "crawler": "playwright_firecrawl",
                "endpoint": endpoint,
                "primary_crawler": "playwright",
                "fallback_crawler": "firecrawl",
                "fallback_used": fallback_used,
                "attempts": attempts,
                "elapsed_seconds": round(now() - started, 3),
                "identity_match": identity_match_summary,
                "llm_confidence": confidence_summary,
                "result": result_payload,
            },
            status=200 if not failed else 422,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the Web Search Agent crawl dashboard.")
    parser.add_argument("--host", default=os.getenv("WEB_SEARCH_DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("WEB_SEARCH_DASHBOARD_PORT", "9101")))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    start_news_execution_worker()

    def request_shutdown(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, request_shutdown)
    print(f"Serving crawl dashboard on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_stealth_browser_runtime()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
