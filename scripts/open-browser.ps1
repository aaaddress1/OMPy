param([int]$Port = 8000)
$deadline = (Get-Date).AddMinutes(5)
while ((Get-Date) -lt $deadline) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/canvas" -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            Start-Process "http://127.0.0.1:$Port/canvas"
            break
        }
    } catch { }
    Start-Sleep -Seconds 2
}
