#!/usr/bin/env python3
"""
Virtual Machine Manager - one-click disposable VMs on top of libvirt.

Works on Plasma (X11 or Wayland), COSMIC, and other X11 desktops (via wmctrl).
Everything machine-specific is configured through the in-app Settings dialog
(gear button, or shown automatically the first time you run it) - no editing
this file required:
  * Where your VM disk images live (default folder when adding a VM, and
    where disposable "live" overlays are kept).
  * How many machines you have, what to call them, and which .qcow2 base
    disk each one boots from.
  * Which libvirt connection to use (defaults to qemu:///system).

How it works
  * Each VM's chosen base disk (e.g. kali_disk.qcow2) is treated as a
    read-only "golden" image.
  * The VM actually boots from a thin overlay (<id>_live.qcow2, kept in your
    configured image folder) that stores only the changes. "Reset" = delete
    the overlay and recreate it.
  * "Start" opens a new virtual desktop, switches to it, and launches
    virt-viewer fullscreen. Closing the viewer switches back and removes
    that desktop.

Requirements:  sudo apt install python3-pyqt6 virt-viewer libvirt-clients qemu-utils
Optional (X11 non-KDE fallback for workspaces): sudo apt install wmctrl
Run as your normal user (not sudo) - your user must be in the libvirt group.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from PyQt6.QtCore import QProcess, Qt, QTimer
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QFileDialog,
    QFrame, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

WINDOW_TITLE = "Virtual Machine Manager"
CONFIG_FILE = Path.home() / ".config" / "virtual-machine-manager" / "config.json"
DEFAULT_LIBVIRT_URI = "qemu:///system"

LIBVIRT_URI = DEFAULT_LIBVIRT_URI   # set from config at startup; used by virsh()


class Cancelled(Exception):
    pass


# ----------------------------------------------------------------------------
# Config - everything machine-specific lives here, not in this file
# ----------------------------------------------------------------------------
def default_config():
    return {
        "image_dir": str(Path.home() / "virt-machines" / "images"),
        "libvirt_uri": DEFAULT_LIBVIRT_URI,
        "vms": [],   # each: {"id", "label", "base", "domain"(optional, set after first link)}
    }


def load_config():
    cfg = default_config()
    try:
        loaded = json.loads(CONFIG_FILE.read_text())
        cfg.update({k: v for k, v in loaded.items() if k in cfg})
        for vm in cfg.get("vms", []):
            vm.setdefault("id", new_vm_id(cfg["vms"]))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def new_vm_id(existing_vms):
    used = {vm.get("id") for vm in existing_vms}
    while True:
        vid = uuid.uuid4().hex[:8]
        if vid not in used:
            return vid


def overlay_path(image_dir, vm):
    return Path(image_dir) / f"{vm['id']}_live.qcow2"


# ----------------------------------------------------------------------------
# Small helpers around virsh / qemu-img
# ----------------------------------------------------------------------------
def run(cmd, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"{cmd[0]} failed")
    return r.stdout


def virsh(*args, check=True):
    return run(["virsh", "-c", LIBVIRT_URI, *args], check)


def all_domains():
    return [d.strip() for d in virsh("list", "--all", "--name").splitlines() if d.strip()]


def domain_xml(name):
    return ET.fromstring(virsh("dumpxml", "--inactive", name))


def file_disks(root):
    return [
        d for d in root.findall("./devices/disk")
        if d.get("device") == "disk" and d.get("type") == "file"
    ]


def find_domain(paths):
    """Find the libvirt domain whose disk is one of the given files."""
    wanted = {str(p) for p in paths}
    for name in all_domains():
        for disk in file_disks(domain_xml(name)):
            src = disk.find("source")
            if src is not None and src.get("file") in wanted:
                return name
    return None


def make_overlay(base, overlay):
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.unlink(missing_ok=True)
    run([
        "qemu-img", "create", "-f", "qcow2", "-F", "qcow2",
        "-b", str(base.resolve()), str(overlay),
    ])


def point_disk_at_overlay(domain, base, overlay, running):
    """Make the domain boot from the overlay instead of the base image."""
    root = domain_xml(domain)
    disks = file_disks(root)
    target = None
    for disk in disks:
        src = disk.find("source")
        if src is not None and src.get("file") in (str(base), str(overlay)):
            target = disk
    if target is None and disks:
        target = disks[0]          # user explicitly linked this domain; assume its first disk
    if target is None:
        raise RuntimeError(f"Couldn't find a file-backed disk in '{domain}'.")

    src = target.find("source")
    if src is None:
        raise RuntimeError(f"The first disk of '{domain}' has no source file.")
    if src.get("file") == str(overlay):
        return                      # already correct
    if running:
        raise RuntimeError(
            "This VM is running from the original image. Shut it down first, then try again."
        )

    src.set("file", str(overlay))
    drv = target.find("driver")
    if drv is not None:
        drv.set("type", "qcow2")
    for bs in target.findall("backingStore"):
        target.remove(bs)

    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as f:
        f.write(ET.tostring(root, encoding="unicode"))
        tmp = f.name
    try:
        virsh("define", tmp)
    finally:
        Path(tmp).unlink(missing_ok=True)


# ----------------------------------------------------------------------------
# Workspace (virtual desktop) backends - unchanged. KDE's D-Bus interface
# works identically on Plasma X11 and Wayland, so no session-type check is
# needed for KDE specifically; only the plain-X11 fallback cares about it.
# ----------------------------------------------------------------------------
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


class KDEWorkspaces:
    """KDE Plasma (X11 or Wayland) through KWin's D-Bus interface."""
    IFACE = "org.kde.KWin.VirtualDesktopManager"
    BASE = [
        "gdbus", "call", "--session", "--dest", "org.kde.KWin",
        "--object-path", "/VirtualDesktopManager", "--method",
    ]

    def _get(self, prop):
        return run(self.BASE + ["org.freedesktop.DBus.Properties.Get", self.IFACE, prop])

    def desktops(self):
        return UUID_RE.findall(self._get("desktops"))

    def current(self):
        m = UUID_RE.search(self._get("current"))
        if not m:
            raise RuntimeError("couldn't read current desktop")
        return m.group(0)

    def create(self, name):
        before = self.desktops()
        run(self.BASE + [f"{self.IFACE}.createDesktop", f"uint32 {len(before)}", f"'{name}'"])
        for _ in range(30):
            new = [d for d in self.desktops() if d not in before]
            if new:
                return new[0]
            time.sleep(0.05)
        raise RuntimeError("desktop was not created")

    def switch(self, desktop_id):
        try:
            run(self.BASE + ["org.freedesktop.DBus.Properties.Set", self.IFACE, "current", f"<'{desktop_id}'>"])
        except RuntimeError:
            pass
        time.sleep(0.1)
        if self.current() != desktop_id:      # property write didn't take: use the numeric API
            idx = self.desktops().index(desktop_id) + 1
            run([
                "gdbus", "call", "--session", "--dest", "org.kde.KWin",
                "--object-path", "/KWin", "--method", "org.kde.KWin.setCurrentDesktop", str(idx),
            ])

    def remove(self, desktop_id):
        run(self.BASE + [f"{self.IFACE}.removeDesktop", f"'{desktop_id}'"])


