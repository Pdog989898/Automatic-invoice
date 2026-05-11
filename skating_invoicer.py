import json
import logging
import os
import secrets
import sqlite3
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

try:
    from PySide6.QtCore import QDate, QObject, QRunnable, Qt, QThreadPool, Signal
    from PySide6.QtGui import QColor, QFont
    from PySide6.QtWidgets import QApplication, QCheckBox, QColorDialog, QComboBox, QDateEdit, QDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea, QSlider, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget
    PYSIDE_AVAILABLE = True
except ImportError:
    PYSIDE_AVAILABLE = False

    class _MissingQt:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("PySide6 is required to run the desktop UI.")

    QApplication = QCheckBox = QColorDialog = QComboBox = QDateEdit = QDialog = QFrame = QHBoxLayout = QInputDialog = QLabel = QLineEdit = QListWidget = QMainWindow = QMenu = QMessageBox = QPushButton = QScrollArea = QSlider = QSpinBox = QTableWidget = QTableWidgetItem = QTabWidget = QVBoxLayout = QWidget = _MissingQt
    QDate = QObject = QRunnable = Qt = QThreadPool = QColor = QFont = _MissingQt

    class _FallbackSignal:
        def __init__(self, *args, **kwargs):
            self._slots = []

        def __get__(self, instance, owner):
            return self if instance is None else self

        def connect(self, slot):
            self._slots.append(slot)

        def emit(self, *args, **kwargs):
            for slot in list(self._slots):
                slot(*args, **kwargs)

    def Signal(*args, **kwargs):
        return _FallbackSignal(*args, **kwargs)

try:
    import bcrypt
except ImportError:
    bcrypt = None

try:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
except ImportError:
    letter = (612.0, 792.0)
    canvas = None

DB_FILE = "skating.db"
SETTINGS_FILE = "settings.json"
INVOICE_FOLDER = "invoices"
TEMPLATES_FOLDER = "templates"
CONFIG_FOLDER = "config"
AUTH_FILE = Path(CONFIG_FOLDER) / "auth.json"
SESSION_FILE = Path(CONFIG_FOLDER) / "session.dat"
LOG_FILE = Path(CONFIG_FOLDER) / "app.log"

try:
    Path(CONFIG_FOLDER).mkdir(exist_ok=True)
    logging.basicConfig(filename=LOG_FILE, level=logging.ERROR, format="%(asctime)s %(levelname)s %(message)s")
except OSError:
    logging.basicConfig(level=logging.ERROR, format="%(asctime)s %(levelname)s %(message)s")


def log_exception(context, exc):
    logging.error("%s: %s\n%s", context, exc, traceback.format_exc())
    print(f"[ERROR] {context}: {exc}", file=sys.stderr, flush=True)
    traceback.print_exc()


def debug_trace(message):
    print(f"[Skating Invoice Pro] {message}", flush=True)


def show_error(parent, title, message):
    if PYSIDE_AVAILABLE:
        QMessageBox.critical(parent, title, message)
    else:
        print(f"{title}: {message}", file=sys.stderr)


def atomic_write_text(path: Path, content: str):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

SECTION_LABELS = {
    "skater": "Skater Information",
    "period": "Invoice Period",
    "lessons": "Lesson Table",
    "total": "Total Section",
}

DEFAULT_INVOICE_TEMPLATE = {
    "name": "Default",
    "header_bg": "#7a4bd1",
    "header_text": "#ffffff",
    "title": "INVOICE",
    "header_height": 88,
    "title_font_size": 22,
    "body_font_size": 10,
    "section_spacing": 18,
    "section_order": ["skater", "period", "lessons", "total"],
}


def default_invoice_template():
    return json.loads(json.dumps(DEFAULT_INVOICE_TEMPLATE))


def normalize_invoice_template(template):
    def safe_int(value, fallback):
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    merged = default_invoice_template()
    if isinstance(template, dict):
        merged.update(template)
    valid_sections = [key for key in merged.get("section_order", []) if key in SECTION_LABELS]
    for key in DEFAULT_INVOICE_TEMPLATE["section_order"]:
        if key not in valid_sections:
            valid_sections.append(key)
    merged["section_order"] = valid_sections
    merged["header_height"] = max(56, min(150, safe_int(merged.get("header_height"), 88)))
    merged["title_font_size"] = max(14, min(36, safe_int(merged.get("title_font_size"), 22)))
    merged["body_font_size"] = max(8, min(16, safe_int(merged.get("body_font_size"), 10)))
    merged["section_spacing"] = max(8, min(40, safe_int(merged.get("section_spacing"), 18)))
    return merged


def hex_to_rgb(hex_color):
    value = (hex_color or "#000000").lstrip("#")
    if len(value) != 6:
        value = "000000"
    try:
        return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return (0, 0, 0)


