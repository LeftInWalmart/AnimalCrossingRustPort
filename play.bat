@echo off
rem Launch the recompiled Animal Crossing Bevy app (builds first if needed).
rem The disc image defaults to %USERPROFILE%\Downloads\Emus\GAMECUBE\Animal Crossing (USA).ciso;
rem set AC_DISC to use another .iso/.ciso. The memory card is saved to rust\memcard\slotA.bin.
cd /d "%~dp0rust"
cargo run -p ac-bevy %*
if errorlevel 1 pause
