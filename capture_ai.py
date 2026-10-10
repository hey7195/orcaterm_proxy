# 监听 OrcaTerm WebView (CDP 9223) 的 AI 请求，抓完整请求头/请求体存 JSON
import json, asyncio, websockets, time, os

WS = 'ws://127.0.0.1:9223/devtools/page/8B486C171EC32B1F6CAD5B62F632753F'
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'captured_requests.json')

async def main():
    reqs = []
    extra_headers = {}   # requestId -> cookies 等（requestWillBeSentExtraInfo）
    async with websockets.connect(WS, max_size=10**7) as ws:
        await ws.send(json.dumps({'id': 1, 'method': 'Network.enable'}))
        print('listening...', flush=True)
        deadline = time.time() + 300  # 监听 5 分钟
        while time.time() < deadline:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print('ws closed:', e, flush=True)
                break
            m = msg.get('method')
            if m == 'Network.requestWillBeSent':
                r = msg['params']['request']
                u = r['url']
                if any(k in u for k in ('lightai', 'assistant', 'cgi/api', 'conversation', 'tool-agent', '/messages', '/chat')):
                    entry = {'ts': msg['params'].get('timestamp'), 'requestId': msg['params']['requestId'],
                             'url': u, 'method': r['method'], 'headers': r.get('headers', {}),
                             'postData': r.get('postData', '')[:2000]}
                    reqs.append(entry)
                    print('CAPTURED:', r['method'], u, flush=True)
            elif m == 'Network.requestWillBeSentExtraInfo':
                rid = msg['params']['requestId']
                h = msg['params'].get('headers', {})
                if any(k.lower() in ('cookie', 'x-uin', 'x-skey', 'authorization') for k in h):
                    extra_headers[rid] = h
            elif m == 'Network.responseReceived':
                resp = msg['params']['response']
                for e in reqs:
                    if e['requestId'] == msg['params']['requestId']:
                        e['status'] = resp.get('status')
                        e['responseHeaders'] = resp.get('headers', {})
    # 合并 extra info
    for e in reqs:
        if e['requestId'] in extra_headers:
            e['extraHeaders'] = extra_headers[e['requestId']]
    json.dump(reqs, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    print(f'done, {len(reqs)} requests -> {OUT}', flush=True)

asyncio.run(main())
