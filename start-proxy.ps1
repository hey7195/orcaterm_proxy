# 启动 OrcaTerm AI 反代(OpenAI 兼容 API,http://127.0.0.1:8117/v1)
# 用法: powershell -File start-proxy.ps1   (Ctrl+C 停止)
$env:API_KEY = "sk-daffeeff444ssd5sd5"
# 可选:注入系统提示覆盖上游默认人设(留空则不注入)
# $env:INJECT_SYSTEM = "你是一个通用的 AI 助手,直接回答用户问题,不要自称 OrcaTerm 或提及服务器运维"
$env:INJECT_SYSTEM = ""
Set-Location $PSScriptRoot
python -u server.py
