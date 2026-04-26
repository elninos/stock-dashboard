# Windows PC 세팅 가이드

> **목적**: 윈도우 PC를 24/7 AI 요약 머신으로 설정  
> **비용**: 0원 추가 (Claude Max 구독 포함)  
> **작업**: 하루 4회 AI 요약 실행 → GitHub → 자동 배포

---

## 전체 흐름

```
Task Scheduler (08:00 / 12:00 / 16:00 / 20:00 KST)
  └─ run_daily.bat
       └─ run_daily.py
            ├─ 1. git pull          ← GitHub Actions이 올린 최신 briefing.json 받아오기
            ├─ 2. summarize_briefing_claude.py   ← Claude AI 요약
            ├─ 3. summarize_stock_news_claude.py ← Claude AI 뉴스 요약
            └─ 4. git push          → GitHub Actions deploy.yml 트리거 → 자동 배포
```

> **참고**: 데이터 수집(텔레그램/블로그/DART)은 GitHub Actions가 2시간마다 자동 실행.  
> 윈도우 PC는 AI 요약만 담당.

---

## Step 1: 필수 프로그램 설치

### Python 3.11+
```
https://www.python.org/downloads/
```
- 설치 시 **"Add Python to PATH"** 반드시 체크
- 설치 확인: `python --version`

### Git
```
https://git-scm.com/download/win
```
- 기본 옵션으로 설치
- 설치 확인: `git --version`

### Claude Code CLI
```
https://claude.ai/download
```
- 설치 후 **Max 구독 계정으로 로그인**
- 설치 확인: `claude --version`
- 동작 확인: `claude --print "안녕"` → 응답 나오면 OK

---

## Step 2: 저장소 클론

PowerShell 또는 명령 프롬프트에서:

```powershell
cd C:\Users\%USERNAME%\Documents
git clone https://github.com/elninos/stock-dashboard.git
cd stock-dashboard
```

### Git 사용자 설정 (처음 한 번만)
```powershell
git config --global user.name "Sangrok"
git config --global user.email "srshin614@gmail.com"
```

### GitHub 인증 설정

**방법 A: GitHub Desktop** (가장 간단, 추천)
```
https://desktop.github.com/
```
GitHub Desktop에서 로그인하면 git push 자동 인증.

**방법 B: Personal Access Token**
1. GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)
2. `repo` 권한으로 토큰 생성
3. git push 시 비밀번호 대신 토큰 입력

---

## Step 3: run_daily.bat 경로 수정

`windows\run_daily.bat` 파일을 메모장으로 열고:

```batch
REM ── 이 줄만 실제 경로로 수정 ──
set REPO_DIR=C:\Users\%USERNAME%\Documents\stock-dashboard
```

> `%USERNAME%`은 자동으로 현재 사용자명으로 치환됩니다.

---

## Step 4: 수동 테스트 (Task Scheduler 등록 전 반드시)

```powershell
cd C:\Users\%USERNAME%\Documents\stock-dashboard
python windows\run_daily.py
```

정상 실행 시 출력 예시:
```
[2026-04-26 12:00:00] ============================================================
[2026-04-26 12:00:00] Windows 일일 브리핑 시작
[2026-04-26 12:00:00] [1/4] git pull
[2026-04-26 12:00:00] [2/4] summarize_briefing_claude.py
[2026-04-26 12:05:00] Push 완료 → GitHub Actions 배포 트리거됨
[2026-04-26 12:05:00] ✓ 완료
```

---

## Step 5: Task Scheduler 등록

**PowerShell을 관리자 권한으로 실행** 후:

```powershell
$repo = "C:\Users\$env:USERNAME\Documents\stock-dashboard"
$bat  = "$repo\windows\run_daily.bat"

$action   = New-ScheduledTaskAction -Execute $bat -WorkingDirectory $repo
$trigger  = New-ScheduledTaskTrigger -Daily -At "08:00"
$settings = New-ScheduledTaskSettingsSet `
    -RunOnlyIfNetworkAvailable `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

$trigger.RepetitionInterval = New-TimeSpan -Hours 4
$trigger.RepetitionDuration = [TimeSpan]::MaxValue

Register-ScheduledTask `
    -TaskName  "StockDashboardBriefing" `
    -Action    $action `
    -Trigger   $trigger `
    -Settings  $settings `
    -RunLevel  Highest `
    -Force
```

등록 후 즉시 테스트:
```powershell
Start-ScheduledTask -TaskName "StockDashboardBriefing"
```

---

## 로그 확인

```powershell
Get-Content "$env:USERPROFILE\Documents\stock-dashboard\windows\run_daily.log" -Tail 50 -Wait
```

---

## 트러블슈팅

### `claude` 명령어를 찾을 수 없음
```powershell
where.exe claude
# 경로 확인 후 run_daily.py의 call_claude_cli()에서 전체 경로로 수정
```

### git push 실패 (인증 오류)
GitHub Desktop 설치 후 로그인하면 자동 해결.

### Task Scheduler에서 실행 안 됨
작업 속성에서 확인:
- "로그온 여부에 관계없이 실행" 체크
- "최고 수준의 권한으로 실행" 체크

### 요약 JSON 파싱 오류
`windows\run_daily.log`에서 `[ERROR]` 라인 확인.  
`claude --print "테스트"` 로 Claude Code 동작 먼저 확인.
