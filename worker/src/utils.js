/**
 * 공통 유틸리티
 */

// ── HTML → 텍스트 변환 ────────────────────────────────────────────────

export function stripHtml(html) {
  return html
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<[^>]+>/g, '')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, ' ')
    .trim();
}

// ── RFC 2822 날짜 파싱 ("Mon, 13 Apr 2026 12:34:56 +0000") ───────────

export function parseRfc2822Date(dateStr) {
  if (!dateStr) return null;
  try {
    return new Date(dateStr);
  } catch {
    return null;
  }
}

// ── Claude API 호출 ───────────────────────────────────────────────────

export async function callClaude(prompt, env, maxTokens = 4096) {
  const apiKey = env.ANTHROPIC_API_KEY;
  if (!apiKey) throw new Error('ANTHROPIC_API_KEY가 설정되지 않음');

  const resp = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: {
      'x-api-key': apiKey,
      'anthropic-version': '2023-06-01',
      'content-type': 'application/json',
    },
    body: JSON.stringify({
      model: 'claude-sonnet-4-5',
      max_tokens: maxTokens,
      messages: [{ role: 'user', content: prompt }],
    }),
  });

  if (!resp.ok) {
    const body = await resp.text();
    throw new Error(`Claude API 오류 ${resp.status}: ${body.slice(0, 200)}`);
  }

  const data = await resp.json();
  let raw = data.content?.[0]?.text?.trim() || '';

  // 마크다운 코드 블록 제거
  if (raw.startsWith('```')) {
    raw = raw.split('```')[1];
    if (raw.startsWith('json')) raw = raw.slice(4);
    raw = raw.trim();
  }

  return raw;
}
