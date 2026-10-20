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
    Коротко по-русски + одна строка от драйвера (без полного stacktrace в логе).
    """
    raw = str(exc).strip()
    low = raw.lower()

    if "element click intercepted" in low:
        hint = (
            "Selenium: клик перехвачен другим слоем страницы.\n"
            "Частая причина на X: на экране два поля «Post text» с одинаковым data-testid — "
            "клик попал не в то (модалка ответа перекрыта или продублирована). "
            "Скрипт должен целиться в поле внутри окна ответа (role=dialog)."
        )
    elif "stale element" in low or "staleelementreference" in low.replace(" ", ""):
        hint = (
            "Selenium: элемент исчез со страницы до завершения действия (DOM обновился). "
            "Повторите отправку или увеличьте паузы."
        )
    elif "timeout" in low or isinstance(exc, TimeoutException):
        hint = (
            "Selenium: превышено время ожидания элемента (страница не успела отрисоваться, "
            "другой layout или нет доступа к твиту)."
        )
    elif "session not created" in low or "only supports chrome version" in low:
        hint = (
            "Selenium: версия ChromeDriver не совпадает с установленным Chrome.\n"
            "Скрипт подставляет major из `Google Chrome --version`; при сбое задайте UC_CHROME_VERSION_MAIN=146 "
            "(или ваш major) и перезапустите."
        )
    elif "invalid session" in low or "no such window" in low or "web view not found" in low:
        hint = (
            "Selenium: сессия браузера закрыта или окно пропало.\n"
            "На macOS чаще всего помогает UC_USE_SUBPROCESS=0 (уже по умолчанию для Darwin) "
            "или обновление Chrome; убедитесь, что окно автоматизации не закрываете вручную."
        )
    else:
        hint = "Selenium / браузер:\n"

    one_line = raw.replace("\n", " ")
    if len(one_line) > 280:
        one_line = one_line[:277] + "..."
    return f"{hint}\nДеталь: {one_line}"


def _scroll_center(driver: webdriver.Chrome, el) -> None:
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});",
        el,
    )


def _scroll_clear_sticky_header(driver: webdriver.Chrome, el, offset_px: int = 140) -> None:
    """Скролл к элементу + сдвиг вверх, чтобы Reply не оказался под фиксированной шапкой X."""
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});",
        el,
    )
    time.sleep(0.12)
    driver.execute_script("window.scrollBy(0, arguments[0]);", -offset_px)


def _robust_click(driver: webdriver.Chrome, el, pause: float = 0.35) -> None:
    """
    Цепочка кликов: обычный → ActionChains (центр элемента) → JS click → synthetic MouseEvent.
    Нужна, когда span/оверлей перехватывает клик (element click intercepted).
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
    """Обёртка: тот же robust-клик (шапка X, перехват клика)."""
    _robust_click(driver, el, pause=pause)


def _focus_contenteditable(driver: webdriver.Chrome, el) -> None:
    driver.execute_script(
        "arguments[0].focus(); arguments[0].click();",
        el,
    )


def _find_reply_button_on_tweet_page(driver: webdriver.Chrome, wait: WebDriverWait):
    """Reply на фокальном твите: ждём кликабельность, не только presence."""
    wait.until(
        EC.presence_of_element_located((By.CSS_SELECTOR, 'div[data-testid="primaryColumn"]'))
    )
    time.sleep(0.45)
    # На странице статуса первый article — обычно сам твит; кликабельность снижает ложные перехваты.
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
    На X часто несколько Draft.js с data-testid=tweetTextarea_0; берём поле внутри модалки ответа.
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
        "Не найдено видимое поле ввода (tweetTextarea_0). Модалка ответа могла не открыться."
    )


