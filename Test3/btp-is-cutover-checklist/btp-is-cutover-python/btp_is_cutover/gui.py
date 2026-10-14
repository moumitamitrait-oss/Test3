"""BTP IS Cutover Wizard: Tkinter window with five steps
1 Connect, 2 Select iFlows, 3 Details, 4 Dependencies, 5 Create."""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Dict, List, Optional

from . import __version__
from .api import ApiError
from .models import Dep
from .service import Analysis, analyze, generate, validate_details
from .settings import load_settings, save_settings
from .tenant import TYPE_LABELS, SelectedFlow, Tenant

GREEN, DEEP, SOFT, ERR_BG, ERR = "#86bc25", "#046a38", "#f4f5f6", "#fff5f4", "#b3261e"
STEPS = ["Connect", "Select iFlows", "Details", "Dependencies", "Create"]


class ScrollFrame(ttk.Frame):
    """A frame with a vertical scrollbar; put the content into `.inner`."""

    def __init__(self, parent, height: int = 300):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, height=height, background="white")
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")
        self.bind("<Enter>", self._bind_wheel)
        self.bind("<Leave>", self._unbind_wheel)

    def _bind_wheel(self, _e=None):
        self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))
        self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))

    def _unbind_wheel(self, _e=None):
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.unbind_all(seq)


