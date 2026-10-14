# -*- coding: utf-8 -*-
"""壳检测/剥壳/桥接抢救的单测。覆盖三种壳形态 + 实际遇到的坏 JSON 样本。"""
import json
import server


def test_shell_mark_regex():
    """中途壳标志必须能命中三种形态,含空格容错。"""
    cases_hit = [
        '前面有说明文字\n```json\n{"action": "review"}\n```',
        'winget 安装失败。\n{ "action": "review", "tool": {} }',   # 空格 + 无围栏
        '我先读安装日志。\n<tool_call>json { "action": "review" }</tool_call>',
        '{"action": "completion", "taskCompletion": "hi"}',          # 裸 JSON 开头
        '<tool_call>{ "action" ...',                                  # 截断中的 XML 标志
    ]
    for c in cases_hit:
        assert server._SHELL_MARK_RE.search(c), f'miss: {c[:50]!r}'
    cases_miss = [
        '纯文本回答,含大括号 { 但不是壳',
        '```bash\nls -la\n```',            # bash 围栏不是壳
        '普通 JSON 数组 [{"a": 1}]',
    ]
    for c in cases_miss:
        assert not server._SHELL_MARK_RE.search(c), f'false hit: {c[:50]!r}'
    print('PASS _SHELL_MARK_RE')


def test_looks_like():
    for c in ['{ "action"', '```json', '<tool_call>json {', '{"action"']:
        assert server.looks_like_agent_json(c), c
    # 注意:```bash 也命中(``` 到位时无法区分 json/bash,先攒壳,解析失败原文吐出)
    for c in ['纯文本', 'winget 安装失败']:
        assert not server.looks_like_agent_json(c), c
    print('PASS looks_like_agent_json')


def test_parse_xmp_wrapper():
    """XML 包裹 + 空格容错:严格 JSON 是合法的,应完整解析。"""
    raw = ('<tool_call>json { "action": "review", "thinking": "读日志", '
           '"tool": { "type": "mcp", "name": "execute_command", '
           '"args": { "command": "grep -i error C:\\\\msi.log" } } } </tool_call>')
    o = server.parse_agent_json(raw)
    assert o and o['action'] == 'review', o
    assert o['tool']['args']['command'] == 'grep -i error C:\\msi.log'
    print('PASS parse xml wrapper (valid json)')


def test_salvage_broken_json():
    """实际泄漏样本:command 值内嵌未转义引号 → 严格解析失败 → 容错抢救。"""
    raw = ('<tool_call>json { "action": "review", "thinking": "定位 MSI 失败原因", '
           '"tool": { "type": "mcp", "name": "execute_command", '
           '"args": { "command": "LOG="/c/Users/hy/AppData/Local/Package/Microsoft.DesktopAppInstaller_8wekyb3d8bbwe/Debug/install.log" && ls -la "LOG" && tail -100 "LOG" } } } </tool_call>')
    o = server.parse_agent_json(raw)
    assert o, 'salvage returned None'
    assert o['action'] == 'review'
    cmd = o['tool']['args']['command']
    assert 'install.log' in cmd and 'tail -100' in cmd, cmd
    assert cmd.startswith('LOG='), cmd
    # 桥接应产出可执行的 tool_calls
    body = {'tools': [{'type': 'function', 'function': {
        'name': 'Bash', 'parameters': {'properties': {'command': {}}}}}]}
    b = server.bridge_tool_call(o, body)
    assert b and b['name'] == 'Bash'
    assert json.loads(b['arguments'])['command'] == cmd
    print('PASS salvage broken json + bridge')


def test_unwrap_completion():
    raw = '```json\n{"action": "completion", "taskCompletion": "搞定。"}\n```'
    assert server.try_unwrap_agent_json(raw) == '搞定。'
    raw2 = '<tool_call>json { "action": "ask", "question": "继续吗?" } </tool_call>'
    assert server.try_unwrap_agent_json(raw2) == '继续吗?'
    print('PASS unwrap')


def test_plain_text_untouched():
    """纯文本不误伤。"""
    assert server.parse_agent_json('就是普通回答,没有壳') is None
    assert server.try_unwrap_agent_json('```bash\nls\n```') is None
    print('PASS plain text')


def test_salvage_multishell_repetition():
    """实际泄漏样本(2026-10-14):上游复读上轮壳+幻觉标签+本轮壳截断,
    三段拼接。旧实现贪心跨壳抓 command,把 "}}} </arg_value>{"action"... 垃圾
    全吞进命令,桥接出必败命令(退出码 49)且 thinking 残渣泄漏成正文。"""
    raw = ('我来查看当前安装的 MCP 配置。用户想查看当前安装了哪些 MCP。zcode 的 MCP 配置通常存放在'
           '用户目录的 .zcode 配置文件中，我先读取配置文件来获取已安装的 MCP 列表。", "tool": {"type": "mcp", '
           '"mcpServer": "mcp-server-orcaterm-oauth", "name": "execute_command", "description": "读取配置", '
           '"args": {"command": "cat ~/.zcode/mcp.json 2>/dev/null || ls ~/.zcode/*.json 2>/dev/null"}}} '
           '</arg_value>{"action": "review", "thinking": "您想查看当前安装了哪些 MCP。我先读取本机 zcode')
    o = server.parse_agent_json(raw)
    assert o, 'multi-shell salvage returned None'
    cmd = o['tool']['args']['command']
    assert cmd == 'cat ~/.zcode/mcp.json 2>/dev/null || ls ~/.zcode/*.json 2>/dev/null', cmd
    # 桥接产出可执行 tool_calls
    body = {'tools': [{'type': 'function', 'function': {
        'name': 'Bash', 'parameters': {'properties': {'command': {}}}}}]}
    b = server.bridge_tool_call(o, body)
    assert b and json.loads(b['arguments'])['command'] == cmd
    print('PASS salvage multi-shell repetition')


def test_clean_command_allows_redirect():
    """洁净度检查不能误伤 shell 重定向(2>/dev/null、>、heredoc)。"""
    assert server._is_clean_command('cat a.log 2>/dev/null || ls *.json 2>/dev/null')
    assert server._is_clean_command('tail -100 "LOG"')
    assert not server._is_clean_command('cmd"}}} </arg_value>{"action"')
    assert not server._is_clean_command('echo {action}')
    print('PASS clean command redirect allowance')


if __name__ == '__main__':
    test_shell_mark_regex()
    test_looks_like()
    test_parse_xmp_wrapper()
    test_salvage_broken_json()
    test_salvage_multishell_repetition()
    test_clean_command_allows_redirect()
    test_unwrap_completion()
    test_plain_text_untouched()
    print('ALL PASS')
