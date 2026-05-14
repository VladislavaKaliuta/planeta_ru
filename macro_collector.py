"""
macro_collector.py — Сборщик макроэкономических показателей России
для ML-исследования успешности краудфандинговых проектов (planeta.ru)

Теоретическое обоснование включённых показателей:
  ┌─────────────────────────────────────────────────────────────────┐
  │ Монетарные (ЦБ РФ):                                            │
  │   cbr_key_rate      — ключевая ставка, %                       │
  │                        ↑ ставка → ↓ риск-аппетит → ↓ бэкинг   │
  │   usd_rub           — курс USD/RUB, среднемесячный             │
  │   usd_rub_mom_pct   — изменение курса м/м, %                   │
  │   usd_rub_yoy_pct   — изменение курса г/г, % (волатильность)   │
  │                                                                 │
  │ Фондовый рынок (MOEX ISS):                                     │
  │   moex_imoex        — Индекс МосБиржи, закр. посл. дня мес.   │
  │   moex_imoex_mom_pct — изменение м/м, %                        │
  │   moex_imoex_yoy_pct — изменение г/г, % (wealth effect)       │
  │                                                                 │
  │ Реальный сектор (Росстат, ручная загрузка):                    │
  │   cpi_mom           — ИПЦ, % к предыдущему месяцу              │
  │                        ↑ инфляция → ↓ реальные доходы → ↓ бэк  │
  │   cpi_yoy           — ИПЦ, % г/г (накопленная инфляция)       │
  │   unemployment_rate — безработица МОТ, %                       │
  │   real_income_yoy   — реальные доходы, % г/г                   │
  │                                                                 │
  │ Флаги кризисов (binary):                                       │
  │   crisis_2014       — девальвация рубля, первые санкции         │
  │   crisis_covid      — пандемия COVID-19 (локдауны)             │
  │   crisis_2022       — военный шок, жёсткие санкции             │
  └─────────────────────────────────────────────────────────────────┘

Автоматическая загрузка (запускается скриптом):
  ✓ Курс USD/RUB    — ЦБ РФ XML API
  ✓ Ключевая ставка — ЦБ РФ HTML (scraping таблицы)
  ✓ Индекс IMOEX    — MOEX ISS REST API

Ручная загрузка Росстат (инструкция в конце файла):
  → ИПЦ:             https://rosstat.gov.ru/price
  → Безработица:     https://rosstat.gov.ru/labour_market_employment_salaries
  → Реальные доходы: https://rosstat.gov.ru/folder/13397

Запуск:
    python macro_collector.py

Объединение с данными проектов:
    df_macro    = pd.read_csv("macro_indicators.csv")
    df_projects = pd.concat([
        pd.read_csv("planeta_class1.csv"),
        pd.read_csv("planeta_class0.csv"),
    ], ignore_index=True)
    df_merged = merge_macro(df_projects, df_macro)
    df_merged.to_csv("planeta_with_macro.csv", index=False, encoding="utf-8-sig")
"""

import re
import time
import logging
import warnings
from datetime import datetime
from pathlib import Path

import requests
import pandas as pd
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore", category=FutureWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("macro_collector.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# КОНФИГУРАЦИЯ
# ═══════════════════════════════════════════════════════════════════════════

START_YEAR  = 2012
END_YEAR    = datetime.today().year
OUTPUT_FILE = "macro_indicators.csv"
HEADERS     = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


# ═══════════════════════════════════════════════════════════════════════════
# УТИЛИТЫ
# ═══════════════════════════════════════════════════════════════════════════

def make_month_frame(start_year: int, end_year: int) -> pd.DataFrame:
    """Базовый датафрейм: все (год, месяц) в диапазоне."""
    rows, y, m = [], start_year, 1
    ey, em = end_year, datetime.today().month
    while (y, m) <= (ey, em):
        rows.append({"year": y, "month": m})
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return pd.DataFrame(rows)


def _get(url: str, retries: int = 3, **kwargs) -> requests.Response:
    """GET с retry при ошибке соединения."""
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30, **kwargs)
            r.raise_for_status()
            return r
        except requests.exceptions.ConnectionError as e:
            if attempt < retries - 1:
                log.debug("  retry %d/%d: %s", attempt + 1, retries, e)
                time.sleep(2 ** attempt)
            else:
                raise
    raise RuntimeError("Не удалось подключиться")


