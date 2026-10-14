"""--web: Chrome/Edge pages through the DevTools protocol. A fake browser on a local socket and mocked relay calls;
no real browser, no user settings, no network."""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import atticus


def frame(data, op=1):
    """A server frame (servers never mask)."""
    n = len(data)
    size = bytes([n]) if n < 126 else bytes([126]) + n.to_bytes(2, 'big') if n < 65536 else bytes([127]) + n.to_bytes(8, 'big')
    return bytes([0x80 | op]) + size + data


class FakeBrowser:
    """Speaks just enough of the DevTools WebSocket: handshake, masked client frames, events and pings between answers."""
    def __init__(self, page=lambda expr: 'page text'):
        self.page, self.calls = page, []
        self.srv = socket.socket()
        self.srv.bind(('127.0.0.1', 0)); self.srv.listen(1)
        self.url = f'ws://127.0.0.1:{self.srv.getsockname()[1]}/devtools/browser/x'
        threading.Thread(target=self.serve, daemon=True).start()

    def read(self, c, n):
        out = b''
        while len(out) < n:
            b = c.recv(n - len(out))
            if not b:
                raise ConnectionError
            out += b
        return out

    def serve(self):
        c, _ = self.srv.accept()
        head = b''
        while b'\r\n\r\n' not in head:
            head += c.recv(1)
        key = next(l.split(b':', 1)[1].strip() for l in head.split(b'\r\n') if l.lower().startswith(b'sec-websocket-key'))
        accept = base64.b64encode(hashlib.sha1(key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
        c.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
        try:
            while True:
                b0, b1 = self.read(c, 2)
                assert b1 & 0x80, 'client frames must be masked'
                n = b1 & 0x7f
                n = int.from_bytes(self.read(c, 2), 'big') if n == 126 else int.from_bytes(self.read(c, 8), 'big') if n == 127 else n
                mask = self.read(c, 4)
                data = bytes(x ^ mask[i % 4] for i, x in enumerate(self.read(c, n)))
                if b0 & 0x0f != 1:
                    continue   # pongs
                m = json.loads(data)
                self.calls.append(m)
                c.sendall(frame(json.dumps({'method': 'Target.targetInfoChanged', 'params': {}}).encode()) + frame(b'hi', 9))   # an event and a ping first
                if m['method'] == 'Target.getTargets':
                    r = {'targetInfos': [{'targetId': 'T1', 'type': 'page', 'title': 'Fixture', 'url': 'https://fixture.invalid/'},
                                         {'targetId': 'W1', 'type': 'service_worker', 'title': '', 'url': 'https://fixture.invalid/sw.js'}]}
                elif m['method'] == 'Target.attachToTarget':
                    r = {'sessionId': 'S1'}
                elif m['method'] == 'Runtime.evaluate':
                    r = {'result': {'type': 'string', 'value': self.page(m['params']['expression'])}}
                else:
                    c.sendall(frame(json.dumps({'id': m['id'], 'error': {'message': 'nope'}}).encode()))
                    continue
                c.sendall(frame(json.dumps({'id': m['id'], 'result': r}).encode()))
        except (ConnectionError, OSError):
            pass


class WebTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.prefs = {}
        for p in (patch.object(atticus, 'DIR', root.name), patch.object(atticus, 'pref', side_effect=lambda k, d: self.prefs.get(k, d)),
                  patch.object(atticus.winctl, 'find', return_value=[]), patch.object(atticus.winctl, 'notify'),
                  patch.dict(os.environ, {'ATTICUS_ASK': '', 'ATTICUS_FRONT': 'Google Chrome', 'LOCALAPPDATA': root.name})):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop('ATTICUS_ASK')

    def test_websocket_client_skips_events_answers_pings_and_handles_long_messages(self):
        long = 'x' * 70000   # a 64-bit length frame both ways
        fake = FakeBrowser(lambda expr: expr)
        c = atticus.Cdp(fake.url, 5)
        self.assertEqual(c.call('Target.getTargets')['targetInfos'][0]['targetId'], 'T1')
        self.assertEqual(c.call('Runtime.evaluate', {'expression': long}, 'S1')['result']['value'], long)
        self.assertEqual(fake.calls[-1]['sessionId'], 'S1')
        with self.assertRaisesRegex(RuntimeError, 'nope'):
            c.call('Page.crash')
        c.close()

    def test_relay_holds_one_connection_and_answers_targets_and_eval(self):
        fake = FakeBrowser(lambda expr: 'evaluated ' + expr)
        with patch.object(atticus, 'WEB_IDLE', 3):
            t = threading.Thread(target=atticus.web_relay, args=('Google Chrome', fake.url), daemon=True)
            t.start()
            for _ in range(100):
                if os.path.exists(atticus.relay_file('Google Chrome')):
                    break
                threading.Event().wait(0.05)
            self.assertEqual(atticus.cdp('Google Chrome', 'targets'), [{'id': 'T1', 'title': 'Fixture', 'url': 'https://fixture.invalid/'}])
            self.assertEqual(atticus.cdp('Google Chrome', 'eval', target='T1', expr='1+1'), 'evaluated 1+1')
            self.assertEqual(atticus.cdp('Google Chrome', 'eval', target='T1', expr='2'), 'evaluated 2')
            self.assertEqual(sum(m['method'] == 'Target.attachToTarget' for m in fake.calls), 1)   # one session per tab, kept
            self.assertTrue(all(m['params'].get('userGesture') for m in fake.calls if m['method'] == 'Runtime.evaluate'))
            t.join(6)
        self.assertFalse(t.is_alive())   # it lets go after the idle time
        self.assertFalse(os.path.exists(atticus.relay_file('Google Chrome')))

    def test_relay_ignores_a_wrong_token(self):
        fake = FakeBrowser()
        with patch.object(atticus, 'WEB_IDLE', 2):
            threading.Thread(target=atticus.web_relay, args=('Google Chrome', fake.url), daemon=True).start()
            for _ in range(100):
                if os.path.exists(atticus.relay_file('Google Chrome')):
                    break
                threading.Event().wait(0.05)
            r = json.loads(Path(atticus.relay_file('Google Chrome')).read_text())
            with socket.create_connection(('127.0.0.1', r['port']), 2) as s:
                s.sendall(b'{"token": "guess", "op": "targets"}\n')
                self.assertEqual(s.makefile('rb').readline(), b'')
        self.assertFalse(any(m['method'] == 'Target.getTargets' for m in fake.calls))

    def test_debug_url_reads_the_browsers_port_file(self):
        d = self.root / 'Microsoft' / 'Edge' / 'User Data'
        d.mkdir(parents=True)
        (d / 'DevToolsActivePort').write_text('51234\n/devtools/browser/abc\n')
        self.assertEqual(atticus.debug_url('Microsoft Edge'), 'ws://127.0.0.1:51234/devtools/browser/abc')
        self.assertEqual(atticus.debug_url('Google Chrome'), '')

    def test_not_shared_yet_explains_setup_and_never_starts_a_relay(self):
        with patch.object(atticus.subprocess, 'Popen') as popen:
            out = atticus.web('read')
        popen.assert_not_called()
        self.assertIn('Set up web pages', out)
        self.assertIn('--ui see', out)

    def test_other_browsers_and_the_off_switch(self):
        self.assertIn("can't be driven", atticus.web('read', app='Firefox'))
        with patch.dict(os.environ, {'ATTICUS_FRONT': 'Firefox'}):
            self.assertIn("can't be driven", atticus.web('read'))
        self.prefs['webPages'] = False
        self.assertIn('off in Atticus Settings', atticus.web('read'))

    def test_pick_tab_prefers_the_new_page_then_the_front_window(self):
        tabs = [{'id': 'D', 'title': 'DevTools', 'url': 'devtools://devtools/x'}, {'id': 'A', 'title': 'Inbox', 'url': 'https://mail.example.com/'},
                {'id': 'B', 'title': 'Inbox (3) - Mail', 'url': 'https://other.example.com/'}, {'id': 'C', 'title': 'Shop', 'url': 'https://www.shop.example/s'}]
        with patch.object(atticus.winctl, 'find', return_value=[{'title': 'Inbox (3) - Mail - Google Chrome'}]):
            self.assertEqual(atticus.pick_tab('Google Chrome', tabs), 'B')   # the longest title the window shows
            self.assertEqual(atticus.pick_tab('Google Chrome', tabs, 'https://shop.example/s?k=x'), 'C')
        with patch.object(atticus.winctl, 'find', return_value=[]):
            self.assertEqual(atticus.pick_tab('Google Chrome', tabs), 'A')   # never DevTools

    def fake_cdp(self, page):
        """cdp() stand-in: one tab; eval runs page(command, n, text) as web.js would answer."""
        def cdp(app, op, **kw):
            if op == 'targets':
                return [{'id': 'T1', 'title': 'Fixture', 'url': 'https://fixture.invalid/'}]
            expr = kw['expr']
            if not expr.startswith('(//'):
                return 'Fixture https://fixture.invalid/'
            args = json.loads('[' + expr.rsplit(')(', 1)[1][:-1] + ']')
            return page(*args)
        return cdp

    def test_read_fill_and_a_plain_click(self):
        def page(c, n, text):
            return {'read': 'Fixture\n\nControls:\n1 box: Name\n2 button: Search', 'fill': f'Filled {n}: Name = "{text}"', 'ready': json.dumps({'ready': True, 'signature': 'https://fixture.invalid/|1'}),
                    'name': json.dumps({'what': 'Search', 'short': 'Search', 'url': 'https://fixture.invalid/'}), 'click': f'Clicked {n}: Search'}[c]
        with patch.object(atticus, 'cdp', side_effect=self.fake_cdp(page)), patch.object(atticus, 'ask_user') as ask:
            self.assertIn('1 box: Name', atticus.web('read'))
            self.assertEqual(atticus.web('fill', '1', 'Sam'), 'Filled 1: Name = "Sam"')
            out = atticus.web('click', '2')
        self.assertTrue(out.startswith('Clicked 2: Search\nNow: Fixture'))
        ask.assert_not_called()   # "Search" is not risky

    def test_risky_click_asks_first_and_no_means_no(self):
        clicks = []
        def page(c, n, text):
            if c == 'click':
                clicks.append(text)   # the checked tab's url rides along
                return f'Clicked {n}: Send'
            if c == 'ready':
                return json.dumps({'ready': True, 'signature': 'https://fixture.invalid/|1'})
            return json.dumps({'what': 'Send now', 'short': 'Send', 'url': 'https://fixture.invalid/'})
        with patch.object(atticus, 'cdp', side_effect=self.fake_cdp(page)):
            self.assertIn('Safe mode', atticus.web('click', '3'))   # no Allow card outside an ask (a shortcut): never blind
            with tempfile.TemporaryDirectory() as ask_dir, patch.dict(os.environ, {'ATTICUS_ASK': ask_dir}):
                with patch.object(atticus, 'ask_user', return_value='n') as ask:
                    self.assertIn('said no', atticus.web('click', '3'))
                self.assertEqual(ask.call_args.args[0]['q'], 'Click "Send"?')
                with patch.object(atticus, 'ask_user', return_value='y'):
                    self.assertTrue(atticus.web('click', '3').startswith('Clicked 3: Send'))
            self.prefs['safe'] = False
            self.assertTrue(atticus.web('click', '3').startswith('Clicked 3'))
        self.assertEqual(clicks, ['https://fixture.invalid/'] * 2)

    def test_a_control_that_changes_during_the_ask_is_not_clicked(self):
        names = iter([{'what': 'Delete', 'short': 'Delete', 'url': 'u'}, {'what': 'Delete all', 'short': 'Delete all', 'url': 'u'}])
        page = lambda c, n, text: json.dumps(next(names)) if c == 'name' else self.fail('clicked a changed control')
        with tempfile.TemporaryDirectory() as ask_dir, patch.dict(os.environ, {'ATTICUS_ASK': ask_dir}), \
             patch.object(atticus, 'cdp', side_effect=self.fake_cdp(page)), patch.object(atticus, 'ask_user', return_value='y'):
            self.assertIn('changed', atticus.web('click', '1'))

    def test_readiness_failure_after_a_click_keeps_success_without_repeating(self):
        clicks = []
        def page(c, n, text):
            if c == 'click':
                clicks.append(n); return 'Clicked 1: Go'
            if c == 'ready':
                raise OSError('timed out')
            return json.dumps({'what': 'Go', 'short': 'Go', 'url': 'u'})
        with patch.object(atticus, 'cdp', side_effect=self.fake_cdp(page)), patch.object(atticus, 'page_ready', side_effect=OSError('timed out')):
            out = atticus.web('click', '1')
        self.assertTrue(out.startswith('Clicked 1'))
        self.assertIn('Read again', out)
        self.assertEqual(clicks, ['1'])

    def test_one_tab_for_the_whole_ask(self):
        calls = []
        def cdp(app, op, **kw):
            calls.append(op)
            return [{'id': 'T9', 'title': 'Fixture', 'url': 'https://fixture.invalid/'}] if op == 'targets' else 'page'
        with tempfile.TemporaryDirectory() as ask_dir, patch.dict(os.environ, {'ATTICUS_ASK': ask_dir}), patch.object(atticus, 'cdp', side_effect=cdp):
            atticus.web('read'); atticus.web('fill', '1', 'x'); atticus.web('read')
        self.assertEqual(calls.count('targets'), 1)

    def test_a_closed_tab_is_forgotten(self):
        with tempfile.TemporaryDirectory() as ask_dir, patch.dict(os.environ, {'ATTICUS_ASK': ask_dir}):
            with patch.object(atticus, 'cdp', side_effect=lambda app, op, **kw: [{'id': 'T1', 'title': '', 'url': 'https://x.invalid/'}] if op == 'targets' else (_ for _ in ()).throw(RuntimeError('No target with given id found'))):
                self.assertIn('Could not use the page', atticus.web('read'))
            self.assertFalse(os.path.exists(os.path.join(ask_dir, 'web.json')))

    def test_page_ready_waits_for_a_stable_page_of_the_opened_host(self):
        now = [0]
        def sleep(s): now[0] += s
        read = lambda timeout: json.dumps({'ready': True, 'signature': ('https://mail.example.com/x|1' if now[0] < 1 else 'https://www.shop.example/s|2')})
        with patch.object(atticus.time, 'monotonic', side_effect=lambda: now[0]), patch.object(atticus.time, 'sleep', side_effect=sleep):
            self.assertTrue(atticus.page_ready(read, 'https://shop.example/s?k=x'))
            self.assertGreaterEqual(now[0], 1)
            now[0] = 0
            self.assertFalse(atticus.page_ready(lambda timeout: json.dumps({'ready': False})))
            self.assertLessEqual(now[0], 5.01)

    def test_opening_a_page_waits_only_when_pages_are_already_shared(self):
        with patch.object(atticus.os, 'startfile', create=True), patch.object(atticus.winctl, 'opener', return_value='Google Chrome'), \
             patch.object(atticus, 'front', return_value=('Notepad', '')), patch.object(atticus, 'web', return_value='Page ready.') as web:
            self.assertEqual(atticus.show('https://shop.example/s'), 'opened https://shop.example/s in front')
            web.assert_not_called()   # not connected: opening a page must never make the browser ask to Allow
            Path(atticus.relay_file('Google Chrome')).write_text('{}')
            atticus.show('https://shop.example/s')
        web.assert_called_once_with('wait', text='https://shop.example/s', app='Google Chrome', fresh=True)

    def test_prompt_label_and_gate(self):
        self.assertIn(f'{atticus.WEB} read|click <n>|fill <n>', atticus.SYSTEM)
        self.assertIn('On web pages use it first', atticus.SYSTEM)
        self.assertEqual(atticus.label('Bash', {'command': f'{atticus.WEB} read'}), 'Reading the page')
        self.assertEqual(atticus.label('Bash', {'command': f'{atticus.WEB} fill 2 "hi"'}), 'Filling it in')
        self.assertEqual(atticus.risky(f'{atticus.WEB} fill 3 "buy now"; {atticus.WEB} click 4'), '')   # --web asks itself, by the control's name
        self.assertTrue(Path(atticus.WEB_JS).exists())


if __name__ == '__main__':
    unittest.main()
