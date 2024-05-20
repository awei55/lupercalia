@echo off
setlocal

set "df=C:\Users\synch\OneDrive\Desktop"

:monitorLoop

if exist "%df%\.tmp.drivedownload" (
    attrib +s +h "%df%\.tmp.drivedownload" >nul 2>&1
)

if exist "%df%\.tmp.driveupload" (
    attrib +s +h "%df%\.tmp.driveupload" >nul 2>&1
)

timeout /t 10 /nobreak >nul
goto :monitorLoop