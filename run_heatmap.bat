@echo off
rem Double-click to rebuild the engineer heat map from CMS593 and open it.
cd /d "%~dp0"
python -m heatmap build --open
pause
