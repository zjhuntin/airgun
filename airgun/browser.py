"""Tools to help getting Playwright and widgetastic browser instance to run UI
tests.
"""

from contextlib import contextmanager
from datetime import datetime
import logging
import os

from playwright.sync_api import Error, sync_playwright
from wait_for import TimedOutError, wait_for
from widgetastic.browser import Browser, DefaultPlugin
from widgetastic.exceptions import NoAlertPresentException

from widgetastic.xpath import normalize_space

from airgun import settings
from airgun.widgets import (
    ConfirmationDialog,
    Pf4ConfirmationDialog,
    Pf5ConfirmationDialog,
)

LOGGER = logging.getLogger(__name__)

BROWSER_TYPE_MAP = {
    'chrome': 'chromium',
    'chromium': 'chromium',
    'firefox': 'firefox',
}


class PlaywrightBrowserFactory:
    """Factory which creates a Playwright browser page for UI testing.

    Replaces the legacy SeleniumBrowserFactory. Manages the Playwright
    instance, browser, context, and page lifecycle.

    Usage::

        factory = PlaywrightBrowserFactory(test_name=test_name)
        page = factory.get_browser()
        # navigate to desired url, perform tests
        factory.finalize(passed)
    """

    def __init__(
        self, provider=None, browser=None, test_name=None, session_cookie=None, hostname=None
    ):
        self.provider = provider or getattr(settings.playwright, 'provider', 'local')
        self.browser_name = browser or getattr(settings.playwright, 'browser', 'chromium')
        self.test_name = test_name
        self._session = session_cookie
        self._hostname = hostname or settings.satellite.hostname
        self._playwright = None
        self._browser_instance = None
        self._context = None
        self._page = None

    def get_browser(self):
        """Returns a Playwright Page instance.

        :return: playwright.sync_api.Page instance
        :raises: ValueError: If wrong browser specified.
        """
        browser_type = BROWSER_TYPE_MAP.get(self.browser_name)
        if not browser_type:
            raise ValueError(
                f'"{self.browser_name}" browser is not supported. '
                f'Please use one of {tuple(BROWSER_TYPE_MAP.keys())}'
            )

        self._playwright = sync_playwright().start()

        launcher = getattr(self._playwright, browser_type)

        headless = getattr(settings.playwright, 'headless', True)
        if isinstance(headless, str):
            headless = headless.lower() in ('true', '1', 'yes')

        slow_mo = int(getattr(settings.playwright, 'slow_mo', 0))

        launch_args = []
        browseroptions = getattr(settings.playwright, 'browseroptions', None)
        if browseroptions:
            launch_args = browseroptions.split(';')

        if self.provider == 'remote':
            ws_endpoint = getattr(settings.playwright, 'ws_endpoint', None)
            if ws_endpoint:
                self._browser_instance = launcher.connect(ws_endpoint, slow_mo=slow_mo)
            else:
                self._browser_instance = launcher.launch(
                    headless=headless, slow_mo=slow_mo, args=launch_args
                )
        else:
            self._browser_instance = launcher.launch(
                headless=headless, slow_mo=slow_mo, args=launch_args
            )

        context_kwargs = {
            'viewport': {'width': 1920, 'height': 1080},
            'ignore_https_errors': True,
            'accept_downloads': True,
        }

        record_video = getattr(settings.playwright, 'record_video', False)
        if isinstance(record_video, str):
            record_video = record_video.lower() in ('true', '1', 'yes')
        if record_video:
            videos_path = getattr(settings.playwright, 'videos_path', 'videos/')
            os.makedirs(videos_path, exist_ok=True)
            context_kwargs['record_video_dir'] = videos_path
            context_kwargs['record_video_size'] = {'width': 1920, 'height': 1080}

        self._context = self._browser_instance.new_context(**context_kwargs)

        self._page = self._context.new_page()
        self._set_session_cookie()
        return self._page

    def post_init(self):
        """Perform all required post-init tweaks and workarounds."""
        pass

    def finalize(self, passed=True):
        """Finalize browser — close page, context, browser, and Playwright.

        When video recording is enabled, captures the video path before closing
        resources. On test pass the video is deleted; on failure it is kept and
        the path is logged so it can be reviewed.
        """
        video_path = None
        if self._page and not self._page.is_closed():
            try:
                video = self._page.video
                if video:
                    video_path = video.path()
            except (AttributeError, RuntimeError):
                pass

        for resource, name in [
            (self._page, 'page'),
            (self._context, 'context'),
            (self._browser_instance, 'browser'),
            (self._playwright, 'playwright'),
        ]:
            try:
                if resource:
                    if name == 'page' and resource.is_closed():
                        continue
                    if name == 'playwright':
                        resource.stop()
                    else:
                        resource.close()
            except (RuntimeError, OSError):
                LOGGER.debug('Error closing %s during finalize', name)

        if video_path and os.path.exists(video_path):
            if passed:
                os.remove(video_path)
                LOGGER.debug('Removed video for passed test: %s', video_path)
            else:
                LOGGER.info('Video saved for failed test: %s', video_path)

    def _set_session_cookie(self):
        """Add the session cookie (if provided) to the browser context."""
        if self._session:
            self._context.add_cookies(
                [
                    {
                        'name': '_session_id',
                        'value': self._session.cookies.get_dict()['_session_id'],
                        'domain': self._hostname,
                        'path': '/',
                    }
                ]
            )


