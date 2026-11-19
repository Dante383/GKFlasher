"""Byte progress and completion messages shared by GUI and CLI transfers."""

import time

from alive_progress import alive_bar


def transfer_summary(action: str, size: int, started_at: float, *, elapsed: float | None = None) -> str:
	"""Allow writers to exclude separate verification passes from their timing."""
	elapsed = max(time.monotonic() - started_at if elapsed is None else elapsed, 0.001)
	return "[*] {} {} bytes in {:.1f} seconds at {:.0f} B/s".format(action, size, elapsed, size / elapsed)


class Progress:
	def __init__ (self, progress_callback, total: int, label: str = '', *, incremental: bool = False) -> None:
		self.progress_callback = progress_callback
		self.total = max(total, 1)
		self.label = label
		self.started_at = time.monotonic()
		self.incremental = incremental
		self.done = 0
		self._bar_context = None
		self._bar = None
		if progress_callback:
			progress_callback.emit((self.total, 0))
			progress_callback.emit((0,))
		else:
			self._bar_context = alive_bar(self.total, title=label, unit='B', manual=True)
			self._bar = self._bar_context.__enter__()

	def __call__ (self, done: int) -> None:
		self.done = min(self.total, max(0, self.done + done if self.incremental else done))
		done = self.done
		if self.progress_callback:
			rate = done / max(time.monotonic() - self.started_at, 0.001)
			self.progress_callback.emit((done, rate, 'bytes'))
			return
		if self._bar is not None:
			self._bar(done / self.total)

	def title (self, title: str) -> None:
		if self._bar is not None:
			self._bar.title(title)

	def finish (self) -> None:
		if self._bar_context is not None:
			self._bar_context.__exit__(None, None, None)
			self._bar_context = None
			self._bar = None
