# Starts the three services the phone agent needs on this machine, each in its own window.
# Close a window to stop that service. Then open http://127.0.0.1:8002/test
#
#   erp-lookup    :8100  records, caller identity, transfer numbers
#   rag-backend   :8000  the shared brain (answers, lead capture)
#   phone-agent   :8002  the call itself + the browser test page
#
# Each service also writes its log to C:\ACT-Calling-Agent\logs\ (git-ignored), so a call can be
# checked afterwards: phone-agent.log has the [turn], [latency] and [interrupt] lines.
$root = $PSScriptRoot
$web = "C:\ACT-Voice-Assistant\rag-backend"
$py = "$root\phone-agent\venv\Scripts\python.exe"
$logs = "$root\logs"
New-Item -ItemType Directory -Force $logs | Out-Null

Start-Process powershell -WorkingDirectory "$root\erp-lookup" -ArgumentList "-NoExit", "-Command", "`$host.UI.RawUI.WindowTitle='erp-lookup 8100'; & '$py' -m uvicorn api:app --host 127.0.0.1 --port 8100 2>&1 | Tee-Object -FilePath '$logs\erp-lookup.log'"
Start-Process powershell -WorkingDirectory $web -ArgumentList "-NoExit", "-Command", "`$host.UI.RawUI.WindowTitle='rag-backend 8000'; & '$web\venv\Scripts\python.exe' -m uvicorn main:app --host 127.0.0.1 --port 8000 2>&1 | Tee-Object -FilePath '$logs\rag-backend.log'"
Start-Process powershell -WorkingDirectory "$root\phone-agent" -ArgumentList "-NoExit", "-Command", "`$host.UI.RawUI.WindowTitle='phone-agent 8002'; & '$py' -m uvicorn server:app --host 127.0.0.1 --port 8002 2>&1 | Tee-Object -FilePath '$logs\phone-agent.log'"

Write-Host "Starting... wait about 30 seconds, then open http://127.0.0.1:8002/test"
Write-Host "Health: http://127.0.0.1:8100/health  http://127.0.0.1:8000/health  http://127.0.0.1:8002/health"
