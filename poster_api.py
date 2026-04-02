import requests
import time
from datetime import datetime

def _clean_name(name):
    """Видаляє emoji та спецсимволи, залишає тільки букви і пробіли."""
    if not name:
        return ""
    cleaned = "".join(c if (c.isalpha() or c == " " or c == "-") else " " for c in name)
    return " ".join(cleaned.split())

POSTER_TOKEN = "773062:5036007527734bc857f4813fd8e63706"
POSTER_ACCOUNT = "pivnii-na-raioni"
BASE_URL = "https://joinposter.com/api"
ACCOUNT_BASE_URL = f"https://{POSTER_ACCOUNT}.joinposter.com/api"

def _get(method, params=None):
    url = f"{BASE_URL}/{method}"
    p = {"token": POSTER_TOKEN}
    if params:
        p.update(params)
    for attempt in range(3):
        try:
            r = requests.get(url, params=p, timeout=10)
            r.raise_for_status()
            data = r.json()
            if "response" in data:
                return data["response"]
            print(f"[poster_api] Unexpected response for {method}: {data}")
            return None
        except Exception as e:
            print(f"[poster_api] GET {method} attempt {attempt+1} failed: {e}")
            time.sleep(1)
    return None

def _post(method, payload):
    # Poster API: account-specific URL, token в query params, решта в form-encoded body
    url = f"{ACCOUNT_BASE_URL}/{method}"
    params = {"token": POSTER_TOKEN}
    form = {k: v for k, v in payload.items() if v is not None}
    for attempt in range(3):
        try:
            r = requests.post(url, params=params, data=form, timeout=10)
            r.raise_for_status()
            resp = r.json()
            if "response" in resp:
                return resp["response"]
            print(f"[poster_api] Unexpected response for {method}: {resp}")
            return None
        except Exception as e:
            print(f"[poster_api] POST {method} attempt {attempt+1} failed: {e}")
            time.sleep(1)
    return None