class AuthService:
    def __init__(self, auth_file: Path = AUTH_FILE, session_file: Path = SESSION_FILE):
        self.auth_file = Path(auth_file)
        self.session_file = Path(session_file)

    def exists(self):
        return self.auth_file.exists()

    def is_configured(self):
        return bool(self.load().get("password_hash"))

    def load(self):
        if not self.auth_file.exists():
            return {}
        try:
            with self.auth_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            logging.error("Unable to read auth file", exc_info=True)
            return {}

    def save(self, data):
        safe = {
            "username": data.get("username", ""),
            "password_hash": data.get("password_hash", ""),
            "remember_me": bool(data.get("remember_me", False)),
            "session_token_hash": data.get("session_token_hash", ""),
        }
        try:
            atomic_write_text(self.auth_file, json.dumps(safe, indent=2))
        except OSError as exc:
            log_exception("Unable to save auth file", exc)
            raise RuntimeError("Authentication settings could not be saved.")

    def hash_secret(self, secret):
        if bcrypt is None:
            raise RuntimeError("bcrypt is required for local authentication.")
        return bcrypt.hashpw(secret.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    def verify_secret(self, secret, stored_hash):
        if bcrypt is None or not secret or not stored_hash:
            return False
        try:
            return bcrypt.checkpw(secret.encode("utf-8"), stored_hash.encode("utf-8"))
        except (TypeError, ValueError):
            return False

    def create_user(self, username, password, remember_me=False):
        data = {
            "username": (username or "").strip(),
            "password_hash": self.hash_secret(password),
            "remember_me": bool(remember_me),
            "session_token_hash": "",
        }
        if remember_me:
            self.start_session(data)
        else:
            self.clear_session_file()
        self.save(data)

    def authenticate(self, password, remember_me=False):
        data = self.load()
        if not self.verify_secret(password, data.get("password_hash", "")):
            return False
        data["remember_me"] = bool(remember_me)
        if remember_me:
            self.start_session(data)
        else:
            data["session_token_hash"] = ""
            self.clear_session_file()
        self.save(data)
        return True

    def start_session(self, data):
        token = secrets.token_urlsafe(48)
        data["session_token_hash"] = self.hash_secret(token)
        try:
            atomic_write_text(self.session_file, token)
            try:
                os.chmod(self.session_file, 0o600)
            except OSError:
                pass
        except OSError as exc:
            log_exception("Unable to save session token", exc)
            data["remember_me"] = False
            data["session_token_hash"] = ""

    def has_valid_session(self):
        try:
            data = self.load()
            debug_trace(f"Auth session check: configured={bool(data.get('password_hash'))}, remember_me={bool(data.get('remember_me'))}")
            if not data.get("remember_me"):
                return False
            try:
                token = self.session_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                debug_trace(f"Auth session token missing/unreadable: {exc}")
                return False
            valid = self.verify_secret(token, data.get("session_token_hash", ""))
            debug_trace(f"Auth session valid={valid}")
            return valid
        except Exception as exc:
            log_exception("Auth session validation failed", exc)
            return False

    def clear_session_file(self):
        try:
            self.session_file.unlink(missing_ok=True)
        except OSError as exc:
            log_exception("Unable to remove session token", exc)

    def username(self):
        return self.load().get("username", "")


class AuthDialog(QDialog):
    def __init__(self, auth_service: AuthService):
        super().__init__()
        self.auth_service = auth_service
        self.setup_mode = not self.auth_service.is_configured()
        debug_trace(f"AuthDialog initialized; setup_mode={self.setup_mode}")
        self.setWindowTitle("Skating Invoice Pro")
        self.setModal(True)
        self.setFixedSize(430, 460 if self.setup_mode else 390)
        self.build_ui()
        self.apply_auth_styles()

    def build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(34, 34, 34, 34)
        root.setSpacing(16)

        title = QLabel("Create Local Password" if self.setup_mode else "Welcome Back")
        title.setObjectName("authTitle")
        subtitle = QLabel("Set up secure local access before Skating Invoice Pro opens." if self.setup_mode else "Enter your local password to unlock Skating Invoice Pro.")
        subtitle.setObjectName("authSubtitle")
        subtitle.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(subtitle)

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Username (optional)")
        if self.setup_mode:
            root.addWidget(self.username_input)
        else:
            username = self.auth_service.username()
            user_label = QLabel(username or "Local user")
            user_label.setObjectName("authUser")
            root.addWidget(user_label)

        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Password")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.returnPressed.connect(self.submit)
        root.addWidget(self.password_input)

        self.confirm_input = QLineEdit()
        self.confirm_input.setPlaceholderText("Confirm password")
        self.confirm_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm_input.returnPressed.connect(self.submit)
        if self.setup_mode:
            root.addWidget(self.confirm_input)

        self.stay_logged_in = QCheckBox("Stay logged in on this computer")
        root.addWidget(self.stay_logged_in)

        self.error_label = QLabel("")
        self.error_label.setObjectName("authError")
        self.error_label.setWordWrap(True)
        root.addWidget(self.error_label)
        root.addStretch(1)

        self.submit_btn = QPushButton("Save Password" if self.setup_mode else "Unlock")
        self.submit_btn.setMinimumHeight(44)
        self.submit_btn.clicked.connect(self.submit)
        root.addWidget(self.submit_btn)

    def apply_auth_styles(self):
        self.setFont(QFont("Segoe UI", 10))
        self.setStyleSheet("""
            QDialog { background: #f8fafc; color: #0f172a; }
            QLabel { background: transparent; }
            #authTitle { font-size: 24px; font-weight: 700; color: #0f172a; }
            #authSubtitle { color: #64748b; line-height: 140%; }
            #authUser { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 12px; font-weight: 600; }
            #authError { color: #dc2626; min-height: 34px; }
            QLineEdit { background: #ffffff; border: 1px solid #cbd5e1; border-radius: 10px; padding: 12px; }
            QLineEdit:focus { border: 1px solid #4f46e5; }
            QPushButton { background: #4f46e5; color: white; border: none; border-radius: 10px; padding: 12px 14px; font-weight: 600; }
            QPushButton:hover { background: #4338ca; }
            QCheckBox { spacing: 10px; padding: 6px; }
        """)

    def submit(self):
        debug_trace(f"AuthDialog submit; setup_mode={self.setup_mode}, remember={self.stay_logged_in.isChecked()}")
        password = self.password_input.text()
        if self.setup_mode:
            username = self.username_input.text().strip()
            confirm = self.confirm_input.text()
            if len(password) < 8:
                debug_trace("Setup rejected: password too short")
                self.show_error("Use at least 8 characters for the local password.")
                return
            if password != confirm:
                debug_trace("Setup rejected: confirmation mismatch")
                self.show_error("Passwords do not match.")
                return
            try:
                self.auth_service.create_user(username, password, self.stay_logged_in.isChecked())
            except RuntimeError as exc:
                debug_trace(f"Setup failed: {exc}")
                self.show_error(str(exc))
                return
            debug_trace("Setup accepted")
            self.accept()
            return

        if not password:
            debug_trace("Login rejected: empty password")
            self.show_error("Enter your password.")
            return
        try:
            authenticated = self.auth_service.authenticate(password, self.stay_logged_in.isChecked())
        except RuntimeError as exc:
            debug_trace(f"Login failed: {exc}")
            self.show_error(str(exc))
            return
        if authenticated:
            debug_trace("Login accepted")
            self.accept()
        else:
            self.password_input.clear()
            debug_trace("Login rejected: incorrect password")
            self.show_error("Incorrect password. Please try again.")

    def show_error(self, message):
        self.error_label.setText(message)


class AccountSettingsDialog(QDialog):
    def __init__(self, auth_service: AuthService, parent=None):
        super().__init__(parent)
        self.auth_service = auth_service
        self.setWindowTitle("Account Settings")
        self.setModal(True)
        self.setFixedSize(420, 280)
        self.build_ui()
        self.apply_account_styles()

    def build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 28, 28, 28)
        root.setSpacing(14)

        title = QLabel("Account Settings")
        title.setObjectName("accountTitle")
        root.addWidget(title)

        self.username_input = QLineEdit(self.auth_service.username())
        self.username_input.setPlaceholderText("Username")
        root.addWidget(self.username_input)

        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("New password")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.returnPressed.connect(self.save_account)
        root.addWidget(self.password_input)

        self.error_label = QLabel("")
        self.error_label.setObjectName("accountError")
        self.error_label.setWordWrap(True)
        root.addWidget(self.error_label)
        root.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("secondary")
        cancel_btn.clicked.connect(self.reject)
        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self.save_account)
        buttons.addWidget(cancel_btn)
        buttons.addWidget(self.save_btn)
        root.addLayout(buttons)

    def apply_account_styles(self):
        self.setFont(QFont("Segoe UI", 10))
        self.setStyleSheet("""
            QDialog { background: #f8fafc; color: #0f172a; }
            QLabel { background: transparent; }
            #accountTitle { font-size: 22px; font-weight: 700; color: #0f172a; }
            #accountError { color: #dc2626; min-height: 28px; }
            QLineEdit { background: #ffffff; border: 1px solid #cbd5e1; border-radius: 10px; padding: 10px; }
            QLineEdit:focus { border: 1px solid #4f46e5; }
            QPushButton { background: #4f46e5; color: white; border: none; border-radius: 10px; padding: 10px 16px; font-weight: 600; }
            QPushButton:hover { background: #4338ca; }
            QPushButton#secondary { background: #ffffff; color: #334155; border: 1px solid #cbd5e1; }
            QPushButton#secondary:hover { background: #f1f5f9; }
        """)

    def save_account(self):
        username = self.username_input.text().strip()
        password = self.password_input.text()
        if not username:
            self.error_label.setText("Enter a username.")
            return
        if not password:
            self.error_label.setText("Enter a password.")
            return
        if len(password) < 8:
            self.error_label.setText("Use at least 8 characters for the password.")
            return
        try:
            data = self.auth_service.load()
            data["username"] = username
            data["password_hash"] = self.auth_service.hash_secret(password)
            self.auth_service.save(data)
        except RuntimeError as exc:
            self.error_label.setText(str(exc))
            return
        self.accept()


class SettingsManager:
    DEFAULTS = {"header_bg": "#7a4bd1", "header_text": "#ffffff", "title": "INVOICE", "from_name": "", "from_email": "", "invoice_month": "", "invoice_year": "", "active_invoice_template": "", "invoice_template": default_invoice_template()}

    def __init__(self, settings_file: str = SETTINGS_FILE):
        self.settings_file = Path(settings_file)

    def load(self) -> dict:
        if not self.settings_file.exists():
            return dict(self.DEFAULTS)
        try:
            with self.settings_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
            merged = dict(self.DEFAULTS)
            merged.update(data or {})
            return merged
        except (OSError, json.JSONDecodeError):
            logging.error("Unable to read settings file", exc_info=True)
            return dict(self.DEFAULTS)

    def save(self, data: dict) -> None:
        merged = dict(self.DEFAULTS)
        merged.update(data)
        try:
            atomic_write_text(self.settings_file, json.dumps(merged, indent=2))
        except OSError as exc:
            log_exception("Unable to save settings file", exc)


