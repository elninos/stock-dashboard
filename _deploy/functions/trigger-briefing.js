/**
 * Cloudflare Pages Function: POST /trigger-briefing
 * GitHub Actions workflow_dispatch를 트리거합니다.
 * 환경변수 GITHUB_TOKEN 필요 (Cloudflare Pages → Settings → Environment Variables)
 */

const REPO  = 'elninos/stock-dashboard';
const WORKFLOW = 'update-briefing.yml';
const BRANCH   = 'main';

const CORS = {
  'Access-Control-Allow-Origin':  '*',
  'Access-Control-Allow-Methods': 'POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
};

export async function onRequestOptions() {
  return new Response(null, { status: 204, headers: CORS });
}

export async function onRequestPost({ env }) {
  const token = env.GITHUB_TOKEN;
  if (!token) {
    return new Response(
      JSON.stringify({ ok: false, error: 'GITHUB_TOKEN not configured' }),
      { status: 500, headers: { 'Content-Type': 'application/json', ...CORS } }
    );
  }

  const resp = await fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
    {
      method: 'POST',
      headers: {
        Authorization:          `Bearer ${token}`,
        Accept:                 'application/vnd.github+json',
        'Content-Type':         'application/json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent':           'stock-dashboard-cf-function',
      },
      body: JSON.stringify({ ref: BRANCH }),
    }
  );

  if (resp.status === 204) {
    return new Response(
      JSON.stringify({ ok: true }),
      { status: 200, headers: { 'Content-Type': 'application/json', ...CORS } }
    );
  }

  const body = await resp.text();
  return new Response(
    JSON.stringify({ ok: false, error: body, status: resp.status }),
    { status: resp.status, headers: { 'Content-Type': 'application/json', ...CORS } }
  );
}
