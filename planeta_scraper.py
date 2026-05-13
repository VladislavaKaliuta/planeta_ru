"""
Парсер Planeta.ru v3 — исправленные таймауты и обход Cloudflare.

Запуск:
    python planeta_scraper.py

Изменения v3:
    - Таймаут увеличен до 60 сек (Cloudflare проверка занимает ~10-30 сек)
    - Ждём любой элемент <main> вместо ul[class*='list']
    - Если Cloudflare — ждём вручную и продолжаем
    - CSV создаётся даже при 0 результатах
    - Добавлен скролл страницы для подгрузки lazy-элементов
"""

import time
import random
import logging
import re
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional

import pandas as pd
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from webdriver_manager.chrome import ChromeDriverManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("planeta_scraper.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

BASE_URL        = "https://planeta.ru"
# OUTPUT_FILE определяется автоматически из MODE (см. выше)

# ─── ЧТО СОБИРАТЬ ──────────────────────────────────────────────────────────
# Вариант 1: Все категории сразу (один проход, без фильтра)
CATEGORIES = [""]   # пустая строка = параметр category не добавляется в URL

# Вариант 2: Каждую категорию отдельно (больше контроля, можно возобновить)
# Раскомментируйте и закомментируйте Вариант 1 если нужно по категориям:
# CATEGORIES = [
#     "CHECKPOINT", "DESIGN", "FILM", "LITERATURE", "MUSIC", "THEATER",
#     "CHARITY", "SOCIAL", "ECOLOGY_AND_NATURE", "EDUCATION", "SCIENCE",
#     "TECHNOLOGY", "BUSINESS", "SOCIAL_BUSINESS", "SPORT", "GAMES",
#     "TRAVEL", "EVENTS", "FOOD", "CALENDARS", "ARCHITECTURE",
#     "FPSP", "PSYCHOLOGY", "APPS",
# ]

# ═══════════════════════════════════════════════════════════════════════════
# РЕЖИМ СБОРА — меняйте только эти три строки
# ───────────────────────────────────────────────────────────────────────────
# MODE = "class1"  → STATUS="success", собирает успешные проекты (класс 1)
# MODE = "class0"  → STATUS="",        собирает завершённые с pct<50 (класс 0)
# ═══════════════════════════════════════════════════════════════════════════
MAX_PROJECTS    = 200      # сколько новых проектов собрать за запуск (0 = без лимита)
MAX_PAGES       = 8      # лимит прокруток infinite scroll
STATUS          = "success"
OUTPUT_FILE     = "planeta_class1.csv"
DELAY_MIN       = 2.0
DELAY_MAX       = 4.0
PAGE_TIMEOUT    = 60
JS_RENDER_WAIT  = 5.0


@dataclass
class Project:
    project_id:      Optional[str]   = None
    url:             Optional[str]   = None
    title:           Optional[str]   = None
    category:        Optional[str]   = None
    status:          Optional[str]   = None
    region:          Optional[str]   = None
    goal_sum:        Optional[float] = None
    collected_sum:   Optional[float] = None
    collected_pct:   Optional[float] = None
    backers_count:   Optional[int]   = None
    nmb_video:       Optional[int]   = None
    nmb_image:       Optional[int]   = None
    nmb_reward:      Optional[int]   = None
    min_reward:      Optional[float] = None
    max_reward:      Optional[float] = None
    nmb_word:        Optional[int]   = None
    nmb_sentence:    Optional[int]   = None
    short_desc:      Optional[str]   = None
    full_text:       Optional[str]   = None
    author_name:     Optional[str]   = None
    author_projects: Optional[int]   = None
    author_backers:  Optional[int]   = None
    date_start:      Optional[str]   = None
    date_end:        Optional[str]   = None
    updates_count:   Optional[int]   = None
    comments_count:  Optional[int]   = None
    parsed_date:     Optional[str]   = None
    target:          int             = 1    # всегда 1 для успешных


def make_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    opts.add_argument("--lang=ru-RU,ru;q=0.9")
    opts.add_argument("--window-size=1400,900")
    service = Service(ChromeDriverManager().install())
    driver  = webdriver.Chrome(service=service, options=opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"},
    )
    return driver


def is_cloudflare(driver) -> bool:
    src = driver.page_source.lower()
    return any(s in src for s in ("небольшая проверка", "just a moment",
                                   "checking your browser", "cf-browser-verification"))


def wait_for_page(driver, url: str) -> Optional[BeautifulSoup]:
    try:
        driver.get(url)
    except Exception as e:
        log.warning("Ошибка навигации: %s", e)
        return None

    deadline = time.time() + PAGE_TIMEOUT
    cloudflare_warned = False

    while time.time() < deadline:
        if is_cloudflare(driver):
            if not cloudflare_warned:
                log.warning("Cloudflare! Пройдите проверку в браузере вручную...")
                print("\n" + "="*55)
                print("CLOUDFLARE ПРОВЕРКА!")
                print("Нажмите кнопку/чекбокс в открытом браузере.")
                print("Скрипт продолжит автоматически.")
                print("="*55 + "\n")
                cloudflare_warned = True
            time.sleep(2)
            continue

        try:
            WebDriverWait(driver, 3).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "main"))
            )
            time.sleep(JS_RENDER_WAIT)
            # Скролл — подгружаем lazy-блоки (автор, описание)
            driver.execute_script("window.scrollTo(0, 600);")
            time.sleep(1.0)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.4);")
            time.sleep(1.0)
            driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(0.5)
            return BeautifulSoup(driver.page_source, "lxml")
        except TimeoutException:
            time.sleep(2)
            continue

    log.warning("Таймаут (%d сек): %s", PAGE_TIMEOUT, url)
    src = driver.page_source
    if src:
        Path(f"debug_timeout.html").write_text(src, encoding="utf-8")
        log.info("Сохранён debug_timeout.html для анализа")
    return BeautifulSoup(src, "lxml") if src else None


