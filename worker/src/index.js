/**
 * stock-dashboard-api Worker
 *
 * Endpoints:
 *   GET  /api/prices    → 현재 주가 (KV: "prices")
 *   GET  /api/briefing  → 브리핑 요약 (KV: "briefing")
 *   GET  /api/news      → 뉴스 요약 (KV: "news")
 *   POST /api/trigger   → 수동 업데이트 트리거
 *   GET  /api/status    → 마지막 업데이트 시각
 *
 * Cron: 2시간마다 자동 실행
 */

import { fetchBriefingRaw, buildBriefingSummary } from './briefing.js';
import { fetchNewsRaw, buildNewsSummary } from './news.js';
import { fetchPrices } from './prices.js';

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
};

// ── HTTP handler ──────────────────────────────────────────────────────

export default {
  async fetch(request, env, ctx) {
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: CORS });
    }

    const url = new URL(request.url);
    const path = url.pathname;

    // GET /api/prices
    if (path === '/api/prices' && request.method === 'GET') {
      return kvGet(env, 'prices');
    }

    // GET /api/briefing
    if (path === '/api/briefing' && request.method === 'GET') {
      return kvGet(env, 'briefing');
    }

    // GET /api/news
    if (path === '/api/news' && request.method === 'GET') {
      return kvGet(env, 'news');
    }

    // GET /api/status
    if (path === '/api/status' && request.method === 'GET') {
      const status = await env.KV.get('_status', { type: 'json' }) || {};
      return jsonResponse(status);
    }

    // POST /api/trigger — 수동 실행 (trigger-briefing.js 대체)
    if (path === '/api/trigger' && request.method === 'POST') {
      // 백그라운드에서 실행 (응답은 즉시)
      ctx.waitUntil(runUpdates(env).catch(e => console.error('업데이트 실패:', e)));
      return jsonResponse({ ok: true, message: '업데이트 시작됨' });
    }

    return new Response('Not Found', { status: 404 });
  },

  // ── Cron handler ────────────────────────────────────────────────────
  async scheduled(event, env, ctx) {
    ctx.waitUntil(runUpdates(env));
  },
};

// ── Core update logic ─────────────────────────────────────────────────

async function runUpdates(env) {
  const startedAt = new Date().toISOString();
  console.log(`[${startedAt}] 업데이트 시작`);

  try {
    // Phase 1: 데이터 병렬 수집
    console.log('Phase 1: 데이터 수집 중...');
    const [briefingRaw, newsRaw, prices] = await Promise.all([
      fetchBriefingRaw(env).catch(e => { console.error('briefing fetch 실패:', e); return null; }),
      fetchNewsRaw(env).catch(e => { console.error('news fetch 실패:', e); return null; }),
      fetchPrices(env).catch(e => { console.error('prices fetch 실패:', e); return null; }),
    ]);

    // Phase 2: 원본 데이터 KV 저장 + Claude 요약 병렬 실행
    console.log('Phase 2: 요약 생성 중...');
    const tasks = [];

    if (prices) {
      tasks.push(env.KV.put('prices', JSON.stringify(prices)));
    }

    if (briefingRaw) {
      tasks.push(env.KV.put('briefing_raw', JSON.stringify(briefingRaw)));
      tasks.push(
        buildBriefingSummary(briefingRaw, env)
          .then(summary => env.KV.put('briefing', JSON.stringify(summary)))
          .catch(e => console.error('briefing 요약 실패:', e))
      );
    }

    if (newsRaw) {
      tasks.push(env.KV.put('news_raw', JSON.stringify(newsRaw)));
      tasks.push(
        buildNewsSummary(newsRaw, env)
          .then(summary => env.KV.put('news', JSON.stringify(summary)))
          .catch(e => console.error('news 요약 실패:', e))
      );
    }

    await Promise.all(tasks);

    // 상태 업데이트
    await env.KV.put('_status', JSON.stringify({
      last_updated: new Date().toISOString(),
      started_at: startedAt,
      prices_ok: !!prices,
      briefing_ok: !!briefingRaw,
      news_ok: !!newsRaw,
    }));

    console.log('업데이트 완료');
  } catch (e) {
    console.error('업데이트 실패:', e);
    await env.KV.put('_status', JSON.stringify({
      last_updated: new Date().toISOString(),
      error: e.message,
    }));
  }
}

// ── Helpers ───────────────────────────────────────────────────────────

async function kvGet(env, key) {
  const data = await env.KV.get(key, { type: 'json' });
  if (!data) return new Response(JSON.stringify({ error: 'No data yet' }), {
    status: 404, headers: { 'Content-Type': 'application/json', ...CORS }
  });
  return jsonResponse(data);
}

function jsonResponse(data) {
  return new Response(JSON.stringify(data), {
    headers: { 'Content-Type': 'application/json', ...CORS }
  });
}
