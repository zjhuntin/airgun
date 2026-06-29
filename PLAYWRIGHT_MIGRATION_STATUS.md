# Airgun: Selenium to Playwright Migration

## Where We Are

We rewrote airgun to run on Playwright instead of Selenium. The core migration is finished — every import, every API call, every widget has been converted. There are zero Selenium references left in the codebase. We are now in the testing phase, running real UI tests against a Satellite instance to find whatever we missed.

13 UI tests pass end-to-end against a live Satellite 6.19 instance. One known bug remains: the ACE code editor doesn't reliably save content through form submission under Playwright's faster execution model.

**Branches:**
- Airgun: `zjhuntin/airgun` branch `playwright-migration` (6 commits, pushed)
- Robottelo: branch `playwright-migration` (1 commit + local config changes)

---

## What We Did

The migration touched 101 files across airgun. The diff is roughly 1,580 lines added and 1,331 removed. Here is what changed and why.

### Replaced the entire browser engine

The old stack was Selenium WebDriver managed by webdriver-kaifuku's `BrowserManager`, which handled browser lifecycle, session recovery, and Selenium Grid connections. All of that is gone.

In its place is a `PlaywrightBrowserFactory` in `browser.py` (545 lines total). It manages Playwright's browser, context, and page objects directly. The factory handles:

- **Browser launch** — Chromium by default, with configurable headless mode and `slow_mo` for debugging.
- **Cookie-based sessions** — Uses `context.add_cookies()` instead of Selenium's `webdriver.add_cookie()`.
- **Video recording** — Playwright's built-in `record_video` context option, configured through robottelo's settings. No more external video capture.
- **Screenshot capture** — `page.screenshot()` instead of `webdriver.save_screenshot()`.
- **Page safety checks** — The `AirgunBrowserPlugin` still runs the same JavaScript checks for jQuery, AJAX, Angular, and Satellite-specific spinners. The JS itself is unchanged; only the execution method changed from `execute_script()` to Playwright's `page.evaluate()`.

### Upgraded the widget libraries

Three dependency changes:

1. **Removed `widgetastic.patternfly` (PF3)** — The original PatternFly 3 widget library. Selenium-only, no Playwright port exists.
2. **Removed `widgetastic.patternfly4`** — PF4 is end-of-life upstream. Also Selenium-only.
3. **Upgraded `widgetastic.core` from v1.x to v2.x** — Version 2 speaks Playwright natively. It provides the `Browser`, `Widget`, `View`, `Locator`, and `Text` primitives that airgun builds on.
4. **Added `widgetastic.patternfly5` v26.x** — PF5 equivalents for buttons, dropdowns, tables, tabs, pagination, switches, navigation, and OUIA components.

Every PF3 and PF4 import across 44 view files was replaced with a PF5 equivalent. The imports changed but the widget names stayed the same in most cases (`Button`, `Dropdown`, `Pagination`, `Select`, `Tab`, etc.), so view code required minimal changes beyond the import lines.

Four PF3 widgets had no PF5 equivalent. We reimplemented them locally in `widgets.py`:
- **`FlashMessage` / `FlashMessages`** — Satellite's flash notification bar. Reimplemented with XPath locators matching the actual Satellite HTML.
- **`Kebab`** — Three-dot action menu. Reimplemented as a simple click-to-open, select-item widget.
- **`AggregateStatusCard`** — Dashboard summary cards. Reimplemented with the Satellite-specific HTML structure.

### Fixed XPath scoping (694 locations)

This was the single largest category of changes. Selenium evaluates `//` XPath from the document root regardless of context. Playwright scopes `//` to the parent element's subtree. Every `//` in a widget locator that was meant to search from the document root needed to become `.//` to keep the same behavior under Playwright.

We converted these in two passes:

**Pass 1:** Bulk `//` to `.//` conversion across all view and widget files. This fixed most locators but introduced a subtle bug in about 15 locations.

**Pass 2:** Fixed multi-line Python string concatenation breakage. In Python, adjacent string literals concatenate implicitly:

```python
locator = (
    './/div[contains(@class, "main")]'
    '//table[@id="results"]'
)
```

This produces `.//div[contains(@class, "main")]//table[@id="results"]` — valid XPath. But the bulk conversion changed it to:

```python
locator = (
    './/div[contains(@class, "main")]'
    './/table[@id="results"]'
)
```

Which produces `.//div[contains(@class, "main")].//table[@id="results"]` — invalid XPath. The leading `.` on continuation lines had to be removed. We also had to distinguish this from cases inside XPath predicates like `label[.//strong[...]]`, where `.//` is correct and must be preserved.

### Fixed widgets that broke under Playwright

Several airgun widgets made assumptions about browser behavior that Selenium tolerated but Playwright does not.

**SatTab** — Satellite uses two tab styles: PF5 tabs and older Rails-style Bootstrap tabs. PF5 tabs use `<button>` elements with `aria-selected`. Bootstrap tabs use `<a>` elements inside `<li>` with an `.active` class. The PF5 `Tab` widget from widgetastic-patternfly5 only handles the PF5 case. We rewrote `SatTab.click()` and `SatTab.is_active` to detect which style is present and handle both.

**FilteredDropdown / Select2** — Select2 dropdown menus are portaled to the document body. When you click a Select2 widget, the menu appears as a direct child of `<body>`, outside the parent widget's DOM subtree. Under Selenium, `//` searched the whole document, so locators found these menus. Under Playwright, `.//` only searches within the parent, so the menus became invisible. We fixed this by adjusting the click target (clicking the container instead of the arrow element) and using document-root locators for the portaled menu.

**SatSelect** — Several `SatSelect` widgets use inline JavaScript to manipulate form values. Selenium's `execute_script` accepts `function(){}` syntax. Playwright requires arrow functions `() => {}`. We converted all of them.

**Button** — PF5's `Button('Submit')` widget looks for PF5-specific CSS classes (`pf-v5-c-button` or `pf-c-button`). Bootstrap buttons don't have these classes, so `Button('Submit')` fails on Rails-style pages. We converted approximately 100 Bootstrap buttons across the views to `Button(locator='.//button[normalize-space(.)="Submit"]')`, which matches by text without checking CSS classes. This preserves button semantics while working on both PF5 and Bootstrap pages.

**ActionsDropdown, FieldWithEditButton, CVESelect** — Various locator and interaction fixes. The portaled-menu problem affected several dropdown-style widgets beyond Select2.

### Fixed Selenium API calls in entity methods

Grep'd the entire codebase for Selenium-specific method calls that don't exist on Playwright Locators:

- **`get_property('innerHTML')`** — Selenium WebElement method. Replaced with `inner_html()` in `provisioning_template.py` and `report_template.py`.
- **`is_selected()`** — Selenium method for checkboxes. Replaced with `is_checked()` in `cloud_vulnerabilities.py`.
- **`save_downloaded_file()`** — The old implementation scraped `chrome://downloads` using shadow DOM JavaScript. The new implementation accepts an optional `trigger` callable. When provided, it wraps the trigger action inside Playwright's `expect_download()` context manager, which reliably captures the download. Nine entity files were updated to pass their click action as a trigger lambda.

### Fixed text extraction

Selenium's `.text` property returns visible text — it strips hidden elements, collapses whitespace, and respects CSS `display: none`. Playwright's `text_content()` returns raw text content including hidden elements. This caused test assertions to fail because they were comparing against visible text.

We overrode `AirgunBrowser.text()` to use `inner_text()` instead of `text_content()`. `inner_text()` matches Selenium's visible-text behavior. One exception: SVG elements, where `inner_text()` is undefined in the DOM spec. For SVG nodes, we fall back to `text_content()`.

### Updated robottelo's config and harness

- **`configure_airgun()`** in `robottelo/config/__init__.py` — Now passes Playwright settings (browser type, headless mode, `slow_mo`, video recording, video path) instead of Selenium Grid URLs and Chrome options.
- **`conftest.py`** — Removed the `video_cleanup` pytest plugin (Selenium-era video handling).
- **`Dockerfile.playwright`** and **`Jenkinsfile.playwright`** — Built for CI, not yet validated with a real pipeline run.

