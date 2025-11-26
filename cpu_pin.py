#!/usr/bin/env python3
import sys
import os
import json
import subprocess
from typing import Dict, List

import psutil
from PyQt6 import QtCore, QtWidgets
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QFont, QAction


CONFIG_PATH = os.path.expanduser("~/.cpu_affinity_manager.json")


# ---------------------------------------------------------
#   Parse CPU sockets from: lscpu --extended
# ---------------------------------------------------------
def get_socket_core_map() -> Dict[int, List[int]]:
    socket_map: Dict[int, List[int]] = {}

    try:
        out = subprocess.check_output(["lscpu", "--extended"], text=True)
    except Exception:
        return {0: list(range(psutil.cpu_count()))}

    lines = out.strip().splitlines()
    if not lines:
        return {0: list(range(psutil.cpu_count()))}

    header = lines[0]
    cols = [c.strip().upper() for c in header.split()]
    try:
        cpu_idx = cols.index("CPU")
        socket_idx = cols.index("SOCKET")
    except ValueError:
        return {0: list(range(psutil.cpu_count()))}

    for line in lines[1:]:
        parts = line.split()
        if len(parts) <= max(cpu_idx, socket_idx):
            continue
        try:
            cpu_id = int(parts[cpu_idx])
            socket_id = int(parts[socket_idx])
        except ValueError:
            continue

        socket_map.setdefault(socket_id, []).append(cpu_id)

    for s in socket_map:
        socket_map[s] = sorted(socket_map[s])
    return socket_map


# ---------------------------------------------------------
#   Read CPU package temperatures via lm-sensors
# ---------------------------------------------------------
def read_socket_temperatures() -> Dict[int, float]:
    temps: Dict[int, float] = {}

    try:
        out = subprocess.check_output(["sensors"], text=True)
    except Exception:
        return temps

    current_socket = None

    for line in out.splitlines():
        line = line.strip()
        if "Package id 0:" in line:
            current_socket = 0
        elif "Package id 1:" in line:
            current_socket = 1

        if current_socket is not None and "+" in line and "°C" in line:
            try:
                temp_val = float(line.split("+")[1].split("°C")[0].strip())
                temps[current_socket] = temp_val
                current_socket = None
            except Exception:
                current_socket = None

    return temps


