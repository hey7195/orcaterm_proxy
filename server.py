"""
OrcaTerm AI 反代 —— 把 OrcaTerm(腾讯云 LightAI) 的 AI 额度暴露为 OpenAI / Anthropic 兼容 API。

用法:
  python server.py                 # 监听 0.0.0.0:8117
  API_KEY=sk-xxx python server.py # 要求客户端带 Authorization: Bearer sk-xxx 或 x-api-key
  INJECT_SYSTEM="..." python server.py  # 注入系统提示,覆盖上游的 OrcaTerm 运维助手人设

端点:
  GET  /v1/models                 (OpenAI)
  POST /v1/chat/completions       (OpenAI,支持 stream / 非流式)
  POST /v1/messages               (Anthropic Messages,支持 stream / 非流式)

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

import anthropic_compat

DATA_BIN = os.path.expandvars(r'%APPDATA%/com.orcaterm-desktop.app/data.bin')
UPSTREAM = 'https://lightai.cloud.tencent.com/assistant/chat?name=orcaterm&mode=0&csrfCode=&sseresume=true'
API_KEY = os.environ.get('API_KEY', '')
# 注入到每次请求最前的系统提示,覆盖上游默认的"OrcaTerm AI 运维助手"人设
INJECT_SYSTEM = os.environ.get('INJECT_SYSTEM', '')

# 拼在 emphasisPrompt 尾部的格式护栏,按请求形态二选一:
# 带 tools(agent 客户端) -> 鼓励原生 JSON 工具调用且必须给完整命令(反代桥接用);
# 无 tools(纯聊天) -> 纯文本输出,流式才能实时透传。
_FMT_GUARD = '输出格式硬性要求:直接以纯文本/Markdown 回答。严禁输出 {"action":...} JSON 动作对象或调用任何工具。需要执行命令时把命令写进 ```bash 代码块,由宿主执行。'
_FMT_GUARD_TOOLS = ('工具调用硬性要求:需要执行命令或抓取网页时,用原生 JSON 动作格式输出,'
                    '且 args.command 必须是完整、可直接执行的真实命令——禁止只写自然语言描述。'
                    '严禁使用 <tool_call> XML 或任何非 JSON 格式。纯文本回答直接写,不要包 JSON。')
CDP_PORT = int(os.environ.get('CDP_PORT', '9223'))  # OrcaTerm 带 --remote-debugging-port 启动时可用

# 实测可用的模型(上游对未开通/不存在的模型名返回空流,所以只列已验证的)
MODELS = {
    'glm-5.3': 'TokenHub/glm-5.3',
    'deepseek-chat': 'deepseek-chat-latest',
}
DEFAULT_MODEL = 'glm-5.3'

UPSTREAM_TIMEOUT = int(os.environ.get('UPSTREAM_TIMEOUT', '120'))  # 上游无数据看门狗(秒),超时断开让客户端重试
HEARTBEAT_SEC = float(os.environ.get('HEARTBEAT_SEC', '15'))  # SSE 心跳间隔,防客户端等死

# 上游直连,不走系统代理(代理会缓冲 SSE 流)
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def looks_like_agent_json(text):
    """流式前缀探测:开头是否像 agent JSON 壳({ / ```json / <tool_call>)。
    像才需要攒完整流剥壳,否则纯文本实时透传。"""
    t = text.lstrip()
    return t.startswith(('{', '```', '<tool_call'))


# 中途壳标志:```json 围栏、{ "action" (允许空格)、<tool_call> XML 包裹
_SHELL_MARK_RE = re.compile(r'```json|\{\s*"action"|<tool_call')

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
    """OpenAI messages -> LightAI chat body。
    system 消息和 INJECT_SYSTEM 走 emphasisPrompt(服务端高优先级,能压过 OrcaTerm 人设),
    其余角色拼成文本进 acp-prompt。"""
    model = MODELS.get(openai_body.get('model', DEFAULT_MODEL), 'TokenHub/glm-5.3')
    sys_msgs, parts = [], []
    for m in openai_body.get('messages', []):
        role = m['role']
        content = m.get('content') or ''
        if isinstance(content, list):  # 多模态拍平
            content = '\n'.join(c.get('text', '') for c in content if isinstance(c, dict))
        if role in ('system', 'developer'):
            if content:
                sys_msgs.append(content)
            continue
        tag = {'user': 'User', 'assistant': 'Assistant', 'tool': 'Tool'}.get(role, role)
        parts.append(f'[{tag}]\n{content}')
    emphasis = '\n\n'.join([INJECT_SYSTEM] + sys_msgs) if (INJECT_SYSTEM or sys_msgs) else ''
    # 按请求形态选护栏:带 tools 用工具调用护栏(桥接需要),纯聊天用纯文本护栏(实时透传需要)
    guard = _FMT_GUARD_TOOLS if openai_body.get('tools') else _FMT_GUARD
    emphasis = f'{emphasis}\n\n{guard}' if emphasis else guard
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
                  'id': seq,
                  # emphasisPrompt 在服务端拼到系统提示词后部,能压过 OrcaTerm 默认人设——注入就走这里
                  'emphasisPrompt': emphasis,
                  'files': []},
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
    return _opener.open(req, timeout=UPSTREAM_TIMEOUT)


def iter_sse(resp, watchdog=None):
    """yield 每个 data: JSON 对象。逐字节读避免 read(n) 凑缓冲阻塞流式。
    watchdog: 无数据的秒数超过它就抛 TimeoutError(上游卡死时别让客户端永远等)。"""
    buf = b''
    last_data = time.time()
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
                    o = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                last_data = time.time()
                yield o
        if watchdog and time.time() - last_data > watchdog:
            raise TimeoutError(f'upstream silent for {watchdog}s')


def extract_text(obj):
    """从 LightAI 事件提取正文。模型被 OrcaTerm agent 提示词约束输出 json 代码块,
    优先解析 completion JSON 拿 taskCompletion/answer,失败则原文返回。"""
    return obj.get('content') or '', obj.get('reasoningContent') or ''


def _salvage_agent_json(text):
    """模型偶尔在 command 值里嵌未转义引号(如 LOG="..." 的 Windows 路径),
    严格 JSON 解析整个壳失败。用容错正则抢救桥接所需的最小字段集。"""
    def grab(pat):
        m = re.search(pat, text, re.S)
        return m.group(1).strip() if m else None
    # command 通常是最后一个字段:贪心到结尾闭合括号前的最后一个引号
    cmd = grab(r'"command"\s*:\s*"(.*)"\s*\}\s*\}\s*\}?\s*$') \
       or grab(r'"command"\s*:\s*"(.*)"')
    if not cmd:
        return None
    o = {'action': grab(r'"action"\s*:\s*"([^"]*)"') or 'review',
         'tool': {'name': grab(r'"name"\s*:\s*"(execute_command|fetch|web[\w-]*)"') or 'execute_command',
                  'args': {'command': cmd}}}
    thinking = grab(r'"thinking"\s*:\s*"(.*)"\s*,\s*"(?:taskCompletion|question|answer|tool)"')
    if thinking:
        o['thinking'] = thinking
    return o


def parse_agent_json(full_text):
    """完整解析模型输出的 agent JSON 壳,返回 dict 或 None。
    支持三种包裹:```json 围栏 / 裸 JSON / <tool_call>json ... </tool_call>(模型幻觉的 XML 包裹)。
    严格解析失败时走容错抢救,只要能拿到 args.command 就够桥接。"""
    t = full_text.strip()
    t = re.sub(r'^<tool_call>\s*(?:json)?\s*', '', t)
    t = re.sub(r'\s*</tool_call>\s*$', '', t)
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', t, re.S) \
        or re.fullmatch(r'\s*(\{.*?\})\s*', t, re.S)
    if m:
        try:
            o = json.loads(m.group(1))
            if isinstance(o, dict) and 'action' in o:
                return o
        except json.JSONDecodeError:
            pass
    return _salvage_agent_json(t)


def pick_exec_tool(tools):
    """从客户端声明的 tools 里挑一个能执行命令的,返回 (name, 参数名)。
    上游模型的工具调用本质都是 execute_command,桥接到宿主 agent 的 Bash 类工具。"""
    name_pats = ('bash', 'shell', 'execute_command', 'run_command', 'terminal', 'exec')
    key_pats = ('command', 'cmd', 'script', 'code', 'bash')
    for t in tools or []:
        if not isinstance(t, dict) or t.get('type') != 'function':
            continue
        f = t.get('function') or {}
        name = (f.get('name') or '').lower()
        if not any(p in name for p in name_pats):
            continue
        props = ((f.get('parameters') or {}).get('properties') or {})
        args_key = next((k for k in props if any(p in k.lower() for p in key_pats)), None)
        if args_key:
            return f.get('name'), args_key
    return None, None


def bridge_tool_call(agent_json, openai_body):
    """上游 tool JSON -> OpenAI tool_calls(桥接到客户端声明的执行类工具)。
    支持两类上游工具:
    - execute_command 系(args.command) -> 直接给宿主 Bash
    - fetch/web-fetch 系(args.url)     -> 翻译成 curl 命令给宿主 Bash(宿主通常没有原生 fetch 工具)
    返回 {'id','name','arguments'(str)} 或 None(无可桥接时走文本转译)。"""
    ut = agent_json.get('tool')
    if not isinstance(ut, dict) or not openai_body.get('tools'):
        return None
    args = ut.get('args') or {}
    cmd = args.get('command')
    if not cmd and args.get('url'):
        # fetch 调用翻译成 curl(GitHub 等站点需要 UA 和 -L 跟随跳转)
        maxlen = args.get('maxLength') or 10000
        cmd = f"curl -sL -A 'Mozilla/5.0' '{args['url']}' | head -c {int(maxlen)}"
    if not cmd:
        return None
    tname, akey = pick_exec_tool(openai_body['tools'])
    if not tname:
        return None
    return {'id': 'call_' + uuid.uuid4().hex[:24], 'name': tname,
            'arguments': json.dumps({akey: cmd}, ensure_ascii=False)}


def try_unwrap_agent_json(full_text):
    """模型常输出 ```json {"action":"...","taskCompletion":"..."} ```,
    反代给其他 agent 用时应剥掉这层壳,取用户可读的文本字段。
    变体: completion/ask 有 taskCompletion/question;
    review/thinking 直接带 thinking 或内嵌 tool 调用(OpenAI 客户端无法执行,
    降级为"想执行什么命令"的可读说明)。"""
    o = parse_agent_json(full_text)
    if o is None:
        return None
    for k in ('taskCompletion', 'question', 'answer', 'content', 'result'):
        if o.get(k):
            return o[k]
    tool = o.get('tool')
    if isinstance(tool, dict):
        cmd = tool.get('args', {}).get('command') or tool.get('description') or json.dumps(tool.get('args', {}), ensure_ascii=False)
        thinking = o.get('thinking') or ''
        head = f'{thinking}\n\n' if thinking else ''
        return f'{head}我准备执行以下命令:\n\n```bash\n{cmd}\n```\n\n(由反代从上游工具调用转译;请确认后自行执行或让宿主 agent 执行)'
    if o.get('thinking'):
        return o['thinking']
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
        # OpenAI 客户端走 Authorization: Bearer;Anthropic SDK 走 x-api-key
        if self.headers.get('Authorization', '') == f'Bearer {API_KEY}':
            return True
        return self.headers.get('x-api-key', '') == API_KEY

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
        handlers = {
            '/v1/chat/completions': self._handle_openai_chat,
            '/v1/messages': self._handle_anthropic_messages,
        }
        handler = handlers.get(self.path)
        if handler is None:
            return self._json(404, {'error': {'message': 'not found'}})
        if not self._authed():
            return self._json(401, {'error': {'message': 'bad api key'}})
        length = int(self.headers.get('Content-Length', 0))
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except json.JSONDecodeError:
            return self._json(400, {'error': {'message': 'invalid json'}})
        return handler(body)

    def _handle_openai_chat(self, openai_body):
        return self._proxy_chat(openai_body)

    def _handle_anthropic_messages(self, anth_body):
        # Anthropic 请求 -> 内部 OpenAI 形态,复用同一条上游链路;错误也按 Anthropic 格式回
        internal = anthropic_compat.anthropic_to_internal(anth_body)
        return self._proxy_chat(internal, protocol='anthropic')

    def _proxy_chat(self, openai_body, protocol='openai'):
        """协议无关的聊天主链路:上游请求、剥壳、桥接、流式探测全在这里;
        出口由 protocol 决定用哪种 SSE/响应格式。"""
        err_fmt = (lambda code, msg: self._json(code, {'type': 'error', 'error': {'type': 'api_error', 'message': msg}})
                   if protocol == 'anthropic' else self._json)
        try:
            resp = open_upstream(openai_body)
        except urllib.error.HTTPError as e:
            return err_fmt(502, f'upstream {e.code}: {e.read().decode(errors="replace")[:300]}')
        except FileNotFoundError:
            return err_fmt(503, 'data.bin not found — 先安装并登录 OrcaTerm')
        except Exception as e:
            return err_fmt(502, f'upstream error: {e}')

        model_name = openai_body.get('model', DEFAULT_MODEL)
        stream = bool(openai_body.get('stream'))
        cid = 'chatcmpl-' + uuid.uuid4().hex[:24]
        created = int(time.time())

        if not stream:
            content, reasoning = '', ''
            for ev in iter_sse(resp, watchdog=UPSTREAM_TIMEOUT):
                c, r = extract_text(ev)
                content += c
                reasoning += r
            message = {'role': 'assistant', 'content': content, 'reasoning_content': reasoning or None}
            finish = 'stop'
            agent_json = parse_agent_json(content)
            if agent_json is not None:
                bridged = bridge_tool_call(agent_json, openai_body)
                if bridged is not None:
                    # 客户端声明了 tools:翻译成标准 tool_calls,宿主 agent 会自己执行并回传结果
                    message = {'role': 'assistant', 'content': agent_json.get('thinking') or None,
                               'tool_calls': [{'id': bridged['id'], 'type': 'function',
                                               'function': {'name': bridged['name'],
                                                            'arguments': bridged['arguments']}}]}
                    finish = 'tool_calls'
                else:
                    unwrapped = try_unwrap_agent_json(content)
                    if unwrapped is not None:
                        message['content'] = unwrapped
            if protocol == 'anthropic':
                tc = (message.get('tool_calls') or [None])[0]
                tool_call = ({'id': tc['id'], 'name': tc['function']['name'],
                              'arguments': tc['function']['arguments']} if tc else None)
                anth = anthropic_compat.internal_to_anthropic_response(
                    model_name, agent_json.get('thinking') if agent_json else (reasoning or None),
                    message.get('content') or '', tool_call, finish)
                return self._json(200, anth)
            return self._json(200, {
                'id': cid, 'object': 'chat.completion', 'created': created,
                'model': model_name,
                'choices': [{'index': 0, 'finish_reason': finish, 'message': message}],
                'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0},
            })

        # ---- streaming ----
        self.close_connection = True  # 无 Content-Length,流结束靠断开连接通知客户端
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()

        def sse_chunk(delta_content=None, delta_reasoning=None, finish=None, tool_calls=None):
            delta = {}
            if delta_content is not None:
                delta['content'] = delta_content
            if delta_reasoning is not None:
                delta['reasoning_content'] = delta_reasoning
            if tool_calls is not None:
                delta['tool_calls'] = tool_calls
            chunk = {
                'id': cid, 'object': 'chat.completion.chunk',
                'created': created, 'model': model_name,
                'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}],
            }
            return ('data: ' + json.dumps(chunk, ensure_ascii=False) + '\n\n').encode()

        # 协议出口:openai 用 chunk 流;anthropic 用事件流发射器
        if protocol == 'anthropic':
            astream = anthropic_compat.AnthropicStream(model_name)

        def emit(payloads):
            for p in payloads or []:
                self.wfile.write(p)

        def emit_text(delta):
            if protocol == 'anthropic':
                emit(astream.text_delta(delta))
            else:
                self.wfile.write(sse_chunk(delta_content=delta))

        def emit_thinking(delta):
            if protocol == 'anthropic':
                emit(astream.thinking_delta(delta))
            else:
                self.wfile.write(sse_chunk(delta_reasoning=delta))

        try:
            # 流式策略:纯文本(_FMT_GUARD 生效后是常态)逐 chunk 实时透传;
            # 但壳标志可能在文本中间出现(模型先说话再吐 ```json {"action":...}```),
            # passthrough 期间持续探测,发现即转攒壳。攒壳期间发 SSE 注释行心跳。
            full_content, full_reasoning = '', ''
            sent = 0              # passthrough 已实际发出的字符数;转 buffer 后 = 壳起点(壳从这之后切)
            mode = 'probe'        # probe -> passthrough | buffer;passthrough -可-> buffer
            last_beat = time.time()
            if protocol == 'anthropic':
                # 首事件 message_start 必须最先发
                emit(astream.start())
                self.wfile.flush()
            for ev in iter_sse(resp, watchdog=UPSTREAM_TIMEOUT):
                now = time.time()
                if now - last_beat > HEARTBEAT_SEC:
                    # Anthropic 心跳用官方 ping 事件;OpenAI 用注释行
                    self.wfile.write(astream.ping() if protocol == 'anthropic' else b': heartbeat\n\n')
                    self.wfile.flush()
                    last_beat = now
                c, r = extract_text(ev)
                if r:
                    full_reasoning += r
                    emit_thinking(r)
                    self.wfile.flush()
                    last_beat = now
                if not c:
                    continue
                full_content += c
                if mode == 'probe':
                    # 攒到能判断为止:非空白首字符 + 内容够长(避免 {" 或 ```json 被截断误判)
                    if full_content.strip():
                        if looks_like_agent_json(full_content) and len(full_content.strip()) < 24:
                            continue  # 还不能确定,继续攒
                        mode = 'buffer' if looks_like_agent_json(full_content) else 'passthrough'
                elif mode == 'passthrough':
                    m = _SHELL_MARK_RE.search(full_content, sent)  # 只查未发出部分,标志跨 chunk 截断不漏头
                    if m:
                        # 中途冒出壳标志(```json / { "action" / <tool_call):转攒壳。
                        cut = m.start()
                        # 先补发标志之前、还没发出的正文
                        emit_text(full_content[sent:cut])
                        self.wfile.flush()
                        last_beat = now
                        sent = cut
                        mode = 'buffer'
                        continue
                if mode == 'passthrough':
                    # 尾部扣住可能的截断标志头(24 字符 = 最长标志 <tool_call>json 长度余量),标志补全前不发
                    emit_upto = len(full_content) - 24 if len(full_content) > 24 else 0
                    if emit_upto > sent:
                        emit_text(full_content[sent:emit_upto])
                        self.wfile.flush()
                        last_beat = now
                        sent = emit_upto
            if mode == 'passthrough':
                if len(full_content) > sent:  # 补发尾部扣住的截断余量
                    emit_text(full_content[sent:])
                if protocol == 'anthropic':
                    for p in astream.finish('stop'):
                        self.wfile.write(p)
                else:
                    self.wfile.write(sse_chunk(finish='stop'))
            else:
                # 壳模式:正文前缀(若有)已透传;壳部分剥完一次性发(tool_calls 桥接或纯文本)
                shell_text = full_content[sent:]
                agent_json = parse_agent_json(shell_text)
                bridged = bridge_tool_call(agent_json, openai_body) if agent_json is not None else None
                if bridged is not None:
                    if agent_json.get('thinking'):
                        emit_text(agent_json['thinking'])
                    if protocol == 'anthropic':
                        for p in astream.tool_use(bridged['id'], bridged['name'], bridged['arguments']):
                            self.wfile.write(p)
                        for p in astream.finish('tool_calls'):
                            self.wfile.write(p)
                    else:
                        self.wfile.write(sse_chunk(tool_calls=[{
                            'index': 0, 'id': bridged['id'], 'type': 'function',
                            'function': {'name': bridged['name'], 'arguments': bridged['arguments']}}]))
                        self.wfile.write(sse_chunk(finish='tool_calls'))
                else:
                    unwrapped = try_unwrap_agent_json(shell_text)
                    emit_text(unwrapped if unwrapped is not None else shell_text)
                    if protocol == 'anthropic':
                        for p in astream.finish('stop'):
                            self.wfile.write(p)
                    else:
                        self.wfile.write(sse_chunk(finish='stop'))
            if protocol != 'anthropic':
                self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass  # 客户端断开


def main():
    port = int(os.environ.get('PORT', '8117'))
    srv = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print(f'OrcaTerm proxy (OpenAI + Anthropic compatible) on http://127.0.0.1:{port}')
    print(f'endpoints: /v1/chat/completions /v1/messages /v1/models')
    print(f'models: {", ".join(MODELS)}')
    print(f'auth: {"API_KEY required" if API_KEY else "no auth (local only)"}')
    print(f'inject: {"on (%d chars)" % len(INJECT_SYSTEM) if INJECT_SYSTEM else "off"}')
    srv.serve_forever()


if __name__ == '__main__':
    main()
