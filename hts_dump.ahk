; hts_dump.ahk  —  NH 나무HTS [1503] 거래원 기간별분석 자동 내보내기
; AutoHotkey v2.0 이상 필요
;
; 기능: 보유 KOR 종목별로 48개 거래원 일별 데이터를 xlsx로 Google Drive에 저장
;
; 실행 순서:
;   1. Mac에서 python3 generate_holdings.py → Google Drive/holdings.txt 생성
;   2. Windows에서 NH 나무HTS 실행 및 로그인, [1503] 화면 열기
;   3. 이 스크립트를 관리자 권한으로 실행

#Requires AutoHotkey v2.0
#SingleInstance Force
SetWorkingDir A_ScriptDir
SendMode "Input"
SetTitleMatchMode 2
CoordMode "Mouse", "Screen"

; =====================================================================
; [설정]
; =====================================================================

global GDRIVE_PATH     := "G:\내 드라이브\01.Claude\01.주식"
global HOLDINGS_FILE   := GDRIVE_PATH . "\holdings.txt"
global BROKER_FLOW_DIR := GDRIVE_PATH . "\broker_flow"
global LOG_FILE        := A_ScriptDir . "\hts_dump.log"
global HTS_TITLE       := "N2 MASTER"
global TEST_MODE       := true   ; true = 첫 종목 1개, 거래원 2개
global STEP_MODE       := false  ; 대화상자 없이 자동 진행 (문제 시 true로 변경)
global LOOKBACK_DAYS   := 365
global DATE_INPUT_FMT  := "yyyy-MM-dd"
global EXCEL_MENU_DOWNS := 21    ; "Excel[*.csv]로 저장" = 22번째 항목 → Down 21번
global BROKER_COUNT    := 48

; 대기 시간 (ms)
global DELAY_STOCK    := 3000
global DELAY_BROKER   := 2000
global DELAY_MENU     := 700
global DELAY_EXPORT   := 3000
global DELAY_FILESAVE := 1500

; 창 위치 전역 (시작 시 WinGetPos로 설정)
global g_wx, g_wy, g_ww, g_wh
global win1503 := "거래원 기간별분석"

; =====================================================================
; [초기화 및 메인]
; =====================================================================

DirCreate(BROKER_FLOW_DIR)

if !FileExist(HOLDINGS_FILE) {
    MsgBox(
        "holdings.txt 파일이 없습니다.`n`n"
        . "경로: " . HOLDINGS_FILE . "`n`n"
        . "【해결】 Mac에서 다음을 실행하세요:`n"
        . "  python3 generate_holdings.py",
        "hts_dump — 오류", 0x30)
    ExitApp()
}

