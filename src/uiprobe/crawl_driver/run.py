"""`_Run`: one crawl, assembled from the mixins that hold its methods."""

from __future__ import annotations

from uiprobe.crawl_driver.gate import _GateMixin
from uiprobe.crawl_driver.loop import _LoopMixin
from uiprobe.crawl_driver.observe import _ObserveMixin
from uiprobe.crawl_driver.send import _SendMixin


class _Run(_GateMixin, _SendMixin, _ObserveMixin, _LoopMixin):
    """The state of one crawl: the engine, the writer and what is still open."""
