"""
Диагностика: смотрим что реально получает Selenium с planeta.ru
и находим правильные CSS-селекторы.

Запуск:
    python diagnose_planeta.py

Результат:
    planeta_listing.html  — HTML страницы листинга (откройте в браузере)
    planeta_project.html  — HTML страницы одного проекта
    selectors_report.txt  — что нашли / не нашли
"""

import time
import json
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup

# ─── Настройка драйвера ────────────────────────────────────────────────────
def make_driver(headless=False):
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    opts.add_argument("--lang=ru-RU,ru;q=0.9")
    opts.add_argument("--window-size=1400,900")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"},
    )
    return driver


def wait_for_page(driver, timeout=30):
    """Ждёт пока страница перестанет крутиться (document.readyState == complete)."""
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )
    time.sleep(3)  # доп. пауза на JS-рендеринг


def check_cloudflare(driver):
    """Проверяет, показывает ли страница Cloudflare-проверку."""
    page = driver.page_source.lower()
    cf_signs = ["checking your browser", "just a moment", "cloudflare", "небольшая проверка", "robota", "robot"]
    for sign in cf_signs:
        if sign in page:
            return True, sign
    return False, None


def save_html(html: str, filename: str):
    Path(filename).write_text(html, encoding="utf-8")
    print(f"  ✓ Сохранено: {filename}")


def analyze_soup(soup: BeautifulSoup, label: str) -> dict:
    """Ищет все возможные CSS-паттерны для ссылок на проекты."""
    report = {"label": label, "found": [], "not_found": [], "all_links_sample": []}

    # Паттерны для поиска ссылок на проекты
    selectors_to_test = [
        "a[href*='/campaigns/']",
        "a.campaign-card__link",
        "a.campaign__link",
        ".campaigns-list a",
        ".campaign-card a",
        "[class*='campaign'] a",
        "[class*='project'] a",
        "article a",
        ".feed a",
        ".list a",
        "[data-id] a",
        "a[href^='/campaigns']",
        ".card a",
        ".item a",
        # новые Planeta.ru после редизайна 2024
        "a[href*='/campaigns/']",
        "[class*='CampaignCard'] a",
        "[class*='campaignCard'] a",
        "a[class*='campaign']",
        "a[class*='Campaign']",
        ".project-card a",
        "[data-campaign] a",
    ]

    for sel in selectors_to_test:
        try:
            found = soup.select(sel)
            if found:
                report["found"].append({
                    "selector": sel,
                    "count": len(found),
                    "sample_href": found[0].get("href", "—"),
                    "sample_text": found[0].get_text(strip=True)[:60],
                })
            else:
                report["not_found"].append(sel)
        except Exception as e:
            report["not_found"].append(f"{sel} (ошибка: {e})")

    # Все ссылки на странице (первые 30)
    all_links = soup.find_all("a", href=True)
    for link in all_links[:30]:
        href = link.get("href", "")
        if "/campaigns/" in href or "/campaign/" in href:
            report["all_links_sample"].append({
                "href": href,
                "text": link.get_text(strip=True)[:60],
                "class": link.get("class", []),
            })

    # Смотрим на классы всех элементов (для поиска паттернов)
    all_classes = set()
    for tag in soup.find_all(class_=True):
        for cls in tag.get("class", []):
            if any(kw in cls.lower() for kw in ("campaign", "project", "card", "list", "feed", "item")):
                all_classes.add(f"{tag.name}.{cls}")
    report["interesting_classes"] = sorted(all_classes)[:50]

    return report


