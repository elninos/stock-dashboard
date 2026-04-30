#!/usr/bin/env node
/**
 * KV 데이터 업로드 스크립트
 *
 * 로컬 파일에서 stock_map과 stock_list를 읽어 Cloudflare KV에 업로드합니다.
 *
 * 사용법:
 *   node scripts/upload-kv.js [--preview]
 *
 * 사전 조건:
 *   - wrangler login 완료
 *   - wrangler.toml에 KV namespace ID 설정
 *   - 프로젝트 루트에 stock_map.json, transactions.json 존재
 *
 * --preview 플래그: preview 환경에 업로드 (개발용)
 */

import { readFileSync, writeFileSync } from 'fs';
import { execSync } from 'child_process';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));

// 경로 설정
const WORKER_DIR  = resolve(__dirname, '..');          // worker/
const PROJECT_DIR = resolve(WORKER_DIR, '..');         // 프로젝트 루트
const STOCK_MAP_PATH    = resolve(PROJECT_DIR, 'stock_map.json');
const TRANSACTIONS_PATH = resolve(PROJECT_DIR, 'transactions.json');

const isPreview = process.argv.includes('--preview');
const envFlag   = isPreview ? '--preview' : '';

// ── 파일 읽기 ─────────────────────────────────────────────────────────

function loadJson(path) {
  try {
    return JSON.parse(readFileSync(path, 'utf8'));
  } catch (e) {
    console.error(`파일 읽기 실패: ${path}\n${e.message}`);
    process.exit(1);
  }
}

// ── stock_list 계산 (현재 보유 종목) ─────────────────────────────────

function deriveStockList(transactions, stockMap) {
  // 종목별 누적 수량 계산
  const qty = {};
  for (const tx of transactions) {
    if (!tx.stock) continue;
    const name = tx.stock;
    if (!qty[name]) qty[name] = 0;
    if (tx.type === 'buy')  qty[name] += (tx.qty || 0);
    if (tx.type === 'sell') qty[name] -= (tx.qty || 0);
  }

  // 수량 > 0이면서 stock_map에 있는 종목만 포함
  const held = Object.entries(qty)
    .filter(([name, q]) => q > 0 && stockMap[name])
    .map(([name]) => name)
    .sort();

  // stock_map에만 있는 종목 (수동 등록, 보유 내역 없음) 알림
  const fromMapOnly = Object.keys(stockMap).filter(name => !held.includes(name));
  if (fromMapOnly.length) {
    console.log(`  ※ stock_map에만 있는 종목 (보유 내역 없음): ${fromMapOnly.join(', ')}`);
  }

  return held;
}

// ── KV namespace ID 읽기 ──────────────────────────────────────────────

function getNamespaceId() {
  const toml = readFileSync(resolve(WORKER_DIR, 'wrangler.toml'), 'utf8');
  const match = isPreview
    ? toml.match(/preview_id\s*=\s*"([^"]+)"/)
    : toml.match(/(?<!preview_)id\s*=\s*"([^"]+)"/);
  if (!match) {
    console.error('wrangler.toml에서 KV namespace id를 찾을 수 없습니다.');
    process.exit(1);
  }
  return match[1];
}

// ── wrangler kv key put 실행 ──────────────────────────────────────────

function kvPut(key, value, namespaceId) {
  const json = JSON.stringify(value);
  const tmpFile = `/tmp/kv_upload_${key}.json`;
  writeFileSync(tmpFile, json);

  const cmd = `/Users/r/bin/wrangler kv key put "${key}" --namespace-id "${namespaceId}" --path "${tmpFile}"`;
  try {
    execSync(cmd, { cwd: WORKER_DIR, stdio: 'inherit' });
  } catch {
    console.error(`KV 업로드 실패: ${key}`);
    process.exit(1);
  }
}

// ── 메인 ─────────────────────────────────────────────────────────────

const stockMap     = loadJson(STOCK_MAP_PATH);
const transactions = loadJson(TRANSACTIONS_PATH);

console.log(`=== KV 업로드 시작 (${isPreview ? 'preview' : 'production'}) ===\n`);
console.log(`stock_map: ${Object.keys(stockMap).length}개 종목`);
console.log(`transactions: ${transactions.length}건\n`);

const stockList = deriveStockList(transactions, stockMap);
console.log(`stock_list (보유 종목): ${stockList.length}개`);
console.log(`  ${stockList.join(', ')}\n`);

const namespaceId = getNamespaceId();
console.log(`namespace-id: ${namespaceId}\n`);

console.log('[1/2] stock_map 업로드 중...');
kvPut('stock_map', stockMap, namespaceId);

console.log('\n[2/2] stock_list 업로드 중...');
kvPut('stock_list', stockList, namespaceId);

console.log('\n✅ 업로드 완료');
console.log('수동 실행: curl -X POST https://<worker-url>/api/trigger');
