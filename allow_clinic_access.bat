@echo off
REM ===================================================================
REM  Let the other clinic PCs reach this one. Run ONCE, as Administrator.
REM
REM  Windows Firewall blocks incoming connections by default, so without
REM  this the app runs fine on this PC and every other PC just times out.
REM
REM  Right-click this file -> "Run as administrator".
REM ===================================================================

net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo This needs Administrator rights.
    echo Close this window, right-click allow_clinic_access.bat,
    echo and choose "Run as administrator".
    echo.
    pause
    exit /b 1
)

set RULE=Radiology Report Generator (clinic LAN)

REM Remove any earlier version of the rule so re-running is safe.
netsh advfirewall firewall delete rule name="%RULE%" >nul 2>&1

REM Private/Domain only. Deliberately NOT the public profile: on a cafe or
REM hotel network this app should stay invisible.
netsh advfirewall firewall add rule ^
    name="%RULE%" ^
    dir=in action=allow protocol=TCP localport=8501 ^
    profile=private,domain ^
    description="Radiology report generator, reachable from clinic PCs only"

if errorlevel 1 (
    echo.
    echo Could not add the firewall rule.
    pause
    exit /b 1
)

echo.
echo ===================================================================
echo   Done. Other clinic PCs can now reach this one on port 8501.
echo.
echo   IMPORTANT: this only works while Windows treats your clinic
echo   network as "Private". If staff cannot connect, open
echo   Settings ^> Network ^> Wi-Fi/Ethernet ^> your network,
echo   and set the network profile to Private.
echo ===================================================================
echo.
echo   To undo this later:
echo     netsh advfirewall firewall delete rule name="%RULE%"
echo.
pause
