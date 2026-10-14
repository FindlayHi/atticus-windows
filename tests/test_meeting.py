"""Meeting Copilot workers and archive: offline, fake Claude, scratch folders only (ported from atticus-mac, adapted to Windows)."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import atticus


class MeetingTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory(); self.addCleanup(root.cleanup)   # never the user's settings, log or meetings
        for k, v in (('DIR', root.name), ('PREFS', os.path.join(root.name, 'settings.json'))):
            p = patch.object(atticus, k, v); p.start(); self.addCleanup(p.stop)

    def test_question_rule_and_first_name_boundaries(self):
        for text in ['Budget?', 'What is the date', 'why now', 'How much', 'When is it', 'Where next', 'Who pays', 'Which pilot', 'Can we go', 'Could it work', 'Should we wait', 'Would it help', 'Do we agree', 'Does it fit', 'Did it ship', 'Is it ready', 'Are we done', 'Will it work', 'Saarth, your thoughts.']:
            with self.subTest(text=text):
                self.assertTrue(atticus.meeting_question(text, 'Saarth Ranka'))
        for text in ['', 'Thanks everyone.', 'The pilot starts soon.', 'Whatever works.', 'Saarthi is joining.', 'I know Ranka.']:
            with self.subTest(text=text):
                self.assertFalse(atticus.meeting_question(text, 'Saarth Ranka'))
        self.assertFalse(atticus.meeting_question('Thanks', ''))
        for text in ['Hey, how are you doing today?', 'How are you, Saarth?', 'Can you hear me?', 'We ship Friday, right?', 'How have you been']:
            self.assertFalse(atticus.meeting_question(text, 'Saarth Ranka'), text)
        for text in ['How are you thinking about pricing?', 'How was your experience at Google?']:
            self.assertTrue(atticus.meeting_question(text, 'Saarth Ranka'), text)
        self.assertTrue(atticus.meeting_question('A.J., can you help.', 'A.J. Jones'))

    def test_native_rule_is_the_same_regex(self):
        source = (Path(atticus.SELF).parent / 'win' / 'Atticus' / 'Meeting.cs').read_text(encoding='utf-8')
        self.assertIn('@"' + atticus.MEETING_SMALL_TALK + '"', source)   # a verbatim string: the same characters

    def test_skip_unsure_and_three_line_answer(self):
        for kind in ['SKIP', 'UNSURE']:
            self.assertEqual(atticus.meeting_parse(' \n' + kind + '\n'), {'kind': kind, 'lines': []})
        self.assertEqual(atticus.meeting_parse('**November 12.**\nThe brief says so.\nApproved.\nExtra.'),
                         {'kind': 'answer', 'lines': ['November 12.', 'The brief says so.', 'Approved.']})
        self.assertEqual(atticus.meeting_parse('UNSURE\nThe launch date has not been shared.'),
                         {'kind': 'UNSURE', 'lines': [], 'reason': 'The launch date has not been shared.'})
        self.assertEqual(atticus.meeting_parse('GENERAL\nParis is the capital of France.'),
                         {'kind': 'answer', 'lines': ['Paris is the capital of France.'], 'general': True})
        with self.assertRaises(ValueError):
            atticus.meeting_parse(' \n')

    def test_prompt_budget_recent_window_and_stable_prefix(self):
        rows = [{'at': 100, 'text': 'old secret'}, {'at': 900, 'text': 'x' * 20000}, {'at': 1000, 'text': 'current', 'mic': True}, {'at': 1001, 'text': 'future'}]
        data = {'brief': 'b' * 20000, 'transcript': rows, 'now': 1000, 'question': 'q' * 20000}
        system, prompt = atticus.meeting_prompt(data)
        self.assertLess(len(system) + len(prompt), 10000)
        self.assertNotIn('old secret', prompt)
        self.assertNotIn('future', prompt)
        self.assertIn('You: current', prompt)
        self.assertEqual(system, atticus.meeting_prompt({**data, 'question': 'Other?', 'best': True})[0])
        self.assertIn('Manual ask:', atticus.meeting_prompt({**data, 'question': 'Other?', 'best': True})[1])
        self.assertIn('never return SKIP or UNSURE', system)
        self.assertNotIn('Meeting Copilot', atticus.SYSTEM)
        timed = atticus.meeting_prompt({'question': 'What was said in the last 2 minutes?', 'now': 1000, 'transcript': [{'at': 850, 'text': 'too old'}, {'at': 910, 'text': 'recent'}]})[1]
        self.assertNotIn('too old', timed)
        self.assertIn('[90s ago] recent', timed)

    def test_main_prompt_offers_meeting_history_without_an_allow_card(self):
        self.assertIn(atticus.MEETINGS, atticus.SYSTEM)
        self.assertEqual(atticus.risky(f'{atticus.MEETINGS} "monday budget"'), '')
        self.assertEqual(atticus.label('Bash', {'command': f'{atticus.MEETINGS} "cedar"'}), 'Looking through your meetings')

    def test_claude_is_tool_free_and_receives_no_personal_settings(self):
        seen = {}

        class Worker:
            pid = 12345
            returncode = 0

            def communicate(self, payload, timeout):
                args = start.call_args.args[0]
                seen['settings'] = json.loads(Path(args[args.index('--settings') + 1]).read_text(encoding='utf-8'))
                seen['system'] = Path(args[args.index('--system-prompt-file') + 1]).read_text(encoding='utf-8')
                seen['payload'] = json.loads(payload)
                return json.dumps({'type': 'result', 'result': 'November 12.', 'total_cost_usd': .001}), ''
        with patch.object(atticus.shutil, 'which', return_value='claude'), \
                patch.object(atticus.subprocess, 'Popen', return_value=Worker()) as start, patch.object(atticus, 'stop_claude'), \
                patch.object(atticus.signal, 'signal'), patch.object(atticus.atexit, 'register'), patch.object(atticus, 'log_turn') as log:
            answer, metrics = atticus.meeting_call('Stable prefix', 'A question', image='aW1n')
            args = start.call_args.args[0]
            self.assertFalse(os.path.exists(os.path.dirname(args[args.index('--settings') + 1])), 'temp prompt folder left behind')
        self.assertEqual(args[args.index('--tools') + 1], '')
        self.assertEqual(args[args.index('--model') + 1], 'haiku')
        self.assertIn('--no-session-persistence', args)
        self.assertIn('--strict-mcp-config', args)
        self.assertEqual(args[args.index('--setting-sources') + 1], '')
        self.assertEqual(seen['settings'], {'disableAllHooks': True})
        self.assertEqual(seen['system'], 'Stable prefix')
        self.assertEqual(seen['payload']['message']['content'][0]['source']['data'], 'aW1n')
        self.assertTrue(all(len(a) < 1000 for a in args), 'a long argument would hit cmd.exe\'s 8 KB limit')
        self.assertEqual(log.call_args.args[:2], ('Meeting Copilot', ''))
        self.assertEqual(answer, 'November 12.')
        self.assertEqual(metrics['usd'], .001)

    def test_stop_cancels_claude_tree_and_no_claude_is_an_error(self):
        class Worker:
            pid = 12345

            def communicate(self, *args, **kwargs):
                raise subprocess.TimeoutExpired('claude', 45)
        worker = Worker()
        with patch.object(atticus.shutil, 'which', return_value='claude'), patch.object(atticus.subprocess, 'Popen', return_value=worker), \
                patch.object(atticus, 'stop_claude') as stop, patch.object(atticus.signal, 'signal'), patch.object(atticus.atexit, 'register'):
            with self.assertRaises(subprocess.TimeoutExpired):
                atticus.meeting_call('prefix', 'question')
            stop.assert_called_once_with(worker)
        with patch.object(atticus.shutil, 'which', return_value=None), patch.object(atticus.subprocess, 'Popen') as start:
            with self.assertRaises(RuntimeError):
                atticus.meeting_call('prefix', 'question')
            start.assert_not_called()

    def run_main(self, args, data):
        with patch.object(sys, 'stdin', io.StringIO(json.dumps(data))), patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertTrue(atticus.meeting_main(args))
        return json.loads(out.getvalue())

    def test_answer_and_prep_workers_with_fake_claude(self):
        with patch.object(atticus, 'meeting_call', return_value=('GENERAL\nRayleigh scattering.', {'secs': 1, 'usd': .0004})) as call:
            r = self.run_main(['--meeting-answer'], {'question': 'Why is the sky blue?', 'brief': 'Cedar', 'transcript': [], 'now': 10})
        self.assertEqual(r['kind'], 'answer'); self.assertTrue(r['general']); self.assertEqual(r['usd'], .0004)
        self.assertIn('Why is the sky blue?', call.call_args.args[1])
        with patch.object(atticus, 'meeting_call', return_value=('Short brief', {'secs': 1, 'usd': 0})) as call:
            r = self.run_main(['--meeting-prep'], {'text': 'x' * 30000})
        self.assertEqual(r['brief'], 'Short brief')
        self.assertLessEqual(len(call.call_args.args[1]), 20100)
        with patch.object(atticus, 'meeting_call', side_effect=RuntimeError('offline')):
            self.assertEqual(self.run_main(['--meeting-answer'], {'question': 'x'}), {'error': 'Could not finish. Try again.'})
        self.assertIn('18,000', self.run_main(['--meeting-demo-brief'], {})['brief'])
        self.assertFalse(atticus.meeting_main(['--meetingz']))

    def test_demo_speaks_two_voices_through_windows_speech(self):
        with patch.object(atticus.subprocess, 'run') as run, patch.object(atticus.time, 'sleep'), patch('sys.stdout', new_callable=io.StringIO) as out, \
                patch.object(atticus, 'me', return_value={'name': 'Sam Lee'}):
            atticus.meeting_demo()
        rows = [json.loads(l) for l in out.getvalue().splitlines()]
        self.assertIn('18,000', rows[0]['brief'])
        self.assertEqual(len(rows), 5)
        self.assertEqual(run.call_count, 4)
        self.assertEqual(run.call_args_list[0].kwargs['env']['ATTICUS_SAY'], 'Sam, can you remind us of the approved pilot budget?')
        self.assertEqual([c.kwargs['env']['ATTICUS_VOICE'] for c in run.call_args_list], ['0', '1', '0', '1'])
        self.assertNotIn('Sam', run.call_args_list[0].args[0][-1], 'spoken words go in the environment, never the command line')


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for k, v in (('DIR', str(self.root)), ('PREFS', str(self.root / 'settings.json'))):   # never the user's settings or meetings
            p = patch.object(atticus, k, v); p.start(); self.addCleanup(p.stop)
        self.folder = self.root / 'meetings' / 'fake-call'
        self.folder.mkdir(parents=True, mode=0o700)
        (self.folder / 'metadata.json').write_text(json.dumps({'started': 1791745200, 'title': 'Cedar budget', 'app': 'Zoom'}))
        (self.folder / 'events.jsonl').touch()

    def link(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except OSError:
            self.skipTest('symlinks need Developer Mode on Windows')

    def test_incremental_stream_survives_kill_and_torn_tail(self):
        script = "import atticus,time; atticus.meeting_append('fake-call', {'kind':'transcript','text':'Launch Friday','speaker':'You','seconds':12}); time.sleep(30)"
        p = subprocess.Popen([sys.executable, '-c', script], cwd=Path(atticus.SELF).parent, env={**os.environ, 'ATTICUS_DIR': str(self.root)})
        self.addCleanup(lambda: p.poll() is None and p.kill())
        deadline = time.monotonic() + 10
        events = self.folder / 'events.jsonl'
        while not events.stat().st_size and time.monotonic() < deadline: time.sleep(.01)
        p.kill(); p.wait(timeout=5)
        with events.open('a') as f: f.write('{"kind":"trans')
        m = atticus.meeting_read('fake-call')
        self.assertEqual(m['transcript'][0]['text'], 'Launch Friday')
        self.assertEqual(m['duration'], 12)
        self.assertEqual(m['status'], 'Interrupted or listening')
        self.assertNotIn(b'\r\n', events.read_bytes(), 'journal lines stay LF on Windows')

    def test_search_date_export_delete_and_boundaries(self):
        atticus.meeting_append('fake-call', {'kind': 'transcript', 'text': 'We will follow up on pricing.', 'speaker': 'You', 'seconds': 62})
        atticus.meeting_append('fake-call', {'kind': 'ended', 'at': 1791745262, 'duration': 62})
        self.assertEqual(len(atticus.meeting_list('PRICING cedar')), 1)
        date = atticus.datetime.fromtimestamp(1791745200).strftime('%Y-%m-%d')
        self.assertIn('pricing', atticus.meeting_history(date))
        self.assertIn('pricing', atticus.meeting_history(atticus.datetime.fromtimestamp(1791745200).strftime('%A')))
        self.assertIn('pricing', atticus.meeting_history("What did we decide in " + atticus.datetime.fromtimestamp(1791745200).strftime('%A') + "'s meeting?"))
        self.assertEqual(atticus.meeting_history('nothing like this'), 'No saved meeting matches.')
        with patch.object(atticus.shutil, 'which', return_value=None):
            m = atticus.meeting_finish('fake-call')
        self.assertEqual(m['action_items'], ['We will follow up on pricing.'])
        md = atticus.meeting_command({'action': 'export', 'id': 'fake-call'})['markdown']
        self.assertIn('[00:01:02] You: We will', md)
        for bad in ['../settings', '', None, '/tmp', 'fake-call/metadata.json', 'C:\\Windows', '..\\x']:
            with self.assertRaises(ValueError): atticus.meeting_path(bad)
        with self.assertRaises(ValueError): atticus.meeting_command({'action': 'rename', 'id': 'fake-call'})
        result = atticus.meeting_command({'action': 'delete', 'id': 'fake-call'})
        self.assertEqual(result['deleted'], 'fake-call'); self.assertFalse(self.folder.exists())
        self.assertEqual(atticus.meeting_command({'action': 'clear'}), {'meetings': []})
        self.assertFalse((self.root / 'meetings').exists())

    def test_cli_reports_errors_as_json(self):
        with patch.object(sys, 'stdin', io.StringIO('{"action":"get","id":"missing"}')), patch('sys.stdout', new_callable=io.StringIO) as out:
            atticus.meeting_main(['--meetings'])
        self.assertEqual(json.loads(out.getvalue()), {'error': 'Could not open saved meetings. Try again.'})

    def test_linked_folders_and_files_are_refused(self):
        self.link(self.root / 'meetings' / 'alias', self.folder)
        with self.assertRaises(ValueError): atticus.meeting_path('alias')
        for name in ['metadata.json', 'events.jsonl']:
            original = (self.folder / name).read_text()
            (self.root / 'secret').write_text(original)
            (self.folder / name).unlink(); self.link(self.folder / name, self.root / 'secret')
            with self.assertRaises(OSError): atticus.meeting_read('fake-call')
            self.assertEqual([m['id'] for m in atticus.meeting_list()], [])
            (self.folder / name).unlink(); (self.folder / name).write_text(original)

    def test_summary_reads_the_transcript_once_only_when_switched_on(self):
        atticus.meeting_append('fake-call', {'kind': 'transcript', 'text': 'We agreed on the budget', 'seconds': 10})
        quiet = self.root / 'meetings' / 'quiet-call'; quiet.mkdir()
        (quiet / 'metadata.json').write_text(json.dumps({'started': 1791745200, 'title': 'Quiet', 'app': 'Zoom'})); (quiet / 'events.jsonl').touch()
        atticus.meeting_append('quiet-call', {'kind': 'transcript', 'text': 'PRIVATE WORDS', 'seconds': 10})
        with patch.object(atticus.shutil, 'which', return_value='C:/fake/claude.exe'), patch.object(atticus, 'meeting_call', return_value=('```json\n{"summary":"Budget agreed","action_items":[]}\n```', {})) as call:
            atticus.meeting_finish('fake-call'); atticus.meeting_finish('fake-call')
            call.assert_called_once()
            self.assertIn('Them: We agreed on the budget', str(call.call_args))
            self.assertEqual(atticus.meeting_read('fake-call')['summary'], 'Budget agreed')
            call.reset_mock()
            with patch.object(atticus, 'pref', side_effect=lambda k, d=None: False if k == 'meetingSummary' else d):
                result = atticus.meeting_finish('quiet-call')
            call.assert_not_called()   # off: nothing leaves the PC, the local recap stays
            self.assertIn('PRIVATE WORDS', result['summary'])

    def test_failed_summary_keeps_transcript_and_local_recap(self):
        atticus.meeting_append('fake-call', {'kind': 'transcript', 'text': 'Opening words', 'seconds': 0})
        atticus.meeting_append('fake-call', {'kind': 'notes', 'text': 'Notes'})
        atticus.meeting_append('fake-call', {'kind': 'answer', 'lines': ['Answer']})
        for failure in [RuntimeError('offline'), None]:
            with patch.object(atticus.shutil, 'which', return_value='C:/fake/claude.exe'), \
                    patch.object(atticus, 'meeting_call', side_effect=failure, return_value=('not json', {})):
                result = atticus.meeting_finish('fake-call')
            self.assertIn('Opening words', result['summary'])
            self.assertEqual(len(result['transcript']), 1)
            self.assertEqual(result['notes'], 'Notes')

    def test_partial_recovery_and_finalization_do_not_duplicate(self):
        atticus.meeting_append('fake-call', {'kind': 'partial', 'you': 'I will follow up', 'them': 'Launch Fri', 'seconds': 2})
        self.assertEqual(len(atticus.meeting_read('fake-call')['transcript']), 2)
        atticus.meeting_append('fake-call', {'kind': 'transcript', 'mic': False, 'speaker': 'Zoom', 'text': 'Launch Friday.', 'seconds': 3})
        m = atticus.meeting_read('fake-call')
        self.assertEqual([r['text'] for r in m['transcript']], ['Launch Friday.', 'I will follow up'])
        self.assertTrue(m['transcript'][-1]['recovered'])

    def test_resume_discards_in_flight_recap_and_finishes_full_transcript(self):
        atticus.meeting_append('fake-call', {'kind': 'transcript', 'text': 'Opening decision', 'seconds': 10})
        atticus.meeting_append('fake-call', {'kind': 'ended', 'at': 1, 'duration': 10})

        def resume(system, said):
            atticus.meeting_append('fake-call', {'kind': 'resumed', 'duration': 10})
            atticus.meeting_append('fake-call', {'kind': 'transcript', 'text': 'Resumed decision', 'seconds': 20})
            return '{"summary":"STALE RECAP","action_items":[]}', {}
        with patch.object(atticus.shutil, 'which', return_value='C:/fake/claude.exe'), patch.object(atticus, 'meeting_call', side_effect=resume):
            result = atticus.meeting_finish('fake-call')
        self.assertNotIn('STALE RECAP', (self.folder / 'events.jsonl').read_text(encoding='utf-8'))
        self.assertEqual(result['summary'], '')
        atticus.meeting_append('fake-call', {'kind': 'ended', 'at': 2, 'duration': 20})
        with patch.object(atticus.shutil, 'which', return_value='C:/fake/claude.exe'), patch.object(atticus, 'meeting_call', return_value=('{"summary":"Full recap","action_items":[]}', {})) as call:
            result = atticus.meeting_finish('fake-call'); atticus.meeting_finish('fake-call')
        self.assertEqual(result['summary'], 'Full recap'); call.assert_called_once()
        self.assertIn('Resumed decision', call.call_args.args[1])
        self.assertEqual(len(result['transcript']), 2)

    def test_summary_lock_waits_for_the_other_worker_and_a_dead_one_never_blocks(self):
        (self.folder / 'summary.once').touch()
        atticus.meeting_append('fake-call', {'kind': 'transcript', 'text': 'Survives cancellation', 'seconds': 10})
        holder = ("import atticus,sys,time\nwith atticus._Lock(sys.argv[1]):\n print('held', flush=True); time.sleep(1.5)\n")
        p = subprocess.Popen([sys.executable, '-c', holder, str(self.folder / 'summary.once')], cwd=Path(atticus.SELF).parent, stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: p.poll() is None and p.kill()); self.addCleanup(p.stdout.close)
        self.assertEqual(p.stdout.readline().strip(), 'held')
        started = time.monotonic()
        with patch.object(atticus.shutil, 'which', return_value=None):
            result = atticus.meeting_finish('fake-call')
        self.assertGreater(time.monotonic() - started, .5, 'the second recap did not wait for the first')
        self.assertIn('Survives cancellation', result['summary'])
        p.wait(timeout=5)
        killed = subprocess.Popen([sys.executable, '-c', holder.replace('1.5', '30'), str(self.folder / 'summary.once')], cwd=Path(atticus.SELF).parent, stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: killed.poll() is None and killed.kill()); self.addCleanup(killed.stdout.close)
        self.assertEqual(killed.stdout.readline().strip(), 'held')
        killed.kill(); killed.wait(timeout=5)
        atticus.meeting_append('fake-call', {'kind': 'transcript', 'text': 'After a cancelled recap', 'seconds': 20})
        with patch.object(atticus.shutil, 'which', return_value=None):
            self.assertIn('After a cancelled recap', atticus.meeting_finish('fake-call')['summary'])


if __name__ == '__main__':
    unittest.main()
