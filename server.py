"""
OrcaTerm AI 反代 —— 把 OrcaTerm(腾讯云 LightAI) 的 AI 额度暴露为 OpenAI 兼容 API。

用法:
  python server.py                 # 监听 0.0.0.0:8117
  API_KEY=sk-xxx python server.py # 要求客户端带 Authorization: Bearer sk-xxx

端点:
  GET  /v1/models
  POST /v1/chat/completions       (支持 stream / 非流式)

凭证: 自动从 OrcaTerm data.bin 读取 JWT + 身份信息。
JWT 过期(2h)后需要重新打开 OrcaTerm 登录刷新——本服务每次请求都重读文件,
OrcaTerm 保持运行时 token 会自动轮换,无需重启本服务。
"""
import json
import os
import re
import time
import uuid
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA_BIN = os.path.expandvars(r'%APPDATA%/com.orcaterm-desktop.app/data.bin')
UPSTREAM = 'https://lightai.cloud.tencent.com/assistant/chat?name=orcaterm&mode=0&csrfCode=&sseresume=true'
API_KEY = os.environ.get('API_KEY', '')
CDP_PORT = int(os.environ.get('CDP_PORT', '9223'))  # OrcaTerm 带 --remote-debugging-port 启动时可用

# 实测可用的模型(上游对未开通/不存在的模型名返回空流,所以只列已验证的)
MODELS = {
    'glm-5.3': 'TokenHub/glm-5.3',
    'deepseek-chat': 'deepseek-chat-latest',
}
DEFAULT_MODEL = 'glm-5.3'

# 上游直连,不走系统代理(代理会缓冲 SSE 流)
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# token 缓存:OrcaTerm 刷新 token 后不一定回写 data.bin(实测 19:02 换的 token 只在前端内存),
# 所以优先走 CDP 从前端拿活 token,拿不到再退回 data.bin。
# 过期 token 上游不报 401,而是 0.3s 静默返回空流——必须提前识别。
_token_cache = {'token': None, 'ts': 0}
CDP_EXPR = """
(()=>{ try {
  const ed = document.querySelector('.chat-ui-rich-input__editor');
  if (ed) { ed.focus(); }  // 触发 reconcile(focus) 促使 OrcaTerm 刷新 token
  const w = window.__ot_token_getter;
  return w ? w() : 'NO_GETTER';
} catch(e){ return 'ERR:'+e; } })()
"""


def _jwt_exp(token):
    try:
        import base64
        p = token.split('.')[1]
        pl = json.loads(base64.urlsafe_b64decode(p + '=' * (-len(p) % 4)))
        return pl.get('exp', 0)
    except Exception:
        return 0


def _cdp_get_token():
    """通过 CDP 调用 OrcaTerm 前端的 token 获取器(由 inject_cdp_hook.py 注入)。"""
    try:
        import websocket  # pip install websocket-client
        pages = json.load(_opener.open(f'http://127.0.0.1:{CDP_PORT}/json/list', timeout=2))
        main = next((p for p in pages if p.get('url', '') == 'http://tauri.localhost/'), None)
        if not main:
            return None
        ws = websocket.create_connection(main['webSocketDebuggerUrl'], timeout=5, suppress_origin=True)
        try:
            # 优先用注入的 getter;否则从 hook 日志里翻
            ws.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate', 'params': {
                'expression': """
                (()=>{ try {
                  // 首选:OrcaTerm 自己维护的 localStorage cookie(重启也在,始终最新)
                  const raw = localStorage.getItem('__tauri_cookies__');
                  if (raw) {
                    const o = JSON.parse(raw);
                    const tok = o.ot_session && o.ot_session.value;
                    if (tok && String(tok).startsWith('eyJ')) {
                      const uin = (o.uin && o.uin.value) || '';
                      const uid = (o.userId && o.userId.value) || '';
                      return JSON.stringify({tok, uin, uid});
                    }
                  }
                  // 后备:翻 hook 日志里的 Bearer(需注入过 __fullreq)
                  const reqs = window.__fullreq||[];
                  for (let i=reqs.length-1;i>=0;i--) {
                    try {
                      const cfg = JSON.parse(reqs[i].body).clientConfig;
                      if (!/assistant\\/chat/.test(cfg.url||'')) continue;
                      const auth = (cfg.headers.find(h=>h[0]==='authorization')||[])[1]||'';
                      if (auth.startsWith('Bearer ')) return JSON.stringify({tok: auth.slice(7), uin: null, uid: null});
                    } catch(e){}
                  }
                  return null;
                } catch(e){ return null; } })()
                """,
                'returnByValue': True}}))
            import time as _t
            t0 = _t.time()
            while _t.time() - t0 < 5:
                msg = json.loads(ws.recv())
                if msg.get('id') == 1:
                    v = msg.get('result', {}).get('result', {}).get('value')
                    if isinstance(v, str):
                        try:
                            o = json.loads(v)  # localStorage 版: {tok, uin, uid}
                            return o.get('tok') or None, o.get('uin'), o.get('uid')
                        except json.JSONDecodeError:
                            # hook 后备版: 裸 JWT 字符串
                            if v.startswith('eyJ'):
                                return v, None, None
                    return None, None, None
        finally:
            ws.close()
    except Exception:
        return None, None, None
    return None, None, None