# ═══════════════════════════════════════════════════════════════════════════
# ЦБ РФ — КУРС USD/RUB
# ═══════════════════════════════════════════════════════════════════════════

def get_cbr_usd_rub() -> pd.DataFrame:
    """
    Среднемесячный курс USD/RUB из XML API ЦБ РФ.
    R01235 = Доллар США, ежедневные данные → агрегируем в среднее за месяц.
    Документация: https://www.cbr.ru/development/SXML/
    """
    log.info("CBR: загружаем курс USD/RUB...")
    url = (
        "https://www.cbr.ru/scripts/XML_dynamic.asp"
        f"?date_req1=01/01/{START_YEAR}&date_req2=31/12/{END_YEAR}"
        "&VAL_NM_RQ=R01235"
    )
    r = _get(url)
    r.encoding = "windows-1251"
    root = ET.fromstring(r.content)

    records = []
    for rec in root.findall("Record"):
        d   = datetime.strptime(rec.get("Date"), "%d.%m.%Y")
        val = float(rec.find("Value").text.replace(",", "."))
        records.append({"year": d.year, "month": d.month, "usd_rub": val})

    df     = pd.DataFrame(records)
    result = df.groupby(["year", "month"])["usd_rub"].mean().reset_index()
    log.info("  → USD/RUB: %d месяцев", len(result))
    return result


# ═══════════════════════════════════════════════════════════════════════════
# ЦБ РФ — КЛЮЧЕВАЯ СТАВКА (HTML scraping)
# ═══════════════════════════════════════════════════════════════════════════

def get_cbr_key_rate() -> pd.DataFrame:
    """
    История ключевой ставки ЦБ РФ — парсим таблицу с сайта ЦБ.
    Endpoint: https://www.cbr.ru/hd_base/KeyRate/
    Ставка устанавливается нерегулярно → берём значение на конец месяца,
    затем forward-fill для месяцев без изменений.
    """
    log.info("CBR: загружаем ключевую ставку (HTML)...")
    url = "https://www.cbr.ru/hd_base/KeyRate/"
    params = {
        "UniDbQuery.Posted": "True",
        "UniDbQuery.From": f"01.01.{START_YEAR}",
        "UniDbQuery.To":   f"31.12.{END_YEAR}",
    }
    r = _get(url, params=params)
    r.encoding = "utf-8"

    soup  = BeautifulSoup(r.content, "lxml")
    table = soup.find("table", class_=re.compile(r"data"))

    if table is None:
        # Запасной поиск: первая таблица с числовыми ячейками
        tables = soup.find_all("table")
        table  = next((t for t in tables if t.find("td")), None)

    if table is None:
        log.warning("  ! Таблица ключевой ставки не найдена на странице ЦБ")
        return pd.DataFrame(columns=["year", "month", "cbr_key_rate"])

    records = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        date_txt = cells[0].get_text(strip=True)
        rate_txt = cells[1].get_text(strip=True).replace(",", ".").replace(" ", "")
        try:
            d    = datetime.strptime(date_txt, "%d.%m.%Y")
            rate = float(rate_txt)
            records.append({"year": d.year, "month": d.month, "date": d, "cbr_key_rate": rate})
        except ValueError:
            continue

    if not records:
        log.warning("  ! Строки ключевой ставки не распознаны")
        return pd.DataFrame(columns=["year", "month", "cbr_key_rate"])

    df     = pd.DataFrame(records).sort_values("date")
    result = df.groupby(["year", "month"])["cbr_key_rate"].last().reset_index()
    log.info("  → ключевая ставка: %d записей", len(result))
    return result


# ═══════════════════════════════════════════════════════════════════════════
# MOEX — ИНДЕКС МОСБИРЖИ (IMOEX)
# ═══════════════════════════════════════════════════════════════════════════

