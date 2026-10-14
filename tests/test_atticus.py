"""Offline regressions: no Claude calls, Mac actions, or user settings."""
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import atticus


class BrainTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()   # never the user's real log, chat or rules (and CI has no ~/.config/atticus)
        self.addCleanup(root.cleanup)
        p = patch.object(atticus, 'DIR', root.name)
        p.start()
        self.addCleanup(p.stop)
        self.prefs = patch.object(atticus, 'pref', side_effect=lambda k, d: d)
        self.prefs.start()
        self.addCleanup(self.prefs.stop)

    def test_domain_dots_and_url_case_survive(self):
        with patch.object(atticus, 'skills', return_value={}), patch.object(atticus.os, 'startfile', create=True) as run:
            for request, url in [('Open github.com.', 'https://github.com'),
                                 ('Open github dot com', 'https://github.com'),
                                 ('Open https://github.com/SaarthurR/atticus?q=UI', 'https://github.com/SaarthurR/atticus?q=UI')]:
                self.assertTrue(atticus.fast(request))
                self.assertEqual(run.call_args.args[0], url)

    def test_close_and_quit_do_not_restore_focus(self):
        with patch.object(atticus.winctl, 'focus_app') as focus:
            for task in ['Close Spotify', 'quit Spotify', 'exit Spotify', 'close this window']:
                atticus.restore_focus('Spotify', task, [])
            focus.assert_not_called()

    def test_focus_restore_only_addresses_existing_process(self):
        with patch.object(atticus.winctl, 'focus_app') as focus, patch.object(atticus.winctl, 'launch') as launch:
            atticus.restore_focus('Spotify', 'what time is it', [])
            focus.assert_called_once_with('Spotify')   # an open window only: never a relaunch
            launch.assert_not_called()

    def test_explicit_focus_and_front_commands_keep_their_destination(self):
        with patch.object(atticus.winctl, 'focus_app') as focus:
            atticus.restore_focus('Notes', 'switch to Spotify', [])
            atticus.restore_focus('Notes', 'show me Spotify', [atticus.FRONT + ' Spotify'])
            atticus.restore_focus('Notes', 'open Spotify', [atticus.SHOW + ' Spotify'])
            focus.assert_not_called()

    def test_missing_display_does_not_send_wrong_screen(self):
        snap = unittest.mock.Mock()
        snap.shots = [('monitor.jpg', 'Screen 2: external monitor')]
        displays = [('Laptop', 0, 0, 1400, 900, True), ('Monitor', 1400, 0, 1900, 1000, False)]
        with patch.object(atticus, 'pref', side_effect=lambda k, d: d), patch.object(atticus, 'screens', return_value=displays):
            self.assertEqual(atticus.screen_for('read this screen', snap, 50), [])
            self.assertEqual(atticus.screen_for('read this screen', snap, 1500), [snap.shots[0]])
            self.assertIsNone(atticus.screen_for('open notes', snap, 50))

    def test_commit_waits_for_confirmation(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / 'commit')
            timer = threading.Timer(0.15, lambda: Path(path).write_text('go'))
            with patch.dict(os.environ, {'ATTICUS_COMMIT': path}):
                timer.start()
                try:
                    started = time.monotonic()
                    self.assertTrue(atticus.wait_commit())
                    self.assertGreaterEqual(time.monotonic() - started, 0.15)
                finally:
                    timer.join()

    def test_gate_denies_unconfirmed_request(self):
        with patch.object(atticus, 'wait_commit', return_value=False), \
             patch('sys.stdin', io.StringIO(json.dumps({'tool_input': {'command': 'open -a Notes'}}))), \
             patch('sys.stdout', new_callable=io.StringIO) as out:
            atticus.gate()
            self.assertEqual(json.loads(out.getvalue())['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_cancelled_parent_cannot_use_leftover_commit(self):
        with patch.dict(os.environ, {'ATTICUS_COMMIT': '/fake/commit'}), \
             patch.object(atticus.os, 'getppid', side_effect=[100, 101]), \
             patch.object(atticus.os.path, 'exists', return_value=True):
            self.assertFalse(atticus.wait_commit())

    def test_live_does_not_replay_learned_shell(self):
        with patch.dict(os.environ, {'ATTICUS_LIVE': '1'}), \
             patch.object(atticus, 'skills', return_value={k: {'cmds': ['echo private'], 'reply': 'Done'} for k in ['daily mode', 'open spotify']}), \
             patch.object(atticus.subprocess, 'run') as run:
            self.assertIsNone(atticus.fast('daily mode'))
            self.assertIsNone(atticus.fast('open Spotify'))
            run.assert_not_called()

    def test_risky_saved_shortcut_defers_to_safe_gate(self):
        with patch.object(atticus, 'skills', return_value={'send mail': {'cmds': ['osascript -e \'tell application "Mail" to send outgoing message 1\''], 'reply': 'Sent'}}), \
             patch.object(atticus.subprocess, 'run') as run:
            self.assertIsNone(atticus.replay('send mail'))
            run.assert_not_called()

    def test_fast_action_is_logged_once_at_zero_cost(self):
        with tempfile.TemporaryDirectory() as root, patch.object(atticus, 'DIR', root), \
             patch.multiple(atticus, licensed=lambda: True, ping=lambda: None,
                            fast=lambda _: 'Opened Spotify.', remember_turn=lambda *_: None), \
             patch.object(atticus.signal, 'signal'), \
             patch('sys.argv', ['atticus.py', '--fast', 'open Spotify']), \
             patch('sys.stdout', io.StringIO()), patch.dict(os.environ, {'ATTICUS_LIVE': '1'}):
            atticus.main()
            rows = atticus.read_rows(str(Path(root) / 'log.jsonl'))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['usd'], 0)
            self.assertTrue(rows[0]['live'])

    def test_confirmed_instant_command_skips_context_work(self):
        with patch.multiple(atticus, **{name: unittest.mock.DEFAULT for name in
                            ['start_claude', 'licensed', 'ping', 'fast', 'remember_turn', 'say', 'front', 'stop_claude']}) as mocks, \
             patch.object(atticus.threading, 'Thread'), \
             patch.object(atticus.signal, 'signal'), patch('sys.argv', ['atticus.py', '--wait']), \
             patch('sys.stdin', io.StringIO('Notes\topen Notes\n')), patch.dict(os.environ, {'ATTICUS_COMMIT': ''}):
            # Even without a CLI, --wait must read the request instead of treating its flag as a question.
            mocks['start_claude'].return_value = None
            mocks['licensed'].return_value = True
            mocks['fast'].return_value = 'Opened Notes.'
            atticus.main()
            mocks['fast'].assert_called_once_with('open Notes')
            mocks['front'].assert_not_called()
            mocks['say'].assert_called_once_with('✓', 'Opened Notes.')

    def test_stream_requires_a_real_terminal_answer(self):
        for stream, expected in [([], 'No answer came back. Please try again.'),
                                 ([{'type': 'result', 'result': ''}], 'No answer came back. Please try again.'),
                                 ([{'type': 'result', 'result': 'Found it.', 'total_cost_usd': .001}], 'Found it.')]:
            p = unittest.mock.Mock()
            p.stdin = io.StringIO()
            p.stdout = [json.dumps(row) + '\n' for row in stream]
            p.ask = ''; p.returncode = 0
            reply, _, _ = atticus.run('test', p)
            self.assertEqual(reply, expected)
            self.assertEqual(atticus.run.cmds, [])
            self.assertEqual(atticus.run.ok, expected == 'Found it.')

    def test_truncated_history_preserves_valid_turns(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'recent.jsonl'
            turn = {'t': time.time(), 'q': 'Hi', 'a': 'Hello'}
            path.write_text(json.dumps(turn) + '\nnull\n{"q":"cut off\n{}\n')
            with patch.object(atticus, 'RECENT', str(path)):
                self.assertEqual(atticus.recent(), [turn])

    def test_error_result_is_not_learned(self):
        p = unittest.mock.Mock()
        p.stdin = io.StringIO(); p.ask = ''; p.returncode = 0
        p.stdout = map(json.dumps, [
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'open -a Notes'}}]}},
            {'type': 'result', 'is_error': True, 'result': 'Could not finish.'}])
        with patch.object(atticus, 'say'):
            atticus.run('test', p)
        self.assertEqual(atticus.run.cmds, [])
        self.assertEqual(atticus.run.ran, ['open -a Notes'])
        self.assertFalse(atticus.run.ok)

    def test_recovered_tool_error_keeps_success_but_is_not_learned(self):
        p = unittest.mock.Mock()
        p.stdin = io.StringIO(); p.ask = ''; p.returncode = 0
        p.stdout = map(json.dumps, [
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'is_error': True}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'is_error': False, 'content': 'Verified.'}]}},
            {'type': 'result', 'result': 'Recovered.'}])
        atticus.run('test', p)
        self.assertTrue(atticus.run.ok)
        self.assertEqual(atticus.run.cmds, [])

    def test_failed_final_tool_cannot_be_reported_as_done(self):
        p = unittest.mock.Mock()
        p.stdin = io.StringIO(); p.ask = ''; p.returncode = 0
        p.stdout = map(json.dumps, [
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'is_error': True, 'content': 'Image not found'}]}},
            {'type': 'result', 'result': 'I sent the message.'}])
        reply, _, _ = atticus.run('send the question', p)
        self.assertFalse(atticus.run.ok)
        self.assertIn('could not verify', reply)
        self.assertNotIn('I sent', reply)

    def test_cancellation_stops_descendant(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / 'child-survived'
            ready = Path(root) / 'ready'
            child = f'import time; from pathlib import Path; Path({str(ready)!r}).touch(); time.sleep(0.5); Path({str(marker)!r}).touch()'
            parent = f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{child!r}]); time.sleep(30)'
            p = subprocess.Popen([sys.executable, '-c', parent], start_new_session=True)
            try:
                deadline = time.monotonic() + 3
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists())
                atticus.stop_claude(p)
                p.wait(timeout=3)
                time.sleep(.6)
                self.assertFalse(marker.exists())
            finally:
                atticus.stop_claude(p)
                p.wait(timeout=3)

    def test_fast_worker_sigterm_stops_its_action_group(self):
        with tempfile.TemporaryDirectory() as root:
            marker, ready = Path(root) / 'action', Path(root) / 'ready'
            action = f'import time; from pathlib import Path; Path({str(ready)!r}).touch(); time.sleep(.5); Path({str(marker)!r}).touch()'
            script = f'''import atticus, subprocess, sys, time
atticus.ping = lambda: None
atticus.licensed = lambda: True
def fast(_):
    subprocess.Popen([sys.executable, '-c', {action!r}])
    time.sleep(30)
atticus.fast = fast
sys.argv = ['atticus.py', '--fast', 'test']
atticus.main()
'''
            p = subprocess.Popen([sys.executable, '-c', script], env={**os.environ, 'ATTICUS_DIR': root})
            try:
                deadline = time.monotonic() + 3
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists())
                atticus.winctl.kill_tree(p.pid); p.wait(timeout=3); time.sleep(.6)   # what Atticus.exe does: the whole tree
                self.assertFalse(marker.exists())
            finally:
                if p.poll() is None:
                    p.terminate(); p.wait(timeout=3)

    def test_cli_stderr_cannot_block_the_stream(self):
        with tempfile.TemporaryDirectory() as root:
            script = Path(root) / 'claude.py'
            script.write_text(f'import sys\nsys.stderr.write("x" * 1000000)\nprint(\'{json.dumps({"type": "result", "result": "Ready."})}\')\n')
            fake = Path(root) / 'claude.cmd'
            fake.write_text(f'@"{sys.executable}" "{script}"\r\n')
            previous_handler = signal.getsignal(signal.SIGTERM)
            try:
                with patch.object(atticus.shutil, 'which', return_value=str(fake)), \
                     patch.dict(os.environ, {'PATH': root + os.pathsep + os.environ['PATH'], 'ATTICUS_COMMIT': ''}), \
                     patch.object(atticus, 'pref', side_effect=lambda k, d: False if k == 'safe' else d):
                    p = atticus.start_claude()
                    timer = threading.Timer(3, atticus.stop_claude, args=(p,))
                    timer.start()
                    try:
                        self.assertEqual(atticus.run('test', p)[0], 'Ready.')
                    finally:
                        timer.cancel(); timer.join(); atticus.stop_claude(p); p.errors.close()
            finally:
                signal.signal(signal.SIGTERM, previous_handler)


