@echo off
rem Headless boot check: runs the recompiled game without a window for N seconds (default 90)
rem and reports whether it renders, keeps progressing, plays audio and uses the memory card.
rem The report is written to rust\preflight_report.txt. Uses a throwaway memory card.
cd /d "%~dp0rust"
set AC_MEMCARD=%TEMP%\ac_preflight_card.bin
del /q "%AC_MEMCARD%" 2>nul
cargo run -p ac-recomp --bin preflight -- %1
set AC_MEMCARD=
pause
