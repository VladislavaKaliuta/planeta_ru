"""
Поиск неуспешных проектов Planeta.ru через Wayback Machine (archive.org).

Логика:
  1. CDX API возвращает все сохранённые снапшоты planeta.ru/campaigns/*
  2. Для каждого URL смотрим снапшоты за разные годы
  3. Парсим архивную страницу — если проект завершён и pct < 50 → класс 0
  4. Сохраняем в wayback_class0.csv

Зависимости:
    pip install requests beautifulsoup4 lxml pandas

Запуск:
    python wayback_scraper.py
"""

import time
import re
import logging
import random
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional

import requests
import pandas as pd
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("wayback_scraper.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ─── Конфигурация ─────────────────────────────────────────────────────────
OUTPUT_FILE  = "wayback_class0.csv"
CDX_API      = "https://web.archive.org/cdx/search/cdx"
WB_BASE      = "https://web.archive.org/web"
MAX_PROJECTS = 200       # сколько неуспешных проектов хотим найти (0 = без лимита)
YEARS        = ["2019", "2020", "2021", "2022", "2023", "2024"]  # годы для поиска
DELAY_MIN    = 2.0       # вежливая пауза между запросами
DELAY_MAX    = 4.0
REQUEST_TIMEOUT = 30


# ─── Структура проекта ─────────────────────────────────────────────────────
@dataclass
class Project:
    project_id:    Optional[str]   = None
    url:           Optional[str]   = None   # оригинальный URL на planeta.ru
    wayback_url:   Optional[str]   = None   # URL архивного снапшота
    snapshot_date: Optional[str]   = None   # дата снапшота YYYYMMDD
    title:         Optional[str]   = None
    category:      Optional[str]   = None
    status:        Optional[str]   = None
    region:        Optional[str]   = None
    goal_sum:      Optional[float] = None
    collected_sum: Optional[float] = None
    collected_pct: Optional[float] = None
    backers_count: Optional[int]   = None
    nmb_reward:    Optional[int]   = None
    min_reward:    Optional[float] = None
    max_reward:    Optional[float] = None
    nmb_word:      Optional[int]   = None
    nmb_image:     Optional[int]   = None
    nmb_video:     Optional[int]   = None
    short_desc:    Optional[str]   = None
    author_name:   Optional[str]   = None
    author_projects: Optional[int] = None
    date_start:    Optional[str]   = None
    target:        int             = 0      # всегда 0 для этого файла


# ─── Утилиты ──────────────────────────────────────────────────────────────
def _get(url: str, params: dict = None, retries: int = 3) -> Optional[requests.Response]:
    """GET с повторными попытками и задержкой."""
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT,
                             headers={"User-Agent": "Mozilla/5.0 research project"})
            if r.status_code == 200:
                return r
            log.warning("HTTP %d: %s", r.status_code, url)
        except requests.RequestException as e:
            log.warning("Попытка %d/%d: %s", attempt + 1, retries, e)
        time.sleep(2 ** attempt)  # экспоненциальная задержка
    return None


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


# ─── ШАГ 1: Получаем список URL из CDX API ────────────────────────────────
def get_cdx_urls(year: str, page_size: int = 200) -> list[tuple]:
    """
    Запрашивает CDX API постранично (resumeKey) — не вызывает таймаутов.
    Возвращает список (original_url, timestamp).

    Ключевые решения:
      - HTTPS (не HTTP) — иначе таймаут
      - page_size=200 — небольшими порциями
      - resumeKey — пагинация без лимита
      - collapse=urlkey — один снапшот на URL
    """
    log.info("CDX: запрашиваем снапшоты за %s (постранично)...", year)

    results = []
    resume_key = None
    page = 0

    while True:
        params = {
            "url":       "planeta.ru/campaigns/*",
            "matchType": "prefix",
            "output":    "json",
            "fl":        "original,timestamp",
            "filter":    ["statuscode:200", "mimetype:text/html"],
            "from":      f"{year}0101",
            "to":        f"{year}1231",
            "collapse":  "urlkey",
            "limit":     page_size,
            "showResumeKey": "true",
        }
        if resume_key:
            params["resumeKey"] = resume_key

        r = _get(CDX_API, params=params)
        if not r:
            log.warning("  CDX не ответил (стр. %d) для %s", page, year)
            break

        try:
            data = r.json()
        except Exception:
            log.warning("  CDX вернул невалидный JSON для %s", year)
            break

        if not data or len(data) <= 1:
            break

        # Последняя строка может быть resumeKey (не список)
        last = data[-1]
        if isinstance(last, list) and len(last) == 1:
            # Это resumeKey
            resume_key = last[0]
            rows = data[1:-1]
        elif isinstance(last, str):
            resume_key = last
            rows = data[1:-1]
        else:
            resume_key = None
            rows = data[1:]

        headers = data[0]
        orig_idx = headers.index("original") if "original" in headers else 0
        ts_idx   = headers.index("timestamp") if "timestamp" in headers else 1

        new_found = 0
        for row in rows:
            if not isinstance(row, list) or len(row) < 2:
                continue
            original  = row[orig_idx]
            timestamp = row[ts_idx]
            path_part = original.split("planeta.ru")[-1].split("?")[0]
            if re.match(r"^/campaigns/[^/?#]+/?$", path_part):
                results.append((original, timestamp))
                new_found += 1

        page += 1
        log.info("  Стр. %d: +%d URL (итого %d)", page, new_found, len(results))

        if not resume_key or new_found == 0:
            break

        time.sleep(1.5)  # пауза между страницами

    log.info("  За %s: %d уникальных URL проектов.", year, len(results))
    return results