---

## What Works

These tests ran end-to-end against a live Satellite 6.19 instance and passed. Each test exercises the full stack: Playwright browser, airgun session, navigation, view rendering, widget interaction, form filling, table reading, and flash message validation.

| Test File | Tests | All Passed |
|-----------|-------|------------|
| `test_bookmarks` | 3 (UserGroup, Product, ProvisioningTemplate) | Yes |
| `test_settings` | 1 (login_text) | Yes |
| `test_lifecycleenvironment` | 1 (end to end) | Yes |
| `test_activationkey` | 1 (CRUD) | Yes |
| `test_reporttemplates` | 1 (end to end, including file download) | Yes |
| `test_user` | 8 | Yes |
| `test_location` | 3 | Yes |
| `test_media` | 1 (end to end) | Yes |
| `test_subnet` | 1 (end to end) | Yes |

Five additional tests failed during fixture setup due to missing settings values (`offline_token`, `libvirt.hostname`), a CDN registration error, and a nailgun API incompatibility. None of these failures touch airgun or Playwright — they fail before the browser even opens.

---

## Test Coverage: What We Ran and What We Didn't

There are 73 UI test files in robottelo. We ran tests from 10 of them. The other 63 have not been touched. Here is a full accounting of both groups and the reasoning behind the selection.

### Why these 10 files

We chose these files as Wave 1 because they exercise the most common widget patterns — text inputs, dropdowns, tables, tabs, flash messages, and form submission — without requiring complex infrastructure or exotic view layouts. The goal was to validate that the core migration works broadly across many different pages, not deeply within a single page. If a FilteredDropdown fix works on activation keys, it works on domains. If SatTab renders correctly on users, it renders correctly on operating systems. Running one straightforward test from each of 10 different pages covers more surface area than running 30 tests from a single page.

Each file also had at least one test that could run with minimal Satellite configuration — no LDAP server, no compute resources, no content synced from CDN, no puppet modules installed.

### Files tested (10 of 73)

| File | Tests run | Tests in file | What it validated |
|------|-----------|---------------|-------------------|
| `test_activationkey` | 1 | ~30 | CRUD form with FilteredDropdown (Select2), table search, LCE selector |
| `test_bookmarks` | 3 | 5 | Bookmark creation across three different entity pages, modal dialogs |
| `test_lifecycleenvironment` | 1 | 6 | LCE path creation, drag-style workflow |
| `test_location` | 3 | 4 | CRUD with multi-select widgets, org/location context switching |
| `test_media` | 1 | 1 | Simple CRUD, text inputs, flash message validation |
| `test_reporttemplates` | 1 | ~23 | End-to-end including file download (validates `expect_download()` trigger pattern) |
| `test_settings` | 1 | ~16 | Settings page read/write, inline edit widget |
| `test_subnet` | 1 | 1 | CRUD with SatTab (Bootstrap-style tabs), multiple tab forms |
| `test_user` | 8 | 12 | CRUD, role assignment, password change, table pagination, admin flag |
| `test_jobtemplate` | 0 passing | 2 | ACE code editor — both tests fail due to form sync bug (see below) |

**Total: ~20 tests run across 10 files. 20 passed, 1 failed (ACE editor), 5 fixture errors (infrastructure, not Playwright).**

### Files not tested (63 of 73) and why

These 63 files fall into five groups based on what blocks them or why they were deferred.

**Group 1: Simple views we haven't reached yet (19 files)**

These use the same widget patterns as the Wave 1 files — CRUD forms, tables, dropdowns, tabs. They should work without new fixes. They were deferred only because we had not yet gotten to them, not because we expect problems.

