"""
Масова синхронізація клієнтів Telegram-бота з Poster POS.

Запуск:  python3 sync_clients.py

Що робить:
  1. Читає всіх реальних Telegram-користувачів з SQLite (user_id >= 300_000_000)
  2. Для кожного: верифікує/створює/оновлює клієнта в Poster
  3. Перевіряє баланс; якщо < 50 грн → донараховує різницю (безпечно, 3 retry)
  4. Виводить підсумок та повну таблицю клієнтів
"""

import sqlite3
import time
from datetime import datetime

import poster_api

# ─── Константи ─────────────────────────────────────────────────────────────
MIN_BONUS     = 50             # мінімальний стартовий бонус у гривнях
REAL_USER_MIN = 300_000_000    # нижня межа справжнього Telegram user_id
DELAY         = 0.5            # пауза між API-запитами

# ─── Допоміжні функції ─────────────────────────────────────────────────────

def normalize_phone(phone):
    return poster_api._normalize_phone(phone)

def parse_birthday(birth_str):
    if not birth_str:
        return None
    try:
        return datetime.strptime(birth_str.strip(), "%d.%m.%Y").strftime("%Y-%m-%d")
    except Exception:
        return None

def build_poster_index(clients):
    """Будує два індекси для O(1)-пошуку: за client_id та за нормалізованим телефоном."""
    by_id    = {}
    by_phone = {}
    by_ext   = {}
    by_card  = {}
    for c in clients:
        cid = c.get("client_id") or c.get("id")
        if cid:
            by_id[str(cid)] = c
        ph = normalize_phone(c.get("phone") or "")
        if ph:
            by_phone[ph] = c
        ext = str(c.get("external_id") or "")
        if ext:
            by_ext[ext] = c
        card = str(c.get("card_number") or "")
        if card:
            by_card[card] = c
    return by_id, by_phone, by_ext, by_card

def find_in_index(by_id, by_phone, by_ext, by_card,
                  stored_id, user_id, phone_norm):
    """Пошук клієнта за 4 стратегіями, повертає (client_dict | None, strategy_str)."""
    # 1. Збережений poster_client_id → верифікуємо телефон
    if stored_id:
        c = by_id.get(str(stored_id))
        if c:
            c_phone = normalize_phone(c.get("phone") or "")
            if c_phone == phone_norm:
                return c, "stored_id+phone_match"
            # ID знайдено, але телефон різний → продовжуємо пошук за телефоном
    # 2. Телефон
    c = by_phone.get(phone_norm)
    if c:
        return c, "phone"
    # 3. external_id
    c = by_ext.get(str(user_id))
    if c:
        return c, "external_id"
    # 4. card_number
    c = by_card.get(str(user_id))
    if c:
        return c, "card_number"
    return None, None

def update_client_full(client_id, user_id, name, phone, birthday):
    """Оновлює поля клієнта в Poster: ім'я, телефон, card_number, external_id, birthday."""
    parts     = poster_api._clean_name(name or "Клієнт").split(" ", 1)
    firstname = parts[0] if parts else "Клієнт"
    lastname  = parts[1] if len(parts) > 1 else ""
    payload = {
        "client_id":   int(client_id),
        "firstname":   firstname,
        "lastname":    lastname,
        "phone":       phone,
        "card_number": str(user_id),
        "external_id": str(user_id),
    }
    if birthday:
        payload["birthday"] = birthday
    return poster_api._post("clients.updateClient", payload)

def get_loyalty_type(client_id):
    """Повертає loyalty_type клієнта: 1=бонуси, 2=знижка, None=невідомо."""
    data = poster_api.get_client(int(client_id))
    if isinstance(data, list):
        c = data[0] if data else None
    elif isinstance(data, dict):
        c = data
    else:
        c = None
    if not c:
        return None
    lt = c.get("loyalty_type")
    return int(lt) if lt is not None else None

