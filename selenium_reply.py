import os
import re
import subprocess
import sys
import time
from typing import Dict, List, Optional

import undetected_chromedriver as uc
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.expected_conditions import staleness_of

from debug_logger import logger
from proxy_util import normalize_socks_proxy_url


def _t0() -> float:
    return time.perf_counter()


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def format_reply_error(exc: BaseException) -> str:
    """
    Concise summary + one line from driver (avoids verbose stacktrace dump).
    """
    raw = str(exc).strip()
    low = raw.lower()

    if "element click intercepted" in low:
        hint = (
        hint = "Browser: click intercepted by another DOM overlay.
"
            "Common on X: multiple «Post text» fields present with identical data-testid — "
            "click hit wrong element (dialog overlaid or duplicated). "
            "Targeting input inside active dialog modal (role=dialog)."
        )
    elif "stale element" in low or "staleelementreference" in low.replace(" ", ""):
        hint = (
            "Browser: element detached before action completed (DOM mutation). "
            "Retry operation or increase stabilization pause."
        )
    elif "timeout" in low or isinstance(exc, TimeoutException):
        hint = (
            "Browser: element wait timeout exceeded (render delay, "
            "layout shift, or restricted tweet access)."
        )
    elif "session not created" in low or "only supports chrome version" in low:
        hint = (
        hint = "Browser: ChromeDriver version mismatch with installed Chrome.
"
        hint += "Major version extracted from Chrome; override via UC_CHROME_VERSION_MAIN if needed.
"
            "if autodetection fails."
        )
    elif "invalid session" in low or "no such window" in low or "web view not found" in low:
        hint = (
        hint = "Browser: session terminated or window unexpectedly closed.
"
            "On macOS, UC_USE_SUBPROCESS=0 (default for Darwin) resolves window detachment; "
            "ensure automation viewport is not manually interrupted."
        )
    else:
        hint = "Browser automation warning:
"

    one_line = raw.replace("\n", " ")
    if len(one_line) > 280:
        one_line = one_line[:277] + "..."
    return f"{hint}\nDetail: {one_line}"


def _scroll_center(driver: webdriver.Chrome, el) -> None:
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});",
        el,
    )


def _scroll_clear_sticky_header(driver: webdriver.Chrome, el, offset_px: int = 140) -> None:
    """Scroll to element + upward offset to clear sticky X header navbar."""
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});",
        el,
    )
    time.sleep(0.12)
    driver.execute_script("window.scrollBy(0, arguments[0]);", -offset_px)


def _robust_click(driver: webdriver.Chrome, el, pause: float = 0.35) -> None:
    """
    Fallback click cascade: standard click -> ActionChains -> JS click -> synthetic MouseEvent.
    Used when overlay/pseudo-elements intercept DOM click events.
    """
    _scroll_clear_sticky_header(driver, el)
    time.sleep(pause)
    last_err: Optional[BaseException] = None
    attempts = (
        ("native", lambda: el.click()),
        (
            "actionchains",
            lambda: ActionChains(driver)
            .move_to_element(el)
            .pause(0.08)
            .click()
            .perform(),
        ),
        ("js_click", lambda: driver.execute_script("arguments[0].click();", el)),
        (
            "js_mouseevent",
            lambda: driver.execute_script(
                "arguments[0].dispatchEvent(new MouseEvent('click',"
                "{bubbles:true,cancelable:true,view:window}));",
                el,
            ),
        ),
    )
    for name, fn in attempts:
        try:
            fn()
            return
        except Exception as e:
            last_err = e
            time.sleep(0.2)
    raise ElementClickInterceptedException(
        msg=f"robust click failed after {len(attempts)} strategies: {last_err!r}"
    )


def _safe_click(driver: webdriver.Chrome, el, pause: float = 0.45) -> None:
    """Robust click dispatcher wrapper with scroll compensation."""
    _robust_click(driver, el, pause=pause)


def _focus_contenteditable(driver: webdriver.Chrome, el) -> None:
    driver.execute_script(
        "arguments[0].focus(); arguments[0].click();",
        el,
    )