- `test_architecture` — simple CRUD
- `test_audit` — read-only table view
- `test_branding` — read-only page checks
- `test_config_group` — simple CRUD
- `test_containerimagetag` — table view
- `test_dashboard` — read-only widgets
- `test_documentation_links` — link validation
- `test_domain` — CRUD with parameters tab
- `test_eol_banner` — banner display check
- `test_fact` — read-only table
- `test_hardwaremodel` — simple CRUD
- `test_http_proxy` — CRUD with auth fields
- `test_operatingsystem` — CRUD with SatTab
- `test_product` — CRUD with sync plan
- `test_role` — CRUD with permission assignment
- `test_search` — search bar behavior
- `test_syncplan` — CRUD with date/time picker
- `test_usergroup` — CRUD with role/user assignment
- `test_subscription` — table view, manifest upload

**Group 2: ACE editor dependent (4 files)**

These create or edit templates through the ACE code editor widget. They will fail with the same form sync bug that blocks `test_jobtemplate`. Fixing the ACE editor unblocks all four at once.

- `test_jobtemplate` — already tested, fails on editor content
- `test_partitiontable` — creates partition tables with editor content
- `test_provisioningtemplate` — creates provisioning templates with editor content
- `test_templatesync` — imports and exports templates

**Group 3: Complex views with unique widget patterns (14 files)**

These use view patterns we have not yet validated — `ConditionalSwitchableView`, nested dynamic tabs, composite content views, OUIA components, or multi-step workflows with modal dialogs. They are the most likely source of new widget bugs.

- `test_ansible` — role assignment, variable management, mixed PF4/PF5 patterns
- `test_contentcredentials` — file upload widget
- `test_contentview` — composite content views, filter creation, publish/promote workflows with modals
- `test_discoveredhost` — discovery rule matching, provision workflow
- `test_discoveryrule` — conditional form sections
- `test_errata` — complex table with expandable rows, host assignment
- `test_host` — the most complex view in airgun (ConditionalSwitchableView, nested tabs, dynamic form sections that change based on OS and compute resource selection)
- `test_hostcollection` — collection management, host assignment
- `test_hostgroup` — inheritance-aware forms where values cascade from parent groups
- `test_ldap_authentication` — modal-heavy flow with test connection, requires LDAP server
- `test_modulestreams` — stream management tables
- `test_oscappolicy` — multi-step wizard with conditional steps
- `test_organization` — similar to location but with more complex context switching
- `test_registration` — multi-step registration workflow

**Group 4: Requires specific infrastructure (15 files)**

These tests need infrastructure beyond a basic Satellite instance — compute resource providers, content synced from CDN, puppet modules, RHEL content hosts, or cloud service credentials. They cannot run in our current test environment.

- `test_capsulecontent` — requires a configured capsule server
- `test_computeprofiles` — requires a compute resource connection
- `test_computeresource_azurerm` — requires Azure credentials and subscription
- `test_computeresource_ec2` — requires AWS credentials
- `test_computeresource_gce` — requires GCE service account
- `test_computeresource_libvirt` — requires libvirt hypervisor host
- `test_computeresource_ocpv` — requires OpenShift Virtualization cluster
- `test_computeresource_vmware` — requires vCenter connection
- `test_flatpak` — requires flatpak remote configuration
- `test_imagemode` — requires bootc image infrastructure
- `test_leapp_client` — requires RHEL content host for upgrade testing
- `test_oscapcontent` — requires SCAP content uploaded
- `test_oscaptailoringfile` — requires SCAP tailoring files
- `test_puppetclass` — requires puppet modules installed
- `test_puppetenvironment` — requires puppet environment configured

**Group 5: Cloud and remote execution (11 files)**

These interact with external services — Red Hat Cloud, remote execution on hosts, content syncing — and require API tokens, registered hosts, or content repositories that our test Satellite does not have configured.

- `test_acs` — alternate content sources, requires repo configuration
- `test_package` — package management, requires synced content
- `test_remoteexecution` — requires a host with remote execution configured
- `test_repositories` — requires repository sync configuration
- `test_repository` — requires content setup
- `test_rhc` — requires rhc client configuration
- `test_rhcloud_insights` — requires Insights registration and `offline_token` setting
- `test_rhcloud_insights_vulnerability` — requires Insights vulnerability data
- `test_rhcloud_inventory` — requires cloud inventory registration
- `test_rhcloud_iop` — requires Insights Optimization Platform data
- `test_sync` — requires repository sync setup