def safe_topup(client_id, user_id, target=MIN_BONUS):
    """Донараховує бонуси до target якщо поточний баланс < target.
    Ніколи не зменшує баланс.
    Повертає (added: int, final_balance: float | None).
    """
    for attempt in range(3):
        current = poster_api.get_poster_balance(int(client_id))
        if current is None:
            print(f"    [bonus_retry] attempt={attempt+1} — не вдалося прочитати баланс")
            time.sleep(1)
            continue

        if current >= target:
            print(f"    [bonus_skipped] client_id={client_id} баланс={current:.0f} "
                  f">= {target} грн — пропускаємо")
            return 0, current

        to_add = round(target - current)
        print(f"    [bonus_checked] client_id={client_id} баланс={current:.0f} грн "
              f"→ донараховуємо +{to_add} грн до {target} грн")

        ok, status, _ = poster_api.add_bonus(int(client_id), to_add,
                                              comment="sync_topup_50")
        time.sleep(DELAY)

        if ok:
            final = poster_api.get_poster_balance(int(client_id))
            if final is not None and final >= target - 1:
                print(f"    [bonus_added] ✅ user_id={user_id} client_id={client_id} "
                      f"+{to_add} грн → баланс={final:.0f} грн")
                return to_add, final
            else:
                print(f"    [bonus_retry] attempt={attempt+1} верифікація: "
                      f"final={final} (очікувалось >= {target})")
                time.sleep(1)
        else:
            print(f"    [bonus_retry] attempt={attempt+1} add_bonus failed "
                  f"status={status}")
            time.sleep(1)

    print(f"    [poster_bonus_error] ❌ не вдалося донарахувати "
          f"client_id={client_id} після 3 спроб")
    return 0, None


# ─── Основна логіка ────────────────────────────────────────────────────────