class WatchAndBudgetTests(unittest.TestCase):
    def setUp(self):
        self.notes = []
        for p in (patch.object(atticus.time, 'sleep'), patch.object(atticus, 'notify', side_effect=self.notes.append)):
            p.start()
            self.addCleanup(p.stop)

    def fake_app(self, out, code=0):
        f = tempfile.NamedTemporaryFile('w', suffix='.cmd', delete=False)
        f.write(f'@echo off\r\necho {out}\r\nexit /b {code}\r\n' if out else f'@exit /b {code}\r\n')
        f.close()
        self.addCleanup(os.remove, f.name)
        return patch.object(atticus, 'APP', f.name)

    def test_cmd_watch_fires_after_two_passing_checks(self):
        atticus.watch('true', '')
        self.assertEqual(len(self.notes), 1)
        self.assertIn('done', self.notes[0])

    def test_phrase_gone_fires_and_present_waits(self):
        with self.fake_app('All set. Ready for the next task'):
            atticus.watch('', '', 'Orca', '', 'gone', 'esc to  interrupt')
        self.assertIn('Orca: done.', self.notes[-1])
        with self.fake_app('Working (12s - esc to interrupt)'), patch.object(atticus.time, 'time', side_effect=[0, 0, 1, 2, 1e9]):
            atticus.watch('', '', 'Orca', '', 'gone', 'esc to interrupt')
        self.assertIn('3 hours', self.notes[-1])

    def test_unreadable_window_stops_with_a_reason(self):
        with self.fake_app('', 1):
            atticus.watch('', '', 'Orca', '', 'shows', 'done')
        self.assertIn("can't see its window", self.notes[-1])

    def test_title_words_that_name_a_tab_fall_back_to_the_main_window(self):
        f = tempfile.NamedTemporaryFile('w', suffix='.cmd', delete=False)   # no window has that title; the main one reads fine
        f.write('@echo off\r\nif not "%~3"=="" exit /b 1\r\necho All set\r\n')
        f.close()
        self.addCleanup(os.remove, f.name)
        with patch.object(atticus, 'APP', f.name):
            atticus.watch('', '', 'Orca', 'Atticus meeting mode spec', 'gone', 'esc to interrupt')
        self.assertIn('Orca: done.', self.notes[-1])

    def test_settled_waits_while_text_changes_then_fires(self):
        reads = iter(['Sprouting (3s)', 'Incubating (8s)', 'Done.', 'Done.', 'Done.', 'Done.', 'Done.'])
        with patch.object(atticus.subprocess, 'run', side_effect=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=next(reads))):
            atticus.watch('', '', 'Orca', '', 'settled', '')
        self.assertIn('Orca: done.', self.notes[-1])
        self.assertEqual(next(reads, 'used up'), 'used up')   # it needed all 5 matching reads: 4 unchanged in a row

    def test_settled_requires_nonempty_continuous_successful_reads(self):
        reads = iter([(0, ''), (0, ''), (0, 'Done'), (0, 'Done'), (1, 'Done'),
                      (0, 'Done'), (0, 'Done'), (0, 'Done'), (0, 'Done'), (0, 'Done')])
        def run(*args, **kwargs):
            code, text = next(reads)
            return subprocess.CompletedProcess(args, code, stdout=text)
        with patch.object(atticus.subprocess, 'run', side_effect=run):
            atticus.watch('', '', 'Orca', '', 'settled', '')
        self.assertEqual(next(reads, None), None)
        self.assertIn('Orca: done.', self.notes[-1])

    def test_title_fallback_retries_original_title_on_each_poll(self):
        calls = []
        def run(cmd, **kwargs):
            calls.append(cmd[-1])
            return subprocess.CompletedProcess(cmd, bool(cmd[-1]), stdout='Done')
        with patch.object(atticus.subprocess, 'run', side_effect=run):
            atticus.watch('', '', 'Orca', 'Tab', 'gone', 'busy')
        self.assertEqual(calls, ['Tab', '', 'Tab', ''])

    def test_settled_is_a_watch_mode_the_ai_can_ask_for(self):
        with patch.object(atticus.subprocess, 'Popen') as spawn, patch('sys.stdout', new_callable=io.StringIO) as out, \
             patch.object(atticus, 'WATCHING', self.enterContext(tempfile.TemporaryDirectory())), patch.object(sys, 'argv', ['atticus.py', '--watch', 'Windows Terminal', '', 'settled', '']):
            spawn.return_value.pid = 4242
            atticus.main()
        self.assertIn('--watching', spawn.call_args.args[0])
        self.assertIn('watching Windows Terminal', out.getvalue())
        self.assertIn('settled', atticus.SYSTEM)

    def test_pick_prints_the_choice_the_box_sends_back(self):
        for answer, printed in [('1', 'b.pdf'), ('', 'None of these'), (None, 'The user did not pick')]:   # None: no answer in time
            with patch.dict(os.environ, {'ATTICUS_ASK': '/fake'}), patch.object(atticus, 'ask_user', return_value=answer) as ask, \
                 patch('sys.stdout', new_callable=io.StringIO) as out:
                atticus.pick('Which one?', ['a.pdf', 'b.pdf'])
            self.assertEqual(ask.call_args.args[0]['options'], ['a.pdf', 'b.pdf'])
            self.assertTrue(out.getvalue().startswith(printed))
        self.assertEqual(atticus.label('Bash', {'command': f'{atticus.PICK} "Which?" "a" "b"'}), 'Asking you which one')
        self.assertFalse(atticus.risky(f'{atticus.PICK} "Which file?" "~/delete-me.txt" "~/send.pdf"'))

    def test_clicks_are_as_risky_as_the_button_name(self):
        with tempfile.TemporaryDirectory() as root, patch.object(atticus.winctl, 'UI_FILE', os.path.join(root, 'ui.json')):
            items = {'B1': {'name': 'Bold', 'type': 'Button', 'rid': [], 'rect': [0, 0, 1, 1]},
                     'B2': {'name': 'Send now', 'type': 'Button', 'rid': [], 'rect': [0, 0, 1, 1]}}
            json.dump({'hwnd': 0, 'items': items}, open(atticus.winctl.UI_FILE, 'w'))
            self.assertEqual(atticus.risky(f'{atticus.UI} click B1'), '')
            self.assertEqual(atticus.risky(f'{atticus.UI} click B1 "Send now"'), 'Click "Send now"?')   # by name, in a batch
            self.assertEqual(atticus.risky(f'{atticus.UI} click B2'), 'Click "Send now"?')

    def test_enter_asks_only_after_typing_a_message(self):
        with tempfile.TemporaryDirectory() as ask, patch.dict(os.environ, {'ATTICUS_ASK': ask}):
            K, T = atticus.KEYS, atticus.TYPE
            switch = f'{K} Discord ctrl+k; {T} --in Discord "Sam"; {K} Discord enter'
            self.assertEqual(atticus.risky(switch), '')                        # a search box: Enter just goes there
            self.assertEqual(atticus.risky(switch + f'; {T} --in Discord "late"; {K} Discord enter'), 'Send this?')
            for part in (f'{K} Discord ctrl+k', f'{T} x', f'{K} Discord enter', f'{T} late'):   # the same, one call at a time
                atticus.typing_save(part)
            self.assertEqual(atticus.risky(f'{K} Discord enter'), 'Send this?')

    def test_joplin_new_note_goes_in_the_named_notebook(self):
        calls = []
        def api(method, path, body=None, **q):
            calls.append((method, path, body))
            if path == 'folders':
                return {'items': [{'id': 'f1', 'title': 'Work Notes'}], 'has_more': False}
            return {'id': 'n1', 'title': body['title']}
        with patch.object(atticus.winctl, 'joplin', side_effect=api):
            self.assertIn('created note "Plan"', atticus.winctl.joplin_cmd(['new', 'Plan', 'step one', '--in', 'work']))
            self.assertEqual(calls[-1], ('POST', 'notes', {'title': 'Plan', 'body': 'step one', 'parent_id': 'f1'}))
            self.assertIn('Notebooks: Work Notes', atticus.winctl.joplin_cmd(['new', 'Plan', 'x', '--in', 'Recipes']))   # never a guess

    def test_integrations_are_only_mentioned_when_set_up_and_asked_about(self):
        with tempfile.TemporaryDirectory() as root, patch.object(atticus.integrations, 'CONFIG', os.path.join(root, 'i.json')), \
             patch.object(atticus.winctl, 'secret_set', return_value=True) as store, patch.object(atticus.winctl, 'secret_get', return_value='k'):
            self.assertEqual(atticus.integrations.hints('what should I watch tonight', atticus.INT), '')   # not set up: nothing
            mod = atticus.integrations.modules()['jellyfin']
            with patch.object(mod, 'check', return_value='Connected.'):
                self.assertIn('Connected', atticus.integrations.setup('jellyfin', {'url': 'http://x:8096/', 'user': 'fin', 'key': 'secret'}))
            store.assert_called_once_with('jellyfin/key', 'secret')   # the key goes to Credential Manager, not the file
            self.assertNotIn('secret', open(atticus.integrations.CONFIG).read())
            self.assertIn('atticus --int jellyfin search', atticus.integrations.hints('what should I watch tonight', atticus.INT))
            self.assertEqual(atticus.integrations.hints('what time is it in Tokyo', atticus.INT), '')   # unrelated: costs nothing

    def test_every_integration_loads_and_describes_itself(self):
        files = {f[:-3] for f in os.listdir(Path(atticus.__file__).parent / 'integrations') if f.endswith('.py') and not f.startswith('_')}
        mods = atticus.integrations.modules()
        self.assertEqual(set(mods), files)   # a syntax error would drop one silently
        for name, mod in mods.items():
            for attr in ('TITLE', 'ABOUT', 'EXAMPLE', 'KEY_HELP', 'FIELDS', 'WORDS', 'HELP'):
                self.assertTrue(getattr(mod, attr, None), f'{name}.{attr}')
            self.assertTrue(callable(getattr(mod, 'run', None)), name)

    def test_home_assistant_security_actions_ask_first(self):
        ha = atticus.integrations.modules()['homeassistant']
        self.assertTrue(ha.mcp_risky('HassTurnOff', {'name': 'Front door lock'}))
        self.assertTrue(ha.mcp_risky('HassSetPosition', {'name': 'Garage door', 'position': 100}))
        self.assertEqual(ha.mcp_risky('HassTurnOff', {'name': 'Office lights'}), '')
        self.assertEqual(ha.mcp_risky('GetLiveContext', {}), '')

    def test_people_can_add_their_own_services(self):
        I = atticus.integrations
        with tempfile.TemporaryDirectory() as root, patch.object(I, 'CONFIG', os.path.join(root, 'i.json')), \
             patch.object(atticus.winctl, 'secret_set', return_value=True), patch.object(atticus.winctl, 'secret_get', return_value=''):
            I._mods.clear()
            os.makedirs(os.path.join(root, 'integrations'))
            Path(root, 'integrations', 'hello.py').write_text("from . import arg\nTITLE='Hello'\nABOUT='a'\nEXAMPLE='x'\nKEY_HELP='k'\n"
                                                             "FIELDS=[('url','Address',False)]\nWORDS=r'\\bhello\\b'\nHELP='{CMD} greet'\nRISKY={}\n"
                                                             "def run(cfg, args): return 'hi ' + arg(args, 0)\n")
            from integrations import _custom
            with patch.object(_custom.Custom, 'check', return_value='Connected.'):
                self.assertIn('saved', I.add_custom({'kind': 'api', 'title': 'Mealie', 'url': 'http://m:9000', 'auth': 'none', 'words': 'recipe, meal plan'}))
                self.assertIn('saved', I.add_custom({'kind': 'mcp', 'title': 'Garden', 'url': 'http://g/mcp', 'words': 'sprinklers'}))
            self.assertIn('hello', I.modules())   # the personal folder loads
            self.assertIn('my_mealie', I.configured())   # no key needed for your own
            self.assertIn('my_mealie get', I.hints('what is the meal plan tonight', atticus.INT))
            self.assertEqual(I.hints('what time is it', atticus.INT), '')
            self.assertEqual(atticus.risky(f'{atticus.INT} my_mealie delete "/api/recipes/1"'), 'Change something in Mealie?')
            self.assertEqual(atticus.risky(f'{atticus.INT} my_mealie get "/api/recipes"'), '')
            self.assertEqual(list(I.mcp_for('water the sprinklers')), ['my_garden'])
            self.assertTrue(I.mcp_risky('mcp__my_garden__StartZone', {'name': 'back'}))
            self.assertEqual(I.mcp_risky('mcp__my_garden__ListZones', {}), '')
            I.remove('my_mealie')
            self.assertNotIn('my_mealie', I.modules())
        I._mods.clear()

    def test_the_feature_is_called_integrations(self):
        root = Path(atticus.__file__).parent   # the user renamed it; "homelab" must not creep back into anything people read
        for f in [root / 'README.md', *root.glob('ui/*.html'), *root.glob('integrations/*.py'), root / 'atticus.py']:
            self.assertNotIn('homelab', f.read_text(encoding='utf-8').lower(), f.name)

    def test_window_rects_fill_halves_and_center_within_the_work_area(self):
        area = (-2560, 0, 2560, 1392)   # a monitor left of the main one
        self.assertEqual(atticus.winctl.rect_for('left', area, (800, 600)), (-2560, 0, 1280, 1392))
        self.assertEqual(atticus.winctl.rect_for('right', area, (800, 600)), (-1280, 0, 1280, 1392))
        self.assertEqual(atticus.winctl.rect_for('center', area, (3000, 600)), (-2560, 396, 2560, 600))   # never wider than the screen

    def test_set_flips_a_switch_by_name_safe_mode_only_on(self):
        self.assertIn('askMe', atticus.switches())
        self.assertNotIn('paused', atticus.switches())
        with patch.dict(os.environ, {'ATTICUS_ASK': '/fake'}), patch.object(atticus, 'ask_user') as ask, patch('sys.stdout', new_callable=io.StringIO):
            atticus.set_switch('safe mode', 'on')
        self.assertEqual(ask.call_args.args[0], {'set': 'safe', 'value': True})
        with patch.dict(os.environ, {'ATTICUS_ASK': '/fake'}), patch.object(atticus, 'ask_user') as ask, patch('sys.stdout', new_callable=io.StringIO):
            atticus.set_switch('claude model', 'sonnet')   # a choice setting, by its option
        self.assertEqual(ask.call_args.args[0], {'set': 'model', 'value': 'sonnet'})
        for words, key in [('the asking setting', 'askMe'), ('read answers aloud', 'speak'), ('safe mode', None)]:
            with patch.dict(os.environ, {'ATTICUS_ASK': '/fake'}), patch.object(atticus, 'ask_user') as ask, patch('sys.stdout', new_callable=io.StringIO):
                atticus.set_switch(words, 'off')
            self.assertEqual(ask.call_args.args[0] if ask.called else None, key and {'set': key, 'value': False})

    def test_unknown_command_never_shows_raw_shell(self):
        self.assertEqual(atticus.label('Bash', {'command': 'cat > /private/var/folders/x/prompt.txt <<EOF'}), 'Working on it')

    def test_prompt_stays_under_budget(self):
        # Every ask pays for this prompt. Raising the cap is a decision: check `atticus.py --budget` first.
        full = atticus.SYSTEM + atticus.ASK_ME + atticus.POINTER + atticus.LEARN_ME + atticus.TEACH + atticus.ASK_FIRST + atticus.WORKFLOW + atticus.JOPLIN_TOOLS
        self.assertLessEqual(len(full), 10000)


