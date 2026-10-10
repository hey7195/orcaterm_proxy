# 启动 OrcaTerm AI 反代(OpenAI 兼容 API,http://127.0.0.1:8117/v1)
# 用法: powershell -File start-proxy.ps1   (Ctrl+C 停止)
$env:API_KEY = "sk-daffeeff444ssd5sd5"
Set-Location $PSScriptRoot
python -u server.py