class Database:
    def __init__(self, db_file: str = DB_FILE):
        self.conn = sqlite3.connect(db_file, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.init()

    def init(self):
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS skaters(id INTEGER PRIMARY KEY,name TEXT NOT NULL,email TEXT,paid INTEGER DEFAULT 0)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS lesson_types(id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE,rate REAL NOT NULL CHECK(rate >= 0))""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS lessons(id INTEGER PRIMARY KEY,skater_id INTEGER NOT NULL,lesson_type_id INTEGER NOT NULL,date TEXT NOT NULL,duration REAL NOT NULL CHECK(duration > 0),FOREIGN KEY(skater_id) REFERENCES skaters(id) ON DELETE CASCADE,FOREIGN KEY(lesson_type_id) REFERENCES lesson_types(id) ON DELETE RESTRICT)""")
        self.conn.commit()

    def fetch(self, q, p=()):
        return [dict(r) for r in self.conn.execute(q, p)]

    def run(self, q, p=()):
        self.conn.execute(q, p)
        self.conn.commit()


class FinanceService:
    def __init__(self, db: Database):
        self.db = db

    def totals(self):
        rows = self.db.fetch("""SELECT s.id, s.paid, COALESCE(SUM(l.duration * t.rate), 0) AS total FROM skaters s LEFT JOIN lessons l ON l.skater_id = s.id LEFT JOIN lesson_types t ON t.id = l.lesson_type_id GROUP BY s.id, s.paid""")
        expected = sum(float(r["total"]) for r in rows)
        collected = sum(float(r["total"]) for r in rows if r["paid"])
        return expected, collected


class WorkerSignals(QObject):
    finished = Signal(str)
    error = Signal(str)


class InvoiceWorker(QRunnable):
    def __init__(self, settings: dict, skater: dict, month: int, year: int, invoice_template=None):
        super().__init__()
        self.settings = settings
        self.skater = skater
        self.month = month
        self.year = year
        self.invoice_template = normalize_invoice_template(invoice_template or settings.get("invoice_template"))
        self.signals = WorkerSignals()

    def run(self):
        conn = None
        try:
            conn = sqlite3.connect(DB_FILE, timeout=15)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            lessons = cursor.execute(
                """
                SELECT l.date, l.duration, t.rate, t.name
                FROM lessons l
                JOIN lesson_types t ON l.lesson_type_id = t.id
                WHERE l.skater_id = ?
                ORDER BY l.date
                """,
                (self.skater["id"],)
            ).fetchall()

            os.makedirs(INVOICE_FOLDER, exist_ok=True)
            file = os.path.join(
                INVOICE_FOLDER,
                f"{self.skater['name'].replace('/', '-')}_{self.year}_{self.month:02d}.pdf"
            )

            c = canvas.Canvas(file, pagesize=letter)
            w, h = letter
            period = datetime(self.year, self.month, 1).strftime("%B %Y")
            margin = 48
            table_left = margin
            table_right = w - margin
            accent = hex_to_rgb(self.invoice_template["header_bg"])
            header_text = hex_to_rgb(self.invoice_template["header_text"])
            header_height = self.invoice_template["header_height"]
            title_font_size = self.invoice_template["title_font_size"]
            body_font_size = self.invoice_template["body_font_size"]
            section_spacing = self.invoice_template["section_spacing"]
            section_order = self.invoice_template["section_order"]
            muted = (0.39, 0.45, 0.55)
            dark = (0.06, 0.09, 0.16)
            line = (0.88, 0.91, 0.95)
            soft = (0.97, 0.98, 0.99)
            col_x = [table_left, table_left + 86, table_left + 252, table_left + 344, table_left + 424]

            def draw_text(text, x, y_pos, size=10, color=dark, font="Helvetica"):
                c.setFont(font, size)
                c.setFillColorRGB(*color)
                c.drawString(x, y_pos, str(text))

            def draw_right(text, x, y_pos, size=10, color=dark, font="Helvetica"):
                c.setFont(font, size)
                c.setFillColorRGB(*color)
                c.drawRightString(x, y_pos, str(text))

            def draw_page_header():
                c.setFillColorRGB(*accent)
                c.rect(0, h - header_height, w, header_height, fill=1, stroke=0)
                c.setFillColorRGB(*header_text)
                c.setFont("Helvetica-Bold", title_font_size)
                c.drawString(margin, h - max(34, header_height / 2 + 5), self.invoice_template.get("title") or "INVOICE")
                c.setFont("Helvetica", 9)
                c.drawRightString(w - margin, h - max(32, header_height / 2), "Skating Invoice Pro")
                c.drawRightString(w - margin, h - max(50, header_height / 2 + 18), f"Generated {datetime.now().strftime('%B %d, %Y')}")

            def draw_table_header(y_pos):
                c.setFillColorRGB(*soft)
                c.roundRect(table_left, y_pos - 18, table_right - table_left, 28, 6, fill=1, stroke=0)
                draw_text("Date", col_x[0] + 10, y_pos - 7, 8, muted, "Helvetica-Bold")
                draw_text("Lesson Type", col_x[1], y_pos - 7, 8, muted, "Helvetica-Bold")
                draw_right("Duration", col_x[3] - 16, y_pos - 7, 8, muted, "Helvetica-Bold")
                draw_right("Rate", col_x[4] - 16, y_pos - 7, 8, muted, "Helvetica-Bold")
                draw_right("Amount", table_right - 10, y_pos - 7, 8, muted, "Helvetica-Bold")

            def new_page(include_table_header=False):
                draw_page_header()
                top = h - header_height - 36
                if include_table_header:
                    draw_table_header(top)
                    return top - 28
                return top

            draw_page_header()
            y = h - header_height - 36

            total = 0.0
            for row in lessons:
                total += float(row["duration"]) * float(row["rate"])

            def ensure_space(required):
                nonlocal y
                if y - required < 56:
                    c.showPage()
                    y = new_page()

            def draw_card(x, y_pos, width, title, primary, secondary):
                c.setFillColorRGB(1, 1, 1)
                c.roundRect(x, y_pos - 72, width, 72, 8, fill=1, stroke=0)
                c.setStrokeColorRGB(*line)
                c.roundRect(x, y_pos - 72, width, 72, 8, fill=0, stroke=1)
                draw_text(title, x + 14, y_pos - 22, 8, muted, "Helvetica-Bold")
                draw_text(primary, x + 14, y_pos - 42, body_font_size + 3, dark, "Helvetica-Bold")
                draw_text(secondary, x + 14, y_pos - 58, max(8, body_font_size - 1), muted)

            for section in section_order:
                if section == "skater":
                    ensure_space(72 + section_spacing)
                    draw_card(margin, y, table_right - table_left, "Billed To", self.skater["name"], self.skater.get("email") or "No email on file")
                    y -= 72 + section_spacing
                elif section == "period":
                    ensure_space(72 + section_spacing)
                    sender = self.settings.get("from_name") or "Sender"
                    email = self.settings.get("from_email") or "No email provided"
                    draw_card(margin, y, table_right - table_left, "Invoice Period", period, f"{sender} | {email}")
                    y -= 72 + section_spacing
                elif section == "lessons":
                    ensure_space(56 + section_spacing)
                    draw_table_header(y)
                    y -= 28
                    for row in lessons:
                        amount = float(row["duration"]) * float(row["rate"])
                        if y < 92:
                            c.showPage()
                            y = new_page(True)
                        c.setStrokeColorRGB(*line)
                        c.line(table_left, y - 11, table_right, y - 11)
                        draw_text(row["date"], col_x[0] + 10, y, body_font_size - 1)
                        draw_text(row["name"], col_x[1], y, body_font_size - 1)
                        draw_right(f"{float(row['duration']):.2f} h", col_x[3] - 16, y, body_font_size - 1)
                        draw_right(f"${float(row['rate']):.2f}", col_x[4] - 16, y, body_font_size - 1)
                        draw_right(f"${amount:.2f}", table_right - 10, y, body_font_size - 1, dark, "Helvetica-Bold")
                        y -= body_font_size + 16
                    y -= section_spacing
                elif section == "total":
                    ensure_space(58 + section_spacing)
                    c.setFillColorRGB(*accent)
                    c.roundRect(table_right - 214, y - 48, 214, 48, 8, fill=1, stroke=0)
                    c.setFillColorRGB(*header_text)
                    c.setFont("Helvetica", 9)
                    c.drawString(table_right - 196, y - 19, "Total Due")
                    c.setFont("Helvetica-Bold", 18)
                    c.drawRightString(table_right - 18, y - 22, f"${total:.2f}")
                    y -= 48 + section_spacing

            c.save()

            self.signals.finished.emit(file)

        except Exception as exc:
            log_exception("Invoice generation failed", exc)
            self.signals.error.emit(str(exc))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error as exc:
                    log_exception("Unable to close invoice worker SQLite connection", exc)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        debug_trace("MainWindow __init__ started")
        self.setWindowTitle("Skating Invoice Pro")
        self.resize(1260, 780)

        os.makedirs(INVOICE_FOLDER, exist_ok=True)
        os.makedirs(TEMPLATES_FOLDER, exist_ok=True)
        self.db = Database()
        self.settings_manager = SettingsManager()
        self.settings = self.settings_manager.load()
        self.invoice_template = self.load_startup_invoice_template()
        self.finance_service = FinanceService(self.db)
        self.selected = None
        self.skaters = []
        self.lesson_types = []
        self.thread_pool = QThreadPool.globalInstance()

        self.build_ui()
        self.apply_styles()
        self.refresh_all()
        debug_trace("MainWindow __init__ finished")

    def card(self, object_name="card"):
        f = QFrame()
        f.setObjectName(object_name)
        return f

    def build_ui(self):
        self.auth_service = AuthService()
        self.current_user = {"username": self.auth_service.username() or "Local user", "is_guest": False}

        root = QWidget()
        self.setCentralWidget(root)
        shell = QHBoxLayout(root)
        shell.setContentsMargins(16, 16, 16, 16)
        shell.setSpacing(16)

        self.sidebar = self.card("sidebar")
        self.sidebar.setFixedWidth(270)
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(16, 16, 16, 16)
        side.setSpacing(12)

        title = QLabel("Skating Invoice Pro")
        title.setObjectName("sidebarTitle")
        side.addWidget(title)
        sec = QLabel("Skaters")
        sec.setObjectName("sectionLabel")
        side.addWidget(sec)

        self.skater_combo = QComboBox()
        self.skater_combo.currentIndexChanged.connect(self.select_skater)
        side.addWidget(self.skater_combo)

        self.add_skater_btn = QPushButton("Add Skater")
        self.add_skater_btn.clicked.connect(self.add_skater)
        side.addWidget(self.add_skater_btn)

        self.edit_skater_btn = QPushButton("Edit Skater")
        self.edit_skater_btn.setObjectName("secondary")
        self.edit_skater_btn.clicked.connect(self.edit_skater)
        side.addWidget(self.edit_skater_btn)

        self.manage_types_btn = QPushButton("Manage Lesson Types")
        self.manage_types_btn.setObjectName("secondary")
        self.manage_types_btn.clicked.connect(self.manage_lesson_types)
        side.addWidget(self.manage_types_btn)
        side.addStretch(1)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.build_lessons_tab(), "Lessons")
        self.tabs.addTab(self.build_invoices_tab(), "Invoices")
        self.tabs.addTab(self.build_invoice_editor_tab(), "Invoice Editor")
        self.tabs.addTab(self.build_finance_tab(), "Finance")
        
        shell.addWidget(self.sidebar)
        shell.addWidget(self.tabs, 1)
        
        self.profile_btn = QPushButton(self.profile_avatar_text(), self)
        self.profile_btn.setFixedSize(40, 40)
        self.profile_btn.setObjectName("profileButton")
        self.profile_btn.setToolTip("Profile")
        self.profile_btn.setStyleSheet("""
            QPushButton#profileButton {
                background: #4f46e5;
                color: #ffffff;
                border: 2px solid #ffffff;
                border-radius: 20px;
                font-size: 15px;
                font-weight: 700;
                padding: 0;
            }
            QPushButton#profileButton:hover { background: #4338ca; }
            QPushButton#profileButton:pressed { background: #3730a3; }
        """)
        self.profile_btn.clicked.connect(self.show_profile_menu)
        self.position_profile_button()
        self.profile_btn.raise_()
    
    def profile_avatar_text(self):
        if getattr(self, "current_user", {}).get("is_guest"):
            return "G"
        username = getattr(self, "current_user", {}).get("username", "")
        return (username[:1] or "P").upper()

    def position_profile_button(self):
        if hasattr(self, "profile_btn"):
            margin = 22
            self.profile_btn.move(self.width() - self.profile_btn.width() - margin, margin)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.position_profile_button()

    def show_profile_menu(self):
        menu = QMenu(self)
        menu.setObjectName("profileMenu")
        menu.setStyleSheet("""
            QMenu#profileMenu {
                background: #ffffff;
                border: 1px solid #e2e8f0;
                border-radius: 12px;
                padding: 8px;
                color: #0f172a;
            }
            QMenu#profileMenu::item {
                border-radius: 8px;
                padding: 9px 30px 9px 14px;
                margin: 2px;
            }
            QMenu#profileMenu::item:selected {
                background: #eef2ff;
                color: #3730a3;
            }
            QMenu#profileMenu::separator {
                height: 1px;
                background: #e2e8f0;
                margin: 6px 4px;
            }
        """)
        account_settings = menu.addAction("Account Settings")
        guest_account = menu.addAction("Use Guest Account")
        menu.addSeparator()
        log_out = menu.addAction("Log Out")

        account_settings.triggered.connect(self.open_account_settings)
        guest_account.triggered.connect(self.switch_to_guest)
        log_out.triggered.connect(self.logout_user)

        menu.exec(self.profile_btn.mapToGlobal(self.profile_btn.rect().bottomLeft()))

    def open_account_settings(self):
        dialog = AccountSettingsDialog(self.auth_service, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.current_user = {"username": self.auth_service.username() or "Local user", "is_guest": False}
            self.profile_btn.setText(self.profile_avatar_text())
            QMessageBox.information(self, "Account Settings", "Account settings saved.")

    def switch_to_guest(self):
        self.clear_user_session()
        self.current_user = {"username": "Guest", "is_guest": True}
        self.selected = None
        if hasattr(self, "skater_combo"):
            self.skater_combo.setCurrentIndex(-1)
        if hasattr(self, "lesson_table"):
            self.lesson_table.setRowCount(0)
        if hasattr(self, "profile_btn"):
            self.profile_btn.setText(self.profile_avatar_text())
        self.settings["guest_mode"] = True
        self.settings_manager.save(self.settings)
        debug_trace("[Skating Invoice Pro] Switched to guest account")
        QMessageBox.information(self, "Guest Account", "Guest account mode is now active.")

    def logout_user(self):
        self.clear_user_session()
        self.current_user = {"username": "", "is_guest": False}
        self.selected = None
        self.hide()

        auth_dialog = AuthDialog(self.auth_service)
        if auth_dialog.exec() == QDialog.DialogCode.Accepted:
            self.current_user = {"username": self.auth_service.username() or "Local user", "is_guest": False}
            self.settings["guest_mode"] = False
            self.settings_manager.save(self.settings)
            self.profile_btn.setText(self.profile_avatar_text())
            self.refresh_all()
            self.show()
            debug_trace("[Skating Invoice Pro] User logged back in")
            return

        debug_trace("[Skating Invoice Pro] Logout completed; login canceled")
        self.close()

    def clear_user_session(self):
        data = self.auth_service.load()
        if data:
            data["remember_me"] = False
            data["session_token_hash"] = ""
            self.auth_service.save(data)
        self.auth_service.clear_session_file()
        self.settings["guest_mode"] = False
    
    def build_lessons_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(12)

        input_card = self.card()
        ic = QVBoxLayout(input_card)
        ic.setContentsMargins(16, 16, 16, 16)
        ic.setSpacing(12)

        row = QHBoxLayout()
        row.setSpacing(12)
        self.lesson_type_combo = QComboBox()
        self.lesson_date = QDateEdit(calendarPopup=True)
        self.lesson_date.setDisplayFormat("yyyy-MM-dd")
        self.lesson_date.setDate(QDate.currentDate())
        self.lesson_duration = QLineEdit()
        self.lesson_duration.setPlaceholderText("Duration in hours")
        row.addWidget(self.lesson_type_combo)
        row.addWidget(self.lesson_date)
        row.addWidget(self.lesson_duration)
        ic.addWidget(QLabel("Lesson Entry"))
        ic.addLayout(row)
        self.add_lesson_btn = QPushButton("Add Lesson")
        self.add_lesson_btn.setObjectName("success")
        self.add_lesson_btn.clicked.connect(self.add_lesson)
        ic.addWidget(self.add_lesson_btn)

        history_card = self.card()
        hc = QVBoxLayout(history_card)
        hc.setContentsMargins(16, 16, 16, 16)
        hc.setSpacing(12)
        hc.addWidget(QLabel("Lesson History"))
        self.lesson_table = QTableWidget(0, 4)
        self.lesson_table.setHorizontalHeaderLabels(["Date", "Lesson Type", "Duration", "Amount"])
        self.lesson_table.horizontalHeader().setStretchLastSection(True)
        self.lesson_table.setAlternatingRowColors(True)
        self.lesson_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.lesson_table.setSelectionBehavior(QTableWidget.SelectRows)
        hc.addWidget(self.lesson_table)

        lay.addWidget(input_card)
        lay.addWidget(history_card, 1)
        return tab

    def build_invoices_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(16)

        heading = QLabel("Invoice Center")
        heading.setObjectName("pageTitle")
        subheading = QLabel("Prepare sender details, choose a billing period, and generate a polished PDF invoice for the selected skater.")
        subheading.setObjectName("pageSubtitle")
        lay.addWidget(heading)
        lay.addWidget(subheading)

        row = QHBoxLayout()
        row.setSpacing(16)

        def field(label, widget):
            group = QVBoxLayout()
            group.setSpacing(6)
            text = QLabel(label)
            text.setObjectName("fieldLabel")
            group.addWidget(text)
            group.addWidget(widget)
            return group

        sender = self.card()
        s = QVBoxLayout(sender)
        s.setContentsMargins(20, 20, 20, 20)
        s.setSpacing(14)
        sender_title = QLabel("Sender Information")
        sender_title.setObjectName("cardTitle")
        sender_hint = QLabel("Shown in the invoice summary and PDF header details.")
        sender_hint.setObjectName("cardHint")
        s.addWidget(sender_title)
        s.addWidget(sender_hint)
        self.from_name = QLineEdit(self.settings.get("from_name", ""))
        self.from_name.setPlaceholderText("Your name or business")
        self.from_email = QLineEdit(self.settings.get("from_email", ""))
        self.from_email.setPlaceholderText("billing@example.com")
        s.addLayout(field("From Name", self.from_name))
        s.addLayout(field("From Email", self.from_email))
        s.addStretch(1)

        period = self.card()
        p = QVBoxLayout(period)
        p.setContentsMargins(20, 20, 20, 20)
        p.setSpacing(14)
        period_title = QLabel("Invoice Period")
        period_title.setObjectName("cardTitle")
        period_hint = QLabel("Used for the PDF file name and visible invoice period.")
        period_hint.setObjectName("cardHint")
        p.addWidget(period_title)
        p.addWidget(period_hint)
        self.invoice_month = QLineEdit(self.settings.get("invoice_month", ""))
        self.invoice_month.setPlaceholderText("1-12")
        self.invoice_year = QLineEdit(self.settings.get("invoice_year", ""))
        self.invoice_year.setPlaceholderText("YYYY")
        period_fields = QHBoxLayout()
        period_fields.setSpacing(12)
        period_fields.addLayout(field("Month", self.invoice_month))
        period_fields.addLayout(field("Year", self.invoice_year))
        p.addLayout(period_fields)
        p.addStretch(1)

        action = self.card()
        a = QVBoxLayout(action)
        a.setContentsMargins(20, 20, 20, 20)
        a.setSpacing(14)
        action_title = QLabel("Actions")
        action_title.setObjectName("cardTitle")
        action_hint = QLabel("Invoices are generated in the background so the app stays responsive.")
        action_hint.setObjectName("cardHint")
        a.addWidget(action_title)
        a.addWidget(action_hint)
        self.invoice_status = QLabel("Ready to generate")
        self.invoice_status.setObjectName("cardHint")
        self.generate_btn = QPushButton("Generate Invoice")
        self.generate_btn.setMinimumHeight(42)
        self.generate_btn.clicked.connect(self.generate_invoice)
        self.open_folder_btn = QPushButton("Open Invoices Folder")
        self.open_folder_btn.setObjectName("secondary")
        self.open_folder_btn.setMinimumHeight(42)
        self.open_folder_btn.clicked.connect(self.open_folder)
        a.addStretch(1)
        a.addWidget(self.invoice_status)
        a.addWidget(self.generate_btn)
        a.addWidget(self.open_folder_btn)

        row.addWidget(sender, 2)
        row.addWidget(period, 2)
        row.addWidget(action, 1)
        lay.addLayout(row)
        lay.addStretch(1)
        return tab

    def build_invoice_editor_tab(self):
        tab = QWidget()
        root = QHBoxLayout(tab)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(16)

        preview_shell = self.card()
        preview_layout = QVBoxLayout(preview_shell)
        preview_layout.setContentsMargins(20, 20, 20, 20)
        preview_layout.setSpacing(14)
        title = QLabel("Live Invoice Preview")
        title.setObjectName("pageTitle")
        subtitle = QLabel("A structured invoice layout preview using the same template settings as PDF export.")
        subtitle.setObjectName("pageSubtitle")
        preview_layout.addWidget(title)
        preview_layout.addWidget(subtitle)

        preview_scroll = QScrollArea()
        preview_scroll.setWidgetResizable(True)
        preview_scroll.setObjectName("previewScroll")
        self.invoice_preview_page = QFrame()
        self.invoice_preview_page.setObjectName("invoicePreviewPage")
        self.invoice_preview_layout = QVBoxLayout(self.invoice_preview_page)
        self.invoice_preview_layout.setContentsMargins(0, 0, 0, 0)
        self.invoice_preview_layout.setSpacing(0)
        preview_scroll.setWidget(self.invoice_preview_page)
        preview_layout.addWidget(preview_scroll, 1)

        controls = self.card()
        controls.setFixedWidth(340)
        control_layout = QVBoxLayout(controls)
        control_layout.setContentsMargins(18, 18, 18, 18)
        control_layout.setSpacing(12)

        control_title = QLabel("Template Controls")
        control_title.setObjectName("cardTitle")
        control_layout.addWidget(control_title)

        self.template_combo = QComboBox()
        self.template_combo.currentTextChanged.connect(self.load_selected_template)
        control_layout.addLayout(self.editor_field("Saved Template", self.template_combo))

        template_buttons = QHBoxLayout()
        template_buttons.setSpacing(10)
        self.save_template_btn = QPushButton("Save Template")
        self.save_template_btn.clicked.connect(self.save_current_template)
        self.load_template_btn = QPushButton("Load")
        self.load_template_btn.setObjectName("secondary")
        self.load_template_btn.clicked.connect(lambda: self.load_selected_template(self.template_combo.currentText()))
        template_buttons.addWidget(self.save_template_btn)
        template_buttons.addWidget(self.load_template_btn)
        control_layout.addLayout(template_buttons)

        self.editor_title_input = QLineEdit(self.invoice_template["title"])
        self.editor_title_input.textChanged.connect(self.update_invoice_template_from_controls)
        control_layout.addLayout(self.editor_field("Invoice Title", self.editor_title_input))

        color_row = QHBoxLayout()
        color_row.setSpacing(10)
        self.header_bg_btn = QPushButton(self.invoice_template["header_bg"])
        self.header_bg_btn.clicked.connect(lambda: self.pick_template_color("header_bg"))
        self.header_text_btn = QPushButton(self.invoice_template["header_text"])
        self.header_text_btn.setObjectName("secondary")
        self.header_text_btn.clicked.connect(lambda: self.pick_template_color("header_text"))
        color_row.addLayout(self.editor_field("Header Color", self.header_bg_btn))
        color_row.addLayout(self.editor_field("Text Color", self.header_text_btn))
        control_layout.addLayout(color_row)

        self.header_height_slider = self.editor_slider(56, 150, self.invoice_template["header_height"], self.update_invoice_template_from_controls)
        control_layout.addLayout(self.editor_field("Header Height", self.header_height_slider))

        size_row = QHBoxLayout()
        size_row.setSpacing(10)
        self.title_font_size_spin = self.editor_spin(14, 36, self.invoice_template["title_font_size"])
        self.body_font_size_spin = self.editor_spin(8, 16, self.invoice_template["body_font_size"])
        size_row.addLayout(self.editor_field("Title Size", self.title_font_size_spin))
        size_row.addLayout(self.editor_field("Body Size", self.body_font_size_spin))
        control_layout.addLayout(size_row)

        self.section_spacing_spin = self.editor_spin(8, 40, self.invoice_template["section_spacing"])
        control_layout.addLayout(self.editor_field("Section Spacing", self.section_spacing_spin))

        order_label = QLabel("Section Order")
        order_label.setObjectName("fieldLabel")
        control_layout.addWidget(order_label)
        self.section_order_list = QListWidget()
        self.section_order_list.setObjectName("sectionOrderList")
        self.section_order_list.setFixedHeight(132)
        control_layout.addWidget(self.section_order_list)

        order_buttons = QHBoxLayout()
        order_buttons.setSpacing(10)
        up_btn = QPushButton("Move Up")
        up_btn.setObjectName("secondary")
        up_btn.clicked.connect(lambda: self.move_invoice_section(-1))
        down_btn = QPushButton("Move Down")
        down_btn.setObjectName("secondary")
        down_btn.clicked.connect(lambda: self.move_invoice_section(1))
        order_buttons.addWidget(up_btn)
        order_buttons.addWidget(down_btn)
        control_layout.addLayout(order_buttons)

        export_btn = QPushButton("Generate PDF")
        export_btn.clicked.connect(self.generate_invoice)
        control_layout.addWidget(export_btn)
        control_layout.addStretch(1)

        root.addWidget(preview_shell, 1)
        root.addWidget(controls)

        for source in (self.from_name, self.from_email, self.invoice_month, self.invoice_year):
            source.textChanged.connect(self.update_invoice_preview)
        self.refresh_template_combo()
        self.sync_editor_controls()
        self.update_invoice_preview()
        return tab

    def editor_field(self, label, widget):
        layout = QVBoxLayout()
        layout.setSpacing(6)
        text = QLabel(label)
        text.setObjectName("fieldLabel")
        layout.addWidget(text)
        layout.addWidget(widget)
        return layout

    def editor_slider(self, minimum, maximum, value, handler):
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setValue(value)
        slider.valueChanged.connect(handler)
        return slider

    def editor_spin(self, minimum, maximum, value):
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        spin.valueChanged.connect(self.update_invoice_template_from_controls)
        return spin

    def template_path(self, name):
        safe = "".join(ch for ch in (name or "template") if ch.isalnum() or ch in (" ", "-", "_")).strip() or "template"
        return Path(TEMPLATES_FOLDER) / f"{safe.replace(' ', '_')}.json"

    def load_startup_invoice_template(self):
        active = self.settings.get("active_invoice_template")
        if active:
            path = self.template_path(active)
            if path.exists():
                try:
                    return normalize_invoice_template(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    logging.error("Unable to read startup invoice template", exc_info=True)
                    pass
        return normalize_invoice_template(self.settings.get("invoice_template"))

    def refresh_template_combo(self):
        if not hasattr(self, "template_combo"):
            return
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        self.template_combo.addItem("Default")
        for path in sorted(Path(TEMPLATES_FOLDER).glob("*.json")):
            self.template_combo.addItem(path.stem.replace("_", " "))
        active = self.invoice_template.get("name", "Default")
        index = self.template_combo.findText(active)
        self.template_combo.setCurrentIndex(max(index, 0))
        self.template_combo.blockSignals(False)

    def sync_editor_controls(self):
        if not hasattr(self, "editor_title_input"):
            return
        self._syncing_invoice_controls = True
        self.editor_title_input.setText(self.invoice_template["title"])
        self.header_bg_btn.setText(self.invoice_template["header_bg"])
        self.header_text_btn.setText(self.invoice_template["header_text"])
        self.header_bg_btn.setStyleSheet(f"background: {self.invoice_template['header_bg']}; color: {self.invoice_template['header_text']};")
        self.header_text_btn.setStyleSheet(f"background: {self.invoice_template['header_text']}; color: {self.invoice_template['header_bg']};")
        self.header_height_slider.setValue(self.invoice_template["header_height"])
        self.title_font_size_spin.setValue(self.invoice_template["title_font_size"])
        self.body_font_size_spin.setValue(self.invoice_template["body_font_size"])
        self.section_spacing_spin.setValue(self.invoice_template["section_spacing"])
        self.section_order_list.clear()
        for section in self.invoice_template["section_order"]:
            self.section_order_list.addItem(SECTION_LABELS[section])
        self._syncing_invoice_controls = False

    def update_invoice_template_from_controls(self):
        if getattr(self, "_syncing_invoice_controls", False) or not hasattr(self, "editor_title_input"):
            return
        self.invoice_template.update({
            "title": self.editor_title_input.text().strip() or "INVOICE",
            "header_height": self.header_height_slider.value(),
            "title_font_size": self.title_font_size_spin.value(),
            "body_font_size": self.body_font_size_spin.value(),
            "section_spacing": self.section_spacing_spin.value(),
        })
        self.invoice_template = normalize_invoice_template(self.invoice_template)
        self.settings["invoice_template"] = self.invoice_template
        self.settings_manager.save(self.settings)
        self.update_invoice_preview()

    def pick_template_color(self, key):
        color = QColorDialog.getColor(QColor(self.invoice_template[key]), self, "Choose Color")
        if not color.isValid():
            return
        self.invoice_template[key] = color.name()
        self.settings["invoice_template"] = self.invoice_template
        self.settings_manager.save(self.settings)
        self.sync_editor_controls()
        self.update_invoice_preview()

    def move_invoice_section(self, direction):
        row = self.section_order_list.currentRow()
        target = row + direction
        if row < 0 or target < 0 or target >= self.section_order_list.count():
            return
        order = list(self.invoice_template["section_order"])
        order[row], order[target] = order[target], order[row]
        self.invoice_template["section_order"] = order
        self.sync_editor_controls()
        self.section_order_list.setCurrentRow(target)
        self.update_invoice_template_from_controls()
        self.update_invoice_preview()

    def save_current_template(self):
        name, ok = QInputDialog.getText(self, "Save Template", "Template name", text=self.invoice_template.get("name", "Custom"))
        if not ok or not name.strip():
            return
        self.update_invoice_template_from_controls()
        self.invoice_template["name"] = name.strip()
        path = self.template_path(name)
        try:
            atomic_write_text(path, json.dumps(self.invoice_template, indent=2))
        except OSError as exc:
            log_exception("Unable to save invoice template", exc)
            QMessageBox.critical(self, "Template Error", "Template could not be saved.")
            return
        self.settings["active_invoice_template"] = self.invoice_template["name"]
        self.settings["invoice_template"] = self.invoice_template
        self.settings_manager.save(self.settings)
        self.refresh_template_combo()
        QMessageBox.information(self, "Template Saved", f"Template saved:\n{path}")

    def load_selected_template(self, name):
        if not name:
            return
        if name == "Default":
            self.invoice_template = default_invoice_template()
        else:
            path = self.template_path(name)
            if not path.exists():
                return
            try:
                self.invoice_template = normalize_invoice_template(json.loads(path.read_text(encoding="utf-8")))
                self.invoice_template["name"] = name
            except (OSError, json.JSONDecodeError) as exc:
                log_exception("Unable to load invoice template", exc)
                QMessageBox.critical(self, "Template Error", str(exc))
                return
        self.settings["active_invoice_template"] = self.invoice_template.get("name", name)
        self.settings["invoice_template"] = self.invoice_template
        self.settings_manager.save(self.settings)
        self.sync_editor_controls()
        self.update_invoice_preview()

    def clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            child_layout = item.layout()
            if widget:
                widget.deleteLater()
            elif child_layout:
                self.clear_layout(child_layout)

    def preview_lessons(self):
        if self.selected:
            try:
                rows = self.db.fetch("SELECT l.date, l.duration, t.name, t.rate FROM lessons l JOIN lesson_types t ON l.lesson_type_id=t.id WHERE skater_id=? ORDER BY l.date DESC LIMIT 4", (self.selected["id"],))
                if rows:
                    return rows
            except sqlite3.Error as exc:
                log_exception("Unable to load preview lessons", exc)
        return [
            {"date": "2026-05-01", "duration": 1.0, "name": "Private Lesson", "rate": 65.0},
            {"date": "2026-05-08", "duration": 0.5, "name": "Edge Work", "rate": 55.0},
            {"date": "2026-05-15", "duration": 1.0, "name": "Choreography", "rate": 70.0},
        ]

    def update_invoice_preview(self):
        if not hasattr(self, "invoice_preview_layout"):
            return
        template = normalize_invoice_template(self.invoice_template)
        self.invoice_template = template
        self.clear_layout(self.invoice_preview_layout)
        self.invoice_preview_page.setStyleSheet("#invoicePreviewPage { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; }")

        header = QFrame()
        header.setObjectName("previewHeader")
        header.setFixedHeight(template["header_height"])
        header.setStyleSheet(f"#previewHeader {{ background: {template['header_bg']}; border-top-left-radius: 10px; border-top-right-radius: 10px; }}")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(28, 12, 28, 12)
        heading = QLabel(template["title"])
        heading.setStyleSheet(f"color: {template['header_text']}; font-size: {template['title_font_size']}px; font-weight: 700;")
        meta = QLabel("Skating Invoice Pro\nGenerated Today")
        meta.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        meta.setStyleSheet(f"color: {template['header_text']}; font-size: 11px;")
        header_layout.addWidget(heading)
        header_layout.addStretch(1)
        header_layout.addWidget(meta)
        self.invoice_preview_layout.addWidget(header)

        body = QFrame()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(28, 28, 28, 28)
        body_layout.setSpacing(template["section_spacing"])
        self.invoice_preview_layout.addWidget(body)

        skater_name = self.selected["name"] if self.selected else "Selected Skater"
        skater_email = (self.selected.get("email") if self.selected else "") or "No email on file"
        month_text = self.invoice_month.text().strip() if hasattr(self, "invoice_month") else ""
        year_text = self.invoice_year.text().strip() if hasattr(self, "invoice_year") else ""
        try:
            period = datetime(int(year_text), int(month_text), 1).strftime("%B %Y")
        except (TypeError, ValueError):
            period = "Invoice Period"
        lessons = self.preview_lessons()
        total = sum(float(row["duration"]) * float(row["rate"]) for row in lessons)

        for section in template["section_order"]:
            if section == "skater":
                body_layout.addWidget(self.preview_info_card("Billed To", skater_name, skater_email, template))
            elif section == "period":
                sender = self.from_name.text().strip() if hasattr(self, "from_name") else ""
                email = self.from_email.text().strip() if hasattr(self, "from_email") else ""
                body_layout.addWidget(self.preview_info_card("Invoice Period", period, f"{sender or 'Sender'} | {email or 'No email provided'}", template))
            elif section == "lessons":
                body_layout.addWidget(self.preview_lesson_table(lessons, template))
            elif section == "total":
                body_layout.addWidget(self.preview_total_card(total, template))
        body_layout.addStretch(1)

    def preview_info_card(self, eyebrow, primary, secondary, template):
        card = self.card("previewSection")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        label = QLabel(eyebrow)
        label.setObjectName("fieldLabel")
        main = QLabel(primary)
        main.setStyleSheet(f"font-size: {template['body_font_size'] + 4}px; font-weight: 700;")
        sub = QLabel(secondary)
        sub.setObjectName("cardHint")
        layout.addWidget(label)
        layout.addWidget(main)
        layout.addWidget(sub)
        return card

    def preview_lesson_table(self, lessons, template):
        card = self.card("previewSection")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        title = QLabel("Lessons")
        title.setObjectName("fieldLabel")
        layout.addWidget(title)
        table = QTableWidget(len(lessons), 5)
        table.setHorizontalHeaderLabels(["Date", "Lesson Type", "Duration", "Rate", "Amount"])
        table.horizontalHeader().setStretchLastSection(True)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setFixedHeight(165)
        for row_index, row in enumerate(lessons):
            amount = float(row["duration"]) * float(row["rate"])
            values = [row["date"], row["name"], f"{float(row['duration']):.2f} h", f"${float(row['rate']):.2f}", f"${amount:.2f}"]
            for col, value in enumerate(values):
                table.setItem(row_index, col, QTableWidgetItem(value))
        table.setStyleSheet(f"font-size: {template['body_font_size']}px;")
        layout.addWidget(table)
        return card

    def preview_total_card(self, total, template):
        card = QFrame()
        card.setObjectName("previewTotal")
        card.setStyleSheet(f"#previewTotal {{ background: {template['header_bg']}; border-radius: 10px; }}")
        layout = QHBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 14)
        label = QLabel("Total Due")
        label.setStyleSheet(f"color: {template['header_text']}; font-size: {template['body_font_size']}px;")
        value = QLabel(f"${total:.2f}")
        value.setAlignment(Qt.AlignmentFlag.AlignRight)
        value.setStyleSheet(f"color: {template['header_text']}; font-size: {template['title_font_size'] - 2}px; font-weight: 700;")
        layout.addWidget(label)
        layout.addStretch(1)
        layout.addWidget(value)
        return card

    def build_finance_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(12)

        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.expected_value = self.kpi_card(cards, "Expected Revenue", "kpiBlue")
        self.collected_value = self.kpi_card(cards, "Collected Revenue", "kpiGreen")
        self.outstanding_value = self.kpi_card(cards, "Outstanding Balance", "kpiPurple")

        status = self.card()
        st = QVBoxLayout(status)
        st.setContentsMargins(16, 16, 16, 16)
        st.addWidget(QLabel("Payment Status"))
        self.paid_checks_holder = QVBoxLayout()
        self.paid_checks_holder.setSpacing(8)
        st.addLayout(self.paid_checks_holder)
        st.addStretch(1)

        lay.addLayout(cards)
        lay.addWidget(status, 1)
        return tab

    def kpi_card(self, layout, label, obj):
        card = self.card(obj)
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 16, 16, 16)
        v.addWidget(QLabel(label))
        value = QLabel("$0.00")
        value.setObjectName("kpiValue")
        v.addWidget(value)
        v.addStretch(1)
        layout.addWidget(card)
        return value

    def apply_styles(self):
        self.setFont(QFont("Segoe UI", 10))
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #f8fafc; color: #0f172a; }
            #sidebar, #card, #kpiBlue, #kpiGreen, #kpiPurple { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; }
            #kpiBlue { background: #eff6ff; }
            #kpiGreen { background: #ecfdf5; }
            #kpiPurple { background: #f5f3ff; }
            QLabel { background: transparent; }
            #sidebarTitle { font-size: 18px; font-weight: 700; padding-bottom: 8px; }
            #sectionLabel { color: #64748b; font-weight: 600; }
            #pageTitle { font-size: 22px; font-weight: 700; color: #0f172a; padding-left: 2px; }
            #pageSubtitle { color: #64748b; padding-left: 2px; padding-bottom: 4px; }
            #cardTitle { font-size: 14px; font-weight: 700; color: #0f172a; }
            #cardHint { color: #64748b; font-size: 12px; }
            #fieldLabel { color: #475569; font-size: 12px; font-weight: 600; }
            QTabWidget::pane { border: none; }
            QTabBar::tab { background: #e2e8f0; padding: 10px 16px; border-radius: 10px; margin-right: 6px; }
            QTabBar::tab:selected { background: #4f46e5; color: white; }
            QPushButton { background: #4f46e5; color: white; border: none; border-radius: 10px; padding: 10px 14px; }
            QPushButton:hover { background: #4338ca; }
            QPushButton:disabled { background: #94a3b8; color: #e2e8f0; }
            QPushButton#secondary { background: #ffffff; color: #334155; border: 1px solid #cbd5e1; }
            QPushButton#secondary:hover { background: #f1f5f9; }
            QPushButton#success { background: #16a34a; }
            QPushButton#success:hover { background: #15803d; }
            QLineEdit, QComboBox, QDateEdit { background: #ffffff; border: 1px solid #cbd5e1; border-radius: 10px; padding: 10px; }
            QLineEdit:focus, QComboBox:focus, QDateEdit:focus { border: 1px solid #4f46e5; }
            QTableWidget { background: #ffffff; alternate-background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px; gridline-color: #eef2f7; }
            QHeaderView::section { background: #f1f5f9; border: none; padding: 10px; color: #334155; font-weight: 600; }
            QTableWidget::item { padding: 10px; border-bottom: 1px solid #f1f5f9; }
            QTableWidget::item:selected { background: #e0e7ff; color: #1e293b; }
            QLabel#kpiValue { font-size: 28px; font-weight: 700; }
            QCheckBox { spacing: 10px; padding: 6px; border-radius: 8px; }
            QCheckBox:hover { background: #f8fafc; }
            QScrollArea#previewScroll { background: #f8fafc; border: none; }
            #previewSection { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; }
            QListWidget#sectionOrderList { background: #ffffff; border: 1px solid #cbd5e1; border-radius: 10px; padding: 6px; }
            QListWidget#sectionOrderList::item { padding: 8px; border-radius: 8px; }
            QListWidget#sectionOrderList::item:selected { background: #e0e7ff; color: #1e293b; }
            QSlider::groove:horizontal { height: 6px; background: #e2e8f0; border-radius: 3px; }
            QSlider::handle:horizontal { background: #4f46e5; width: 16px; margin: -5px 0; border-radius: 8px; }
            QSpinBox { background: #ffffff; border: 1px solid #cbd5e1; border-radius: 10px; padding: 8px; }
        """)

    def refresh_all(self):
        try:
            self.skaters = self.db.fetch("SELECT * FROM skaters ORDER BY name")
        except sqlite3.Error as exc:
            log_exception("Unable to refresh skaters", exc)
            QMessageBox.critical(self, "Database Error", str(exc))
            self.skaters = []
        current_id = self.selected["id"] if self.selected else None
        self.skater_combo.blockSignals(True)
        self.skater_combo.clear()
        self.skater_combo.addItems([s["name"] for s in self.skaters])
        idx = next((i for i, s in enumerate(self.skaters) if s["id"] == current_id), 0)
        self.selected = self.skaters[idx] if self.skaters else None
        if self.skaters:
            self.skater_combo.setCurrentIndex(idx)
        self.skater_combo.blockSignals(False)
        self.refresh_lesson_types()
        self.refresh_lessons()
        self.refresh_finance()

    def select_skater(self):
        i = self.skater_combo.currentIndex()
        if 0 <= i < len(self.skaters):
            self.selected = self.skaters[i]
            self.refresh_lessons()
            self.update_invoice_preview()

    def add_skater(self):
        name, ok = QInputDialog.getText(self, "Skater", "Skater name")
        if not ok or not name.strip():
            return
        email, _ = QInputDialog.getText(self, "Skater", "Email")
        try:
            self.db.run("INSERT INTO skaters(name,email) VALUES(?,?)", (name.strip(), (email or "").strip()))
        except sqlite3.Error as exc:
            log_exception("Unable to add skater", exc)
            QMessageBox.critical(self, "Database Error", str(exc))
            return
        self.refresh_all()

    def edit_skater(self):
        if not self.selected:
            QMessageBox.warning(self, "Validation", "Select a skater first.")
            return
        name, ok = QInputDialog.getText(self, "Edit Skater", "Skater name", text=self.selected["name"])
        if not ok or not name.strip():
            return
        email, _ = QInputDialog.getText(self, "Edit Skater", "Email", text=self.selected.get("email") or "")
        try:
            self.db.run("UPDATE skaters SET name=?, email=? WHERE id=?", (name.strip(), (email or "").strip(), self.selected["id"]))
        except sqlite3.Error as exc:
            log_exception("Unable to edit skater", exc)
            QMessageBox.critical(self, "Database Error", str(exc))
            return
        self.refresh_all()

    def manage_lesson_types(self):
        name, ok = QInputDialog.getText(self, "Lesson Type", "Name")
        if not ok or not name.strip():
            return
        rate, ok = QInputDialog.getDouble(self, "Lesson Type", "Rate", decimals=2, minValue=0)
        if not ok:
            return
        try:
            self.db.run("INSERT INTO lesson_types(name, rate) VALUES(?,?)", (name.strip(), rate))
        except sqlite3.Error as exc:
            log_exception("Unable to add lesson type", exc)
            QMessageBox.critical(self, "Database Error", str(exc))
            return
        self.refresh_lesson_types()

    def refresh_lesson_types(self):
        try:
            self.lesson_types = self.db.fetch("SELECT * FROM lesson_types ORDER BY name")
        except sqlite3.Error as exc:
            log_exception("Unable to refresh lesson types", exc)
            QMessageBox.critical(self, "Database Error", str(exc))
            self.lesson_types = []
        self.lesson_type_combo.clear()
        self.lesson_type_combo.addItems([t["name"] for t in self.lesson_types])

    def add_lesson(self):
        if not self.selected:
            QMessageBox.warning(self, "Validation", "Please select a skater first.")
            return
        if not self.lesson_types:
            QMessageBox.warning(self, "Validation", "Create a lesson type before adding lessons.")
            return
        try:
            duration = float(self.lesson_duration.text().strip())
            if duration <= 0:
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, "Validation", "Enter a positive duration.")
            return
        lesson_date = self.lesson_date.date().toString("yyyy-MM-dd")
        lid = next((t["id"] for t in self.lesson_types if t["name"] == self.lesson_type_combo.currentText()), None)
        if lid is None:
            QMessageBox.warning(self, "Validation", "Select a valid lesson type.")
            return
        try:
            self.db.run("INSERT INTO lessons(skater_id, lesson_type_id, date, duration) VALUES (?,?,?,?)", (self.selected["id"], lid, lesson_date, duration))
        except sqlite3.Error as exc:
            log_exception("Unable to add lesson", exc)
            QMessageBox.critical(self, "Database Error", str(exc))
            return
        self.lesson_duration.clear()
        self.refresh_lessons()
        self.refresh_finance()
        self.update_invoice_preview()

    def refresh_lessons(self):
        self.lesson_table.setRowCount(0)
        if not self.selected:
            return
        try:
            lessons = self.db.fetch("SELECT l.date, l.duration, t.name, t.rate FROM lessons l JOIN lesson_types t ON l.lesson_type_id=t.id WHERE skater_id=? ORDER BY l.date DESC", (self.selected["id"],))
        except sqlite3.Error as exc:
            log_exception("Unable to refresh lessons", exc)
            QMessageBox.critical(self, "Database Error", str(exc))
            return
        for i, l in enumerate(lessons):
            self.lesson_table.insertRow(i)
            amount = float(l["duration"]) * float(l["rate"])
            values = [l["date"], l["name"], f"{l['duration']:.2f}h", f"${amount:.2f}"]
            for c, v in enumerate(values):
                self.lesson_table.setItem(i, c, QTableWidgetItem(v))

    def save_invoice_settings(self):
        self.settings.update({"from_name": self.from_name.text().strip(), "from_email": self.from_email.text().strip(), "invoice_month": self.invoice_month.text().strip(), "invoice_year": self.invoice_year.text().strip(), "invoice_template": normalize_invoice_template(self.invoice_template)})
        self.settings_manager.save(self.settings)

    def generate_invoice(self):
        if canvas is None:
            QMessageBox.critical(self, "Missing Dependency", "reportlab is required to generate PDFs.")
            return
        if not self.selected:
            QMessageBox.warning(self, "Validation", "Please select a skater.")
            return
        self.save_invoice_settings()
        try:
            month = int(self.invoice_month.text().strip())
            year = int(self.invoice_year.text().strip())
            datetime(year, month, 1)
        except ValueError:
            QMessageBox.warning(self, "Validation", "Month and year must be valid numbers.")
            return
        worker = InvoiceWorker(self.settings.copy(), self.selected, month, year, self.invoice_template)
        self.generate_btn.setEnabled(False)
        self.generate_btn.setText("Generating...")
        self.invoice_status.setText("Generating invoice in the background")
        worker.signals.finished.connect(self.invoice_generated)
        worker.signals.error.connect(self.invoice_failed)
        self.thread_pool.start(worker)

    def invoice_generated(self, file):
        self.generate_btn.setEnabled(True)
        self.generate_btn.setText("Generate Invoice")
        self.invoice_status.setText("Invoice generated successfully")
        QMessageBox.information(self, "Saved", f"Invoice created:\n{file}")

    def invoice_failed(self, error):
        self.generate_btn.setEnabled(True)
        self.generate_btn.setText("Generate Invoice")
        self.invoice_status.setText("Invoice generation failed")
        QMessageBox.critical(self, "PDF Error", error)

    def open_folder(self):
        os.makedirs(INVOICE_FOLDER, exist_ok=True)
        path = os.path.abspath(INVOICE_FOLDER)
        if os.name == "nt":
            try:
                os.startfile(path)
            except OSError as exc:
                log_exception("Unable to open invoice folder", exc)
                QMessageBox.critical(self, "Folder Error", str(exc))
        else:
            QMessageBox.information(self, "Folder", f"Invoices folder: {path}")

    def refresh_finance(self):
        while self.paid_checks_holder.count():
            item = self.paid_checks_holder.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        try:
            skaters = self.db.fetch("SELECT * FROM skaters ORDER BY name")
        except sqlite3.Error as exc:
            log_exception("Unable to refresh finance skaters", exc)
            QMessageBox.critical(self, "Database Error", str(exc))
            skaters = []
        for s in skaters:
            cb = QCheckBox(s["name"])
            cb.setChecked(bool(s["paid"]))
            cb.stateChanged.connect(lambda state, sid=s["id"]: self._update_paid(sid, state))
            self.paid_checks_holder.addWidget(cb)
        try:
            expected, collected = self.finance_service.totals()
        except sqlite3.Error as exc:
            log_exception("Unable to calculate finance totals", exc)
            QMessageBox.critical(self, "Database Error", str(exc))
            expected, collected = 0.0, 0.0
        self.expected_value.setText(f"${expected:.2f}")
        self.collected_value.setText(f"${collected:.2f}")
        self.outstanding_value.setText(f"${expected - collected:.2f}")

    def _update_paid(self, skater_id, state):
        try:
            self.db.run("UPDATE skaters SET paid=? WHERE id=?", (1 if state == Qt.CheckState.Checked else 0, skater_id))
            expected, collected = self.finance_service.totals()
        except sqlite3.Error as exc:
            log_exception("Unable to update payment status", exc)
            QMessageBox.critical(self, "Database Error", str(exc))
            return
        self.expected_value.setText(f"${expected:.2f}")
        self.collected_value.setText(f"${collected:.2f}")
        self.outstanding_value.setText(f"${expected - collected:.2f}")


