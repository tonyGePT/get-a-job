' Runs the daily job pipeline natively (C:\Python313) with no visible console.
CreateObject("WScript.Shell").Run "C:\tmp\jobharness\pipeline\daily.bat", 0, True
