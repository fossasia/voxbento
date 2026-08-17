import asyncio
import logging
import os
import signal
from typing import Optional

from portal.config import settings

logger = logging.getLogger(__name__)

class FfmpegProcess:
    """
    Robust async context manager for ffmpeg subprocess lifecycle.
    Guarantees process group termination even during severe cascading cancellations.
    """
    def __init__(self, rtsp_url: str, sample_rate: str, booth_id: str):
        self.rtsp_url = rtsp_url
        self.sample_rate = sample_rate
        self.booth_id = booth_id
        self.process: Optional[asyncio.subprocess.Process] = None
        self.stderr_task: Optional[asyncio.Task] = None

        # Determine configured timeout
        timeout = getattr(settings, "ffmpeg_termination_timeout_secs", 3.0)
        self.termination_timeout = timeout if timeout > 0 else 3.0

    async def __aenter__(self):
        ffmpeg_cmd = [
            "ffmpeg",
            "-rtsp_transport", "tcp",
            "-i", self.rtsp_url,
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", self.sample_rate,
            "-ac", "1",
            "-f", "s16le",
            "-"
        ]

        # start_new_session=True places ffmpeg and all descendants into their own process group.
        # This is strictly required so that SIGTERM/SIGKILL can clean up the entire tree.
        self.process = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True
        )

        self.stderr_task = asyncio.create_task(self._log_stderr())
        logger.info(f"[{self.booth_id}] ffmpeg started (pid={self.process.pid})")
        return self.process

    async def _log_stderr(self):
        try:
            while True:
                line = await self.process.stderr.readline()
                if not line:
                    break
                logger.debug(f"[{self.booth_id}] ffmpeg: {line.decode().strip()}")
        except Exception as e:
            logger.debug(f"[{self.booth_id}] ffmpeg stderr logger stopped: {e}")

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if not self.process:
            return

        # Shield the cleanup operation itself from being interrupted by external cancellation.
        # We wrap the core teardown logic in a shielded task.
        cleanup_task = asyncio.create_task(self._perform_cleanup())
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            # If a cancellation hits us *while* we are waiting for the shielded task,
            # we must still wait for the shielded task to finish before bubbling it up!
            # Otherwise we violate the invariant that __aexit__ does not return prematurely.
            await cleanup_task
            raise

    async def _perform_cleanup(self):
        if self.process.returncode is None:
            logger.info(f"[{self.booth_id}] Attempting termination of ffmpeg process group (pid={self.process.pid})")

            try:
                # Send SIGTERM to the entire process group
                os.killpg(self.process.pid, signal.SIGTERM)

                try:
                    await asyncio.wait_for(self.process.wait(), timeout=self.termination_timeout)
                    logger.info(f"[{self.booth_id}] ffmpeg process group terminated cleanly.")
                except TimeoutError:
                    logger.warning(f"[{self.booth_id}] ffmpeg did not exit within {self.termination_timeout}s. Escalating to SIGKILL.")
                    os.killpg(self.process.pid, signal.SIGKILL)
                    await self.process.wait()
                    logger.info(f"[{self.booth_id}] ffmpeg process group killed.")
            except ProcessLookupError:
                # The process group already exited.
                logger.debug(f"[{self.booth_id}] Process group {self.process.pid} already exited.")
            except OSError as e:
                # Unexpected signaling error. Do not fail the cleanup sequence.
                logger.error(f"[{self.booth_id}] Unexpected OSError during process group signaling: {e}")
            except Exception as e:
                # Catch-all to ensure idempotency and registry unblocking
                logger.error(f"[{self.booth_id}] Unexpected error during ffmpeg cleanup: {e}", exc_info=True)

        if self.stderr_task and not self.stderr_task.done():
            self.stderr_task.cancel()
            try:
                await self.stderr_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"[{self.booth_id}] Unexpected error awaiting stderr_task: {e}")

        logger.info(f"[{self.booth_id}] ffmpeg cleanup fully completed.")