class WmctrlWorkspaces:
    """X11 desktops (GNOME/Pop, XFCE, ...) via wmctrl. Reuses the spare empty
    workspace at the end when there is one (GNOME's dynamic workspaces)."""

    def __init__(self):
        self.created = set()

    def _rows(self):
        return run(["wmctrl", "-d"]).splitlines()

    def current(self):
        for row in self._rows():
            parts = row.split()
            if parts[1] == "*":
                return int(parts[0])
        return 0

    def _used(self):
        used = set()
        for row in run(["wmctrl", "-l"]).splitlines():
            parts = row.split()
            if len(parts) > 1 and parts[1].lstrip("-").isdigit():
                used.add(int(parts[1]))
        return used

    def create(self, name):
        n = len(self._rows())
        last = n - 1
        if last != self.current() and last not in self._used():
            return last                       # a spare empty workspace already exists
        run(["wmctrl", "-n", str(n + 1)])
        self.created.add(n)
        return n

    def switch(self, idx):
        run(["wmctrl", "-s", str(idx)])

    def remove(self, idx):
        if idx in self.created:
            self.created.discard(idx)
            run(["wmctrl", "-n", str(idx)])   # drops the last desktop


class CosmicWorkspaces:
    """COSMIC (Wayland) through the third-party `cos-cli` tool.

    COSMIC keeps a spare empty workspace at the end, so instead of creating one we
    hop onto an empty workspace, and COSMIC discards it again once it's empty."""

    def __init__(self, exe):
        self.exe = exe

    def _info(self):
        return json.loads(run([self.exe, "info", "--json"]))

    @staticmethod
    def _where(info):
        """(group, workspace) of this launcher's window, else of the focused window."""
        apps = info.get("apps", [])
        ours = [a for a in apps if a.get("title") == WINDOW_TITLE]
        focused = [a for a in apps if "activated" in [str(s).lower() for s in a.get("state", [])]]
        for app in ours + focused:
            for w in app.get("workspaces", []):
                return (w["group_index"], w["index"])
        raise RuntimeError("couldn't tell which workspace the launcher is on")

    def current(self):
        return self._where(self._info())

    def create(self, name):
        info = self._info()
        group, here = self._where(info)
        used = {
            w["index"] for a in info.get("apps", [])
            for w in a.get("workspaces", []) if w["group_index"] == group
        }
        spaces = [
            w["index"] for g in info.get("workspace_groups", []) if g["index"] == group
            for w in g["workspaces"]
        ]
        empty = [i for i in spaces if i not in used and i != here]
        if not empty:
            raise RuntimeError(
                "no empty workspace available (turn on dynamic workspaces in COSMIC Settings)")
        return (group, max(empty))

    def switch(self, target):
        group, idx = target
        run([self.exe, "ws-activate", "--workspace", str(idx), "--workspace-group", str(group)])

    def place_window(self, domain, target):
        """Move the virt-viewer window for `domain` onto `target` and show it. True once done."""
        group, idx = target
        apps = self._info().get("apps", [])

        def is_viewer(a):
            return ("virt-viewer" in a.get("app_id", "").lower()
                    or "virt viewer" in a.get("title", "").lower())

        mine = [a for a in apps if is_viewer(a) and domain.lower() in a.get("title", "").lower()]
        found = mine or [a for a in apps if is_viewer(a)]
        if not found:
            return False
        app = found[-1]
        already = any(w["group_index"] == group and w["index"] == idx
                      for w in app.get("workspaces", []))
        if not already:
            run([self.exe, "move", "--index", str(app["index"]),
                 "--workspace", str(idx), "--workspace-group", str(group)])
        self.switch(target)
        return True

    def remove(self, target):
        pass    # COSMIC discards empty dynamic workspaces on its own