### What the coverage tells us

The 10 tested files prove the migration works for the bread-and-butter UI patterns: CRUD forms, table operations, dropdowns, tabs, flash messages, navigation, and file downloads. These patterns account for the majority of airgun's widget usage across all 73 files.

The 63 untested files break down into work that is straightforward but not yet done (19 files), work blocked by the ACE editor bug (4 files), work that will likely require new widget fixes (14 files), and work that cannot run without specific infrastructure we do not currently have configured (26 files). The first two groups are next in line. The complex views are where the remaining bugs will hide. The infrastructure-dependent tests come last, once the foundation is proven solid.

---

## What Doesn't Work Yet

### The ACE code editor doesn't save content reliably

This is the one confirmed Playwright regression. `test_jobtemplate::test_positive_end_to_end` fails consistently (reproduced three times).

The ACE editor widget uses JavaScript to set content:

```python
self.browser.execute_script(f"ace.edit('{id}').setValue(arguments[0])", value)
```

This updates ACE's in-memory buffer. But Satellite's template form has a separate change listener that syncs the buffer to a hidden `<textarea>` form field. Under Selenium, execution was slow enough that this listener fired before the test clicked Submit. Under Playwright, `setValue()` and the Submit click happen so fast that the listener never runs, and the form sends the old content.

We tried three approaches:
1. **Triggering ACE's internal change signal** — `editor.session._signal('change')` crashed ACE's `onDocumentChange` handler because it expects a structured delta object describing what changed, not a bare signal.
2. **Dispatching DOM events on the textarea** — Dispatching `change` and `input` events on the backing textarea caused a feedback loop where the value was written twice.
3. **`press_sequentially()` (current approach)** — Click the editor's textarea, select all with Ctrl+A, then type the new value character by character using Playwright's `press_sequentially(delay=5)`. This fires every keydown/keypress/keyup event, which should trigger ACE's internal change listeners and Satellite's form sync handler. Implemented but not yet validated with a test run.

**What this blocks:** Any test that creates or edits template content through the ACE editor. This includes job templates, provisioning templates, partition tables, and report templates (though report template tests that only read templates still pass).

### Complex views haven't been tested

The tests we've run cover straightforward pages — forms with text inputs, dropdowns, tables, and tabs. We have not yet tested the views that have the most complex widget interactions:

- **Host views (`host.py`, `host_new.py`)** — `ConditionalSwitchableView` changes the form layout based on selected operating system or compute resource. Nested tab structures. Dynamic form sections that appear and disappear. These are the most complex views in airgun (84 and 151 lines of diff respectively).
- **Content view (`contentview_new.py`)** — Composite content view management, filter creation, publish and promote workflows with modal dialogs.
- **Compute resource views (`computeresource.py`)** — Each provider (VMware, Libvirt, EC2, GCE, Azure) has a unique form layout. The `ConditionalSwitchableView` widget selects which sub-view to display based on the provider dropdown.
- **Ansible views** — Role assignment, variable management, mixed PF4/PF5 patterns.
- **Hostgroup views** — Inheritance-aware forms where values cascade from parent groups.
- **LDAP authentication** — Modal-heavy flow with test connection functionality.

These will almost certainly surface new widget issues. The simple views proved the foundation works. The complex views will test the edges.

### Download handling works, but only with the trigger pattern

