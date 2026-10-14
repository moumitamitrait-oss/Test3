# BTP IS Cutover Wizard (Python)

Python version of the BTP IS Cutover Wizard browser extension. It connects to SAP Integration Suite, lets you pick
packages / iFlows, detects the common dependent artifacts and creates one **cutover workbook per iFlow**
(List of Dependent Artifacts, CutOver_&lt;ENV&gt;_&lt;Interface ID&gt;, Config Params with Parameter Name / DEV / QA / PROD).

## Install and start

* Python 3.9 or newer. Tkinter (the window) is part of the python.org installers; on Linux: `sudo apt install python3-tk`.
* `pip install -r requirements.txt`
* `python run_wizard.py` opens the wizard. On Windows you can double-click `run_wizard.bat` (it creates its own environment the first time).

## The wizard (same five steps as the extension)

1. **Connect** – API base URL, token URL, client ID, client secret of the service key (Process Integration Runtime, plan api). The secret is never saved.
2. **Select iFlows** – browse a package and add one or more iFlows, or enter iFlow IDs. The active version is used.
3. **Details** – Interface ID per iFlow (required), Point of contact (required), target environment, integration / functional / security team.
4. **Dependencies** – detected from the iFlow design; untick, edit, remove or add rows (package -> artifact dropdowns, the type is filled in).
   * references to script collections, mappings, value mappings, message types (the property name decides the type)
   * the shared artifacts used *inside* the message mappings (global script collections, value mappings, message types)
   * iFlows called through a ProcessDirect adapter
   * the default value mapping `VM_Global_IFlow_to_EMailRecipients` (change `DEFAULT_DEPS` in `btp_is_cutover/detect.py`)
5. **Create** – choose a folder; one `.xlsx` per iFlow is written there (existing files are never overwritten).

## Command line (no window)

```
python -m btp_is_cutover packages --base-url ... --token-url ... --client-id ...
python -m btp_is_cutover iflows --package P66_HVC_App --base-url ... --token-url ... --client-id ...
python -m btp_is_cutover create --config config.json
```

The client secret is read from the `BTP_IS_CLIENT_SECRET` environment variable, or asked for. The other API values can come from options,
the `api` block of the config file or `BTP_IS_BASE_URL`, `BTP_IS_TOKEN_URL`, `BTP_IS_CLIENT_ID`. See `config.example.json`
(`extra_dependencies` adds rows, `exclude_dependencies` unticks artifact IDs). Interface ID and Point of contact are required here as well.

## What goes into the workbook

* Steps: validate the common artifacts and credentials, e-mail recipient steps (when the e-mail value mapping is a dependency),
  scheduler timings from Business / Functional (SFTP sender or timer start), service-user check for the receiver credential,
  share / change the sender details with the `<System> Team` (not for ProcessDirect senders or timer-only iFlows), configure parameters, deploy, smoke test.
* Sender system = participant starting with `S_`, receiver system = the receiver that owns the credential; ProcessDirect channels never count.
* Credentials: only real aliases (values like `false` or `Basic` are ignored).
* Config Params: every parameter once (Sender, then Receiver, then the rest); DEV from the connected tenant, QA and PROD left empty.

## Notes

* Settings (URLs, client ID, teams, folder) are remembered in `~/.btp_is_cutover/settings.json` – never the secret. All API calls are read-only GET requests.
* Behind a proxy: `HTTPS_PROXY` is honoured; for an internal CA use `REQUESTS_CA_BUNDLE=/path/to/ca.pem`.
* The service key needs read access to design-time content; downloading iFlows / mappings may need an extra role. If an iFlow cannot be downloaded the
  workbook is still created, without sender / receiver / credential steps and dependencies (a note says so).
* How CPI names things inside iFlows and mappings (reference properties, message types) can differ per tenant: check the notes under the
  dependency table of step 4 and add anything missing with **+ Add dependency**.

## Tests

`pip install pytest` then `pytest` (uses a fake tenant, no network). GUI smoke test: `xvfb-run -a python tests/gui_smoke.py` (Linux) or run it on a desktop.

## Layout

`api.py` API client · `design.py` iFlow XML · `tenant.py` packages and caches · `cutover.py` cutover data · `detect.py` dependencies ·
`steps.py` cutover steps · `workbook.py` Excel · `service.py` analyse / generate · `gui.py` window · `cli.py` command line.