# ─── ШАГ 2: Парсим архивную страницу ─────────────────────────────────────
def parse_wayback_page(original_url: str, timestamp: str) -> Optional[Project]:
    """
    Загружает архивную страницу и парсит данные проекта.
    Возвращает Project или None если страница не подходит.
    """
    wayback_url = f"{WB_BASE}/{timestamp}/{original_url}"

    r = _get(wayback_url)
    if not r:
        return None

    soup = BeautifulSoup(r.text, "lxml")

    p = Project(
        url          = original_url,
        wayback_url  = wayback_url,
        snapshot_date= timestamp[:8],  # YYYYMMDD
    )

    # ID из URL
    m = re.search(r"/campaigns/([^/?#]+)", original_url)
    p.project_id = m.group(1) if m else None

    # Заголовок
    h1 = soup.find("h1", class_=re.compile("title"))
    if not h1:
        h1 = soup.find("h1")
    p.title = h1.get_text(strip=True) if h1 else None

    # Если нет заголовка — страница не загрузилась нормально
    if not p.title:
        return None

    # Короткое описание
    desc_el = soup.find("p", class_=re.compile("description"))
    p.short_desc = desc_el.get_text(strip=True) if desc_el else None

    # Финансовые данные
    sum_el = soup.find("span", class_=re.compile("fundingSumValue"))
    p.collected_sum = _money(sum_el.get_text()) if sum_el else None

    target_el = soup.find("span", class_=re.compile("fundingSumTarget"))
    p.goal_sum = _money(target_el.get_text()) if target_el else None

    pct_el = soup.find("p", class_=re.compile("progress-text"))
    if pct_el:
        p.collected_pct = _money(pct_el.get_text())
    elif p.goal_sum and p.collected_sum and p.goal_sum > 0:
        p.collected_pct = round(p.collected_sum / p.goal_sum * 100, 2)

    # Если нет финансовых данных — старый дизайн сайта, пробуем другие селекторы
    if p.collected_pct is None:
        # Старый дизайн Planeta (до 2022)
        bar = soup.find("div", class_=re.compile("progress|funded|collected"))
        if bar:
            style = bar.get("style", "")
            m_pct = re.search(r"width:\s*([\d.]+)%", style)
            if m_pct:
                p.collected_pct = float(m_pct.group(1))

        # Ищем числа с % рядом с "собрано"
        if p.collected_pct is None:
            for text in soup.find_all(string=re.compile(r"\d+\s*%")):
                m_p = re.search(r"(\d+)\s*%", text)
                if m_p:
                    val = int(m_p.group(1))
                    if 0 <= val <= 500:  # разумный диапазон
                        p.collected_pct = float(val)
                        break

    # Статус проекта
    # Ключевой критерий: ищем признаки завершённости
    page_text = soup.get_text(" ", strip=True).lower()

    # Признаки активного проекта — пропускаем
    if any(w in page_text for w in ("идёт сбор", "осталось дней", "поддержать проект", "добавить в корзину")):
        # Возможно активный на момент снапшота — проверяем дату
        # Если снапшот старый (> 1 года назад) — скорее всего уже завершён
        snap_year = int(timestamp[:4])
        import datetime
        if snap_year >= datetime.date.today().year - 1:
            # Слишком свежий — пропускаем
            return None
        # Старый снапшот активного проекта — статус неизвестен, пропускаем
        p.status = "unknown"
    elif any(w in page_text for w in ("сбор завершён", "кампания завершена", "проект завершён")):
        p.status = "finished"
    else:
        p.status = "unknown"

    # dt/dd мета-поля
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
        elif "регион" in label:
            p.region = val
        elif "категория" in label:
            lnk = dd_el.find("a")
            if lnk:
                p.category = lnk.get_text(strip=True)

    # Автор
    author_el = soup.find("h2", class_=re.compile("authorName"))
    p.author_name = author_el.get_text(strip=True) if author_el else None

    meta_vals = soup.find_all("dd", class_=re.compile("authorMetaValue"))
    if len(meta_vals) >= 1:
        p.author_projects = _int(meta_vals[0].get_text())

    # Вознаграждения
    rewards = [r for r in soup.find_all("article", class_=re.compile("reward"))
               if r.get("id", "") != "reward-donate"]
    p.nmb_reward = len(rewards)
    prices = []
    for rw in rewards:
        price_el = rw.find("dd", class_=re.compile("priceValue"))
        if price_el:
            v = _money(price_el.get_text())
            if v is not None:
                prices.append(v)
    if prices:
        p.min_reward = min(prices)
        p.max_reward = max(prices)

    # Медиа и текст
    desc_block = soup.find("div", class_=re.compile(
        r"common-wrapper-module__wrapper|campaign-info-module__text"
    ))
    if desc_block:
        p.nmb_image = len(desc_block.find_all("img"))
        p.nmb_video = len(desc_block.find_all("iframe", src=re.compile(r"youtube|vimeo|rutube", re.I)))
        p.nmb_word  = _words(desc_block.get_text(" ", strip=True))

    return p


