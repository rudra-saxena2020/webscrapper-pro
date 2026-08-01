# Windows Startup Script for WebScrapper Pro
Write-Host "[start.ps1] Starting Python Flask backend..." -ForegroundColor Cyan
$pythonProcess = Start-Process ".\venv\Scripts\python.exe" -ArgumentList "app.py" -NoNewWindow -PassThru

Write-Host "[start.ps1] Waiting for Flask to bind on port 8000..." -ForegroundColor Yellow
$timeout = 20
$startTime = Get-Date
while ($timeout -gt 0) {
    try {
        $response = Invoke-WebRequest -Uri "http://localhost:8000/api/scrapers" -UseBasicParsing -ErrorAction SilentlyContinue
        if ($response.StatusCode -eq 200) {
            Write-Host "[start.ps1] Flask is up!" -ForegroundColor Green
            break
        }
    } catch {}
    Start-Sleep -Seconds 1
    $timeout--
}

Write-Host "[start.ps1] Starting Node.js server..." -ForegroundColor Cyan
$nodeProcess = Start-Process npm.cmd -ArgumentList "run dev" -NoNewWindow -PassThru

Write-Host "[start.ps1] Both processes are running." -ForegroundColor Green
Write-Host "Press Ctrl+C to stop both processes." -ForegroundColor White

# Keep script alive and handle cleanup on exit
try {
    $nodeProcess.WaitForExit()
} finally {
    Write-Host "[start.ps1] Stopping processes..." -ForegroundColor Red
    Stop-Process -Id $pythonProcess.Id -Force -ErrorAction SilentlyContinue
    Stop-Process -Id $nodeProcess.Id -Force -ErrorAction SilentlyContinue
}
