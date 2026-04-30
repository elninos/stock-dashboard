---
name: stock-analysis
description: Orchestrates Korean stock analysis workflows using project rules, profiles, and cached market data. Use when analyzing a single stock, portfolio, or account and when extracting 상승 원인, 조정 원인, event logs, warning signals, and report outputs.
---

# Stock Analysis Skill

## 목적
한국주식 분석을 표준 절차로 실행한다.
이 스킬은 분석 규칙을 직접 대체하지 않고, CLAUDE.md, ARCHITECTURE.md, CODEBASE.md, profiles/를 조합해 분석 흐름을 실행한다.

## 언제 사용할지
- 단일 종목 분석.
- 보유 종목 일괄 점검.
- 특정 계좌 분석.
- 상승 원인, 조정 원인, 이벤트 로그, 경고 신호를 구조화해야 할 때.
- 분석 결과를 보고서 파일로 저장해야 할 때.

## 입력
- 종목명 또는 종목코드.
- 기준일.
- 분석 범위(단일 / 포트폴리오 / 계좌).
- 데이터 소스 우선순위.
- 필요 시 profiles/{ticker}.md.
- 필요 시 저장할 보고서 유형(상세 / 요약).

## 실행 절차
1. CLAUDE.md의 공통 분석 규칙을 읽고 적용한다.
2. profiles/{ticker}.md가 있으면 함께 읽어 종목별 보정을 반영한다.
3. ARCHITECTURE.md로 구조와 데이터 흐름을 확인한다.
4. CODEBASE.md로 DB/캐시/단위 규칙을 확인한다.
5. 로컬 DB 우선, 부족분만 API 보충 원칙으로 필요한 데이터를 준비한다.
6. 종목을 유형 분류한다.
7. 1M / 6M / 1Y 구조로 분석한다.
8. 조정/하락 이벤트를 추출한다.
9. 유효 상승 원인과 약화/소멸 원인을 분리한다.
10. 경고 Top 5를 도출한다.
11. 보고서가 필요하면 산출물을 저장한다.
12. 마지막에 JSON 요약을 작성한다.

## 출력 형식
- 한 줄 요약.
- 종목 상승 유형 분류.
- 핵심 상승 원인 Top 3.
- 기간 3층 분석.
- 상승 단계 구분.
- 상승 중 조정/하락 이벤트 로그.
- 각 이벤트의 원인 판정.
- 현재 유효한 상승 논리.
- 이미 무너진 논리 또는 약화된 논리.
- 가장 먼저 무너지면 위험한 선행 지표 Top 5.
- 현재 판단.
- 추가 확인이 필요한 데이터.
- JSON 요약.

## 산출물 저장 규칙
- 모든 분석 결과 보고서는 `analysis/reports/` 아래에 저장한다.
- 기본 파일명은 `YYYYMMDD_{name}_{ticker}.md` 형식을 사용한다.
- JSON 부속 파일은 `YYYYMMDD_{name}_{ticker}.json` 형식을 사용한다.
- HTML 뷰어 파일은 `YYYYMMDD_{name}_{ticker}_viewer.html` 형식을 사용한다.
- 종목명이 길거나 특수문자가 있으면 안전한 평문 이름으로 정리한다.
- 요약본과 상세본이 모두 있으면 구분 접미사(`_summary`, `_full`)를 붙인다.
- 동일 일자에 여러 번 생성되면 번호를 추가한다. 예: `20260427_대한광통신_010170_02.md`
- 가능하면 Markdown으로 저장하고, 필요 시 JSON 요약을 별도 부속 파일로 둔다.
- 연월별로 파일이 많아지면 `analysis/reports/YYYY/MM/` 구조로 확장할 수 있다.

## 뷰어 생성 규칙
- 분석 결과를 저장할 때는 Markdown 본문, JSON 부속 파일, viewer.html을 함께 생성한다.
- viewer.html은 placeholder를 런타임에 채우지 않고, 분석 시점에 완성형 정적 HTML로 미리 렌더링한다.
- 최종 조회는 pre-rendered viewer 파일을 사용한다.
- placeholder 기반 HTML은 설계용 템플릿으로만 사용하고, 실제 산출물은 완성형 viewer.html로 저장한다.
- sandboxed iframe 환경을 고려해 런타임 fetch보다 pre-rendered 정적 viewer 생성을 우선한다.

## viewer 생성 절차
1. 분석 본문을 Markdown으로 저장한다.
2. 구조화 결과를 JSON으로 저장한다.
3. Markdown 본문과 JSON 요약을 읽어 viewer.html에 삽입한다.
4. viewer.html은 단독으로 열어도 주요 내용을 모두 볼 수 있어야 한다.
5. Launch preview 또는 정적 파일 열람으로 바로 확인 가능해야 한다.

## 참조 문서
- CLAUDE.md
- ARCHITECTURE.md
- CODEBASE.md
- profiles/{ticker}.md

## 유지 원칙
- 스킬은 실행 오케스트레이션만 담당한다.
- 판정 기준은 CLAUDE.md가 우선한다.
- 구조/역할은 ARCHITECTURE.md가 우선한다.
- 구현 관행과 캐시 규칙은 CODEBASE.md가 우선한다.
- 종목별 예외는 profiles/에만 둔다.
- 산출물 저장 규칙은 CODEBASE.md와 일관되게 유지한다.