def _normalize_phone(phone):
    digits = "".join(c for c in str(phone) if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits

def get_clients():
    return _get("clients.getClients") or []

def get_client(client_id):
    return _get("clients.getClient", {"client_id": client_id})

def get_client_by_external_id(external_id):
    clients = get_clients()
    for c in clients:
        if str(c.get("client_groups_id_client") or "") == str(external_id):
            return c
        if str(c.get("external_id") or "") == str(external_id):
            return c
    return None

def get_client_by_phone(phone):
    """Знайти клієнта в Poster за номером телефону."""
    _norm = _normalize_phone(phone)
    clients = get_clients()
    for c in clients:
        if _normalize_phone(c.get("phone") or "") == _norm:
            return c
        if _normalize_phone(c.get("phone_number") or "") == _norm:
            return c
    return None

def update_client_info(client_id, name=None, phone=None, birthday=None):
    """Оновити ім'я / телефон клієнта в Poster."""
    payload = {"client_id": int(client_id)}
    if name:
        parts = _clean_name(name).split(" ", 1)
        payload["firstname"] = parts[0] if parts else "Клієнт"
        payload["lastname"] = parts[1] if len(parts) > 1 else ""
        payload["client_name"] = f"{payload['firstname']} {payload['lastname']}".strip()
    if phone:
        payload["phone"] = phone
    if birthday:
        payload["birthday"] = birthday
    return _post("clients.updateClient", payload)

def create_client(name, phone, external_id=None, birthday=None):
    parts = _clean_name(name or "").split(" ", 1)
    firstname = parts[0] if parts else "Клієнт"
    if not firstname:
        firstname = "Клієнт"
    lastname = parts[1] if len(parts) > 1 else ""
    print(f"[create_client] firstname={repr(firstname)} lastname={repr(lastname)} phone={phone}")
    payload = {
        "firstname": firstname,
        "lastname": lastname,
        "client_name": f"{firstname} {lastname}".strip(),
        "phone": phone,
        "client_groups_id_client": 1
    }
    if external_id is not None:
        payload["external_id"] = str(external_id)
        payload["card_number"] = str(external_id)
    if birthday:
        payload["birthday"] = birthday
    return _post("clients.createClient", payload)

def update_client(client_id, card_number, birthday=None):
    payload = {
        "client_id": int(client_id),
        "card_number": str(card_number)
    }
    if birthday:
        payload["birthday"] = birthday
    return _post("clients.updateClient", payload)

def get_transactions(date_from=None, date_to=None):
    if date_from is None:
        date_from = datetime.now().strftime("%Y%m%d")
    if date_to is None:
        date_to = date_from

    raw = _get("transactions.getTransactions", {
        "date_from": date_from,
        "date_to": date_to
    })

    if raw is None:
        raw = _get("dash.getTransactions", {"dateFrom": date_from, "dateTo": date_to})

    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        result = []
        for key, val in raw.items():
            if isinstance(val, list):
                result.extend(val)
            elif isinstance(val, dict):
                for v in val.values():
                    if isinstance(v, list):
                        result.extend(v)
        return result
    return []

def get_last_order(poster_client_id):
    """Повертає останній чек клієнта з Poster POS за сьогодні/вчора.
    Результат: {"total": float, "bonus_used": float} або None.
    """
    try:
        from datetime import datetime as _dt, timedelta as _td
        today   = _dt.now().strftime("%Y%m%d")
        yday    = (_dt.now() - _td(days=1)).strftime("%Y%m%d")

        txs = get_transactions(date_from=yday, date_to=today)
        if not txs:
            return None

        cid = int(poster_client_id)
        client_txs = [
            t for t in txs
            if (isinstance(t, dict) and int(t.get("client_id", 0)) == cid
                and float(t.get("sum", 0)) > 0)
        ]
        if not client_txs:
            return None

        last = sorted(
            client_txs,
            key=lambda t: t.get("date_close", ""),
            reverse=True
        )[0]

        total      = float(last.get("sum", 0))
        bonus_used = float(last.get("payed_bonus", 0))
        return {"total": total, "bonus_used": bonus_used}

    except Exception as e:
        print(f"[get_last_order] ❌ {e}")
        return None

def get_bonus(client_id):
    client = get_client(client_id)
    if client and "bonus" in client:
        return client["bonus"]
    return None

def _get_client_bonus_kopecks(poster_client_id):
    """Повертає баланс бонусів у копійках (raw, як зберігається у Poster)."""
    try:
        if not poster_client_id:
            print("[balance] ❌ poster_client_id пустий")
            return None

        print(f"[balance] clients.getClient client_id={poster_client_id}")
        data = get_client(int(poster_client_id))

        if isinstance(data, list):
            client = data[0] if data else None
        elif isinstance(data, dict):
            client = data
        else:
            client = None

        if not client:
            print(f"[balance] ❌ Клієнт {poster_client_id} не знайдений")
            return None

        bonus = client.get("bonus")
        if bonus is None:
            print(f"[balance] ❌ Немає поля bonus у клієнта {poster_client_id}")
            return None

        print(f"[balance] ✅ client_id={poster_client_id} bonus_kopecks={bonus}")
        return float(bonus)  # у копійках

    except Exception as e:
        print(f"[balance] ❌ ERROR: {e}")
        return None

def get_poster_balance(poster_client_id):
    """Повертає баланс бонусів у гривнях.
    Poster зберігає bonus у clients.getClient у копійках → ділимо на 100.
    """
    kopecks = _get_client_bonus_kopecks(poster_client_id)
    if kopecks is None:
        return None
    hrn = kopecks / 100.0
    print(f"[balance] → {hrn} грн")
    return hrn

def check_token():
    try:
        url = f"{ACCOUNT_BASE_URL}/clients.getClients"
        r = requests.get(url, params={"token": POSTER_TOKEN}, timeout=10)
        print(f"[token_check] Status: {r.status_code} | {r.text[:300]}")
    except Exception as e:
        print(f"[token_check] ERROR: {e}")

def add_bonus(client_id, amount, comment=None):
    """Безпечне нарахування бонусів через clients.updateClient з retry та верифікацією.

    Алгоритм (до 3 спроб):
      1. Прочитати поточний баланс (грн)
      2. expected = max(0, round(current + amount))   ← захист від мінусу
      3. Записати updateClient(bonus=expected)
      4. Прочитати баланс знову і порівняти:
         - actual ≈ expected → SUCCESS
         - actual < expected → запис перетертий ([bonus_conflict]) → retry
         - actual > expected → хтось ще додав паралельно ([bonus_fixed]) → OK
    Повертає (ok: bool, status_code: int, data: dict|None).
    """
    MAX_RETRIES = 3
    TOLERANCE = 1      # ±1 грн — похибка округлення
    RETRY_DELAY = 0.5  # секунди між спробами

    update_url = f"{ACCOUNT_BASE_URL}/clients.updateClient"
    last_status, last_data = 0, None

    for attempt in range(MAX_RETRIES):
        prefix = f"[poster_bonus_add] attempt={attempt + 1}/{MAX_RETRIES} client_id={client_id}"

        # ── 1. Читаємо поточний баланс ────────────────────────────────────
        current_hrn = get_poster_balance(int(client_id))
        if current_hrn is None:
            print(f"{prefix} | [poster_bonus_error] не вдалося прочитати баланс")
            time.sleep(RETRY_DELAY)
            continue

        # ── 2. Обчислюємо цільовий баланс ─────────────────────────────────
        expected_hrn = max(0, round(float(current_hrn) + float(amount)))
        print(f"{prefix} | {current_hrn:.2f} + {amount} = {expected_hrn} грн")

        # ── 3. Записуємо ──────────────────────────────────────────────────
        try:
            r = requests.post(
                update_url,
                params={"token": POSTER_TOKEN},
                data={"client_id": int(client_id), "bonus": expected_hrn},
                timeout=10,
            )
            last_status = r.status_code
            print(f"{prefix} | status={last_status} | {r.text[:120]}")
            try:
                last_data = r.json()
            except Exception:
                last_data = None

            write_ok = (
                last_status == 200
                and isinstance(last_data, dict)
                and "response" in last_data
                and last_data.get("response") not in [None, False, "0", 0]
            )
        except Exception as exc:
            print(f"{prefix} | [poster_bonus_error] запит упав: {exc}")
            time.sleep(RETRY_DELAY * (attempt + 1))
            continue

        if not write_ok:
            print(f"{prefix} | [poster_bonus_error] неуспішна відповідь: {last_data}")
            time.sleep(RETRY_DELAY * (attempt + 1))
            continue

        # ── 4. Верифікація після запису ────────────────────────────────────
        time.sleep(0.3)
        actual_hrn = get_poster_balance(int(client_id))

        if actual_hrn is None:
            # Запис пройшов, але перевірити не вдалося — приймаємо як успіх
            print(f"{prefix} | ⚠️ запис OK, але верифікацію балансу пропущено")
            return True, last_status, last_data

        diff = round(float(actual_hrn) - float(expected_hrn))

        if abs(diff) <= TOLERANCE:
            # Баланс відповідає очікуваному — всe ОК
            print(f"[bonus_success] client_id={client_id} | "
                  f"очікувалось={expected_hrn} | фактично={actual_hrn:.2f} грн")
            return True, last_status, last_data

        if float(actual_hrn) > float(expected_hrn) + TOLERANCE:
            # Хтось додав бонуси паралельно ПІСЛЯ нашого запису — наш запис ОК,
            # поточний баланс навіть більший за очікуваний
            print(f"[bonus_fixed] client_id={client_id} | "
                  f"фактично={actual_hrn:.2f} > очікувалось={expected_hrn} "
                  f"(+{diff} грн від паралельного запису) — прийнятно")
            return True, last_status, last_data

        # actual < expected: наш запис перетертий стороннім процесом
        print(f"[bonus_conflict] client_id={client_id} | attempt={attempt + 1} | "
              f"записали {expected_hrn}, але фактично {actual_hrn:.2f} грн "
              f"(втрата {abs(diff)} грн)")
        if attempt < MAX_RETRIES - 1:
            print(f"[bonus_retry] повторюємо спробу {attempt + 2}/{MAX_RETRIES}...")
            time.sleep(RETRY_DELAY * (attempt + 1))
            # Наступна ітерація прочитає актуальний balanс і додасть amount знову

    print(f"[poster_bonus_error] ❌ вичерпано {MAX_RETRIES} спроб для client_id={client_id}")
    return False, last_status, last_data