# ─── ГЛАВНАЯ ДИАГНОСТИКА ──────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("Диагностика парсера Planeta.ru")
    print("=" * 60)

    # ШАГ 1: Листинг
    listing_url = "https://planeta.ru/campaigns?category=science"
    alt_urls = [
        "https://planeta.ru/campaigns",
        "https://planeta.ru/campaigns?page=1",
    ]

    print(f"\n[1/3] Открываем браузер (ВИДИМЫЙ режим)...")
    print("      Если появится Cloudflare — пройдите проверку вручную (нажмите кнопку)")
    print("      Скрипт будет ждать до 60 секунд.\n")

    driver = make_driver(headless=False)  # ВИДИМЫЙ браузер

    try:
        # ── Листинг ──────────────────────────────────────────────────────
        print(f"[2/3] Загружаем листинг: {listing_url}")
        driver.get(listing_url)

        # Ждём — даём время пройти Cloudflare вручную если нужно
        print("      Ожидаем загрузки (до 60 сек)... Если видите проверку — пройдите её.")
        try:
            WebDriverWait(driver, 60).until(
                lambda d: (
                    "небольшая проверка" not in d.page_source.lower()
                    and "just a moment" not in d.page_source.lower()
                    and d.execute_script("return document.readyState") == "complete"
                )
            )
        except Exception:
            print("      Таймаут — сохраняем что есть.")

        time.sleep(3)

        is_cf, sign = check_cloudflare(driver)
        if is_cf:
            print(f"  ⚠ Cloudflare обнаружен ('{sign}'). Сохраняем страницу как есть.")
        else:
            print("  ✓ Страница загружена без Cloudflare!")

        listing_html = driver.page_source
        save_html(listing_html, "planeta_listing.html")

        listing_soup = BeautifulSoup(listing_html, "lxml")
        listing_report = analyze_soup(listing_soup, "LISTING")

        print(f"\n  Заголовок страницы: {driver.title}")
        print(f"  URL после редиректа: {driver.current_url}")
        print(f"  Размер HTML: {len(listing_html):,} символов")

        # ── Страница одного проекта ───────────────────────────────────────
        project_url = "https://planeta.ru/campaigns/trvscience"
        print(f"\n[3/3] Загружаем страницу проекта: {project_url}")
        driver.get(project_url)

        try:
            WebDriverWait(driver, 30).until(
                lambda d: (
                    "небольшая проверка" not in d.page_source.lower()
                    and d.execute_script("return document.readyState") == "complete"
                )
            )
        except Exception:
            pass

        time.sleep(3)
        project_html = driver.page_source
        save_html(project_html, "planeta_project.html")
        project_soup = BeautifulSoup(project_html, "lxml")
        project_report = analyze_soup(project_soup, "PROJECT")

        print(f"  Заголовок: {driver.title}")

        # ── Отчёт ────────────────────────────────────────────────────────
        report_lines = []
        report_lines.append("=" * 60)
        report_lines.append("ОТЧЁТ: Что нашли на страницах planeta.ru")
        report_lines.append("=" * 60)

        for rep in [listing_report, project_report]:
            report_lines.append(f"\n{'─'*40}")
            report_lines.append(f"Страница: {rep['label']}")
            report_lines.append(f"{'─'*40}")

            if rep["found"]:
                report_lines.append(f"\n✓ НАЙДЕННЫЕ СЕЛЕКТОРЫ ({len(rep['found'])}):")
                for f in rep["found"]:
                    report_lines.append(
                        f"  [{f['count']:3d}] {f['selector']}\n"
                        f"         href: {f['sample_href']}\n"
                        f"         text: {f['sample_text']}"
                    )
            else:
                report_lines.append("\n✗ Ни один селектор не сработал!")

            if rep["all_links_sample"]:
                report_lines.append(f"\n⟶ ССЫЛКИ ВИДА /campaigns/ на странице:")
                for lnk in rep["all_links_sample"][:15]:
                    report_lines.append(
                        f"  {lnk['href']:<50} class={lnk['class']}"
                    )
            else:
                report_lines.append("\n⟶ Ссылок вида /campaigns/ НЕ найдено!")

            if rep["interesting_classes"]:
                report_lines.append(f"\n⟶ КЛАССЫ с 'campaign/project/card' в имени:")
                for cls in rep["interesting_classes"][:20]:
                    report_lines.append(f"  {cls}")

        report_text = "\n".join(report_lines)
        Path("selectors_report.txt").write_text(report_text, encoding="utf-8")

        print("\n" + report_text)
        print(f"\n✓ Отчёт сохранён: selectors_report.txt")
        print(f"✓ Откройте planeta_listing.html и planeta_project.html в браузере")
        print(f"  чтобы увидеть что получил Selenium")

    finally:
        input("\nНажмите Enter чтобы закрыть браузер...")
        driver.quit()


if __name__ == "__main__":
    main()