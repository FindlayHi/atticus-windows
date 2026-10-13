"""Syntax checks for the embedded UI modules using the installed Node runtime."""
from pathlib import Path
import re
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node is optional for UI syntax checks')
class JavaScriptTests(unittest.TestCase):
    def test_embedded_scripts_parse(self):
        root = Path(__file__).resolve().parents[1]
        files = list((root / 'ui').glob('*.html')) + list((root / 'tests').glob('*.html'))
        for path in files:
            for attrs, code in re.findall(r'<script([^>]*)>(.*?)</script>', path.read_text(), re.S):
                if 'text/plain' in attrs or 'src=' in attrs:
                    continue
                with self.subTest(file=path.name):
                    p = subprocess.run(['node', '--check', '--input-type=module'], input=code, capture_output=True, text=True)
                    self.assertEqual(p.returncode, 0, p.stderr)

    def test_page_script_parses(self):   # ui/web.js runs in the user's browser tab through --web
        root = Path(__file__).resolve().parents[1]
        p = subprocess.run(['node', '--check', str(root / 'ui' / 'web.js')], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)


if __name__ == '__main__':
    unittest.main()
