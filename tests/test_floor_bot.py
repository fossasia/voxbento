import json
import os
import subprocess
import sys
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch

# Add floor-bot directory to sys.path so we can import bot
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "floor-bot")))

import bot


class TestWaitForProc(unittest.TestCase):
    """Unit tests for the _wait_for_proc helper function."""

    def test_wait_for_proc_already_exited(self):
        """Verify that already-exited processes are not sent signals."""
        proc = MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = 0
        bot._wait_for_proc(proc, "test-room")
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()

    def test_wait_for_proc_clean_exit(self):
        """Verify graceful exit when process responds to SIGTERM."""
        proc = MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = None
        proc.wait.return_value = 0

        bot._wait_for_proc(proc, "test-room")
        proc.terminate.assert_called_once()
        proc.wait.assert_called_once_with(timeout=5)
        proc.kill.assert_not_called()

    def test_wait_for_proc_sigkill_fallback(self):
        """Verify fallback to SIGKILL when SIGTERM times out."""
        proc = MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = None
        # First wait times out, second wait succeeds after kill
        proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="test", timeout=5), 0]

        bot._wait_for_proc(proc, "test-room")
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        self.assertEqual(proc.wait.call_count, 2)
        proc.wait.assert_called_with(timeout=3)

    def test_wait_for_proc_sigkill_timeout_tolerated(self):
        """Verify that SIGKILL timeouts do not crash the teardown thread."""
        proc = MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = None
        # Both waits time out
        proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="test", timeout=5),
            subprocess.TimeoutExpired(cmd="test", timeout=3),
        ]

        bot._wait_for_proc(proc, "test-room")
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()

    def test_wait_for_proc_unexpected_exception_handled(self):
        """Verify unexpected errors during termination are caught and kill is attempted."""
        proc = MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = None
        proc.terminate.side_effect = RuntimeError("Process lookup error")

        bot._wait_for_proc(proc, "test-room")
        proc.kill.assert_called_once()


class TestSubprocessManager(unittest.TestCase):
    """Unit tests for SubprocessManager methods."""

    def setUp(self):
        """Initialize a fresh SubprocessManager instance."""
        self.mgr = bot.SubprocessManager()

    def test_stop_room_async_not_in_rooms(self):
        """Verify stopping a nonexistent room is a safe no-op."""
        self.mgr.stop_room_async("nonexistent-1")
        self.assertEqual(self.mgr.stopping_threads, {})

    def test_stop_room_async_eager_removal_and_background_teardown(self):
        """Verify eager dictionary cleanup and eventual state transition to stopped."""
        proc = MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = None
        release_wait = threading.Event()

        def slow_wait(*args, **kwargs):
            release_wait.wait(timeout=2.0)
            return 0

        proc.wait.side_effect = slow_wait

        key = "event-1"
        self.mgr.rooms[key] = proc
        self.mgr.room_states[key] = "in_meeting"
        self.mgr.room_failures[key] = "some_error"
        self.mgr.room_meta[key] = {"event_slug": "event", "room_id": 1}

        self.mgr.stop_room_async(key)

        # Immediately after stop_room_async returns:
        # Key is removed from rooms, meta, failures, and marked stopping
        self.assertNotIn(key, self.mgr.rooms)
        self.assertNotIn(key, self.mgr.room_meta)
        self.assertNotIn(key, self.mgr.room_failures)
        self.assertEqual(self.mgr.room_states[key], "stopping")
        self.assertIn(key, self.mgr.stopping_threads)

        # Allow teardown to finish
        release_wait.set()
        self.mgr.wait_for_all_stops(timeout=2.0)

        # After teardown finishes:
        self.assertEqual(self.mgr.room_states[key], "stopped")
        self.assertNotIn(key, self.mgr.stopping_threads)

    def test_stop_room_sync_waits_for_stopping_thread(self):
        """Verify synchronous stop_room joins any active teardown thread."""
        proc = MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = None
        release_wait = threading.Event()

        def slow_wait(*args, **kwargs):
            release_wait.wait(timeout=2.0)
            return 0

        proc.wait.side_effect = slow_wait

        key = "event-1"
        self.mgr.rooms[key] = proc
        self.mgr.stop_room_async(key)

        # Ensure background thread is running
        self.assertIn(key, self.mgr.stopping_threads)

        # Unblock and call synchronous stop_room
        release_wait.set()
        self.mgr.stop_room(key)
        self.assertEqual(self.mgr.room_states[key], "stopped")
        self.assertNotIn(key, self.mgr.stopping_threads)

    def test_start_room_waits_for_stopping_thread(self):
        """Verify start_room coordinates with an active stopping thread before spawning."""
        proc = MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = None
        release_wait = threading.Event()

        def slow_wait(*args, **kwargs):
            release_wait.wait(timeout=2.0)
            return 0

        proc.wait.side_effect = slow_wait

        key = "event-1"
        self.mgr.rooms[key] = proc
        self.mgr.stop_room_async(key)

        started = False
        mock_p = MagicMock()
        mock_p.stdout.readline.return_value = ""
        mock_p.stderr.readline.return_value = ""

        with patch.object(bot.subprocess, "Popen", return_value=mock_p):
            def mock_start():
                nonlocal started
                self.mgr.start_room("event", 1, "https://meet.example.com", "rtsp://mediamtx:8554")
                started = True

            t = threading.Thread(target=mock_start)
            t.start()

            # Before teardown completes, started should still be False
            time.sleep(0.05)
            self.assertFalse(started)

            # Release teardown
            release_wait.set()
            t.join(timeout=2.0)
            self.assertTrue(started)
            self.assertIn(key, self.mgr.rooms)

    def test_start_room_restarts_running_room_asynchronously(self):
        """Verify start_room detaches an already running room without holding lock during wait."""
        old_proc = MagicMock(spec=subprocess.Popen)
        old_proc.poll.return_value = None
        release_wait = threading.Event()

        def slow_wait(*args, **kwargs):
            release_wait.wait(timeout=2.0)
            return 0

        old_proc.wait.side_effect = slow_wait

        key = "event-1"
        self.mgr.rooms[key] = old_proc
        self.mgr.room_states[key] = "in_meeting"

        started = False
        mock_p = MagicMock()
        mock_p.stdout.readline.return_value = ""
        mock_p.stderr.readline.return_value = ""

        with patch.object(bot.subprocess, "Popen", return_value=mock_p):
            def mock_start():
                nonlocal started
                self.mgr.start_room("event", 1, "https://meet.example.com", "rtsp://mediamtx:8554")
                started = True

            t = threading.Thread(target=mock_start)
            t.start()

            # While old_proc is tearing down, manager.lock should be free for other callers
            time.sleep(0.05)
            self.assertFalse(started)
            acquired = self.mgr.lock.acquire(timeout=0.5)
            self.assertTrue(acquired)
            self.mgr.lock.release()

            # Release teardown
            release_wait.set()
            t.join(timeout=2.0)
            self.assertTrue(started)
            self.assertIn(key, self.mgr.rooms)
            self.assertNotEqual(self.mgr.rooms[key], old_proc)

    def test_concurrent_start_room_serialized_under_lock(self):
        """Verify concurrent start_room calls for the same room are safely serialized."""
        key = "event-1"
        procs_created = []
        first_call_event = threading.Event()
        release_first_event = threading.Event()

        def fake_popen(*args, **kwargs):
            mock_p = MagicMock()
            mock_p.stdout.readline.return_value = ""
            mock_p.stderr.readline.return_value = ""
            mock_p.poll.return_value = None
            mock_p.wait.return_value = 0
            procs_created.append(mock_p)
            if len(procs_created) == 1:
                first_call_event.set()
                release_first_event.wait(timeout=2.0)
            return mock_p

        with patch.object(bot.subprocess, "Popen", side_effect=fake_popen):
            t1 = threading.Thread(
                target=lambda: self.mgr.start_room("event", 1, "https://meet1.com", "rtsp://mtx:8554")
            )
            t2 = threading.Thread(
                target=lambda: self.mgr.start_room("event", 1, "https://meet2.com", "rtsp://mtx:8554")
            )
            t1.start()
            self.assertTrue(first_call_event.wait(timeout=1.0))
            t2.start()

            # Confirm t2 is blocked by lock contention and has not entered Popen yet
            time.sleep(0.05)
            self.assertEqual(len(procs_created), 1)

            # Release first call so t1 finishes and t2 proceeds
            release_first_event.set()
            t1.join(timeout=2.0)
            t2.join(timeout=2.0)

            self.assertFalse(t1.is_alive())
            self.assertFalse(t2.is_alive())
            self.assertEqual(len(procs_created), 2)
            procs_created[0].terminate.assert_called_once()

            self.assertIn(key, self.mgr.rooms)
            self.mgr.wait_for_all_stops(timeout=2.0)
            self.assertEqual(len(self.mgr.rooms), 1)
            self.assertEqual(self.mgr.rooms[key], procs_created[1])


