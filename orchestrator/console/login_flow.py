"""Browser authorization status; one-time URLs stay in memory, never in logs."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from urllib.parse import urlsplit

_lock = threading.Lock()
_states = {}


def valid_url(url):
    parsed = urlsplit(url)
    return parsed.scheme == 'https' and parsed.hostname == 'copilot.tencent.com'


def status(root):
    with _lock:
        return dict(_states.get(str(root), {'state': 'idle'}))


def start(root):
    key = str(root)
    with _lock:
        if _states.get(key, {}).get('state') in {'starting', 'waiting'}:
            return {'ok': True, **_states[key]}
        _states[key] = {'state': 'starting', 'message': '正在获取中国站授权链接…'}
    threading.Thread(target=_run, args=(root,), daemon=True).start()
    return {'ok': True, **status(root)}


def _run(root):
    def update(value):
        with _lock:
            _states[str(root)] = value
    python = root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    env = os.environ.copy()
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get('PYTHONPATH', '')
    env['CODEBUDDY_SKIP_GIT_BASH_CHECK'] = '1'
    try:
        with subprocess.Popen(
            [str(python) if python.is_file() else sys.executable, '-u', '-m',
             'orchestrator.console.login_flow', str(root)],
            cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        ) as process:
            for line in process.stdout:
                try:
                    event = json.loads(line)
                    state = event.get('state')
                    if state == 'waiting' and valid_url(event.get('url', '')):
                        update({'state': state, 'url': event['url'], 'message': '请打开授权页完成登录'})
                    elif state == 'completed':
                        update({'state': state, 'message': '登录已完成'})
                    elif state == 'failed':
                        update({'state': state, 'message': '认证失败或已超时，请重新授权'})
                except (ValueError, TypeError, AttributeError):
                    continue
            if status(root)['state'] != 'completed':
                update({'state': 'failed', 'message': '授权未完成：请检查项目 SDK、网络或重新授权'})
    except OSError:
        update({'state': 'failed', 'message': '无法启动授权程序，请检查项目运行环境'})


async def worker(root):
    flow = None
    def emit(**event):
        print(json.dumps(event), flush=True)
    try:
        from codebuddy_agent_sdk import authenticate
        from orchestrator.adapters.codebuddy_config import preferred_codebuddy_cli, codebuddy_china_environment
        flow = await asyncio.wait_for(authenticate(
            environment='internal', env=codebuddy_china_environment(),
            codebuddy_code_path=preferred_codebuddy_cli(root), timeout=300), 30)
        if flow.auth_url:
            if not valid_url(flow.auth_url):
                raise ValueError('Unexpected authorization host')
            emit(state='waiting', url=flow.auth_url)
        await asyncio.wait_for(flow.wait(), 300)
        emit(state='completed')
    except Exception:
        emit(state='failed')
    finally:
        if flow is not None:
            await flow.cancel()


if __name__ == '__main__':
    asyncio.run(worker(Path(sys.argv[1])))