def main():
    conn   = sqlite3.connect("users.db")
    cursor = conn.cursor()

    cursor.execute(
        "SELECT user_id, phone, name, birth, poster_client_id FROM users "
        "WHERE user_id >= ? ORDER BY user_id",
        (REAL_USER_MIN,)
    )
    users = cursor.fetchall()

    print(f"\n{'='*62}")
    print(f"  Синхронізація клієнтів: Telegram-бот ↔ Poster POS")
    print(f"  Реальних Telegram-користувачів: {len(users)}")
    print(f"  Мінімальний бонус: {MIN_BONUS} грн")
    print(f"{'='*62}\n")

    # Завантажуємо всіх Poster-клієнтів одним запитом
    print("[client_sync] Завантаження клієнтів з Poster...")
    all_clients = poster_api.get_clients()
    if not all_clients:
        print("❌ Не вдалося отримати клієнтів з Poster.")
        conn.close()
        return
    print(f"[client_sync] Poster повернув {len(all_clients)} клієнтів\n")

    by_id, by_phone, by_ext, by_card = build_poster_index(all_clients)

    cnt_updated = cnt_created = cnt_topup = cnt_skipped = cnt_error = 0
    report_rows = []

    for (user_id, phone, name, birth, stored_poster_id) in users:
        print(f"── user_id={user_id}  name={name!r}  phone={phone}  "
              f"stored_poster_id={stored_poster_id}")

        # ── 0. Немає телефону → пропускаємо ─────────────────────────────
        if not phone:
            print("  ⏭ Пропускаємо — немає телефону\n")
            cnt_skipped += 1
            report_rows.append((user_id, name, stored_poster_id, "—",
                                 "⏭ пропущено (немає телефону)"))
            continue

        phone_norm = normalize_phone(phone)
        birthday   = parse_birthday(birth)
        poster_id  = int(float(stored_poster_id)) if stored_poster_id else None

        # ── 1. Пошук клієнта в Poster ────────────────────────────────────
        found, strategy = find_in_index(
            by_id, by_phone, by_ext, by_card,
            poster_id, user_id, phone_norm
        )

        if found:
            poster_id = int(float(found.get("client_id") or found.get("id")))
            print(f"  [client_sync] знайдено через «{strategy}» "
                  f"→ poster_client_id={poster_id}")

            # Оновлюємо всі поля клієнта
            update_client_full(poster_id, user_id, name, phone, birthday)
            print(f"  [client_updated] poster_client_id={poster_id}")

            # Зберігаємо в SQLite якщо відрізняється
            if stored_poster_id != poster_id:
                cursor.execute(
                    "UPDATE users SET poster_client_id=? WHERE user_id=?",
                    (poster_id, user_id)
                )
                conn.commit()
                print(f"  [client_sync] SQLite оновлено: "
                      f"{stored_poster_id} → {poster_id}")

            cnt_updated += 1

        else:
            # Не знайдено → створюємо
            print(f"  [client_sync] не знайдено → створюємо в Poster...")
            result = poster_api.create_client(
                name or "Клієнт", phone,
                external_id=user_id, birthday=birthday
            )
            if result:
                if isinstance(result, (int, float)):
                    poster_id = int(result)
                elif isinstance(result, dict):
                    raw = result.get("client_id") or result.get("id")
                    poster_id = int(raw) if raw else None

            if poster_id:
                cursor.execute(
                    "UPDATE users SET poster_client_id=? WHERE user_id=?",
                    (poster_id, user_id)
                )
                conn.commit()
                print(f"  [client_created] ✅ poster_client_id={poster_id}")
                # Оновлюємо індекс для наступних ітерацій
                time.sleep(DELAY)
                fresh = poster_api.get_clients()
                if fresh:
                    all_clients = fresh
                    by_id, by_phone, by_ext, by_card = build_poster_index(fresh)
                cnt_created += 1
            else:
                print(f"  [poster_bonus_error] ❌ не вдалося створити клієнта\n")
                cnt_error += 1
                report_rows.append((user_id, name, "—", "—",
                                     "❌ помилка: не створено в Poster"))
                continue

        time.sleep(DELAY)

        # ── 2. Перевірка loyalty_type ────────────────────────────────────
        loyalty = get_loyalty_type(poster_id)
        if loyalty == 2:
            print(f"  [client_loyalty_warning] ⚠️  client_id={poster_id} має "
                  f"loyalty_type=2 (знижкова картка) — бонуси неможливі через API.\n"
                  f"  Виправлення: Poster → Клієнти → клієнт → тип карти → «Бонусна»")
            cnt_skipped += 1
            report_rows.append((user_id, name, poster_id, "—",
                                 "⚠️  знижкова картка (loyalty_type=2) — змінити вручну в Poster"))
            print()
            continue

        # ── 3. Перевірка та донарахування бонусів ───────────────────────
        added, final_bal = safe_topup(poster_id, user_id, target=MIN_BONUS)

        if added > 0:
            cnt_topup += 1
            status_str = f"🎁 +{added} грн → {final_bal:.0f} грн"
        elif final_bal is not None:
            status_str = f"✅ вже {final_bal:.0f} грн"
        else:
            status_str = "❌ баланс недоступний"
            cnt_error += 1

        report_rows.append((
            user_id, name, poster_id,
            f"{final_bal:.0f}" if final_bal is not None else "—",
            status_str
        ))
        print()

    conn.close()

    # ── Підсумок ─────────────────────────────────────────────────────────────
    print(f"\n{'='*62}")
    print(f"  ПІДСУМОК СИНХРОНІЗАЦІЇ")
    print(f"{'='*62}")
    print(f"  ✅ Оновлено у Poster:     {cnt_updated}")
    print(f"  ➕ Створено у Poster:     {cnt_created}")
    print(f"  🎁 Донараховано 50 грн:  {cnt_topup}")
    print(f"  ⏭ Пропущено:             {cnt_skipped}")
    print(f"  ❌ Помилок:               {cnt_error}")
    print(f"{'='*62}\n")

    # ── Таблиця клієнтів ─────────────────────────────────────────────────────
    hdr = f"  {'user_id':>12}  {'name':>24}  {'poster_id':>9}  {'баланс':>7}  статус"
    sep = "─" * 88
    print(sep)
    print(hdr)
    print(sep)
    for (uid, nm, pid, bal, st) in report_rows:
        print(f"  {str(uid):>12}  {str(nm or '?'):>24}  "
              f"{str(pid):>9}  {str(bal):>7}  {st}")
    print(sep)
    print()


if __name__ == "__main__":
    main()