class App(tk.Tk):
    def __init__(self, connect_fn: Optional[Callable[..., Tenant]] = None):
        super().__init__()
        self.title(f"BTP IS Cutover Wizard {__version__}")
        self.geometry("1120x780")
        self.minsize(980, 640)
        self.connect_fn = connect_fn or Tenant.connect
        self.settings: Dict[str, Any] = load_settings()
        self.tenant: Optional[Tenant] = None
        self.selected: Dict[str, SelectedFlow] = {}
        self.analyses: Dict[str, Analysis] = {}
        self.created: List[str] = []
        self.notes: List[str] = []
        self._q: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._style()
        self._build_chrome()
        self.pages: List[ttk.Frame] = [ConnectPage(self), SelectPage(self), DetailsPage(self), DepsPage(self), DonePage(self)]
        self.current = -1
        self.show(0)
        self.after(60, self._pump)

    # ---- plumbing ----------------------------------------------------------------------------------
    def _style(self) -> None:
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("TFrame", background="white")
        st.configure("TLabel", background="white", font=("Segoe UI", 10))
        st.configure("H.TLabel", font=("Segoe UI", 15, "bold"))
        st.configure("Lead.TLabel", foreground="#5b5f66")
        st.configure("Err.TLabel", foreground=ERR)
        st.configure("Ok.TLabel", foreground=DEEP)
        st.configure("Small.TLabel", foreground="#5b5f66", font=("Segoe UI", 9))
        st.configure("TButton", font=("Segoe UI", 10), padding=(12, 6))
        st.configure("Primary.TButton", background=DEEP, foreground="white", font=("Segoe UI", 10, "bold"))
        st.map("Primary.TButton", background=[("active", "#05562e"), ("disabled", "#9ab")])
        st.configure("TNotebook.Tab", font=("Segoe UI", 10), padding=(12, 5))
        st.configure("TCheckbutton", background="white")
        st.configure("Head.TLabel", background=DEEP, foreground="white", font=("Segoe UI", 10, "bold"), padding=(6, 5))

    def _build_chrome(self) -> None:
        top = tk.Frame(self, bg="#0c0c0c")
        top.pack(fill="x")
        tk.Label(top, text="BTP IS", bg=GREEN, fg="black", font=("Segoe UI", 11, "bold"), padx=8, pady=4).pack(side="left", padx=16, pady=12)
        box = tk.Frame(top, bg="#0c0c0c")
        box.pack(side="left")
        tk.Label(box, text="BTP IS Cutover Wizard", bg="#0c0c0c", fg="white", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        tk.Label(box, text="Create cutover workbooks for one or many iFlows", bg="#0c0c0c", fg="#b8bcc2", font=("Segoe UI", 9)).pack(anchor="w")
        bar = tk.Frame(self, bg=SOFT)
        bar.pack(fill="x")
        self.step_labels: List[tk.Label] = []
        for i, name in enumerate(STEPS, 1):
            lab = tk.Label(bar, text=f"  {i}  {name}", bg="white", fg="#5b5f66", font=("Segoe UI", 10), anchor="w", pady=7,
                           highlightthickness=1, highlightbackground="#d6d8db")
            lab.pack(side="left", fill="x", expand=True, padx=(12 if i == 1 else 3, 12 if i == len(STEPS) else 3), pady=10)
            self.step_labels.append(lab)
        self.body = ttk.Frame(self, padding=(24, 16))
        self.body.pack(fill="both", expand=True)

    def show(self, index: int) -> None:
        if self.current >= 0:
            self.pages[self.current].pack_forget()
        self.current = index
        for i, lab in enumerate(self.step_labels):
            done, active = i < index, i == index
            lab.configure(fg="black" if active else "#5b5f66", font=("Segoe UI", 10, "bold" if active else "normal"),
                          highlightbackground=DEEP if active else "#d6d8db",
                          text=f"  {'✓' if done else i + 1}  {STEPS[i]}")
        page = self.pages[index]
        page.pack(fill="both", expand=True, in_=self.body)
        if hasattr(page, "on_show"):
            page.on_show()

    def ui(self, fn: Callable[[], None]) -> None:
        """Run `fn` in the window thread (safe to call from a worker thread)."""
        self._q.put(fn)

    def _pump(self) -> None:
        try:
            while True:
                self._q.get_nowait()()
        except queue.Empty:
            pass
        self.after(60, self._pump)

    def run_bg(self, work: Callable[[], Any], done: Callable[[Any, Optional[BaseException]], None]) -> None:
        def target():
            try:
                result, err = work(), None
            except BaseException as exc:  # noqa: BLE001 - shown to the user
                result, err = None, exc
            self.ui(lambda: done(result, err))
        threading.Thread(target=target, daemon=True).start()

    def remember(self, **values: Any) -> None:
        self.settings.update(values)
        save_settings(self.settings)

    @staticmethod
    def friendly(exc: BaseException) -> str:
        return str(exc) or exc.__class__.__name__


# ================================================================================================================
class Page(ttk.Frame):
    title = ""
    lead = ""

    def __init__(self, app: App):
        super().__init__(app.body)
        self.app = app
        ttk.Label(self, text=self.title, style="H.TLabel").pack(anchor="w")
        if self.lead:
            ttk.Label(self, text=self.lead, style="Lead.TLabel", wraplength=1000, justify="left").pack(anchor="w", pady=(2, 12))

    def message(self, parent=None) -> ttk.Label:
        return ttk.Label(parent or self, text="", wraplength=1000, justify="left")

    @staticmethod
    def say(label: ttk.Label, text: str, kind: str = "") -> None:
        label.configure(text=text, style={"err": "Err.TLabel", "ok": "Ok.TLabel"}.get(kind, "TLabel"))

    def nav(self, back: Optional[Callable], nxt: Callable, next_text: str):
        row = ttk.Frame(self)
        row.pack(side="bottom", fill="x", pady=(10, 0))
        if back:
            ttk.Button(row, text="Back", command=back).pack(side="left")
        btn = ttk.Button(row, text=next_text, style="Primary.TButton", command=nxt)
        btn.pack(side="left", padx=(10, 0))
        return btn


# ---- 1 Connect ------------------------------------------------------------------------------------------------
class ConnectPage(Page):
    title = "Step 1: Connect with your API details"
    lead = "Use the service key of the Process Integration Runtime instance (plan api). The client secret is never saved."

    def __init__(self, app: App):
        super().__init__(app)
        form = ttk.Frame(self)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        self.vars: Dict[str, tk.StringVar] = {}
        rows = [("base_url", "API base URL", "the url value of the service key (/api/v1 is added if missing)"),
                ("token_url", "Token URL", "the tokenurl value of the service key"),
                ("client_id", "Client ID", ""), ("secret", "Client secret", "typed every time, never saved")]
        for r, (key, label, hint) in enumerate(rows):
            ttk.Label(form, text=label, font=("Segoe UI", 10, "bold")).grid(row=2 * r, column=0, sticky="w", pady=(10, 0), padx=(0, 16))
            var = tk.StringVar(value=app.settings.get(key, "") if key != "secret" else "")
            self.vars[key] = var
            ttk.Entry(form, textvariable=var, show="*" if key == "secret" else "", width=70).grid(row=2 * r, column=1, sticky="ew", pady=(10, 0))
            if hint:
                ttk.Label(form, text=hint, style="Small.TLabel").grid(row=2 * r + 1, column=1, sticky="w")
        self.btn = ttk.Button(self, text="Connect and load packages", style="Primary.TButton", command=self.connect)
        self.btn.pack(anchor="w", pady=(18, 6))
        self.msg = self.message()
        self.msg.pack(anchor="w")

    def connect(self) -> None:
        v = {k: var.get().strip() for k, var in self.vars.items()}
        v["secret"] = self.vars["secret"].get()
        if not all(v.values()):
            self.say(self.msg, "API base URL, token URL, client ID and client secret are required.", "err")
            return
        self.btn.configure(state="disabled")
        self.say(self.msg, "Connecting and loading packages...")
        self.app.run_bg(lambda: self.app.connect_fn(v["base_url"], v["token_url"], v["client_id"], v["secret"]), self.connected)

    def connected(self, tenant: Optional[Tenant], err: Optional[BaseException]) -> None:
        self.btn.configure(state="normal")
        if err:
            self.say(self.msg, self.app.friendly(err), "err")
            return
        self.app.tenant = tenant
        self.app.remember(base_url=self.vars["base_url"].get().strip(), token_url=self.vars["token_url"].get().strip(),
                          client_id=self.vars["client_id"].get().strip())
        self.vars["secret"].set("")
        self.say(self.msg, "")
        self.app.show(1)


# ---- 2 Select iFlows ------------------------------------------------------------------------------------------
class SelectPage(Page):
    title = "Step 2: Select the iFlows"
    lead = "Pick them from a package, or enter iFlow IDs directly. The active version of each iFlow is used."

    def __init__(self, app: App):
        super().__init__(app)
        self.nav(lambda: app.show(0), self.next, "Next: details")
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)

        # browse tab
        browse = ttk.Frame(self.nb, padding=8)
        self.nb.add(browse, text="Browse packages")
        browse.columnconfigure(0, weight=2, uniform="c")
        browse.columnconfigure(1, weight=3, uniform="c")
        browse.rowconfigure(1, weight=1)
        ttk.Label(browse, text="Packages", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w")
        self.flow_title = ttk.Label(browse, text="iFlows", font=("Segoe UI", 10, "bold"))
        self.flow_title.grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.pkg_filter = tk.StringVar()
        self.pkg_filter.trace_add("write", lambda *_: self.fill_packages())
        self.flow_filter = tk.StringVar()
        self.flow_filter.trace_add("write", lambda *_: self.fill_flows())
        left, right = ttk.Frame(browse), ttk.Frame(browse)
        left.grid(row=1, column=0, sticky="nsew")
        right.grid(row=1, column=1, sticky="nsew", padx=(12, 0))
        ttk.Entry(left, textvariable=self.pkg_filter).pack(fill="x", pady=(2, 4))
        self.pkg_box = self.listbox(left)
        self.pkg_box.bind("<<ListboxSelect>>", self.open_package)
        bar = ttk.Frame(right)
        bar.pack(fill="x", pady=(2, 4))
        ttk.Entry(bar, textvariable=self.flow_filter).pack(side="left", fill="x", expand=True)
        ttk.Button(bar, text="Add selected", command=self.add_selected).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Add all shown", command=self.add_all_shown).pack(side="left", padx=(6, 0))
        self.flow_box = self.listbox(right, extended=True)
        self.flow_box.bind("<Double-Button-1>", lambda e: self.add_selected())
        self.packages: List[Dict[str, str]] = []
        self.shown_packages: List[Dict[str, str]] = []
        self.flows: List[Dict[str, str]] = []
        self.shown_flows: List[Dict[str, str]] = []
        self.current_pkg: Optional[Dict[str, str]] = None

        # IDs tab
        ids = ttk.Frame(self.nb, padding=8)
        self.nb.add(ids, text="Enter iFlow IDs")
        ttk.Label(ids, text="iFlow IDs (one per line, or separated by commas)", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.ids_text = tk.Text(ids, height=6, font=("Consolas", 10))
        self.ids_text.pack(fill="x", pady=4)
        self.ids_btn = ttk.Button(ids, text="Add iFlows", command=self.add_ids)
        self.ids_btn.pack(anchor="w")
        self.ids_msg = self.message(ids)
        self.ids_msg.pack(anchor="w", pady=6)

        # selected
        sel = ttk.Frame(self)
        sel.pack(fill="x", pady=(10, 0))
        self.sel_title = ttk.Label(sel, text="Selected iFlows (0)", font=("Segoe UI", 10, "bold"))
        self.sel_title.pack(anchor="w")
        row = ttk.Frame(sel)
        row.pack(fill="x")
        self.sel_box = tk.Listbox(row, height=5, selectmode="extended", activestyle="none", exportselection=False)
        self.sel_box.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Remove selected", command=self.remove_selected).pack(side="left", padx=(8, 0), anchor="n")

    @staticmethod
    def listbox(parent, extended: bool = False) -> tk.Listbox:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        box = tk.Listbox(frame, selectmode="extended" if extended else "browse", activestyle="none", exportselection=False, font=("Segoe UI", 10))
        sb = ttk.Scrollbar(frame, orient="vertical", command=box.yview)
        box.configure(yscrollcommand=sb.set)
        box.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return box

    def on_show(self) -> None:
        self.packages = self.app.tenant.packages if self.app.tenant else []
        self.fill_packages()
        self.refresh_selected()

    def fill_packages(self) -> None:
        q = self.pkg_filter.get().strip().lower()
        self.shown_packages = [p for p in self.packages if q in (p["Name"] + " " + p["Id"]).lower()]
        self.pkg_box.delete(0, "end")
        for p in self.shown_packages:
            self.pkg_box.insert("end", p["Name"])

    def open_package(self, _e=None) -> None:
        sel = self.pkg_box.curselection()
        if not sel:
            return
        pkg = self.shown_packages[sel[0]]
        self.current_pkg = pkg
        self.flow_title.configure(text=pkg["Name"])
        self.flow_box.delete(0, "end")
        self.flow_box.insert("end", "Loading iFlows...")
        self.app.run_bg(lambda: self.app.tenant.list_flows(pkg["Id"]), lambda res, err: self.loaded_flows(pkg, res, err))

    def loaded_flows(self, pkg, flows, err) -> None:
        if pkg is not self.current_pkg:
            return
        self.flow_box.delete(0, "end")
        if err:
            self.flow_box.insert("end", self.app.friendly(err))
            return
        self.flows = flows
        self.fill_flows()

    def fill_flows(self) -> None:
        q = self.flow_filter.get().strip().lower()
        self.shown_flows = [f for f in self.flows if q in (f["Name"] + " " + f["Id"]).lower()]
        self.flow_box.delete(0, "end")
        for f in self.shown_flows:
            label = f["Name"] if f["Name"] == f["Id"] else f"{f['Name']}  ({f['Id']})"
            self.flow_box.insert("end", f"{label}   v{f['Version']}" if f["Version"] else label)
        if not self.flows and self.current_pkg:
            self.flow_box.insert("end", "This package has no iFlows.")

    def _add(self, pkg: Dict[str, str], f: Dict[str, str]) -> None:
        flow = SelectedFlow(pkg["Id"], pkg["Name"], f["Id"], f["Name"], f.get("Version", ""))
        self.app.selected.setdefault(flow.key, flow)

    def add_selected(self) -> None:
        if not self.current_pkg:
            return
        for i in self.flow_box.curselection():
            if i < len(self.shown_flows):
                self._add(self.current_pkg, self.shown_flows[i])
        self.refresh_selected()

    def add_all_shown(self) -> None:
        if self.current_pkg:
            for f in self.shown_flows:
                self._add(self.current_pkg, f)
            self.refresh_selected()

    def add_ids(self) -> None:
        import re
        ids = list(dict.fromkeys(x for x in re.split(r"[\s,;]+", self.ids_text.get("1.0", "end")) if x))
        if not ids:
            self.say(self.ids_msg, "Enter at least one iFlow ID.", "err")
            return
        self.ids_btn.configure(state="disabled")

        def work():
            added, problems = [], []
            for iflow_id in ids:
                self.app.ui(lambda i=iflow_id: self.say(self.ids_msg, f"Looking up {i}..."))
                try:
                    added.append(self.app.tenant.resolve_iflow_by_id(iflow_id))
                except ApiError as exc:
                    problems.append(f"{iflow_id}: {exc}")
            return added, problems

        def done(res, err):
            self.ids_btn.configure(state="normal")
            if err:
                self.say(self.ids_msg, self.app.friendly(err), "err")
                return
            added, problems = res
            for f in added:
                self.app.selected.setdefault(f.key, f)
            self.refresh_selected()
            self.say(self.ids_msg, (f"{len(added)} iFlow(s) added.\n" if added else "") + "\n".join(problems), "err" if problems else "ok")
            if not problems:
                self.ids_text.delete("1.0", "end")

        self.app.run_bg(work, done)

    def refresh_selected(self) -> None:
        self.sel_box.delete(0, "end")
        for f in self.app.selected.values():
            self.sel_box.insert("end", f"{f.name}   ({f.pkg_name})")
        self.sel_title.configure(text=f"Selected iFlows ({len(self.app.selected)})")

    def remove_selected(self) -> None:
        keys = list(self.app.selected.keys())
        for i in reversed(self.sel_box.curselection()):
            self.app.selected.pop(keys[i], None)
        self.refresh_selected()

    def next(self) -> None:
        if not self.app.selected:
            messagebox.showinfo("Select iFlows", "Select at least one iFlow first.")
            return
        self.app.show(2)


# ---- 3 Details ------------------------------------------------------------------------------------------------
class DetailsPage(Page):
    title = "Step 3: Cutover details"
    lead = "Enter an interface ID for each iFlow and the people and teams for the cutover steps. Fields marked * are required."

    def __init__(self, app: App):
        super().__init__(app)
        self.nav(lambda: app.show(1), self.next, "Next: detect dependencies")
        self.table = ScrollFrame(self, height=170)
        self.table.pack(fill="x")
        self.rows: List[tuple] = []
        form = ttk.Frame(self)
        form.pack(fill="x", pady=(14, 0))
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)
        self.fields: Dict[str, tk.Entry] = {}
        spec = [("env", "Target environment"), ("poc", "Point of contact *"), ("team_is", "Integration team"),
                ("team_func", "Functional / business team"), ("team_sec", "Security team")]
        for i, (key, label) in enumerate(spec):
            r, c = divmod(i, 2)
            ttk.Label(form, text=label, font=("Segoe UI", 10, "bold")).grid(row=r, column=2 * c, sticky="w", pady=6, padx=(0 if c == 0 else 24, 10))
            e = tk.Entry(form, font=("Segoe UI", 10), relief="solid", bd=1)
            e.grid(row=r, column=2 * c + 1, sticky="ew", pady=6, ipady=3)
            e.bind("<KeyRelease>", lambda ev, k=key: self.clear_flag(self.fields[k]))
            self.fields[key] = e
        self.msg = self.message()
        self.msg.pack(anchor="w", pady=8)

    @staticmethod
    def clear_flag(entry: tk.Entry) -> None:
        entry.configure(bg="white")

    def on_show(self) -> None:
        for child in self.table.inner.winfo_children():
            child.destroy()
        self.rows = []
        inner = self.table.inner
        inner.columnconfigure(0, weight=3)
        inner.columnconfigure(1, weight=2)
        inner.columnconfigure(2, weight=2)
        for c, h in enumerate(["iFlow", "Package", "Interface ID *"]):
            ttk.Label(inner, text=h, style="Head.TLabel").grid(row=0, column=c, sticky="ew")
        for r, flow in enumerate(self.app.selected.values(), 1):
            ttk.Label(inner, text=flow.name).grid(row=r, column=0, sticky="w", padx=6, pady=4)
            ttk.Label(inner, text=flow.pkg_name).grid(row=r, column=1, sticky="w", padx=6)
            e = tk.Entry(inner, font=("Segoe UI", 10), relief="solid", bd=1)
            e.insert(0, flow.interface_id)
            e.grid(row=r, column=2, sticky="ew", padx=6, pady=4, ipady=3)
            e.bind("<KeyRelease>", lambda ev, ent=e: self.clear_flag(ent))
            self.rows.append((flow, e))
        for key, entry in self.fields.items():
            if not entry.get():
                entry.insert(0, str(self.app.settings.get(key, "")))
        self.table.canvas.configure(height=min(220, 36 * (len(self.rows) + 1)))

    def next(self) -> None:
        for flow, e in self.rows:
            flow.interface_id = e.get().strip()
            e.configure(bg="white" if flow.interface_id else ERR_BG)
        poc = self.fields["poc"].get().strip()
        self.fields["poc"].configure(bg="white" if poc else ERR_BG)
        missing = validate_details(list(self.app.selected.values()), poc)
        if missing:
            self.say(self.msg, "Please enter " + " and ".join(missing) + ".", "err")
            return
        self.say(self.msg, "")
        if not self.fields["env"].get().strip():
            self.fields["env"].insert(0, "QA")
        self.app.remember(**{k: e.get().strip() for k, e in self.fields.items()})
        self.app.show(3)


# ---- 4 Dependencies -------------------------------------------------------------------------------------------
class AddDependencyDialog(tk.Toplevel):
    """Package dropdown -> artifact dropdown (grouped by type) -> the type is filled in automatically."""

    def __init__(self, app: App, on_done: Callable[[Dep], None], fill: Optional[Dep] = None):
        super().__init__(app)
        self.app, self.on_done, self.fill = app, on_done, fill
        self.title("Add dependency")
        self.configure(bg="white", padx=18, pady=14)
        self.transient(app)
        self.resizable(False, False)
        self.items: List[tuple] = []
        ttk.Label(self, text="Package", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w")
        self.pkg_var = tk.StringVar()
        names = [p["Name"] for p in (app.tenant.packages if app.tenant else [])]
        self.pkg_cb = ttk.Combobox(self, textvariable=self.pkg_var, values=names, state="readonly", width=56)
        self.pkg_cb.grid(row=1, column=0, sticky="ew", pady=(2, 10))
        self.pkg_cb.bind("<<ComboboxSelected>>", self.pick_package)
        ttk.Label(self, text="Artifact", font=("Segoe UI", 10, "bold")).grid(row=2, column=0, sticky="w")
        self.art_var = tk.StringVar()
        self.art_cb = ttk.Combobox(self, textvariable=self.art_var, values=[], state="disabled", width=56)
        self.art_cb.grid(row=3, column=0, sticky="ew", pady=(2, 10))
        self.art_cb.bind("<<ComboboxSelected>>", self.pick_artifact)
        ttk.Label(self, text="Artifact type", font=("Segoe UI", 10, "bold")).grid(row=4, column=0, sticky="w")
        self.type_var = tk.StringVar()
        ttk.Combobox(self, textvariable=self.type_var, values=TYPE_LABELS, width=56).grid(row=5, column=0, sticky="ew", pady=(2, 6))
        self.msg = ttk.Label(self, text="Select the package, then the artifact (the type is filled in automatically).", style="Small.TLabel", wraplength=420)
        self.msg.grid(row=6, column=0, sticky="w", pady=(0, 10))
        row = ttk.Frame(self)
        row.grid(row=7, column=0, sticky="e")
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="Add", style="Primary.TButton", command=self.ok).pack(side="left")
        self.pkg_id = ""
        self.grab_set()

    def pick_package(self, _e=None) -> None:
        pkg = next((p for p in self.app.tenant.packages if p["Name"] == self.pkg_var.get()), None)
        if not pkg:
            return
        self.pkg_id = pkg["Id"]
        self.art_cb.configure(state="disabled", values=[])
        self.art_var.set("Loading artifacts...")
        self.app.run_bg(lambda: self.app.tenant.package_contents(pkg["Id"]), self.loaded)

    def loaded(self, groups, err) -> None:
        if not self.winfo_exists():
            return
        self.art_var.set("")
        self.items = []
        if not err:
            for g in groups:
                for it in g["items"]:
                    self.items.append((g["label"], it))
        if self.items:
            self.art_cb.configure(state="readonly", values=[
                f"[{label}]  {it['Name']}" + (f"  ({it['Id']})" if it["Name"] != it["Id"] else "") for label, it in self.items])
            self.msg.configure(text="")
        else:
            self.art_cb.configure(state="normal")
            self.msg.configure(text="The artifact list of this package is not available. Type the artifact ID and choose the type.")

    def pick_artifact(self, _e=None) -> None:
        i = self.art_cb.current()
        if 0 <= i < len(self.items):
            self.type_var.set(self.items[i][0])

    def ok(self) -> None:
        i = self.art_cb.current()
        artifact = self.items[i][1]["Id"] if 0 <= i < len(self.items) else self.art_var.get().strip()
        if not artifact or not self.type_var.get().strip():
            self.msg.configure(text="Select an artifact and its type.", style="Err.TLabel")
            return
        dep = self.fill or Dep(source="Added manually", manual=False)
        dep.pkg, dep.pkg_id, dep.artifact, dep.system = self.pkg_var.get(), self.pkg_id, artifact, self.type_var.get().strip()
        dep.include, dep.manual = True, False
        if not dep.source:
            dep.source = "Added manually"
        self.on_done(dep)
        self.destroy()


class DepsPage(Page):
    title = "Step 4: Common dependent artifacts"
    lead = ("Detected from the iFlow design: referenced script collections, mappings, message types, what the message mappings use, "
            "and iFlows called through ProcessDirect. Untick, edit or add rows before creating the workbooks.")

    def __init__(self, app: App):
        super().__init__(app)
        self.next_btn = self.nav(lambda: app.show(2), self.create, "Create cutover templates")
        self.prog_frame = ttk.Frame(self)
        self.bar = ttk.Progressbar(self.prog_frame, mode="indeterminate")
        self.bar.pack(fill="x")
        self.prog_msg = ttk.Label(self.prog_frame, text="", style="Small.TLabel")
        self.prog_msg.pack(anchor="w", pady=4)
        self.nb: Optional[ttk.Notebook] = None
        self.msg = self.message()
        self.msg.pack(side="bottom", anchor="w")
        self.counters: Dict[str, ttk.Label] = {}
        self.busy = False

    def on_show(self) -> None:
        if self.nb is not None:
            self.nb.destroy()
            self.nb = None
        self.say(self.msg, "")
        self.next_btn.configure(state="disabled")
        self.prog_frame.pack(fill="x")
        self.bar.start(12)
        selected = list(self.app.selected.values())

        def progress(m: str) -> None:
            self.app.ui(lambda: self.prog_msg.configure(text=m))

        self.app.run_bg(lambda: analyze(self.app.tenant, selected, progress), self.analysed)

    def analysed(self, res: Optional[Dict[str, Analysis]], err: Optional[BaseException]) -> None:
        self.bar.stop()
        self.prog_frame.pack_forget()
        if err:
            self.say(self.msg, self.app.friendly(err), "err")
            return
        self.app.analyses = res or {}
        self.build_tabs()
        self.next_btn.configure(state="normal" if any(a.data for a in self.app.analyses.values()) else "disabled")

    def build_tabs(self) -> None:
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)
        self.counters = {}
        for flow in self.app.selected.values():
            a = self.app.analyses.get(flow.key)
            if not a:
                continue
            tab = ttk.Frame(self.nb, padding=8)
            self.nb.add(tab, text=flow.name if len(flow.name) <= 34 else flow.name[:31] + "...")
            ttk.Label(tab, text=f"{flow.name}  ({flow.pkg_name})", font=("Segoe UI", 10, "bold")).pack(anchor="w")
            if a.error:
                ttk.Label(tab, text=f"This iFlow could not be analysed: {a.error}\nIt will be skipped.", style="Err.TLabel", wraplength=980).pack(anchor="w", pady=8)
                continue
            counter = ttk.Label(tab, text="", style="Small.TLabel")
            counter.pack(anchor="w")
            self.counters[flow.key] = counter
            for w in a.data.warnings:
                ttk.Label(tab, text=w, style="Err.TLabel", wraplength=980).pack(anchor="w", pady=2)
            foot = ttk.Frame(tab)
            foot.pack(side="bottom", fill="x", pady=(6, 0))
            notes = list(a.result.notes)
            if a.result.skipped:
                notes.append("Not listed (no matching artifact found in this tenant): " + ", ".join(a.result.skipped)
                             + ". Use + Add dependency if one of them is needed.")
            if notes:
                ttk.Label(foot, text="\n".join(notes), style="Small.TLabel", wraplength=980, justify="left").pack(anchor="w", pady=(0, 6))
            sf = ScrollFrame(tab, height=300)
            sf.pack(fill="both", expand=True, pady=(6, 0))
            ttk.Button(foot, text="+ Add dependency", command=lambda f=flow, s=sf: self.add_dep(f, s)).pack(anchor="w")
            self.draw_rows(flow, sf)

    def draw_rows(self, flow: SelectedFlow, sf: ScrollFrame) -> None:
        a = self.app.analyses[flow.key]
        inner = sf.inner
        for child in inner.winfo_children():
            child.destroy()
        for col, weight in enumerate([0, 3, 4, 3, 4, 0]):
            inner.columnconfigure(col, weight=weight)
        for col, h in enumerate(["Use", "Package", "Artifact", "Artifact type", "Detected from", ""]):
            ttk.Label(inner, text=h, style="Head.TLabel").grid(row=0, column=col, sticky="ew")
        deps = a.result.deps
        if not deps:
            ttk.Label(inner, text="No dependencies detected. Use + Add dependency if there are any.", style="Small.TLabel").grid(row=1, column=0, columnspan=6, sticky="w", padx=6, pady=8)
        for r, dep in enumerate(deps, 1):
            use = tk.BooleanVar(value=dep.include)
            use.trace_add("write", lambda *_, d=dep, v=use, f=flow: (setattr(d, "include", v.get()), self.count(f)))
            ttk.Checkbutton(inner, variable=use).grid(row=r, column=0, padx=6, pady=3)
            for col, attr in ((1, "pkg"), (2, "artifact")):
                var = tk.StringVar(value=getattr(dep, attr))
                var.trace_add("write", lambda *_, d=dep, a_=attr, v=var: setattr(d, a_, v.get()))
                ttk.Entry(inner, textvariable=var).grid(row=r, column=col, sticky="ew", padx=3, pady=3)
            tvar = tk.StringVar(value=dep.system)
            tvar.trace_add("write", lambda *_, d=dep, v=tvar: setattr(d, "system", v.get()))
            values = TYPE_LABELS + ([dep.system] if dep.system and dep.system not in TYPE_LABELS else [])
            ttk.Combobox(inner, textvariable=tvar, values=values, width=18).grid(row=r, column=3, sticky="ew", padx=3, pady=3)
            ttk.Label(inner, text=dep.source, style="Small.TLabel", wraplength=300, justify="left").grid(row=r, column=4, sticky="w", padx=6)
            cell = ttk.Frame(inner)
            cell.grid(row=r, column=5, padx=3)
            if dep.manual:
                ttk.Button(cell, text="Pick…", width=6, command=lambda d=dep, f=flow, s=sf: self.pick(d, f, s)).pack(side="left", padx=(0, 3))
            ttk.Button(cell, text="×", width=3, command=lambda d=dep, f=flow, s=sf: self.remove(d, f, s)).pack(side="left")
        self.count(flow)

    def count(self, flow: SelectedFlow) -> None:
        a = self.app.analyses.get(flow.key)
        label = self.counters.get(flow.key)
        if a and label:
            label.configure(text=f"{sum(1 for d in a.result.deps if d.include)} dependencies selected")

    def remove(self, dep: Dep, flow: SelectedFlow, sf: ScrollFrame) -> None:
        self.app.analyses[flow.key].result.deps.remove(dep)
        self.draw_rows(flow, sf)

    def add_dep(self, flow: SelectedFlow, sf: ScrollFrame) -> None:
        def done(dep: Dep) -> None:
            self.app.analyses[flow.key].result.deps.append(dep)
            self.draw_rows(flow, sf)
        AddDependencyDialog(self.app, done)

    def pick(self, dep: Dep, flow: SelectedFlow, sf: ScrollFrame) -> None:
        AddDependencyDialog(self.app, lambda d: self.draw_rows(flow, sf), fill=dep)

    def create(self) -> None:
        folder = filedialog.askdirectory(title="Where should the Excel files be saved?",
                                         initialdir=self.app.settings.get("out_dir") or os.path.join(os.path.expanduser("~"), "Downloads"))
        if not folder:
            return
        self.app.remember(out_dir=folder)
        self.next_btn.configure(state="disabled")
        self.say(self.msg, "Creating workbooks...")
        opts = {"env": self.app.settings.get("env") or "QA", "poc": self.app.settings.get("poc"), "team_is": self.app.settings.get("team_is"),
                "team_func": self.app.settings.get("team_func"), "team_sec": self.app.settings.get("team_sec")}
        selected = list(self.app.selected.values())
        self.app.run_bg(lambda: generate(self.app.analyses, selected, opts, folder), self.created)

    def created(self, res, err) -> None:
        self.next_btn.configure(state="normal")
        if err:
            self.say(self.msg, self.app.friendly(err), "err")
            return
        paths, notes = res
        if not paths:
            self.say(self.msg, "No workbook could be created.\n" + "\n".join(notes), "err")
            return
        self.app.created, self.app.notes = paths, notes
        self.say(self.msg, "")
        self.app.show(4)


