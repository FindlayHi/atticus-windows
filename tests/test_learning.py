"""Learned commands, saved procedures, history, Try again and failure reports (ported from atticus-mac).
Scratch storage only: no Claude calls, no real apps, no network."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import atticus


class LearningTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        for p in (patch.object(atticus, 'DIR', root.name), patch.object(atticus, 'SKILLS', str(self.root / 'skills.json')),
                  patch.object(atticus, 'LAST', str(self.root / 'last_app')), patch.object(atticus, 'pref', side_effect=lambda k, d: d),
                  patch.dict(os.environ, {'ATTICUS_LIVE': ''})):
            p.start()
            self.addCleanup(p.stop)

    def test_when_i_say_runs_now_and_saves_the_words_with_no_ai(self):
        with patch.object(atticus, 'app_named', side_effect=lambda w: 'Notepad' if w == 'notepad' else None), \
             patch.object(atticus, 'open_app', return_value=True) as opened, patch.object(atticus, 'tip') as tip:
            self.assertEqual(atticus.fast('When I say writing time, open Notepad'), 'Opened Notepad.')
        opened.assert_called_once_with('Notepad')
        saved = atticus.skills()['writing time']
        self.assertEqual(saved['cmds'], [f"{atticus.SHOW} Notepad"])
        self.assertTrue(saved['mine'])   # the user's own words never expire
        self.assertIn('Learned', tip.call_args.args[1])
        with patch.object(atticus.subprocess, 'run', return_value=unittest.mock.Mock(returncode=0, stdout='opened Notepad in front', stderr='')) as replayed, \
             patch.object(atticus, 'shim_env', return_value={}):
            self.assertEqual(atticus.fast('Writing time.'), 'Opened Notepad.')
        self.assertEqual(replayed.call_args.args[0][-1], f'{atticus.SHOW} Notepad')

    def test_learned_task_replays_in_any_order_and_for_another_app(self):
        apps = {'notepad': 'Notepad', 'paint': 'Paint'}
        with patch.object(atticus, 'app_named', side_effect=apps.get):
            atticus.learn('put notepad on the left', [f'{atticus.PLACE} Notepad left current'], 'Notepad is on the left.')
            self.assertEqual(atticus.like(atticus.gist('on the left put notepad'), atticus.skills()), ('put notepad on the left', None))
            k, swap = atticus.like(atticus.gist('put paint on the left'), atticus.skills())
            self.assertEqual((k, swap), ('put notepad on the left', ('Notepad', 'Paint')))
            ok = unittest.mock.Mock(returncode=0, stdout='Paint window -> left of DELL', stderr='')
            with patch.object(atticus.subprocess, 'run', return_value=ok) as run, patch.object(atticus, 'shim_env', return_value={}):
                self.assertEqual(atticus.replay(k, swap), 'Paint is on the left.')
            self.assertEqual(run.call_args.args[0][-1], f'{atticus.PLACE} Paint left current')

    def test_a_replay_step_that_says_it_failed_hands_over_to_the_ai(self):
        atticus.save_skills({'open budget': {'cmds': [f'{atticus.SHOW} budget.xlsx'], 'reply': 'Opened.', 'mine': True}})
        bad = unittest.mock.Mock(returncode=0, stdout='could not open budget.xlsx', stderr='')
        with patch.object(atticus.subprocess, 'run', return_value=bad), patch.object(atticus, 'shim_env', return_value={}):
            self.assertIsNone(atticus.replay('open budget'))

    def test_shortcuts_keep_actions_drop_looking_around_and_refuse_what_needs_the_ai(self):
        sc = atticus.shortcut_commands
        self.assertEqual(sc('x', [f'{atticus.LOOK}; {atticus.SHOW} Spotify; {atticus.PLAY} "jazz"']), [f'{atticus.SHOW} Spotify', f"{atticus.PLAY} jazz"])
        self.assertEqual(sc('x', [f'{atticus.PLACE} Chrome left current', f'{atticus.PLACE} Code right current']),
                         [f'{atticus.PLACE} Chrome left current + Code right current'])   # back-to-back moves go at once
        self.assertIsNone(sc('x', [f'{atticus.CLIP} | grep x']))   # pipes need the AI
        self.assertIsNone(sc('x', [f'{atticus.WINDOWS} Chrome && {atticus.PLACE} Chrome left current']))   # a check it acts on can't be dropped
        self.assertEqual(sc('x', [f'{atticus.PLACE} Chrome left current && {atticus.LOOK}']), [f'{atticus.PLACE} Chrome left current'])   # a check after the act can

    def test_unused_learned_tasks_expire_but_the_users_own_stay(self):
        old = time.strftime('%Y-%m-%d', time.localtime(time.time() - 40 * 86400))
        atticus.save_skills({'a': {'cmds': ['x'], 'reply': 'A', 'at': old}, 'b': {'cmds': ['x'], 'reply': 'B', 'at': old, 'mine': True},
                             'c': {'cmds': ['x'], 'reply': 'C', 'at': time.strftime('%Y-%m-%d')}})
        self.assertEqual(set(atticus.skills()), {'b', 'c'})

    def test_moves_in_any_word_order(self):
        with patch.object(atticus, 'app_named', side_effect=lambda w: {'chrome': 'Google Chrome', 'notepad': 'Notepad'}.get(w)):
            self.assertEqual(atticus.move_target('full screen chrome on my big screen'), ('Google Chrome', 'full', 'monitor'))
            self.assertEqual(atticus.move_target('move chrome to the external display and maximize it'), None)   # "it" left over: the AI decides
            self.assertEqual(atticus.move_target('put chrome on the left of the laptop'), ('Google Chrome', 'left', 'laptop'))
            self.assertIsNone(atticus.move_target("what's on my monitor"))
            with patch.object(atticus, 'place', return_value='Google Chrome window -> left of DELL') as place, patch.object(atticus, 'proc_of', side_effect=lambda a: a):
                self.assertEqual(atticus.fast('Chrome left, Notepad right'), 'Google Chrome: left. Notepad: right.')
            self.assertEqual(place.call_count, 2)
            self.assertEqual(atticus.fast.cmds, [f"{atticus.PLACE} 'Google Chrome' left current + Notepad right current"])

    def test_procedures_are_saved_from_verified_tasks_and_follow_the_words(self):
        reply, saved = atticus.remember_workflow('make a calendar event',
            'Created it. [workflow create calendar event | Open Outlook calendar, fill the form, save; check the event shows.]', True, ['x'])
        self.assertEqual((reply, saved), ('Created it.', True))
        self.assertIn('fill the form', atticus.workflows('Can you create a new calendar event tomorrow?'))
        self.assertEqual(atticus.workflows('open calendar'), '')
        for reply, ok, cmds in [('Done. [workflow a b | c]', False, ['x']), ('Could not do it. [workflow a b | c]', True, ['x']), ('Done. [workflow a b | c]', True, [])]:
            self.assertFalse(atticus.remember_workflow('a b', reply, ok, cmds)[1])
        self.assertIn('saved the procedure', atticus.fast('remember how to create a calendar event'))
        atticus.unlearn('make a new calendar event')   # "no, that's wrong" drops it, whatever the wording
        self.assertEqual(atticus.workflows('create calendar event'), '')

    def test_ai_replay_tags_are_only_saved_when_safe(self):
        atticus.remember_workflow('open my music', f'Playing. [workflow open my music | Play the playlist.] [replay {atticus.PLAY} "focus"]', True, ['x'])
        self.assertEqual(atticus.skills()['open my music']['cmds'], [f'{atticus.PLAY} "focus"'])
        for bad in ['rm -rf ~/x', f'{atticus.UI} click B3', f'{atticus.TRASH} ~/x']:
            atticus.remember_workflow('do thing', f'Done. [workflow do thing | a] [replay {bad}]', True, ['x'])
            self.assertNotIn('do thing', atticus.skills())

    def test_searches_on_other_sites_go_to_the_ai(self):
        with patch.object(atticus, 'show') as opened:
            self.assertIsNone(atticus.fast('Search for a pancake recipe on allrecipes'))
            self.assertIsNone(atticus.fast('search for a desk lamp at ikea.com'))
            opened.assert_not_called()
            self.assertEqual(atticus.fast('search for lamps on amazon'), 'Searched lamps on Amazon.')
            self.assertEqual(atticus.fast('search for weather in toronto'), 'Searched weather in toronto.')
        self.assertTrue(all(c.kwargs.get('bg') for c in opened.call_args_list))   # behind the user's app, never over it

    def test_history_finds_past_asks_by_words_or_date(self):
        with open(self.root / 'log.jsonl', 'w') as f:
            f.write(json.dumps({'at': '2026-10-10 09:00:00', 'task': 'find the lamp link', 'reply': 'https://example.com/lamp'}) + '\n')
            f.write('not json\n' + json.dumps({'at': '2026-10-11 10:00:00', 'task': 'weather', 'reply': 'Sunny.'}) + '\n')
        self.assertIn('example.com/lamp', atticus.history('that lamp link'))
        self.assertIn('Sunny', atticus.history('2026-10-11'))
        self.assertEqual(atticus.history('zebra'), 'Nothing matches.')

    def test_all_rules_go_while_they_fit_and_earlier_answers_keep_more(self):
        mem = self.root / 'memory.md'
        with patch.object(atticus, 'MEMORY', str(mem)):
            mem.write_text('When I say dad, use the contact Papa.\n' + 'Keep replies short and friendly please.\n' * 40)
            self.assertIn('dad', atticus.relevant_rules('email Papa'))
        text = atticus.earlier_chat([{'q': f'q{i}', 'a': str(i) * 2000} for i in range(3)])
        self.assertIn('A: ' + '0' * 1000 + '\n', text)
        self.assertIn('A: ' + '2' * 2000 + '\n', text)

    def test_tool_help_never_runs_gui_apps_or_windows_own_tools(self):
        if sys.platform != 'win32':
            self.skipTest('Windows executables')
        windir = os.environ.get('SystemRoot', r'C:\Windows')
        self.assertFalse(atticus.console_exe(os.path.join(windir, 'notepad.exe')))   # GUI: --help would open it
        self.assertTrue(atticus.console_exe(os.path.join(windir, 'System32', 'where.exe')))
        with patch.object(atticus.subprocess, 'run') as run:
            self.assertEqual(atticus.tool_help('open notepad and run where'), '')   # Windows' own folder
        run.assert_not_called()

    def test_failure_reports_need_a_key_and_a_yes_and_are_cleaned(self):
        with patch.object(atticus, 'posthog') as send:
            atticus.report('email sam@x.com', 'failed', [], 1, 'error')   # no key in this build
            with patch.object(atticus, 'POSTHOG_KEY', 'k'):
                atticus.report('x', 'failed', [], 1, 'error')   # the user hasn't said yes
                with patch.object(atticus, 'pref', side_effect=lambda k, d: True if k == 'improve' else d):
                    atticus.report('email sam@x.com about C:\\Users\\me\\taxes.pdf', 'failed', [f'{atticus.TYPE} --in Discord "hi Sam"'], 1, 'error')
        send.assert_called_once()
        props = send.call_args.args[1]
        self.assertNotIn('sam@x.com', props['task']); self.assertNotIn('taxes', props['task']); self.assertNotIn('hi Sam', props['steps'][0])

    def test_failed_task_offers_retry_and_sonnet_reruns_once(self):
        for answer, model, expected in [('r', 'haiku', 'haiku'), ('y', 'haiku', 'sonnet'), ('', 'haiku', None)]:
            with self.subTest(answer=answer):
                def attempt(prompt, *args):
                    brain.ok = brain.call_count > 1
                    brain.cmds, brain.ran = [], ['step']
                    return ('Finished.' if brain.ok else 'Fixture failed.'), .001, ['fixture step']
                brain = unittest.mock.Mock(side_effect=attempt)
                with patch.multiple(atticus, licensed=lambda: True, ping=lambda: None, fast=lambda _: None, front=lambda: ('Notepad', ''),
                                    screens=lambda: [], describe=lambda *a: '', me_line=lambda: '', relevant_rules=lambda _: '', recent=lambda: [],
                                    screen_for=lambda *a: None, wait_commit=lambda: True, run=brain, remember_turn=lambda *a: None,
                                    restore_focus=lambda *a: None, tip=lambda *a: None, look=lambda: [], tool_help=lambda _: ''), \
                     patch.object(atticus, 'pref', side_effect=lambda k, d: {'model': model}.get(k, d)), \
                     patch.object(atticus, 'retry_offer', return_value=answer) as offer, patch.object(atticus, 'start_claude', return_value=object()) as start, \
                     patch.object(atticus, 'log_turn') as log, patch.object(atticus.winctl, 'find', return_value=[]), \
                     patch.object(atticus.integrations, 'mcp_for', return_value={}), patch.object(atticus.integrations, 'hints', return_value=''), \
                     patch('sys.argv', ['atticus.py', 'Do fixture']), patch('sys.stdout', io.StringIO()):
                    atticus.main()
                offer.assert_called_once_with(True)   # on Haiku, the card offers Sonnet too
                self.assertEqual(brain.call_count, 2 if expected else 1)
                if expected:
                    start.assert_called_once_with({}, expected)
                    self.assertIn('A previous try failed: Fixture failed.', brain.call_args.args[0])
                    self.assertEqual(log.call_args.args[3], .002)
                else:
                    start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
