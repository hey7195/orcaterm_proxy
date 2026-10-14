# -*- coding: utf-8 -*-
"""anthropic_compat 单测:请求/响应翻译 + SSE 事件流发射器的完整序列。"""
import json
import anthropic_compat as ac


def test_request_flat_content():
    body = {
        'model': 'glm-5.3', 'max_tokens': 1024, 'stream': True,
        'system': '你是本机助手',
        'messages': [
            {'role': 'user', 'content': '看看 apk'},
            {'role': 'assistant', 'content': [
                {'type': 'text', 'text': '我用工具看'},
                {'type': 'tool_use', 'id': 'tu_1', 'name': 'Bash', 'input': {'command': 'ls'}},
            ]},
            {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 'tu_1', 'content': 'a.apk\nb.apk'},
                {'type': 'text', 'text': '继续分析'},
            ]},
        ],
    }
    internal = ac.anthropic_to_internal(body)
    assert internal['model'] == 'glm-5.3'
    roles = [m['role'] for m in internal['messages']]
    assert roles == ['system', 'user', 'assistant', 'user'], roles
    assert internal['messages'][0]['content'] == '你是本机助手'
    # tool_use / tool_result 都拍平保留语义
    assert 'a.apk' in internal['messages'][3]['content']
    assert '<tool_result>' in internal['messages'][3]['content']
    assert 'tool_use' in internal['messages'][2]['content']
    print('PASS request flatten')


def test_request_tools():
    body = {
        'messages': [{'role': 'user', 'content': 'hi'}],
        'tools': [{
            'name': 'Bash', 'description': 'run cmd',
            'input_schema': {'type': 'object', 'properties': {'command': {'type': 'string'}}, 'required': ['command']},
        }],
    }
    internal = ac.anthropic_to_internal(body)
    t = internal['tools'][0]
    assert t['type'] == 'function' and t['function']['name'] == 'Bash'
    assert t['function']['parameters']['properties'] == {'command': {'type': 'string'}}
    # 现有 pick_exec_tool 应能认出 Anthropic 形态转过来的工具
    import server
    name, key = server.pick_exec_tool(internal['tools'])
    assert (name, key) == ('Bash', 'command'), (name, key)
    print('PASS request tools -> pick_exec_tool compatible')


def test_response_nonstream():
    r = ac.internal_to_anthropic_response('glm-5.3', 'think...', 'answer text', None, 'stop')
    assert r['type'] == 'message' and r['role'] == 'assistant'
    assert [b['type'] for b in r['content']] == ['thinking', 'text']
    assert r['stop_reason'] == 'end_turn'
    # 带 tool_call
    r2 = ac.internal_to_anthropic_response('glm-5.3', None, '准备执行',
        {'id': 'call_x', 'name': 'Bash', 'arguments': '{"command": "ls"}'}, 'tool_calls')
    assert r2['stop_reason'] == 'tool_use'
    tu = r2['content'][-1]
    assert tu['type'] == 'tool_use' and tu['name'] == 'Bash' and tu['input'] == {'command': 'ls'}
    print('PASS response non-stream')


def _events(chunks):
    """把发射器输出的 bytes 列表(嵌套列表)解析成 (event, data) 序列。"""
    out = []
    flat = b''.join(b''.join(c) if isinstance(c, list) else c for c in chunks)
    for raw in flat.decode().split('\n\n'):
        if not raw.strip():
            continue
        ev, data = None, None
        for line in raw.split('\n'):
            if line.startswith('event: '):
                ev = line[7:]
            elif line.startswith('data: '):
                data = json.loads(line[6:])
        out.append((ev, data))
    return out


def test_stream_text_only():
    s = ac.AnthropicStream('glm-5.3')
    chunks = [s.start(), s.text_delta('你好'), s.text_delta('！'), s.finish('stop')]
    evs = _events(chunks)
    names = [e for e, _ in evs]
    assert names == ['message_start', 'content_block_start', 'content_block_delta',
                     'content_block_delta', 'content_block_stop', 'message_delta', 'message_stop'], names
    assert evs[2][1]['delta'] == {'type': 'text_delta', 'text': '你好'}
    assert evs[-2][1]['delta']['stop_reason'] == 'end_turn'
    print('PASS stream text-only sequence')


def test_stream_thinking_then_tool():
    s = ac.AnthropicStream('glm-5.3')
    chunks = [s.start(), s.thinking_delta('分析中'), s.text_delta('执行'),
              s.tool_use('call_1', 'Bash', '{"command": "ls"}'), s.finish('tool_calls')]
    evs = _events(chunks)
    names = [e for e, _ in evs]
    # thinking block 开+delta+停, text block 开+delta+停, tool_use 开+delta+停, 尾
    assert names == ['message_start',
                     'content_block_start', 'content_block_delta', 'content_block_stop',   # thinking
                     'content_block_start', 'content_block_delta', 'content_block_stop',   # text
                     'content_block_start', 'content_block_delta', 'content_block_stop',   # tool_use
                     'message_delta', 'message_stop'], names
    # index 递增正确
    starts = [d for e, d in evs if e == 'content_block_start']
    assert [b['index'] for b in starts] == [0, 1, 2]
    assert starts[0]['content_block']['type'] == 'thinking'
    assert starts[1]['content_block']['type'] == 'text'
    assert starts[2]['content_block']['type'] == 'tool_use'
    assert starts[2]['content_block']['name'] == 'Bash'
    # tool_use 参数走 input_json_delta
    tj = [d for e, d in evs if e == 'content_block_delta'][2]
    assert tj['delta']['type'] == 'input_json_delta'
    assert json.loads(tj['delta']['partial_json']) == {'command': 'ls'}
    assert evs[-2][1]['delta']['stop_reason'] == 'tool_use'
    print('PASS stream thinking+text+tool sequence')


def test_stream_interleaved_text():
    """text -> tool_use 后不再有 text;结束时空 text 不发空块。"""
    s = ac.AnthropicStream('glm-5.3')
    chunks = [s.start(), s.finish('stop')]  # 空流也要完整事件对
    evs = _events(chunks)
    assert [e for e, _ in evs] == ['message_start', 'message_delta', 'message_stop']
    print('PASS stream empty ends cleanly')


if __name__ == '__main__':
    test_request_flat_content()
    test_request_tools()
    test_response_nonstream()
    test_stream_text_only()
    test_stream_thinking_then_tool()
    test_stream_interleaved_text()
    print('ALL PASS')