def get_moex_imoex() -> pd.DataFrame:
    """
    Индекс МосБиржи (IMOEX) из MOEX ISS REST API.
    Берём закрытие последнего торгового дня каждого месяца.
    Документация: https://iss.moex.com/
    """
    log.info("MOEX: загружаем IMOEX...")
    base_url = (
        "https://iss.moex.com/iss/history/engines/stock/markets/index"
        "/boards/SNDX/securities/IMOEX.json"
    )

    records, cursor, page_size = [], 0, 100

    while True:
        url  = (
            f"{base_url}?from={START_YEAR}-01-01"
            f"&till={END_YEAR}-12-31&start={cursor}"
        )
        try:
            data = _get(url, retries=5).json()
        except Exception as e:
            log.warning("  ! MOEX страница cursor=%d: %s", cursor, e)
            break

        history = data["history"]
        cols    = history["columns"]
        rows    = history["data"]

        if not rows:
            break

        ci_close = cols.index("CLOSE")
        ci_date  = cols.index("TRADEDATE")

        for row in rows:
            d     = datetime.strptime(row[ci_date], "%Y-%m-%d")
            close = row[ci_close]
            if close is not None:
                records.append({
                    "year": d.year, "month": d.month, "date": d,
                    "moex_imoex": float(close)
                })

        cursor += page_size
        time.sleep(0.3)

        if len(rows) < page_size:
            break

    if not records:
        log.warning("  ! IMOEX не получен")
        return pd.DataFrame(columns=["year", "month", "moex_imoex"])

    df     = pd.DataFrame(records).sort_values("date")
    result = df.groupby(["year", "month"])["moex_imoex"].last().reset_index()
    log.info("  → IMOEX: %d месяцев", len(result))
    return result


# ═══════════════════════════════════════════════════════════════════════════
# РОССТАТ — РУЧНАЯ ЗАГРУЗКА
# ═══════════════════════════════════════════════════════════════════════════

def load_rosstat_cpi(filepath: str) -> pd.DataFrame:
    """
    Загружает данные ИПЦ Росстата из скачанного Excel-файла.

    КАК СКАЧАТЬ:
    1. Перейдите на https://rosstat.gov.ru/price
    2. Найдите таблицу "Индексы потребительских цен" → скачайте Excel
       (обычно файл вида: i_ipc.xlsx или ipc_YYYY.xlsx)
    3. Укажите путь к файлу в аргументе filepath

    Формат файла Росстата: строки = годы, столбцы = месяцы (Янв..Дек)
    Значения: % к предыдущему месяцу (напр., 100.5 означает рост 0.5%)
    """
    return _load_rosstat_wide(filepath, "cpi_mom")


def load_rosstat_unemployment(filepath: str) -> pd.DataFrame:
    """
    Загружает уровень безработицы из скачанного файла Росстата.

    КАК СКАЧАТЬ:
    1. Перейдите на https://rosstat.gov.ru/labour_market_employment_salaries
    2. Раздел "Уровень безработицы" → Excel
       (файл вида: ч_р.xlsx, unempl.xlsx или аналогичный)
    3. Значения: % от рабочей силы (методология МОТ)
    """
    return _load_rosstat_wide(filepath, "unemployment_rate")


def load_rosstat_real_income(filepath: str) -> pd.DataFrame:
    """
    Загружает индекс реальных располагаемых доходов из файла Росстата.

    КАК СКАЧАТЬ:
    1. Перейдите на https://rosstat.gov.ru/folder/13397
    2. Таблица "Реальные располагаемые денежные доходы" → Excel
       Значения: % к соответствующему периоду предыдущего года
    """
    return _load_rosstat_wide(filepath, "real_income_yoy")


