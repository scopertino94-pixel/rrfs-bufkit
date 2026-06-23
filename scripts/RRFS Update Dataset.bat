@echo off
REM ============================================================================
REM  Update RRFS Dataset  -  PRODUCER step (run on ONE machine only)
REM
REM  Decodes the latest RRFS cycle and publishes the .buf files to the public
REM  Hugging Face dataset (ORG/rrfs-bufkit) so everyone's downloader gets
REM  fresh data. Also writes them to the local BUFKIT Data folder.
REM
REM  Takes ~10-12 min when there's a new cycle; a few seconds if already current
REM  (it no-ops until a new 00/06/12/18Z cycle has landed on AWS).
REM
REM  NOTE: this is the PUBLISH step, different from "WW Bufkit RRFS.pl"
REM  (which just downloads the finished files into BUFKIT).
REM
REM  EDIT THE TWO PATHS BELOW for this machine:
REM    PYTHON  = path to python.exe (Anaconda recommended; see requirements.txt)
REM    PROJECT = folder where this project is installed (contains the "code" dir)
REM  And set --publish-repo to your own dataset if you are not using this one.
REM ============================================================================
set "PYTHON=C:\path\to\python.exe"
set "PROJECT=C:\path\to\RRFS BUFKIT Handoff"

echo Updating the RRFS BUFKIT dataset - please wait...
echo.
"%PYTHON%" "%PROJECT%\code\tools\update_rrfs.py" --source det --publish-repo ORG/rrfs-bufkit
echo.
echo Finished. Press any key to close.
pause >nul
