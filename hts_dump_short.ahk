; hts_dump_short.ahk — NH HTS [1320] 공매도 종합 현황 자동 추출
; AutoHotkey v2.0 이상 필요
;
; 기능: 보유 종목별 공매도 거래량/잔고/비중 데이터를 CSV로 Google Drive에 저장
;
; 실행 순서:
;   1. Mac에서 generate_holdings.py → Google Drive/holdings.txt 생성
;   2. Windows에서 NH HTS 실행 및 로그인
;   3. 이 스크립트를 관리자 권한으로 실행
;
; 출력: Google Drive/daily_short/{종목}/{종목}_{YYYYMMDD}.csv

#Requires AutoHotkey v2.0
#SingleInstance Force
SetWorkingDir A_ScriptDir
SendMode "Input"
SetTitleMatchMode 2
CoordMode "Mouse", "Screen"

; =====================================================================
; [설정]
; =====================================================================

global GDRIVE_PATH    := "G:\내 드라이브\01.Claude\01.주식"
global HOLDINGS_FILE  := GDRIVE_PATH . "\holdings.txt"
global SHORT_DIR      := GDRIVE_PATH . "\daily_short"
global LOG_FILE       := A_ScriptDir . "\hts_dump_short.log"
global HTS_TITLE      := "N2 MASTER"
global TEST_MODE      := true   ; true = 첫 종목 1개 테스트
global SCREEN_CODE    := "1320"  ; 공매도 종합 화면 (확인 필요)
global EXCEL_DOWNS    := 21      ; "Excel[*.csv]로 저장" 메뉴 위치 (1502와 동일 추정)
global LOOKBACK_DAYS  := 90      ; 90일치 공매도 이력

; 대기 시간 (ms)
global DELAY_STOCK   := 2500
global DELAY_MENU    := 700
global DELAY_EXPORT  := 3000
global DELAY_FILE    := 1500

global g_wx, g_wy, g_ww, g_wh
global win_short := "공매도"  ; 화면 제목 일부 (실제 제목 확인 필요)

; =====================================================================
; [초기화]
; =====================================================================

DirCreate(SHORT_DIR)

if !FileExist(HOLDINGS_FILE) {
    MsgBox("holdings.txt 파일 없음`n경로: " . HOLDINGS_FILE, "오류", 0x30)
    ExitApp()
}

if !WinExist(HTS_TITLE) {
    MsgBox("NH HTS 창 없음. 로그인 후 재시도", "오류", 0x30)
    ExitApp()
}

LogWrite("=== 공매도 데이터 추출 시작 ===")

; HTS 활성화
WinActivate(HTS_TITLE)
Sleep(500)
WinMaximize(HTS_TITLE)
Sleep(800)

; ── [1320] 화면 열기
LogWrite("화면 [" . SCREEN_CODE . "] 열기")
Send(SCREEN_CODE)
Sleep(300)
Send("{Enter}")
Sleep(2000)

; 화면이 떴는지 확인 (제목에 "공매도" 포함)
if !WinExist(win_short) {
    MsgBox("[" . SCREEN_CODE . "] 공매도 화면이 안 열림.`n수동으로 열어두고 재실행하세요.", "오류", 0x30)
    ExitApp()
}

; 화면 위치 감지
hwnd := WinGetID(win_short)
WinGetPos(&g_wx, &g_wy, &g_ww, &g_wh, "ahk_id " . hwnd)
LogWrite("[" . SCREEN_CODE . "] 위치: x=" . g_wx . " y=" . g_wy . " w=" . g_ww . " h=" . g_wh)

; ── 보유 종목 처리
end_date := FormatTime(A_Now, "yyyyMMdd")
start_ts := DateAdd(A_Now, -LOOKBACK_DAYS, "Days")
start_date := FormatTime(start_ts, "yyyyMMdd")

stock_idx := 0
Loop Read HOLDINGS_FILE
{
    line := Trim(A_LoopReadLine)
    if (line = "" || SubStr(line, 1, 1) = "#")
        Continue

    parts := StrSplit(line, ",")
    if parts.Length < 2
        Continue

    stock_name := Trim(parts[1])
    stock_code := Trim(parts[2])
    if (stock_name = "" || stock_code = "")
        Continue

    stock_idx++
    LogWrite("[" . stock_idx . "] " . stock_name . " (" . stock_code . ")")

    ProcessShort(stock_name, stock_code, start_date, end_date)

    if TEST_MODE {
        MsgBox("첫 종목 테스트 완료. 결과 확인 후 TEST_MODE := false 로 변경.", "테스트 완료", 0x40)
        ExitApp()
    }
    Sleep(1500)
}

