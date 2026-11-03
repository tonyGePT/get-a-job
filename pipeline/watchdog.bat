@echo off
cd /d C:\tmp\jobharness\pipeline
"C:\Python313\python.exe" watchdog.py >> state\logs\watchdog.log 2>&1
