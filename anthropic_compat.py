# -*- coding: utf-8 -*-
"""Anthropic Messages 协议兼容层 —— 纯函数翻译,不碰网络。

请求方向: /v1/messages (Anthropic) -> 内部规范体(与 OpenAI chat 同构),
复用 server.build_upstream_body / bridge_tool_call / 剥壳等全部既有机制。
响应方向: 内部结果 -> Anthropic content blocks / SSE 事件流。
"""
import json

# ---- 请求翻译: Anthropic -> 内部(OpenAI 形态) ----

def _flatten_content(content):
    """Anthropic content: str 或 block 列表 -> 拍平成纯文本。
    tool_result 的内容带 <tool_result> 标记保留上下文语义。"""
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        t = b.get('type')
        if t == 'text':
            parts.append(b.get('text') or '')
        elif t == 'tool_result':
            inner = _flatten_content(b.get('content'))
            parts.append(f'<tool_result>\n{inner}\n</tool_result>')
        elif t == 'tool_use':
            parts.append(f'<tool_use name="{b.get("name")}">{json.dumps(b.get("input"), ensure_ascii=False)}</tool_use>')
        elif t == 'thinking':
            pass  # 历史思考不回灌
    return '\n'.join(p for p in parts if p)


def anthropic_to_internal(body):
    """Anthropic Messages 请求 -> OpenAI chat 形态的内部体。
    model 缺省 glm-5.3(上游实测可用);tools 转 OpenAI function 格式。"""
    sys_text = body.get('system')
    if isinstance(sys_text, list):  # system 也可能是 block 列表
        sys_text = '\n'.join(b.get('text', '') for b in sys_text if isinstance(b, dict))
    msgs = []
    if sys_text:
        msgs.append({'role': 'system', 'content': sys_text})
    for m in body.get('messages') or []:
        role = 'assistant' if m.get('role') == 'assistant' else 'user'
        msgs.append({'role': role, 'content': _flatten_content(m.get('content'))})
    tools = [{'type': 'function', 'function': {
        'name': t.get('name'),
        'description': t.get('description') or '',
        'parameters': t.get('input_schema') or {'type': 'object', 'properties': {}},
    }} for t in body.get('tools') or [] if isinstance(t, dict) and t.get('name')]
    internal = {'model': body.get('model') or 'glm-5.3',
                'messages': msgs, 'tools': tools, 'stream': bool(body.get('stream'))}
    if body.get('max_tokens'):
        internal['max_tokens'] = body['max_tokens']
    return internal


# ---- 响应翻译: 内部结果 -> Anthropic ----

def _stop_reason(openai_finish):
    return 'tool_use' if openai_finish == 'tool_calls' else 'end_turn'


def internal_to_anthropic_response(model, thinking, text, tool_call, stop_reason):
    """非流式:内部结果(剥壳后) -> Anthropic Messages 响应。
    tool_call: {'id','name','arguments'(str)} 或 None。"""
    content = []
    if thinking:
        content.append({'type': 'thinking', 'thinking': thinking})
    if text or not content:
        content.append({'type': 'text', 'text': text or ''})
    if tool_call is not None:
        try:
            input_obj = json.loads(tool_call['arguments'])
        except (json.JSONDecodeError, TypeError):
            input_obj = {}
        content.append({'type': 'tool_use', 'id': tool_call['id'],
                        'name': tool_call['name'], 'input': input_obj})
    return {
        'id': 'msg_' + (tool_call['id'] if tool_call else 'stop')[:20],
        'type': 'message', 'role': 'assistant', 'model': model,
        'content': content,
        'stop_reason': _stop_reason(stop_reason), 'stop_sequence': None,
        'usage': {'input_tokens': 0, 'output_tokens': 0},
    }


class AnthropicStream:
    """内部结果 -> Anthropic SSE 事件流发射器。
    事件序列: message_start -> (thinking/text/tool_use blocks with deltas)
    -> message_delta(stop_reason) -> message_stop。"""
    def __init__(self, model):
        self.model = model
        self.msg_id = 'msg_'
        self._started = False
        self._block_idx = -1
        self._open_type = None

    def _new_id(self):
        import uuid
        return 'msg_' + uuid.uuid4().hex[:20]

    def start(self):
        self.msg_id = self._new_id()
        self._started = True
        return [self._emit('message_start', {
            'type': 'message_start',
            'message': {'id': self.msg_id, 'type': 'message', 'role': 'assistant',
                        'model': self.model, 'content': [], 'stop_reason': None,
                        'stop_sequence': None,
                        'usage': {'input_tokens': 0, 'output_tokens': 0}},
        })]

    def _emit(self, event, data):
        return (f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n').encode()

    def _ensure_block(self, btype):
        if self._open_type != btype:
            if self._open_type is not None:
                out = [self._emit('content_block_stop', {
                    'type': 'content_block_stop', 'index': self._block_idx})]
            else:
                out = []
            self._block_idx += 1
            self._open_type = btype
            content = ({'type': 'thinking', 'thinking': ''} if btype == 'thinking'
                       else {'type': 'text', 'text': ''} if btype == 'text'
                       else {'type': 'tool_use', 'id': '', 'name': '', 'input': {}})
            out.append(self._emit('content_block_start', {
                'type': 'content_block_start', 'index': self._block_idx,
                'content_block': content}))
            return out
        return []

    def text_delta(self, delta):
        out = list(self._ensure_block('text'))
        out.append(self._emit('content_block_delta', {
            'type': 'content_block_delta', 'index': self._block_idx,
            'delta': {'type': 'text_delta', 'text': delta}}))
        return out

    def thinking_delta(self, delta):
        out = list(self._ensure_block('thinking'))
        out.append(self._emit('content_block_delta', {
            'type': 'content_block_delta', 'index': self._block_idx,
            'delta': {'type': 'thinking_delta', 'thinking': delta}}))
        return out

    def tool_use(self, tc_id, name, arguments):
        """工具调用:先闭合当前 text/thinking 块,再发独立 tool_use 块。
        arguments(str)按 Anthropic 惯例以 partial_json 发。"""
        out = []
        if self._open_type is not None:
            out.append(self._emit('content_block_stop', {
                'type': 'content_block_stop', 'index': self._block_idx}))
            self._open_type = None
        self._block_idx += 1
        out.append(self._emit('content_block_start', {
            'type': 'content_block_start', 'index': self._block_idx,
            'content_block': {'type': 'tool_use', 'id': tc_id, 'name': name, 'input': {}}}))
        out.append(self._emit('content_block_delta', {
            'type': 'content_block_delta', 'index': self._block_idx,
            'delta': {'type': 'input_json_delta', 'partial_json': arguments}}))
        out.append(self._emit('content_block_stop', {
            'type': 'content_block_stop', 'index': self._block_idx}))
        return out

    def finish(self, stop_reason):
        out = []
        if self._open_type is not None:
            out.append(self._emit('content_block_stop', {
                'type': 'content_block_stop', 'index': self._block_idx}))
            self._open_type = None
        out.append(self._emit('message_delta', {
            'type': 'message_delta',
            'delta': {'stop_reason': _stop_reason(stop_reason), 'stop_sequence': None},
            'usage': {'output_tokens': 0}}))
        out.append(self._emit('message_stop', {'type': 'message_stop'}))
        return out

    @staticmethod
    def ping():
        return (b'event: ping\ndata: {"type": "ping"}\n\n')
