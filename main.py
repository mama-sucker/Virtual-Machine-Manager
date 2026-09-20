# Python based QT application for easily controlling my virtual machines in libvirt 

#!/usr/bin/env python3
"""
Virtual Machine Manager - one-click disposable Kali / Windows 11 VMs on top of libvirt.

How it works
  * kali_disk.qcow2 / windows11_disk.qcow2 are treated as read-only "golden" base images.
  * The VMs actually boot from thin overlays (kali_live.qcow2 / windows11_live.qcow2)
    that store only the changes. "Reset" = delete the overlay and recreate it.
  * "Start" opens a new virtual desktop, switches to it, and launches virt-viewer fullscreen.
    When you close the viewer, it switches back and removes that desktop.

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
import xml.etree.ElementTree as ET
from pathlib import Path

from PyQt6.QtCore import QProcess, Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QInputDialog, QLabel,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

# ----------------------------------------------------------------------------
# Settings - tweak these if your setup differs
# ----------------------------------------------------------------------------
LIBVIRT_URI = "qemu:///system"          # use "qemu:///session" if your VMs live in the user session
IMAGE_DIR = Path.home() / "virt-machines" / "images"
CONFIG_FILE = Path.home() / ".config" / "virtual-machine-manager" / "config.json"
WINDOW_TITLE = "Virtual Machine Manager"

VMS = {
    "kali": {
        "label": "Kali Linux",
        "base": IMAGE_DIR / "kali_disk.qcow2",
        "overlay": IMAGE_DIR / "kali_live.qcow2",
    },
    "windows": {
        "label": "Windows 11",
        "base": IMAGE_DIR / "windows11_disk.qcow2",
        "overlay": IMAGE_DIR / "windows11_live.qcow2",
    },
}


class Cancelled(Exception):
    pass


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
# Workspace (virtual desktop) backends
# ----------------------------------------------------------------------------
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


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
        return UUID.findall(self._get("desktops"))

    def current(self):
        m = UUID.search(self._get("current"))
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
            return KDEWorkspaces(), "Workspaces: KDE Plasma"
        return None, "Workspaces off: gdbus missing (sudo apt install libglib2.0-bin)"
    if session == "x11":
        if shutil.which("wmctrl"):
            return WmctrlWorkspaces(), "Workspaces: wmctrl (X11)"
        return None, "Workspaces off: run  sudo apt install wmctrl"
    return None, f"Workspaces off: no API for '{desktop or 'this desktop'}' on {session}"


# ----------------------------------------------------------------------------
# Config (remembers which libvirt domain belongs to which machine)
# ----------------------------------------------------------------------------
def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def save_config(cfg):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
STYLE = """
QWidget { background:#15171c; color:#e6e6e6; font-size:14px; }
QLabel { background:transparent; }
QFrame#card { background:#1e2129; border:1px solid #2c3140; border-radius:12px; }
QLabel#title { font-size:20px; font-weight:600; }
QLabel#status { color:#8b93a7; }
QPushButton { background:#2c3140; border:none; border-radius:8px; padding:10px 14px; }
QPushButton:hover { background:#384058; }
QPushButton:disabled { color:#5c6478; background:#22262f; }
QPushButton#primary { background:#a31a1a; color:white; font-weight:600; padding:14px; }
QPushButton#primary:hover { background:#2f6fd8; }
QPushButton#primary:disabled { background:#22262f; color:#5c6478; }
QPushButton#danger { color:#ff9a9a; }
"""


class Card:
    pass


class Launcher(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(WINDOW_TITLE)
        self.setStyleSheet(STYLE)
        self.config = load_config()
        self.ws, ws_note = pick_workspaces()
        self.sessions = {}     # key -> {"proc", "origin", "new"}
        self.cards = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 12)
        row = QHBoxLayout()
        row.setSpacing(16)
        for key, vm in VMS.items():
            row.addWidget(self._build_card(key, vm))
        outer.addLayout(row)
        self.note = QLabel(ws_note)
        self.note.setObjectName("status")
        outer.addWidget(self.note)
        self.resize(560, 270)

        for key in VMS:                       # try to auto-link domains to machines
            try:
                self.domain_for(key)
            except Exception:
                pass
        self.refresh()
        timer = QTimer(self)
        timer.timeout.connect(self.refresh)
        timer.start(2000)

    # ---- UI construction -------------------------------------------------
    def _build_card(self, key, vm):
        c = Card()
        frame = QFrame()
        frame.setObjectName("card")
        col = QVBoxLayout(frame)
        col.setContentsMargins(18, 18, 18, 18)
        col.setSpacing(10)

        title = QLabel(vm["label"])
        title.setObjectName("title")
        c.status = QLabel("...")
        c.status.setObjectName("status")
        c.start = QPushButton("Start")
        c.start.setObjectName("primary")
        c.start.clicked.connect(lambda _=False, k=key: self.launch(k))

        small = QHBoxLayout()
        c.stop = QPushButton("Shut down")
        c.stop.clicked.connect(lambda _=False, k=key: self.shutdown(k))
        c.reset = QPushButton("Reset")
        c.reset.setObjectName("danger")
        c.reset.clicked.connect(lambda _=False, k=key: self.reset(k))
        small.addWidget(c.stop)
        small.addWidget(c.reset)

        col.addWidget(title)
        col.addWidget(c.status)
        col.addStretch()
        col.addWidget(c.start)
        col.addLayout(small)
        self.cards[key] = c
        return frame

    def error(self, text):
        QMessageBox.critical(self, "Virtual Machine Manager", text)

    # ---- domain / state --------------------------------------------------
    def domain_for(self, key, interactive=False):
        if key in self.config:
            return self.config[key]
        vm = VMS[key]
        name = find_domain([vm["base"], vm["overlay"]])
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
                f"overlay on top of:\n{vm['base']}",
            )
            if answer != QMessageBox.StandardButton.Yes:
                raise Cancelled()
        if name:
            self.config[key] = name
            save_config(self.config)
        return name

    def domstate(self, domain):
        return virsh("domstate", domain, check=False).strip()

    def refresh(self):
        for key, c in self.cards.items():
            domain = self.config.get(key)
            state = self.domstate(domain) if domain else ""
            if not domain:
                text = "Ready - will link on first start"
            else:
                text = {"running": "Running", "shut off": "Stopped", "paused": "Paused"}.get(
                    state, state or "Unknown")
            if key in self.sessions:
                text += "  -  viewing"
            c.status.setText(text)
            c.start.setText("Open" if state in ("running", "paused") else "Start")
            c.start.setEnabled(key not in self.sessions)
            c.stop.setEnabled(state == "running")

    # ---- preparing the overlay ------------------------------------------
    def prepare(self, key, recreate=False):
        vm = VMS[key]
        base, overlay = vm["base"], vm["overlay"]
        if not base.exists():
            raise RuntimeError(f"Base image not found:\n{base}")
        domain = self.domain_for(key, interactive=True)
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
    def launch(self, key):
        if key in self.sessions:
            return
        self.setCursor(Qt.CursorShape.WaitCursor)
        try:
            domain = self.prepare(key)
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

        session = {"proc": None, "origin": None, "new": None}
        self.sessions[key] = session
        if self.ws:
            try:
                session["origin"] = self.ws.current()
                session["new"] = self.ws.create(f"Virtual Machine Manager: {VMS[key]['label']}")
                self.ws.switch(session["new"])
            except Exception as e:
                self.note.setText(f"Workspace switch failed: {e}")
                print(f"[vm-lab] workspace switch failed: {e}", file=sys.stderr)
        self.refresh()
        QTimer.singleShot(500, lambda: self.spawn_viewer(key, domain))

    def spawn_viewer(self, key, domain):
        session = self.sessions.get(key)
        if session is None:
            return
        proc = QProcess(self)
        proc.finished.connect(lambda *_: self.viewer_closed(key))
        proc.errorOccurred.connect(lambda *_: self.viewer_closed(key))
        session["proc"] = proc
        proc.start("virt-viewer", ["-c", LIBVIRT_URI, "--full-screen", domain])

        if session["new"] is not None and hasattr(self.ws, "place_window"):
            session["tries"] = 0
            timer = QTimer(self)
            timer.timeout.connect(lambda: self.place_viewer(key, domain))
            session["timer"] = timer
            timer.start(400)

    def place_viewer(self, key, domain):
        """COSMIC: once the viewer window exists, pin it to the new workspace."""
        session = self.sessions.get(key)
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

    def viewer_closed(self, key):
        session = self.sessions.pop(key, None)
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

    def shutdown(self, key):
        domain = self.config.get(key)
        if domain:
            virsh("shutdown", domain, check=False)
        self.refresh()

    def reset(self, key):
        label = VMS[key]["label"]
        answer = QMessageBox.question(
            self, "Reset",
            f"Reset {label} to its original install?\n\n"
            "Everything done in it since the base image (files, installs, settings) will be erased.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        session = self.sessions.get(key)
        if session and session["proc"]:
            session["proc"].terminate()
        self.setCursor(Qt.CursorShape.WaitCursor)
        try:
            self.prepare(key, recreate=True)
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
            None, "Virtual Machine Manager",
            "Missing tools: " + ", ".join(missing) +
            "\n\nInstall with:\nsudo apt install virt-viewer libvirt-clients qemu-utils",
        )
        return 1
    win = Launcher()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())