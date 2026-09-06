"""Проверка цен Wildberries и уведомление в Telegram при снижении."""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from curl_cffi import requests as wb_http
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "data" / "prices.json"
WB_URL = "https://card.wb.ru/cards/v4/detail"
PRODUCT_URL = "https://www.wildberries.ru/catalog/{nm}/detail.aspx"
ARTICLE_RE = re.compile(r"(?:catalog/)?(\d{6,})")

log = logging.getLogger("wb-tracker")


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Нет файла {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def parse_article(value: str) -> int | None:
    match = ARTICLE_RE.search(value.strip())
    if not match:
        return None
    return int(match.group(1))


def fetch_product(nm: int, dest: int) -> dict[str, Any]:
    # card.wb.ru отклоняет обычный Python TLS (403); нужен отпечаток браузера.
    response = wb_http.get(
        WB_URL,
        params={"appType": 1, "curr": "rub", "dest": dest, "spp": 30, "nm": nm},
        impersonate="chrome",
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") or payload
    products = data.get("products") or []
    if not products:
        raise LookupError(f"Товар {nm} не найден")

    product = products[0]
    price_kopecks = None
    for size in product.get("sizes") or []:
        price = (size.get("price") or {}).get("product")
        if price:
            price_kopecks = int(price)
            break
    if price_kopecks is None and product.get("salePriceU"):
        price_kopecks = int(product["salePriceU"])
    if price_kopecks is None:
        raise LookupError(f"Нет цены для артикула {nm}")

    return {
        "nm": nm,
        "name": product.get("name") or f"Артикул {nm}",
        "brand": product.get("brand") or "",
        "price": round(price_kopecks / 100, 2),
        "url": PRODUCT_URL.format(nm=nm),
    }


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="minutes")


def format_when(value: str | None) -> str:
    if not value:
        return "дата неизвестна"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    return dt.strftime("%d.%m.%Y %H:%M")


def normalize_record(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        last = float(value["last"])
        return {
            "last": last,
            "min": float(value.get("min", last)),
            "min_at": value.get("min_at"),
        }
    last = float(value)
    return {"last": last, "min": last, "min_at": None}


def load_state() -> dict[str, dict[str, Any]]:
    if not STATE_PATH.exists():
        return {}
    raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {str(k): normalize_record(v) for k, v in raw.items()}


def save_state(state: dict[str, dict[str, Any]]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def price_lines(current: float, record: dict[str, Any]) -> str:
    return (
        f"Сейчас: <b>{current:.2f} ₽</b>\n"
        f"Минимум: <b>{record['min']:.2f} ₽</b> ({html.escape(format_when(record.get('min_at')))})"
    )


def send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id or token.startswith("123456"):
        log.info("Telegram не настроен — сообщение только в лог:\n%s", text)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = requests.post(
        url,
        data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        },
        timeout=30,
    )
    if not response.ok:
        log.warning("Telegram: HTTP %s %s", response.status_code, response.text[:300])
        return


def is_placeholder(nm: int | None) -> bool:
    return nm is None or nm == 0


def check_once() -> None:
    config = load_config()
    dest = int(config.get("dest") or -1257786)
    products = config.get("products") or []
    if not products:
        raise ValueError("В config.json пустой список products")

    state = load_state()
    log.info("Проверка %s товар(ов)", len(products))

    for item in products:
        source = str(item.get("url") or item.get("nm") or "").strip()
        nm = parse_article(source)
        if is_placeholder(nm):
            log.warning("Пропуск заглушки: %s", source)
            continue

        label = str(item.get("note") or "").strip() or f"товар {nm}"
        try:
            info = fetch_product(nm, dest)
        except Exception as exc:
            log.warning("%s: %s", label, exc)
            continue

        new_price = float(info["price"])
        key = str(nm)
        old = state.get(key)
        checked_at = now_iso()
        if old is None:
            record = {"last": new_price, "min": new_price, "min_at": checked_at}
        else:
            record = dict(old)
            if new_price < float(record["min"]):
                record["min"] = new_price
                record["min_at"] = checked_at
            if not record.get("min_at"):
                record["min_at"] = checked_at
            record["last"] = new_price

        old_price = None if old is None else float(old["last"])
        safe_label = html.escape(label)
        stats = price_lines(new_price, record)

        if old_price is None:
            log.info("[new] %s — %.2f ₽ (без уведомления)", label, new_price)
        elif new_price < old_price:
            saved = old_price - new_price
            log.info("[drop] %s — %.2f → %.2f", label, old_price, new_price)
            send_telegram(
                f"📉 Цена снизилась\n<b>{safe_label}</b>\n"
                f"{old_price:.2f} ₽ → <b>{new_price:.2f} ₽</b>\n"
                f"Экономия: {saved:.2f} ₽\n{stats}\n{info['url']}"
            )
        elif new_price > old_price:
            log.info("[up] %s — %.2f → %.2f", label, old_price, new_price)
            send_telegram(
                f"📈 Цена выросла\n<b>{safe_label}</b>\n"
                f"{old_price:.2f} ₽ → <b>{new_price:.2f} ₽</b>\n{stats}\n{info['url']}"
            )
        else:
            log.info("[same] %s — %.2f ₽", label, new_price)

        state[key] = record

    save_state(state)


def main() -> int:
    parser = argparse.ArgumentParser(description="Трекер цен Wildberries")
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Проверять по кругу, пока скрипт запущен",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        if args.watch:
            minutes = int(load_config().get("check_interval_minutes") or 30)
            while True:
                check_once()
                log.info("Следующая проверка через %s мин.", minutes)
                time.sleep(minutes * 60)
        else:
            check_once()
    except KeyboardInterrupt:
        log.info("Остановлено")
        return 0
    except Exception as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