def find_cos_cli():
    for cand in (shutil.which("cos-cli"),
                 Path.home() / ".cargo" / "bin" / "cos-cli",
                 Path.home() / ".local" / "bin" / "cos-cli"):
        if cand and Path(cand).exists():
            return str(cand)
    return None


def pick_workspaces():
    """Returns (backend or None, note explaining what was picked or why not)."""
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    session = os.environ.get("XDG_SESSION_TYPE", "unknown")
    if "COSMIC" in desktop.upper():
        exe = find_cos_cli()
        if exe:
            return CosmicWorkspaces(exe), "Workspaces: COSMIC (cos-cli)"
        return None, "Workspaces off: cos-cli not found (cargo install --git https://github.com/estin/cos-cli)"
    if "KDE" in desktop.upper():
        if shutil.which("gdbus"):
            return KDEWorkspaces(), f"Workspaces: KDE Plasma ({session})"
        return None, "Workspaces off: gdbus missing (sudo apt install libglib2.0-bin)"
    if session == "x11":
        if shutil.which("wmctrl"):
            return WmctrlWorkspaces(), "Workspaces: wmctrl (X11)"
        return None, "Workspaces off: run  sudo apt install wmctrl"
    return None, f"Workspaces off: no API for '{desktop or 'this desktop'}' on {session}"