if !WinExist(HTS_TITLE) {
    MsgBox(
        "NH 나무HTS 창을 찾을 수 없습니다.`n"
        . "설정된 창 제목: `"" . HTS_TITLE . "`"`n`n"
        . "HTS를 실행 및 로그인 후 다시 시도하세요.",
        "hts_dump — 오류", 0x30)
    ExitApp()
}

; 날짜 계산
end_date_file   := FormatTime(A_Now, "yyyyMMdd")
end_date_disp   := FormatTime(A_Now, DATE_INPUT_FMT)
start_ts        := DateAdd(A_Now, -LOOKBACK_DAYS, "Days")
start_date_file := FormatTime(start_ts, "yyyyMMdd")
start_date_disp := FormatTime(start_ts, DATE_INPUT_FMT)

LogWrite("=== hts_dump 시작 ===")
LogWrite("기간: " . start_date_file . " ~ " . end_date_file)
if TEST_MODE
    LogWrite("※ TEST_MODE: 첫 종목 1개, 거래원 2개")
if STEP_MODE
    LogWrite("※ STEP_MODE: 단계별 확인 활성화")

WinActivate(HTS_TITLE)
Sleep(500)

; 7777 인트로 팝업 자동 닫기
Loop 5 {
    if WinExist("7777") {
        WinClose("7777")
        Sleep(500)
    }
}

; [1503] 창 위치 감지 및 최대화
if !WinExist(win1503) {
    MsgBox("[1503] 거래원 기간별분석 화면을 열어주세요.", "hts_dump", 0x30)
    ExitApp()
}

; N2 MASTER (부모 창) 최대화
WinMaximize(HTS_TITLE)
Sleep(800)

; [1503] □ 최대화 버튼 직접 클릭 (스크린샷에서 버튼 위치 확인)
WinActivate(win1503)
Sleep(400)
hwnd1503 := WinGetID(win1503)
WinGetPos(&g_wx, &g_wy, &g_ww, &g_wh, "ahk_id " . hwnd1503)
LogWrite("[1503] WinMaximize 후: x=" . g_wx . " y=" . g_wy . " w=" . g_ww . " h=" . g_wh)

; WinMaximize가 실패한 경우(w<1500)에만 □ 버튼 클릭
; 이미 최대화(w≥1500)면 버튼 좌표(wx+ww-30)가 화면 우상단 → N2 MASTER X버튼 충돌
if (g_ww < 1500) {
    maxBtnX := g_wx + g_ww - 30
    maxBtnY := g_wy + 11
    LogWrite("최대화 버튼 클릭: (" . maxBtnX . ", " . maxBtnY . ")")
    Click(maxBtnX, maxBtnY)
    Sleep(1000)
    WinGetPos(&g_wx, &g_wy, &g_ww, &g_wh, "ahk_id " . hwnd1503)
    LogWrite("[1503] 버튼 클릭 후: x=" . g_wx . " y=" . g_wy . " w=" . g_ww . " h=" . g_wh)
} else {
    LogWrite("[1503] 이미 최대화 (w=" . g_ww . ") — 버튼 클릭 생략")
}

; holdings.txt 읽기 및 처리
stock_idx := 0
Loop Read HOLDINGS_FILE
{
    line := Trim(A_LoopReadLine)
    if (line = "" || SubStr(line, 1, 1) = "#")
        Continue

    parts := StrSplit(line, ",")
    if (parts.Length < 2)
        Continue

    stock_name := Trim(parts[1])
    stock_code := Trim(parts[2])
    if (stock_name = "" || stock_code = "")
        Continue

    stock_idx++
    LogWrite("[종목 " . stock_idx . "] " . stock_name . " (" . stock_code . ")")

    ProcessStock(stock_name, stock_code, start_date_disp, end_date_disp, start_date_file, end_date_file)

    if TEST_MODE {
        MsgBox(
            "첫 종목 테스트 완료.`n`n"
            . "결과 확인 후 TEST_MODE := false, STEP_MODE := false 로 변경하세요.",
            "TEST_MODE 완료", 0x40)
        ExitApp()
    }
    Sleep(2000)
}

LogWrite("=== hts_dump 완료: " . stock_idx . "개 종목 ===")
MsgBox("수집 완료! " . stock_idx . "개 종목`n`n출력: " . BROKER_FLOW_DIR, "hts_dump 완료", 0x40)
ExitApp()

; =====================================================================
; [함수] ProcessStock — 1개 종목 처리
; =====================================================================
ProcessStock(stock_name, stock_code, start_disp, end_disp, start_file, end_file) {
    global g_wx, g_wy, win1503
    global BROKER_COUNT, BROKER_FLOW_DIR, TEST_MODE, STEP_MODE
    global DELAY_STOCK, DELAY_BROKER, DELAY_MENU, DELAY_EXPORT, DELAY_FILESAVE
    global EXCEL_MENU_DOWNS

    wx := g_wx   ; e.g. 2
    wy := g_wy   ; e.g. 106

    ; ── 좌표 (Window Spy 실측, 최대화 wx=-3 wy=-3 기준, offset = abs+3)
    stock_x  := wx + 62    ; abs=59   종목 코드 입력
    stock_y  := wy + 143   ; abs=140
    chkbox_x := wx + 546   ; abs=543  조회기간 체크박스 (날짜 입력 활성화)
    chkbox_y := wy + 169   ; abs=166
    start_x  := wx + 621   ; abs=618  조회기간 시작일 (yyyy 첫 필드)
    start_y  := wy + 162   ; abs=159
    end_x    := wx + 731   ; abs=728  조회기간 종료일 (yyyy 첫 필드)
    end_y    := wy + 167   ; abs=164
    query_x  := wx + 295   ; abs≈292  조회 버튼 (추정 — STEP_MODE에서 확인)
    query_y  := wy + 143   ; 종목과 같은 행
    broker_x := wx + 78    ; abs=75   거래원 목록 (좌측 패널)
    broker_y := wy + 215   ; abs=212  첫 번째 거래원
    row_h    := 17         ; 행 높이 (abs: 212→229 = 17px 확인)
    right_x  := wx + 453   ; abs≈450  오른쪽 패널 데이터 영역
    right_y  := wy + 208   ; abs≈205

    ; ── 1. 창 활성화
    WinActivate(win1503)
    Sleep(400)

    ; ── 2. 종목 코드 입력
    MouseMove(stock_x, stock_y, 25)
    Sleep(500)
    Click(stock_x, stock_y)
    Sleep(300)
    Send("^a")
    Sleep(100)
    Send(stock_code)
    Sleep(300)

    LogWrite("종목 코드 클릭: (" . stock_x . ", " . stock_y . ") → " . stock_code)

    ; Enter로 종목 확정 — MsgBox 전에 실행해야 HTS에 전달됨
    Send("{Enter}")
    Sleep(800)

    if STEP_MODE {
        MsgBox(
            "【STEP 1/5】 종목 코드 입력`n`n"
            . "좌표: (" . stock_x . ", " . stock_y . ")`n"
            . "입력값: " . stock_code . "`n`n"
            . "▶ 종목 필드에 " . stock_code . " 이(가) 입력됐나요?`n`n"
            . "OK",
            "STEP 1/5  [" . stock_name . "]", 0x40)
    }

    ; ── 3. 조회기간 체크박스 클릭 (날짜 직접 입력 활성화)
    WinActivate(win1503)
    Sleep(500)
    MouseMove(chkbox_x, chkbox_y, 20)
    Sleep(300)
    Click(chkbox_x, chkbox_y)
    Sleep(400)
    LogWrite("조회기간 체크박스 클릭: (" . chkbox_x . ", " . chkbox_y . ")")

    ; ── 4. 날짜 입력 — yyyyMMdd 8자리 입력 시 종료일로 자동 이동
    MouseMove(start_x, start_y, 20)
    Sleep(300)
    Click(start_x, start_y)
    Sleep(300)
    Send(start_file)   ; e.g. "20250425" → 자동으로 종료일 필드로 이동
    Sleep(500)
    Send(end_file)     ; e.g. "20260425"
    Sleep(300)

    LogWrite("날짜 입력: " . start_file . " ~ " . end_file)

    if STEP_MODE {
        MsgBox(
            "【STEP 2/5】 체크박스 + 날짜 입력`n`n"
            . "체크박스: (" . chkbox_x . ", " . chkbox_y . ")`n"
            . "시작일: (" . start_x . ", " . start_y . ") → " . start_disp . "`n"
            . "종료일: (" . end_x . ", " . end_y . ") → " . end_disp . "`n`n"
            . "▶ 날짜 필드가 올바르게 채워졌나요?`n`n"
            . "OK",
            "STEP 2/5  [" . stock_name . "]", 0x40)
    }

    ; 날짜 입력 완료 후 자동 조회됨 — 버튼 클릭 불필요
    Sleep(DELAY_STOCK)

    ; ── 5. 거래원 이름 읽기 시도 (실패해도 계속)
    broker_names := TryLoadBrokerNames()

    ; ── 6. 첫 번째 거래원 클릭
    MouseMove(broker_x, broker_y, 20)
    Sleep(300)
    Click(broker_x, broker_y)
    Sleep(DELAY_BROKER)

    LogWrite("첫 거래원 클릭: (" . broker_x . ", " . broker_y . ")")

    if STEP_MODE {
        MsgBox(
            "【STEP 4/5】 첫 번째 거래원 클릭`n`n"
            . "좌표: (" . broker_x . ", " . broker_y . ")`n`n"
            . "▶ 왼쪽 패널에서 첫 번째 항목이 선택됐나요?`n"
            . "▶ 오른쪽 패널 데이터가 바뀌었나요?`n`n"
            . "OK",
            "STEP 4/5  [" . stock_name . "]", 0x40)
    }

    max_brokers := TEST_MODE ? 2 : BROKER_COUNT

    Loop max_brokers {
        broker_idx := A_Index

        ; 2번째부터 Down 한 번씩 — O(n), 매번 row1 재클릭 불필요
        if broker_idx > 1 {
            Send("{Down}")
            Sleep(DELAY_BROKER)
        }

        if broker_names.Has(broker_idx) && broker_names[broker_idx] != ""
            cur_broker := broker_names[broker_idx]
        else
            cur_broker := (broker_idx < 10) ? "거래원0" . broker_idx : "거래원" . broker_idx

        ; ── 7. 오른쪽 패널 우클릭
        MouseMove(right_x, right_y, 25)
        Sleep(1200)
        Click("Right", right_x, right_y)
        Sleep(DELAY_MENU)

        LogWrite("우클릭: (" . right_x . ", " . right_y . ") — " . cur_broker)

        ; STEP_MODE: 첫 번째 거래원에서만 확인
        if STEP_MODE && broker_idx = 1 {
            result := MsgBox(
                "【STEP 5/5】 오른쪽 패널 우클릭`n`n"
                . "좌표: (" . right_x . ", " . right_y . ")`n`n"
                . "▶ 컨텍스트 메뉴가 나타났나요?`n"
                . "  나타났으면: 메뉴 항목들을 기억해 두세요`n"
                . "  안 나타났으면: 아니오 선택`n`n"
                . "예=메뉴 보임  아니오=메뉴 없음/다른 위치",
                "STEP 5/5  [" . stock_name . "]", 0x23)

            if result = "No" {
                Send("{Escape}")
                LogWrite("[DEBUG] STEP_MODE: 우클릭 메뉴 미확인 → 좌표 조정 필요")
                LogWrite("[DEBUG] 현재 right_x=" . right_x . " right_y=" . right_y)
                MsgBox(
                    "우클릭 메뉴가 나타나지 않았습니다.`n`n"
                    . "현재 좌표: (" . right_x . ", " . right_y . ")`n"
                    . "[1503] 창: x=" . wx . " y=" . wy . " w=" . g_ww . " h=" . g_wh . "`n`n"
                    . "hts_dump.ahk의 right_x/right_y 오프셋을 조정하고 다시 실행하세요.",
                    "좌표 수정 필요", 0x30)
                return
            }
        }

        ; ── 8. Excel 메뉴 선택
        if !SelectExcelMenu(right_x, right_y, EXCEL_MENU_DOWNS) {
            LogWrite("[SKIP] " . stock_name . " " . cur_broker . " — Excel 메뉴 미발견")
            Send("{Escape}")
            Continue
        }
        Sleep(DELAY_EXPORT)

        ; ── 9. 저장 다이얼로그 처리
        if !WinWait("다른 이름으로 저장", , 6) {
            LogWrite("[WARN] " . stock_name . " " . cur_broker . " — 저장 다이얼로그 없음")
            Continue
        }

        safe_broker := SanitizeFilename(cur_broker)
        outfile := BROKER_FLOW_DIR . "\" . stock_name . "_" . safe_broker
                 . "_" . start_file . "_" . end_file . ".csv"

        WinActivate("다른 이름으로 저장")
        Sleep(300)
        try ControlFocus("Edit1", "다른 이름으로 저장")
        Sleep(200)
        try ControlSetText(outfile, "Edit1", "다른 이름으로 저장")
        Sleep(300)
        Send("{Enter}")
        Sleep(DELAY_FILESAVE)

        if WinExist("확인")
            Send("{Enter}")
        if WinExist("Microsoft Excel")
            Send("{Enter}")
        Sleep(500)

        LogWrite("저장: " . outfile)
    }

    LogWrite("완료: " . stock_name . " (" . max_brokers . "/" . BROKER_COUNT . "개)")
}

