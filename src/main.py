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

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


# ------------------ МОДЕЛИ ДАННЫХ ------------------
@dataclass
class Place:
    name: str
    rating: Optional[float]
    categories: List[str]


@dataclass
class ParserConfig:
    city_url: str = "https://zoon.ru/tomsk/restaurants/"
    total_pages: int = 34
    headless: bool = False
    user_agents: Tuple[str, ...] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/118.0.5993.90 Safari/537.36",
    )
    accept_language: str = "ru-RU,ru;q=0.9,en-US;q=0.8"
    out_dir: Path = field(default_factory=lambda: Path.cwd() / "zoon_out")
    csv_filename: str = "zoon_tomsk_restaurants.csv"
    log_filename: str = "zoon_loader_log.txt"
    wait_timeout: int = 25
    initial_sleep: Tuple[float, float] = (1.5, 3.0)
    page_sleep: Tuple[float, float] = (2.8, 5.2)
    scroll_sleep: Tuple[float, float] = (0.6, 1.2)
    scroll_reset_sleep: Tuple[float, float] = (0.5, 1.1)
    scroll_steps_range: Tuple[int, int] = (4, 7)
    protect_poll_attempts: int = 10
    protect_poll_delay: Tuple[float, float] = (2.2, 4.2)
    protect_manual_retry_attempts: int = 6
    protect_manual_retry_delay: Tuple[float, float] = (3.5, 5.5)
    window_width_range: Tuple[int, int] = (1280, 1680)
    window_height_range: Tuple[int, int] = (820, 1050)

    def __post_init__(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def page_url(self, page: int) -> str:
        return self.city_url if page == 1 else f"{self.city_url.rstrip('/')}/page-{page}/"

    def choose_user_agent(self) -> str:
        return random.choice(self.user_agents)

    def random_window_size(self) -> Tuple[int, int]:
        width = random.randint(*self.window_width_range)
        height = random.randint(*self.window_height_range)
        return width, height

    @property
    def csv_path(self) -> Path:
        return self.out_dir / self.csv_filename

    @property
    def log_path(self) -> Path:
        return self.out_dir / self.log_filename


# ------------------ УТИЛИТЫ ------------------
def humanized_sleep(bounds: Tuple[float, float]) -> None:
    """Случайная пауза в заданном диапазоне."""
    a, b = bounds
    time.sleep(random.uniform(a, b))


def df_from_places(places: Iterable[Place]) -> pd.DataFrame:
    """Конвертация списка мест в DataFrame для сохранения."""
    rows = [{
        "Название": p.name,
        "Рейтинг": p.rating if p.rating is not None else "",
        "Направления": " | ".join(p.categories) if p.categories else "",
    } for p in places]
    return pd.DataFrame(rows)


# ------------------ ОСНОВНАЯ ЛОГИКА ------------------
class ZoonScraper:
    PROTECT_TOKENS: Sequence[str] = (
        "мы проверяем, что вы не робот",
        "checking your browser",
        "you will be redirected",
        "cloudflare",
        "подождите несколько секунд",
    )
    CARD_SELECTORS: Sequence[str] = (
        "li.minicard-item.js-results-item",
        "div.minicard-item",
    )
    FEATURES_SELECTOR = ".minicard-item__features a, .service-items a, .tags a"
    TITLE_SELECTORS: Sequence[str] = (
        "a.title-link.js-item-url",
        ".minicard-item__title",
        "h2",
    )
    RATING_SELECTOR = ".minicard-item__rating, .rating, .stars"

    def __init__(self, config: ParserConfig) -> None:
        self.config = config
        self.places: List[Place] = []
        self.logs: List[str] = []
        self._driver: Optional[webdriver.Chrome] = None
        self._user_agent: str = self.config.choose_user_agent()

    # ---------- Публичный интерфейс ----------
    def run(self) -> Tuple[List[Place], List[str]]:
        driver = self._ensure_driver()
        try:
            for page in range(1, self.config.total_pages + 1):
                if not self._process_page(page, driver):
                    break
        finally:
            self.close()
        return self.places, self.logs

    def close(self) -> None:
        if self._driver is None:
            return
        try:
            self._driver.quit()
        except Exception:
            pass
        finally:
            self._driver = None

    # ---------- Внутренняя механика ----------
    def _ensure_driver(self) -> webdriver.Chrome:
        if self._driver is None:
            self._driver = self._build_driver()
        return self._driver

    def _build_driver(self) -> webdriver.Chrome:
        opts = Options()
        if self.config.headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--lang=ru-RU")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        width, height = self.config.random_window_size()
        opts.add_argument(f"--window-size={width},{height}")
        opts.add_argument(f"--user-agent={self._user_agent}")

        driver = webdriver.Chrome(options=opts)
        driver.execute_cdp_cmd(
            "Network.setUserAgentOverride",
            {
                "userAgent": self._user_agent,
                "acceptLanguage": self.config.accept_language,
                "platform": "Win32",
            },
        )
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": self._detector_evasion_script()},
        )
        return driver

    def _detector_evasion_script(self) -> str:
        # Скрываем признаки Selenium, встречающиеся в публичных антибот-чеках Cloudflare.
        return (
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});\n"
            "Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});\n"
            "Object.defineProperty(navigator, 'language', {get: () => 'ru-RU'});\n"
            "Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU','ru','en-US']});\n"
            "Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});\n"
            "Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});\n"
            "const getParameter = WebGLRenderingContext.prototype.getParameter;\n"
            "WebGLRenderingContext.prototype.getParameter = function(parameter) {\n"
            "  if (parameter === 37445) {return 'Intel Inc.';};\n"
            "  if (parameter === 37446) {return 'Intel Iris OpenGL Engine';};\n"
            "  return getParameter.call(this, parameter);\n"
            "};\n"
        )

    def _process_page(self, page: int, driver: webdriver.Chrome) -> bool:
        url = self.config.page_url(page)
        print(f"[ШАГ] Открываем страницу {page}: {url}")
        self.logs.append(f"Открыта страница {page}: {url}")

        driver.get(url)
        humanized_sleep(self.config.initial_sleep)

        if not self._handle_protect_screen(page, driver):
            return False

        if not self._wait_cards_container(driver, page):
            return False

        self._gentle_scroll(driver)

        cards = self._collect_cards(driver)
        if not cards:
            self.logs.append(f"Стр. {page}: карточек нет — стоп")
            print("  ↳ Карточек нет — стоп.")
            return False

        added = self._append_cards(cards)
        self.logs.append(
            f"Стр. {page}: карточек {len(cards)}, добавлено {added}, всего {len(self.places)}"
        )
        print(f"  ↳ Найдено {len(cards)}; добавлено {added}; всего {len(self.places)}")

        self._save_partial(page)
        humanized_sleep(self.config.page_sleep)
        return True

    def _handle_protect_screen(self, page: int, driver: webdriver.Chrome) -> bool:
        if not self._is_protect_screen(driver):
            return True

        self.logs.append(f"Стр. {page}: экран проверки — ждём редирект")
        print("  ↳ Обнаружена проверка. Ждём авто-редирект…")
        for _ in range(self.config.protect_poll_attempts):
            humanized_sleep(self.config.protect_poll_delay)
            if not self._is_protect_screen(driver):
                return True

        if self.config.headless:
            self.logs.append(f"Стр. {page}: проверка не пройдена — стоп")
            print("  ↳ Проверка не пройдена — стоп.")
            return False

        self.logs.append(f"Стр. {page}: ручное подтверждение пользователя")
        print("  ↳ Пройдите проверку в окне браузера и нажмите Enter в терминале…")
        try:
            input()
        except Exception:
            pass

        for _ in range(self.config.protect_manual_retry_attempts):
            humanized_sleep(self.config.protect_manual_retry_delay)
            if not self._is_protect_screen(driver):
                return True
            driver.refresh()

        self.logs.append(
            f"Стр. {page}: проверка не пройдена после ручного подтверждения — стоп"
        )
        print("  ↳ Проверка не пройдена — стоп.")
        return False

    def _is_protect_screen(self, driver: webdriver.Chrome) -> bool:
        html = driver.page_source.lower()
        return any(token in html for token in self.PROTECT_TOKENS)

    def _wait_cards_container(self, driver: webdriver.Chrome, page: int) -> bool:
        candidates = (
            (By.CSS_SELECTOR, "ul.js-results-group"),
            (By.CSS_SELECTOR, "div.catalog-list"),
            (By.CSS_SELECTOR, "div.results-container"),
        )
        for by, selector in candidates:
            try:
                WebDriverWait(driver, self.config.wait_timeout).until(
                    EC.presence_of_element_located((by, selector))
                )
                return True
            except TimeoutException:
                continue
        self.logs.append(f"Стр. {page}: контейнер не найден — стоп")
        print("  ↳ Контейнер не найден — стоп.")
        return False

    def _gentle_scroll(self, driver: webdriver.Chrome) -> None:
        height = driver.execute_script("return document.body.scrollHeight") or 1000
        steps = random.randint(*self.config.scroll_steps_range)
        for idx in range(steps):
            y = int(height * (idx + 1) / (steps + 1))
            driver.execute_script(f"window.scrollTo({{top:{y}, behavior:'smooth'}});")
            humanized_sleep(self.config.scroll_sleep)
        driver.execute_script("window.scrollTo({top:0, behavior:'smooth'});")
        humanized_sleep(self.config.scroll_reset_sleep)

    def _collect_cards(self, driver: webdriver.Chrome):
        for selector in self.CARD_SELECTORS:
            items = driver.find_elements(By.CSS_SELECTOR, selector)
            if items:
                return items
        return []

    def _append_cards(self, cards) -> int:
        before = len(self.places)
        for card in cards:
            place = self._parse_card(card)
            if place.name:
                self.places.append(place)
        return len(self.places) - before

    def _parse_card(self, card) -> Place:
        name = self._extract_name(card)
        rating = self._extract_rating(card)
        categories = self._extract_categories(card)
        return Place(name=name, rating=rating, categories=categories)

    def _extract_name(self, card) -> str:
        for selector in self.TITLE_SELECTORS:
            try:
                text = card.find_element(By.CSS_SELECTOR, selector).text.strip()
                if text:
                    return text
            except NoSuchElementException:
                continue
        return ""

    def _extract_rating(self, card) -> Optional[float]:
        try:
            text = card.find_element(By.CSS_SELECTOR, self.RATING_SELECTOR).text.strip()
        except NoSuchElementException:
            return None
        match = re.search(r"(\d+[.,]\d+|\d+)", text)
        if not match:
            return None
        return float(match.group(1).replace(",", "."))

    def _extract_categories(self, card) -> List[str]:
        try:
            links = card.find_elements(By.CSS_SELECTOR, self.FEATURES_SELECTOR)
        except NoSuchElementException:
            return []
        categories: List[str] = []
        seen = set()
        for link in links:
            text = link.text.strip(" ·—-\n\t")
            if text and text not in seen:
                seen.add(text)
                categories.append(text)
        return categories

    def _save_partial(self, page: int) -> None:
        suffix = f"{page:02d}"
        csv_path = self.config.out_dir / f"zoon_tomsk_restaurants_page_{suffix}.csv"
        log_path = self.config.out_dir / f"zoon_loader_log_page_{suffix}.txt"
        df_from_places(self.places).to_csv(csv_path, index=False, encoding="utf-8-sig")
        log_path.write_text("\n".join(self.logs), encoding="utf-8")
        print(
            "  [autosave] "
            f"CSV: {csv_path.name} | LOG: {log_path.name} | строк: {len(self.places)}"
        )


# ------------------ ТОЧКА ВХОДА ------------------
def main() -> None:
    print("[СТАРТ] Учебный парсер ZOON (Томск) — строгая пагинация 1..34")
    config = ParserConfig()
    scraper = ZoonScraper(config)
    places, history = scraper.run()

    df = df_from_places(places)
    df.to_csv(config.csv_path, index=False, encoding="utf-8-sig")
    config.log_path.write_text("\n".join(history), encoding="utf-8")

    print(f"\n[ГОТОВО] CSV: {config.csv_path} | строк: {len(df)}")
    print(f"[ГОТОВО] LOG: {config.log_path}")
    print("[ФИНИШ] Завершено.")


if __name__ == "__main__":
    main()