# ---- 5 Done ---------------------------------------------------------------------------------------------------
class DonePage(Page):
    title = "Step 5: Done"

    def __init__(self, app: App):
        super().__init__(app)
        self.text = ttk.Label(self, text="", style="Ok.TLabel", justify="left", wraplength=1000)
        self.text.pack(anchor="w", pady=(4, 8))
        self.notes = ttk.Label(self, text="", style="Small.TLabel", justify="left", wraplength=1000)
        self.notes.pack(anchor="w")
        row = ttk.Frame(self)
        row.pack(anchor="w", pady=14)
        ttk.Button(row, text="Open folder", style="Primary.TButton", command=self.open_folder).pack(side="left")
        ttk.Button(row, text="Create more", command=self.more).pack(side="left", padx=10)

    def on_show(self) -> None:
        paths = self.app.created
        self.text.configure(text=f"{len(paths)} cutover workbook(s) created:\n" + "\n".join(paths))
        self.notes.configure(text=("Notes:\n" + "\n".join(self.app.notes)) if self.app.notes else "")

    def open_folder(self) -> None:
        if not self.app.created:
            return
        folder = os.path.dirname(self.app.created[0])
        if sys.platform.startswith("win"):
            os.startfile(folder)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])

    def more(self) -> None:
        self.app.selected.clear()
        self.app.analyses.clear()
        self.app.show(1)


def run_gui(connect_fn: Optional[Callable[..., Tenant]] = None) -> None:
    App(connect_fn).mainloop()
