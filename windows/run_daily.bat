@echo off
REM =====================================================================
REM  Windows Task Scheduler 진입점
REM  Task Scheduler → 이 배치 파일 → run_daily.py 실행
REM
REM  설정 방법:
REM    Task Scheduler > 새 작업 만들기
REM    - 트리거: 매일 08:00, 12:00, 16:00, 20:00 (KST)
REM    - 동작: 이 .bat 파일 실행
REM    - 조건: 네트워크 연결 시에만
REM =====================================================================

REM 저장소 경로 (실제 경로로 수정 필요)
set REPO_DIR=%~dp0..

REM Python 경로 (필요시 절대경로로 수정, 예: C:\Python311\python.exe)
set PYTHON=python

REM 로그 파일
set LOG=%REPO_DIR%\windows\run_daily.log

echo [%DATE% %TIME%] Task Scheduler 트리거됨 >> "%LOG%"

cd /d "%REPO_DIR%"
%PYTHON% "%REPO_DIR%\windows\run_daily.py" >> "%LOG%" 2>&1

if %ERRORLEVEL% NEQ 0 (
    echo [%DATE% %TIME%] 오류 발생 (exit code: %ERRORLEVEL%) >> "%LOG%"
) else (
    echo [%DATE% %TIME%] 정상 완료 >> "%LOG%"
)