def clean_text(text: str) -> str:
    """Убирает HTML-артефакты: NBSP, нулевые пробелы, HTML-сущности."""
    if not text:
        return text
    text = text.replace('\xa0', ' ')
    text = text.replace('\u200b', '')
    text = text.replace('\u2019', "'")
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;',  '&', text)
    text = re.sub(r'&lt;',   '<', text)
    text = re.sub(r'&gt;',   '>', text)
    text = re.sub(r' {2,}', ' ', text)
    return text.strip()


def _money(text: str) -> Optional[float]:
    if not text:
        return None
    clean = re.sub(r"[^\d,.]", "", text.replace("\xa0", "").replace(" ", ""))
    try:
        return float(clean.replace(",", "."))
    except ValueError:
        return None


def _int(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def _words(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text, re.UNICODE)) if text else 0


def _sentences(text: str) -> int:
    return len(re.split(r"[.!?]+", text.strip())) if text and text.strip() else 0


def collect_listing_urls(driver: webdriver.Chrome, category: str) -> list[str]:
    """
    Planeta.ru использует infinite scroll — новые карточки подгружаются
    при прокрутке вниз, а не при переходе на страницу 2.
    Стратегия: открываем листинг один раз и скроллим до конца.
    """
    urls: list[str] = []

    parts = []
    if category:
        parts.append("category=" + category)
    if STATUS:
        parts.append("status=" + STATUS)

    base_url = BASE_URL + "/search/projects"
    if parts:
        base_url += "?" + "&".join(parts)

    label = category if category else "ВСЕ КАТЕГОРИИ"
    log.info("[%s] Открываем листинг: %s", label, base_url)

    soup = wait_for_page(driver, base_url)
    if soup is None:
        log.warning("Листинг не загрузился.")
        return urls

    # Страница загружает проекты по кнопке "Ещё проекты"
    # Кликаем пока кнопка есть на странице

    BTN_SELECTOR = "button[class*='btnMore']"

    def collect_urls_from_page():
        """Собирает все ссылки на проекты с текущей страницы."""
        src = BeautifulSoup(driver.page_source, "lxml")
        before = len(urls)
        for a in src.find_all("a", href=re.compile(r"^/campaigns/[^/]+$")):
            u = BASE_URL + a["href"]
            if u not in urls:
                urls.append(u)
        return len(urls) - before

    def find_btn():

        selectors = [

            # новый текст
            "//button[contains(., 'Показать ещё')]",

            # старый текст (fallback)
            "//button[contains(., 'Ещё проекты')]",

            # по class
            "//button[contains(@class, 'btnMore')]",

        ]

        for xpath in selectors:

            try:
                btn = driver.find_element(By.XPATH, xpath)

                if btn:
                    return btn

            except Exception:
                continue

        return None

    # Ждём появления первых карточек и кнопки (страница грузится через React)
    log.info("  Ждём загрузки первых карточек...")
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/campaigns/']"))
        )
    except TimeoutException:
        log.warning("  Карточки не появились за 20 сек")

    time.sleep(3.0)  # дополнительная пауза для рендера React

    # Собираем первую порцию
    added = collect_urls_from_page()
    log.info("  Первая порция: %d ссылок", added)

    # Кликаем кнопку пока она есть
    click_count = 0
    no_btn_count = 0  # сколько раз подряд кнопка не нашлась

    while click_count < MAX_PAGES:
        log.info("=== НОВЫЙ ЦИКЛ ===")
        log.info("Скроллим вниз...")
        # Скроллим к последней карточке проекта
        cards = driver.find_elements(
            By.CSS_SELECTOR,
            "a[href*='/campaigns/']"
        )

        if cards:
            driver.execute_script("""
                arguments[0].scrollIntoView({
                    behavior: 'instant',
                    block: 'center'
                });
            """, cards[-1])

            time.sleep(1.5)

        time.sleep(3)

        log.info("Ищем кнопку...")
        btn = find_btn()

        log.info("Кнопка: %s", "НАЙДЕНА" if btn else "НЕТ")

        if btn is None:
            if no_btn_count >= 5:
                log.info("Кнопка исчезла окончательно.")
                break
            continue

        no_btn_count = 0

        prev_count = len(urls)

        try:
            # скроллим к кнопке
            driver.execute_script("""
                arguments[0].scrollIntoView({
                    behavior: 'instant',
                    block: 'center'
                });
            """, btn)

            time.sleep(1)

            # JS click надёжнее
            driver.execute_script("""
                arguments[0].dispatchEvent(
                    new MouseEvent('click', {
                        bubbles: true,
                        cancelable: true,
                        view: window
                    })
                );
            """, btn)

            click_count += 1

            log.info("Клик %d", click_count)

        except Exception as e:
            log.warning("Ошибка клика: %s", e)
            time.sleep(2)
            continue

        # ЖДЁМ НОВЫЕ КАРТОЧКИ
        loaded = False

        for _ in range(20):  # ждём до ~20 сек

            time.sleep(1)

            collect_urls_from_page()

            current = len(urls)

            if current > prev_count:
                log.info("Добавилось %d проектов", current - prev_count)
                loaded = True
                break

        if not loaded:
            log.info("Новые карточки не появились")

            # возможно кнопка ещё есть
            time.sleep(3)

            collect_urls_from_page()

            if len(urls) == prev_count:
                log.info("Похоже, проекты закончились")
                break

    # Финальный сбор на случай если что-то пропустили
    collect_urls_from_page()

    log.info("Итого собрано %d ссылок на проекты.", len(urls))
    return urls


