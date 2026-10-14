"""Background work and the mini screen: fake windows and a temp ask folder; never the user's apps, windows or settings."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import atticus

WIN = {'hwnd': 4242, 'title': 'Fixture - Notepad', 'exe': 'C:/Windows/notepad.exe', 'proc': 'notepad', 'app': 'Notepad'}


@unittest.skipUnless(atticus.winctl, 'Windows only')
class BackgroundTests(unittest.TestCase):
    def setUp(self):
        root, ask = tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup); self.addCleanup(ask.cleanup)
        self.dir, self.ask, self.prefs = root.name, ask.name, {}
        for p in [patch.dict(os.environ, {'ATTICUS_ASK': ask.name}), patch.object(atticus, 'DIR', root.name),
                  patch.object(atticus, 'wait_commit', return_value=True), patch.object(atticus, 'ask_user', return_value='y'),
                  patch.object(atticus, 'pref', side_effect=lambda k, d: self.prefs.get(k, d)),
                  patch.object(atticus.winctl, 'windows', return_value=[WIN]), patch.object(atticus.winctl, 'frame', return_value=(10, 20, 800, 600)),
                  patch.object(atticus.winctl, 'pid_of', return_value=99)]:
            p.start(); self.addCleanup(p.stop)
        for k in ('ATTICUS_LIVE', 'ATTICUS_FOCUS_GUARD', 'ATTICUS_REQUEST'):
            os.environ.pop(k, None)

    def request(self, text):
        Path(self.ask, 'request').write_text(text, encoding='utf-8')

    def gate(self, cmd):
        with patch('sys.stdin', io.StringIO(json.dumps({'tool_name': 'Bash', 'tool_input': {'command': cmd}}))), patch('sys.stdout', new_callable=io.StringIO) as out:
            atticus.gate()
        return json.loads(out.getvalue())['hookSpecificOutput']['permissionDecision'] if out.getvalue() else 'allow'

    def relayed(self):
        with patch('sys.stdout', new_callable=io.StringIO) as out:
            atticus.relay_hands(self.ask)
        return out.getvalue().splitlines()

    def test_unasked_focus_stealing_is_denied(self):
        self.request('add milk to my shopping list')
        for cmd in ['start notepad', 'start "" "https://x.invalid"', 'cmd //c start notepad', 'cmd.exe /c start x', 'explorer.exe https://x.invalid',
                    'powershell -NoProfile -Command "Start-Process notepad"', 'echo hi && start notepad', 'xargs start',
                    'powershell -c "(New-Object -ComObject WScript.Shell).AppActivate(\'Notepad\')"']:
            self.assertEqual(self.gate(cmd), 'deny', cmd)
        for cmd in [f'{atticus.SHOW} "https://x.invalid"', 'echo start', f'{atticus.NOTE} "start the day"', f'{atticus.UI} see Notepad',
                    'powershell -c "Start-Process notepad -WindowStyle Hidden"', 'powershell -c "Start-Sleep 1"']:
            self.assertEqual(self.gate(cmd), 'allow', cmd)
        self.request('Open Notepad')
        self.assertEqual(self.gate('start notepad'), 'allow')

    def test_wants_focus_reads_what_the_user_asked(self):
        for t in ['open Notepad', 'show me the downloads', 'switch to Chrome', 'bring Spotify forward', 'make it full screen']:
            self.assertTrue(atticus.wants_focus(t), t)
        for t in ['add milk to my list', 'open Notepad in the background', "don't open Notepad, just add a note", 'is Spotify open', 'is it open?']:
            self.assertFalse(atticus.wants_focus(t), t)
        self.request('Show me Spotify')
        self.assertTrue(atticus.wants_focus())

    def test_preview_goes_through_the_ask_folder_in_order_and_records_the_window(self):
        atticus.preview_app('Notepad', 'open')
        atticus.preview_app('Notepad', 'press')
        lines = self.relayed()
        self.assertEqual([l[:2] for l in lines], ['▣ ', '▣ '])
        first = json.loads(lines[0][2:])
        self.assertEqual({k: first[k] for k in ('pid', 'windowID', 'windowFrame', 'app', 'window', 'action')},
                         {'pid': 99, 'windowID': '4242', 'windowFrame': [10, 20, 800, 600], 'app': 'Notepad', 'window': 'Fixture - Notepad', 'action': 'open'})
        self.assertEqual(json.loads(lines[1][2:])['action'], 'press')
        self.assertEqual(json.loads(Path(self.dir, 'worked.json').read_text(encoding='utf-8')), {'app': 'Notepad', 'window': 'Fixture - Notepad'})
        self.assertEqual(self.relayed(), [])   # each line once
        with patch.dict(os.environ, {'ATTICUS_LIVE': '1'}):
            self.assertIsNone(atticus.preview_app('Notepad'))   # mid-speech instant commands never preview
        with patch.object(atticus.winctl, 'windows', return_value=[]), patch.object(atticus.winctl, 'find', return_value=[]):
            self.assertIsNone(atticus.preview_app('Nothing'))

    def test_no_ask_folder_prints_straight_to_the_box(self):
        os.environ.pop('ATTICUS_ASK')
        with patch('sys.stdout', new_callable=io.StringIO) as out:
            atticus.preview_app('', hwnd=4242)
        self.assertTrue(out.getvalue().startswith('▣ {'))

    def test_ui_steps_point_at_the_element_unless_the_mini_screen_is_off(self):
        Path(self.dir, 'ui.json').write_text(json.dumps({'hwnd': 4242, 'app': 'Notepad', 'items': {'B1': {'name': 'Save', 'rect': [100, 200, 140, 220]}}}))
        with patch.object(atticus.winctl, 'UI_FILE', str(Path(self.dir, 'ui.json'))):
            atticus.preview_ui('click', 'B1')
            lines = self.relayed()
            self.assertEqual(json.loads(lines[0][2:])['action'], 'click')
            self.assertEqual(lines[1], '@ 120 210')
            self.prefs['miniScreen'] = False
            atticus.preview_ui('click', 'B1')
            self.assertEqual(len(self.relayed()), 1)

    def test_background_open_keeps_the_user_in_front(self):
        with patch.object(atticus.os, 'startfile', create=True) as start, patch.object(atticus.winctl, 'opener', return_value='Google Chrome'), \
                patch.object(atticus.winctl, 'front', return_value=('Editor', 'Doc', 111)), patch.object(atticus, 'keep_behind') as behind, \
                patch.object(atticus, 'preview_app', return_value={'app': 'Google Chrome'}) as preview:
            self.assertEqual(atticus.show('https://x.invalid/a', bg=True), 'opened https://x.invalid/a in the background')
            start.assert_called_once_with('https://x.invalid/a')
            preview.assert_called_once_with('Google Chrome', 'open', '', wait=3)
            behind.assert_called_once_with(111, 'Google Chrome')
            self.prefs['workIn'] = 'front'   # Settings → Work on sites: In front
            behind.reset_mock()
            self.assertEqual(atticus.show('https://x.invalid/b', bg=True), 'opened https://x.invalid/b in front')
            behind.assert_not_called()
            self.assertTrue(Path(self.dir, 'raise').exists())   # the focus guard lets the browser come forward

    def test_keep_behind_undoes_only_the_launched_app(self):
        with patch.object(atticus.time, 'sleep'), patch.object(atticus.winctl, 'bring') as bring:
            with patch.object(atticus.winctl, 'front', side_effect=[('Editor', '', 111), ('Google Chrome', '', 222)]):
                atticus.keep_behind(111, 'Google Chrome')
            bring.assert_called_once_with(111)
            bring.reset_mock()
            with patch.object(atticus.winctl, 'front', return_value=('Slack', '', 333)):
                atticus.keep_behind(111, 'Google Chrome')   # the user switched to Slack: leave it
            bring.assert_not_called()
            with patch.dict(os.environ, {'ATTICUS_FOCUS_GUARD': '1'}), patch.object(atticus.winctl, 'front') as front:
                atticus.keep_behind(111, 'Google Chrome')   # Atticus.exe guards it natively
                front.assert_not_called()

    def test_show_inside_an_ask_opens_behind_unless_asked_to_see_it(self):
        def run(*args):
            with patch.object(atticus, 'show', return_value='ok') as show, patch('sys.stdout', new_callable=io.StringIO), patch('sys.argv', ['atticus.py', '--show', *args]):
                atticus.main()
            return show.call_args.args
        self.request('find a recipe for pancakes')
        self.assertEqual(run('https://x.invalid'), ('https://x.invalid', True))
        self.request('Open the pancake recipe')
        self.assertEqual(run('https://x.invalid'), ('https://x.invalid', False))
        self.assertEqual(run('--bg', 'https://x.invalid'), ('https://x.invalid', True))
        os.environ.pop('ATTICUS_ASK')   # replays and the command line: as before
        self.assertEqual(run('Notepad'), ('Notepad', False))

    def test_restore_does_not_interrupt_an_app_the_user_switched_to(self):
        with patch.object(atticus.winctl, 'focus_app') as focus:
            with patch.object(atticus.winctl, 'front', return_value=('Slack', '', 5)):
                atticus.restore_focus('Editor', 'add milk to my list', [f'{atticus.UI} click B3'])
            focus.assert_not_called()
            Path(self.dir, 'worked.json').write_text(json.dumps({'app': 'Google Chrome', 'window': 'Recipes'}))
            with patch.object(atticus.winctl, 'front', return_value=('Google Chrome', 'Recipes', 6)):
                atticus.restore_focus('Editor', 'find a recipe', [f'{atticus.SHOW} "https://x.invalid"'])
            focus.assert_called_once_with('Editor')   # the window this task worked in jumped forward: the user's app comes back

    def test_ui_actions_work_behind_and_only_a_mouse_click_brings_the_window(self):
        it = {'type': 'Button', 'name': 'Save'}
        works = Mock(); works.GetInvokePattern.return_value = Mock()
        with patch.object(atticus.winctl, 'bring') as bring:
            self.assertEqual(atticus.winctl._press(works, it, 4242), 'clicked button "Save"')
            bring.assert_not_called()
            mouse = Mock(**{f'Get{p}Pattern.return_value': None for p in ('Invoke', 'Toggle', 'SelectionItem', 'ExpandCollapse')})
            with patch.object(atticus.winctl.time, 'sleep'):
                self.assertIn('with the mouse', atticus.winctl._press(mouse, it, 4242))
            bring.assert_called_once_with(4242)

    def test_deliberate_raises_tell_the_focus_guard(self):
        with patch.object(atticus.winctl.user32, 'IsIconic', return_value=0), patch.object(atticus.winctl.user32, 'GetForegroundWindow', return_value=0), \
                patch.object(atticus.winctl.user32, 'keybd_event'), patch.object(atticus.winctl.user32, 'BringWindowToTop'), \
                patch.object(atticus.winctl.user32, 'SetForegroundWindow', return_value=1):
            atticus.winctl.bring(4242)
        self.assertTrue(Path(self.dir, 'raise').exists())
        atticus.allow_focus('open Notepad')
        self.assertTrue(Path(self.dir, 'focus').exists())
        atticus.allow_focus('add milk to my list')
        self.assertFalse(Path(self.dir, 'focus').exists())

    def test_settings_rows_exist_for_voice(self):
        self.assertEqual(atticus.picks()['workIn'][1], {'In the background': 'back', 'In front': 'front'})
        self.assertIn('miniScreen', atticus.switches())


if __name__ == '__main__':
    unittest.main()
