import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from orchestrator.console import login_flow as f


class LoginFlowTests(unittest.TestCase):
    def test_rejects_foreign_or_insecure_url(self):
        self.assertTrue(f.valid_url('https://copilot.tencent.com/login?a=b'))
        for url in ['http://copilot.tencent.com', 'https://copilot.tencent.com.evil.test', 'javascript:alert(1)']:
            self.assertFalse(f.valid_url(url))

    def test_duplicate_start_preserves_active_flow(self):
        root=Path('test-login-dedup')
        with patch.object(f.threading, 'Thread') as thread:
            f.start(root)
            f.start(root)
            self.assertEqual(thread.call_count, 1)
        f._states.pop(str(root))

    def test_spawn_failure_is_visible(self):
        root=Path('test-login-failure')
        with patch.object(f.subprocess, 'Popen', side_effect=OSError('private detail')):
            f._run(root)
        result=f.status(root)
        self.assertEqual(result['state'], 'failed')
        self.assertNotIn('private detail', str(result))
        f._states.pop(str(root))
