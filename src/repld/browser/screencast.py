"""`Page.startScreencast` as a per-tab stream handed to a handler.

Frames never reach the event store: one JPEG per repaint would evict the
network and console history the store exists for.

**Each frame is acked only after its handler returns, and handler calls never
overlap.** Chrome stops sending once a few frames are un-acked, so a slow
consumer lowers the frame rate instead of growing a queue; at most that
window of frames waits here, in order.
"""

import asyncio
import base64
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self

from .. import bg

if TYPE_CHECKING:
    from .cdp import CDPSession

logger = logging.getLogger(__name__)

__all__ = ["Frame", "Screencast", "begin", "discard", "on_frame", "restore"]

_FORMATS = ("jpeg", "png")


@dataclass(frozen=True, slots=True)
class Frame:
    data: bytes  # the encoded image, already base64-decoded
    meta: dict[
        str, Any
    ]  # Page.ScreencastFrameMetadata: deviceWidth/Height, offsetTop, scrollOffsetX/Y, pageScaleFactor, timestamp


Handler = Callable[[Frame], Awaitable[None] | None]


class Screencast:
    """The stream `Tab.screencast` returns; at most one per tab."""

    def __init__(
        self, cdp: "CDPSession", handler: Handler, params: dict[str, Any]
    ) -> None:
        self._cdp = cdp
        self._handler = handler
        self.params = params
        self._turn = asyncio.Lock()
        self.frames = 0  # handed to the handler without it raising
        self.errors = 0  # handler raised
        # Why a reattach could not resume the stream; `active` is False then.
        self.error: Exception | None = None

    @property
    def active(self) -> bool:
        return self._cdp._screencast is self

    async def stop(self) -> None:
        """Stop the stream. Idempotent; a tab that is already gone is fine."""
        if not self.active:
            return
        self._cdp._screencast = None
        try:
            await self._cdp.execute("Page.stopScreencast")
        except Exception as exc:
            logger.debug("Page.stopScreencast: %s", exc)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()


def begin(
    cdp: "CDPSession",
    handler: Handler,
    *,
    format: str,
    quality: int,
    max_width: int | None,
    max_height: int | None,
    every_nth_frame: int,
) -> Screencast:
    """Validate and register the stream; the caller sends `Page.startScreencast`."""
    if not callable(handler):
        raise TypeError("screencast handler must be callable")
    if format not in _FORMATS:
        raise ValueError(f"format must be one of {_FORMATS}, got {format!r}")
    if not 0 <= quality <= 100:
        raise ValueError(f"quality must be 0-100, got {quality}")
    if every_nth_frame < 1:
        raise ValueError(f"every_nth_frame must be >= 1, got {every_nth_frame}")
    if cdp._screencast is not None:
        raise RuntimeError("this tab already has a screencast; stop() it first")
    params: dict[str, Any] = {"format": format, "everyNthFrame": every_nth_frame}
    if format == "jpeg":
        params["quality"] = quality
    if max_width is not None:
        params["maxWidth"] = max_width
    if max_height is not None:
        params["maxHeight"] = max_height
    sc = Screencast(cdp, handler, params)
    cdp._screencast = sc
    return sc


def discard(sc: Screencast) -> None:
    """Forget a stream whose `Page.startScreencast` failed."""
    if sc.active:
        sc._cdp._screencast = None


def on_frame(cdp: "CDPSession", params: dict[str, Any]) -> None:
    """Hand one `Page.screencastFrame` to the handler. Called from the recv loop."""
    sc = cdp._screencast
    if sc is None:
        return  # a frame already in flight when stop() landed
    try:
        frame = Frame(base64.b64decode(params["data"]), params.get("metadata", {}))
        ack_id = params["sessionId"]
    except Exception as exc:
        logger.debug("malformed screencast frame: %s", exc)
        return
    bg.spawn(_deliver(sc, frame, ack_id), name="repld-screencast-frame")


async def _deliver(sc: Screencast, frame: Frame, ack_id: int) -> None:
    async with sc._turn:
        if not sc.active:
            return  # queued behind a handler when stop() ran
        await _call(sc, frame, ack_id)


async def _call(sc: Screencast, frame: Frame, ack_id: int) -> None:
    try:
        result = sc._handler(frame)
        if inspect.isawaitable(result):
            await result
        sc.frames += 1
    except Exception as exc:
        sc.errors += 1
        if sc.errors == 1:
            logger.warning("screencast handler raised (stream continues): %r", exc)
    finally:
        # The ack is what keeps frames coming, so a raising handler must not
        # skip it. After stop() there is nothing left to ack.
        if sc.active:
            try:
                await sc._cdp.send_nowait(
                    "Page.screencastFrameAck", {"sessionId": ack_id}
                )
            except Exception as exc:
                logger.debug("Page.screencastFrameAck: %s", exc)


async def restore(cdp: "CDPSession") -> None:
    """Re-issue the stream under a new CDP session; Chrome drops it on detach."""
    sc = cdp._screencast
    if sc is None:
        return
    try:
        await cdp.execute("Page.startScreencast", sc.params)
    except Exception as exc:
        sc.error = exc
        cdp._screencast = None
        logger.warning("screencast not resumed after reattach: %s", exc)