LogWrite("=== 공매도 추출 완료: " . stock_idx . "종목 ===")
MsgBox("완료: " . stock_idx . "종목", "공매도 추출", 0x40)
ExitApp()

; =====================================================================
; [함수] ProcessShort — 단일 종목 공매도 데이터 추출
; =====================================================================
ProcessShort(stock_name, stock_code, start_date, end_date) {
    global g_wx, g_wy, win_short, SHORT_DIR
    global DELAY_STOCK, DELAY_MENU, DELAY_EXPORT, DELAY_FILE, EXCEL_DOWNS

    wx := g_wx
    wy := g_wy

    ; ── 좌표 (사용자 확인 필요 — Window Spy로 측정)
    ; [1320] 공매도 화면에서:
    ;   stock_x, stock_y     : 종목 코드 입력 필드
    ;   start_x, start_y     : 조회 시작일 입력 필드
    ;   end_x, end_y         : 조회 종료일 입력 필드
    ;   query_x, query_y     : 조회 버튼 (또는 Enter로 대체)
    ;   right_x, right_y     : 우클릭할 데이터 영역 (그리드 중간)
    stock_x  := wx + 62    ; ⚠️ 사용자 확인: [1502]와 같은지
    stock_y  := wy + 100   ; ⚠️ 화면마다 다름. Window Spy 측정 필요

    start_x  := wx + 200
    start_y  := wy + 100
    end_x    := wx + 320
    end_y    := wy + 100

    right_x  := wx + 453   ; 데이터 영역 우클릭 (1502 기준 — 변경 가능)
    right_y  := wy + 250

    ; ── 1. 창 활성화
    WinActivate(win_short)
    Sleep(400)

    ; ── 2. 종목 코드 입력
    MouseMove(stock_x, stock_y, 20)
    Sleep(300)
    Click(stock_x, stock_y)
    Sleep(300)
    Send("^a")
    Sleep(100)
    Send(stock_code)
    Sleep(300)
    Send("{Enter}")
    Sleep(800)
    LogWrite("종목 입력: " . stock_code)

    ; ── 3. 날짜 범위 입력 (공매도 화면은 보통 기본 기간이 있음)
    ; 만약 직접 입력 필요하면 아래 주석 해제
    ; MouseMove(start_x, start_y, 20)
    ; Click(start_x, start_y)
    ; Sleep(200)
    ; Send(start_date)
    ; Sleep(500)
    ; Send(end_date)
    ; Sleep(300)

    Sleep(DELAY_STOCK)

    ; ── 4. 우클릭 → Excel 저장
    MouseMove(right_x, right_y, 25)
    Sleep(600)
    Click("Right", right_x, right_y)
    Sleep(DELAY_MENU)

    LogWrite("우클릭: (" . right_x . ", " . right_y . ")")

    ; Down × 21 + Enter
    Loop EXCEL_DOWNS {
        Send("{Down}")
        Sleep(80)
    }
    Send("{Enter}")
    Sleep(DELAY_EXPORT)

    ; ── 5. 저장 다이얼로그
    if !WinWait("다른 이름으로 저장", , 6) {
        LogWrite("[WARN] " . stock_name . " 저장 다이얼로그 미발생")
        Send("{Escape}")
        return
    }

    ; 폴더 생성
    safe_name := SanitizeFilename(stock_name)
    stock_dir := SHORT_DIR . "\" . safe_name
    DirCreate(stock_dir)

    outfile := stock_dir . "\" . safe_name . "_" . end_date . ".csv"

    WinActivate("다른 이름으로 저장")
    Sleep(300)
    try ControlFocus("Edit1", "다른 이름으로 저장")
    Sleep(200)
    try ControlSetText(outfile, "Edit1", "다른 이름으로 저장")
    Sleep(300)
    Send("{Enter}")
    Sleep(DELAY_FILE)

    ; "이미 존재" 다이얼로그 처리
    if WinExist("확인")
        Send("{Enter}")
    if WinExist("Microsoft Excel")
        Send("{Enter}")
    Sleep(500)

    LogWrite("저장: " . outfile)
}

; =====================================================================
; [헬퍼 함수]
; =====================================================================
SanitizeFilename(name) {
    for ch in ["\", "/", ":", "*", "?", "`"", "<", ">", "|"]
        name := StrReplace(name, ch, "_")
    return Trim(name, " .")
}

LogWrite(msg) {
    global LOG_FILE
    ts := FormatTime(A_Now, "yyyy-MM-dd HH:mm:ss")
    FileAppend(ts . " | " . msg . "`n", LOG_FILE)
}

; =====================================================================
; [핫키] Esc — 중단
; =====================================================================
Esc:: {
    if MsgBox("중단하시겠습니까?", "공매도 추출", 0x24) = "Yes"
        ExitApp()
}
