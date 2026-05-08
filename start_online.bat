@echo off
echo ========================================================
echo Starting Smart Street Light App - Online Mode
echo ========================================================
echo.

:: Start the Flask app in a new window with the online flag
echo Starting Flask Server...
start "Flask Server" cmd /c "python app.py --online"

:: Wait a few seconds for Flask to initialize
echo Waiting for server to initialize...
timeout /t 4 /nobreak > nul

:: Start Localtunnel to expose it to the internet
echo.
echo ========================================================
echo Tunneling to the Internet via Localtunnel...
echo A browser window may open, or a URL will be displayed below.
echo ========================================================
npx localtunnel --port 5000

echo.
pause