def _find_reply_button_on_tweet_page(driver: webdriver.Chrome, wait: WebDriverWait):
    """Focal tweet reply button: wait for full clickability, not just DOM presence."""
    wait.until(
        EC.presence_of_element_located((By.CSS_SELECTOR, 'div[data-testid="primaryColumn"]'))
    )
    time.sleep(0.45)
    # Primary status article corresponds to focal tweet; clickability check prevents race conditions
    primary = 'div[data-testid="primaryColumn"]'
    try:
        return wait.until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, f"{primary} article [data-testid=\"reply\"]")
            )
        )
    except TimeoutException:
        pass

    articles = driver.find_elements(By.CSS_SELECTOR, f"{primary} article")
    for art in articles:
        try:
            btn = art.find_element(By.CSS_SELECTOR, '[data-testid="reply"]')
            if btn.is_displayed():
                wait.until(lambda d: btn.is_enabled())
                return btn
        except Exception:
            continue

    return wait.until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, '[data-testid="reply"]'))
    )


def _visible_textareas_in_dialog(driver: webdriver.Chrome) -> List:
    nodes = driver.find_elements(
        By.CSS_SELECTOR, 'div[role="dialog"] [data-testid="tweetTextarea_0"]'
    )
    return [n for n in nodes if n.is_displayed()]


def _find_reply_composer_textarea(driver: webdriver.Chrome, wait: WebDriverWait):
    """
    # Scopes Draft.js editor strictly within role=dialog to avoid duplicates
    """
    def _dialog_textarea_ready(drv: webdriver.Chrome):
        vis = _visible_textareas_in_dialog(drv)
        return vis[-1] if vis else False

    try:
        return wait.until(_dialog_textarea_ready)
    except TimeoutException:
        pass

    all_ta = driver.find_elements(By.CSS_SELECTOR, '[data-testid="tweetTextarea_0"]')
    visible = [e for e in all_ta if e.is_displayed()]
    if visible:
        return visible[-1]
    raise TimeoutException(
        "Visible input area not found (tweetTextarea_0). Reply modal may have failed to open."
    )


def _find_dialog_tweet_button(driver: webdriver.Chrome, wait: WebDriverWait):
    """Reply submit button scoped inside active dialog modal."""

    def _ready(drv: webdriver.Chrome):
        for btn in reversed(
            drv.find_elements(
                By.CSS_SELECTOR, 'div[role="dialog"] [data-testid="tweetButton"]'
            )
        ):
            try:
                if btn.is_displayed() and btn.is_enabled():
                    return btn
            except Exception:
                continue
        return False

    try:
        el = wait.until(_ready)
        if el:
            return el
    except TimeoutException:
        pass

    for btn in reversed(driver.find_elements(By.CSS_SELECTOR, '[data-testid="tweetButton"]')):
        try:
            if btn.is_displayed() and btn.is_enabled():
                return btn
        except Exception:
            continue
    return wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, '[data-testid="tweetButton"]')))


