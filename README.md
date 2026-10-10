# OrcaTerm AI 反代

把腾讯云 OrcaTerm 桌面版的 AI 助手(后端 LightAI)暴露为 OpenAI 兼容 API。

## 快速启动

1. 启动 OrcaTerm（必须带 CDP 参数，用脚本）：
   ```
   powershell -File E:\python\tools\orcaterm_proxy\start-orcaterm.ps1
   ```
2. 启动反代：
   ```
   powershell -File E:\python\tools\orcaterm_proxy\start-proxy.ps1
   ```
   显示 `auth: API_KEY required` 即就绪，窗口保持开着（Ctrl+C 停止）。

## 客户端接入

- base_url：`http://127.0.0.1:8117/v1`（客户端拼 `/chat/completions` 时）
- api_key：`sk-daffeeff444ssd5sd5`（在 start-proxy.ps1 里改）
- 模型：`glm-5.3`（默认）、`deepseek-chat`

## 依赖

- Python 3.10+，`pip install websocket-client`（CDP 读取 token 用）
- OrcaTerm 保持运行且已登录（token 源头）

## 工作原理

- 上游：`POST https://lightai.cloud.tencent.com/assistant/chat?name=orcaterm&mode=0`（SSE）
- token：OrcaTerm 每 2h 轮换但不回写 data.bin；反代在 data.bin token 剩余 <5min 时经 CDP(9223) 读前端 localStorage 的最新 ot_session
- 过期 token 的上游行为是静默空流（不报 401）——遇到"全部空回复"先查 OrcaTerm 是否带 CDP 参数启动、是否掉登录

## 常见问题

- **回复一直是空的**：多半是端口上挂了两个反代进程——Windows 允许重复绑定 8117，旧进程（跑着旧代码）抢走请求，新进程收不到流量且不报错。检查：
  `netstat -ano | findstr :8117`，看到两个 LISTENING PID 就全杀：
  `Get-NetTCPConnection -LocalPort 8117 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }`
  再用 start-proxy.ps1 重启。
- 新启报 `Address already in use`：同上，旧实例占着端口
- 客户端 404：base_url 拼接问题（自动拼 `/v1/chat/completions` 的客户端填 `http://127.0.0.1:8117` 即可）
- 偶发空回复：上游抖动，重试即可
