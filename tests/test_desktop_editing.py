"""Exercise actual Tk entry bindings without reading or replacing the OS clipboard."""

import os
import tkinter as tk
from tkinter import ttk

import pytest

from cennik.collector.desktop import bind_entry_editing

pytestmark = pytest.mark.skipif(
    os.name != "nt" and not os.environ.get("DISPLAY"), reason="Desktop display required"
)


@pytest.fixture(scope="module")
def root():
    window = tk.Tk()
    yield window
    window.destroy()


@pytest.fixture
def entry(root):
    widget = bind_entry_editing(ttk.Entry(root))
    widget.pack()
    root.update()
    widget.focus_force()
    root.update()
    widget.clipboard_get = lambda: "https://205.172.56.135:8443"
    try:
        yield root, widget
    finally:
        widget.destroy()


@pytest.mark.skipif(os.name != "nt", reason="Windows physical key codes")
def test_physical_ctrl_v_replaces_selection_once(entry):
    root, widget = entry
    widget.insert(0, "old URL")
    widget.selection_range(0, "end")
    widget.event_generate("<KeyPress>", keycode=86, state=4)
    root.update()
    assert widget.get() == "https://205.172.56.135:8443"
    widget.event_generate("<KeyPress>", keycode=65, state=4)
    root.update()
    assert widget.selection_get() == widget.get()


def test_shift_insert_and_context_menu_paste(entry):
    root, widget = entry
    widget.insert(0, "old URL")
    widget.selection_range(0, "end")
    widget.event_generate("<Shift-Insert>")
    root.update()
    assert widget.get() == "https://205.172.56.135:8443"
    widget.selection_range(0, "end")
    menu = next(child for child in widget.winfo_children() if isinstance(child, tk.Menu))
    menu.invoke(2)
    assert widget.get() == "https://205.172.56.135:8443"
