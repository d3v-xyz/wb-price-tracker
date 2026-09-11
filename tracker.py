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


def save_config(config: dict[str, Any]) -> None:
    payload = {
        "dest": config.get("dest", -1257786),
        "check_interval_minutes": config.get("check_interval_minutes", 30),
        "products": list(config.get("products") or []),
    }
    CONFIG_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


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


def default_owner() -> str:
    return os.getenv("TELEGRAM_CHAT_ID", "").strip()


def item_owner(item: dict[str, Any]) -> str:
    return str(item.get("owner_chat_id") or default_owner()).strip()


def state_key(nm: int, owner: str) -> str:
    return f"{nm}:{owner}" if owner else str(nm)


def load_state() -> dict[str, dict[str, Any]]:
    if not STATE_PATH.exists():
        return {}
    raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    owner = default_owner()
    out: dict[str, dict[str, Any]] = {}
    for k, v in raw.items():
        rec = normalize_record(v)
        key = str(k)
        if ":" not in key and key.isdigit() and owner:
            key = state_key(int(key), owner)
        out[key] = rec
    return out


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


def snapshot_products(owner_chat_id: str | None = None) -> list[dict[str, Any]]:
    config = load_config()
    dest = int(config.get("dest") or -1257786)
    products = list(config.get("products") or [])
    if owner_chat_id:
        products = [item for item in products if item_owner(item) == str(owner_chat_id)]
    if not products:
        log.info("Список товаров пуст")
        return []

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
            rows.append({"label": label, "error": str(exc), "owner_chat_id": item_owner(item)})
            continue

        new_price = float(info["price"])
        owner = item_owner(item)
        key = state_key(nm, owner)
        old = state.get(key)
        if old is None and owner:
            old = state.get(str(nm))
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
                "owner_chat_id": owner,
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
        owner = str(row.get("owner_chat_id") or "").strip()
        if not owner:
            log.info("[skip notify] %s — нет владельца", label)
            continue
        if old_price is None:
            log.info("[new] %s — %.2f ₽ (без уведомления)", label, new_price)
        elif new_price < old_price:
            saved = old_price - new_price
            log.info("[drop] %s — %.2f → %.2f → %s", label, old_price, new_price, owner)
            send_telegram(
                f"📉 Цена снизилась\n<b>{safe_label}</b>\n"
                f"{old_price:.2f} ₽ → <b>{new_price:.2f} ₽</b>\n"
                f"Экономия: {saved:.2f} ₽\n{stats}\n{row['url']}",
                chat_id=owner,
            )
        elif new_price > old_price:
            log.info("[up] %s — %.2f → %.2f → %s", label, old_price, new_price, owner)
            send_telegram(
                f"📈 Цена выросла\n<b>{safe_label}</b>\n"
                f"{old_price:.2f} ₽ → <b>{new_price:.2f} ₽</b>\n{stats}\n{row['url']}",
                chat_id=owner,
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


HELP_TEXT = (
    "Команды:\n"
    "/prices — текущие цены\n"
    "/list — список отслеживания\n"
    "/add ссылка [название] — добавить товар WB\n"
    "/del номер|артикул|ссылка — удалить товар\n\n"
    "Можно просто прислать ссылку Wildberries — товар добавится."
)


def command_parts(text: str) -> tuple[str, str]:
    raw = text.strip()
    if not raw:
        return "", ""
    first, _, rest = raw.partition(" ")
    cmd = first.split("@", 1)[0].lower()
    return cmd, rest.strip()


def product_nm(item: dict[str, Any]) -> int | None:
    return parse_article(str(item.get("url") or item.get("nm") or ""))


def owned_products(products: list[Any], chat_id: str) -> list[tuple[int, Any]]:
    chat_id = str(chat_id)
    return [(i, item) for i, item in enumerate(products) if item_owner(item) == chat_id]


def find_owned_index(products: list[Any], query: str, chat_id: str) -> int | None:
    owned = owned_products(products, chat_id)
    query = query.strip()
    if not query or not owned:
        return None
    if query.isdigit():
        idx = int(query) - 1
        if 0 <= idx < len(owned):
            return owned[idx][0]
    nm = parse_article(query)
    if nm:
        for global_i, item in owned:
            if product_nm(item) == nm:
                return global_i
    q = query.lower()
    note_hits = [
        global_i
        for global_i, item in owned
        if str(item.get("note") or "").strip().lower() == q
    ]
    if len(note_hits) == 1:
        return note_hits[0]
    return None


def format_list(products: list[Any], chat_id: str) -> str:
    owned = owned_products(products, chat_id)
    if not owned:
        return "У вас пока нет товаров. Добавьте: /add ссылка"
    lines = ["📋 Ваши товары"]
    for i, (_g, item) in enumerate(owned, start=1):
        note = html.escape(str(item.get("note") or "").strip() or "без названия")
        url = html.escape(str(item.get("url") or ""))
        nm = product_nm(item)
        art = f" · {nm}" if nm else ""
        lines.append(f"\n{i}. <b>{note}</b>{art}\n{url}")
    return "\n".join(lines)


def add_product(raw: str, chat_id: str) -> str:
    if not raw:
        return "Так: /add https://www.wildberries.ru/catalog/123/detail.aspx название"
    first, _, rest = raw.partition(" ")
    nm = parse_article(first) or parse_article(raw)
    if is_placeholder(nm):
        return "Не вижу артикул Wildberries. Пришлите ссылку на карточку."
    note = rest.strip()
    url = PRODUCT_URL.format(nm=nm)
    config = load_config()
    products = list(config.get("products") or [])
    for item in products:
        if product_nm(item) == nm and item_owner(item) == str(chat_id):
            label = html.escape(str(item.get("note") or f"товар {nm}"))
            return f"Уже в вашем списке: <b>{label}</b>"
    dest = int(config.get("dest") or -1257786)
    info = fetch_product(nm, dest)
    if not note:
        brand = str(info.get("brand") or "").strip()
        name = str(info.get("name") or f"товар {nm}").strip()
        note = f"{brand} {name}".strip() if brand else name
    products.append({"url": url, "note": note, "owner_chat_id": str(chat_id)})
    config["products"] = products
    save_config(config)
    checked_at = now_iso()
    price = float(info["price"])
    state = load_state()
    key = state_key(nm, str(chat_id))
    state[key] = {"last": price, "min": price, "min_at": checked_at}
    save_state(state)
    record = state[key]
    return (
        f"➕ Добавил, уведомления придут вам\n<b>{html.escape(note)}</b>\n"
        f"{price_lines(price, record)}\n{url}"
    )


def delete_product(raw: str, chat_id: str) -> str:
    config = load_config()
    products = list(config.get("products") or [])
    owned = owned_products(products, chat_id)
    if not owned:
        return "У вас нет товаров для удаления."
    if not raw:
        return format_list(products, chat_id) + "\n\nУдалить: /del 1"
    idx = find_owned_index(products, raw, chat_id)
    if idx is None:
        return "Не нашёл ваш товар. Смотрите номера в /list."
    removed = products.pop(idx)
    config["products"] = products
    save_config(config)
    nm = product_nm(removed)
    if nm:
        state = load_state()
        state.pop(state_key(nm, str(chat_id)), None)
        save_state(state)
    label = html.escape(str(removed.get("note") or f"товар {nm}"))
    return f"🗑 Удалил <b>{label}</b>"


def handle_bot_text(text: str, chat_id: str) -> str:
    cmd, rest = command_parts(text)
    if cmd in {"/prices", "/price", "/цены"} or text.strip().lower() == "цены":
        rows = snapshot_products(chat_id)
        if not rows:
            return "У вас пока нет товаров. Добавьте: /add ссылка"
        return format_prices_report(rows)
    if cmd in {"/list", "/список"}:
        return format_list(load_config().get("products") or [], chat_id)
    if cmd in {"/add", "/добавить"}:
        return add_product(rest, chat_id)
    if cmd in {"/del", "/delete", "/remove", "/удалить"}:
        return delete_product(rest, chat_id)
    if cmd in {"/start", "/help", "/помощь"}:
        return HELP_TEXT
    if parse_article(text):
        return add_product(text, chat_id)
    return HELP_TEXT


def run_bot() -> None:
    token = telegram_token()
    requests.post(
        f"https://api.telegram.org/bot{token}/deleteWebhook",
        data={"drop_pending_updates": False},
        timeout=30,
    )
    requests.post(
        f"https://api.telegram.org/bot{token}/setMyCommands",
        json={
            "commands": [
                {"command": "prices", "description": "Текущие цены"},
                {"command": "list", "description": "Список товаров"},
                {"command": "add", "description": "Добавить товар WB"},
                {"command": "del", "description": "Удалить товар"},
                {"command": "help", "description": "Команды"},
            ]
        },
        timeout=30,
    )
    log.info("Бот слушает /prices, /add, /del, /list")
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
            chat_id = str((message.get("chat") or {}).get("id") or "")
            if not chat_id:
                continue
            text = str(message.get("text") or "")
            if not text.strip():
                continue
            try:
                send_telegram(handle_bot_text(text, chat_id), chat_id=chat_id)
            except Exception as exc:
                log.error("%s", exc)
                send_telegram(f"Ошибка: {html.escape(str(exc))}", chat_id=chat_id)


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
        help="Слушать Telegram: цены, добавление и удаление товаров",
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