class TestRequestHandlerStopEndpoint(unittest.TestCase):
    """Integration tests for the RequestHandler /stop endpoint."""

    @classmethod
    def setUpClass(cls):
        """Start an ephemeral ThreadingHTTPServer on an OS-assigned port."""
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), bot.RequestHandler)
        cls.port = cls.server.server_port
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        """Shut down the test server."""
        cls.server.shutdown()
        cls.server.server_close()

    def test_post_stop_missing_params(self):
        """Verify that POST /stop rejects missing payload fields with HTTP 400."""
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/stop",
            data=json.dumps({"event_slug": "event"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 400)

    def test_post_stop_non_blocking_returns_202(self):
        """Verify POST /stop responds immediately with 202 and unblocks GET /status."""
        import urllib.request

        proc = MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = None
        block_event = threading.Event()

        def slow_wait(*args, **kwargs):
            block_event.wait(timeout=2.0)
            return 0

        proc.wait.side_effect = slow_wait

        key = "testevent-42"
        bot.manager.rooms[key] = proc
        bot.manager.room_states[key] = "in_meeting"
        bot.manager.room_meta[key] = {"event_slug": "testevent", "room_id": 42}

        try:
            start_time = time.monotonic()
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/stop",
                data=json.dumps({"event_slug": "testevent", "room_id": 42}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                self.assertEqual(resp.status, 202)
                body = json.loads(resp.read().decode())
                self.assertEqual(body.get("status"), "stopping")
                self.assertEqual(body.get("event_slug"), "testevent")

            elapsed = time.monotonic() - start_time
            # Must return well under the 2-second block time (typically < 0.1s)
            self.assertLess(elapsed, 0.5)

            # GET /status immediately afterwards should respond without blocking
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/status", timeout=1.0) as resp:
                self.assertEqual(resp.status, 200)
                status_data = json.loads(resp.read().decode())
                self.assertNotIn(key, status_data.get("active_rooms", {}))
        finally:
            block_event.set()
            bot.manager.wait_for_all_stops(timeout=2.0)
