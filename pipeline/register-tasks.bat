@echo off
schtasks /Create /F /TN JobHuntDaily /SC DAILY /ST 08:00 /TR "wscript.exe C:\tmp\jobharness\pipeline\daily-hidden.vbs"
schtasks /Create /F /TN JobHuntWatchdog /SC MINUTE /MO 120 /TR "wscript.exe C:\tmp\jobharness\pipeline\watchdog-hidden.vbs"
schtasks /Create /F /TN JobHuntRetro /SC DAILY /ST 12:00 /TR "wscript.exe C:\tmp\jobharness\pipeline\retro-hidden.vbs"