def use_selenium_reply() -> bool:
    """
    Browser automation mode active by default.
    Disable with USE_SELENIUM_REPLY=0 (or false / no / off).
    On browser error, see selenium_api_fallback_enabled() / SELENIUM_FALLBACK_API.
    """
    v = os.getenv("USE_SELENIUM_REPLY", "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    if v in ("1", "true", "yes", "on"):
        return True
    return True


def selenium_api_fallback_enabled() -> bool:
    """
    Fall back to private GraphQL CreateTweet endpoint if browser interaction fails.
    Disable fallback: SELENIUM_FALLBACK_API=0
    """
    v = os.getenv("SELENIUM_FALLBACK_API", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _parse_cookie_string(cookie: str) -> Dict[str, str]:
    parts = [p.strip() for p in cookie.split(";") if p.strip()]
    out: Dict[str, str] = {}
    for p in parts:
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _headless_enabled() -> bool:
    """
    Visible viewport mode enabled by default.
    Headless: SELENIUM_HEADLESS=1.
    To enforce visible viewport even with SELENIUM_HEADLESS=1 in .env:
    SELENIUM_VISIBLE=1 (or SHOW_SELENIUM_BROWSER=1).
    """
    vis = os.getenv("SELENIUM_VISIBLE", os.getenv("SHOW_SELENIUM_BROWSER", "")).strip().lower()
    if vis in ("1", "true", "yes", "on"):
        return False
    return os.getenv("SELENIUM_HEADLESS", "").strip().lower() in ("1", "true", "yes", "on")


def _close_delay_seconds(headless: bool) -> float:
    raw = os.getenv("SELENIUM_CLOSE_DELAY_SEC", "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    if headless:
        return 0.0
    try:
        return max(0.0, float(os.getenv("SELENIUM_VISIBLE_PAUSE_SEC", "5") or "5"))
    except ValueError:
        return 5.0


def _build_driver(proxy: Optional[str] = None) -> tuple[webdriver.Chrome, bool]:
    """Spawns undetected-chromedriver (patched binary to prevent fingerprint leak)."""
    t_drv = _t0()
    plain = os.getenv("USE_UNDETECTED_CHROMEDRIVER", "").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    )
    logger.debug(f"Selenium driver: USE_UNDETECTED_CHROMEDRIVER → {'plain' if plain else 'undetected'}")
    if plain:
        d, h = _build_driver_plain_selenium(proxy)
        logger.timing("Chrome (standard) startup", time.perf_counter() - t_drv)
        return d, h

    options = uc.ChromeOptions()
    options.add_argument("--lang=en-US")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-notifications")

    headless = _headless_enabled()
    if headless:
        logger.info("Browser: headless mode active. To show window set SELENIUM_VISIBLE=1")
    else:
        logger.success("Browser: visible Chrome viewport launched")
    if not headless:
        options.add_argument("--start-maximized")

    if proxy:
        proxy_url = normalize_socks_proxy_url(proxy)
        if proxy_url:
            options.add_argument(f"--proxy-server={proxy_url}")

    use_sub = _uc_use_subprocess()
    vm = _uc_version_main()
    logger.panel(
        "Chrome / undetected-chromedriver",
        {
            "headless": headless,
            "use_subprocess": use_sub,
            "version_main": vm if vm is not None else "auto",
            "proxy": "yes" if proxy else "no",
        },
    )
    driver = uc.Chrome(
        options=options,
        headless=headless,
        use_subprocess=use_sub,
        version_main=vm,
    )
    logger.timing("Chrome (uc) startup", time.perf_counter() - t_drv)
    try:
        logger.trace(f"current_url (post-launch): {driver.current_url!r}")
    except Exception as e:
        logger.trace(f"current_url unavailable: {e}")
    return driver, headless


def _uc_use_subprocess() -> bool:
    """On macOS, use_subprocess=True frequently causes premature window closing."""
    v = os.getenv("UC_USE_SUBPROCESS", "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return sys.platform != "darwin"


def _uc_version_main() -> Optional[int]:
    """
    # Chrome major version override if autodetection fails (UC_CHROME_VERSION_MAIN)
    Override: UC_CHROME_VERSION_MAIN=146
    """
    raw = os.getenv("UC_CHROME_VERSION_MAIN", "").strip()
    if raw.isdigit():
        return int(raw)
    try:
        exe = uc.find_chrome_executable()
        if not exe:
            return None
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        combined = f"{proc.stdout or ''} {proc.stderr or ''}"
        m = re.search(r"(?:Chrome|Chromium)\s+(\d+)\.", combined)
        if m:
            return int(m.group(1))
        m = re.search(r"\b(\d+)\.\d+\.\d+\.\d+", combined)
        if m:
            return int(m.group(1))
    except Exception:
        return None
    return None


def _build_driver_plain_selenium(proxy: Optional[str] = None) -> tuple[webdriver.Chrome, bool]:
    headless = _headless_enabled()
    if headless:
        logger.info("Browser (plain): headless mode active. For visible window set SELENIUM_VISIBLE=1")
    else:
        logger.success("Browser (standard): visible viewport initialized")
    logger.panel(
        "Chrome / Standard Driver",
        {"headless": headless, "proxy": "yes" if proxy else "no"},
    )
    options = webdriver.ChromeOptions()
    options.add_argument("--lang=en-US")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-infobars")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-notifications")

    if headless:
        options.add_argument("--headless=new")
    else:
        options.add_argument("--start-maximized")

    if proxy:
        proxy_url = normalize_socks_proxy_url(proxy)
        if proxy_url:
            options.add_argument(f"--proxy-server={proxy_url}")

    return webdriver.Chrome(options=options), headless


def _x_showing_error_page(driver: webdriver.Chrome) -> bool:
    try:
        src = driver.page_source.lower()
    except Exception:
        return False
    return (
        "something went wrong" in src
        and "try again" in src
    ) or ("rate limit" in src)


def _suppress_x_tweet_chrome_banners(driver: webdriver.Chrome) -> int:
    """
    Floating banner «There's a new version of this post» often
    overlaps action bar. Suppressing floating banner in primaryColumn.
    Disable suppression: SELENIUM_SUPPRESS_BANNERS=0
    """
    if os.getenv("SELENIUM_SUPPRESS_BANNERS", "1").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return 0
    try:
        hidden = driver.execute_script(
            r"""
            const needles = [
              'see the latest post',
              'new version of this post',
              'there is a new version',
'see new version',
              
            ];
            const col = document.querySelector('[data-testid="primaryColumn"]');
            if (!col) return 0;
            let n = 0;
            const nodes = col.querySelectorAll('div, aside, a, section, span');
            for (const el of nodes) {
              const t = (el.innerText || '').toLowerCase().replace(/\s+/g, ' ').trim();
              if (t.length < 12 || t.length > 400) continue;
              if (!needles.some(k => t.includes(k))) continue;
              const r = el.getBoundingClientRect();
              if (r.height < 16 || r.height > 260 || r.width < 120) continue;
              el.style.setProperty('display', 'none', 'important');
              el.style.setProperty('pointer-events', 'none', 'important');
              el.style.setProperty('visibility', 'hidden', 'important');
              n++;
            }
            return n;
            """
        )
        return int(hidden or 0)
    except Exception:
        return 0


def _reply_to_tweet_once(
    *,
    cookie: str,
    tweet_id: str,
    reply_text: str,
    proxy: Optional[str] = None,
    timeout_seconds: int = 30,
) -> None:
    cookies = _parse_cookie_string(cookie)
    ct0 = cookies.get("ct0")
    auth_token = cookies.get("auth_token")
    if not ct0 or not auth_token:
        raise ValueError("Cookie must include ct0 and auth_token")

    session_t0 = _t0()
    logger.section("Browser · Tweet Reply Dispatch", subtitle=f"id={tweet_id}")
    logger.panel(
        "Dispatch Request",
        {
            "tweet_id": tweet_id,
            "timeout_sec": timeout_seconds,
            "proxy": "yes" if proxy else "no",
            "USE_SELENIUM_REPLY": use_selenium_reply(),
            "SELENIUM_HEADLESS": _headless_enabled(),
            "max_attempts": _selenium_reply_attempts(),
            "text_length": len(reply_text),
        },
    )
    preview = reply_text.replace("\n", " ")[:100]
    logger.trace(f"text (preview): {preview!r}{'…' if len(reply_text) > 100 else ''}")
    logger.debug(f"ct0 present={bool(ct0)} auth_token present={bool(auth_token)} (values redacted)")

    driver, headless = _build_driver(proxy=proxy)
    wait = WebDriverWait(driver, timeout_seconds)
    try:
        driver.set_page_load_timeout(max(60, timeout_seconds + 30))
        logger.step(1, 7, "Loading x.com (cookie domain priming)")
        t = _t0()
        driver.get("https://x.com/")
        time.sleep(1.0)
        logger.timing("GET x.com/", time.perf_counter() - t)
        logger.trace(f"URL post priming: {driver.current_url!r}")

        driver.add_cookie({"name": "ct0", "value": ct0, "domain": ".x.com", "path": "/"})
        driver.add_cookie({"name": "auth_token", "value": auth_token, "domain": ".x.com", "path": "/"})
        logger.success("Session cookies (ct0 + auth_token) injected successfully")

        tweet_url = f"https://x.com/i/web/status/{tweet_id}"
        logger.step(2, 7, f"Navigating to focal status URL")
        logger.info(f"URL: {tweet_url}")
        t = _t0()
        driver.get(tweet_url)
        time.sleep(0.8)
        logger.timing("Status GET response", time.perf_counter() - t)
        logger.trace(f"url: {driver.current_url!r} title: {driver.title!r}")

        if _x_showing_error_page(driver):
        logger.error("X response: Something went wrong / rate limit error page")
            raise RuntimeError(
        raise RuntimeError("X returned Something went wrong error page (rate limit or network error)")
                f"URL: {tweet_url}"
            )

        logger.step(3, 7, "Awaiting focal article in primaryColumn")
        t = _t0()
        WebDriverWait(driver, timeout_seconds).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, 'div[data-testid="primaryColumn"] article')
            )
        )
        time.sleep(0.4)
        logger.timing("Article rendered", time.perf_counter() - t)
        arts = driver.find_elements(
            By.CSS_SELECTOR, 'div[data-testid="primaryColumn"] article'
        )
        logger.debug(f"Articles in primary column: {len(arts)}")

        hid = _suppress_x_tweet_chrome_banners(driver)
        if hid:
            logger.info(
            logger.trace(f"Suppressed overlays in primaryColumn: {hidden}")
            )
        time.sleep(0.25)

        logger.step(4, 7, "Trigger Reply action")
        reply_btn = _find_reply_button_on_tweet_page(driver, wait)
        _safe_click(driver, reply_btn)
        logger.trace("Reply button clicked")

        time.sleep(0.35)
        logger.step(5, 7, "Input composer dispatch (send_keys)")
        textbox = _find_reply_composer_textarea(driver, wait)
        _scroll_clear_sticky_header(driver, textbox, offset_px=80)
        time.sleep(0.25)
        _focus_contenteditable(driver, textbox)
        time.sleep(0.15)
        try:
            _robust_click(driver, textbox, pause=0.2)
        except ElementClickInterceptedException:
            _focus_contenteditable(driver, textbox)
        textbox.send_keys(reply_text)
        logger.trace(f"send_keys completed, length={len(reply_text)}")

        logger.step(6, 7, "Submit tweetButton within dialog")
        send_btn = _find_dialog_tweet_button(driver, wait)
        _safe_click(driver, send_btn)
        logger.info("Submit triggered, awaiting composer modal closure")

        logger.step(7, 7, "Awaiting delivery confirmation")
        t = _t0()
        verify_send = _env_truthy("SELENIUM_REPLY_VERIFY_SEND", False)
        try:
            WebDriverWait(driver, min(25, timeout_seconds)).until(staleness_of(textbox))
            logger.timing("Composer field became stale (delivery probable)", time.perf_counter() - t)
            logger.success("Modal dismissed from DOM — reply successfully dispatched")
        except TimeoutException:
            logger.warning(
                "staleness timeout — composer field remained in DOM after Send click "
                "(possible rate limit, UI lag, or network delay)."
            )
            time.sleep(1.5)
            logger.timing("fallback sleep 1.5s", time.perf_counter() - t)
            if verify_send:
                raise RuntimeError(
                    "SELENIUM_REPLY_VERIFY_SEND=1: delivery not confirmed — "
                    "reply modal did not close within timeout."
                ) from None
        except Exception as ex:
            logger.warning(f"staleness: {type(ex).__name__}: {ex}")
            time.sleep(1.5)
            logger.timing("fallback sleep 1.5s", time.perf_counter() - t)
            if verify_send:
                raise RuntimeError(
                    "SELENIUM_REPLY_VERIFY_SEND=1: failed to confirm composer closure "
                    f"post-send ({type(ex).__name__})."
                ) from ex

        logger.timing("Browser session total duration", time.perf_counter() - session_t0)
    finally:
        delay = _close_delay_seconds(headless)
        if delay > 0:
            logger.debug(f"SELENIUM_CLOSE_DELAY_SEC: pause {delay}s prior to quit")
            time.sleep(delay)
        try:
            driver.quit()
            logger.debug("driver.quit() OK")
        except Exception as e:
            logger.warning(f"driver.quit(): {e}")