The `save_downloaded_file()` method works reliably when callers pass a `trigger` callable. Nine entity files have been updated to use this pattern. However, the fallback path (event-listener based, for cases where the caller doesn't pass a trigger) is less reliable because the `page.on('download')` event may not fire before the download is checked. Any entity that still uses the old calling convention without a trigger will need to be updated if downloads fail.

### No migration guide exists

Other airgun contributors need to understand the patterns that changed. These are the non-obvious things that will trip people up:

- XPath scoping: when to use `//` vs `.//`, and the multi-line string concatenation trap.
- `Button(locator=...)` vs `Button('text')`: which to use on Bootstrap vs PF5 pages.
- `expect_download()` trigger pattern: why the old event-listener approach doesn't work.
- `inner_text()` vs `text_content()`: why text extraction works differently and the SVG exception.
- `execute_script` differences: arrow functions required, `arguments[0]` still works through widgetastic v2's compatibility wrapper.

### CI pipeline hasn't been validated

We built a `Dockerfile.playwright` and a `Jenkinsfile.playwright` for running tests in CI. The container image is designed to run on the private registry at `images.paas.redhat.com`. But we haven't triggered an actual Jenkins pipeline run. This was deferred because validating CI infrastructure while the tests themselves are still being debugged adds noise without value.

---

## Known Risks Ahead

Beyond the ACE editor bug and the untested complex views, a code-level audit identified several patterns that are likely to cause failures as testing expands. These are problems we have not hit yet but expect to.

### Timing: `time.sleep()` used as a synchronization mechanism

There are 37 `time.sleep()` calls across 12 entity files and `widgets.py`. These exist because `ensure_page_safe()` does not cover every kind of UI state change — it checks jQuery, AJAX, Angular, and spinners, but not React re-renders, dropdown animations, or table reloads.

Under Selenium, these sleeps were usually long enough because Selenium itself added latency. Playwright is faster, so two things will happen: some sleeps will be too short (the UI is not ready when the sleep ends), and others will be unnecessarily long (wasting test time waiting for something that finished instantly).

The worst cases:

- **`job_invocation.py`** — Three methods use `time.sleep(3)` both before and after constructing a view object, with no condition-based wait. The view may be instantiated before the page has rendered.
- **`contentview_new.py`** — `time.sleep(5)` with a comment saying it waits for a "Loading" widget. Should be replaced with a wait for the loading indicator to disappear.
- **`host.py`** and **`host_new.py`** — Multiple `sleep(2)` and `sleep(3)` calls guarding form interactions and table reads after search.
- **`SearchInput.fill()`** in `widgets.py` — Hardcoded 1-second sleep before pressing Enter and 3-second sleep after, to work around a debounce bug (BZ#2140636). Neither sleep is tied to actual DOM state.

The fix for each of these is the same: replace the sleep with `wait_for(lambda: <condition>, timeout=<seconds>)`. But each one requires figuring out what the right condition is for that specific interaction.

### Timing: ActionsDropdown animation race

`ActionsDropdown.open()` checks whether the dropdown is open by reading CSS classes (`open` or `pf-m-expanded`), then the `items` property immediately queries the dropdown menu's DOM children. Under Playwright, the click that opens the dropdown and the CSS class check can complete before the dropdown animation finishes rendering the menu items. The `select` method is worse — it calls `open()`, reads items, closes, then opens again and clicks. Under Playwright's speed, the second open can fire before the first close completes.

This affects virtually every entity in the codebase. Any test that uses a kebab menu, an actions dropdown, or a bulk action menu could hit this.

### Timing: ContextSelector (org/location switching)

`ContextSelector.select_org()` and `select_loc()` click the selector, then immediately query for the target organization or location in the dropdown. There is no wait for the dropdown menu to render. This runs at the start of almost every test session, so if it fails, it fails early and loudly.

### Dialog handling: `cancel=True` is silently broken for native browser dialogs

The Playwright migration installed a `page.on('dialog')` handler that auto-accepts every native browser dialog immediately. This is required by Playwright — unhandled dialogs are auto-dismissed. But it means `handle_alert(cancel=True)` cannot dismiss a native `confirm()` dialog, because the dialog has already been accepted by the time `handle_alert` runs.

In practice, Satellite almost exclusively uses PatternFly modals (DOM elements, not native browser dialogs), so this may never trigger. But if any Satellite page uses a native `confirm()` where dismissal is the correct action, the test will silently do the wrong thing — accept instead of cancel — and pass with incorrect behavior.

49 call sites across 38 entity files use `handle_alert()` or `click(handle_alert=True)`. All of them work correctly for PF modals. Only the native dialog path is broken.

### ConditionalSwitchableView: no-wait reference widget reads

`ConditionalSwitchableView` is a widgetastic descriptor that reads a reference widget (a dropdown, checkbox, or radio group) on every access, then returns the matching sub-view. There are 30 CSV declarations across 11 view files, with 77 registered sub-views.

The risk is that CSV's `__get__` method calls `reference_widget.read()` with no explicit wait. If the reference widget has not rendered yet — which happens when a dropdown selection triggers a form layout change — the read fails immediately. Selenium's implicit waits may have masked this. Playwright has no implicit waits.

The heaviest users are `repository.py` (12 CSVs, nested), `host.py` (3 CSVs), `computeresource.py` (2 CSVs), and `contentviewfilter.py` (2 CSVs). These are exactly the complex views we have not tested yet.

### Multi-tab window management: index-based handle lookup

Four entity methods open new browser tabs (webconsole, dynflow output, host group edit, documentation links). The current implementation uses `context.pages` list indexing — `window_handles[-1]` or `window_handles[1]` — to find the new tab. If Satellite opens tabs asynchronously or a popup appears between the click and the handle lookup, the index could point to the wrong page.

The fix would be to use Playwright's `page.context.expect_page()` to wait for the new tab explicitly, rather than assuming it appears at a specific index.

### What does NOT need fixing

Some areas we investigated turned out to be already handled:

- **File uploads** — widgetastic v2's `FileInput.fill()` already uses Playwright's `set_input_files()`. All 8 file upload widgets across 4 view files will work without changes.
- **Iframe switching** — widgetastic v2 implements `switch_to_frame()` using Playwright's `frame_locator()`. The one iframe usage (host webconsole) is covered.
- **Hover/move-to-element** — widgetastic v2's `move_to_element()` uses Playwright's `.hover()`. No changes needed.
- **ConditionalSwitchableView internals** — The descriptor itself uses no Selenium API. The risk is in the reference widgets it reads, not in CSV's own code.

---

## Playwright Features We Should Adopt

An audit of what Playwright offers versus what we actually use revealed several features that would fix known problems or improve reliability. We are currently using a narrow slice of the Playwright API — mostly `page.evaluate()`, `page.screenshot()`, `page.on()`, and `expect_download()`. Here is what we are missing and where each one helps.

### Features that fix known problems

**`press_sequentially()`** — Types text character by character, firing every key event. This is the fix for the ACE editor bug. Instead of `execute_script("ace.edit(...).setValue(...)")`, which updates the buffer without triggering change listeners, we click the editor's textarea, select all, and type the new content keystroke by keystroke. Slower, but every change listener fires naturally. Implemented in `widgets.py` ACEEditor.fill().

**`expect_popup()`** — Waits for a new browser tab to open, returning the Page object when it appears. This replaces the fragile `context.pages[-1]` index lookup used in four entity methods (host webconsole, dynflow output, host group edit, documentation links). With `expect_popup()`, we get the right page even if tabs open asynchronously or out of order.

**`add_locator_handler()`** — Registers a callback that fires automatically whenever a matching overlay or dialog appears during any Playwright action. This could replace our polling-based `handle_alert()` for PatternFly modals. Instead of 49 explicit `handle_alert()` calls scattered across entity files, a single handler registered at session start would dismiss or confirm modals whenever they block an interaction.

**`page.route()`** and **`expect_response()`** — Intercept or wait for specific network requests. These could replace some of the 37 `time.sleep()` calls that exist because `ensure_page_safe()` does not cover every AJAX response. Instead of sleeping 3 seconds and hoping the table reloaded, we wait for the actual XHR response that populates it.

### Features that improve reliability

**`get_by_role()`, `get_by_label()`, `get_by_text()`** — Semantic locators that find elements by their ARIA role, associated label, or text content. More resilient than XPath because they do not depend on CSS classes or DOM structure. We could use these for new widget code instead of writing XPath locators, though converting existing locators is not worth the effort.

**`expect(locator).to_be_visible()`** and other auto-retrying assertions — Playwright's assertion library retries until a condition is met or a timeout expires. This replaces the `wait_for(lambda: widget.is_displayed, timeout=N)` pattern we use throughout the entity layer. The built-in assertions are cleaner and handle edge cases (like elements that briefly disappear and reappear) that our lambda polling does not.

**`tracing`** — Records a trace of all browser actions, network requests, and DOM snapshots. Traces can be opened in Playwright's Trace Viewer for step-by-step debugging. This would be valuable in CI where video recordings are hard to scrub through. A trace captures what happened at each step, not just what the screen looked like.

**`storage_state()`** — Saves cookies and local storage to a file, which can be loaded into a new browser context. This could speed up test setup by saving a logged-in session state once and reusing it across tests, instead of logging in through the UI for every test.

### What we are not adopting yet

**`page.route()` for request mocking** — Useful for unit-testing UI components against fake API responses, but our tests run against a live Satellite instance. Mocking would defeat the purpose.

**`get_by_test_id()`** — Requires `data-testid` attributes in the Satellite UI HTML. We do not control the Satellite frontend, so we cannot add these attributes. Useful only where they already exist.

---

## The Full Change List

### Files changed by category

| Category | Files | What changed |
|----------|-------|-------------|
| Core (browser, session, settings, exceptions) | 5 | Complete rewrite of browser factory and session lifecycle |
| Widget library | 1 (`widgets.py`, 3,436 lines) | PF3 reimplementations, SatTab rewrite, Button/Dropdown/Select fixes |
| View definitions | 83 files | PF3/PF4 imports replaced with PF5, XPath `//` to `.//`, Bootstrap buttons converted |
| Entity methods | 16 files | Selenium API calls replaced, download trigger pattern added |
| Dependencies | 1 (`setup.py`) | Selenium/kaifuku/PF3/PF4 removed, Playwright/widgetastic v2/PF5 added |
| Navigation | 1 (`navigation.py`) | Exception import updated |
| Robottelo config | 1 (`config/__init__.py`) | Playwright settings instead of Selenium Grid config |
| CI infrastructure | 2 (`Dockerfile.playwright`, `Jenkinsfile.playwright`) | New files for Playwright-based CI |

### Commit history (oldest to newest)

```
424dfb6 fix(deps): remove Selenium, add Playwright and PF5 dependencies
9d4c387 fix(core): update session, settings, exceptions, and navigation for Playwright
3459adf fix(browser): rewrite browser.py with PlaywrightBrowserFactory
2d8ad51 fix(widgets): reimplement PF3 widgets and fix Playwright incompatibilities
f940197 fix(views): replace PF3/PF4 imports with PF5, fix XPath and buttons
aa3af5c fix(entities): replace Selenium API calls and add download triggers
```

---

## What's Next

1. **Validate the ACE editor fix.** Reimplemented `ACEEditor.fill()` using `press_sequentially()` instead of `setValue()`. Needs a test run against `test_jobtemplate::test_positive_end_to_end` to confirm the form sync issue is resolved.

2. **Adopt `expect_popup()` for multi-tab handling.** Replace the index-based `context.pages[-1]` lookups in four entity methods with `expect_popup()` to eliminate the race condition.

3. **Test the complex views.** Run tests that exercise host creation, content view management, compute resource forms, and ansible role assignment. Each of these will likely surface new widget issues that need fixing.

4. **Replace `time.sleep()` calls with condition-based waits.** Use `expect_response()` or `wait_for` with DOM conditions instead of the 37 hardcoded sleeps across entity files.

5. **Write the migration guide.** Document the patterns that changed so other contributors can maintain the Playwright-based codebase.

6. **Validate the CI pipeline.** Trigger a real Jenkins run with the Playwright container to confirm the infrastructure works end-to-end.
