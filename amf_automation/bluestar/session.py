"""Browser session for Bluestar PLM: start Chrome, login wait, pop-ups, grids, buttons."""

import re
import time
from pathlib import Path

from playwright.sync_api import Error as PwError
from playwright.sync_api import TimeoutError as PwTimeout
from playwright.sync_api import sync_playwright

from ..tracker import StepFailed
from . import selectors as S

# Reads the visible grid whose headers contain `hint`, tags it with data-amf-* attributes
# so Playwright can click exact cells afterwards, and returns headers + row values.
_GRID_JS = r"""
(hint) => {
  const norm = s => (s || '').split('\n')[0].replace(/\s+/g, ' ').trim().toLowerCase();
  const val = c => {
    const inp = c.querySelector('input, textarea');
    if (inp && inp.value) return inp.value;
    const a = c.querySelector('a');
    if (a && a.innerText.trim()) return a.innerText;
    return c.innerText || c.getAttribute('title') || '';
  };
  document.querySelectorAll('[data-amf-grid]').forEach(e => e.removeAttribute('data-amf-grid'));
  const grids = Array.from(document.querySelectorAll('[role="grid"]'))
     .filter(g => g.offsetParent !== null || g.getClientRects().length);
  for (const g of grids) {
    const hdrEls = Array.from(g.querySelectorAll('[role="columnheader"]'));
    const headers = hdrEls.map(h => norm(h.innerText || h.getAttribute('aria-label')));
    if (!headers.some(h => h.includes(hint))) continue;
    g.setAttribute('data-amf-grid', '1');
    const byIdx = {};
    hdrEls.forEach((h, i) => { const ci = h.getAttribute('aria-colindex'); if (ci) byIdx[ci] = headers[i]; });
    const rowEls = Array.from(g.querySelectorAll('[role="row"]')).filter(r => r.querySelector('[role="gridcell"]'));
    const rows = rowEls.map((r, ri) => {
      r.setAttribute('data-amf-row', String(ri));
      const cells = Array.from(r.querySelectorAll('[role="gridcell"]'));
      const out = {};
      const offset = headers.length - cells.length;   // positional fallback (selector column)
      cells.forEach((c, i) => {
        const ci = c.getAttribute('aria-colindex');
        let h = (ci && byIdx[ci] !== undefined) ? byIdx[ci] : headers[i + (offset > 0 ? offset : 0)];
        if (!h) h = 'col' + i;
        c.setAttribute('data-amf-col', h);
        out[h] = val(c).replace(/\s+/g, ' ').trim();
      });
      return out;
    });
    return {headers, rows};
  }
  return null;
}
"""

# Finds the visible text input whose current value matches `pattern` (e.g. the default
# "c:\myloadpoint\" of the check-out box) and tags it so it can be filled.
_TAG_INPUT_BY_VALUE_JS = r"""
(pattern) => {
  const re = new RegExp(pattern, 'i');
  document.querySelectorAll('[data-amf-target]').forEach(e => e.removeAttribute('data-amf-target'));
  const inputs = Array.from(document.querySelectorAll('input[type="text"], input:not([type]), textarea'))
     .filter(i => i.offsetParent !== null && re.test(i.value || ''));
  if (!inputs.length) return false;
  inputs[inputs.length - 1].setAttribute('data-amf-target', '1');
  return true;
}
"""


