"""Drives the real Tkinter window against the fake tenant (needs a display; run with xvfb-run). Not part of pytest."""
import os
import subprocess
import sys
import tempfile
import time

os.environ["HOME"] = tempfile.mkdtemp()                      # settings go to a throw-away home
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btp_is_cutover import gui                                # noqa: E402
from btp_is_cutover.tenant import Tenant                       # noqa: E402
from tests import fake_tenant as ft                            # noqa: E402

SHOTS = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()
os.makedirs(SHOTS, exist_ok=True)
OUT = tempfile.mkdtemp()
session = ft.FakeSession()
app = gui.App(lambda b, t, c, s: Tenant.connect(b, t, c, s, session=session))


def pump(seconds=0.3):
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        time.sleep(0.02)


def wait(cond, timeout=15, what=""):
    end = time.time() + timeout
    while time.time() < end:
        app.update()
        if cond():
            return
        time.sleep(0.03)
    raise SystemExit(f"TIMEOUT waiting for {what}")


def shot(name):
    pump(0.4)
    path = os.path.join(SHOTS, name)
    subprocess.run(["import", "-window", "root", path], check=True)


app.geometry("1120x780+0+0")
pump(0.5)
connect, select, details, deps, done = app.pages

# 1 connect (empty form first)
connect.connect()
assert "required" in connect.msg.cget("text")
for k, v in {"base_url": "https://demo-tenant.it-cpi001.cfapps.eu10.hana.ondemand.com", "token_url": "https://demo.authentication.eu10.hana.ondemand.com/oauth/token",
             "client_id": "sb-demo-client!b12345", "secret": "demo-secret"}.items():
    connect.vars[k].set(v)
shot("1_connect.png")
connect.connect()
wait(lambda: app.current == 1, what="step 2")
assert app.tenant and len(app.tenant.packages) == 4
assert connect.vars["secret"].get() == "", "secret must be cleared"

# 2 select: browse a package, add two iFlows, then one by ID
select.pkg_filter.set("")
pump(0.2)
idx = next(i for i, p in enumerate(select.shown_packages) if p['Id'] == 'P_APP')
select.pkg_box.selection_set(idx)
select.open_package()
wait(lambda: select.flows, what="iFlow list")
select.flow_box.selection_set(0, 1)
select.add_selected()
select.nb.select(1)
select.ids_text.insert("1.0", "IF_TIMER\nNOT_THERE")
select.add_ids()
wait(lambda: "NOT_THERE" in select.ids_msg.cget("text"), what="ID lookup")
assert len(app.selected) == 3, list(app.selected)
select.nb.select(0)
shot("2_select.png")
select.next()
wait(lambda: app.current == 2, what="step 3")

# 3 details: mandatory fields
details.next()
assert "Interface ID" in details.msg.cget("text") and "Point of contact" in details.msg.cget("text")
shot("3_details_required.png")
for i, (flow, entry) in enumerate(details.rows, 1):
    entry.delete(0, "end")
    entry.insert(0, f"RICEFW-{1000 + i}")
details.fields["poc"].insert(0, "Jane Doe")
details.next()
wait(lambda: app.current == 3, what="step 4")

# 4 dependencies
wait(lambda: deps.nb is not None, timeout=30, what="analysis")
pump(0.5)
analysed = {f.name: app.analyses[f.key] for f in app.selected.values()}
oss = next(a for n, a in analysed.items() if "OSS" in n)
assert any(d.artifact == "VM_Global_IFlow_to_EMailRecipients" for d in oss.result.deps)
deps.nb.select(1)
shot("4_dependencies.png")
# add a dependency through the dialog code path
dlg = gui.AddDependencyDialog(app, lambda d: None)
dlg.pkg_var.set("P66_Global_GenericHandlers")
dlg.pick_package()
wait(lambda: dlg.items, what="dialog artifacts")
dlg.art_cb.current(0)
dlg.pick_artifact()
shot("4b_add_dependency.png")
dlg.destroy()

# 5 create
gui.filedialog.askdirectory = lambda **kw: OUT
deps.create()
wait(lambda: app.current == 4, what="step 5")
shot("5_done.png")
files = sorted(os.listdir(OUT))
print("files:", files)
assert len(files) == 3 and all(f.endswith(".xlsx") for f in files)
print("GUI smoke test OK")
app.destroy()