def load_creds():
    """优先 CDP 活 token(校验有效期),兜底 data.bin。"""
    d = json.load(open(DATA_BIN, encoding='utf-8'))
    ident = json.loads(d.get('auth_identity') or '{}')
    tok = d['oauth_access_token']
    uin = ident.get('ownerUin') or ident.get('uin') or ''
    user_id = ident.get('userId', '')
    now = time.time()
    # data.bin token 还有 5 分钟以上寿命才直接用
    if _jwt_exp(tok) - now <= 300:
        fresh, cdp_uin, cdp_uid = _cdp_get_token()
        if fresh and _jwt_exp(fresh) - now > 300:
            tok = fresh
            if cdp_uin:
                uin = cdp_uin.lstrip('o')
            if cdp_uid:
                user_id = cdp_uid
            _token_cache.update(token=fresh, ts=now)
    cookie = (f'ot_session={tok}; userAccountLoginMethod=oauth; userAccountType=txcloud; '
              f'userId={user_id}; uin=o{uin.lstrip("o")}; ownerUin=o{uin.lstrip("o")}')
    return tok, cookie, user_id


def build_upstream_body(openai_body, user_id):
    """OpenAI messages -> LightAI chat body。多轮对话拼成一段文本(acp-prompt 只有一条 text)。"""
    model = MODELS.get(openai_body.get('model', DEFAULT_MODEL), 'TokenHub/glm-5.3')
    parts = []
    for m in openai_body.get('messages', []):
        role = m['role']
        content = m.get('content') or ''
        if isinstance(content, list):  # 多模态拍平
            content = '\n'.join(c.get('text', '') for c in content if isinstance(c, dict))
        tag = {'system': 'System', 'user': 'User', 'assistant': 'Assistant', 'tool': 'Tool'}.get(role, role)
        parts.append(f'[{tag}]\n{content}')
    seq = str(uuid.uuid4())
    body = {
        'conversationId': 'cid-' + str(uuid.uuid4()) + '-orcaterm',
        'user': {'id': str(user_id), 'setting': {
            'mcpServers': ['mcp-server-orcaterm-oauth'],
            'uiServers': ['ui-tools-orcaterm-explorer'],
            'tools': [], 'approvedTools': [], 'approvedMCPTools': [],
            'enableAutoSubtaskExecution': False, 'env': [],
            'model': model, 'modelDesc': {}, 'extra': {}}},
        'input': {'type': 'start',
                  'input': json.dumps({'_format': 'acp-prompt', 'version': 1,
                                       'prompt': [{'type': 'text', 'text': '\n\n'.join(parts)}]}),
                  'id': seq, 'emphasisPrompt': '', 'files': []},
        'stream': True,
    }
    return body, seq


def open_upstream(openai_body):
    tok, cookie, uid = load_creds()
    body, seq = build_upstream_body(openai_body, uid)
    req = urllib.request.Request(UPSTREAM, data=json.dumps(body).encode(), headers={
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Authorization': f'Bearer {tok}',
        'Cookie': cookie,
        'x-product': 'orcaterm',
        'x-seqid': seq,
        'User-Agent': 'Mozilla/5.0',
    })
    return _opener.open(req, timeout=300)


def iter_sse(resp):
    """yield 每个 data: JSON 对象。逐字节读避免 read(n) 凑缓冲阻塞流式。"""
    buf = b''
    while True:
        b_ = resp.read(1)
        if not b_:
            break
        buf += b_
        while b'\n' in buf:
            line, buf = buf.split(b'\n', 1)
            line = line.strip()
            if line.startswith(b'data: '):
                payload = line[6:]
                if payload == b'[DONE]':
                    return
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    pass


def extract_text(obj):
    """从 LightAI 事件提取正文。模型被 OrcaTerm agent 提示词约束输出 json 代码块,
    优先解析 completion JSON 拿 taskCompletion/answer,失败则原文返回。"""
    return obj.get('content') or '', obj.get('reasoningContent') or ''


