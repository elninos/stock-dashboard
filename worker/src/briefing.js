/**
 * 브리핑 fetch + Claude 요약
 * Python: fetch_briefing.py + summarize_briefing.py 포팅
 */

import { callClaude, stripHtml, parseRfc2822Date } from './utils.js';

const KEEP_DAYS = 30;
const MAX_POST_CHARS = 2000;

// ── 소스 목록 (sources.json → Worker 상수) ──────────────────────────

const TELEGRAM_SOURCES = [
  { name: '시장 이야기 by 제이슨', id: 'bumgore',               category: '증권사 리포트' },
  { name: '도PB의 생존투자',        id: 'survival_DoPB',         category: '매크로/트레이딩' },
  { name: '그로쓰리서치',           id: 'growthresearch',         category: '산업/기술 분석' },
  { name: '스파르탄 리서치',         id: 'SpartanResearch',        category: '매크로/크립토' },
  { name: '디티커',                 id: 'd_ticker',               category: '매매 신호/트레이딩' },
  { name: '월스트리트 캣츠',         id: 'wallstreet_cats_stock',  category: '미국 시장 분석' },
  { name: '여의도 랩',              id: 'Yeouido_Lab',            category: '국내 시장/종목 분석' },
  { name: '액티브 ETF 알림',        id: 'active_etf_alert_bot',   category: 'ETF 매매 동향' },
  { name: 'KK Kontemporaries',     id: 'kkkontemp',              category: '매크로 분석' },
];

const BLOG_SOURCES = [
  { name: '카이에 블로그',          id: 'cahier',      category: '반도체/AI 심층 분석' },
  { name: '안아줘 투자이야기',       id: 'ehgur06',     category: '투자 아이디어' },
  { name: "Seung's 투자와 생각",    id: 'tmdejr1267',  category: '산업/바이오 분석' },
  { name: 'KK Kontemporaries',     id: 'kk_kontemp',  category: '매크로 분석' },
];

// ── Telegram 파싱 ─────────────────────────────────────────────────────

function parseTelegramHtml(html, channelId) {
  const posts = [];
  // 포스트 블록 분리
  const blocks = html.split('data-post="').slice(1);

  for (const block of blocks) {
    const postIdMatch = block.match(/^([^"]+)"/);
    if (!postIdMatch) continue;
    const postId = postIdMatch[1];

    // 날짜/시간 추출
    const dtMatch = block.match(/<time[^>]*datetime="([^"]+)"/);
    let postDate = '', postTime = '';
    if (dtMatch) {
      try {
        const dt = new Date(dtMatch[1]);
        // UTC+9 변환
        const kst = new Date(dt.getTime() + 9 * 60 * 60 * 1000);
        postDate = kst.toISOString().slice(0, 10);
        postTime = kst.toISOString().slice(11, 16);
      } catch {}
    }

    // 메시지 텍스트
    const textMatch = block.match(/class="tgme_widget_message_text[^"]*"[^>]*>([\s\S]*?)<\/div>/);
    if (!textMatch) continue;

    const rawHtml = textMatch[1];
    const text = stripHtml(rawHtml);
    if (text.length < 10) continue;

    // 링크 추출
    const links = [];
    const linkRe = /href="(https?:\/\/[^"]+)"/g;
    let m;
    while ((m = linkRe.exec(rawHtml)) !== null) links.push(m[1]);
    // 링크 프리뷰 URL
    const previewRe = /class="tgme_widget_message_link_preview"[^>]*href="(https?:\/\/[^"]+)"/g;
    while ((m = previewRe.exec(block)) !== null) links.push(m[1]);

    posts.push({
      date: postDate,
      time: postTime,
      text: text.slice(0, MAX_POST_CHARS),
      links: [...new Set(links)],
      post_url: `https://t.me/${postId}`,
    });
  }

  return posts;
}

