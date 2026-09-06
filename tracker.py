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


def telegram_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or token.startswith("123456"):
        raise RuntimeError("Заполните TELEGRAM_BOT_TOKEN в .env")
    return token


def telegram_chat_id() -> str:
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        raise RuntimeError("Заполните TELEGRAM_CHAT_ID в .env")
    return chat_id


def send_telegram(text: str, chat_id: str | None = None) -> None:
    try:
        token = telegram_token()
        chat_id = chat_id or telegram_chat_id()
    except RuntimeError as exc:
        log.info("%s — сообщение только в лог:\n%s", exc, text)
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


def snapshot_products() -> list[dict[str, Any]]:
    config = load_config()
    dest = int(config.get("dest") or -1257786)
    products = config.get("products") or []
    if not products:
        raise ValueError("В config.json пустой список products")

    state = load_state()
    log.info("Проверка %s товар(ов)", len(products))
    rows: list[dict[str, Any]] = []

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
            rows.append({"label": label, "error": str(exc)})
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
        rows.append(
            {
                "label": label,
                "url": info["url"],
                "price": new_price,
                "old_price": old_price,
                "record": record,
            }
        )
        state[key] = record

    save_state(state)
    return rows


def notify_price_changes(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        if row.get("error"):
            continue
        label = row["label"]
        new_price = float(row["price"])
        old_price = row["old_price"]
        safe_label = html.escape(label)
        stats = price_lines(new_price, row["record"])
        if old_price is None:
            log.info("[new] %s — %.2f ₽ (без уведомления)", label, new_price)
        elif new_price < old_price:
            saved = old_price - new_price
            log.info("[drop] %s — %.2f → %.2f", label, old_price, new_price)
            send_telegram(
                f"📉 Цена снизилась\n<b>{safe_label}</b>\n"
                f"{old_price:.2f} ₽ → <b>{new_price:.2f} ₽</b>\n"
                f"Экономия: {saved:.2f} ₽\n{stats}\n{row['url']}"
            )
        elif new_price > old_price:
            log.info("[up] %s — %.2f → %.2f", label, old_price, new_price)
            send_telegram(
                f"📈 Цена выросла\n<b>{safe_label}</b>\n"
                f"{old_price:.2f} ₽ → <b>{new_price:.2f} ₽</b>\n{stats}\n{row['url']}"
            )
        else:
            log.info("[same] %s — %.2f ₽", label, new_price)


def format_prices_report(rows: list[dict[str, Any]]) -> str:
    parts = ["📌 Текущие цены"]
    for row in rows:
        label = html.escape(str(row["label"]))
        if row.get("error"):
            parts.append(f"\n<b>{label}</b>\nошибка: {html.escape(row['error'])}")
            continue
        parts.append(
            f"\n<b>{label}</b>\n{price_lines(float(row['price']), row['record'])}\n{row['url']}"
        )
    return "\n".join(parts)


def check_once() -> None:
    notify_price_changes(snapshot_products())


PRICE_COMMANDS = {"/prices", "/price", "/цены", "цены", "/start"}


def is_price_command(text: str) -> bool:
    raw = text.strip().lower()
    if not raw:
        return False
    cmd = raw.split()[0]
    cmd = cmd.split("@", 1)[0]
    return cmd in PRICE_COMMANDS or raw in PRICE_COMMANDS


def run_bot() -> None:
    token = telegram_token()
    allowed = telegram_chat_id()
    requests.post(
        f"https://api.telegram.org/bot{token}/deleteWebhook",
        data={"drop_pending_updates": False},
        timeout=30,
    )
    requests.post(
        f"https://api.telegram.org/bot{token}/setMyCommands",
        json={"commands": [{"command": "prices", "description": "Текущие цены"}]},
        timeout=30,
    )
    log.info("Бот слушает команды /prices и «цены»")
    offset = None
    minutes = int(load_config().get("check_interval_minutes") or 30)
    next_check = time.time()
    while True:
        if time.time() >= next_check:
            try:
                check_once()
            except Exception as exc:
                log.error("%s", exc)
            next_check = time.time() + minutes * 60
        params: dict[str, Any] = {"timeout": 25}
        if offset is not None:
            params["offset"] = offset
        try:
            response = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params=params,
                timeout=40,
            )
            response.raise_for_status()
            updates = response.json().get("result") or []
        except requests.RequestException as exc:
            log.warning("Telegram getUpdates: %s", exc)
            time.sleep(3)
            continue
        for update in updates:
            offset = int(update["update_id"]) + 1
            message = update.get("message") or update.get("edited_message") or {}
            chat = message.get("chat") or {}
            if str(chat.get("id", "")) != allowed:
                continue
            text = str(message.get("text") or "")
            if not is_price_command(text):
                send_telegram(
                    "Напишите /prices или «цены» — пришлю актуальные цены.",
                    chat_id=allowed,
                )
                continue
            try:
                rows = snapshot_products()
                send_telegram(format_prices_report(rows), chat_id=allowed)
            except Exception as exc:
                log.error("%s", exc)
                send_telegram(f"Не удалось получить цены: {html.escape(str(exc))}", chat_id=allowed)


def main() -> int:
    parser = argparse.ArgumentParser(description="Трекер цен Wildberries")
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Проверять по кругу, пока скрипт запущен",
    )
    parser.add_argument(
        "--bot",
        action="store_true",
        help="Слушать Telegram: /prices или «цены», плюс проверка по расписанию",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        if args.bot:
            run_bot()
        elif args.watch:
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
