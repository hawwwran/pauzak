"""The undo/redo stack.

A step is a pair of values plus the function that applies one: undo applies
`before`, redo applies `after`. Nothing is stored as a delta, so a step still
means the same thing after the overlay has been changed by something else in
between, and a step whose two values are equal is not worth keeping.

Steps carry a key (which overlay they act on) and a tag (which property they
change). Two steps with the same key and tag arriving within COALESCE_WINDOW of
each other are merged, which is what turns a slider being dragged across its
whole range, or a dozen wheel clicks, into one thing to undo. Window drags do
not rely on that: they open a step when the drag begins and close it when it
ends, however long that takes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from gi.repository import GLib, GObject

COALESCE_WINDOW = 700_000  # microseconds between steps that may still merge
STACK_LIMIT = 100  # steps kept, oldest dropped first


@dataclass
class Step:
    apply: Callable[[Any], None]
    before: Any
    after: Any
    key: Any = None
    tag: str | None = None  # None never merges with anything
    at: int = 0


class UndoStack(GObject.Object):
    __gtype_name__ = "PauzakUndoStack"

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, limit: int = STACK_LIMIT):
        super().__init__()
        self.limit = limit
        # Everything a step's apply() touches reports its own changes back, so
        # the flag is what stops an undo from being recorded as a fresh change.
        self.applying = False
        self._done: list[Step] = []
        self._undone: list[Step] = []

    def push(self, step: Step) -> None:
        step.at = GLib.get_monotonic_time()
        if self.applying or step.before == step.after:
            return
        self._undone.clear()
        if not self._merged_into_top(step):
            self._done.append(step)
            del self._done[: max(0, len(self._done) - self.limit)]
        self.emit("changed")

    def _merged_into_top(self, step: Step) -> bool:
        top = self._done[-1] if self._done else None
        if (
            top is None
            or step.tag is None
            or (top.key, top.tag) != (step.key, step.tag)
            or step.at - top.at > COALESCE_WINDOW
        ):
            return False
        top.after, top.at = step.after, step.at
        if top.before == top.after:
            self._done.pop()  # dragged back to where it started
        return True

    def can_undo(self) -> bool:
        return bool(self._done)

    def can_redo(self) -> bool:
        return bool(self._undone)

    def undo(self) -> bool:
        return self._step(self._done, self._undone, lambda step: step.before)

    def redo(self) -> bool:
        return self._step(self._undone, self._done, lambda step: step.after)

    def keys(self) -> set:
        return {step.key for step in self._done} | {step.key for step in self._undone}

    def forget(self, keys) -> None:
        """Drop every step acting on one of `keys`.

        For when what a step would have to put back is no longer held: an
        overlay whose pixels have been let go of cannot be brought back, and a
        step that silently does nothing is worse than one that is gone.
        """
        counts = (len(self._done), len(self._undone))
        self._done = [step for step in self._done if step.key not in keys]
        self._undone = [step for step in self._undone if step.key not in keys]
        if counts != (len(self._done), len(self._undone)):
            self.emit("changed")

    def _step(self, source: list[Step], target: list[Step], side) -> bool:
        """Move one step between the two stacks, applying the side it lands on."""
        if not source:
            return False
        step = source.pop()
        self.applying = True
        try:
            step.apply(side(step))
        finally:
            self.applying = False
        target.append(step)
        self.emit("changed")
        return True
