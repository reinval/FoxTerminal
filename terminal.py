import sys
import os
import serial
import serial.tools.list_ports
import time
import  json
from datetime import datetime

from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                               QHBoxLayout, QLabel, QComboBox, QPushButton, 
                               QCheckBox, QTextEdit, QLineEdit, QGroupBox , 
                               QFileDialog, QMessageBox, QMenu, QDialog, QRadioButton,
                               QGridLayout, QInputDialog)
from PySide6.QtCore import QTimer, Qt, QObject, Signal, Slot, QThread
from PySide6.QtGui import QAction, QTextCursor, QColor, QTextCharFormat, QFont, QActionGroup, QIcon, QPixmap

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        # Not running in a bundle
        base_path = os.path.dirname(os.path.abspath(__file__))

    return os.path.join(base_path, relative_path)

class SerialManager(QObject):
    """Manages serial port in a separate QThread using signals and slots."""
    data_received = Signal(bytes)
    error_occurred = Signal(str)
    finished = Signal()

    def __init__(self):
        super().__init__()
        self.ser = None
        self._running = False
        self.data_paused = False

    @Slot(dict)
    def run(self, settings):
        """Connects to the serial port and starts the reading loop."""
        if self._running:
            return

        try:
            self.ser = serial.Serial(
                port=settings['port'],
                baudrate=settings['baudrate'],
                bytesize=settings['bytesize'],
                parity=settings['parity'],
                stopbits=settings['stopbits'],
                timeout=0.1,
                xonxoff=settings['xonxoff'],
                rtscts=settings['rtscts'],
                dsrdtr=settings['dsrdtr']
            )
        except serial.SerialException as e:
            self.error_occurred.emit(str(e))
            self.finished.emit()
            return

        self._running = True
        while self._running:
            if not self.ser or not self.ser.is_open:
                self.error_occurred.emit("Ühendus katkes.")
                break

            if not self.data_paused:
                try:
                    chunk = self.ser.read(self.ser.in_waiting or 1)
                    if chunk:
                        self.data_received.emit(chunk)
                except serial.SerialException:
                    self.error_occurred.emit("Andmete lugemisel tekkis viga.")
                    break
            QThread.msleep(10) # Yield to other events

        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self._running = False
        self.finished.emit()

    @Slot()
    def stop(self):
        """Signals the reading loop to stop."""
        self._running = False

    @Slot(bytes)
    def write(self, data):
        """Write data to the serial port."""
        if self.ser and self.ser.is_open:
            try:
                self.ser.write(data)
            except serial.SerialException:
                self.error_occurred.emit("Andmete saatmisel tekkis viga.")

    @Slot(bool)
    def set_data_paused(self, paused):
        """Pause or resume reading data from the port."""
        self.data_paused = paused

