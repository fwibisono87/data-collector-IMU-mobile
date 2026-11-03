@echo off
rem Double-click: writes what ffmpeg sees as cameras to camera_list.txt in the repo root,
rem using the same ffmpeg the server uses. Send that file when cameras are not detected.
cd /d "%~dp0.."
set FF=vendor\ffmpeg\bin\ffmpeg.exe
if not exist "%FF%" set FF=ffmpeg
echo ffmpeg: %FF% > camera_list.txt
"%FF%" -hide_banner -list_devices true -f dshow -i dummy >> camera_list.txt 2>&1
echo. >> camera_list.txt
echo --- server camera list --- >> camera_list.txt
powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/api/state).Content } catch { $_.Exception.Message }" >> camera_list.txt 2>&1
notepad camera_list.txt
