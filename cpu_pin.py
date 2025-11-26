#!/usr/bin/env python3
import sys
import subprocess
from typing import Dict, List

import psutil
from PyQt6 import QtCore, QtWidgets


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
#   Read CPU temperatures using lm-sensors
# ---------------------------------------------------------
def read_socket_temperatures() -> Dict[int, float]:
    temps = {}

    try:
        out = subprocess.check_output(["sensors"], text=True)
    except Exception:
        return temps

    current_socket = None

    for line in out.splitlines():
        if "Package id 0:" in line:
            current_socket = 0
        elif "Package id 1:" in line:
            current_socket = 1

        if current_socket is not None and "+" in line and "°C" in line:
            try:
                temp_str = line.split("+")[1].split("°C")[0].strip()
                temps[current_socket] = float(temp_str)
                current_socket = None
            except Exception:
                pass

    return temps


# ---------------------------------------------------------
#   Process Table Model
# ---------------------------------------------------------
class ProcessTableModel(QtCore.QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.headers = ["PID", "Name", "User", "CPU %", "Affinity"]
        self.rows = []

    def update(self):
        new_rows = []
        for p in psutil.process_iter(attrs=["pid", "name", "username"]):
            try:
                cpu = p.cpu_percent(interval=None)
                aff = p.cpu_affinity() if hasattr(p, "cpu_affinity") else []
                aff_str = ",".join(str(c) for c in aff)
                new_rows.append({
                    "pid": p.info["pid"],
                    "name": p.info.get("name", ""),
                    "user": p.info.get("username", ""),
                    "cpu": cpu,
                    "aff": aff_str,
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # Sort by CPU descending
        new_rows.sort(key=lambda x: x["cpu"], reverse=True)

        self.beginResetModel()
        self.rows = new_rows
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
            if col == 0: return str(row["pid"])
            if col == 1: return row["name"]
            if col == 2: return row["user"]
            if col == 3: return f"{row['cpu']:.1f}"
            if col == 4: return row["aff"]

        return None

    def get_pid_at(self, row):
        if 0 <= row < len(self.rows):
            return self.rows[row]["pid"]
        return -1


# ---------------------------------------------------------
#   Temperature Table Model
# ---------------------------------------------------------
class TempTableModel(QtCore.QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.headers = ["Socket", "Temperature (°C)"]
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
            elif col == 1:
                return f"{row['temp']:.1f}"

        return None


# ---------------------------------------------------------
#   Main Window
# ---------------------------------------------------------
class MainWindow(QtWidgets.QMainWindow):
    HIGH_CPU_THRESHOLD = 100.0
    HIGH_CPU_DURATION = 10

    def __init__(self, socket_map):
        super().__init__()
        self.socket_map = socket_map

        self.setWindowTitle("CPU Affinity Manager")
        self.statusBar()

        self.high_usage_counter = {}

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        # Socket info text
        self.socket_label = QtWidgets.QLabel(self._format_socket_info())
        layout.addWidget(self.socket_label)

        # Temperature table title
        layout.addWidget(QtWidgets.QLabel("CPU Socket Temperatures:"))

        # Temperature table
        self.temp_model = TempTableModel(self)
        self.temp_view = QtWidgets.QTableView()
        self.temp_view.setModel(self.temp_model)
        self.temp_view.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.temp_view)

        # Pause
        self.chk_pause = QtWidgets.QCheckBox("Pause updates")
        layout.addWidget(self.chk_pause)

        # Process table
        layout.addWidget(QtWidgets.QLabel("Processes:"))
        self.table_model = ProcessTableModel(self)
        self.table_view = QtWidgets.QTableView()
        self.table_view.setModel(self.table_model)
        self.table_view.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table_view)

        # Buttons
        btn_layout = QtWidgets.QHBoxLayout()
        self.btn_pin_socket0 = QtWidgets.QPushButton("Pin → Socket 0")
        self.btn_pin_socket1 = QtWidgets.QPushButton("Pin → Socket 1")
        btn_layout.addWidget(self.btn_pin_socket0)
        btn_layout.addWidget(self.btn_pin_socket1)
        layout.addLayout(btn_layout)

        # Auto pin heavy CPU
        self.chk_auto_heavy = QtWidgets.QCheckBox(
            "Auto-pin any process >100% CPU for 10 seconds → Socket 0"
        )
        layout.addWidget(self.chk_auto_heavy)

        # Signals
        self.btn_pin_socket0.clicked.connect(self.pin_socket0)
        self.btn_pin_socket1.clicked.connect(self.pin_socket1)

        # Timers
        self.update_timer = QtCore.QTimer(self)
        self.update_timer.timeout.connect(self.refresh_all)
        self.update_timer.start(2000)

        self.autopin_timer = QtCore.QTimer(self)
        self.autopin_timer.timeout.connect(self.autopin_tick)
        self.autopin_timer.start(1000)

        self.refresh_all()

    def _format_socket_info(self):
        lines = [
            f"Socket {s}: {', '.join(map(str, cores))}"
            for s, cores in sorted(self.socket_map.items())
        ]
        return "\n".join(lines)

    # --------------------------
    # Updaters
    # --------------------------
    def refresh_all(self):
        if not self.chk_pause.isChecked():
            temps = read_socket_temperatures()
            self.temp_model.update(temps)
            self.table_model.update()

    # --------------------------
    # Manual pinning
    # --------------------------
    def selected_pid(self):
        sel = self.table_view.selectionModel().selectedRows()
        if not sel:
            return -1
        return self.table_model.get_pid_at(sel[0].row())

    def _set_affinity(self, pid, cores):
        try:
            psutil.Process(pid).cpu_affinity(cores)
        except Exception:
            pass
        self.refresh_all()

    def pin_socket0(self):
        pid = self.selected_pid()
        if pid >= 0 and 0 in self.socket_map:
            self._set_affinity(pid, self.socket_map[0])

    def pin_socket1(self):
        pid = self.selected_pid()
        if pid >= 0 and 1 in self.socket_map:
            self._set_affinity(pid, self.socket_map[1])

    # --------------------------
    # Auto pinning engine (generic)
    # --------------------------
    def autopin_tick(self):
        if not self.chk_auto_heavy.isChecked():
            return

        target = self.socket_map.get(0)
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
                        f"[AUTO-PIN] PID {pid} ({name}) exceeded {self.HIGH_CPU_THRESHOLD}% CPU "
                        f"for {self.HIGH_CPU_DURATION}s → pinned to Socket 0 {target}"
                    )
                    self.statusBar().showMessage(
                        f"Auto-pinned PID {pid} ({name}) to Socket 0", 5000
                    )

                    self.high_usage_counter[pid] = 0

            except Exception:
                continue


# ---------------------------------------------------------
#   Main
# ---------------------------------------------------------
def main():
    socket_map = get_socket_core_map()

    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow(socket_map)
    w.resize(950, 650)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
