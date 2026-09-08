#!/usr/bin/env python3
"""Exercise an installed Side Dog wheel in a disposable Linux environment.

Run with the installed wheel's Python, outside the source tree. Optional
Playwright checks use an installed Chromium browser. Only synthetic data is used.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import urllib.request


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=30)


def terminal(args: list[str], root: Path, *, color: bool = False) -> None:
    master, slave = pty.openpty()
    before = termios.tcgetattr(slave)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 100, 0, 0))
    process = subprocess.Popen(args, cwd=root, stdin=slave, stdout=slave, stderr=slave)
    output = bytearray()

    def drain(seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.05)[0]:
                output.extend(os.read(master, 65536))

    try:
        drain(4)
        assert process.poll() is None, 'interactive process exited during startup'
        for width in (42, 28, 100):
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 16, width, 0, 0))
            process.send_signal(signal.SIGWINCH)
            drain(0.4)
        for key in (b'?', b'\x1b', b'v', b'\x1b', b'\x1b[B', b'\x1b[A'):
            os.write(master, key)
            drain(0.6)
        os.write(master, b'q')
        deadline = time.monotonic() + 20
        while process.poll() is None and time.monotonic() < deadline:
            drain(0.2)
        assert process.wait(timeout=1) == 0, output.decode('utf-8', errors='replace')[-6000:]
        assert termios.tcgetattr(slave) == before, 'terminal settings were not restored'
        text = output.decode('utf-8', errors='strict')
        assert 'Traceback' not in text, 'interactive traceback'
        assert len(text) > 100, 'no terminal frame'
        if not color:
            assert not re.search(r'\x1b\[[\d;]*m', text), 'color emitted with --no-color'
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
        os.close(slave)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', action='store_true', help='also run Chromium via Playwright')
    args = parser.parse_args()
    assert sys.platform == 'linux', 'Linux only'
    executable = shutil.which('side-dog')
    assert executable, 'installed side-dog must be on PATH'
    import side_dog
    from side_dog.cli import append_event

    print(json.dumps({'os': Path('/etc/os-release').read_text(), 'python': sys.version.split()[0],
                      'version': side_dog.__version__, 'module': side_dog.__file__,
                      'terminal': 'Linux PTY, TERM=xterm-256color, UTF-8',
                      'display': os.environ.get('DISPLAY', 'headless')}, indent=2), flush=True)
    if args.browser:
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(Path.home() / '.cache' / 'ms-playwright'))
    with tempfile.TemporaryDirectory(prefix='side-dog-linux-') as directory:
        base = Path(directory)
        root = base / 'project'
        root.mkdir()
        os.environ.update(HOME=str(base / 'home'), XDG_CONFIG_HOME=str(base / 'config'),
                          XDG_DATA_HOME=str(base / 'data'), SIDE_DOG_STATE_DIR=str(base / 'state'),
                          CODEX_HOME=str(base / 'codex'), TERM='xterm-256color', LANG='C.UTF-8')
        Path(os.environ['HOME']).mkdir()
        config = base / 'config' / 'side-dog'
        config.mkdir(parents=True)
        (config / 'config.toml').write_text('[notify]\nenabled = true\n')
        run('git', 'init', '-q', str(root))
        run('git', '-C', str(root), '-c', 'user.name=Smoke', '-c', 'user.email=smoke@example.invalid',
            'commit', '-qm', 'Synthetic root', '--allow-empty')
        run('git', '-C', str(root), 'worktree', 'add', '-qb', 'smoke-worktree', str(base / 'worktree'))
        event = {'agent': 'codex', 'kind': 'test', 'status': 'failed', 'title': 'Tests failed',
                 'detail': 'unittest'}
        append_event(root, event)
        assert 'Side Dog: Version' in run(executable, 'doctor', str(root), '--no-color')
        frame = run(executable, 'watch', str(root), '--once', '--no-color', '--github-poll', '0')
        assert 'Tests failed' in frame, 'failed event missing from Watch'
        assert '\x1b[' not in frame
        print('PASS isolated config/state, installed doctor, failed-test Watch frame', flush=True)
        for command in (
            ['watch', str(root), '--no-color', '--github-poll', '0'],
            ['watch', str(root), '--no-color', '--no-notify', '--github-poll', '0'],
            ['watch', '--no-color', '--github-poll', '0'],
            ['board', '--no-color', '--github-poll', '0'],
        ):
            terminal([executable, *command], root)
            print('PASS PTY startup/resize/keys/quit/restoration: ' + ' '.join(command[:1]), flush=True)
        terminal([executable, 'demo', '--watch', '--duration', '60'], root, color=True)
        print('PASS colored synthetic demo PTY', flush=True)
        process = subprocess.Popen([executable, 'panel', str(root), '--no-open', '--poll', '0.1'],
                                   cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert select.select([process.stdout], [], [], 30)[0], 'panel startup timed out'
            line = process.stdout.readline()
            url = re.search(r'http://127\.0\.0\.1:\d+/[^ ]+/', line).group(0)
            assert urllib.request.urlopen(url, timeout=5).status == 200
            if args.browser:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=not bool(os.environ.get('DISPLAY')),
                                                         args=['--no-sandbox'])
                    page = browser.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(url)
                    page.get_by_text('Tests failed', exact=False).first.wait_for(timeout=15000)
                    append_event(root, {'agent': 'codex', 'kind': 'test', 'status': 'success',
                                        'title': 'Tests passed', 'detail': 'unittest'})
                    page.get_by_text('Tests passed', exact=False).first.wait_for(timeout=15000)
                    page.goto(url + 'board')
                    page.locator('#connection').filter(has_text='live').wait_for(timeout=15000)
                    assert not errors, 'browser JavaScript errors'
                    browser.close()
                print('PASS Chromium timeline, live event update, Board SSE, no JS errors', flush=True)
            else:
                print('NOT TESTED browser rendering (rerun with --browser)', flush=True)
        finally:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise
        assert process.returncode == 0, 'panel shutdown failed'
        print('PASS panel local HTTP and clean shutdown', flush=True)
        print('BLOCKED real authenticated agent, physical desktop and SSH session require operator environments', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
