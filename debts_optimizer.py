import sqlite3
import os
from collections import defaultdict

# Новый модуль-оптимизатор долгов.
# Подход:
# 1) Считать все непомеченные (is_paid=0) записи expense_participant вместе с валютой и кредитором (expense.user_id).
# 2) Для каждой валюты собрать баланс пользователей: balance[uid] = суммарно + (нужны им) или - (они должны).
#    Реализация: при строке (debtor -> creditor, amount): balance[debtor] -= amount; balance[creditor] += amount
# 3) Для каждой валюты выполнить неттинг: сопоставить должников (balance < 0) и кредиторов (balance > 0) и сформировать переводы.
#    Для точности и избежания ошибок с float работаем в целых копейках (amount_cents = round(amount*100)).
# 4) Произвести аллокации — сопоставить переводам конкретные строки expense_participant должника (по id возрастанию) и вернуть структуру
#    с allocs = [(ep_id, used_amount, expense_id, original_amount), ...]
# 5) mark_allocations_paid выполняет все изменения в одной транзакции (BEGIN IMMEDIATE), проверяет согласованность и либо коммитит, либо откатывает.


def _to_cents(x):
    return int(round(float(x) * 100))


def _from_cents(c):
    return round(c / 100.0, 2)


def _normalize_currency(cur):
    if cur is None:
        return 'RUB'
    s = str(cur).strip().upper()
    return s if s else 'RUB'


def _read_unpaid_rows(conn):
    cur = conn.cursor()
    cur.execute('''
        SELECT ep.id as ep_id, ep.user_id as debtor_id, e.user_id as creditor_id, ep.amount, COALESCE(UPPER(TRIM(e.currency)), 'RUB') as currency, ep.expense_id
        FROM expense_participant ep
        JOIN expense e ON ep.expense_id = e.id
        WHERE ep.is_paid = 0
    ''')
    return cur.fetchall()


def optimize_transfers(db_path='expenses.db'):
    """Возвращает список кортежей (from_id, to_id, amount, currency).

    Алгоритм: по каждой валюте собрать балансы и затем сопоставить должников и кредиторов жадно.
    """
    conn = sqlite3.connect(db_path)
    rows = _read_unpaid_rows(conn)
    conn.close()

    if not rows:
        return []

    by_currency = defaultdict(list)
    for ep_id, debtor_id, creditor_id, amount, currency, expense_id in rows:
        cur = _normalize_currency(currency)
        by_currency[cur].append((debtor_id, creditor_id, amount))

    transfers = []
    for cur, recs in by_currency.items():
        # баланс в копейках
        bal = defaultdict(int)
        for debtor, creditor, amount in recs:
            cents = _to_cents(amount)
            if debtor == creditor:
                continue
            bal[debtor] -= cents
            bal[creditor] += cents

        # отдельные списки должников и кредиторов (id, cents)
        debtors = [(uid, -amt) for uid, amt in bal.items() if amt < 0]
        creditors = [(uid, amt) for uid, amt in bal.items() if amt > 0]

        # сортировка не обязательна, но стабилизирует результат
        debtors.sort(key=lambda x: x[0])
        creditors.sort(key=lambda x: x[0])

        i = 0
        j = 0
        while i < len(debtors) and j < len(creditors):
            deb_id, deb_amt = debtors[i]
            cred_id, cred_amt = creditors[j]
            take = min(deb_amt, cred_amt)
            if take > 0:
                transfers.append((deb_id, cred_id, _from_cents(take), cur))
                deb_amt -= take
                cred_amt -= take
                # обновим
                debtors[i] = (deb_id, deb_amt)
                creditors[j] = (cred_id, cred_amt)
            if debtors[i][1] == 0:
                i += 1
            if j < len(creditors) and creditors[j][1] == 0:
                j += 1

    if os.environ.get('DEBTS_DEBUG'):
        print('optimize_transfers ->', transfers)
    return transfers