async function fetchTelegramPosts(source) {
  const url = `https://t.me/s/${source.id}`;
  try {
    const resp = await fetch(url, {
      headers: {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Accept-Language': 'ko-KR,ko;q=0.9',
      },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const html = await resp.text();
    return parseTelegramHtml(html, source.id);
  } catch (e) {
    console.error(`Telegram fetch 실패 [${source.id}]:`, e.message);
    return [];
  }
}

// ── Naver Blog RSS 파싱 ───────────────────────────────────────────────

function parseNaverRss(xml) {
  const posts = [];
  const items = [...xml.matchAll(/<item>([\s\S]*?)<\/item>/g)];

  for (const [, item] of items) {
    const title   = (item.match(/<title><!\[CDATA\[([\s\S]*?)\]\]><\/title>/)     || [])[1]?.trim() || '';
    const link    = (item.match(/<link><!\[CDATA\[([\s\S]*?)\]\]><\/link>/)       || [])[1]?.trim().replace(/\?fromRss=.*/, '') || '';
    const descRaw = (item.match(/<description><!\[CDATA\[([\s\S]*?)\]\]><\/description>/) || [])[1] || '';
    const pubDate = (item.match(/<pubDate>([\s\S]*?)<\/pubDate>/)                 || [])[1]?.trim() || '';

    const desc = stripHtml(descRaw.replace(/<img[^>]+\/?>/g, '')).trim();
    if (!title || !desc) continue;

    let postDate = '', postTime = '';
    try {
      const dt = parseRfc2822Date(pubDate);
      if (dt) {
        const kst = new Date(dt.getTime() + 9 * 60 * 60 * 1000);
        postDate = kst.toISOString().slice(0, 10);
        postTime = kst.toISOString().slice(11, 16);
      }
    } catch {}

    posts.push({
      date: postDate,
      time: postTime,
      text: `[${title}]\n\n${desc}`.slice(0, 3000),
      links: link ? [link] : [],
      post_url: link,
    });
  }
  return posts;
}

async function fetchNaverBlogPosts(source) {
  const rssUrl = `https://rss.blog.naver.com/${source.id}`;
  try {
    const resp = await fetch(rssUrl, {
      headers: { 'User-Agent': 'Mozilla/5.0', 'Accept-Language': 'ko-KR,ko;q=0.9' },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const xml = await resp.text();
    return parseNaverRss(xml);
  } catch (e) {
    console.error(`Blog RSS 실패 [${source.id}]:`, e.message);
    return [];
  }
}

// ── fetchBriefingRaw (메인 export) ────────────────────────────────────

export async function fetchBriefingRaw(env) {
  const today = new Date();
  const kst = new Date(today.getTime() + 9 * 60 * 60 * 1000);
  const todayStr = kst.toISOString().slice(0, 10);

  // 기존 데이터 로드 (30일 유지)
  const existing = await env.KV.get('briefing_raw', { type: 'json' }) || {};

  // 소스별 병렬 fetch
  const telegramTasks = TELEGRAM_SOURCES.map(src =>
    fetchTelegramPosts(src).then(posts => ({ src, posts, type: 'telegram' }))
  );
  const blogTasks = BLOG_SOURCES.map(src =>
    fetchNaverBlogPosts(src).then(posts => ({ src, posts, type: 'blog' }))
  );

  const results = await Promise.all([...telegramTasks, ...blogTasks]);

  const dayData = {
    fetched_at: new Date().toISOString(),
    sources: results.map(({ src, posts, type }) => {
      console.log(`  [${src.name}] ${posts.length}건`);
      return {
        type,
        name: src.name,
        id: src.id,
        category: src.category,
        channel_url: type === 'telegram' ? `https://t.me/${src.id}` : `https://blog.naver.com/${src.id}`,
        posts,
      };
    }),
  };

  existing[todayStr] = dayData;

  // 30일 이상 오래된 데이터 제거
  const sorted = Object.keys(existing).sort().reverse().slice(0, KEEP_DAYS);
  const trimmed = Object.fromEntries(sorted.map(d => [d, existing[d]]));

  const total = results.reduce((s, r) => s + r.posts.length, 0);
  console.log(`브리핑 수집 완료: ${total}건 (${results.length}개 소스)`);

  return trimmed;
}

// ── buildBriefingSummary ──────────────────────────────────────────────

const PERIODS = [
  { key: 'daily',    days: 1,  label: '당일' },
  { key: 'weekly',   days: 7,  label: '최근 7일' },
  { key: 'biweekly', days: 14, label: '최근 14일' },
  { key: 'monthly',  days: 28, label: '최근 28일' },
];

function collectPosts(briefings, anchorDate, days) {
  const anchor = new Date(anchorDate);
  const cutoff = new Date(anchor);
  cutoff.setDate(cutoff.getDate() - days + 1);
  const posts = [];

  for (const [dateStr, dayData] of Object.entries(briefings)) {
    const d = new Date(dateStr);
    if (d < cutoff || d > anchor) continue;
    for (const src of (dayData.sources || [])) {
      for (const post of (src.posts || [])) {
        posts.push({
          date:    post.date || dateStr,
          time:    post.time || '',
          channel: src.name,
          category: src.category || '',
          text:    post.text,
        });
      }
    }
  }
  return posts;
}

function buildPostsText(posts, maxCharsPerPost = 1200) {
  // 채널별 그룹화
  const byChannel = {};
  for (const p of posts) {
    const key = `${p.channel}||${p.category}`;
    if (!byChannel[key]) byChannel[key] = [];
    byChannel[key].push(p);
  }

  const parts = [];
  for (const [key, channelPosts] of Object.entries(byChannel)) {
    const [channel, category] = key.split('||');
    parts.push(`\n${'='.repeat(60)}\n채널: ${channel} (${category})\n${'='.repeat(60)}`);
    for (const p of channelPosts) {
      parts.push(`\n[${p.date} ${p.time}]\n${p.text.slice(0, maxCharsPerPost)}`);
    }
  }
  return parts.join('\n');
}

async function summarizePeriod(posts, period, anchorDate, env) {
  const postsText = buildPostsText(posts);
  if (!postsText.trim()) return null;

  const periodLabel = `${anchorDate} ${period.label}`;
  const summaryInstruction = period.key === 'daily'
    ? '오늘의 시장 종합 요약 (3-5문장, 핵심 이슈와 분위기)'
    : period.key === 'weekly'
    ? '최근 1주일 시장 흐름 종합 요약 (3-5문장, 주요 변화와 흐름)'
    : period.key === 'biweekly'
    ? '최근 2주 시장 흐름 종합 요약 (3-5문장, 중기 트렌드)'
    : '최근 4주 시장 흐름 종합 요약 (3-5문장, 큰 그림과 방향성)';

  const isDaily = period.key === 'daily';
  const multiDayField = isDaily ? '' : '\n      "days_mentioned": 3,';

  const prompt = `다음은 ${periodLabel} 기간에 수집된 투자 채널의 포스트들입니다.
종합 분석하여 아래 JSON 형식으로 응답하세요.

규칙:
- 반드시 유효한 JSON만 출력 (다른 텍스트 없이)
- 모든 텍스트는 한국어
- 여러 채널 중복 언급 종목/테마 강조
- 실제 포스트 내용 기반으로만 작성

JSON 형식:
{
  "date": "${anchorDate}",
  "period": "${periodLabel}",
  "market_summary": "${summaryInstruction}",
  "themes": [
    {
      "title": "테마명",
      "summary": "2-3문장",
      "sentiment": "positive|negative|neutral",
      "related_stocks": ["종목명"],
      "mentioned_in": ["채널명"]${multiDayField}${isDaily ? '' : '\n      "evolution": "시간에 따른 변화 1문장"'}
    }
  ],
  "stocks": [
    {
      "name": "종목명",
      "ticker": "코드 또는 빈문자열",
      "mention_count": 3,
      "channels": ["채널명"],
      "context": "1-2문장",
      "sentiment": "positive|negative|neutral"${multiDayField}
    }
  ],
  "macro": {
    "kr": "한국 시장 요약",
    "us": "미국 시장 요약",
    "global": "글로벌 매크로 요약"
  },
  "key_numbers": ${isDaily ? `[{"label": "지표명", "value": "수치", "change": "변동", "source": "출처"}]` : '[]'}
}

stocks는 mention_count 내림차순. themes는 중요도순.

수집된 포스트:
${postsText.slice(0, 80000)}`;

  try {
    const raw = await callClaude(prompt, env, 4096);
    return JSON.parse(raw);
  } catch (e) {
    console.error(`[${period.key}] 요약 실패:`, e.message);
    return null;
  }
}

export async function buildBriefingSummary(briefingRaw, env) {
  const kst = new Date(new Date().getTime() + 9 * 60 * 60 * 1000);
  const anchorDate = kst.toISOString().slice(0, 10);

  const dates = Object.keys(briefingRaw).sort().reverse();
  const anchor = dates[0] || anchorDate;

  // 4개 기간 병렬 요약
  const results = await Promise.all(
    PERIODS.map(async (period) => {
      const posts = collectPosts(briefingRaw, anchor, period.days);
      if (!posts.length) return [period.key, null];
      console.log(`[briefing:${period.key}] ${posts.length}건 → Claude 요약 중...`);
      const summary = await summarizePeriod(posts, period, anchor, env);
      return [period.key, summary];
    })
  );

  const output = { updated_at: new Date().toISOString() };
  for (const [key, summary] of results) {
    if (summary) output[key] = summary;
  }
  return output;
}