def _load_rosstat_wide(filepath: str, col_name: str) -> pd.DataFrame:
    """
    Универсальный загрузчик таблиц Росстата в широком формате:
    строки = годы (или периоды), столбцы = месяцы.

    Поддерживает Excel и CSV. Пропускает нечисловые строки.
    """
    fp = Path(filepath)
    if not fp.exists():
        log.error("Файл не найден: %s", filepath)
        return pd.DataFrame(columns=["year", "month", col_name])

    log.info("Загружаем %s из %s...", col_name, filepath)

    df_raw = (
        pd.read_excel(fp, header=None)
        if fp.suffix in (".xlsx", ".xls")
        else pd.read_csv(fp, header=None)
    )

    MONTH_RU = {
        "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "маи": 5,
        "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }

    # Ищем строку-заголовок с названиями месяцев
    header_row = None
    for i, row in df_raw.iterrows():
        vals = [str(v).lower()[:3] for v in row.values if pd.notna(v)]
        matches = sum(1 for v in vals if v in MONTH_RU)
        if matches >= 6:
            header_row = i
            break

    if header_row is None:
        log.warning(
            "  Не найдена строка с названиями месяцев в %s. "
            "Попытка интерпретировать как длинный формат (date, value).", filepath
        )
        return _load_rosstat_long(df_raw, col_name)

    # Определяем соответствие столбцов → номера месяцев
    month_col_map = {}
    for col_idx, cell in enumerate(df_raw.iloc[header_row]):
        key = str(cell).lower()[:3]
        if key in MONTH_RU:
            month_col_map[col_idx] = MONTH_RU[key]

    # Парсим строки с данными (начиная с header_row+1)
    records = []
    for i in range(header_row + 1, len(df_raw)):
        row = df_raw.iloc[i]
        # Ищем год в первых ячейках строки
        year = None
        for cell in row.values[:3]:
            try:
                y = int(float(str(cell)))
                if 2000 <= y <= 2030:
                    year = y
                    break
            except (ValueError, TypeError):
                continue
        if year is None:
            continue
        for col_idx, month in month_col_map.items():
            try:
                val = float(str(row.iloc[col_idx]).replace(",", ".").replace(" ", ""))
                if 50 < val < 150 or col_name != "cpi_mom":  # разумный диапазон
                    records.append({"year": year, "month": month, col_name: val})
            except (ValueError, TypeError):
                continue

    if not records:
        log.warning("  Не удалось извлечь данные из %s", filepath)
        return pd.DataFrame(columns=["year", "month", col_name])

    result = (
        pd.DataFrame(records)
        .groupby(["year", "month"])[col_name]
        .mean()
        .reset_index()
    )
    log.info("  → %s: %d записей из %s", col_name, len(result), filepath)
    return result


def _load_rosstat_long(df_raw: pd.DataFrame, col_name: str) -> pd.DataFrame:
    """Fallback: длинный формат (дата в одном столбце, значение в другом)."""
    records = []
    for _, row in df_raw.iterrows():
        vals = [v for v in row.values if pd.notna(v)]
        if len(vals) < 2:
            continue
        for fmt in ("%d.%m.%Y", "%m.%Y", "%Y-%m-%d", "%Y-%m"):
            try:
                d = datetime.strptime(str(vals[0]).strip(), fmt)
                v = float(str(vals[1]).replace(",", "."))
                records.append({"year": d.year, "month": d.month, col_name: v})
                break
            except (ValueError, TypeError):
                continue
    return pd.DataFrame(records) if records else pd.DataFrame(columns=["year", "month", col_name])


# ═══════════════════════════════════════════════════════════════════════════
# ФЛАГИ КРИЗИСОВ И ПРОИЗВОДНЫЕ ПРИЗНАКИ
# ═══════════════════════════════════════════════════════════════════════════

def add_crisis_flags(df: pd.DataFrame) -> pd.DataFrame:
    """
    Бинарные флаги экономических шоков.

    Обоснование для диссертации:
    - crisis_2014: девальвация рубля ~50%, первые санкции, ставка до 17%.
                   Сильнейший спад потребительских расходов 2015 года.
    - crisis_covid: локдауны (март–май 2020), падение ВВП на 3%.
                    Неоднозначный эффект: рост digital-проектов при падении
                    событийных и физических.
    - crisis_2022: военный шок (фев 2022), беспрецедентные санкции,
                   уход 1000+ брендов, мобилизация (сент 2022),
                   рубль к 120 USD/руб в начале марта.
    """
    df = df.copy()

    df["crisis_2014"] = (
        ((df["year"] == 2014) & (df["month"] >= 11)) |
        (df["year"] == 2015)
    ).astype(int)

    df["crisis_covid"] = (
        (df["year"] == 2020) & (df["month"].between(3, 9))
    ).astype(int)

    df["crisis_2022"] = (
        ((df["year"] == 2022) & (df["month"] >= 2)) |
        (df["year"] >= 2023)
    ).astype(int)

    return df


def add_derived_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Производные временны́е признаки.

    Зачем для модели:
    - MoM (м/м): краткосрочная динамика — шок текущего месяца
    - YoY (г/г): долгосрочный тренд — структурная ситуация в экономике
    - cpi_yoy: накопленная инфляция лучше отражает реальное давление
               на доходы, чем ежемесячные точки
    """
    df = df.sort_values(["year", "month"]).reset_index(drop=True)

    if "usd_rub" in df.columns:
        df["usd_rub_mom_pct"] = df["usd_rub"].pct_change(1).mul(100).round(2)
        df["usd_rub_yoy_pct"] = df["usd_rub"].pct_change(12).mul(100).round(2)

    if "moex_imoex" in df.columns:
        df["moex_imoex_mom_pct"] = df["moex_imoex"].pct_change(1).mul(100).round(2)
        df["moex_imoex_yoy_pct"] = df["moex_imoex"].pct_change(12).mul(100).round(2)

    if "cpi_mom" in df.columns:
        # Точный г/г: произведение 12 месячных факторов
        cpi_f = df["cpi_mom"].apply(lambda x: (x / 100) if pd.notna(x) else None)
        df["cpi_yoy"] = (
            cpi_f
            .rolling(12, min_periods=12)
            .apply(lambda x: (x + 1).prod() * 100 - 100, raw=True)
            .round(2)
        )

    return df


# ═══════════════════════════════════════════════════════════════════════════
# ГЛАВНАЯ ФУНКЦИЯ
# ═══════════════════════════════════════════════════════════════════════════

def build_macro_table(
    rosstat_cpi_file:    str = None,
    rosstat_unemp_file:  str = None,
    rosstat_income_file: str = None,
) -> pd.DataFrame:
    """
    Собирает все макроэкономические показатели в единую ежемесячную таблицу.

    Аргументы (опциональные):
        rosstat_cpi_file    — путь к файлу ИПЦ Росстат (.xlsx)
        rosstat_unemp_file  — путь к файлу безработицы Росстат (.xlsx)
        rosstat_income_file — путь к файлу реальных доходов Росстат (.xlsx)

    Пример с данными Росстат:
        build_macro_table(
            rosstat_cpi_file    = "rosstat/ipc.xlsx",
            rosstat_unemp_file  = "rosstat/unemployment.xlsx",
            rosstat_income_file = "rosstat/income.xlsx",
        )
    """
    log.info("=" * 65)
    log.info("Сбор макроэкономических показателей России %d–%d", START_YEAR, END_YEAR)
    log.info("=" * 65)

    base = make_month_frame(START_YEAR, END_YEAR)

    # ── Автоматические источники ──────────────────────────────────────────
    auto_collectors = [
        (get_cbr_usd_rub,   "Курс USD/RUB"),
        (get_cbr_key_rate,  "Ключевая ставка"),
        (get_moex_imoex,    "IMOEX"),
    ]

    for fn, label in auto_collectors:
        try:
            df = fn()
            if not df.empty:
                base = base.merge(df, on=["year", "month"], how="left")
                log.info("  ✓ %s добавлен", label)
            else:
                log.warning("  ✗ %s: нет данных", label)
        except Exception as e:
            log.error("  ✗ %s: %s", label, e)
        time.sleep(0.5)

    # ── Ручные источники Росстат ──────────────────────────────────────────
    rosstat_sources = [
        (rosstat_cpi_file,    load_rosstat_cpi,          "ИПЦ"),
        (rosstat_unemp_file,  load_rosstat_unemployment,  "Безработица"),
        (rosstat_income_file, load_rosstat_real_income,   "Реальные доходы"),
    ]

    for filepath, loader, label in rosstat_sources:
        if filepath:
            try:
                df = loader(filepath)
                if not df.empty:
                    base = base.merge(df, on=["year", "month"], how="left")
                    log.info("  ✓ %s добавлен из %s", label, filepath)
            except Exception as e:
                log.error("  ✗ %s (%s): %s", label, filepath, e)
        else:
            log.info(
                "  — %s: файл не указан (см. инструкцию по ручной загрузке)", label
            )

    # ── Постобработка ─────────────────────────────────────────────────────

    # Forward-fill ключевой ставки (устанавливается нерегулярно)
    if "cbr_key_rate" in base.columns:
        base["cbr_key_rate"] = base["cbr_key_rate"].ffill()

    # Флаги структурных шоков
    base = add_crisis_flags(base)

    # Производные признаки
    base = add_derived_features(base)

    # Сохраняем
    base.to_csv(OUTPUT_FILE, index=False, encoding="utf-8-sig")

    log.info("=" * 65)
    log.info("Готово: %d строк × %d колонок → %s", len(base), len(base.columns), OUTPUT_FILE)
    log.info("Колонки: %s", list(base.columns))

    missing = base.isnull().mean().mul(100).round(1)
    missing = missing[missing > 0]
    if not missing.empty:
        log.info("Доля пропусков (%%): %s", missing.to_dict())

    return base


# ═══════════════════════════════════════════════════════════════════════════
# ОБЪЕДИНЕНИЕ С ДАННЫМИ ПРОЕКТОВ
# ═══════════════════════════════════════════════════════════════════════════

def merge_macro(df_projects: pd.DataFrame,
                df_macro: pd.DataFrame) -> pd.DataFrame:
    """
    Объединяет датасет проектов с макропоказателями по месяцу старта кампании.

    Аргументы:
        df_projects — датафрейм с колонкой 'date_start' (формат DD.MM.YYYY)
        df_macro    — датафрейм из build_macro_table() или из CSV

    Возвращает:
        df_projects с добавленными макропоказателями на момент старта

    Пример:
        df_macro = pd.read_csv("macro_indicators.csv")
        df = pd.concat([
            pd.read_csv("planeta_class1.csv"),
            pd.read_csv("planeta_class0.csv"),
        ], ignore_index=True)
        df_full = merge_macro(df, df_macro)
        df_full.to_csv("planeta_with_macro.csv", index=False, encoding="utf-8-sig")
    """
    df = df_projects.copy()

    def parse_ym(s):
        if pd.isna(s):
            return None, None
        for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                d = datetime.strptime(str(s).strip(), fmt)
                return d.year, d.month
            except ValueError:
                continue
        return None, None

    parsed       = df["date_start"].apply(parse_ym)
    df["_year"]  = parsed.apply(lambda x: x[0])
    df["_month"] = parsed.apply(lambda x: x[1])

    merged = df.merge(
        df_macro,
        left_on  = ["_year", "_month"],
        right_on = ["year", "month"],
        how      = "left"
    ).drop(columns=["_year", "_month", "year", "month"], errors="ignore")

    anchor = "usd_rub" if "usd_rub" in merged.columns else merged.columns[-1]
    n      = merged[anchor].notna().sum()
    log.info(
        "merge_macro: %d проектов, макроданные найдены для %d (%.1f%%)",
        len(merged), n, 100 * n / max(len(merged), 1)
    )
    return merged


# ═══════════════════════════════════════════════════════════════════════════
# ТОЧКА ВХОДА
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # ─── Укажите пути к файлам Росстат, если скачали ──────────────────────
    # Инструкция:
    #   1. ИПЦ: https://rosstat.gov.ru/price
    #      → Потребительские цены → "Индексы потребительских цен" → Excel
    #   2. Безработица: https://rosstat.gov.ru/labour_market_employment_salaries
    #      → Таблицы → "Численность безработных" / "Уровень безработицы" → Excel
    #   3. Реальные доходы: https://rosstat.gov.ru/folder/13397
    #      → "Реальные располагаемые доходы" → Excel
    ROSSTAT_CPI_FILE    = "ipc_spr_03-2026.xlsx"   # например: "rosstat/ipc.xlsx"
    ROSSTAT_UNEMP_FILE  = "Trud_3_15-72.xlsx"   # например: "rosstat/unemployment.xlsx"
    ROSSTAT_INCOME_FILE = "urov_12kv_1kv-2026.xlsx"   # например: "rosstat/income.xlsx"
    # ──────────────────────────────────────────────────────────────────────

    df_macro = build_macro_table(
        rosstat_cpi_file    = ROSSTAT_CPI_FILE,
        rosstat_unemp_file  = ROSSTAT_UNEMP_FILE,
        rosstat_income_file = ROSSTAT_INCOME_FILE,
    )

    print("\n" + "=" * 65)
    print(f"Файл: {Path(OUTPUT_FILE).absolute()}")
    print(f"Строк: {len(df_macro)} | Колонок: {len(df_macro.columns)}")
    print("\nКолонки:\n  " + "\n  ".join(df_macro.columns.tolist()))
    print("\nПоследние 6 месяцев:")
    with pd.option_context("display.max_columns", None, "display.width", 200,
                           "display.float_format", "{:.2f}".format):
        print(df_macro.tail(6).to_string(index=False))
