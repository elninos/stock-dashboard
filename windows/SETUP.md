# Windows PC 설정 가이드

## 개요

Windows PC (Claude Max 구독)에서 브리핑 AI 요약을 실행합니다.
- **비용**: 0원 추가 (Claude Max 포함)
- **실행 시간**: 하루 4회 (08:00, 12:00, 16:00, 20:00 KST)
- **결과**: `briefing_summary.json` git push → GitHub Actions 자동 배포

---

## 사전 준비

### 1. Python 설치 확인
```cmd
python --version   # 3.10 이상 필요
```

### 2. Git 설정
```cmd
git config --global user.name "Your Name"
git config --global user.email "your@email.com"
```

### 3. 저장소 클론 (이미 있으면 스킵)
```cmd
git clone https://github.com/YOUR_USER/stock-dashboard.git
cd stock-dashboard
```

### 4. Python 패키지 설치
```cmd
pip install anthropic  # summarize_briefing.py 호환용 (실제로는 사용 안 함)
```
> 참고: `summarize_briefing_claude.py`는 `anthropic` 패키지를 직접 사용하지 않음.
> claude CLI subprocess를 호출하므로 Python 표준 라이브러리만 필요.

### 5. Claude Code 설치 확인
```cmd
claude --version
```
> Claude Code가 설치되지 않은 경우: https://claude.ai/download

### 6. Claude 로그인 확인
```cmd
claude --print "hello"
```
> Max 구독 계정으로 로그인되어 있어야 함.

---

## Task Scheduler 설정

### 방법 1: 배치 파일 직접 설정

1. **Win + R** → `taskschd.msc` 실행
2. **작업 만들기** 클릭
3. **일반** 탭:
   - 이름: `Stock Dashboard Briefing`
   - 로그온 여부에 관계없이 실행: ✓
4. **트리거** 탭 → 새로 만들기:
   - 매일 반복: 08:00
   - 반복 간격: 4시간, 기간: 무기한
5. **동작** 탭 → 새로 만들기:
   - 프로그램: `C:\path\to\stock-dashboard\windows\run_daily.bat`
6. **조건** 탭:
   - 네트워크 연결이 있을 때만 시작: ✓
7. **확인**

### 방법 2: PowerShell로 자동 등록

```powershell
# PowerShell을 관리자 권한으로 실행 후:
$Action = New-ScheduledTaskAction `
    -Execute "C:\path\to\stock-dashboard\windows\run_daily.bat"
$Trigger = New-ScheduledTaskTrigger `
    -Daily -At "08:00" -RepetitionInterval (New-TimeSpan -Hours 4) `
    -RepetitionDuration ([TimeSpan]::MaxValue)
$Settings = New-ScheduledTaskSettingsSet `
    -RunOnlyIfNetworkAvailable -StartWhenAvailable
Register-ScheduledTask `
    -TaskName "StockDashboardBriefing" `
    -Action $Action -Trigger $Trigger -Settings $Settings `
    -RunLevel Highest -Force
```

---

## run_daily.bat 수정 필요 사항

`windows/run_daily.bat` 파일을 열어 실제 경로로 수정:

```batch
REM 이 줄을 실제 경로로 변경:
set REPO_DIR=C:\Users\YourName\stock-dashboard
```

---

## 동작 흐름

```
Task Scheduler (08:00, 12:00, 16:00, 20:00 KST)
  └─ run_daily.bat
       └─ run_daily.py
            ├─ git pull               (최신 briefing.json 동기화)
            ├─ fetch_briefing.py      (텔레그램/블로그 수집)
            ├─ summarize_briefing_claude.py  (Claude Code CLI 요약)
            ├─ fetch_stock_news.py    (뉴스 수집)
            ├─ summarize_stock_news_claude.py (Claude Code CLI 요약)
            └─ git push               → GitHub Actions 빌드+배포 트리거
```

---

## 로그 확인

```cmd
type C:\path\to\stock-dashboard\windows\run_daily.log
```

또는 PowerShell:
```powershell
Get-Content "C:\path\to\stock-dashboard\windows\run_daily.log" -Tail 50
```

---

## 수동 테스트

```cmd
cd C:\path\to\stock-dashboard
python windows\run_daily.py
```

---

## 트러블슈팅

**`claude` 명령어를 찾을 수 없음:**
- Claude Code PATH 확인: `where claude`
- 또는 run_daily.py에서 전체 경로 지정:
  ```python
  ["C:\\Users\\YourName\\AppData\\Local\\AnthropicClaude\\claude.exe", "--print", ...]
  ```

**`git push` 실패 (권한 오류):**
- SSH 키 또는 GitHub 자격증명 설정 필요
- GitHub Desktop 설치 후 로그인하면 자동 처리됨

**요약이 빈 파일로 저장:**
- `claude --print "테스트"` 로 Claude Code 동작 확인
- `windows\run_daily.log` 에서 에러 메시지 확인