class BluestarSession:
    def __init__(self, cfg: dict, log, vpn, screenshots_dir: Path):
        self.cfg = cfg
        self.bcfg = cfg["bluestar"]
        self.log = log
        self.vpn = vpn
        self.shots = screenshots_dir
        self.timeout_ms = int(self.bcfg["action_timeout_seconds"]) * 1000
        self._pw = None
        self.ctx = None
        self.page = None

    # ------------------------------------------------------------- lifecycle
    def start(self):
        profile = self.cfg["resolved"]["browser_profile"]
        profile.mkdir(parents=True, exist_ok=True)
        self.log.info("      Starting Chrome (automation profile) ...")
        self._pw = sync_playwright().start()
        kwargs = dict(user_data_dir=str(profile), headless=False, accept_downloads=True,
                      no_viewport=True, args=["--start-maximized"])
        try:
            self.ctx = self._pw.chromium.launch_persistent_context(channel="chrome", **kwargs)
        except PwError as e:
            if "already in use" in str(e).lower() or "lock" in str(e).lower():
                raise StepFailed("The automation Chrome profile is already open. Close that Chrome window and retry.")
            self.log.info("      Google Chrome channel not available, using Playwright Chromium.")
            self.ctx = self._pw.chromium.launch_persistent_context(**kwargs)
        self.ctx.set_default_timeout(self.timeout_ms)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self._install_handlers(self.page)

    def close(self):
        for closer in (lambda: self.ctx and self.ctx.close(), lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception:
                pass
        self.ctx = self._pw = self.page = None

    @property
    def started(self) -> bool:
        return self.page is not None

    def _install_handlers(self, page):
        page.on("dialog", lambda d: d.accept())
        # Auto-answer the two Bluestar session pop-ups whenever Playwright acts on the page.
        try:
            page.add_locator_handler(page.get_by_role("button", name=S.BTN_IM_HERE),
                                     lambda loc: loc.click(), no_wait_after=True)
            page.add_locator_handler(page.get_by_role("button", name=S.BTN_WAIT, exact=True),
                                     lambda loc: loc.click(), no_wait_after=True)
        except Exception:
            pass

    # ------------------------------------------------------------ navigation
    def open_home(self, new_tab: bool = True):
        """STEP-3: open a new Chrome tab and enter the Bluestar URL, wait until usable."""
        self.vpn.ensure_connected("before opening Bluestar")
        if new_tab:
            old = self.page
            self.page = self.ctx.new_page()
            self._install_handlers(self.page)
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
        self.page.goto(self.bcfg["url"], wait_until="domcontentloaded", timeout=120_000)
        self._wait_for_list_page()

    def _wait_for_list_page(self):
        limit = time.time() + self.bcfg["login_timeout_minutes"] * 60
        warned = False
        on_login_since = None
        while time.time() < limit:
            if self._filter_box(visible_only=True) is not None:
                self.wait_idle()
                return
            url = self.page.url.lower()
            if "login" in url or "microsoftonline" in url:
                on_login_since = on_login_since or time.time()
                # saved login normally passes the Microsoft page in a few seconds -> warn only if stuck
                if not warned and time.time() - on_login_since > 30:
                    self.log.warning("  ACTION NEEDED (one time only): sign in to Bluestar in the automation Chrome "
                                     "window. Chrome does not allow automation of your normal profile, so this "
                                     "window has its own profile; the login is remembered for all next runs "
                                     f"(waiting up to {self.bcfg['login_timeout_minutes']} min).")
                    warned = True
                self._accept_stay_signed_in()
            else:
                self.dismiss_popups()
            time.sleep(2)
        raise StepFailed("Bluestar product list page did not load (login timeout or page not reachable)")

    def _accept_stay_signed_in(self):
        """Microsoft 'Stay signed in?' -> tick 'Don't show this again' + Yes, so the login persists."""
        page = self.page
        try:
            if not page.get_by_text(re.compile(r"Stay signed in\?", re.I)).first.is_visible():
                return
            box = page.locator("#KmsiCheckboxField")
            if box.count() and not box.first.is_checked():
                box.first.check()
            yes = page.locator("#idSIButton9")
            if not yes.count():
                yes = page.get_by_role("button", name="Yes", exact=True)
            yes.first.click()
            self.log.info("      Microsoft 'Stay signed in?' -> Yes (login will be remembered)")
            time.sleep(2)
        except PwError:
            pass

    def go_back(self):
        btn = self.page.get_by_role("button", name=S.BTN_BACK, exact=True)
        try:
            btn.first.click(timeout=10_000)
        except Exception:
            self.page.keyboard.press("Escape")
        self.wait_idle()

    # --------------------------------------------------------------- waiting
    def wait_idle(self, timeout_s: float | None = None):
        """Wait until the 'Please wait. We're processing your request.' overlay is gone."""
        end = time.time() + (timeout_s or self.bcfg["action_timeout_seconds"] * 3)
        time.sleep(0.5)
        busy = self.page.get_by_text(S.BUSY_TEXT)
        while time.time() < end:
            self.dismiss_popups()
            try:
                if not busy.first.is_visible():
                    try:
                        self.page.wait_for_load_state("networkidle", timeout=3000)
                    except PwTimeout:
                        pass
                    return
            except PwError:
                return
            time.sleep(1)
        self.log.warning("      Bluestar still busy after waiting; continuing.")

    def dismiss_popups(self, aggressive: bool = False) -> list[str]:
        """STEP-5.7: answer 'Wait', "I'm here" and (aggressive) other Bluestar message boxes."""
        clicked = []
        page = self.page
        candidates = [page.get_by_role("button", name=S.BTN_WAIT, exact=True),
                      page.get_by_role("button", name=S.BTN_IM_HERE)]
        if aggressive:
            dialog = page.locator('[role="dialog"], [role="alertdialog"], .dialog-popup')
            for name in S.GENERIC_CONTINUE_BUTTONS:
                candidates.append(dialog.get_by_role("button", name=name, exact=True))
        for loc in candidates:
            try:
                if loc.first.is_visible():
                    label = loc.first.inner_text().strip()
                    loc.first.click(timeout=5000)
                    clicked.append(label)
                    self.log.info(f"      Pop-up answered: '{label}'")
                    time.sleep(1)
            except PwError:
                continue
        return clicked

    # --------------------------------------------------------------- helpers
    def _filter_box(self, visible_only: bool = False):
        for label in S.FILTER_BOX_LABELS:
            for loc in (self.page.get_by_role("textbox", name=label, exact=True),
                        self.page.get_by_placeholder(label, exact=True),
                        self.page.locator(f'input[aria-label="{label}"]')):
                try:
                    if loc.count() and (not visible_only or loc.first.is_visible()):
                        return loc.first
                except PwError:
                    continue
        return None

    def filter_box(self):
        box = self._filter_box(visible_only=True)
        if box is None:
            raise StepFailed("Bluestar filter box not found on the product list page")
        return box

    def _button_candidates(self, name, control: str | None):
        page = self.page
        if control:
            yield page.locator(f'[data-dyn-controlname="{control}"]')
        if isinstance(name, str):
            yield page.get_by_role("button", name=name, exact=True)
            # Bluestar adds hidden text (shortcut / help) to accessible names -> "starts with" match
            yield page.get_by_role("button", name=re.compile(rf"^\s*{re.escape(name)}(?![A-Za-z])", re.I))
            yield page.locator("button").filter(has_text=re.compile(rf"^\s*{re.escape(name)}\s*$", re.I))
        else:
            yield page.get_by_role("button", name=name)

    def button(self, name, exact: bool = True, last: bool = False, control: str | None = None):
        """First strategy that finds a visible button wins (control name > exact name > starts-with)."""
        fallback = None
        for loc in self._button_candidates(name, control):
            try:
                vis = loc.filter(visible=True)
                if vis.count():
                    return vis.last if last else vis.first
            except PwError:
                continue
            fallback = fallback or loc
        return (fallback.last if last else fallback.first) if fallback is not None else None

    def click_button(self, name, exact: bool = True, last: bool = False, timeout_s: float | None = None,
                     tab: str | None = None, control: str | None = None):
        end = time.time() + (timeout_s or 15)
        btn = None
        while time.time() < end:
            btn = self.button(name, last=last, control=control)
            try:
                if btn is not None and btn.is_visible():
                    break
            except PwError:
                pass
            time.sleep(1)
        else:
            if tab:
                self.open_tab(tab)
            end = time.time() + self.timeout_ms / 1000
            while time.time() < end:
                btn = self.button(name, last=last, control=control)
                try:
                    if btn is not None and btn.is_visible():
                        break
                except PwError:
                    pass
                self.dismiss_popups()
                time.sleep(1)
            else:
                raise StepFailed(f"Button '{name}' not found on the Bluestar page")
        if not btn.is_enabled():
            raise StepFailed(f"Button '{name}' is disabled in Bluestar")
        btn.click()
        self.wait_idle()

    def open_tab(self, name: str):
        for loc in (self.page.get_by_role("tab", name=name, exact=True),
                    self.page.get_by_role("button", name=name, exact=True),
                    self.page.get_by_text(name, exact=True)):
            try:
                if loc.count() and loc.first.is_visible():
                    loc.first.click()
                    time.sleep(1)
                    return
            except PwError:
                continue

    def is_visible_text(self, text) -> bool:
        try:
            loc = self.page.get_by_text(text)
            return loc.count() > 0 and loc.first.is_visible()
        except PwError:
            return False

    def read_grid(self, header_hint: str) -> list[dict]:
        data = self.page.evaluate(_GRID_JS, header_hint.lower())
        if not data:
            return []
        rows = []
        for i, r in enumerate(data["rows"]):
            if any(v for v in r.values()):
                r["_index"] = i
                rows.append(r)
        return rows

    def click_grid_cell(self, row_index: int, column: str):
        """Select a grid row by clicking one of its (non-link) cells."""
        cell = self.page.locator(f'[data-amf-grid] [data-amf-row="{row_index}"] [data-amf-col="{column}"]')
        if not cell.count():
            cell = self.page.locator(f'[data-amf-grid] [data-amf-row="{row_index}"] [role="gridcell"]').last
        cell.first.click()
        time.sleep(1)
        self.wait_idle()

    def fill_input_with_value(self, value_pattern: str, new_value: str) -> bool:
        if not self.page.evaluate(_TAG_INPUT_BY_VALUE_JS, value_pattern):
            return False
        box = self.page.locator('[data-amf-target="1"]')
        box.click()
        box.fill(new_value)
        box.press("Tab")
        time.sleep(0.5)
        return True

    def fill_labeled(self, label: str, value: str) -> bool:
        for loc in (self.page.get_by_label(label, exact=True),
                    self.page.get_by_role("combobox", name=label),
                    self.page.get_by_role("textbox", name=label),
                    self.page.locator(f'input[aria-label="{label}"]')):
            try:
                if loc.count() and loc.first.is_visible():
                    loc.first.click()
                    loc.first.fill(value)
                    loc.first.press("Tab")
                    time.sleep(0.5)
                    return True
            except PwError:
                continue
        return False

    def message_bar_text(self) -> str:
        try:
            loc = self.page.locator(".messageBar-message, [role='alert']")
            texts = [t.strip() for t in loc.all_inner_texts() if t.strip()]
            return " | ".join(texts[:3])
        except PwError:
            return ""

    def screenshot(self, name: str) -> str:
        if not self.page:
            return ""
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
        path = self.shots / f"{time.strftime('%H%M%S')}_{safe}.png"
        try:
            self.page.screenshot(path=str(path), full_page=False)
            return str(path)
        except Exception:
            return ""