# ---------------------------------------------------------
#   Process table (PID, Name, CPU%, etc.)
# ---------------------------------------------------------
class ProcessTableModel(QtCore.QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.headers = ["PID", "Name", "User", "CPU %", "Affinity"]
        self.rows = []

    def update(self):
        data = []
        for p in psutil.process_iter(attrs=["pid", "name", "username"]):
            try:
                cpu = p.cpu_percent(interval=None)
                aff = p.cpu_affinity()
                aff_str = ",".join(str(c) for c in aff)
                data.append({
                    "pid": p.info["pid"],
                    "name": p.info.get("name", ""),
                    "user": p.info.get("username", ""),
                    "cpu": cpu,
                    "aff": aff_str,
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        data.sort(key=lambda x: x["cpu"], reverse=True)

        self.beginResetModel()
        self.rows = data
        self.endResetModel()

    def rowCount(self, parent=None):
        return len(self.rows)

    def columnCount(self, parent=None):
        return len(self.headers)

    def data(self, index, role):
        if not index.isValid():
            return None

        row = self.rows[index.row()]
        col = index.column()

        if role == QtCore.Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return str(row["pid"])
            if col == 1:
                return row["name"]
            if col == 2:
                return row["user"]
            if col == 3:
                return f"{row['cpu']:.1f}"
            if col == 4:
                return row["aff"]

        return None

    def get_pid_at(self, row):
        if 0 <= row < len(self.rows):
            return self.rows[row]["pid"]
        return -1


# ---------------------------------------------------------
#   Temperature table
# ---------------------------------------------------------
class TempTableModel(QtCore.QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []

    def update(self, temps: Dict[int, float]):
        self.beginResetModel()
        self.rows = [{"socket": s, "temp": temps[s]} for s in sorted(temps.keys())]
        self.endResetModel()

    def rowCount(self, parent=None):
        return len(self.rows)

    def columnCount(self, parent=None):
        return 2

    def data(self, index, role):
        if not index.isValid():
            return None

        row = self.rows[index.row()]
        col = index.column()

        if role == QtCore.Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return str(row["socket"])
            if col == 1:
                return f"{row['temp']:.1f}"

        return None


# ---------------------------------------------------------
#   Main GUI window
# ---------------------------------------------------------
class MainWindow(QtWidgets.QMainWindow):
    HIGH_CPU_THRESHOLD = 100.0
    HIGH_CPU_DURATION = 10  # seconds above threshold

    def __init__(self, socket_map):
        super().__init__()
        self.socket_map = socket_map
        self.setWindowTitle("CPU Affinity Manager")
        self.statusBar()

        # Track CPU usage durations & autopinned PIDs
        self.high_usage_counter: Dict[int, int] = {}
        self.autopinned_pids = set()

        # Cooler socket & tray icon state
        self.current_cooler_socket = None
        self.last_icon_state = None

        # Settings
        self.settings = self._load_settings()

        # ---------------- GUI LAYOUT ----------------
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        # Socket info
        self.socket_label = QtWidgets.QLabel(self._format_socket_info())
        layout.addWidget(self.socket_label)

        # Temperature table
        layout.addWidget(QtWidgets.QLabel("CPU Socket Temperatures:"))
        self.temp_model = TempTableModel(self)
        self.temp_view = QtWidgets.QTableView()
        self.temp_view.setModel(self.temp_model)
        self.temp_view.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.temp_view)

        # Per-core loads (sorted, hides 0%)
        layout.addWidget(QtWidgets.QLabel("Per-core CPU Load:"))
        self.core_table = QtWidgets.QTableWidget()
        self.core_table.setColumnCount(2)
        self.core_table.setHorizontalHeaderLabels(["Core", "Load %"])
        self.core_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.core_table)

        # Checkboxes
        self.chk_pause = QtWidgets.QCheckBox("Pause updates")
        self.chk_pause.setChecked(self.settings.get("pause", False))
        layout.addWidget(self.chk_pause)

        self.chk_auto_heavy = QtWidgets.QCheckBox(
            "Auto-pin any process >100% CPU for 10 seconds → cooler socket"
        )
        self.chk_auto_heavy.setChecked(self.settings.get("auto_heavy", False))
        layout.addWidget(self.chk_auto_heavy)

        # Exit button
        self.btn_exit_full = QtWidgets.QPushButton("Exit Application")
        self.btn_exit_full.setStyleSheet("background-color:#b33a3a; color:white;")
        layout.addWidget(self.btn_exit_full)
        self.btn_exit_full.clicked.connect(self.exit_application)

        # Process table
        layout.addWidget(QtWidgets.QLabel("Processes:"))
        self.table_model = ProcessTableModel(self)
        self.table_view = QtWidgets.QTableView()
        self.table_view.setModel(self.table_model)
        self.table_view.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table_view)

        # Manual pin buttons
        btn_l = QtWidgets.QHBoxLayout()
        self.btn_pin_socket0 = QtWidgets.QPushButton("Pin → Socket 0")
        self.btn_pin_socket1 = QtWidgets.QPushButton("Pin → Socket 1")
        btn_l.addWidget(self.btn_pin_socket0)
        btn_l.addWidget(self.btn_pin_socket1)
        layout.addLayout(btn_l)

        self.btn_pin_socket0.clicked.connect(self.pin_socket0)
        self.btn_pin_socket1.clicked.connect(self.pin_socket1)

        # Timers
        self.update_timer = QtCore.QTimer(self)
        self.update_timer.timeout.connect(self.refresh_all)
        self.update_timer.start(2000)

        self.autopin_timer = QtCore.QTimer(self)
        self.autopin_timer.timeout.connect(self.autopin_tick)
        self.autopin_timer.start(1000)

        # Tray icon
        self.tray_icon = self._create_tray_icon()
        self.tray_icon.show()

    # ---------------------------------------------------------
    # SETTINGS
    # ---------------------------------------------------------
    def _load_settings(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_settings(self):
        data = {
            "auto_heavy": self.chk_auto_heavy.isChecked(),
            "pause": self.chk_pause.isChecked(),
        }
        try:
            with open(CONFIG_PATH, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    # ---------------------------------------------------------
    # TRAY ICON
    # ---------------------------------------------------------
    def _make_icon_pixmap(self, char, color):
        pix = QPixmap(32, 32)
        pix.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        f = QFont()
        f.setPointSize(18)
        painter.setFont(f)
        painter.setPen(color)
        painter.drawText(pix.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, char)
        painter.end()
        return pix

    def _create_tray_icon(self):
        pix = self._make_icon_pixmap("C", QtCore.Qt.GlobalColor.white)
        tray = QtWidgets.QSystemTrayIcon(QIcon(pix), self)

        menu = QtWidgets.QMenu()

        act_show = QAction("Show", self)
        act_hide = QAction("Hide", self)
        act_quit = QAction("Quit", self)

        act_show.triggered.connect(self.show_from_tray)
        act_hide.triggered.connect(self.hide_from_tray)
        act_quit.triggered.connect(self.exit_application)

        menu.addAction(act_show)
        menu.addAction(act_hide)
        menu.addSeparator()
        menu.addAction(act_quit)

        tray.setContextMenu(menu)
        tray.activated.connect(self._tray_click)

        tray.setToolTip("CPU Affinity Manager")
        return tray

    def _update_tray_icon(self, max_temp):
        if max_temp is None:
            state = "idle"
            char = "C"
            col = QtCore.Qt.GlobalColor.white
        else:
            if max_temp <= 55:
                state = "cool"
                char = "C"
                col = QtCore.Qt.GlobalColor.white
            elif max_temp <= 70:
                state = "warm"
                char = "W"
                col = QtCore.Qt.GlobalColor.white
            else:
                state = "hot"
                char = "H"
                col = QtCore.Qt.GlobalColor.red

        if state == self.last_icon_state:
            return

        pix = self._make_icon_pixmap(char, col)
        self.tray_icon.setIcon(QIcon(pix))
        if max_temp is not None:
            self.tray_icon.setToolTip(f"Max temp: {max_temp:.1f} °C")
        else:
            self.tray_icon.setToolTip("CPU Affinity Manager")
        self.last_icon_state = state

    def _tray_click(self, reason):
        if reason == QtWidgets.QSystemTrayIcon.ActivationReason.Trigger:
            if self.isVisible():
                self.hide()
            else:
                self.show_from_tray()

    def show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def hide_from_tray(self):
        self.hide()

    # ---------------------------------------------------------
    # FORMAT SOCKET INFO
    # ---------------------------------------------------------
    def _format_socket_info(self):
        lines = []
        for s, cores in sorted(self.socket_map.items()):
            core_list = ", ".join(str(c) for c in cores)
            lines.append(f"Socket {s}: {core_list}")
        return "\n".join(lines)

    # ---------------------------------------------------------
    # EXIT HANDLING
    # ---------------------------------------------------------
    def exit_application(self):
        print("[EXIT] Closing application...")
        self._save_settings()

        try:
            self.tray_icon.hide()
        except Exception:
            pass

        try:
            self.update_timer.stop()
        except Exception:
            pass

        try:
            self.autopin_timer.stop()
        except Exception:
            pass

        QtWidgets.QApplication.quit()

    def closeEvent(self, event):
        event.ignore()
        self.hide()
        self.statusBar().showMessage("Still running in tray.", 3000)

    # ---------------------------------------------------------
    # UPDATE FUNCTIONS
    # ---------------------------------------------------------
    def refresh_all(self):
        if self.chk_pause.isChecked():
            return

        # Socket temps
        temps = read_socket_temperatures()
        self.temp_model.update(temps)

        # Determine cooler socket
        new_cooler = None
        if temps:
            new_cooler = min(temps.keys(), key=lambda s: temps[s])
        else:
            if 0 in self.socket_map:
                new_cooler = 0

        # If cooler socket changed, re-pin auto-managed PIDs
        if new_cooler != self.current_cooler_socket:
            old = self.current_cooler_socket
            self.current_cooler_socket = new_cooler
            if old is not None and new_cooler is not None:
                self._on_cooler_socket_changed(old, new_cooler)

        # Update tray icon
        max_temp = max(temps.values()) if temps else None
        self._update_tray_icon(max_temp)

        # Per-core loads
        self._update_core_loads()

        # Process table
        self.table_model.update()

    def _update_core_loads(self):
        percs = psutil.cpu_percent(interval=None, percpu=True)

        # Keep only active cores, sort by descending usage
        core_data = [(i, p) for i, p in enumerate(percs) if p > 0]
        core_data.sort(key=lambda x: x[1], reverse=True)

        self.core_table.setRowCount(len(core_data))

        for row, (core_id, val) in enumerate(core_data):
            item = QtWidgets.QTableWidgetItem(str(core_id))
            item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
            self.core_table.setItem(row, 0, item)

            bar = QtWidgets.QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(int(val))
            bar.setTextVisible(True)
            self.core_table.setCellWidget(row, 1, bar)

    # ---------------------------------------------------------
    # COOLER SOCKET CHANGE HANDLING
    # ---------------------------------------------------------
    def _on_cooler_socket_changed(self, old_socket, new_socket):
        print(f"[INFO] Cooler socket changed {old_socket} → {new_socket}")
        target = self.socket_map.get(new_socket)
        if not target:
            return

        for pid in list(self.autopinned_pids):
            try:
                psutil.Process(pid).cpu_affinity(target)
                print(f"[RE-PIN] PID {pid} moved to socket {new_socket}")
            except psutil.NoSuchProcess:
                self.autopinned_pids.discard(pid)
            except Exception:
                continue

        self.statusBar().showMessage(
            f"Cooler socket is now {new_socket}. Re-pinned auto-managed processes.",
            5000,
        )

    # ---------------------------------------------------------
    # MANUAL PIN BUTTONS
    # ---------------------------------------------------------
    def selected_pid(self):
        sel = self.table_view.selectionModel().selectedRows()
        if not sel:
            return -1
        return self.table_model.get_pid_at(sel[0].row())

    def pin_socket0(self):
        pid = self.selected_pid()
        if pid >= 0 and 0 in self.socket_map:
            try:
                psutil.Process(pid).cpu_affinity(self.socket_map[0])
            except Exception:
                pass
            self.refresh_all()

    def pin_socket1(self):
        pid = self.selected_pid()
        if pid >= 0 and 1 in self.socket_map:
            try:
                psutil.Process(pid).cpu_affinity(self.socket_map[1])
            except Exception:
                pass
            self.refresh_all()

    # ---------------------------------------------------------
    # AUTO-PIN ENGINE
    # ---------------------------------------------------------
    def autopin_tick(self):
        if not self.chk_auto_heavy.isChecked():
            return

        cooler = self.current_cooler_socket
        if cooler is None:
            if 0 in self.socket_map:
                cooler = 0
            else:
                return

        target = self.socket_map.get(cooler)
        if not target:
            return

        for p in psutil.process_iter(attrs=["pid", "name"]):
            try:
                pid = p.pid
                cpu = p.cpu_percent(interval=None)

                if cpu > self.HIGH_CPU_THRESHOLD:
                    self.high_usage_counter[pid] = self.high_usage_counter.get(pid, 0) + 1
                else:
                    self.high_usage_counter[pid] = 0

                if self.high_usage_counter[pid] >= self.HIGH_CPU_DURATION:
                    p.cpu_affinity(target)
                    name = p.info.get("name", "?")

                    print(
                        f"[AUTO-PIN] {pid} ({name}) >{self.HIGH_CPU_THRESHOLD}% for "
                        f"{self.HIGH_CPU_DURATION}s → socket {cooler}"
                    )
                    self.statusBar().showMessage(
                        f"Auto-pinned {pid} ({name}) to socket {cooler}",
                        5000,
                    )

                    self.high_usage_counter[pid] = 0
                    self.autopinned_pids.add(pid)

            except Exception:
                continue


# ---------------------------------------------------------
#   MAIN ENTRY
# ---------------------------------------------------------
def main():
    socket_map = get_socket_core_map()
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow(socket_map)
    # Start minimized to tray: don't show the window initially
    # (uncomment next line if you want it visible on start)
    # w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