def parse_project(driver: webdriver.Chrome, url: str, category: str) -> Optional[Project]:
    soup = wait_for_page(driver, url)
    if soup is None:
        return None

    p = Project(url=url, category=category)
    m = re.search(r"/campaigns/([^/?#]+)", url)
    p.project_id = m.group(1) if m else url.split("/")[-1]

    h1 = soup.find("h1", class_=re.compile("title"))
    p.title = clean_text(h1.get_text(strip=True)) if h1 else None

    short_p = soup.find("p", class_=re.compile("description"))
    p.short_desc = clean_text(short_p.get_text(strip=True)) if short_p else None

    sum_el = soup.find("span", class_=re.compile("fundingSumValue"))
    p.collected_sum = _money(sum_el.get_text()) if sum_el else None

    target_el = soup.find("span", class_=re.compile("fundingSumTarget"))
    p.goal_sum = _money(target_el.get_text()) if target_el else None

    pct_el = soup.find("p", class_=re.compile("progress-text"))
    if pct_el:
        p.collected_pct = _money(pct_el.get_text())
    elif p.goal_sum and p.collected_sum and p.goal_sum > 0:
        p.collected_pct = round(p.collected_sum / p.goal_sum * 100, 2)

    status_el = soup.find(class_=re.compile("status"))
    if status_el:
        raw = status_el.get_text(strip=True).lower()
        if any(w in raw for w in ("успешн", "завершён", "funded")):
            p.status = "successful"
        elif any(w in raw for w in ("не собрал", "failed", "неуспешн")):
            p.status = "failed"
        elif any(w in raw for w in ("идёт", "сбор", "active")):
            p.status = "active"
    if not p.status and p.collected_pct is not None:
        p.status = "successful" if p.collected_pct >= 50 else "failed"

    from datetime import date as _date
    p.parsed_date = _date.today().isoformat()

    for dt_el in soup.find_all("dt"):
        label = dt_el.get_text(strip=True).lower()
        dd_el = dt_el.find_next_sibling("dd")
        if not dd_el:
            continue
        val = dd_el.get_text(strip=True)
        if "поддержали" in label:
            p.backers_count = _int(val)
        elif "запущен" in label:
            p.date_start = val
        elif any(w in label for w in ("завершён","завершился","действовал до","окончание")):
            if re.match(r"\d{2}\.\d{2}\.\d{4}", val.strip()):
                p.date_end = val.strip()
        elif "осталось" in label:
            pass  # не сохраняем — меняется каждый день
        elif "регион" in label:
            p.region = val
        elif "категория" in label:
            lnk = dd_el.find("a")
            if lnk:
                p.category = lnk.get_text(strip=True)

    author_name_el = soup.find("h2", class_=re.compile("authorName"))
    p.author_name = author_name_el.get_text(strip=True) if author_name_el else None

    meta_vals = soup.find_all("dd", class_=re.compile("authorMetaValue"))
    if len(meta_vals) >= 1:
        p.author_projects = _int(meta_vals[0].get_text())
    if len(meta_vals) >= 2:
        p.author_backers  = _int(meta_vals[1].get_text())

    rewards = [r for r in soup.find_all("article", class_=re.compile("reward"))
               if r.get("id", "") != "reward-donate"]
    p.nmb_reward = len(rewards)
    prices = []
    for r in rewards:
        price_el = r.find("dd", class_=re.compile("priceValue"))
        if price_el:
            v = _money(price_el.get_text())
            if v is not None:
                prices.append(v)
    if prices:
        p.min_reward = min(prices)
        p.max_reward = max(prices)

    # Инициализируем 0 — если вкладка есть но без счётчика значит 0
    p.updates_count  = 0
    p.comments_count = 0
    for a in soup.select("a[class*='link']"):
        href = a.get("href", "")
        count_el = a.find("div", class_=re.compile("count"))
        count = _int(count_el.find("span").get_text()) if count_el and count_el.find("span") else 0
        if "/updates" in href:
            p.updates_count = count
        elif "/comments" in href:
            p.comments_count = count

    desc_block = soup.find("div", class_=re.compile(
        r"common-wrapper-module__wrapper|campaign-info-module__text"
    ))
    if desc_block:
        iframes = desc_block.find_all("iframe", src=re.compile(r"youtube|vimeo|rutube", re.I))
        video_tags = desc_block.find_all("video")
        video_divs = desc_block.find_all(attrs={"data-video": True})
        p.nmb_video = len(iframes) + len(video_tags) + len(video_divs)
        imgs = [img for img in desc_block.find_all("img")
                if "fluentui-emoji" not in img.get("src", "")]
        p.nmb_image = len(imgs)
        raw_text = desc_block.get_text(" ", strip=True)
        p.nmb_word     = _words(raw_text)
        p.nmb_sentence = _sentences(raw_text)
        p.full_text    = clean_text(raw_text) if raw_text else None

    return p