def _find_dialog_tweet_button(driver: webdriver.Chrome, wait: WebDriverWait):
    """Кнопка Reply в той же модалке, не другая Tweet на странице."""

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
    По умолчанию ответы через браузер (Selenium).
    Отключить: USE_SELENIUM_REPLY=0 (или false / no / off).
    При сбое Selenium см. selenium_api_fallback_enabled() / SELENIUM_FALLBACK_API.
    """
    v = os.getenv("USE_SELENIUM_REPLY", "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    if v in ("1", "true", "yes", "on"):
        return True
    return True


def selenium_api_fallback_enabled() -> bool:
    """
    Если Selenium упал — повторить отправку через GraphQL CreateTweet (Twitter.reply_to_tweet).
    Отключить: SELENIUM_FALLBACK_API=0
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
    По умолчанию браузер видимый (окно на экране).
    Headless: SELENIUM_HEADLESS=1.
    Чтобы принудительно показать окно даже при SELENIUM_HEADLESS=1 в .env:
    SELENIUM_VISIBLE=1 (или SHOW_SELENIUM_BROWSER=1).
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
    """Chrome через undetected-chromedriver (патч драйвера, меньше следов автоматизации)."""
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
        logger.timing("Chrome (plain Selenium) старт", time.perf_counter() - t_drv)
        return d, h

    options = uc.ChromeOptions()
    options.add_argument("--lang=en-US")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-notifications")

    headless = _headless_enabled()
    if headless:
        logger.info("Selenium: режим headless (окна не будет). Чтобы видеть браузер: уберите SELENIUM_HEADLESS из .env или задайте SELENIUM_VISIBLE=1")
    else:
        logger.success("Selenium: видимое окно Chrome (можно следить за действиями)")
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
            "proxy": "да" if proxy else "нет",
        },
    )
    driver = uc.Chrome(
        options=options,
        headless=headless,
        use_subprocess=use_sub,
        version_main=vm,
    )
    logger.timing("Chrome (uc) старт", time.perf_counter() - t_drv)
    try:
        logger.trace(f"current_url (после старта): {driver.current_url!r}")
    except Exception as e:
        logger.trace(f"current_url недоступен: {e}")
    return driver, headless