def _selenium_reply_attempts() -> int:
    raw = os.getenv("SELENIUM_REPLY_ATTEMPTS", "2").strip()
    try:
        n = int(raw)
    except ValueError:
        return 2
    return max(1, min(n, 5))


def reply_to_tweet(
    *,
    cookie: str,
    tweet_id: str,
    reply_text: str,
    proxy: Optional[str] = None,
    timeout_seconds: int = 30,
) -> None:
    """
    Sends a reply via real browser (Selenium).
    Relies on auth cookies: ct0 + auth_token in `cookie`.
    """
    attempts = _selenium_reply_attempts()
    logger.info(f"reply_to_tweet: up to {attempts} attempts (SELENIUM_REPLY_ATTEMPTS)")
    last: Optional[WebDriverException] = None
    for attempt in range(attempts):
        try:
            if attempt > 0:
                logger.warning(
                    f"Retrying {attempt + 1}/{attempts} following WebDriverException"
                )
            _reply_to_tweet_once(
                cookie=cookie,
                tweet_id=tweet_id,
                reply_text=reply_text,
                proxy=proxy,
                timeout_seconds=timeout_seconds,
            )
            logger.divider("·")
            logger.success(f"Browser reply dispatched successfully (attempt {attempt + 1}/{attempts})")
            return
        except WebDriverException as e:
            last = e
            msg = str(e).lower()
            retryable = (
                "no such window" in msg
                or "invalid session" in msg
                or "web view not found" in msg
            )
            logger.error(
                f"WebDriverException encountered (attempt {attempt + 1}/{attempts}): "
                f"retryable={retryable}"
            )
            logger.trace(str(e)[:500])
            if attempt + 1 >= attempts or not retryable:
                raise
            time.sleep(2.0)
    if last is not None:
        raise last

