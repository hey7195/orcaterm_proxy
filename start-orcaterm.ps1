# 启动 OrcaTerm(带 CDP 调试端口,供反代 server.py 读取最新 token 用)
# 用法: powershell -File start-orcaterm.ps1  或右键"使用 PowerShell 运行"
$env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = "--remote-debugging-port=9223"
Start-Process "E:\OrcaTerm\OrcaTerm.exe"