# ─── ШАГ 3: Проверяем подходит ли проект ─────────────────────────────────
def is_failed_project(p: Project) -> bool:
    """
    Возвращает True если проект завершился неуспешно (pct < 50).
    """
    if p.collected_pct is None:
        return False
    if p.collected_pct >= 50:
        return False   # успешный — не нужен
    # Дополнительная проверка: если нет финансовых данных совсем — пропускаем
    if p.goal_sum is None and p.collected_sum is None:
        return False
    return True


# ─── Основной сборщик ─────────────────────────────────────────────────────
def run():
    import csv as _csv

    output_path = Path(OUTPUT_FILE)
    seen_urls: set[str] = set()

    if not output_path.exists():
        pd.DataFrame(columns=list(Project.__dataclass_fields__.keys())).to_csv(
            output_path, index=False, encoding="utf-8-sig", quoting=_csv.QUOTE_ALL
        )
        log.info("Создан файл: %s", output_path.absolute())
    else:
        existing = pd.read_csv(output_path, engine="python", on_bad_lines="warn")
        seen_urls = set(existing["url"].dropna().tolist())
        log.info("Возобновление: уже %d проектов.", len(seen_urls))

    saved_count = 0
    buffer: list[dict] = []

    for year in YEARS:
        if MAX_PROJECTS and saved_count >= MAX_PROJECTS:
            log.info("Лимит %d достигнут.", MAX_PROJECTS)
            break

        cdx_results = get_cdx_urls(year)
        random.shuffle(cdx_results)  # перемешиваем чтобы не парсить подряд

        for original_url, timestamp in cdx_results:
            if MAX_PROJECTS and saved_count >= MAX_PROJECTS:
                break

            # Нормализуем URL — ключ дедупликации не зависит от протокола
            clean_url = re.sub(r"^https?://", "", original_url).rstrip("/")
            clean_url = re.sub(r":80(/|$)", r"\1", clean_url)
            if clean_url in seen_urls:
                continue

            log.info("Проверяем: %s [%s]", original_url, timestamp[:8])
            project = parse_wayback_page(original_url, timestamp)

            if project is None:
                seen_urls.add(clean_url)
                time.sleep(random.uniform(0.5, 1.5))
                continue

            if not is_failed_project(project):
                log.info("  SKIP: '%s' | pct=%s%%",
                         project.title, project.collected_pct)
                seen_urls.add(clean_url)
                time.sleep(random.uniform(0.5, 1.5))
                continue

            # Нашли неуспешный!
            saved_count += 1
            left = f"{MAX_PROJECTS - saved_count} до лимита" if MAX_PROJECTS else "∞"
            log.info("  FOUND [%d, %s]: '%s' | pct=%.0f%% | %s",
                     saved_count, left,
                     project.title, project.collected_pct,
                     timestamp[:8])

            row = asdict(project)
            row["target"] = 0
            buffer.append(row)
            seen_urls.add(clean_url)

            if len(buffer) >= 10:
                _flush(buffer, output_path)
                buffer.clear()

            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    if buffer:
        _flush(buffer, output_path)

    df = pd.read_csv(output_path, engine="python", on_bad_lines="warn")
    log.info("ИТОГО неуспешных проектов: %d → %s", len(df), output_path.absolute())
    print(f"\nНайдено неуспешных проектов: {len(df)}")
    print(f"Файл: {output_path.absolute()}")
    if not df.empty:
        print(df[["title", "collected_pct", "snapshot_date"]].head(10).to_string())


def _flush(rows: list[dict], path: Path):
    import csv as _csv
    pd.DataFrame(rows).to_csv(
        path, mode="a", header=False, index=False,
        encoding="utf-8-sig", quoting=_csv.QUOTE_ALL
    )


if __name__ == "__main__":
    run()