class AirgunBrowserPlugin(DefaultPlugin):
    """Plug-in for :class:`AirgunBrowser` which adds satellite-specific
    JavaScript to make sure page is loaded completely. Checks for absence of
    jQuery, AJAX, Angular requests, absence of spinner indicating loading
    progress and ensures ``document.readyState`` is "complete".
    """

    ENSURE_PAGE_SAFE = """
        () => {
            function jqueryInactive() {
             return (typeof jQuery === "undefined") ? true : jQuery.active < 1
            }
            function ajaxInactive() {
             return (typeof Ajax === "undefined") ? true :
                Ajax.activeRequestCount < 1
            }
            function angularNoRequests() {
             if (typeof angular === "undefined") {
               return true
             } else if (typeof angular.element(
                 document).injector() === "undefined") {
               injector = angular.injector(["ng"]);
               return injector.get("$http").pendingRequests.length < 1
             } else {
               return angular.element(document).injector().get(
                 "$http").pendingRequests.length < 1
             }
            }
            function spinnerInvisible() {
             spinner = document.getElementById("vertical-spinner")
             return (spinner === null) ? true : spinner.style["display"] == "none"
            }
            function reactLoadingInvisible() {
             react = document.querySelector("#reactRoot .loading-state")
             return react === null
            }
            function anySpinnerInvisible() {
             spinners = Array.prototype.slice.call(
              document.querySelectorAll('.spinner')
              ).filter(function (item,index) {
                return item.offsetWidth > 0 || item.offsetHeight > 0
                 || item.getClientRects().length > 0;
               }
              );
             return spinners.length === 0
            }
            return {
                jquery: jqueryInactive(),
                ajax: ajaxInactive(),
                angular: angularNoRequests(),
                spinner: spinnerInvisible(),
                any_spinner: anySpinnerInvisible(),
                react: reactLoadingInvisible(),
                document: document.readyState == "complete",
            }
        }
        """

    def __init__(self, *args, **kwargs):
        self._ignore_ensure_page_safe_timeout = False
        super().__init__(*args, **kwargs)

    @property
    def ignore_ensure_page_safe_timeout(self):
        return self._ignore_ensure_page_safe_timeout

    @ignore_ensure_page_safe_timeout.setter
    def ignore_ensure_page_safe_timeout(self, value):
        self._ignore_ensure_page_safe_timeout = value

    def ensure_page_safe(self, timeout=30):
        """Ensures page is fully loaded using Satellite-specific JS checks.

        First waits for network to settle, then runs custom JS checks for
        jQuery, AJAX, Angular, spinners, and React loading indicators.
        """
        try:
            if self.ignore_ensure_page_safe_timeout:
                timeout = 2

            def _check():
                result = self.browser.page.evaluate(self.ENSURE_PAGE_SAFE)
                try:
                    return all(result.values())
                except AttributeError:
                    return True

            wait_for(_check, timeout=timeout, delay=0.2, very_quiet=True)
        except TimedOutError:
            if not self.ignore_ensure_page_safe_timeout:
                raise

    def before_click(self, element, locator=None):
        """Invoked before clicking on an element. Ensure page is fully loaded
        before clicking.
        """
        self.ensure_page_safe()

    def after_click(self, element, locator=None):
        """Invoked after clicking on an element. Ensure page is fully loaded
        before proceeding further.
        """
        pass

    def do_refresh(self):
        """Refresh current page."""
        self.browser.refresh()
        self.browser.plugin.ensure_page_safe()