class UniversalTerminal(QMainWindow):
    start_serial_worker = Signal(dict)
    stop_serial_worker = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Fox Terminal v2.2")
        self.icon_path = resource_path("favicon.ico")
        self.setWindowIcon(QIcon(self.icon_path))
        self.resize(1000, 700)
        
        # --- MUUTUJAD ---
        self.log_file = None
        self.last_receive_time = 0
        self.buffer = b""
        self.binary_log = False
        self.packet_timeout = 0.2 # Sekundit - aeg, millal loeme paketi lõppenuks
        self.eol_char = b'\n' # Paketi lõpumärk
        self.parity_map = {
            "None": serial.PARITY_NONE, "Even": serial.PARITY_EVEN, "Odd": serial.PARITY_ODD,
            "Mark": serial.PARITY_MARK, "Space": serial.PARITY_SPACE
        }
        self.stopbits_map = {
            "1": serial.STOPBITS_ONE, "1.5": serial.STOPBITS_ONE_POINT_FIVE, "2": serial.STOPBITS_TWO
        }

        # Olekumuutujad (asendavad tkinteri *Var muutujaid)
        self.is_connected = False
        self.screen_paused = False
        self.is_logging  = False
        self.show_timestamps = True
        self.send_as_hex = False
        self.auto_reconnect = False
        self.strip_incoming_text = True
        
        # --- Lõimede seadistus ---
        self.worker_thread = QThread()
        self.serial_manager = SerialManager()
        self.serial_manager.moveToThread(self.worker_thread)

        # Signaalide ja slottide ühendamine
        self.serial_manager.data_received.connect(self.process_incoming_bytes)
        self.serial_manager.error_occurred.connect(self._handle_serial_error)
        self.serial_manager.finished.connect(self._on_worker_finished)
        self.start_serial_worker.connect(self.serial_manager.run)
        self.stop_serial_worker.connect(self.serial_manager.stop, Qt.DirectConnection)
        self.worker_thread.start()
        
        self.setup_ui()
        
        self.buffer_flush_timer = QTimer(self)
        self.buffer_flush_timer.timeout.connect(self.check_buffer_timeout)
        self.buffer_flush_timer.start(100)

    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        self.main_layout = QVBoxLayout(central_widget)

        self._setup_menubar()
        
        # --- ÜLEMINE RIDA (Ühendus + Makrod) ---
        top_row_layout = QHBoxLayout()
        self.main_layout.addLayout(top_row_layout)

        top_row_layout.addWidget(self._create_connection_group())
        top_row_layout.addWidget(self._create_macro_group())

        self._setup_delimiter_group()
        self._setup_control_group()
        self._setup_terminal_area()
        self._setup_input_area()

        # Algne portide laadimine
        self.refresh_ports()
        self.set_theme("Dark")

    def _setup_menubar(self):
        menubar = self.menuBar()
        
        # File menüü
        file_menu = menubar.addMenu("File")
        exit_action = QAction("Exit Program ", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        
        # Settings menüü
        settings_menu = menubar.addMenu("Settings")
        theme_menu = settings_menu.addMenu("Theme")
        
        self.action_dark = QAction("Dark Mode", self)
        self.action_dark.setCheckable(True)
        self.action_dark.triggered.connect(lambda: self.set_theme("Dark"))
        theme_menu.addAction(self.action_dark)

        self.action_light = QAction("Light Mode", self)
        self.action_light.setCheckable(True)
        self.action_light.triggered.connect(lambda: self.set_theme("Light"))
        theme_menu.addAction(self.action_light)

        theme_group = QActionGroup(self)
        theme_group.addAction(self.action_dark)
        theme_group.addAction(self.action_light)
        self.action_dark.setChecked(True)

        settings_menu.addSeparator()

        # Packet Timeout menüü
        timeout_menu = settings_menu.addMenu("Packet Timeout")
        timeout_group = QActionGroup(self)
        self.timeout_actions = {}

        for t_ms in [100, 200, 500, 1000]:
            action = QAction(f"{t_ms} ms", self)
            action.setCheckable(True)
            action.triggered.connect(lambda checked, val=t_ms: self.set_packet_timeout(val))
            timeout_menu.addAction(action)
            timeout_group.addAction(action)
            self.timeout_actions[t_ms] = action
            if t_ms == 200: # Vaikimisi väärtus
                action.setChecked(True)

        self.custom_timeout_action = QAction("Custom...", self)
        self.custom_timeout_action.setCheckable(True)
        self.custom_timeout_action.triggered.connect(self.ask_custom_timeout)
        timeout_menu.addAction(self.custom_timeout_action)
        timeout_group.addAction(self.custom_timeout_action)

        # About menüü
        about_menu = menubar.addMenu("About")
        
        help_action = QAction("Help", self)
        help_action.triggered.connect(self.show_help)
        about_menu.addAction(help_action)
        
        credits_action = QAction("Credits", self)
        credits_action.triggered.connect(self.show_credits)
        about_menu.addAction(credits_action)

    def _create_connection_group(self):
        conn_group = QGroupBox("Ühenduse seaded")
        conn_layout = QGridLayout(conn_group)

        # Row 0
        conn_layout.addWidget(QLabel("Port:"), 0, 0)
        self.port_combo = QComboBox()
        self.port_combo.setToolTip("Vali seadme COM port")
        conn_layout.addWidget(self.port_combo, 0, 1)
        
        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setFixedWidth(30)
        self.refresh_btn.setToolTip("Värskenda portide nimekirja")
        self.refresh_btn.clicked.connect(self.refresh_ports)
        conn_layout.addWidget(self.refresh_btn, 0, 2)

        conn_layout.addWidget(QLabel("Baud:"), 0, 3)
        self.baud_combo = QComboBox()
        self.baud_combo.setEditable(True)
        self.baud_combo.addItems(["1200", "2400", "4800", "9600", "14400", "19200", "38400", "57600", "115200", "230400", "460800", "921600"])
        self.baud_combo.setCurrentText("9600")
        self.baud_combo.setToolTip("Vali või sisesta käsitsi (Custom)")
        conn_layout.addWidget(self.baud_combo, 0, 4)

        # Row 1
        conn_layout.addWidget(QLabel("Data Bits:"), 1, 0)
        self.databits_combo = QComboBox()
        self.databits_combo.addItems(["8", "7", "6", "5"])
        self.databits_combo.setCurrentText("8")
        conn_layout.addWidget(self.databits_combo, 1, 1)

        conn_layout.addWidget(QLabel("Parity:"), 1, 3)
        self.parity_combo = QComboBox()
        self.parity_combo.addItems(list(self.parity_map.keys()))
        self.parity_combo.setCurrentText("None")
        conn_layout.addWidget(self.parity_combo, 1, 4)

        # Row 2
        conn_layout.addWidget(QLabel("Stop Bits:"), 2, 0)
        self.stopbits_combo = QComboBox()
        self.stopbits_combo.addItems(list(self.stopbits_map.keys()))
        self.stopbits_combo.setCurrentText("1")
        conn_layout.addWidget(self.stopbits_combo, 2, 1)

        conn_layout.addWidget(QLabel("Flow Control:"), 2, 3)
        self.flowcontrol_combo = QComboBox()
        self.flowcontrol_combo.addItems(["None", "XON/XOFF", "RTS/CTS", "DSR/DTR"])
        self.flowcontrol_combo.setCurrentText("None")
        conn_layout.addWidget(self.flowcontrol_combo, 2, 4)

        # Right side column for connect button and mode
        v_layout = QVBoxLayout()
        self.connect_btn = QPushButton("Ühenda")
        self.connect_btn.setToolTip("Ava või sule ühendus valitud pordiga")
        self.connect_btn.clicked.connect(self.toggle_connection)
        self.connect_btn.setStyleSheet("background-color: #e8f5e9; color: black;")
        v_layout.addWidget(self.connect_btn)

        mode_layout = QHBoxLayout()
        mode_layout.addWidget(QLabel("REŽIIM:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Tekst", "HEX", "JSON", "CSV", "Modbus", "ASCII Table"])
        self.mode_combo.setToolTip("Tekst: Tavaline lugemine\nHEX: Toored baidid\nJSON: Teeb andmed loetavaks\nCSV: Joondab tabelina\nModbus: Eraldab ID ja Käsu\nASCII Table: HEX ja sümbol")
        mode_layout.addWidget(self.mode_combo)
        v_layout.addLayout(mode_layout)
        v_layout.addStretch()
        
        conn_layout.addLayout(v_layout, 0, 5, 3, 1) # Span all 3 rows
        conn_layout.setColumnStretch(6, 1)
        
        return conn_group

    def _create_macro_group(self):
        macro_group = QGroupBox("Makrod")
        macro_layout = QGridLayout(macro_group)

        self.btn_m1 = QPushButton("M1")
        self.btn_m1.setToolTip("Saada Makro 1")
        self.btn_m1.clicked.connect(lambda: self.send_macro(1))
        self.entry_m1 = QLineEdit()
        self.entry_m1.setPlaceholderText("Käsk 1")
        macro_layout.addWidget(self.btn_m1, 0, 0)
        macro_layout.addWidget(self.entry_m1, 0, 1)

        self.btn_m2 = QPushButton("M2")
        self.btn_m2.setToolTip("Saada Makro 2")
        self.btn_m2.clicked.connect(lambda: self.send_macro(2))
        self.entry_m2 = QLineEdit()
        self.entry_m2.setPlaceholderText("Käsk 2")
        macro_layout.addWidget(self.btn_m2, 1, 0)
        macro_layout.addWidget(self.entry_m2, 1, 1)
        
        return macro_group

    def _setup_delimiter_group(self):
        delim_group = QGroupBox("Paketi Lõpumärk")
        delim_layout = QHBoxLayout()
        delim_group.setLayout(delim_layout)
        self.main_layout.addWidget(delim_group)

        delimiters = [("LF (\\n)", b'\n'), ("CR (\\r)", b'\r'), ("Semicolon (;)", b';'), ("Colon (:)", b':')]
        
        for name, code in delimiters:
            rb = QRadioButton(name)
            if code == self.eol_char: rb.setChecked(True)
            rb.toggled.connect(lambda checked, c=code: setattr(self, 'eol_char', c) if checked else None)
            delim_layout.addWidget(rb)
        
        delim_layout.addStretch()

    def _setup_control_group(self):
        mgmt_group = QGroupBox("Kontroll ja Logimine")
        mgmt_layout = QHBoxLayout()
        mgmt_group.setLayout(mgmt_layout)
        self.main_layout.addWidget(mgmt_group)

        self.cb_ts = QCheckBox("Ajatemplid")
        self.cb_ts.setChecked(True)
        self.cb_ts.setToolTip("Näita iga uue paketi alguses kellaaega")
        self.cb_ts.stateChanged.connect(lambda s: setattr(self, 'show_timestamps', bool(s)))
        mgmt_layout.addWidget(self.cb_ts)

        self.cb_log = QCheckBox("Logi faili")
        self.cb_log.setToolTip("Salvesta sissetulev info faili (.txt või .bin)")
        self.cb_log.clicked.connect(self.toggle_logging_file)
        mgmt_layout.addWidget(self.cb_log)

        self.cb_pause = QCheckBox("Peata ekraan")
        self.cb_pause.setToolTip("Peata teksti lisamine terminali (logimine jätkub taustal)")
        self.cb_pause.stateChanged.connect(lambda s: setattr(self, 'screen_paused', bool(s)))
        mgmt_layout.addWidget(self.cb_pause)

        self.cb_data = QCheckBox("Peata andmed")
        self.cb_data.setToolTip("Lõpeta andmete lugemine serial puhvrist")
        self.cb_data.stateChanged.connect(lambda s: self.serial_manager.set_data_paused(bool(s)))
        mgmt_layout.addWidget(self.cb_data)

        self.cb_reconnect = QCheckBox("Auto-reconnect")
        self.cb_reconnect.setToolTip("Proovi ühendust taastada, kui see katkeb")
        self.cb_reconnect.stateChanged.connect(lambda s: setattr(self, 'auto_reconnect', bool(s)))
        mgmt_layout.addWidget(self.cb_reconnect)

        self.cb_strip = QCheckBox("Eemalda reavahetus")
        self.cb_strip.setChecked(True)
        self.cb_strip.setToolTip("Eemalda sissetuleva teksti algusest ja lõpust tühikud (sh reavahetused)")
        self.cb_strip.stateChanged.connect(lambda s: setattr(self, 'strip_incoming_text', bool(s)))
        mgmt_layout.addWidget(self.cb_strip)

        mgmt_layout.addStretch()
        
        copy_btn = QPushButton("Kopeeri kõik")
        copy_btn.clicked.connect(self.copy_all)
        mgmt_layout.addWidget(copy_btn)
        
        clear_btn = QPushButton("Puhasta ekraan")
        clear_btn.clicked.connect(self.clear_screen)
        mgmt_layout.addWidget(clear_btn)

    def _setup_terminal_area(self):
        self.output_area = QTextEdit()
        self.output_area.setReadOnly(True)
        font = QFont("Consolas", 10)
        self.output_area.setFont(font)
        self.main_layout.addWidget(self.output_area)

    def _setup_input_area(self):
        input_layout = QHBoxLayout()
        self.main_layout.addLayout(input_layout)

        self.input_field = QLineEdit()
        self.input_field.returnPressed.connect(self.send_command)
        input_layout.addWidget(self.input_field)

        self.cb_hex_send = QCheckBox("Saada HEX")
        self.cb_hex_send.setToolTip("Tõlgenda sisendit HEX koodidena (nt 41 42)")
        self.cb_hex_send.stateChanged.connect(lambda s: setattr(self, 'send_as_hex', bool(s)))
        input_layout.addWidget(self.cb_hex_send)

        send_btn = QPushButton("Saada")
        send_btn.setStyleSheet("background-color: #bbdefb; color: black;")
        send_btn.clicked.connect(self.send_command)
        input_layout.addWidget(send_btn)

    def get_ports(self):
        ports = serial.tools.list_ports.comports()
        return [port.device for port in ports] or ["Puudub"]

    def refresh_ports(self):
        ports = self.get_ports()
        self.port_combo.clear()
        self.port_combo.addItems(ports)

    def _get_connection_settings(self):
        """Kogub ühenduse seaded kasutajaliideselt ja tagastab need sõnastikuna."""
        port = self.port_combo.currentText()
        if port == "Puudub":
            raise serial.SerialException("Porti pole valitud või ei leitud ühtegi porti.")
        
        return {
            'port': port,
            'baudrate': int(self.baud_combo.currentText()),
            'bytesize': int(self.databits_combo.currentText()),
            'parity': self.parity_map[self.parity_combo.currentText()],
            'stopbits': self.stopbits_map[self.stopbits_combo.currentText()],
            'xonxoff': self.flowcontrol_combo.currentText() == "XON/XOFF",
            'rtscts': self.flowcontrol_combo.currentText() == "RTS/CTS",
            'dsrdtr': self.flowcontrol_combo.currentText() == "DSR/DTR"
        }

    def toggle_connection(self):
        if self.is_connected:
            self.stop_serial_worker.emit()
        else:
            try:
                settings = self._get_connection_settings()
                self.start_serial_worker.emit(settings)
                self.is_connected = True
                self.connect_btn.setText("Katkesta")
                self.connect_btn.setStyleSheet("background-color: #ffcdd2; color: black;")
                self.buffer = b"" # Tühjenda puhver uuel ühendusel
            except Exception as e: 
                QMessageBox.critical(self, "Viga", str(e))
                self.is_connected = False

    def check_buffer_timeout(self):
        if self.buffer and (time.time() - self.last_receive_time > self.packet_timeout):
            self.flush_buffer()

    @Slot(str)
    def _handle_serial_error(self, error_message):
        # See slott kutsutakse välja, kui worker saadab vea signaali
        # Kuna 'finished' signaal saadetakse alati, siis UI uuendamine toimub seal.
        self.log_to_screen(f"\n[SÜSTEEM] Viga: {error_message}", is_new=True, tag="sys")
        if self.auto_reconnect and not "Porti pole valitud" in error_message:
            self.start_reconnect_loop()

    @Slot()
    def _on_worker_finished(self):
        """Called when the serial worker thread's main task is finished."""
        self.is_connected = False
        self.connect_btn.setText("Ühenda")
        self.connect_btn.setStyleSheet("background-color: #e8f5e9; color: black;")

    def start_reconnect_loop(self):
        self.log_to_screen("\n[SÜSTEEM] Ootan pordi taastumist...", is_new=True, tag="sys")
        QTimer.singleShot(2000, self._try_reconnect)

    def _try_reconnect(self):
        if not self.auto_reconnect: return
        
        current_port = self.port_combo.currentText()
        if current_port in self.get_ports():
            try:
                settings = self._get_connection_settings()
                self.start_serial_worker.emit(settings)
                self.connect_btn.setText("Katkesta")
                self.is_connected = True
                self.connect_btn.setStyleSheet("background-color: #ffcdd2; color: black;")
                self.buffer = b""
                self.log_to_screen("\n[SÜSTEEM] Taasühendatud!", is_new=True, tag="sys")
                return
            except: 
                # Vaikne ebaõnnestumine, proovime varsti uuesti
                pass
        
        QTimer.singleShot(2000, self._try_reconnect)

    @Slot(bytes)
    def process_incoming_bytes(self, chunk):
        now = time.time()
        is_continuation = (now - self.last_receive_time) < self.packet_timeout
        self.last_receive_time = now

        if self.is_logging and self.log_file and self.binary_log:
            self.log_file.write(chunk) 
            self.log_file.flush()

        mode = self.mode_combo.currentText()
        if mode in ["HEX", "ASCII Table"]:
            self._handle_stream_mode(chunk, mode, is_continuation)
        else:
            self._handle_line_mode(chunk)

    def _handle_stream_mode(self, chunk, mode, is_continuation=False):
        if mode == "HEX":
            output = " ".join([f"{b:02X}" for b in chunk]) + " "
        else: # ASCII Table
            output = " ".join([f"{b:02X}:{(chr(b) if 32 <= b <= 126 else '.')}" for b in chunk]) + " "
        self.log_to_screen(output, is_new=not is_continuation)

    def _handle_line_mode(self, chunk):
        for byte in chunk:
            b = bytes([byte])
            if b == self.eol_char: self.flush_buffer()
            else: self.buffer += b
  
    def flush_buffer(self):
        if not self.buffer: return
        raw_text = self.buffer.decode('utf-8', errors='replace')
        raw_text = raw_text.replace('\x00', '') # Eemalda null-baidid, mis lõhuvad copy-paste
        if self.strip_incoming_text:
            raw_text = raw_text.strip('\r\n')
        self.buffer = b""
        mode = self.mode_combo.currentText()
        output, tag = self._format_by_mode(raw_text, mode)
        self.log_to_screen(output, is_new=True, tag=tag)
  
    def _format_by_mode(self, text, mode):
        if mode == "JSON":
            try:
                return "\n" + json.dumps(json.loads(text), indent=4), None
            except: pass
        elif mode == "CSV": 
            parts = text.replace(';', ',').split(',')
            if self.strip_incoming_text:
                return " | ".join([p.strip().ljust(12) for p in parts]), None
            else:
                return " | ".join([p.ljust(12) for p in parts]), None
        elif mode == "Modbus":
            hex_pts = [f"{b:02X}" for b in text.encode('utf-8', errors='replace')]
            if len(hex_pts) >= 3:
                return f"ID:{hex_pts[0]} | CMD:{hex_pts[1]} | DATA:{' '.join(hex_pts[2:-2])} | CRC:{''.join(hex_pts[-2:])}", "modbus"
        return text, None

    def log_to_screen(self, text, is_new=False, tag=None):
        # Logimine faili
        if self.is_logging and self.log_file and not self.binary_log:
            ts_str = ""
            if is_new:
                ts_str = "\n" + datetime.now().strftime("[%H:%M:%S.%f]"[:-3]) + "] "
            self.log_file.write(ts_str + text)
            self.log_file.flush()

        # Ekraanile kuvamine
        if not self.screen_paused:
            cursor = self.output_area.textCursor()
            cursor.movePosition(QTextCursor.End)
            
            # Ajatempel
            if is_new:
                if self.show_timestamps:
                    ts = "\n" + datetime.now().strftime("[%H:%M:%S.%f]"[:-3]) + "] "
                    fmt_ts = QTextCharFormat()
                    fmt_ts.setForeground(QColor("#888888")) # Hall
                    cursor.insertText(ts, fmt_ts)
                else:
                    cursor.insertText("\n")

            # Tekst ise
            fmt_text = QTextCharFormat()
            if tag == "sent": fmt_text.setForeground(QColor("#4da6ff")) # Sinine
            elif tag == "sys": fmt_text.setForeground(QColor("orange" if self.action_dark.isChecked() else "red"))
            elif tag == "modbus": fmt_text.setForeground(QColor("#afff00")) # Laimiroheline
            else: fmt_text.setForeground(QColor("#ffffff" if self.action_dark.isChecked() else "#000000"))
            
            cursor.insertText(text, fmt_text)
            
            self.output_area.setTextCursor(cursor)
            self.output_area.ensureCursorVisible()

    def send_command(self):
        if not self.is_connected: return
        cmd = self.input_field.text()
        if not cmd: return
        try:
            if self.send_as_hex:
                self.serial_manager.write(bytes.fromhex(cmd.replace(" ", "")))
                self.log_to_screen(f">> [HEX] {cmd}", is_new=True, tag="sent")
            else:
                self.serial_manager.write((cmd + "\r\n").encode('utf-8'))
                self.log_to_screen(f">> {cmd}", is_new=True, tag="sent")
            self.input_field.clear()
        except: QMessageBox.critical(self, "Viga", "Vale HEX formaat!")

    def send_macro(self, num):
        if not self.is_connected: return
        cmd = self.entry_m1.text() if num == 1 else self.entry_m2.text()
        if not cmd: return
        try:
            if self.send_as_hex:
                self.serial_manager.write(bytes.fromhex(cmd.replace(" ", "")))
                self.log_to_screen(f">> [HEX M{num}] {cmd}", is_new=True, tag="sent")
            else:
                self.serial_manager.write((cmd + "\r\n").encode('utf-8'))
                self.log_to_screen(f">> [M{num}] {cmd}", is_new=True, tag="sent")
        except: QMessageBox.critical(self, "Viga", "Vale HEX formaat!")

    def toggle_logging_file(self):
        # Kui kast oli just märgitud (olek True), siis küsime faili
        if self.cb_log.isChecked():
            fname, _ = QFileDialog.getSaveFileName(self, "Salvesta logi", "", "Text file (*.txt);;Binary file (*.bin)")
            if fname:
                if fname.lower().endswith(".bin"):
                    self.binary_log = True
                    self.log_file = open(fname, "ab")
                else:
                    self.binary_log = False
                    self.log_file = open(fname, "a", encoding="utf-8")
                self.is_logging = True
            else:
                self.cb_log.setChecked(False)
                self.is_logging = False
        else:
            # Kui kast võeti maha
            if self.log_file:
                self.log_file.close()
                self.log_file = None
            self.binary_log = False
            self.is_logging = False

    def copy_all(self):
        self.output_area.selectAll()
        self.output_area.copy()
        self.output_area.moveCursor(QTextCursor.End)

    def clear_screen(self):
        self.output_area.clear()

    def set_theme(self, theme_name):
        if theme_name == "Dark":
            self.setStyleSheet("""
                QMainWindow, QWidget { background-color: #2b2b2b; color: #e0e0e0; }
                QGroupBox { font-weight: bold; border: 1px solid #555; margin-top: 10px; }
                QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; padding: 0 3px; }
                QTextEdit { background-color: #1e1e1e; color: #ffffff; border: 1px solid #444; }
                QLineEdit, QComboBox { background-color: #333; color: #fff; border: 1px solid #555; }
                QMenu { background-color: #2b2b2b; color: #fff; border: 1px solid #555; }
                QMenu::item:selected { background-color: #444; }
                QMenuBar { background-color: #2b2b2b; color: #fff; }
                QMenuBar::item:selected { background-color: #444; }
                QPushButton { background-color: #444; color: #fff; border: 1px solid #555; padding: 4px; }
                QPushButton:hover { background-color: #555; }
                QCheckBox { spacing: 5px; color: #e0e0e0; }
                QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #aaa; background: #333; border-radius: 2px; }
                QCheckBox::indicator:checked { background: #4da6ff; border: 1px solid #4da6ff; image: url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24' fill='none' stroke='white' stroke-width='4' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='20 6 9 17 4 12'/%3E%3C/svg%3E"); }
                QCheckBox::indicator:hover { border-color: #fff; }
                QRadioButton { spacing: 5px; color: #e0e0e0; }
                QRadioButton::indicator { width: 14px; height: 14px; border: 1px solid #aaa; background: #333; border-radius: 7px; }
                QRadioButton::indicator:checked { background: #4da6ff; border: 1px solid #4da6ff; }
                QRadioButton::indicator:hover { border-color: #fff; }
            """)
        else:
            self.setStyleSheet("""
                QMainWindow, QWidget { background-color: #f0f0f0; color: #000000; }
                QTextEdit { background-color: #ffffff; color: #000000; border: 1px solid #ccc; }
                QLineEdit, QComboBox { background-color: #fff; color: #000; border: 1px solid #ccc; }
                QPushButton { background-color: #e0e0e0; color: #000; border: 1px solid #ccc; padding: 4px; }
                QPushButton:hover { background-color: #d0d0d0; }
                QCheckBox { spacing: 5px; color: #000; }
                QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #555; background: #fff; border-radius: 2px; }
                QCheckBox::indicator:checked { background: #0078d7; border: 1px solid #0078d7; image: url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24' fill='none' stroke='white' stroke-width='4' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='20 6 9 17 4 12'/%3E%3C/svg%3E"); }
                QCheckBox::indicator:hover { border-color: #000; }
                QRadioButton { spacing: 5px; color: #000; }
                QRadioButton::indicator { width: 14px; height: 14px; border: 1px solid #555; background: #fff; border-radius: 7px; }
                QRadioButton::indicator:checked { background: #0078d7; border: 1px solid #0078d7; }
                QRadioButton::indicator:hover { border-color: #000; }
            """)

    def set_packet_timeout(self, ms):
        self.packet_timeout = ms / 1000.0

    def ask_custom_timeout(self):
        current_ms = int(self.packet_timeout * 1000)
        val, ok = QInputDialog.getInt(self, "Custom Timeout", "Sisesta aeg (ms):", value=current_ms, minValue=10, maxValue=10000)
        if ok:
            self.set_packet_timeout(val)
            if val in self.timeout_actions:
                self.timeout_actions[val].setChecked(True)
        else:
            if current_ms in self.timeout_actions:
                self.timeout_actions[current_ms].setChecked(True)
            else:
                self.custom_timeout_action.setChecked(True)

    def show_help(self):
        help_text = """
        - Ühenduse seaded: Vali COM port, kiirus (Baud), andmebittide arv (Data Bits), paarsuskontroll (Parity), stop-bittide arv (Stop Bits) ja voo kontroll (Flow Control).
        - Ühenda: Ava või sule ühendus valitud pordiga.
        - Režiim: Tekst, HEX, JSON, CSV, Modbus, ASCII Table.
        - Ajatemplid: Näita iga uue paketi alguses kellaaega.
        - Logi faili: Salvesta sissetulev info faili (.txt või .bin).
        - Peata ekraan: Peata teksti lisamine terminali (logimine jätkub taustal).
        - Peata andmed: Lõpeta andmete lugemine serial puhvrist.
        - Auto-reconnect: Proovi ühendust taastada, kui see katkeb.
        - Eemalda reavahetus: Eemalda sissetuleva teksti algusest ja lõpust tühikud (sh reavahetused).
        - Packet Delimiter: Vali paketi lõpumärk (vaikimisi LF).
        - Packet Timeout: Määra aeg, millal loeme paketi lõppenuks (Custom: 10-10000 ms).
        - Saada HEX: Tõlgenda sisendit HEX koodidena (nt 41 42).
        """
        QMessageBox.information(self, "Abi", help_text)

    def show_credits(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Credits")
        dialog.setWindowIcon(QIcon(self.icon_path))
        dialog.setMinimumWidth(300)
        layout = QVBoxLayout(dialog)

        # Ikoon (keskel)
        lbl_icon = QLabel()
        pix = QPixmap(self.icon_path)
        if not pix.isNull():
            # Skaleerime pildi sobivaks (nt 128x128), säilitades proportsioonid
            pix = pix.scaled(128, 128, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            lbl_icon.setPixmap(pix)
        lbl_icon.setAlignment(Qt.AlignCenter)
        layout.addWidget(lbl_icon)

        # Tekst (keskel)
        lbl_text = QLabel("Fox Terminal v2.2\n\nKirjutatud Pythonis\nPyside6 raamistikuga\n© 2026 Fox")
        lbl_text.setAlignment(Qt.AlignCenter)
        lbl_text.setStyleSheet("font-size: 11pt; margin-top: 10px; margin-bottom: 10px;")
        layout.addWidget(lbl_text)

        # OK Nupp
        btn = QPushButton("OK")
        btn.clicked.connect(dialog.accept)
        btn.setFixedWidth(80)
        
        # Nupu tsentreerimine
        h_layout = QHBoxLayout()
        h_layout.addStretch()
        h_layout.addWidget(btn)
        h_layout.addStretch()
        layout.addLayout(h_layout)

        dialog.exec()
    
    def closeEvent(self, event):
        """Ensure resources are cleaned up on window close."""
        self.stop_serial_worker.emit()
        self.worker_thread.quit()
        self.worker_thread.wait() # Wait for thread to finish
        if self.log_file:
            self.log_file.close()
        event.accept()

    def force_disconnect(self):
        if self.is_connected:
            self.stop_serial_worker.emit()

if __name__ == "__main__":
    # See koodijupp tagab, et ikoon oleks nähtav ka Windowsi tegumiribal (Taskbar)
    try:
        import ctypes
        myappid = 'rein.fox.terminal.2.2'
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except:
        pass # Toimib ainult Windowsis
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(resource_path("favicon.ico")))
    terminal = UniversalTerminal()
    terminal.show()
    sys.exit(app.exec())