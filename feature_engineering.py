"""
Постобработка датасета Planeta.ru:
  - очистка данных
  - добавление SEO-метрик текста (водность, заспамленность)
  - расчёт макроэкономических признаков по дате кампании
  - экспорт финального датасета

Зависимости:
    pip install pandas numpy scipy pymorphy3 requests

Запуск:
    python feature_engineering.py
"""

import re
import logging
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger(__name__)

INPUT_FILE  = "planeta_projects.csv"
OUTPUT_FILE = "planeta_dataset_final.csv"


# ═══════════════════════════════════════════════════════════════
# 1. Загрузка и базовая очистка
# ═══════════════════════════════════════════════════════════════

def load_and_clean(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    log.info("Загружено %d строк, %d столбцов.", len(df), df.shape[1])

    # Удаляем дубликаты по URL
    df = df.drop_duplicates(subset="url")
    log.info("После удаления дублей: %d строк.", len(df))

    # Преобразуем числовые столбцы
    num_cols = [
        "goal_sum", "collected_sum", "collected_pct",
        "backers_count", "duration_days",
        "nmb_video", "nmb_image", "nmb_reward",
        "min_reward", "max_reward",
        "nmb_word", "nmb_sentence",
        "author_projects", "author_backed",
    ]
    for col in num_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Целевая переменная: успешность (1/0)
    # Planeta.ru: проект успешен при сборе >= 50% цели
    if "status" in df.columns:
        df["success"] = (df["status"] == "successful").astype(int)
    elif "collected_pct" in df.columns:
        df["success"] = (df["collected_pct"] >= 50).astype(int)

    # Удаляем строки без ключевых данных
    df = df.dropna(subset=["goal_sum", "success"])
    log.info("После удаления строк без цели/статуса: %d строк.", len(df))

    return df


# ═══════════════════════════════════════════════════════════════
# 2. SEO-признаки текста (без внешних сервисов)
# ═══════════════════════════════════════════════════════════════

# Стоп-слова русского языка (основной набор)
STOPWORDS_RU = {
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то",
    "все", "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за",
    "бы", "по", "только", "ее", "мне", "было", "вот", "от", "меня", "еще", "нет",
    "о", "из", "ему", "теперь", "когда", "даже", "ну", "вдруг", "ли", "если",
    "уже", "или", "ни", "быть", "был", "него", "до", "вас", "нибудь", "опять",
    "уж", "вам", "ведь", "там", "потом", "себя", "ничего", "ей", "может",
    "они", "тут", "где", "есть", "надо", "ней", "для", "мы", "тебя", "их",
    "чем", "была", "сам", "чтоб", "без", "будто", "чего", "раз", "тоже",
    "себе", "под", "будет", "ж", "тогда", "кто", "этот", "того", "потому",
    "этого", "какой", "совсем", "ним", "здесь", "этом", "один", "почти",
    "мой", "тем", "чтобы", "нее", "сейчас", "были", "куда", "зачем", "всех",
    "никогда", "можно", "при", "наконец", "два", "об", "другой", "хоть",
    "после", "над", "больше", "тот", "через", "эти", "нас", "про", "всего",
    "них", "какая", "много", "разве", "три", "эту", "моя", "впрочем", "хорошо",
    "свою", "этой", "перед", "иногда", "лучше", "чуть", "том", "нельзя",
    "такой", "им", "более", "всегда", "конечно", "всю", "между",
}


def compute_water_pct(text: str) -> float:
    """
    Водность = доля стоп-слов в тексте (%).
    Чем выше — тем меньше смысловой нагрузки.
    """
    if not text or not text.strip():
        return 0.0
    words = re.findall(r"\b[а-яёА-ЯЁa-zA-Z]+\b", text.lower())
    if not words:
        return 0.0
    stop_count = sum(1 for w in words if w in STOPWORDS_RU)
    return round(stop_count / len(words) * 100, 2)


def compute_spam_pct(text: str) -> float:
    """
    Заспамленность = доля наиболее часто повторяющегося слова (%).
    Аналог «академической тошноты».
    """
    if not text or not text.strip():
        return 0.0
    words = [
        w for w in re.findall(r"\b[а-яёА-ЯЁa-zA-Z]{3,}\b", text.lower())
        if w not in STOPWORDS_RU
    ]
    if not words:
        return 0.0
    most_common_count = Counter(words).most_common(1)[0][1]
    return round(most_common_count / len(words) * 100, 2)


def compute_sentiment(text: str) -> float:
    """
    Упрощённая тональность на основе словарей.
    Возвращает значение от -1 (негатив) до +1 (позитив).
    Для точного результата замените на ruSentiment или dostoevsky.
    """
    positive_words = {
        "успешный", "отличный", "замечательный", "уникальный", "лучший",
        "инновационный", "эффективный", "важный", "необходимый", "полезный",
        "интересный", "новый", "развитие", "помощь", "поддержка", "создание",
        "достижение", "прекрасный", "удобный", "доступный", "качественный",
        "профессиональный", "надёжный", "перспективный", "современный",
    }
    negative_words = {
        "проблема", "трудный", "сложный", "плохой", "невозможный", "опасный",
        "риск", "угроза", "недостаток", "отсутствие", "нехватка", "кризис",
        "затруднение", "препятствие", "ограничение", "неудача", "потеря",
    }
    if not text:
        return 0.0
    words = re.findall(r"\b[а-яёА-ЯЁ]{4,}\b", text.lower())
    if not words:
        return 0.0
    pos = sum(1 for w in words if w in positive_words)
    neg = sum(1 for w in words if w in negative_words)
    total = pos + neg
    return round((pos - neg) / total, 3) if total > 0 else 0.0


def count_errors(text: str) -> int:
    """
    Грубый подсчёт потенциальных ошибок:
    слова в CAPS_LOCK, двойные пробелы, лишние знаки препинания.
    Для полного орфографического анализа используйте pyspellchecker или Яндекс.Спеллер API.
    """
    if not text:
        return 0
    errors = 0
    errors += len(re.findall(r"\b[А-ЯЁ]{4,}\b", text))          # капслок слова
    errors += len(re.findall(r"  +", text))                        # двойные пробелы
    errors += len(re.findall(r"[.!?]{3,}", text))                  # многоточие из 4+
    errors += len(re.findall(r"[,;:]{2,}", text))                  # двойные запятые
    return errors


def add_text_features(df: pd.DataFrame) -> pd.DataFrame:
    """Добавляет SEO-признаки текста, если в датасете есть колонка description."""
    if "description" not in df.columns:
        log.warning("Колонка 'description' не найдена — пропускаем SEO-признаки.")
        log.warning("Добавьте парсинг полного текста описания в planeta_scraper.py")
        return df

    log.info("Вычисляем SEO-признаки текста...")
    df["water_pct"]   = df["description"].fillna("").apply(compute_water_pct)
    df["spam_pct"]    = df["description"].fillna("").apply(compute_spam_pct)
    df["text_tone"]   = df["description"].fillna("").apply(compute_sentiment)
    df["nmb_mistake"] = df["description"].fillna("").apply(count_errors)
    log.info("SEO-признаки добавлены.")
    return df


# ═══════════════════════════════════════════════════════════════
# 3. Макроэкономические признаки (ЦБ РФ + Росстат)
# ═══════════════════════════════════════════════════════════════

def fetch_cbr_key_rate() -> pd.DataFrame:
    """
    Загружает историю ключевой ставки ЦБ РФ.
    Источник: публичный XML ЦБ РФ.
    """
    url = "https://www.cbr.ru/scripts/XML_val.asp?d=0"
    # ЦБ предоставляет данные через XML API — ключевая ставка отдельным эндпоинтом
    # Используем заранее подготовленную таблицу (можно обновить вручную)
    # Данные ключевой ставки 2012-2024 (основные изменения)
    key_rate_data = [
        ("2013-09-13", 5.50), ("2014-03-03", 7.00), ("2014-04-25", 7.50),
        ("2014-07-28", 8.00), ("2014-11-05", 9.50), ("2014-12-12", 10.50),
        ("2014-12-16", 17.00), ("2015-02-02", 15.00), ("2015-03-16", 14.00),
        ("2015-05-05", 12.50), ("2015-06-16", 11.50), ("2015-07-31", 11.00),
        ("2015-10-30", 11.00), ("2016-06-14", 10.50), ("2016-09-19", 10.00),
        ("2017-03-27", 9.75), ("2017-05-02", 9.25), ("2017-06-19", 9.00),
        ("2017-09-18", 8.50), ("2017-10-30", 8.25), ("2017-12-18", 7.75),
        ("2018-02-12", 7.50), ("2018-03-26", 7.25), ("2018-09-14", 7.50),
        ("2018-12-17", 7.75), ("2019-06-17", 7.50), ("2019-07-29", 7.25),
        ("2019-09-09", 7.00), ("2019-10-28", 6.50), ("2019-12-16", 6.25),
        ("2020-02-10", 6.00), ("2020-04-27", 5.50), ("2020-06-22", 4.50),
        ("2020-07-27", 4.25), ("2021-03-22", 4.50), ("2021-04-26", 5.00),
        ("2021-06-11", 5.50), ("2021-07-23", 6.50), ("2021-09-10", 6.75),
        ("2021-10-22", 7.50), ("2021-12-17", 8.50), ("2022-02-28", 20.00),
        ("2022-04-11", 17.00), ("2022-05-04", 14.00), ("2022-05-26", 11.00),
        ("2022-06-10", 9.50), ("2022-07-25", 8.00), ("2022-09-16", 7.50),
        ("2023-07-21", 8.50), ("2023-08-15", 12.00), ("2023-09-15", 13.00),
        ("2023-10-27", 15.00), ("2023-12-15", 16.00), ("2024-07-26", 18.00),
        ("2024-09-13", 19.00), ("2024-10-25", 21.00),
    ]
    df_rate = pd.DataFrame(key_rate_data, columns=["date", "key_rate"])
    df_rate["date"] = pd.to_datetime(df_rate["date"])
    df_rate = df_rate.sort_values("date").reset_index(drop=True)
    return df_rate


def get_key_rate_on_date(date_str: str, df_rate: pd.DataFrame) -> Optional[float]:
    """Возвращает ключевую ставку ЦБ на указанную дату (последнее значение до даты)."""
    try:
        date = pd.to_datetime(date_str)
    except Exception:
        return None
    past_rates = df_rate[df_rate["date"] <= date]
    return float(past_rates.iloc[-1]["key_rate"]) if not past_rates.empty else None


def add_macro_features(df: pd.DataFrame) -> pd.DataFrame:
    """Добавляет макроэкономические признаки по дате начала кампании."""
    if "date_start" not in df.columns or df["date_start"].isna().all():
        log.warning("Нет колонки 'date_start' — пропускаем макро-признаки.")
        return df

    log.info("Загружаем историю ключевой ставки ЦБ...")
    df_rate = fetch_cbr_key_rate()

    log.info("Добавляем макро-признаки...")
    df["key_rate"] = df["date_start"].apply(
        lambda d: get_key_rate_on_date(d, df_rate)
    )

    # Год и квартал для анализа временных трендов
    df["campaign_year"]    = pd.to_datetime(df["date_start"], errors="coerce").dt.year
    df["campaign_quarter"] = pd.to_datetime(df["date_start"], errors="coerce").dt.quarter

    # Флаг кризисного периода (COVID + санкции 2022)
    df["crisis_period"] = df["campaign_year"].apply(
        lambda y: 1 if y in (2020, 2022) else 0
    )

    log.info("Макро-признаки добавлены: key_rate, campaign_year, campaign_quarter, crisis_period.")
    return df


# ═══════════════════════════════════════════════════════════════
# 4. Логарифмирование и удаление выбросов (как в дипломе)
# ═══════════════════════════════════════════════════════════════

LOG_COLS = ["goal_sum", "collected_sum", "nmb_word", "nmb_reward",
            "min_reward", "max_reward", "backers_count"]


def log_transform(df: pd.DataFrame) -> pd.DataFrame:
    """Добавляет логарифмированные версии числовых признаков."""
    for col in LOG_COLS:
        if col in df.columns:
            df[f"{col}_log"] = np.log1p(df[col].clip(lower=0))
    log.info("Логарифмирование выполнено для %d столбцов.", len(LOG_COLS))
    return df


def remove_outliers_zscore(df: pd.DataFrame, threshold: float = 3.0) -> pd.DataFrame:
    """Удаляет выбросы методом z-оценки (как в дипломе)."""
    from scipy import stats
    log_cols_in_df = [f"{c}_log" for c in LOG_COLS if f"{c}_log" in df.columns]
    initial_len = len(df)
    z_scores = np.abs(stats.zscore(df[log_cols_in_df].fillna(0)))
    mask = (z_scores < threshold).all(axis=1)
    df = df[mask]
    log.info(
        "Выбросы удалены: %d строк → %d строк (убрано %d).",
        initial_len, len(df), initial_len - len(df),
    )
    return df


# ═══════════════════════════════════════════════════════════════
# 5. Итоговый экспорт
# ═══════════════════════════════════════════════════════════════

def print_summary(df: pd.DataFrame) -> None:
    print("\n" + "═" * 55)
    print(f"  Финальный датасет: {len(df)} проектов")
    print("═" * 55)
    if "success" in df.columns:
        vc = df["success"].value_counts()
        print(f"  Успешных:    {vc.get(1, 0)} ({vc.get(1, 0)/len(df)*100:.1f}%)")
        print(f"  Неуспешных:  {vc.get(0, 0)} ({vc.get(0, 0)/len(df)*100:.1f}%)")
    if "category" in df.columns:
        print(f"\n  По категориям:")
        for cat, cnt in df["category"].value_counts().items():
            print(f"    {cat:<25} {cnt:>5}")
    if "key_rate" in df.columns:
        print(f"\n  Ключевая ставка: мин={df['key_rate'].min()}% макс={df['key_rate'].max()}%")
    print("═" * 55)
    print(f"\n  Признаки ({len(df.columns)}):")
    for col in sorted(df.columns):
        na_pct = df[col].isna().mean() * 100
        print(f"    {col:<30}  NA: {na_pct:.0f}%")


def main():
    if not Path(INPUT_FILE).exists():
        log.error("Файл %s не найден. Сначала запустите planeta_scraper.py", INPUT_FILE)
        return

    df = load_and_clean(INPUT_FILE)
    df = add_text_features(df)
    df = add_macro_features(df)
    df = log_transform(df)
    df = remove_outliers_zscore(df)

    df.to_csv(OUTPUT_FILE, index=False, encoding="utf-8-sig")
    log.info("Финальный датасет сохранён: %s", OUTPUT_FILE)
    print_summary(df)


if __name__ == "__main__":
    from typing import Optional   # нужен для type hints внутри функций
    main()