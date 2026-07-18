from __future__ import annotations

from dataclasses import dataclass

import mss


@dataclass
class Region:
    x: int
    y: int
    width: int
    height: int


def _absolute_tk_geometry(*, left: int, top: int, width: int, height: int) -> str:
    """Return Tk geometry using absolute virtual-desktop coordinates.

    Tk treats a conventional ``-1920`` offset as relative to the right edge.
    Prefixing every coordinate with ``+`` produces ``+-1920`` for negative
    absolute coordinates, which correctly addresses monitors left of or above
    the primary display.
    """
    return f"{width}x{height}+{left}+{top}"


def select_region() -> Region | None:
    import tkinter as tk

    with mss.mss() as capture:
        virtual_screen = capture.monitors[0]
    left = int(virtual_screen["left"])
    top = int(virtual_screen["top"])
    screen_width = int(virtual_screen["width"])
    screen_height = int(virtual_screen["height"])

    root = tk.Tk()
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.3)
    root.overrideredirect(True)
    root.geometry(
        _absolute_tk_geometry(
            left=left,
            top=top,
            width=screen_width,
            height=screen_height,
        )
    )
    root.configure(bg="black")
    root.title("Select Combat Region")
    root.lift()
    root.focus_force()
    root.update_idletasks()

    canvas = tk.Canvas(root, cursor="cross", bg="gray")
    canvas.pack(fill=tk.BOTH, expand=True)

    state = {"start": None, "rect": None, "result": None}

    def on_mouse_down(event):
        state["start"] = (event.x, event.y)
        if state["rect"] is not None:
            canvas.delete(state["rect"])
        state["rect"] = canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="red", width=2)

    def on_mouse_drag(event):
        if not state["start"] or state["rect"] is None:
            return
        x0, y0 = state["start"]
        canvas.coords(state["rect"], x0, y0, event.x, event.y)

    def on_mouse_up(event):
        if not state["start"]:
            return
        x0, y0 = state["start"]
        x1, y1 = event.x, event.y
        x, y = min(x0, x1), min(y0, y1)
        width, height = abs(x1 - x0), abs(y1 - y0)
        if width > 10 and height > 10:
            state["result"] = Region(x=x + left, y=y + top, width=width, height=height)
        root.quit()

    def on_escape(_):
        root.quit()

    canvas.bind("<ButtonPress-1>", on_mouse_down)
    canvas.bind("<B1-Motion>", on_mouse_drag)
    canvas.bind("<ButtonRelease-1>", on_mouse_up)
    root.bind("<Escape>", on_escape)

    root.mainloop()
    root.destroy()
    return state["result"]
