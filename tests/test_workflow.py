"""End-to-end checks of the educational model, not EDA correctness."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DemoWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        for source in ROOT.glob('*.py'):
            shutil.copy2(source, cls.root)
        shutil.copytree(ROOT / 'libs', cls.root / 'libs')
        cls.output = cls.run_commands([
            'cd ~/projects/sky130_demo',
            'dc_shell -f scripts/run_dc.tcl',
            'pt_shell -f scripts/run_pt.tcl',
            'fm_shell -f scripts/run_fm.tcl',
            'dc_shell -f scripts/run_corners.tcl',
            'exit',
        ])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @classmethod
    def run_commands(cls, commands):
        result = subprocess.run(
            [sys.executable, str(cls.root / 'dc_shell_sim.py'), '--no-color'],
            input='\n'.join(commands) + '\n', text=True, encoding='utf-8',
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cls.root,
            env=dict(os.environ, SIM_LINEEDIT='0', SIM_HIGHLIGHT='0', PYTHONIOENCODING='utf-8'),
            timeout=120,
        )
        if result.returncode:
            raise AssertionError(result.stderr + result.stdout)
        if 'Traceback' in result.stderr or 'Error:' in result.stdout:
            raise AssertionError(result.stderr + result.stdout)
        return result.stdout

    def test_dc_pt_fm_workflow(self):
        self.assertIn('check_timing succeeded.', self.output)
        self.assertIn('Verification SUCCEEDED', self.output)
        project = self.root / 'home/projects/sky130_demo'
        self.assertTrue(any((project / 'outputs').glob('*.v')))
        self.assertTrue(any((project / 'outputs').glob('*.sdc')))

    def test_corner_slack_changes_sign(self):
        import re
        reports = self.root / 'home/projects/sky130_demo/reports'
        slacks = {}
        for corner in ('tt_025C_1v80', 'ss_n40C_1v40', 'ff_n40C_1v95'):
            data = (reports / ('qor_' + corner + '.rpt')).read_text(encoding='utf-8')
            slacks[corner] = float(re.search(r'Critical Path Slack:\s*([-\d.]+)', data).group(1))
        self.assertLess(slacks['ss_n40C_1v40'], 0)
        self.assertGreater(slacks['tt_025C_1v80'], 0)
        self.assertGreater(slacks['ff_n40C_1v95'], slacks['tt_025C_1v80'])

    def test_modified_rtl_is_detected(self):
        # Use a fresh netlist before changing the reference RTL.
        self.run_commands(['cd ~/projects/sky130_demo', 'dc_shell -f scripts/run_dc.tcl', 'exit'])
        rtl = self.root / 'home/projects/sky130_demo/rtl/counter.v'
        original = rtl.read_text(encoding='utf-8')
        self.assertIn('+', original)
        try:
            rtl.write_text(original.replace('+', '-', 1), encoding='utf-8')
            output = self.run_commands(['cd ~/projects/sky130_demo', 'fm_shell -f scripts/run_fm.tcl', 'exit'])
            self.assertIn('Verification FAILED', output)
        finally:
            rtl.write_text(original, encoding='utf-8')


if __name__ == '__main__':
    unittest.main()
