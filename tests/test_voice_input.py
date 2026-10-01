import json
import socket
import subprocess
import threading
import tempfile
import time
from pathlib import Path
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import voice_input as app


class InjectionTests(unittest.TestCase):
    def test_unknown_window_still_attempts_paste(self):
        with patch.object(app, 'IS_WINDOWS', False), patch.object(app.subprocess, 'run'), patch.object(app, 'get_active_window_class', return_value=''), patch.object(app, 'linux_send_key', return_value={'ok': True}) as send:
            result = app.inject_text('中文', 'auto')
        self.assertTrue(result['typed'])
        send.assert_called_once_with('paste')

    def test_clipboard_timeout_is_not_success(self):
        with patch.object(app, 'IS_WINDOWS', False), patch.object(app.subprocess, 'run', side_effect=subprocess.TimeoutExpired('wl-copy', 3)):
            result = app.inject_text('中文')
        self.assertFalse(result['ok'])
        self.assertFalse(result['copied'])

    def test_background_clipboard_owner_does_not_hold_request_open(self):
        # Reproduce wl-copy's fork: the child retains stderr after parent exit.
        import os
        if os.name != 'posix':
            self.skipTest('wl-copy is a Linux backend')
        with tempfile.TemporaryDirectory() as directory:
            command = Path(directory) / 'wl-copy'
            command.write_text('#!/usr/bin/python3\nimport os,time\nif os.fork()==0:\n time.sleep(2)\n os._exit(0)\n')
            command.chmod(0o755)
            env = {**app.APP_ENV, 'PATH': directory}
            before = time.monotonic()
            with patch.object(app, 'IS_WINDOWS', False), patch.object(app, 'APP_ENV', env):
                result = app.inject_text('中文', 'clipboard_only')
            self.assertTrue(result['ok'], result)
            self.assertLess(time.monotonic() - before, 1)

    def test_keyboard_failure_preserves_clipboard(self):
        with patch.object(app, 'IS_WINDOWS', False), patch.object(app.subprocess, 'run'), patch.object(app, 'get_active_window_class', return_value=''), patch.object(app, 'linux_send_key', return_value={'ok': False, 'error': '不可用'}):
            result = app.inject_text('中文')
        self.assertTrue(result['ok'])
        self.assertTrue(result['copied'])
        self.assertFalse(result['typed'])
        self.assertIn('手动粘贴', result['warning'])


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.VoiceRequestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:%s' % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def post(self, data):
        request = urllib.request.Request(self.url + '/api/type', data=json.dumps(data).encode(), headers={'Content-Type': 'application/json'})
        try:
            response = urllib.request.urlopen(request, timeout=2)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_idle_connection_does_not_block_health(self):
        idle = socket.create_connection(self.server.server_address)
        try:
            with urllib.request.urlopen(self.url + '/api/status', timeout=1) as response:
                self.assertEqual(json.load(response)['status'], 'ok')
        finally:
            idle.close()

    def test_invalid_json_values_are_client_errors(self):
        with patch.object(app, 'inject_text') as inject:
            for data in [[], {'text': 42}, {'text': ' '}, {'text': 'abc', 'mode': 'bad'}, {'text': 'abc', 'enter': 'false'}]:
                self.assertEqual(self.post(data)[0], 400)
            inject.assert_not_called()

    def test_clipboard_only_does_not_press_enter(self):
        result = {'ok': True, 'copied': True, 'typed': False}
        with patch.object(app, 'inject_text', return_value=result), patch.object(app, 'play_feedback_sound'), patch.object(app, 'press_key') as press:
            status, body = self.post({'text': '中文', 'mode': 'clipboard_only', 'enter': True})
            self.assertEqual(status, 200)
            self.assertFalse(body['entered'])
            press.assert_not_called()

    def test_enter_failure_is_reported_after_paste(self):
        result = {'ok': True, 'copied': True, 'typed': True}
        with patch.object(app, 'inject_text', return_value=result), patch.object(app, 'play_feedback_sound'), patch.object(app, 'press_key', return_value={'ok': False, 'error': '回车失败'}):
            status, body = self.post({'text': '中文', 'enter': True})
            self.assertEqual(status, 200)
            self.assertFalse(body['entered'])
            self.assertEqual(body['warning'], '回车失败')


if __name__ == '__main__':
    unittest.main()
