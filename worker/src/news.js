/**
 * 뉴스 fetch + Claude 요약
 * Python: fetch_stock_news.py + summarize_stock_news.py 포팅
 *
 * 보유 종목 목록은 KV["stock_list"]에서 읽음
 * → 포트폴리오 변경 시 scripts/upload-kv.js 로 업데이트
 */

import { callClaude } from './utils.js';

const MAX_ARTICLES = 8;
const NEWS_DAYS = 7;
const BATCH_SIZE = 5;

// ── Google News RSS 수집 ──────────────────────────────────────────────

function parseGoogleNewsRss(xml, stockName) {
  const cutoff = Date.now() - NEWS_DAYS * 24 * 60 * 60 * 1000;
  const articles = [];

  const items = [...xml.matchAll(/<item>([\s\S]*?)<\/item>/g)];
  for (const [, item] of items) {
    const title   = (item.match(/<title>([\s\S]*?)<\/title>/)     || [])[1]?.trim().replace(/<!\[CDATA\[(.*?)\]\]>/s, '$1') || '';
    const link    = (item.match(/<link>([\s\S]*?)<\/link>/)       || [])[1]?.trim() || '';
    const pubDate = (item.match(/<pubDate>([\s\S]*?)<\/pubDate>/) || [])[1]?.trim() || '';
    const desc    = (item.match(/<description>([\s\S]*?)<\/description>/) || [])[1]?.replace(/<[^>]+>/g, '').trim() || '';
    const srcEl   = item.match(/<source[^>]*>([\s\S]*?)<\/source>/);
    const source  = srcEl?.[1]?.trim() || '';

    if (!title) continue;

    let dateStr = '';
    try {
      // "Mon, 13 Apr 2026 12:34:56 GMT" 형식
      const dt = new Date(pubDate.slice(0, 25));
      if (isNaN(dt.getTime()) || dt.getTime() < cutoff) continue;
      // KST 표시
      const kst = new Date(dt.getTime() + 9 * 60 * 60 * 1000);
      dateStr = kst.toISOString().slice(0, 16).replace('T', ' ');
    } catch {
      dateStr = pubDate.slice(0, 10);
    }

    articles.push({ title, link, date: dateStr, source, snippet: desc.slice(0, 400) });
    if (articles.length >= MAX_ARTICLES) break;
  }
  return articles;
}

async function fetchGoogleNews(stockName) {
  const encoded = encodeURIComponent(stockName);
  const url = `https://news.google.com/rss/search?q=${encoded}&hl=ko&gl=KR&ceid=KR:ko`;
  try {
    const resp = await fetch(url, {
      headers: { 'User-Agent': 'Mozilla/5.0' },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const xml = await resp.text();
    return parseGoogleNewsRss(xml, stockName);
  } catch (e) {
    console.error(`뉴스 fetch 실패 [${stockName}]:`, e.message);
    return [];
  }
}

// ── fetchNewsRaw (메인 export) ────────────────────────────────────────

export async function fetchNewsRaw(env) {
  // 보유 종목 목록을 KV에서 읽기
  const stockList = await env.KV.get('stock_list', { type: 'json' }) || [];
  if (!stockList.length) {
    console.warn('stock_list가 KV에 없음. scripts/upload-kv.js를 먼저 실행하세요.');
    return { fetched_at: new Date().toISOString(), stocks: {} };
  }

  console.log(`뉴스 수집: ${stockList.length}개 종목`);

  // 병렬 fetch (동시에 너무 많으면 rate limit → 청크로 처리)
  const CHUNK = 10;
  const result = {};
  for (let i = 0; i < stockList.length; i += CHUNK) {
    const chunk = stockList.slice(i, i + CHUNK);
    const fetched = await Promise.all(
      chunk.map(async (stock) => {
        const articles = await fetchGoogleNews(stock);
        return [stock, articles];
      })
    );
    for (const [stock, articles] of fetched) {
      result[stock] = articles;
    }
  }

  const withNews = Object.values(result).filter(a => a.length > 0).length;
  console.log(`뉴스 수집 완료: ${withNews}/${stockList.length}개 종목에 뉴스 있음`);

  return { fetched_at: new Date().toISOString(), stocks: result };
}

// ── Claude 요약 ───────────────────────────────────────────────────────

async function summarizeBatch(batch, env) {
  // batch: [[stockName, articles[]], ...]
  const sections = batch
    .filter(([, arts]) => arts.length > 0)
    .map(([stock, articles]) => {
      const lines = [`\n## ${stock} (${articles.length}건)`];
      for (const a of articles) {
        lines.push(`[${a.date}] [${a.source}] ${a.title}`);
        if (a.snippet) lines.push(`  ${a.snippet.slice(0, 200)}`);
      }
      return lines.join('\n');
    });

  if (!sections.length) return {};

  const prompt = `다음은 주식 보유 종목들의 최근 7일 뉴스입니다.
각 종목별로 아래 JSON 형식으로 분석해주세요.

규칙:
- 반드시 유효한 JSON만 출력 (다른 텍스트 없이)
- 모든 텍스트는 한국어
- 기사가 없는 종목은 결과에서 제외
- 두산: 두산 베어스 야구 기사가 혼입됨 → 투자 관련만 분석
- 통위: 방통위(방송통신위원회) 기사 혼입 → 실제 통위(Tongwei) 뉴스 없으면 제외

JSON 형식:
{
  "종목명": {
    "summary": "핵심 내용 2-3문장 (투자 관점 중심)",
    "sentiment": "positive|negative|neutral",
    "sentiment_reason": "감성 판단 근거 한 줄",
    "keywords": ["키워드1", "키워드2", "키워드3", "키워드4"],
    "notable": "특히 주목할 기사 제목 (없으면 빈문자열)"
  }
}

뉴스 데이터:
${sections.join('\n')}`;

  try {
    const raw = await callClaude(prompt, env, 2048);
    return JSON.parse(raw);
  } catch (e) {
    console.error('뉴스 배치 요약 실패:', e.message);
    return {};
  }
}

export async function buildNewsSummary(newsRaw, env) {
  const allStocks = newsRaw.stocks || {};
  const withNews = Object.entries(allStocks).filter(([, arts]) => arts.length > 0);

  console.log(`뉴스 요약: ${withNews.length}개 종목 → Claude (배치 ${BATCH_SIZE}개씩)`);

  const summaries = {};
  for (let i = 0; i < withNews.length; i += BATCH_SIZE) {
    const batch = withNews.slice(i, i + BATCH_SIZE);
    const names = batch.map(([s]) => s).join(', ');
    console.log(`  배치 ${Math.floor(i / BATCH_SIZE) + 1}: ${names}`);
    const result = await summarizeBatch(batch, env);
    Object.assign(summaries, result);
  }

  const output = {
    updated_at: new Date().toISOString(),
    fetched_at: newsRaw.fetched_at,
    stocks: {},
  };

  for (const [stock, articles] of Object.entries(allStocks)) {
    const s = summaries[stock] || {};
    output.stocks[stock] = {
      articles,
      article_count: articles.length,
      has_news: articles.length > 0,
      summary: s.summary || '',
      sentiment: s.sentiment || 'neutral',
      sentiment_reason: s.sentiment_reason || '',
      keywords: s.keywords || [],
      notable: s.notable || '',
    };
  }

  return output;
}