def _flush(rows: list, path: Path):
    """Сохраняет список dict-ов в CSV с QUOTE_ALL (запятые в текстах не ломают файл)."""
    import csv as _csv
    df = pd.DataFrame(rows)
    df.to_csv(path, mode="a", header=False, index=False,
              encoding="utf-8-sig", quoting=_csv.QUOTE_ALL)


def _append_csv(projects: list, path: Path):
    """Обратная совместимость — вызывает _flush."""
    import dataclasses
    _flush([dataclasses.asdict(p) if not isinstance(p, dict) else p for p in projects], path)


def run():
    import csv as _csv

    output_path = Path(OUTPUT_FILE)
    seen_urls: set[str] = set()

    log.info("РЕЖИМ: %s | лимит: %s | файл: %s",
             "class1", MAX_PROJECTS if MAX_PROJECTS else "∞", output_path.name)

    col_names = list(Project.__dataclass_fields__.keys())

    if output_path.exists():
        with open(output_path, encoding="utf-8-sig", newline="") as _f:
            import csv as _csv3
            all_rows = list(_csv3.reader(_f))

        if not all_rows:
            pd.DataFrame(columns=col_names).to_csv(
                output_path, index=False, encoding="utf-8-sig", quoting=_csv.QUOTE_ALL)
        else:
            has_header = (all_rows[0][0] == "project_id" if all_rows[0] else False)
            if not has_header:
                log.warning("Заголовок отсутствует — восстанавливаем...")
                valid = [r for r in all_rows if len(r) == len(col_names)]
                pd.DataFrame(valid, columns=col_names).to_csv(
                    output_path, index=False, encoding="utf-8-sig", quoting=_csv.QUOTE_ALL)
                with open(output_path, encoding="utf-8-sig", newline="") as _f:
                    all_rows = list(_csv3.reader(_f))
            url_idx = all_rows[0].index("url") if "url" in all_rows[0] else 1
            for row in all_rows[1:]:
                if len(row) > url_idx and row[url_idx].startswith("http"):
                    seen_urls.add(row[url_idx])
        log.info("Возобновление: уже %d проектов.", len(seen_urls))
    else:
        pd.DataFrame(columns=col_names).to_csv(
            output_path, index=False, encoding="utf-8-sig", quoting=_csv.QUOTE_ALL)
        log.info("Создан файл: %s", output_path.absolute())

    driver = make_driver()
    buffer: list = []
    saved_count = 0

    try:
        for cat in CATEGORIES:
            if MAX_PROJECTS and saved_count >= MAX_PROJECTS:
                log.info("Лимит %d достигнут — стоп.", MAX_PROJECTS)
                break

            log.info("==== Категория: %s ====", cat or "ВСЕ")
            listing_urls = collect_listing_urls(driver, cat)
            log.info("Найдено %d URL", len(listing_urls))

            for i, url in enumerate(listing_urls, 1):
                if MAX_PROJECTS and saved_count >= MAX_PROJECTS:
                    break

                if url in seen_urls:
                    continue

                log.info("[%d/%d] %s", i, len(listing_urls), url)
                project = parse_project(driver, url, cat)

                if not project:
                    log.warning("  FAIL: %s", url)
                    continue

                pct = project.collected_pct or 0

                # ── Фильтры по режиму ────────────────────────────────────
                # Фильтр: только успешные завершённые (pct >= 50)
                # if pct < 50:
                #     log.info("  SKIP (pct=%.0f%% < 50): '%s'", pct, project.title)
                #     seen_urls.add(url)
                #     continue

                # ── Сохраняем ────────────────────────────────────────────
                import dataclasses
                row = dataclasses.asdict(project)
                row["target"] = 1
                buffer.append(row)
                seen_urls.add(url)
                saved_count += 1

                left = f"{MAX_PROJECTS - saved_count} до лимита" if MAX_PROJECTS else "∞"
                log.info("  OK [%d, %s] target=1: '%s' | %.0f%%",
                         saved_count, left, project.title, pct)

                if len(buffer) >= 10:
                    _flush(buffer, output_path)
                    buffer.clear()

                time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    except KeyboardInterrupt:
        log.info("Остановлено вручную (Ctrl+C).")
    finally:
        if buffer:
            _flush(buffer, output_path)
        driver.quit()

    # Итоговая статистика
    df = pd.read_csv(output_path, engine="python", on_bad_lines="warn")
    if "target" in df.columns:
        c1 = (df["target"] == 1).sum()
        c0 = (df["target"] == 0).sum()
        log.info("ИТОГО: %d строк | класс 1 (успех): %d | класс 0 (неуспех): %d", len(df), c1, c0)
        print(f"\nВсего: {len(df)} | Успешных (класс 1): {c1} | Неуспешных (класс 0): {c0}")
    else:
        log.info("ИТОГО: %d строк → %s", len(df), output_path.absolute())
    print(f"Файл: {output_path.absolute()}")
    print(f"Новых за этот запуск: {saved_count}")


if __name__ == "__main__":
    run()