def _uc_use_subprocess() -> bool:
    """На macOS use_subprocess=True часто даёт «target window already closed»."""
    v = os.getenv("UC_USE_SUBPROCESS", "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return sys.platform != "darwin"


def _uc_version_main() -> Optional[int]:
    """
    Major-версия Chrome для патча драйвера (иначе uc может скачать 147 при установленном 146).
    Переопределение: UC_CHROME_VERSION_MAIN=146
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
        logger.info("Selenium (plain): headless. Для окна: SELENIUM_VISIBLE=1 или отключите SELENIUM_HEADLESS")
    else:
        logger.success("Selenium (plain): видимое окно")
    logger.panel(
        "Chrome / обычный Selenium",
        {"headless": headless, "proxy": "да" if proxy else "нет"},
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
    Плашка «There's a new version of this post» / «See the latest post» часто
    перекрывает нижнюю панель твита (Reply). Скрываем блок в primaryColumn.
    Отключить: SELENIUM_SUPPRESS_BANNERS=0
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
              'есть новая версия',
              'посмотреть новую версию',
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
    logger.section("Selenium · ответ на твит", subtitle=f"id={tweet_id}")
    logger.panel(
        "Запрос",
        {
            "tweet_id": tweet_id,
            "timeout_sec": timeout_seconds,
            "proxy": "да" if proxy else "нет",
            "USE_SELENIUM_REPLY": use_selenium_reply(),
            "SELENIUM_HEADLESS": _headless_enabled(),
            "попыток (макс.)": _selenium_reply_attempts(),
            "длина текста": len(reply_text),
        },
    )
    preview = reply_text.replace("\n", " ")[:100]
    logger.trace(f"текст (превью): {preview!r}{'…' if len(reply_text) > 100 else ''}")
    logger.debug(f"ct0 present={bool(ct0)} auth_token present={bool(auth_token)} (значения не логируются)")

    driver, headless = _build_driver(proxy=proxy)
    wait = WebDriverWait(driver, timeout_seconds)
    try:
        driver.set_page_load_timeout(max(60, timeout_seconds + 30))
        logger.step(1, 7, "Загрузка x.com (контекст куки)")
        t = _t0()
        driver.get("https://x.com/")
        time.sleep(1.0)
        logger.timing("GET x.com/", time.perf_counter() - t)
        logger.trace(f"url после x.com: {driver.current_url!r}")

        driver.add_cookie({"name": "ct0", "value": ct0, "domain": ".x.com", "path": "/"})
        driver.add_cookie({"name": "auth_token", "value": auth_token, "domain": ".x.com", "path": "/"})
        logger.success("Куки ct0 + auth_token записаны в сессию")

        tweet_url = f"https://x.com/i/web/status/{tweet_id}"
        logger.step(2, 7, f"Открытие твита")
        logger.info(f"URL: {tweet_url}")
        t = _t0()
        driver.get(tweet_url)
        time.sleep(0.8)
        logger.timing("GET статуса", time.perf_counter() - t)
        logger.trace(f"url: {driver.current_url!r} title: {driver.title!r}")

        if _x_showing_error_page(driver):
            logger.error("Страница X: «Something went wrong» или rate limit (фрагмент HTML распознан)")
            raise RuntimeError(
                "X отдал страницу «Something went wrong» (твит недоступен, сеть, расширения или нужен вход). "
                f"URL: {tweet_url}"
            )

        logger.step(3, 7, "Ожидание article в primaryColumn")
        t = _t0()
        WebDriverWait(driver, timeout_seconds).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, 'div[data-testid="primaryColumn"] article')
            )
        )
        time.sleep(0.4)
        logger.timing("article готов", time.perf_counter() - t)
        arts = driver.find_elements(
            By.CSS_SELECTOR, 'div[data-testid="primaryColumn"] article'
        )
        logger.debug(f"article в колонке: {len(arts)}")

        hid = _suppress_x_tweet_chrome_banners(driver)
        if hid:
            logger.info(
                f"Скрыто плашек в primaryColumn (напр. «new version / See the latest post»): {hid}"
            )
        time.sleep(0.25)

        logger.step(4, 7, "Клик Reply")
        reply_btn = _find_reply_button_on_tweet_page(driver, wait)
        _safe_click(driver, reply_btn)
        logger.trace("кнопка Reply нажата")

        time.sleep(0.35)
        logger.step(5, 7, "Поле ввода + send_keys")
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
        logger.trace(f"send_keys завершён, символов={len(reply_text)}")

        logger.step(6, 7, "Клик отправки (tweetButton в диалоге)")
        send_btn = _find_dialog_tweet_button(driver, wait)
        _safe_click(driver, send_btn)
        logger.info("Отправка нажата, ждём закрытие модалки / staleness поля")

        logger.step(7, 7, "Ожидание результата")
        t = _t0()
        verify_send = _env_truthy("SELENIUM_REPLY_VERIFY_SEND", False)
        try:
            WebDriverWait(driver, min(25, timeout_seconds)).until(staleness_of(textbox))
            logger.timing("поле стало stale (успех вероятен)", time.perf_counter() - t)
            logger.success("Модалка/поле ушло из DOM — ответ, скорее всего, ушёл")
        except TimeoutException:
            logger.warning(
                "staleness: таймаут — поле композера осталось в DOM после клика Send "
                "(ошибка X, клик не дошёл, или медленная сеть)."
            )
            time.sleep(1.5)
            logger.timing("fallback sleep 1.5s", time.perf_counter() - t)
            if verify_send:
                raise RuntimeError(
                    "SELENIUM_REPLY_VERIFY_SEND=1: отправка не подтверждена — "
                    "модалка ответа не закрылась за отведённое время."
                ) from None
        except Exception as ex:
            logger.warning(f"staleness: {type(ex).__name__}: {ex}")
            time.sleep(1.5)
            logger.timing("fallback sleep 1.5s", time.perf_counter() - t)
            if verify_send:
                raise RuntimeError(
                    "SELENIUM_REPLY_VERIFY_SEND=1: после Send не удалось дождаться "
                    f"закрытия композера ({type(ex).__name__})."
                ) from ex

        logger.timing("сессия Selenium (вся)", time.perf_counter() - session_t0)
    finally:
        delay = _close_delay_seconds(headless)
        if delay > 0:
            logger.debug(f"SELENIUM_CLOSE_DELAY_SEC: пауза {delay}s перед quit")
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
    logger.info(f"reply_to_tweet: до {attempts} попыток (SELENIUM_REPLY_ATTEMPTS)")
    last: Optional[WebDriverException] = None
    for attempt in range(attempts):
        try:
            if attempt > 0:
                logger.warning(
                    f"Повторная попытка {attempt + 1}/{attempts} после WebDriverException"
                )
            _reply_to_tweet_once(
                cookie=cookie,
                tweet_id=tweet_id,
                reply_text=reply_text,
                proxy=proxy,
                timeout_seconds=timeout_seconds,
            )
            logger.divider("·")
            logger.success(f"Selenium-ответ завершён (попытка {attempt + 1}/{attempts})")
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
                f"WebDriverException (попытка {attempt + 1}/{attempts}): "
                f"retryable={retryable}"
            )
            logger.trace(str(e)[:500])
            if attempt + 1 >= attempts or not retryable:
                raise
            time.sleep(2.0)
    if last is not None:
        raise last