def main():
    debug_trace("main() entered")
    if not PYSIDE_AVAILABLE:
        print("PySide6 is required to run the desktop UI.", file=sys.stderr)
        return 1
    debug_trace("Creating QApplication")
    app = QApplication(sys.argv)
    if bcrypt is None:
        debug_trace("bcrypt missing; showing dependency error")
        show_error(None, "Missing Dependency", "bcrypt is required for local authentication. Install it with: pip install bcrypt")
        return 1
    auth_service = AuthService()
    try:
        session_valid = auth_service.has_valid_session()
    except Exception as exc:
        log_exception("Unhandled auth session error", exc)
        session_valid = False
    debug_trace(f"Session bypass allowed={session_valid}")
    if not session_valid:
        debug_trace("Opening AuthDialog")
        auth_dialog = AuthDialog(auth_service)
        accepted_code = getattr(QDialog, "Accepted", None)
        if accepted_code is None:
            accepted_code = QDialog.DialogCode.Accepted
        try:
            dialog_result = auth_dialog.exec()
        except Exception as exc:
            log_exception("AuthDialog failed", exc)
            show_error(None, "Authentication Error", f"Authentication window failed:\n{exc}")
            return 1
        debug_trace(f"AuthDialog result={dialog_result}, accepted={accepted_code}")
        if dialog_result != accepted_code:
            debug_trace("AuthDialog rejected; exiting before MainWindow")
            return 0
    try:
        debug_trace("Creating MainWindow")
        window = MainWindow()
    except Exception as exc:
        log_exception("Application startup failed", exc)
        show_error(None, "Startup Error", f"Skating Invoice Pro could not start:\n{exc}")
        return 1
    debug_trace("MainWindow created; calling show()")
    window.show()
    debug_trace("Entering QApplication event loop")
    try:
        result = app.exec()
        debug_trace(f"QApplication event loop exited with code {result}")
        return result
    except Exception as exc:
        log_exception("QApplication event loop failed", exc)
        show_error(None, "Runtime Error", f"Application event loop failed:\n{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