class AirgunBrowser(Browser):
    """A wrapper around :class:`widgetastic.browser.Browser` which injects
    :class:`airgun.session.Session` and :class:`AirgunBrowserPlugin`.
    """

    def __init__(self, page, session, extra_objects=None):
        """Pass Playwright page instance, session and other extra objects.

        :param page: :class:`playwright.sync_api.Page` instance.
        :param session: :class:`airgun.session.Session` instance.
        :param extra_objects: any extra objects you want to include.
        """
        extra_objects = extra_objects or {}
        extra_objects.update({'session': session})
        super().__init__(page, plugin_class=AirgunBrowserPlugin, extra_objects=extra_objects)
        self._last_dialog = None
        self._last_download = None
        self.page.on('download', self._on_download)
        self.page.on('dialog', self._on_dialog)

    def _on_download(self, download):
        """Capture downloads for save_downloaded_file()."""
        self._last_download = download

    def _on_dialog(self, dialog):
        """Handle native browser alerts immediately (Playwright requirement).

        Playwright auto-dismisses dialogs if not handled in the callback.
        We accept by default and store the dialog for later inspection.
        """
        self._last_dialog = dialog
        dialog.accept()

    def text(self, locator, *args, **kwargs):
        """Return visible text using inner_text() instead of text_content().

        text_content() concatenates raw DOM text nodes without whitespace,
        turning block-level layouts like "<span>13</span><span>Total</span>"
        into "13Total". inner_text() respects rendered layout and inserts
        whitespace between block elements, producing "13\nTotal" which
        normalize_space() collapses to "13 Total".

        Falls back to text_content() for SVG elements, which don't support
        innerText (they're not HTMLElements).
        """
        el = self.element(locator, *args, **kwargs)
        try:
            raw = el.inner_text() or ""
        except Error:
            raw = el.text_content() or ""
        return normalize_space(raw)

    def get_client_datetime(self):
        """Make Javascript call inside of browser session to get exact current
        date and time.

        :return: Datetime object that contains data for current date and time
            on a client
        """
        script = """
            () => {
                var currentdate = new Date();
                return (
                    currentdate.getFullYear() + "-"
                    + (currentdate.getMonth()+1) + "-"
                    + currentdate.getDate() + " : "
                    + currentdate.getHours() + ":"
                    + currentdate.getMinutes()
                );
            }
        """
        client_datetime = self.page.evaluate(script)
        return datetime.strptime(client_datetime, '%Y-%m-%d : %H:%M')

    def get_downloads_list(self):
        """Return a list of downloaded files using Playwright's download API.

        Note: This method requires downloads to be tracked via
        page.expect_download() in the calling code. For legacy compatibility,
        it attempts the Chrome downloads page approach.

        :return: list of strings representing file paths
        """
        raise NotImplementedError(
            'get_downloads_list is not supported with Playwright. '
            'Use expect_download() context manager instead.'
        )

    def save_downloaded_file(self, save_path=None, trigger=None):
        """Save a downloaded file to the specified local directory.

        :param str save_path: directory to save the file in
        :param callable trigger: action that triggers the download (e.g. a
            button click lambda). When provided, uses Playwright's
            expect_download() context manager for reliable capture.
            When None, falls back to the event-listener approach.
        :return: full path to the saved file
        """
        if not save_path:
            save_path = getattr(settings.airgun, 'tmp_dir', '/tmp')  # noqa: S108

        if trigger is not None:
            with self.expect_download(timeout=60000) as download_info:
                trigger()
            download = download_info.value
        elif self._last_download is not None:
            download = self._last_download
        else:
            download, _ = wait_for(
                lambda: self._last_download,
                timeout=60,
                delay=1,
                fail_condition=lambda d: d is None,
            )

        self._last_download = None
        filename = download.suggested_filename or 'download'
        file_path = os.path.join(save_path, filename)
        download.save_as(file_path)
        return file_path

    def check_alert(self, locator):
        """Check if a modal dialog element is displayed."""
        try:
            loc = self.page.locator(locator)
            return loc.count() > 0 and loc.first.is_visible()
        except (TimeoutError, RuntimeError):
            return False

    def get_alert(self, squash=False):
        """Returns the current alert/PF modal object.

        Checks for PF4/PF5 modal dialogs first, then falls back to native
        browser dialogs via Playwright's dialog handler.

        :param bool optional squash: Whether or not to squash errors during
            alert handling. Default False
        """
        pf4_modal_locator = "//div[@data-ouia-component-type='PF4/ModalContent']"
        pf5_modal_locator = "//div[@data-ouia-component-type='PF5/ModalContent']"
        modal_locator = "//div[@class='modal-content']"
        modal_map = {
            pf4_modal_locator: Pf4ConfirmationDialog,
            modal_locator: ConfirmationDialog,
            pf5_modal_locator: Pf5ConfirmationDialog,
        }

        for locator, locator_class in modal_map.items():
            if self.check_alert(locator):
                return locator_class(self.browser)

        if self._last_dialog is not None:
            dialog = self._last_dialog
            self._last_dialog = None
            return dialog

        if squash:
            return False
        return None

    def handle_alert(
        self,
        cancel=False,
        wait=30.0,
        squash=False,
        prompt=None,
        check_present=False,
    ):
        """Handle PF modal dialogs and native browser alerts.

        For PF modals, clicks confirm/cancel buttons.
        For native browser dialogs, the dialog was already auto-accepted
        by the _on_dialog handler installed in __init__.
        """
        # widgetastic's ClickableMixin passes wait=2.0 which is too short
        # for PF5 modals that render asynchronously
        wait = max(wait, 10.0)
        try:
            popup, _ = wait_for(
                lambda: self.get_alert(squash=True),
                timeout=wait,
                delay=0.5,
                fail_condition=lambda p: not p,
            )
        except TimedOutError:
            popup = None
        if popup is None:
            if check_present:
                raise NoAlertPresentException('No alert present')
            return None

        if isinstance(popup, Pf4ConfirmationDialog | ConfirmationDialog | Pf5ConfirmationDialog):
            if cancel:
                self.logger.info('  dismissing')
                popup.cancel()
            else:
                popup.confirm()
            return True

        # Native Playwright dialog (already auto-accepted by _on_dialog handler)
        if hasattr(popup, 'type'):
            self.logger.info('  native dialog was auto-accepted: %s', popup.message)
            return True

        if squash:
            return False
        return None

    @property
    def window_handles(self):
        """Return list of page indices (analogous to Selenium window handles)."""
        return list(range(len(self.page.context.pages)))

    def switch_to_window(self, handle):
        """Switch to a different page/tab by index."""
        pages = self.page.context.pages
        if isinstance(handle, int) and 0 <= handle < len(pages):
            target_page = pages[handle]
            target_page.bring_to_front()
            self.page = target_page
            self.active_context = target_page

    def close_window(self, handle=None):
        """Close a page/tab by index. If closing the current page, switch to another."""
        context = self.page.context
        pages = context.pages
        if handle is not None and isinstance(handle, int) and 0 <= handle < len(pages):
            target = pages[handle]
            is_current = target == self.page
            target.close()
            if is_current and context.pages:
                self.page = context.pages[0]
                self.active_context = self.page
        else:
            self.page.close()
            if context.pages:
                self.page = context.pages[0]
                self.active_context = self.page

    def new_window(self, url, focus=False):
        """Open URL in a new tab and return its handle index."""
        new_page = self.page.context.new_page()
        new_page.goto(url)
        handle = len(self.page.context.pages) - 1
        if focus:
            self.switch_to_window(handle)
        return handle

    @contextmanager
    def ignore_ensure_page_safe_timeout(self):
        try:
            self.plugin.ignore_ensure_page_safe_timeout = True
            yield
        finally:
            self.plugin.ignore_ensure_page_safe_timeout = False