class ThumbTests(unittest.TestCase):
    @unittest.skipUnless(atticus.winctl, 'Windows only')
    def test_a_picture_gets_a_png_preview_and_text_gets_none(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            Image.new('RGB', (640, 480), (200, 40, 40)).save(os.path.join(d, 'red.jpg'))
            Path(d, 'note.txt').write_text('hello')
            pic, txt, folder = atticus.choices([os.path.join(d, 'red.jpg'), os.path.join(d, 'note.txt'), d])
        self.assertTrue(pic['img'].startswith('data:image/png;base64,iVBOR'))
        self.assertEqual((txt['img'], txt['kind'], folder['img'], folder['kind']), ('', 'TXT', '', 'Folder'))

    @unittest.skipUnless(atticus.winctl, 'Windows only')
    def test_cloud_files_are_skipped_and_a_stuck_preview_gives_up(self):
        with patch.object(atticus.winctl, 'cloud', return_value=True), patch.object(atticus.subprocess, 'run') as run:
            self.assertEqual(atticus.thumb(__file__), '')
            run.assert_not_called()   # never opened, so never downloaded
        with patch.object(atticus.winctl, 'cloud', return_value=False), \
             patch.object(atticus.subprocess, 'run', side_effect=subprocess.TimeoutExpired('python', 4)):
            self.assertEqual(atticus.thumb(__file__), '')


if __name__ == '__main__':
    unittest.main()
