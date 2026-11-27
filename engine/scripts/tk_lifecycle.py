"""Release a Tk interpreter on its creating thread, including Python cycles.

Tk.destroy removes native widgets, but Python widgets and Variables can retain
the interpreter in reference cycles. Later GC in a network worker must never be
the thread that finally destroys those Tcl resources.
"""
import gc
import threading
import tkinter as tk
from tkinter import font as tkfont, ttk


class _ClosedInterpreter:
    """A disposed widget may still answer winfo_exists without retaining Tcl."""
    def call(self, *args):
        if args[:2] == ("winfo", "exists") or args[:2] == ("info", "exists"):
            return 0
        raise tk.TclError("application has been destroyed")

    def getint(self, value):
        return int(value)

    def getboolean(self, value):
        return bool(value)

    def splitlist(self, value):
        raise tk.TclError("application has been destroyed")


_CLOSED = _ClosedInterpreter()


class ManagedTk(tk.Tk):
    """Tk root with deterministic cleanup for normal close and direct destroy."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lifecycle_thread = threading.get_ident()
        self._lifecycle_closed = False
        self._lifecycle_base_keys = frozenset(self.__dict__)

    def _before_tk_destroy(self):
        """Subclasses can stop owned work before their Tcl widgets disappear."""

    def destroy(self):
        if self._lifecycle_closed:
            return
        if threading.get_ident() != self._lifecycle_thread:
            raise RuntimeError("Tk 窗口必须在创建它的界面线程关闭")
        self._lifecycle_closed = True
        interpreter = self.tk
        # Include already-destroyed dialogs/widgets still retained by cycles,
        # not only the current root.children tree. Keep them alive during close.
        resources = [obj for obj in gc.get_objects()
                     if ((isinstance(obj, (tk.Misc, tk.Image, ttk.Style))
                          and getattr(obj, "tk", None) is interpreter)
                         or (isinstance(obj, (tk.Variable, tkfont.Font))
                             and getattr(obj, "_tk", None) is interpreter))]
        try:
            self._before_tk_destroy()
            for identifier in interpreter.splitlist(interpreter.call("after", "info")):
                interpreter.call("after", "cancel", identifier)
            super().destroy()
        finally:
            # Tcl Variables own separate trace commands. Dispose them now, then
            # make a later Python __del__ a no-op, even if an external owner kept
            # a Variable reference until a different thread releases it.
            for resource in resources:
                if isinstance(resource, tk.Variable):
                    try:
                        resource.__del__()
                    except tk.TclError:
                        pass  # A variable/trace already removed with its widget.
                    resource._tk = None
                    resource._root = None
                    resource._tclCommands = None
                elif isinstance(resource, tkfont.Font):
                    resource.delete_font = False
                    resource._tk = _CLOSED
                    # Font caches bound tkapp methods independently of _tk.
                    # Both must stop retaining the native interpreter as well.
                    resource._call = _CLOSED.call
                    resource._split = _CLOSED.splitlist
                else:
                    resource.tk = _CLOSED
                    if isinstance(resource, tk.Misc):
                        resource.master = None
                        resource.children.clear()
            # Drop application-owned widget graphs. Preserve the root's basic
            # Tk bookkeeping and the idempotent-close markers.
            keep = self._lifecycle_base_keys | {"_lifecycle_base_keys"}
            for name in tuple(self.__dict__):
                if name not in keep:
                    del self.__dict__[name]
            resource = None
            resources.clear()
            interpreter = None  # Last Tcl reference is released on this thread.
            gc.collect()