def optimize_transfers_with_allocations(db_path='expenses.db'):
    """Возвращает список dict: {from, to, amount, currency, allocs}
    allocs = [(ep_id, used_amount, expense_id, original_amount), ...]
    """
    transfers = optimize_transfers(db_path)
    if not transfers:
        return []

    # агрегируем по (from,to,currency)
    agg = defaultdict(int)  # cents
    for frm, to, amt, cur in transfers:
        agg[(frm, to, _normalize_currency(cur))] += _to_cents(amt)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    detailed = []
    # кеш свободных записей должника по (user_id, currency): list of [ep_id, available_cents, expense_id, original_amount]
    debtor_cache = {}

    for (frm, to, cur), total_cents in agg.items():
        remaining = total_cents
        allocs = []
        cache_key = (frm, cur)
        if cache_key not in debtor_cache:
            cursor.execute('''
                SELECT ep.id, ep.amount, ep.expense_id
                FROM expense_participant ep
                JOIN expense e ON ep.expense_id = e.id
                WHERE ep.is_paid = 0 AND ep.user_id = ? AND COALESCE(UPPER(TRIM(e.currency)), 'RUB') = ?
                ORDER BY ep.id ASC
            ''', (frm, cur))
            rows = cursor.fetchall()
            # представим в копейках
            debtor_cache[cache_key] = [[r[0], _to_cents(r[1]), r[2], float(r[1])] for r in rows]

        rows = debtor_cache[cache_key]
        idx = 0
        while remaining > 0 and idx < len(rows):
            ep_id, avail, expense_id, orig = rows[idx]
            if avail <= 0:
                idx += 1
                continue
            take = min(avail, remaining)
            if take <= 0:
                idx += 1
                continue
            allocs.append((ep_id, _from_cents(take), expense_id, orig))
            remaining -= take
            rows[idx][1] = avail - take
            if rows[idx][1] <= 0:
                idx += 1

        if remaining > 0:
            conn.close()
            raise Exception(f"Не удалось собрать сумму {frm}->{to} { _from_cents(total_cents) } {cur}: осталось {_from_cents(remaining)}")

        detailed.append({'from': frm, 'to': to, 'amount': _from_cents(total_cents), 'currency': cur, 'allocs': allocs})

    if os.environ.get('DEBTS_DEBUG'):
        print('optimize_transfers_with_allocations ->', detailed)

    conn.close()
    return detailed


def mark_allocations_paid(db_path, transfers_with_allocs):
    """Применяет аллокации атомарно.

    Для каждой аллокации (ep_id, used, expense_id, original_amount):
      - проверяем, что запись существует, is_paid = 0 и в ней достаточно amount
      - если used >= cur_amount - 1 копейка: помечаем запись is_paid = 1
      - иначе: уменьшаем текущую запись на used (UPDATE amount = remaining) и вставляем новую запись с is_paid=1 на used
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        conn.execute('BEGIN IMMEDIATE')
        for t in transfers_with_allocs:
            allocs = t.get('allocs', [])
            for ep_id, used_amount, expense_id, original in allocs:
                used_cents = _to_cents(used_amount)
                cursor.execute('SELECT amount, is_paid, user_id FROM expense_participant WHERE id = ?', (ep_id,))
                row = cursor.fetchone()
                if not row:
                    raise Exception(f'Запись expense_participant id={ep_id} не найдена')
                cur_amount, cur_is_paid, cur_user_id = row
                cur_cents = _to_cents(cur_amount)
                if cur_is_paid:
                    raise Exception(f'Запись expense_participant id={ep_id} уже помечена как оплаченная')
                if cur_cents < used_cents:
                    raise Exception(f'Недостаточно средств в записи id={ep_id}: доступно {_from_cents(cur_cents)}, требуется {_from_cents(used_cents)}')

                # полное списание
                if used_cents >= cur_cents:
                    # помечаем как оплачено
                    cursor.execute('UPDATE expense_participant SET is_paid = 1 WHERE id = ?', (ep_id,))
                else:
                    # частичное списание: уменьшаем существующую запись и создаём новую помеченную
                    remaining_cents = cur_cents - used_cents
                    cursor.execute('UPDATE expense_participant SET amount = ? WHERE id = ?', (_from_cents(remaining_cents), ep_id))
                    cursor.execute('INSERT INTO expense_participant (expense_id, user_id, amount, is_paid) VALUES (?, ?, ?, ?)',
                                   (expense_id, cur_user_id, _from_cents(used_cents), 1))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print('mark_allocations_paid error:', e)
        raise
    finally:
        conn.close()


def mark_all_unpaid_as_paid(db_path='expenses.db'):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute('UPDATE expense_participant SET is_paid = 1 WHERE is_paid = 0')
    conn.commit()
    conn.close()


def get_all_users(db_path='expenses.db'):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute('SELECT id, name, payment_credentials FROM user')
    rows = cur.fetchall()
    conn.close()
    return [(r[0], r[1], r[2]) for r in rows]