# ----------------------------------------------------------------------------
# Styling
# ----------------------------------------------------------------------------
STYLE = """
QWidget { background:#15171c; color:#e6e6e6; font-size:14px; }
QLabel { background:transparent; }
QDialog { background:#15171c; }
QFrame#card { background:#1e2129; border:1px solid #2c3140; border-radius:12px; }
QLabel#title { font-size:20px; font-weight:600; }
QLabel#status { color:#8b93a7; }
QLineEdit { background:#1e2129; border:1px solid #2c3140; border-radius:6px; padding:6px; color:#e6e6e6; }
QTableWidget { background:#1e2129; border:1px solid #2c3140; border-radius:6px; gridline-color:#2c3140; }
QHeaderView::section { background:#1e2129; color:#8b93a7; border:none; padding:6px; }
QPushButton { background:#2c3140; border:none; border-radius:8px; padding:10px 14px; }
QPushButton:hover { background:#384058; }
QPushButton:disabled { color:#5c6478; background:#22262f; }
QPushButton#primary { background:#a31a1a; color:white; font-weight:600; padding:14px; }
QPushButton#primary:hover { background:#c62828; }
QPushButton#primary:disabled { background:#22262f; color:#5c6478; }
QPushButton#danger { color:#ff9a9a; }
QPushButton#gear { background:transparent; font-size:16px; padding:6px 10px; }
QPushButton#gear:hover { background:#2c3140; }
"""


