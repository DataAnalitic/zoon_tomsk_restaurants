#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Учебный парсер каталогов ZOON (Томск → Рестораны) на чистом Selenium.

Алгоритм подгрузки (шаги Selenium для каждой страницы):
1) driver.get(URL страницы /page-N/).
2) Проверяем, не показывает ли сайт экран проверки («мы проверяем, что вы не робот»):
   2.1) Ждём авто-редирект несколько циклов.
   2.2) Если не помогло и окно видно (HEADLESS=False) — просим пройти проверку вручную,
        затем в терминале нажать Enter для продолжения.
3) Явно ждём появления контейнера списка карточек (WebDriverWait по «живучим» селекторам).
4) Делаем короткую «человеческую» прокрутку страницы (имитация поведения, дорисовка DOM).
5) Собираем карточки: название, рейтинг, направления → добавляем в результат.
6) Логируем: номер страницы, найдено карточек, всего записей.
7) Делаем «человеческую» паузу и переходим к следующей странице.
Автосохранение: после КАЖДОЙ страницы сохраняем частичный CSV/LOG.
Итог: сохраняем финальные CSV/LOG.
"""

import re
import time
import random
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# ------------------ НАСТРОЙКИ ------------------
CITY_URL     = "https://zoon.ru/tomsk/restaurants/"
TOTAL_PAGES  = 34                  # обходим страницы 1..34
HEADLESS     = False               # окно видно — можно вручную пройти проверку при необходимости

OUT_DIR      = Path.cwd() / "zoon_out"
OUT_DIR.mkdir(parents=True, exist_ok=True)
CSV_PATH     = OUT_DIR / "zoon_tomsk_restaurants.csv"
LOG_PATH     = OUT_DIR / "zoon_loader_log.txt"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ------------------ МОДЕЛЬ ------------------
@dataclass
class Place:
    name: str
    rating: Optional[float]
    categories: List[str]


# ------------------ ВСПОМОГАТЕЛЬНОЕ ------------------
def human_sleep(a=2.0, b=4.2) -> None:
    """Случайная пауза: имитируем «живого» пользователя и даём странице дорисоваться."""
    time.sleep(random.uniform(a, b))


def page_url(n: int) -> str:
    """URL страницы n (1 → базовая ссылка)."""
    return CITY_URL if n == 1 else f"{CITY_URL.rstrip('/')}/page-{n}/"


def build_driver() -> webdriver.Chrome:
    """Инициализация Chrome WebDriver."""
    opts = Options()
    if HEADLESS:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--start-maximized")
    opts.add_argument("--lang=ru-RU")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument(f"--user-agent={USER_AGENT}")

    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"}
    )
    return driver


def is_protect_screen(driver: webdriver.Chrome) -> bool:
    """Детект экрана проверки («вы не робот») по текстовым признакам в HTML."""
    html = driver.page_source.lower()
    keys = [
        "мы проверяем, что вы не робот",
        "checking your browser",
        "you will be redirected",
        "cloudflare",
        "подождите несколько секунд",
    ]
    return any(k in html for k in keys)


def wait_cards_container(driver: webdriver.Chrome, timeout: int = 25) -> None:
    """Явное ожидание контейнера списка карточек."""
    candidates = [
        (By.CSS_SELECTOR, "ul.js-results-group"),
        (By.CSS_SELECTOR, "div.catalog-list"),
        (By.CSS_SELECTOR, "div.results-container"),
    ]
    last_error = None
    for by, sel in candidates:
        try:
            WebDriverWait(driver, timeout).until(EC.presence_of_element_located((by, sel)))
            return
        except TimeoutException as e:
            last_error = e
    raise last_error or TimeoutException("Контейнер списка не найден.")


def gentle_scroll(driver: webdriver.Chrome) -> None:
    """Короткая «человеческая» прокрутка для дорисовки элементов."""
    h = driver.execute_script("return document.body.scrollHeight") or 1000
    steps = random.randint(3, 5)
    for i in range(steps):
        y = int(h * (i + 1) / (steps + 1))
        driver.execute_script(f"window.scrollTo({{top:{y}, behavior:'smooth'}});")
        time.sleep(random.uniform(0.5, 1.0))
    driver.execute_script("window.scrollTo({top:0, behavior:'smooth'});")
    time.sleep(random.uniform(0.4, 0.8))


def collect_cards(driver: webdriver.Chrome):
    """Возвращаем WebElement-ы карточек на текущей странице."""
    for sel in ("li.minicard-item.js-results-item", "div.minicard-item"):
        items = driver.find_elements(By.CSS_SELECTOR, sel)
        if items:
            return items
    return []


def parse_card(card) -> Place:
    """Извлекаем из карточки: название, рейтинг, направления."""
    try:
        name = card.find_element(By.CSS_SELECTOR, "a.title-link.js-item-url").text.strip()
    except NoSuchElementException:
        try:
            name = card.find_element(By.CSS_SELECTOR, ".minicard-item__title, h2").text.strip()
        except Exception:
            name = ""

    rating = None
    try:
        t = card.find_element(By.CSS_SELECTOR, ".minicard-item__rating, .rating, .stars").text.strip()
        m = re.search(r"(\d+[.,]\d+|\d+)", t)
        if m:
            rating = float(m.group(1).replace(",", "."))
    except NoSuchElementException:
        pass

    cats, seen = [], set()
    try:
        for a in card.find_elements(By.CSS_SELECTOR, ".minicard-item__features a, .service-items a, .tags a"):
            txt = a.text.strip(" ·—-\n\t")
            if txt and txt not in seen:
                seen.add(txt)
                cats.append(txt)
    except NoSuchElementException:
        pass

    return Place(name=name, rating=rating, categories=cats)


def df_from_places(places: List[Place]) -> pd.DataFrame:
    """Удобный конвертер в DataFrame."""
    rows = [{
        "Название": p.name,
        "Рейтинг": p.rating if p.rating is not None else "",
        "Направления": " | ".join(p.categories) if p.categories else ""
    } for p in places]
    return pd.DataFrame(rows)


def save_partial(places: List[Place], logs: List[str], suffix: str) -> None:
    """Автосохранение после каждой страницы."""
    csv_path = OUT_DIR / f"zoon_tomsk_restaurants_page_{suffix}.csv"
    log_path = OUT_DIR / f"zoon_loader_log_page_{suffix}.txt"
    df_from_places(places).to_csv(csv_path, index=False, encoding="utf-8-sig")
    Path(log_path).write_text("\n".join(logs), encoding="utf-8")
    print(f"  [autosave] CSV: {csv_path.name} | LOG: {log_path.name} | строк: {len(places)}")


# ------------------ ОСНОВНОЙ ЦИКЛ ------------------
def scrape_all_pages():
    driver = build_driver()
    places: List[Place] = []
    logs: List[str] = []

    try:
        for page in range(1, TOTAL_PAGES + 1):
            url = page_url(page)
            print(f"[ШАГ] Открываем страницу {page}: {url}")
            logs.append(f"Открыта страница {page}: {url}")
            driver.get(url)
            human_sleep(1.5, 3.0)

            if is_protect_screen(driver):
                logs.append(f"Стр. {page}: экран проверки — ждём редирект")
                print("  ↳ Обнаружена проверка. Ждём авто-редирект…")
                ok = False
                for _ in range(8):
                    time.sleep(2.5)
                    if not is_protect_screen(driver):
                        ok = True
                        break
                if not ok and not HEADLESS:
                    logs.append(f"Стр. {page}: ручное подтверждение пользователя")
                    print("  ↳ Пройдите проверку в окне браузера и нажмите Enter в терминале…")
                    try:
                        input()
                    except Exception:
                        pass

            try:
                wait_cards_container(driver, timeout=25)
            except TimeoutException:
                logs.append(f"Стр. {page}: контейнер не найден — стоп")
                print("  ↳ Контейнер не найден — стоп.")
                break

            gentle_scroll(driver)

            cards = collect_cards(driver)
            if not cards:
                logs.append(f"Стр. {page}: карточек нет — стоп")
                print("  ↳ Карточек нет — стоп.")
                break

            before = len(places)
            for c in cards:
                p = parse_card(c)
                if p.name:
                    places.append(p)
            added = len(places) - before

            logs.append(f"Стр. {page}: карточек {len(cards)}, добавлено {added}, всего {len(places)}")
            print(f"  ↳ Найдено {len(cards)}; добавлено {added}; всего {len(places)}")

            save_partial(places, logs, suffix=f"{page:02d}")

            human_sleep(2.4, 4.8)

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    return places, logs


# ------------------ ТОЧКА ВХОДА ------------------
if __name__ == "__main__":
    print("[СТАРТ] Учебный парсер ZOON (Томск) — строгая пагинация 1..34")
    data, history = scrape_all_pages()

    df = df_from_places(data)
    df.to_csv(CSV_PATH, index=False, encoding="utf-8-sig")
    Path(LOG_PATH).write_text("\n".join(history), encoding="utf-8")

    print(f"\n[ГОТОВО] CSV: {CSV_PATH} | строк: {len(df)}")
    print(f"[ГОТОВО] LOG: {LOG_PATH}")
    print("[ФИНИШ] Завершено.")
