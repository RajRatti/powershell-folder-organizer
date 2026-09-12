"""
Folder Organizer — a full-screen TUI file browser that runs *inside* your
current PowerShell window (via the terminal alternate screen buffer, the
same trick vim/less use — nothing new opens, nothing looks separate).

Because a child process can never change the working directory of the
shell that launched it, this app writes its final result to a small state
file on exit. The PowerShell wrapper function (see Install-Organizer.ps1)
reads that file and performs the real `Set-Location` / venv activation in
your live session.

State file format (plain text, one KEY=VALUE per line):
    PATH=<absolute path to cd into>
    ACTIVATE=<absolute path to an activate script, or empty>
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, ListItem, ListView, Static

# Where we hand off the final result to the PowerShell wrapper function.
# Using the OS temp dir (not the install dir) keeps this out of source
# control and automatically per-machine.
STATE_FILE = Path(tempfile.gettempdir()) / "organizer_action.txt"

# Folder names we treat as "this directory has a venv in it". Checked in
# the *current* directory only, not recursively.
VENV_DIR_NAMES = ("venv", ".venv", "env", ".env")


def find_venv_activate(folder: Path) -> Path | None:
    """Return the PowerShell activate script if `folder` contains a venv.

    Only looks one level deep (folder/<venv_name>/Scripts/Activate.ps1) -
    this is deliberately not recursive so it stays fast on large trees.
    """
    for name in VENV_DIR_NAMES:
        candidate = folder / name / "Scripts" / "Activate.ps1"
        if candidate.is_file():
            return candidate
    return None


def write_state(path: Path, activate: Path | None) -> None:
    """Persist the final folder (and optional venv script) for the
    PowerShell wrapper to pick up after this process exits.
    """
    lines = [f"PATH={path}"]
    lines.append(f"ACTIVATE={activate}" if activate else "ACTIVATE=")
    STATE_FILE.write_text("\n".join(lines), encoding="utf-8")


class FolderItem(ListItem):
    """A single folder row in the list. Clicking it enters the folder."""

    def __init__(self, path: Path) -> None:
        self.folder_path = path
        has_venv = find_venv_activate(path) is not None
        label = f"📁 {path.name}" + ("  [venv]" if has_venv else "")
        super().__init__(Label(label))

    def on_click(self) -> None:
        # Textual's ListView normally requires Enter to "activate" a
        # highlighted row; clicking only highlights it. Posting our own
        # Enter message here is what makes a single click immediately
        # navigate into the folder, per the original requirement.
        self.post_message(self.Enter(self.folder_path))

    class Enter(Message):
        """Custom message: "the user wants to open this folder"."""

        def __init__(self, path: Path) -> None:
            super().__init__()
            self.path = path


class ConfirmScreen(ModalScreen[bool]):
    """Yes/No confirmation dialog, e.g. for deletes.

    Returned value (via push_screen_wait) is True if the user confirmed.
    """

    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    #dialog {
        width: 60; height: auto; padding: 1 2;
        background: $panel; border: thick $error;
    }
    #buttons { align: right middle; height: auto; padding-top: 1; }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.message)
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Delete", id="confirm", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class TextPromptScreen(ModalScreen[str | None]):
    """Single text-input dialog, used for add/rename.

    Returned value (via push_screen_wait) is the entered text, or None
    if the user cancelled or submitted blank input.
    """

    DEFAULT_CSS = """
    TextPromptScreen { align: center middle; }
    #dialog {
        width: 60; height: auto; padding: 1 2;
        background: $panel; border: thick $primary;
    }
    #buttons { align: right middle; height: auto; padding-top: 1; }
    """

    def __init__(self, title: str, initial: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_text)
            yield Input(value=self.initial, id="text-input")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("OK", id="ok", variant="primary")

    def on_mount(self) -> None:
        # Auto-focus the text box so the user can start typing immediately
        # without having to click into it first.
        self.query_one("#text-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Fired when the user presses Enter inside the Input widget -
        # treat that the same as clicking "OK".
        self.dismiss(event.value.strip() or None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            value = self.query_one("#text-input", Input).value.strip()
            self.dismiss(value or None)
        else:
            self.dismiss(None)


class OrganizerApp(App):
    """The main folder-organizer screen."""

    # Textual auto-loads this stylesheet next to the script; edit
    # theme.tcss to restyle the app without touching any Python.
    CSS_PATH = "theme.tcss"
    TITLE = "Folder Organizer"

    # Keyboard shortcuts, shown automatically in the Footer widget.
    BINDINGS = [
        Binding("a", "add_folder", "Add"),
        Binding("r", "rename_folder", "Rename"),
        Binding("d", "delete_folder", "Delete"),
        Binding("backspace", "go_up", "Up a level"),
        Binding("v", "activate_venv", "Activate venv"),
        Binding("escape,q", "back_to_shell", "Back to shell"),
    ]

    # reactive() means assigning to self.current_path elsewhere in the
    # app is enough to trigger Textual's internal bookkeeping; we still
    # call refresh_listing() ourselves after changing it since the
    # listing itself isn't auto-derived from this value.
    current_path: reactive[Path] = reactive(Path.cwd)

    def __init__(self, start_path: Path) -> None:
        super().__init__()
        self.current_path = start_path.resolve()
        # Tracks whichever folder is currently highlighted in the list,
        # so Rename/Delete know what to act on.
        self.selected_path: Path | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(str(self.current_path), id="breadcrumb")
        yield ListView(id="folder-list")
        with Horizontal(id="toolbar"):
            yield Button("Add [a]", id="add")
            yield Button("Rename [r]", id="rename")
            yield Button("Delete [d]", id="delete")
            yield Button("Up [⌫]", id="up")
            yield Button("Activate venv [v]", id="activate", variant="success")
            yield Button("Back to Shell [esc]", id="back", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_listing()

    # -- listing -----------------------------------------------------
    def refresh_listing(self) -> None:
        """Re-scan self.current_path and repaint the breadcrumb + list.

        Called after every navigation and every add/rename/delete, since
        none of those are automatically reflected by Textual on their own.
        """
        self.query_one("#breadcrumb", Static).update(str(self.current_path))
        list_view = self.query_one("#folder-list", ListView)
        list_view.clear()
        try:
            folders = sorted(
                (p for p in self.current_path.iterdir() if p.is_dir()),
                key=lambda p: p.name.lower(),
            )
        except PermissionError:
            # A folder we don't have access to (e.g. C:\System Volume
            # Information) - show it as empty rather than crashing.
            folders = []
        for folder in folders:
            list_view.append(FolderItem(folder))
        self.selected_path = None
        self._update_venv_button()

    def _update_venv_button(self) -> None:
        # Only enable "Activate venv" when the current directory (not a
        # subfolder) actually has one - otherwise it's a dead button.
        activate_btn = self.query_one("#activate", Button)
        activate_btn.disabled = find_venv_activate(self.current_path) is None

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        # Fires continuously as the cursor moves (arrow keys, hover) -
        # just keeps selected_path in sync for Rename/Delete.
        if isinstance(event.item, FolderItem):
            self.selected_path = event.item.folder_path

    def on_folder_item_enter(self, event: FolderItem.Enter) -> None:
        # Our own message, posted by FolderItem.on_click - handles the
        # "single click enters the folder" behavior.
        self._enter_folder(event.path)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # Textual's built-in "activate" event, fired when Enter is
        # pressed while a row is highlighted. Distinct from Highlighted
        # above, which fires on every cursor move, not just activation.
        if isinstance(event.item, FolderItem):
            self._enter_folder(event.item.folder_path)

    def _enter_folder(self, path: Path) -> None:
        """Shared by click and Enter-key navigation."""
        self.current_path = path
        self.refresh_listing()

    # -- actions -------------------------------------------------------
    def action_go_up(self) -> None:
        parent = self.current_path.parent
        # At the filesystem root, parent == current_path itself - guard
        # against refreshing pointlessly (or looping) in that case.
        if parent != self.current_path:
            self.current_path = parent
            self.refresh_listing()

    @work
    async def action_add_folder(self) -> None:
        # @work lets us `await` the modal dialog's result without
        # blocking Textual's event loop - actions can't be plain async
        # defs and still be triggered by BINDINGS without this decorator.
        name = await self.push_screen_wait(TextPromptScreen("New folder name:"))
        if name:
            try:
                (self.current_path / name).mkdir(parents=False, exist_ok=False)
                self.notify(f"Created '{name}'")
            except FileExistsError:
                self.notify(f"'{name}' already exists", severity="error")
            except OSError as e:
                self.notify(f"Could not create folder: {e}", severity="error")
            self.refresh_listing()

    @work
    async def action_rename_folder(self) -> None:
        if not self.selected_path:
            self.notify("Select a folder first", severity="warning")
            return
        old_path = self.selected_path
        new_name = await self.push_screen_wait(
            TextPromptScreen("Rename to:", initial=old_path.name)
        )
        if new_name and new_name != old_path.name:
            try:
                old_path.rename(old_path.parent / new_name)
                self.notify(f"Renamed to '{new_name}'")
            except OSError as e:
                self.notify(f"Rename failed: {e}", severity="error")
            self.refresh_listing()

    @work
    async def action_delete_folder(self) -> None:
        if not self.selected_path:
            self.notify("Select a folder first", severity="warning")
            return
        target = self.selected_path
        confirmed = await self.push_screen_wait(
            ConfirmScreen(f"Delete '{target.name}' and everything inside it?")
        )
        if confirmed:
            try:
                shutil.rmtree(target)
                self.notify(f"Deleted '{target.name}'")
            except OSError as e:
                self.notify(f"Delete failed: {e}", severity="error")
            self.refresh_listing()

    def action_activate_venv(self) -> None:
        # Deliberately exits the app immediately rather than staying
        # open - there's no reason to keep browsing once you've decided
        # to activate a venv and return to the shell.
        activate = find_venv_activate(self.current_path)
        if activate:
            write_state(self.current_path, activate)
            self.exit()
        else:
            self.notify("No venv found here", severity="warning")

    def action_back_to_shell(self) -> None:
        write_state(self.current_path, None)
        self.exit()

    # -- toolbar button wiring -----------------------------------------
    def on_button_pressed(self, event: Button.Pressed) -> None:
        # Routes every toolbar Button through to the same action_* methods
        # the keyboard shortcuts use, so behavior never diverges between
        # clicking a button and pressing its key.
        mapping = {
            "add": self.action_add_folder,
            "rename": self.action_rename_folder,
            "delete": self.action_delete_folder,
            "up": self.action_go_up,
            "activate": self.action_activate_venv,
            "back": self.action_back_to_shell,
        }
        action = mapping.get(event.button.id)
        if action:
            action()


def main() -> None:
    # argv[1] is the directory PowerShell was in when it launched us
    # (passed by the `organizer` function in Install-Organizer.ps1) so
    # the app opens exactly where your shell prompt currently is.
    start_arg = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    start_path = Path(start_arg)
    if not start_path.is_dir():
        start_path = Path.cwd()
    app = OrganizerApp(start_path)
    app.run()


if __name__ == "__main__":
    main()