# ----------------------------------------------------------------------------
# Settings dialog - the only place machine-specific setup happens
# ----------------------------------------------------------------------------
class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{WINDOW_TITLE} - Settings")
        self.resize(640, 420)
        self.vms = [dict(vm) for vm in cfg["vms"]]     # work on a copy until Save

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(
            "VM disk folder (new base disks default here; disposable overlays are always kept here):"))
        dir_row = QHBoxLayout()
        self.dir_edit = QLineEdit(cfg["image_dir"])
        dir_browse = QPushButton("Browse…")
        dir_browse.clicked.connect(self.browse_dir)
        dir_row.addWidget(self.dir_edit)
        dir_row.addWidget(dir_browse)
        layout.addLayout(dir_row)

        layout.addWidget(QLabel("libvirt connection (leave as-is unless you know you need session mode):"))
        self.uri_edit = QLineEdit(cfg.get("libvirt_uri", DEFAULT_LIBVIRT_URI))
        layout.addWidget(self.uri_edit)

        layout.addWidget(QLabel("Virtual machines:"))
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Display name", "Base disk (.qcow2)"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        for vm in self.vms:
            self.add_row(vm)
        layout.addWidget(self.table)

        vm_buttons = QHBoxLayout()
        add_btn = QPushButton("Add VM…")
        add_btn.clicked.connect(self.add_vm)
        remove_btn = QPushButton("Remove selected")
        remove_btn.setObjectName("danger")
        remove_btn.clicked.connect(self.remove_selected)
        vm_buttons.addWidget(add_btn)
        vm_buttons.addWidget(remove_btn)
        vm_buttons.addStretch()
        layout.addLayout(vm_buttons)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "VM disk folder", self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)

    def add_row(self, vm):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(vm["label"]))
        path_btn = QPushButton(vm.get("base") or "(choose a disk…)")
        path_btn.setToolTip(vm.get("base", ""))
        path_btn.clicked.connect(lambda _=False, r=row: self.browse_base(r))
        self.table.setCellWidget(row, 1, path_btn)
        self.table.setRowHeight(row, 34)

    def browse_base(self, row):
        start = self.vms[row].get("base") or self.dir_edit.text()
        path, _ = QFileDialog.getOpenFileName(
            self, "Base disk image", start, "Disk images (*.qcow2 *.img *.raw);;All files (*)")
        if path:
            self.vms[row]["base"] = path
            btn = self.table.cellWidget(row, 1)
            btn.setText(path)
            btn.setToolTip(path)

    def add_vm(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Base disk image", self.dir_edit.text(), "Disk images (*.qcow2 *.img *.raw);;All files (*)")
        if not path:
            return
        default_label = Path(path).stem.replace("_", " ").title()
        label, ok = QInputDialog.getText(self, "Add VM", "Display name:", text=default_label)
        if not ok or not label.strip():
            return
        vm = {"id": new_vm_id(self.vms), "label": label.strip(), "base": path}
        self.vms.append(vm)
        self.add_row(vm)

    def remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        names = ", ".join(self.vms[r]["label"] for r in rows)
        if QMessageBox.question(
            self, "Remove VM", f"Remove {names} from the list?\n\nDisk files on disk are not deleted."
        ) != QMessageBox.StandardButton.Yes:
            return
        for r in rows:
            self.table.removeRow(r)
            del self.vms[r]

    def on_accept(self):
        if not self.dir_edit.text().strip():
            QMessageBox.warning(self, "Settings", "Please choose a VM disk folder.")
            return
        for row in range(self.table.rowCount()):
            self.vms[row]["label"] = self.table.item(row, 0).text().strip() or self.vms[row]["label"]
        missing = [vm["label"] for vm in self.vms if not vm.get("base")]
        if missing:
            QMessageBox.warning(self, "Settings", "Choose a base disk for: " + ", ".join(missing))
            return
        self.accept()

    def result_config(self, cfg):
        cfg["image_dir"] = self.dir_edit.text().strip()
        cfg["libvirt_uri"] = self.uri_edit.text().strip() or DEFAULT_LIBVIRT_URI
        cfg["vms"] = self.vms
        return cfg


class Card:
    pass


# ----------------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------------
class Launcher(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(WINDOW_TITLE)
        self.setStyleSheet(STYLE)
        self.cfg = load_config()
        self._apply_libvirt_uri()
        self.ws, ws_note = pick_workspaces()
        self.sessions = {}     # vm id -> {"proc", "origin", "new"}
        self.cards = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 12)

        header = QHBoxLayout()
        header.addStretch()
        gear = QPushButton("⚙ Settings")
        gear.setObjectName("gear")
        gear.clicked.connect(self.open_settings)
        header.addWidget(gear)
        outer.addLayout(header)

        self.grid_holder = QWidget()
        self.grid = QGridLayout(self.grid_holder)
        self.grid.setSpacing(16)
        outer.addWidget(self.grid_holder)

        self.empty_label = QLabel("No virtual machines configured yet - click Settings to add one.")
        self.empty_label.setObjectName("status")
        outer.addWidget(self.empty_label)

        self.note = QLabel(ws_note)
        self.note.setObjectName("status")
        outer.addWidget(self.note)

        self.rebuild_cards()
        self.resize(640, 300)

        timer = QTimer(self)
        timer.timeout.connect(self.refresh)
        timer.start(2000)
        self._timer = timer  # keep a reference

        if not self.cfg["vms"]:
            QTimer.singleShot(0, self.open_settings)

    def _apply_libvirt_uri(self):
        global LIBVIRT_URI
        LIBVIRT_URI = self.cfg.get("libvirt_uri", DEFAULT_LIBVIRT_URI)

    # ---- settings ----------------------------------------------------
    def open_settings(self):
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.cfg = dlg.result_config(self.cfg)
            save_config(self.cfg)
            self._apply_libvirt_uri()
            self.rebuild_cards()

    # ---- UI construction -------------------------------------------------
    def rebuild_cards(self):
        for i in reversed(range(self.grid.count())):
            w = self.grid.itemAt(i).widget()
            if w:
                w.setParent(None)
        self.cards = {}
        vms = self.cfg["vms"]
        self.empty_label.setVisible(not vms)
        self.grid_holder.setVisible(bool(vms))
        for idx, vm in enumerate(vms):
            row, col = divmod(idx, 3)
            self.grid.addWidget(self._build_card(vm), row, col)
        self.refresh()

    def _build_card(self, vm):
        vid = vm["id"]
        c = Card()
        c.vm_id = vid
        frame = QFrame()
        frame.setObjectName("card")
        frame.setMinimumWidth(190)
        col = QVBoxLayout(frame)
        col.setContentsMargins(18, 18, 18, 18)
        col.setSpacing(10)

        title = QLabel(vm["label"])
        title.setObjectName("title")
        title.setWordWrap(True)
        c.status = QLabel("...")
        c.status.setObjectName("status")
        c.status.setWordWrap(True)
        c.start = QPushButton("Start")
        c.start.setObjectName("primary")
        c.start.clicked.connect(lambda _=False, v=vid: self.launch(v))

        small = QHBoxLayout()
        c.stop = QPushButton("Shut down")
        c.stop.clicked.connect(lambda _=False, v=vid: self.shutdown(v))
        c.reset = QPushButton("Reset")
        c.reset.setObjectName("danger")
        c.reset.clicked.connect(lambda _=False, v=vid: self.reset(v))
        small.addWidget(c.stop)
        small.addWidget(c.reset)

        col.addWidget(title)
        col.addWidget(c.status)
        col.addStretch()
        col.addWidget(c.start)
        col.addLayout(small)
        self.cards[vid] = c
        return frame

    def error(self, text):
        QMessageBox.critical(self, WINDOW_TITLE, text)

    def vm_by_id(self, vid):
        for vm in self.cfg["vms"]:
            if vm["id"] == vid:
                return vm
        return None

    # ---- domain / state --------------------------------------------------
    def domain_for(self, vm, interactive=False):
        if vm.get("domain"):
            return vm["domain"]
        base = Path(vm["base"])
        overlay = overlay_path(self.cfg["image_dir"], vm)
        name = find_domain([base, overlay])
        if not name and interactive:
            names = all_domains()
            if not names:
                raise RuntimeError("libvirt has no VMs defined. Create the VM in virt-manager first.")
            name, ok = QInputDialog.getItem(
                self, "Link VM", f"Which libvirt VM is {vm['label']}?", names, 0, False
            )
            if not ok:
                raise Cancelled()
            answer = QMessageBox.question(
                self, "Link VM",
                f"Use '{name}' as {vm['label']}?\n\nIts main disk will be switched to a disposable "
                f"overlay on top of:\n{base}",
            )
            if answer != QMessageBox.StandardButton.Yes:
                raise Cancelled()
        if name:
            vm["domain"] = name
            save_config(self.cfg)
        return name

    def domstate(self, domain):
        return virsh("domstate", domain, check=False).strip()

    def refresh(self):
        for vid, c in self.cards.items():
            vm = self.vm_by_id(vid)
            if vm is None:
                continue
            domain = vm.get("domain")
            state = self.domstate(domain) if domain else ""
            if not domain:
                text = "Ready - will link on first start"
            elif not Path(vm["base"]).exists():
                text = "Base disk not found - check Settings"
            else:
                text = {"running": "Running", "shut off": "Stopped", "paused": "Paused"}.get(
                    state, state or "Unknown")
            if vid in self.sessions:
                text += "  -  viewing"
            c.status.setText(text)
            c.start.setText("Open" if state in ("running", "paused") else "Start")
            c.start.setEnabled(vid not in self.sessions)
            c.stop.setEnabled(state == "running")

    # ---- preparing the overlay ------------------------------------------
    def prepare(self, vid, recreate=False):
        vm = self.vm_by_id(vid)
        base = Path(vm["base"])
        overlay = overlay_path(self.cfg["image_dir"], vm)
        if not base.exists():
            raise RuntimeError(f"Base disk not found:\n{base}\n\nFix its location in Settings.")
        domain = self.domain_for(vm, interactive=True)
        if not domain:
            raise RuntimeError(
                f"No libvirt VM uses {base.name}. Is the VM defined in virt-manager?")

        running = self.domstate(domain) not in ("shut off", "")
        if recreate or not overlay.exists():
            if running:
                virsh("destroy", domain, check=False)
                running = False
            try:
                base.chmod(0o444)          # protect the golden image from accidental writes
            except OSError:
                pass
            make_overlay(base, overlay)
        point_disk_at_overlay(domain, base, overlay, running)
        return domain

    # ---- actions ---------------------------------------------------------
    def launch(self, vid):
        if vid in self.sessions:
            return
        self.setCursor(Qt.CursorShape.WaitCursor)
        try:
            domain = self.prepare(vid)
            state = self.domstate(domain)
            if state == "paused":
                virsh("resume", domain)
            elif state != "running":
                virsh("start", domain)
        except Cancelled:
            return
        except Exception as e:
            self.error(str(e))
            return
        finally:
            self.unsetCursor()

        vm = self.vm_by_id(vid)
        session = {"proc": None, "origin": None, "new": None}
        self.sessions[vid] = session
        if self.ws:
            try:
                session["origin"] = self.ws.current()
                session["new"] = self.ws.create(f"{WINDOW_TITLE}: {vm['label']}")
                self.ws.switch(session["new"])
            except Exception as e:
                self.note.setText(f"Workspace switch failed: {e}")
                print(f"[vm-lab] workspace switch failed: {e}", file=sys.stderr)
        self.refresh()
        QTimer.singleShot(500, lambda: self.spawn_viewer(vid, domain))

    def spawn_viewer(self, vid, domain):
        session = self.sessions.get(vid)
        if session is None:
            return
        proc = QProcess(self)
        proc.finished.connect(lambda *_: self.viewer_closed(vid))
        proc.errorOccurred.connect(lambda *_: self.viewer_closed(vid))
        session["proc"] = proc
        proc.start("virt-viewer", ["-c", LIBVIRT_URI, "--full-screen", domain])

        if session["new"] is not None and hasattr(self.ws, "place_window"):
            session["tries"] = 0
            timer = QTimer(self)
            timer.timeout.connect(lambda: self.place_viewer(vid, domain))
            session["timer"] = timer
            timer.start(400)

    def place_viewer(self, vid, domain):
        """COSMIC: once the viewer window exists, pin it to the new workspace."""
        session = self.sessions.get(vid)
        if not session:
            return
        session["tries"] += 1
        try:
            if self.ws.place_window(domain, session["new"]):
                session["timer"].stop()
                self.note.setText("Viewer placed on its own workspace")
                return
        except Exception as e:
            session["timer"].stop()
            self.note.setText(f"Couldn't move viewer: {e}")
            return
        if session["tries"] > 25:
            session["timer"].stop()
            self.note.setText("Viewer window never appeared in cos-cli info")

    def viewer_closed(self, vid):
        session = self.sessions.pop(vid, None)
        if not session:
            return
        if session.get("timer"):
            session["timer"].stop()
        if self.ws and session["new"] is not None:
            try:
                if session["origin"] is not None:
                    self.ws.switch(session["origin"])
                self.ws.remove(session["new"])
            except Exception as e:
                print(f"[vm-lab] workspace cleanup failed: {e}", file=sys.stderr)
        self.raise_()
        self.activateWindow()
        self.refresh()

    def shutdown(self, vid):
        vm = self.vm_by_id(vid)
        domain = vm.get("domain") if vm else None
        if domain:
            virsh("shutdown", domain, check=False)
        self.refresh()

    def reset(self, vid):
        vm = self.vm_by_id(vid)
        label = vm["label"]
        answer = QMessageBox.question(
            self, "Reset",
            f"Reset {label} to its original install?\n\n"
            "Everything done in it since the base image (files, installs, settings) will be erased.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        session = self.sessions.get(vid)
        if session and session["proc"]:
            session["proc"].terminate()
        self.setCursor(Qt.CursorShape.WaitCursor)
        try:
            self.prepare(vid, recreate=True)
        except Cancelled:
            return
        except Exception as e:
            self.error(str(e))
            return
        finally:
            self.unsetCursor()
        self.refresh()
        QMessageBox.information(self, "Reset", f"{label} is back to its original state.")

    def closeEvent(self, event):
        for session in list(self.sessions.values()):
            if session["proc"]:
                session["proc"].terminate()   # closes the viewer only; VMs keep running
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    missing = [t for t in ("virsh", "virt-viewer", "qemu-img") if not shutil.which(t)]
    if missing:
        QMessageBox.critical(
            None, WINDOW_TITLE,
            "Missing tools: " + ", ".join(missing) +
            "\n\nInstall with:\nsudo apt install virt-viewer libvirt-clients qemu-utils",
        )
        return 1
    win = Launcher()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())