# SAP BTP-IS Cutover Automation & Management Platform

A small Streamlit app for managing a Cloud Integration tenant, in two tabs
once you're connected:

## 📦 Parameter Management

Viewing, editing and deploying the externalized ("Configure"-screen)
parameters of one or more integration flows at once. Three steps, every time:

1. **Choose Package(s)** — filter by name or ID if there are a lot of
   them, or use **Select all** / **Deselect all** to act on everything
   the current filter is showing in one click. Unchecking a package
   automatically drops any integration flows you'd already picked from
   it too. The table shows `Select`/`Name`/`Version` by default — `Id`,
   `Vendor` and `Modified` are hidden but not gone, toggleable via the
   table's own column-visibility control (the icon above its top-right
   corner).
2. **Check the integration flow(s)** you want, from across every package
   you picked — up to 10 at once by default (adjustable, see below),
   filterable and select-all-able the same way. Shows
   `Select`/`Name`/`Package`/`Version` by default, with `Id` and
   `Description` hidden the same toggleable way.
3. See every checked iFlow's externalized parameters on one screen, each
   in its own section (the iFlow's own name shown in **bold**):
   - **Edit values directly** in a table, one row per parameter
   - **Load values from Excel** — a single sheet (pick which column has
     the name, which has the value), or a whole workbook with one sheet
     per iFlow and a column per environment (DEV / QA / PROD, or
     whatever yours are called) — say which environment's column to use,
     once, then click **Preview Values** to stage them into every
     matched iFlow's table below for review (nothing is written to the
     tenant yet at this point — that's still a separate, explicit Save).
     Each iFlow that had a matching sheet shows that sheet's own data —
     `Parameter Name` plus the environment column you picked, with any
     other environment columns in the same sheet hidden by default but
     still toggleable — right in that iFlow's own section, not a single
     generic preview of just one sheet elsewhere on the page. An iFlow
     with no externalized parameters is skipped with an explicit note,
     same as one with no matching sheet. Leading/trailing whitespace in
     both parameter names and values is trimmed automatically.
   - **Download the current values as Excel** — one tab per iFlow, named
     by its technical ID, with fixed `Parameter Name` / `DEV` / `QA` /
     `PROD` columns: the current value goes in whichever column matches
     the environment you're connected to, the other two stay blank ready
     to fill in. `SAP_ProfileId` and dropdown-constrained parameters
     (both see below) are left out, since this app never writes to
     either — there's no point downloading a row you can't use the
     round trip to actually change. Every other `SAP_`-prefixed
     parameter is a normal, writable value and *is* included. An "Index"
     sheet keeps the round trip correct even for iFlow IDs longer than
     Excel's 31-character tab-name limit.
   - **Save** and/or **deploy** each iFlow independently, both behind an
     explicit confirmation step showing exactly what's about to change
     and which environment it's going to
   - **Remove an iFlow from the screen** on the spot, without starting
     over, if you picked the wrong one

Picking packages and iFlows only ever calls read (`GET`) endpoints. Saving
parameter values and deploying genuinely write to your tenant — see
**Before you use the write features** below before pointing this at
anything other than DEV.

## 🔒 Security Artifacts Management

Viewing, adding and updating security material, one at a time or in bulk
via CSV. Pick a type from the dropdown — **User Credential**, **OAuth
Credential**, or **Secure Parameter** (**All**, a single combined view
across every type, is visible but not yet implemented) — then:

- **📋 View deployed credentials** — a table of everything of that type
  currently deployed, downloadable as CSV for reference. **The secret
  column (Password / Client Secret / Value) never has a real value in
  it** for any of the three types — SAP's API only ever returns it
  write-only once something is deployed, the same as Web UI's own Manage
  Security Material screen; this app shows an honest "not returned by
  API" placeholder rather than pretending otherwise.
- **➕ Add/Update a single credential** — the fields differ by type (see
  below), but submitting always works the same way: it checks whether
  something with this exact Name already exists. If so, it's **updated
  in place** (`PUT`); if not, a **new** one is **created** (`POST`) —
  either way the result (which one happened, success or failure, with
  the tenant's own reason) shows right there.
- **📤 Add/Update credentials in bulk (CSV)** — download a template, fill
  in a row per item (same columns as the single form), upload it, and
  every row becomes its own create-or-update call the same way the
  single form works — there's no batch endpoint on SAP's side for this,
  so that's genuinely what happens under the hood, once per row. A
  results table afterward shows **Added**, **Updated**, or **Failed** for
  every row individually, with the tenant's own reason for anything that
  failed (a row matching an existing Name is not a failure case).

**User Credential** fields: Name, Kind (`default` / `successfactors` /
`openconnectors`), Description, User, Password, and — only when Kind is
`successfactors` — Company ID, which the field appears for
automatically.

**OAuth Credential** fields: Name, Description, Client ID, Client Secret,
Token Service URL (the OAuth2 authorization server's token endpoint).

**Secure Parameter** fields: Name, Description, Value (the secret itself
— a password, API key, token, or similar single value with no separate
username).

This tab writes to your tenant's Security Material immediately on submit —
there's no separate confirmation dialog the way parameter edits and
deploys have, so double-check the connected environment in the sidebar
before adding or updating anything, especially in bulk.

**A real difference in confidence between the three types, worth being
upfront about**: User Credential's field names (`Name`, `Kind`,
`Description`, `User`, `Password`, `CompanyId`) were directly confirmed
against a real tenant via Postman early in this project. OAuth
Credential's and Secure Parameter's field names are this app's
best-informed reading of SAP's own public documentation for each
artifact type's Web UI attributes — genuinely researched, not a blind
guess, but **not independently confirmed against a real tenant's schema**
the same way. `OAuth2ClientCredentials` as the entity set name is an
exception to that — it's confirmed directly, since it's the exact path
from SAP's own Business Accelerator Hub documentation for this API;
`SecureParameters` is a best guess there too. If a create/update call to
either comes back rejected in a way that looks field-related (rather
than a validation message about a value you entered), that's the signal
something here needs correcting, not a bug in the request itself. Two
things in `sap_cpi_client.py` exist specifically to verify and fix this
against your real tenant without guessing again: call
`discover_security_entity_sets("parameter")` to confirm the real entity
set name if `SecureParameters` 404s, and
`discover_entity_properties("OAuth2ClientCredentials")` (or
`"SecureParameters"`) to see the tenant's own real field names directly,
then adjust the relevant client methods to match.

## 📝 Cutover Checklist Generator

Builds a cutover checklist `.xlsx`, filled in through the app, matching the
*exact* visual format of a real, user-supplied reference template
(`CutOver_QA_PTP-I-514_1_ACTIAN_2.xlsx`) — same three sheets, same header
fill colors per sheet, same green "section banner" rows, same column
widths, same Calibri throughout. Nothing here is a generic spreadsheet
export dressed up to look similar; every color, font size, border, and
column width below was measured directly from that file with `openpyxl`
before writing a single line of the generator.

- **Checklist title** — becomes the name of the main sheet (Excel's own
  31-character sheet-name limit is handled automatically, the same way
  the Parameter Management Excel export already handles long iFlow IDs).
- **Interface(s) for this cutover** — pick package(s) then integration
  flow(s) (searchable multiselects, since a real tenant can have
  hundreds of packages) — used only to pull real Config Params below,
  not required to build the rest of the checklist.
- **List of Dependent Artifacts** — add rows one at a time (Package,
  Artifact, System); each has its own ✕ to remove it. S.No is assigned
  automatically based on position.
- **Checklist Steps** — build the checklist as a sequence: add a
  **section banner** to start a new labeled phase (free text — e.g.
  "Integration Team", "Smoke Test(Post Cutover)", or anything else your
  own cutover needs), then add **steps** under it (Reference Artifact,
  Integration Package, Actions, Task execution time, Notes, Assignee
  Team, Point Of Contact, Status, Completion Date). Steps are numbered
  automatically in the order added; section banners don't consume a
  number, matching the reference template's own convention exactly. The
  Integration Package field offers your selected package(s)' real names
  as a quick pick, with a manual-entry fallback.
- **Config Params** — **📥 Load Config Params from selected iFlow(s)**
  pulls each selected iFlow's real, current externalized parameters
  (the same data and the same DEV/QA/PROD shape the Parameter Management
  Excel export already uses) into whichever column matches the
  environment you're currently connected to; fill in the other two by
  hand if you need all three populated before generating.
- **📥 Generate Cutover Checklist** — builds the complete 3-sheet
  workbook from everything above and offers it as a download. Requires
  a title and at least one checklist step; everything else is optional.

This tab's own session state (dependent artifacts, checklist entries,
loaded Config Params) is separate from Parameter Management's — picking
packages/iFlows here never affects, or is affected by, what's selected
in that other tab.

## 1. Get a service key from BTP Cockpit (once per environment)

The app authenticates the same way any external client does: OAuth2
client-credentials against a **Process Integration Runtime** service
instance.

1. In **SAP BTP Cockpit**, open the subaccount for the tenant (do this once
   for DEV, once for QA, once for PROD).
2. Go to **Services → Service Marketplace**, find **Process Integration
   Runtime**, and choose **Create**.
3. Pick the **api** plan. Give the instance a name and, if your tenant uses
   the newer "workspace packages" model, assign roles such as
   `WorkspacePackagesRead` (older tenants generally work with the default
   roles). If you're unsure, your Integration Suite administrator can confirm
   which role collection covers read access to Integration Content.
4. Once created, open the instance → **Create Service Key**.
5. Open the service key and note four values: `clientid`, `clientsecret`,
   `tokenurl`, `url`. You'll paste these into `environments.json` below.

Full click-by-click steps (with screenshots) are in SAP Help Portal under
*"Creating Service Instance and Service Key for Inbound Authentication"*.

## 2. Configure your environments

```bash
cp environments.example.json environments.json
```

Edit `environments.json` and paste in the four values from each service key:

```json
{
  "DEV":  { "clientid": "...", "clientsecret": "...", "tokenurl": "...", "url": "..." },
  "QA":   { "clientid": "...", "clientsecret": "...", "tokenurl": "...", "url": "..." },
  "PROD": { "clientid": "...", "clientsecret": "...", "tokenurl": "...", "url": "..." }
}
```

`environments.json` is git-ignored so real secrets don't get committed. You
can name the keys anything (not just DEV/QA/PROD) and add or remove entries
freely — the app just lists whatever is in the file.

Don't want a config file at all? Skip this step — the sidebar also has a
**"Connect with a service key"** panel where you can upload or paste a
service key JSON directly for a one-off session.

## 3. Set up who can log in

Before anyone can use the app at all, at least one login needs to exist.
Run this directly on the machine the app itself will run on:

```bash
python manage_users.py add <username>
```

This prompts for a password twice (to catch typos — it's never echoed to
the screen, and never accepted as a command-line argument, which would
otherwise land in shell history). Run `add` again for the same username
later to change their password; `python manage_users.py remove
<username>` removes someone; `python manage_users.py list` shows current
usernames (never passwords or hashes).

**This is the only place logins are managed** — there's deliberately no
"add a user" screen inside the running app itself, since exposing user
management through the same web UI the login screen protects would
defeat the point of having one. Passwords are never stored in plaintext;
only a salted PBKDF2-HMAC-SHA256 hash lives in `auth_users.json`
(gitignored, same as `environments.json`).

**Be clear-eyed about what this does and doesn't protect against.** This
app runs locally — it's not a hosted, multi-user web service. A
username/password check is a genuine deterrent against someone casually
opening the app and using it without a password. It is **not** a
substitute for real security: anyone with file access to this machine
could read the credential hashes directly (though never recover the
actual passwords from them), or simply edit `app.py` to remove the login
check entirely, since this is plain, readable Python, not a sandboxed or
compiled binary. For a tool gating production SAP cutovers, treat this
as a baseline, not a hard boundary. The properly robust answer is real
single sign-on against your organization's actual identity provider —
Streamlit's own `st.login()` (stable since v1.42, OIDC-based) supports
this directly once your identity team can register the app and hand back
a client ID, client secret, and metadata URL. See `auth.py`'s own module
docstring for the same explanation in the code itself.

Logging out (the sidebar's **Log out** button) clears the entire session
— not just the login status — so a different person logging in next
never inherits whatever environment was connected or whatever was
in-progress in the Cutover Checklist tab.

## 4. Install and run

```bash
pip install -r requirements.txt
streamlit run app.py
```

This opens the app in your browser (usually http://localhost:8501). Log
in, pick an environment in the sidebar, hit **Connect**, then check the
package(s) and integration flow(s) you want and continue through to
their parameters.

## Before you use the write features

Picking packages and iFlows (steps 1–2) is checkboxes in a table, each
with a filter box above it (matches on name or ID — and on package name
too, for the iFlow table) plus **Select all** / **Deselect all** buttons
that act on whatever the current filter is showing, not necessarily
everything on the tenant. Checked items stay checked even if you change
or clear the filter, including ones the current filter is hiding — what's
checked is tracked by the package/iFlow's actual ID, not by which row
it's sitting on, specifically so that filtering (or a bulk select/deselect)
can never silently swap which item a checkbox refers to. The iFlow table
is additionally capped at `MAX_BULK_IFLOWS` (10 by default, a constant
near the top of `app.py`) checked at once, since each one is a separate
call to the tenant once you continue.

Unchecking a package — individually or via Deselect all — automatically
drops any integration flows you'd already picked from it on the iFlow
screen, with a note on screen saying how many were removed and why. That
only ever removes things from the *current, unsaved selection*; it has
no effect on anything already saved to the tenant.

Step 3 — the parameters screen — is where everything writes to your
tenant:

- **Editing a parameter** (by typing directly into its table, or by
  loading it from Excel) sends an authenticated update straight to the
  active configuration — the same value the running integration reads.
  It does **not** require a redeploy to take effect (that's the whole
  point of externalization), so the change is live as soon as you confirm
  it. Each iFlow's own parameter table doesn't have a filter box (unlike
  the package/iFlow pickers above) — parameter lists per iFlow are
  normally short enough that it hasn't been worth adding the same
  Id-tracked-selection treatment there yet.
- **Loading from Excel** works two ways, both from the same "📥 Load
  values from Excel" panel on this screen:
  - A **single sheet**: tell the app which column has the parameter name
    and which has the value (it guesses from the header text, but you
    can always override). Matching is case/whitespace-insensitive, and a
    blank cell is skipped rather than blanking out a real value.
  - A **whole workbook**, one sheet per iFlow, matched one of two ways.
    If the workbook has an **"Index" sheet** (every file this app
    downloads includes one), it's used to match sheets to iFlows by
    their full technical ID — this is what makes the round trip work
    correctly even when an iFlow's ID is longer than Excel's 31-character
    sheet-name limit, where the data sheet's own name had to be
    shortened (and, if two different iFlows' IDs happen to shorten to
    the same 31 characters, disambiguated with a `_2`, `_3`, etc. suffix
    too). Without an Index sheet — a hand-built file, say — sheets are
    matched the simpler way instead: the sheet's name has to match that
    iFlow's name or technical ID directly. Either way, a sheet that
    doesn't match any checked iFlow is skipped with a warning naming
    which iFlow it couldn't find a sheet for, and **an iFlow with no
    externalized parameters at all is skipped outright with its own
    explicit note**, since there's nothing on its table to update
    regardless of whether a sheet was found. The rest of the import
    still goes ahead either way. Each data sheet needs a parameter-name
    column plus one column per environment (e.g. `DEV`, `QA`, `PROD`);
    the app asks, once, which environment's column to use, and that
    choice applies to every sheet in the upload. Nothing is written
    until you review and explicitly Save — loading from Excel only
    pre-fills the table.
- **Downloading the current values as Excel** (the button next to the
  import panel) produces the mirror image: one tab per checked iFlow,
  named by its technical ID (shortened and disambiguated as above, if
  needed), plus an **"Index" sheet first**, listing every iFlow's full ID
  next to whichever (possibly shortened) tab name it actually got — don't
  rename tabs or edit that sheet, since re-uploading the file later relies
  on it to match correctly. Every data sheet has the same four fixed
  columns, `Parameter Name` / `DEV` / `QA` / `PROD`, regardless of what
  columns your tenant's environments are actually called: the current
  value is written into whichever of those three matches the environment
  you're connected to right now, and the other two are left blank — a
  ready-made starting point for filling in the other environments' target
  values before re-uploading. If your connected environment's name
  doesn't match DEV/QA/PROD, a note appears explaining that all three
  columns came back blank, so that isn't mistaken for a bug. `SAP_ProfileId`
  (see below) is left out of the download entirely, not just flagged —
  there's no point including a row you can't write to anyway. Every
  other `SAP_`-prefixed parameter is included normally.
- **Deploying** makes the chosen version the one that's actually running.
  It's asynchronous — the app tells you the request was accepted, not that
  it's finished — so check the Monitor tab in Web UI afterward to confirm.
- **Removing an iFlow** from this screen (the "✕" button in its section)
  just takes it out of the current view — it doesn't undo or revert
  anything already saved, and you can always get it back via the iFlow
  picker.

Saving and deploying are each **per iFlow**, not all-or-nothing — one
iFlow's Save button only ever touches that iFlow's own changes, and both
are behind a confirmation dialog: the save one lists every parameter about
to change, old → new, no matter how many you edited (by hand or via
Excel) in one go. Both dialogs show which environment you're connected
to, and — specifically when you're connected to **PROD** — an extra
checkbox you have to tick before the confirm button is even clickable.
There's no "undo" button here: if you save or deploy, that's real,
immediately. A couple of practical habits worth it:

- Try any new change against **DEV** first, the same way you would through
  the Web UI directly.
- Double-check the environment badge in the sidebar before confirming
  anything — that's the one piece of context that's easy to lose track of
  if you've been switching between tenants in the same session.
- If a service key is meant to be read-only for safety, don't add write
  roles to it — attempting a write with it will fail cleanly with a clear
  CSRF/permissions error rather than silently doing nothing, but it's
  simpler to just not have the capability available in the first place
  on a service key you don't want used for writes.

## Notes and limitations

- **Dates are shown in this machine's local time, as `DD-MM-YYYY`**: the
  `Modified` column (package picker) and `Deployed On` column (Security
  Artifacts Management) both come from the tenant as UTC timestamps,
  converted to whatever timezone the machine running this app is
  actually set to before being formatted — so the same timestamp can
  display differently if you run this on a different machine.
- **Only `SAP_ProfileId` specifically is flagged as SAP-managed — every
  other `SAP_`-prefixed parameter is a normal, fully editable one**: the
  `SAP_` naming convention alone isn't a reliable signal that a parameter
  is read-only. `SAP_ProfileId` is the one confirmed, on a real tenant,
  to reject writes with an HTTP 400; other `SAP_`-prefixed externalized
  parameters have turned out to be ordinary, writable configuration
  values despite the prefix, so they're treated exactly like any other
  parameter — no flag, no special handling, fully included in Excel
  import/export. For `SAP_ProfileId` specifically, the app still shows a
  heads-up wherever it appears, and still lets you try editing it: if the
  tenant rejects that write (HTTP 400), it shows up as a **yellow
  warning**, not a red error, and any other parameters in the same batch
  still save normally regardless. A 400 on any other parameter is treated
  as a genuine error, since that's not the expected pattern there and is
  worth a closer look. `SAP_ProfileId` is excluded from the Excel
  download entirely (see above) for the same reason. If a *different*
  `SAP_`-prefixed parameter turns out to also reject writes on some
  tenant, `SAP_RESTRICTED_PARAMETER_NAMES` near the top of `app.py` is
  the one place to add it.
- **Parameters that correspond to a dropdown-only field in the real SAP
  CPI adapter UI are flagged, and any edit to them is blocked, not just
  warned about**: a parameter whose name matches a known
  dropdown-constrained field (`Proxy Type`, `Authentication`, `Connection
  Type`, `Transport Protocol`, `Message Protocol`, `Processing
  Type`/`Processing Mode`, `Adapter Type` — case/spacing-insensitive) is
  flagged `⚙️ SAP-defined dropdown value` in the table. The real SAP CPI
  UI only ever lets you pick from a fixed dropdown here — this app's
  free-text table has no equivalent restriction. Unlike `SAP_ProfileId`
  (which is still attempted and may come back rejected by the tenant),
  these are blocked by the app itself before ever reaching the tenant:
  still visible and typeable for awareness, but **the actual Save
  silently excludes any change to one**, regardless of whether it came
  from typing directly into the cell or from a matched Excel upload, and
  neither the Excel import nor the Excel download will stage or include
  one at all. Typing a
  different value here shows an explicit warning naming exactly which
  field(s) were touched and stating plainly that the change will not be
  saved — change these from the real SAP CPI Configure screen instead,
  where only genuinely valid options can be selected. This list is
  confirmed against SAP's own documentation for the fields named above,
  but is still necessarily incomplete: this API has no way to say which
  externalized parameters are dropdown-constrained or what their allowed
  values are, only a name, a current value, and a generic data type. If
  your tenant has a dropdown field under a different name,
  `KNOWN_DROPDOWN_PARAMETER_NAMES` near the top of `app.py` is the one
  place to extend it.
- **Externalized parameters use the active (deployed) version**: the
  Configure screen's values are tied to what's actually deployed, which
  isn't always the same version number shown in the iFlow list (that's the
  saved workspace version, and can be ahead of what's deployed). The app
  always asks for the active version first and only falls back to the
  saved version — with an on-screen note — if the iFlow has never been
  deployed.
- **Permissions**: if packages load but come back empty, or you see an HTTP
  403, the service instance most likely needs a read role for Integration
  Content assigned in BTP Cockpit — this varies by tenant edition, so check
  with your Integration Suite administrator.
- **iFlow-type coverage**: the `IntegrationDesigntimeArtifacts` entity set
  returns classic Integration Flows. Newer artifact types exposed through
  different design paradigms (e.g. some REST API artifacts) aren't covered
  by this entity set yet on SAP's side, so they may not appear even though
  they exist in the package.
- **Read vs. write roles**: the 403 above is about *reading* Integration
  Content. Writing (editing a parameter, deploying) typically needs a
  broader role on the same service instance — if edits or deploys fail
  with a permissions-looking error even though browsing works fine, that's
  the first thing to check with your BTP administrator.
- **How writes work, under the hood**: SAP's OData service requires a CSRF
  token (fetched via a GET with `X-CSRF-Token: Fetch`) before it accepts
  any POST/PUT, and a parameter update specifically goes through the
  `$batch` endpoint with a single embedded PUT — both read-only GETs and
  this write flow are implemented in `sap_cpi_client.py` if you want to
  see exactly what's sent.
- **Security Artifacts Management only covers User Credentials so far**:
  Secure Parameters and OAuth Credentials are visible in the type dropdown
  but intentionally not implemented yet — their exact field layouts
  weren't something that could be confirmed with the same confidence as
  User Credential's (which matches a verified example from SAP's own
  community forum). Extending to them is mostly a matter of confirming
  their field names and wiring up the same create/list pattern already
  used for User Credentials in `sap_cpi_client.py`.
- **Security Material writes have no confirmation dialog**: unlike saving
  a parameter or deploying, adding a credential (single or bulk) happens
  immediately on submit — there's no "review before you commit" step the
  way the rest of this app has. Worth double-checking the connected
  environment before using this tab, same advice as above but more so,
  since there's one less safety net here.
- **Creating a User Credential treats HTTP 202 as success**: confirmed
  against a real tenant, adding a credential returns `202 Accepted`, not
  `201 Created` — the same "accepted, now processing" convention
  `deploy_iflow()` already accounts for elsewhere in this client. A
  credential that visibly deploys correctly while the UI still shows a
  red "could not add" error is exactly this — a client treating only
  200/201 as success would flag a perfectly good 202 as a failure.
  Updating one (`PUT` to `UserCredentials('<Name>')`, confirmed as the
  right request shape against a real tenant's own example) accepts
  200/202/204 for the same reason, though which one a real tenant
  actually returns for this specific call hasn't been directly confirmed
  the way create's 202 was — the wider set is a deliberate hedge against
  the same kind of surprise, not a confirmed fact.
- **Listing User Credentials never sends `$top`/`$skip`**: `UserCredentials`
  (plural) is the correct, confirmed entity set name — verified directly
  against a real tenant via Postman, which succeeds with a bare request
  and no query parameters at all. That same tenant 404s on this specific
  entity set when `$top`/`$skip` **are** present, the same kind of
  per-endpoint quirk already seen with `Configurations` elsewhere in this
  app (which also had to drop pagination params) — so
  `list_user_credentials()` calls a plain, unpaginated GET rather than
  the paginating helper used for larger entity sets like packages. It
  also falls back to the singular `UserCredential` if even that 404s,
  purely as an extra safety net for some other tenant that might
  genuinely use a different name. If credentials still don't show up
  after all that, the app automatically checks the tenant's own
  `$metadata` for anything resembling a credentials entity set and shows
  what it finds (or doesn't) right alongside the error, with a
  **"Download diagnostic log"** button to save the tenant URL, the exact
  error, and that `$metadata` check as a plain-text file — found nothing
  at all there points toward a missing role/permission on the service
  key for Security Content specifically (a separate role from
  Integration Content), rather than a naming or pagination issue.

## Files

| File                          | Purpose                                              |
|-------------------------------|-------------------------------------------------------|
| `app.py`                      | Streamlit UI                                          |
| `sap_cpi_client.py`           | OAuth2 + OData API client (no Streamlit dependency)    |
| `auth.py`                     | Password hashing/verification for the login gate       |
| `manage_users.py`             | CLI for adding/removing logins — run this, not the app |
| `environments.example.json`   | Template — copy to `environments.json` and fill in     |
| `requirements.txt`            | Python dependencies                                    |