def try_unwrap_agent_json(full_text):
    """模型常输出 ```json {"action":"...","taskCompletion":"..."} ```,
    反代给其他 agent 用时应剥掉这层壳,取用户可读的文本字段。"""
    # 兼容无 ``` 包裹的裸 JSON
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', full_text, re.S) \
        or re.fullmatch(r'\s*(\{.*?\})\s*', full_text, re.S)
    if not m:
        return None
    try:
        o = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(o, dict) or 'action' not in o:
        return None
    for k in ('taskCompletion', 'question', 'answer', 'content', 'result'):
        if o.get(k):
            return o[k]
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        print(f'[{time.strftime("%H:%M:%S")}] {fmt % args}')

    # ---- helpers ----
    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        if not API_KEY:
            return True
        h = self.headers.get('Authorization', '')
        return h == f'Bearer {API_KEY}'

    # ---- routes ----
    def do_GET(self):
        if self.path == '/v1/models':
            if not self._authed():
                return self._json(401, {'error': {'message': 'bad api key'}})
            now = int(time.time())
            return self._json(200, {'object': 'list', 'data': [
                {'id': m, 'object': 'model', 'created': now, 'owned_by': 'orcaterm'}
                for m in MODELS]})
        return self._json(404, {'error': {'message': 'not found'}})

    def do_POST(self):
        if self.path != '/v1/chat/completions':
            return self._json(404, {'error': {'message': 'not found'}})
        if not self._authed():
            return self._json(401, {'error': {'message': 'bad api key'}})
        length = int(self.headers.get('Content-Length', 0))
        try:
            openai_body = json.loads(self.rfile.read(length) or b'{}')
        except json.JSONDecodeError:
            return self._json(400, {'error': {'message': 'invalid json'}})

        try:
            resp = open_upstream(openai_body)
        except urllib.error.HTTPError as e:
            return self._json(502, {'error': {'message': f'upstream {e.code}: {e.read().decode(errors="replace")[:300]}'}})
        except FileNotFoundError:
            return self._json(503, {'error': {'message': 'data.bin not found — 先安装并登录 OrcaTerm'}})
        except Exception as e:
            return self._json(502, {'error': {'message': f'upstream error: {e}'}})

        model_name = openai_body.get('model', DEFAULT_MODEL)
        stream = bool(openai_body.get('stream'))
        cid = 'chatcmpl-' + uuid.uuid4().hex[:24]
        created = int(time.time())

        if not stream:
            content, reasoning = '', ''
            for ev in iter_sse(resp):
                c, r = extract_text(ev)
                content += c
                reasoning += r
            unwrapped = try_unwrap_agent_json(content)
            if unwrapped is not None:
                content = unwrapped
            return self._json(200, {
                'id': cid, 'object': 'chat.completion', 'created': created,
                'model': model_name,
                'choices': [{'index': 0, 'finish_reason': 'stop',
                             'message': {'role': 'assistant', 'content': content,
                                         'reasoning_content': reasoning or None}}],
                'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0},
            })

        # ---- streaming ----
        self.close_connection = True  # 无 Content-Length,流结束靠断开连接通知客户端
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()

        def sse_chunk(delta_content=None, delta_reasoning=None, finish=None):
            delta = {}
            if delta_content is not None:
                delta['content'] = delta_content
            if delta_reasoning is not None:
                delta['reasoning_content'] = delta_reasoning
            chunk = {
                'id': cid, 'object': 'chat.completion.chunk',
                'created': created, 'model': model_name,
                'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}],
            }
            return ('data: ' + json.dumps(chunk, ensure_ascii=False) + '\n\n').encode()

        try:
            # 先攒完整内容判断是否要剥 agent json 壳,流式也得剥(客户端拼出来的是全文)
            full_content, full_reasoning = '', ''
            pending = []
            for ev in iter_sse(resp):
                c, r = extract_text(ev)
                if r:
                    full_reasoning += r
                    self.wfile.write(sse_chunk(delta_reasoning=r))
                    self.wfile.flush()
                if c:
                    full_content += c
                    pending.append(c)
            unwrapped = try_unwrap_agent_json(full_content)
            if unwrapped is not None:
                self.wfile.write(sse_chunk(delta_content=unwrapped))
            else:
                for c in pending:
                    self.wfile.write(sse_chunk(delta_content=c))
            self.wfile.write(sse_chunk(finish='stop'))
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass  # 客户端断开


def main():
    port = int(os.environ.get('PORT', '8117'))
    srv = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print(f'OrcaTerm OpenAI-compatible proxy on http://127.0.0.1:{port}/v1')
    print(f'models: {", ".join(MODELS)}')
    print(f'auth: {"API_KEY required" if API_KEY else "no auth (local only)"}')
    srv.serve_forever()


if __name__ == '__main__':
    main()
