/**
 * 주가 + 환율 수집
 * Python: fetch_prices.py 포팅
 *
 * stock_map은 KV["stock_map"]에서 읽음
 * → scripts/upload-kv.js 로 업로드
 */

// ── 환율 ──────────────────────────────────────────────────────────────

async function fetchFxRate(pair, fallback, divisor = 1) {
  try {
    const url = `https://api.stock.naver.com/marketindex/exchange/FX_${pair}KRW`;
    const resp = await fetch(url, { headers: { 'User-Agent': 'Mozilla/5.0' } });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const rateStr = (data?.exchangeInfo?.closePrice || data?.closePrice || '0').replace(/,/g, '');
    return parseFloat(rateStr) / divisor || fallback;
  } catch {
    return fallback;
  }
}

async function fetchAllFxRates() {
  const [usd, jpy, cny, hkd] = await Promise.all([
    fetchFxRate('USD', 1400),
    fetchFxRate('JPY', 9.5, 100),  // Naver는 100 JPY 기준
    fetchFxRate('CNY', 200),
    fetchFxRate('HKD', 190),
  ]);
  console.log(`환율: USD ${usd.toFixed(0)} / JPY ${jpy.toFixed(4)} / CNY ${cny.toFixed(2)} / HKD ${hkd.toFixed(2)}`);
  return { USD: usd, JPY: jpy, CNY: cny, HKD: hkd, KRW: 1 };
}

// ── 주가 수집 ─────────────────────────────────────────────────────────

async function fetchNaverKrPrice(code) {
  try {
    const url = `https://m.stock.naver.com/api/stock/${code}/integration`;
    const resp = await fetch(url, { headers: { 'User-Agent': 'Mozilla/5.0' } });
    if (!resp.ok) return null;
    const data = await resp.json();
    const price = data?.dealTrendInfos?.[0]?.closePrice || data?.stockItem?.closePrice;
    return price ? parseInt(price.toString().replace(/,/g, ''), 10) : null;
  } catch {
    return null;
  }
}

async function fetchNaverForeignPrice(code) {
  try {
    const url = `https://api.stock.naver.com/stock/${code}/integration`;
    const resp = await fetch(url, { headers: { 'User-Agent': 'Mozilla/5.0' } });
    if (!resp.ok) return null;
    const data = await resp.json();
    const price = data?.closePrice || data?.stockItem?.closePrice;
    return price ? parseFloat(price.toString().replace(/,/g, '')) : null;
  } catch {
    return null;
  }
}

async function fetchYahooPrice(ticker) {
  try {
    const url = `https://query1.finance.yahoo.com/v8/finance/chart/${ticker}?interval=1d&range=1d`;
    const resp = await fetch(url, { headers: { 'User-Agent': 'Mozilla/5.0' } });
    if (!resp.ok) return null;
    const data = await resp.json();
    return data?.chart?.result?.[0]?.meta?.regularMarketPrice || null;
  } catch {
    return null;
  }
}

const YAHOO_SUFFIX = { JPN: '.T', CHN: '.SS', HKG: '.HK' };
const NATION_CURRENCY = { KOR: 'KRW', USA: 'USD', JPN: 'JPY', CHN: 'CNY', HKG: 'HKD' };

async function fetchStockPrice(name, info) {
  const { code, nation } = info;
  if (!code) return null;

  let price = null;
  if (nation === 'KOR') {
    price = await fetchNaverKrPrice(code);
  } else {
    price = await fetchNaverForeignPrice(code);
    if (!price) {
      const suffix = YAHOO_SUFFIX[nation] || '';
      const ticker = suffix && !code.endsWith(suffix) ? code + suffix : code;
      price = await fetchYahooPrice(ticker);
    }
  }
  return price;
}

// ── fetchPrices (메인 export) ─────────────────────────────────────────

export async function fetchPrices(env) {
  const stockMap = await env.KV.get('stock_map', { type: 'json' }) || {};
  if (!Object.keys(stockMap).length) {
    console.warn('stock_map이 KV에 없음. scripts/upload-kv.js를 먼저 실행하세요.');
    return { _updated_at: new Date().toISOString() };
  }

  const fx = await fetchAllFxRates();

  // 종목별 병렬 fetch (10개씩 청크)
  const entries = Object.entries(stockMap).filter(([, info]) => info.code);
  const CHUNK = 10;
  const prices = { _updated_at: new Date().toISOString(), _fx: fx };

  for (let i = 0; i < entries.length; i += CHUNK) {
    const chunk = entries.slice(i, i + CHUNK);
    const results = await Promise.all(
      chunk.map(async ([name, info]) => {
        const price = await fetchStockPrice(name, info);
        return [name, price, info];
      })
    );
    for (const [name, price, info] of results) {
      if (price) {
        prices[name] = {
          code: info.code,
          price,
          nation: info.nation || 'KOR',
          market: info.market || '',
          currency: NATION_CURRENCY[info.nation] || 'KRW',
        };
      }
    }
  }

  const count = Object.keys(prices).filter(k => !k.startsWith('_')).length;
  console.log(`주가 수집 완료: ${count}/${entries.length}개`);
  return prices;
}
