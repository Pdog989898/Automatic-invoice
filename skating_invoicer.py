import sqlite3
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, colorchooser
from datetime import datetime
import os
import json
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas


# ================= FILES =================
DB_FILE = "skating.db"
SETTINGS_FILE = "settings.json"
INVOICE_FOLDER = "invoices"

os.makedirs(INVOICE_FOLDER, exist_ok=True)


# ================= SETTINGS =================
def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f)

    return {
        "header_bg": "#7a4bd1",
        "header_text": "#ffffff",
        "title": "INVOICE",
        "from_name": "",
        "from_email": ""
    }


def save_settings(data):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ================= DATABASE =================
class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DB_FILE)
        self.conn.row_factory = sqlite3.Row
        self.init()

    def init(self):
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS skaters(
            id INTEGER PRIMARY KEY,
            name TEXT,
            email TEXT,
            paid INTEGER DEFAULT 0
        )
        """)

        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS lesson_types(
            id INTEGER PRIMARY KEY,
            name TEXT,
            rate REAL
        )
        """)

        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS lessons(
            id INTEGER PRIMARY KEY,
            skater_id INTEGER,
            lesson_type_id INTEGER,
            date TEXT,
            duration REAL
        )
        """)

        self.conn.commit()

    def fetch(self, q, p=()):
        return [dict(r) for r in self.conn.execute(q, p)]

    def run(self, q, p=()):
        self.conn.execute(q, p)
        self.conn.commit()


# ================= LESSONS TAB =================
class LessonsTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app

        left = ttk.Frame(self)
        left.pack(side="left", fill="y", padx=10, pady=10)

        ttk.Label(left, text="Lessons", font=("Segoe UI", 12, "bold")).pack(anchor="w")

        row = ttk.Frame(left)
        row.pack(fill="x")

        self.type_var = tk.StringVar()
        self.type_box = ttk.Combobox(row, textvariable=self.type_var, state="readonly")
        self.type_box.pack(side="left", fill="x", expand=True)

        tk.Button(row, text="+", width=3, command=self.add_type).pack(side="left", padx=5)

        ttk.Label(left, text="Date (YYYY-MM-DD)").pack(anchor="w")
        self.date = ttk.Entry(left)
        self.date.pack(fill="x")

        ttk.Label(left, text="Duration (hours)").pack(anchor="w")
        self.duration = ttk.Entry(left)
        self.duration.pack(fill="x")

        tk.Button(left, text="Add Lesson", bg="green", fg="white",
                  command=self.add_lesson).pack(fill="x", pady=8)

        self.list = ttk.Treeview(left, columns=("info",), show="headings", height=15)
        self.list.heading("info", text="Date | Type | Hours | Amount")
        self.list.pack(fill="both", expand=True)

    def refresh(self):
        self.list.delete(*self.list.get_children())

        types = self.app.db.fetch("SELECT * FROM lesson_types")
        self.type_box["values"] = [t["name"] for t in types]

        if not self.app.selected:
            return

        lessons = self.app.db.fetch("""
        SELECT l.date, l.duration, t.name, t.rate
        FROM lessons l
        JOIN lesson_types t ON l.lesson_type_id=t.id
        WHERE skater_id=?
        ORDER BY l.date DESC
        """, (self.app.selected["id"],))

        for l in lessons:
            amt = float(l["duration"]) * float(l["rate"])
            self.list.insert("", "end",
                             values=(f"{l['date']} | {l['name']} | {l['duration']}h | ${amt:.2f}",))

    def add_type(self):
        name = simpledialog.askstring("Lesson Type", "Name")
        if not name:
            return
        rate = simpledialog.askfloat("Lesson Type", "Rate")
        if rate is None:
            return

        self.app.db.run("INSERT INTO lesson_types(name, rate) VALUES(?,?)", (name, rate))
        self.app.refresh()

    def add_lesson(self):
        if not self.app.selected:
            return

        try:
            datetime.strptime(self.date.get(), "%Y-%m-%d")
        except:
            messagebox.showerror("Error", "Invalid date")
            return

        duration = float(self.duration.get())

        types = self.app.db.fetch("SELECT * FROM lesson_types")
        lid = next((t["id"] for t in types if t["name"] == self.type_var.get()), None)

        self.app.db.run("""
        INSERT INTO lessons(skater_id, lesson_type_id, date, duration)
        VALUES (?,?,?,?)
        """, (self.app.selected["id"], lid, self.date.get(), duration))

        self.app.refresh()

# ================= INVOICE TAB =================
class InvoiceTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.settings = app.settings

        panel = ttk.Frame(self)
        panel.pack(side="left", fill="y", padx=10, pady=10)

        ttk.Label(panel, text="Invoice Generator",
                  font=("Segoe UI", 12, "bold")).pack(anchor="w")

        ttk.Label(panel, text="From Name").pack(anchor="w")
        self.from_name = ttk.Entry(panel)
        self.from_name.insert(0, self.settings["from_name"])
        self.from_name.pack(fill="x")

        ttk.Label(panel, text="From Email").pack(anchor="w")
        self.from_email = ttk.Entry(panel)
        self.from_email.insert(0, self.settings["from_email"])
        self.from_email.pack(fill="x")

        ttk.Label(panel, text="Month").pack(anchor="w")
        self.month = ttk.Entry(panel)
        self.month.pack(fill="x")

        ttk.Label(panel, text="Year").pack(anchor="w")
        self.year = ttk.Entry(panel)
        self.year.pack(fill="x")

        ttk.Separator(panel).pack(fill="x", pady=8)

        tk.Button(panel, text="Header Color",
                  command=self.pick_bg).pack(fill="x")

        tk.Button(panel, text="Header Text Color",
                  command=self.pick_text).pack(fill="x")

        ttk.Label(panel, text="Title").pack(anchor="w")
        self.title = ttk.Entry(panel)
        self.title.insert(0, self.settings["title"])
        self.title.pack(fill="x")

        btn_row = ttk.Frame(panel)
        btn_row.pack(fill="x", pady=10)

        tk.Button(btn_row, text="Generate PDF",
                  bg="white", fg="black",
                  command=self.generate).pack(side="left", expand=True, fill="x", padx=(0,5))

        tk.Button(btn_row, text="Open Invoices Folder",
                  command=self.open_folder).pack(side="left", expand=True, fill="x")

    def save(self):
        self.settings["from_name"] = self.from_name.get()
        self.settings["from_email"] = self.from_email.get()
        self.settings["title"] = self.title.get()
        save_settings(self.settings)

    def pick_bg(self):
        c = colorchooser.askcolor()[1]
        if c:
            self.settings["header_bg"] = c
            self.save()

    def pick_text(self):
        c = colorchooser.askcolor()[1]
        if c:
            self.settings["header_text"] = c
            self.save()

    def open_folder(self):
        os.startfile(os.path.abspath(INVOICE_FOLDER))

    def generate(self):
        self.save()

        sk = self.app.selected
        if not sk:
            return

        lessons = self.app.db.fetch("""
        SELECT l.date, l.duration, t.rate, t.name
        FROM lessons l
        JOIN lesson_types t ON l.lesson_type_id=t.id
        WHERE skater_id=?
        """, (sk["id"],))

        file = os.path.join(INVOICE_FOLDER, f"{sk['name']}.pdf")

        c = canvas.Canvas(file, pagesize=letter)
        w, h = letter

        hb = self.settings["header_bg"].lstrip("#")
        c.setFillColorRGB(int(hb[:2],16)/255, int(hb[2:4],16)/255, int(hb[4:],16)/255)
        c.rect(0, h-80, w, 80, fill=1)

        c.save()

        messagebox.showinfo("Saved", "Invoice created")


# ================= FINANCE TAB =================
class FinanceTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.vars = {}

        box = ttk.Frame(self)
        box.pack(fill="both", expand=True, padx=10, pady=10)

        ttk.Label(box, text="Finance Manager",
                  font=("Segoe UI", 14, "bold")).pack(anchor="w")

        self.container = ttk.Frame(box)
        self.container.pack(fill="both", expand=True)

        self.expected_label = ttk.Label(box, font=("Segoe UI", 11, "bold"))
        self.expected_label.pack(anchor="w")

        self.collected_label = ttk.Label(box, font=("Segoe UI", 11, "bold"), foreground="green")
        self.collected_label.pack(anchor="w")

    def refresh(self):
        for w in self.container.winfo_children():
            w.destroy()

        self.vars.clear()

        for s in self.app.db.fetch("SELECT * FROM skaters"):
            v = tk.IntVar(value=s["paid"])
            self.vars[s["id"]] = v

            tk.Checkbutton(
                self.container,
                text=s["name"],
                variable=v,
                command=self.update_paid
            ).pack(anchor="w")

        self.calculate()

    def update_paid(self):
        for sid, var in self.vars.items():
            self.app.db.run("UPDATE skaters SET paid=? WHERE id=?", (var.get(), sid))

        self.calculate()

    def calculate(self):
        expected = 0
        collected = 0

        for s in self.app.db.fetch("SELECT * FROM skaters"):
            lessons = self.app.db.fetch("""
            SELECT l.duration, t.rate
            FROM lessons l
            JOIN lesson_types t ON l.lesson_type_id=t.id
            WHERE skater_id=?
            """, (s["id"],))

            total = sum(float(l["duration"]) * float(l["rate"]) for l in lessons)

            expected += total
            if s["paid"]:
                collected += total

        self.expected_label.config(text=f"Expected Revenue: ${expected:.2f}")
        self.collected_label.config(text=f"Collected Revenue: ${collected:.2f}")


# ================= MAIN APP =================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Skating System")
        self.geometry("1100x650")

        self.db = Database()
        self.settings = load_settings()
        self.selected = None

        self.build()
        self.refresh()

    def build(self):
        left = ttk.Frame(self)
        left.pack(side="left", fill="y", padx=10)

        ttk.Label(left, text="Skaters").pack(anchor="w")

        self.combo = ttk.Combobox(left, state="readonly")
        self.combo.pack(fill="x")
        self.combo.bind("<<ComboboxSelected>>", lambda e: self.select())

        ttk.Button(left, text="Add Skater", command=self.add).pack(fill="x")

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True)

        self.lessons = LessonsTab(self.tabs, self)
        self.finance = FinanceTab(self.tabs, self)
        self.invoice = InvoiceTab(self.tabs, self)

        self.tabs.add(self.lessons, text="Lessons")
        self.tabs.add(self.invoice, text="Invoices")
        self.tabs.add(self.finance, text="Finance")

    def refresh(self):
        self.skaters = self.db.fetch("SELECT * FROM skaters")
        self.combo["values"] = [s["name"] for s in self.skaters]

        self.lessons.refresh()
        self.finance.refresh()

    def select(self):
        i = self.combo.current()
        if i >= 0:
            self.selected = self.skaters[i]
            self.refresh()

    def add(self):
        n = simpledialog.askstring("Name", "Skater name")
        e = simpledialog.askstring("Email", "Email")
        if n:
            self.db.run("INSERT INTO skaters(name,email) VALUES(?,?)", (n, e))
            self.refresh()


if __name__ == "__main__":
    App().mainloop()