; =====================================================================
; [함수] TryLoadBrokerNames — ListView에서 거래원명 읽기
; =====================================================================
TryLoadBrokerNames() {
    global HTS_TITLE, BROKER_COUNT
    names := Map()

    Loop 5 {
        ctrl := "SysListView3" . A_Index
        try {
            items := ListViewGetContent("", ctrl, HTS_TITLE)
            if items = ""
                Continue

            row := 0
            for line in StrSplit(items, "`n", "`r") {
                row++
                if row > BROKER_COUNT
                    break
                cols := StrSplit(line, "`t")
                candidate := (cols.Length >= 2 && Trim(cols[2]) != "")
                           ? Trim(cols[2]) : Trim(cols[1])
                if candidate != ""
                    names[row] := candidate
            }
            if names.Count > 0 {
                LogWrite("거래원명 로드 완료 (" . ctrl . ", " . names.Count . "개)")
                return names
            }
        }
    }

    LogWrite("거래원명 로드 실패 — 인덱스 번호로 저장")
    return names
}

; =====================================================================
; [함수] SelectExcelMenu — 우클릭 후 "Excel[*.csv]로 저장" 선택
; 메뉴 구조: 22번째 항목 → Down 21번 + Enter
; =====================================================================
SelectExcelMenu(right_x, right_y, downs) {
    ; 우클릭은 ProcessStock에서 이미 완료된 상태
    Sleep(600)

    LogWrite("메뉴 탐색: Down×" . downs . " + Enter")
    Loop downs {
        Send("{Down}")
        Sleep(80)
    }
    Send("{Enter}")
    Sleep(1000)

    ; 저장 다이얼로그 확인 (csv 포함)
    for hwnd in WinGetList() {
        t := WinGetTitle(hwnd)
        if InStr(t, "저장") || InStr(t, "Save") || InStr(t, "다른 이름") || InStr(t, "csv") {
            LogWrite("저장 다이얼로그: " . t)
            return true
        }
    }

    ; 다이얼로그 없이 자동 저장되는 경우도 있음 — 호출자에서 파일 확인
    LogWrite("저장 다이얼로그 미확인 (자동저장 가능)")
    return false
}

; =====================================================================
; [함수] SanitizeFilename
; =====================================================================
SanitizeFilename(name) {
    for ch in ["\", "/", ":", "*", "?", "`"", "<", ">", "|"]
        name := StrReplace(name, ch, "_")
    return Trim(name, " .")
}

; =====================================================================
; [함수] LogWrite
; =====================================================================
LogWrite(msg) {
    global LOG_FILE
    ts := FormatTime(A_Now, "yyyy-MM-dd HH:mm:ss")
    FileAppend(ts . " | " . msg . "`n", LOG_FILE)
}

; =====================================================================
; [핫키] Esc — 언제든 중단
; =====================================================================
Esc:: {
    result := MsgBox("스크립트를 지금 중단하시겠습니까?", "hts_dump — 중단", 0x24)
    if result = "Yes"
        ExitApp()
}
