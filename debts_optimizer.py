import sqlite3
import os
from collections import defaultdict


def _aggregate_edges(rows):
    """Агрегировать ребра (debtor->creditor) и округлить суммы."""
    edges = defaultdict(lambda: defaultdict(float))
    for debtor_id, creditor_id, amount in rows:
        if debtor_id == creditor_id:
            continue
        edges[debtor_id][creditor_id] += float(amount)

    # округление и удаление нулей
    clean = {}
    for u, targets in edges.items():
        for v, a in list(targets.items()):
            a = round(a, 2)
            if a > 0.005:
                clean.setdefault(u, {})[v] = a
    return clean


def build_debt_graph(db_path='expenses.db'):
    """Собирает ориентированный граф долгов из непогашенных записей и группирует по валютам.

    Возвращает (edges_by_currency, user_ids)
    edges_by_currency: {currency: {debtor_id: {creditor_id: amount}}}
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute('''
        SELECT ep.user_id as debtor_id, e.user_id as creditor_id, ep.amount, e.currency
        FROM expense_participant ep
        JOIN expense e ON ep.expense_id = e.id
        WHERE ep.is_paid = 0
    ''')
    rows = cursor.fetchall()

    # rows: list of (debtor_id, creditor_id, amount, currency)
    edges_by_currency = {}
    for debtor_id, creditor_id, amount, currency in rows:
        # normalize currency: trim, upper-case, fallback to 'RUB' only if empty
        if currency is None:
            cur = 'RUB'
        else:
            cur = str(currency).strip().upper()
            if not cur:
                cur = 'RUB'
        edges_by_currency.setdefault(cur, []).append((debtor_id, creditor_id, amount))

    # aggregate per currency
    for cur, recs in list(edges_by_currency.items()):
        edges_by_currency[cur] = _aggregate_edges(recs)

    cursor.execute('SELECT id FROM user')
    user_rows = cursor.fetchall()
    conn.close()

    user_ids = [r[0] for r in user_rows]
    if os.environ.get('DEBTS_DEBUG'):
        print('build_debt_graph edges_by_currency=', edges_by_currency)
    return edges_by_currency, user_ids


def _find_cycle(edges):
    """Найти любую простую ориентированную цикл-цепочку в графе edges или вернуть None.
    edges: {u: {v: amount}}
    Возвращает список узлов [v1, v2, ..., v1]"""
    nodes = set(edges.keys())
    for u, targets in edges.items():
        nodes.update(targets.keys())

    visited = set()
    onstack = set()
    stack = []

    def dfs(u):
        visited.add(u)
        onstack.add(u)
        stack.append(u)
        for v in edges.get(u, {}):
            if edges[u].get(v, 0) <= 0:
                continue
            if v in onstack:
                idx = stack.index(v)
                return stack[idx:] + [v]
            if v not in visited:
                res = dfs(v)
                if res:
                    return res
        stack.pop()
        onstack.remove(u)
        return None

    for n in nodes:
        if n not in visited:
            res = dfs(n)
            if res:
                return res
    return None


def _cancel_cycles(edges):
    """Пока есть цикл — уменьшаем по минимальному ребру вдоль него."""
    # работаем на изменяемой структуре
    while True:
        cycle = _find_cycle(edges)
        if not cycle:
            break
        pairs = [(cycle[i], cycle[i + 1]) for i in range(len(cycle) - 1)]
        minamt = min(edges[a][b] for a, b in pairs)
        minamt = round(minamt, 2)
        for a, b in pairs:
            edges[a][b] = round(edges[a][b] - minamt, 2)
            if edges[a][b] <= 0.005:
                del edges[a][b]
        # удалить пустые вершины
        for k in [k for k, v in list(edges.items()) if not v]:
            del edges[k]
    return edges


def _net_mutual(edges):
    """Для каждой пары (u,v) и (v,u) неттим суммы, оставляя одну сторону."""
    # собираем пары для обработки, чтобы не ломать итерирование
    for u in list(edges.keys()):
        for v in list(edges.get(u, {}).keys()):
            if u == v:
                continue
            a = edges.get(u, {}).get(v, 0)
            b = edges.get(v, {}).get(u, 0)
            if a and b:
                if a > b:
                    edges[u][v] = round(a - b, 2)
                    if v in edges and u in edges[v]:
                        del edges[v][u]
                elif b > a:
                    edges[v][u] = round(b - a, 2)
                    if u in edges and v in edges[u]:
                        del edges[u][v]
                else:
                    # равны — удаляем обе
                    if v in edges.get(u, {}):
                        del edges[u][v]
                    if u in edges.get(v, {}):
                        del edges[v][u]
    # убрать нулевые и пустые
    for u in [k for k, v in list(edges.items())]:
        for v in [t for t in list(edges.get(u, {}).keys())]:
            if edges[u][v] <= 0.005:
                del edges[u][v]
        if not edges.get(u):
            del edges[u]
    return edges


def optimize_transfers(db_path='expenses.db'):
    """Оптимизирует переводы по каждой валюте отдельно и возвращает список переводов

    Возвращает список кортежей (from_id, to_id, amount, currency)
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute('''
        SELECT ep.user_id as debtor_id, e.user_id as creditor_id, ep.amount, COALESCE(UPPER(TRIM(e.currency)), 'RUB') as currency
        FROM expense_participant ep
        JOIN expense e ON ep.expense_id = e.id
        WHERE ep.is_paid = 0
    ''')
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return []

    # Построим ребра по валютам и агрегируем напрямую
    by_currency = defaultdict(list)
    for debtor_id, creditor_id, amount, currency in rows:
        by_currency[currency].append((debtor_id, creditor_id, amount))

    final_transfers = []
    for cur, recs in by_currency.items():
        edges = _aggregate_edges(recs)
        edges = _cancel_cycles(edges)
        edges = _net_mutual(edges)
        # собрать финальные переводы
        for u, targets in edges.items():
            for v, amt in targets.items():
                if amt and amt > 0.005:
                    final_transfers.append((u, v, round(amt, 2), cur))

    if os.environ.get('DEBTS_DEBUG'):
        print('optimize_transfers transfers=', final_transfers)
    return final_transfers


def optimize_transfers_with_allocations(db_path='expenses.db'):
    """Новый вариант: выполняет неттирование по пользователям и сопоставляет переводы с конкретными непогашёнными записями должников.

    Возвращает список словарей: {from,to,amount,currency,allocs}
    where allocs = [(ep_id, used, expense_id, original_amount), ...]
    """
    # сначала получаем переводы неттирования (direct transfers)
    transfers = optimize_transfers(db_path)
    if not transfers:
        return []

    # агрегируем переводы по (from,to,currency)
    agg = {}
    for frm, to, amount, currency in transfers:
        key = (frm, to, currency)
        agg[key] = agg.get(key, 0.0) + float(amount)
    if os.environ.get('DEBTS_DEBUG'):
        print('optimize_transfers_with_allocations agg=', agg)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    detailed = []
    debtor_cache = {}  # key: (user_id, currency) -> list of (ep.id, amount, expense_id)

    # теперь создаём аллокации для каждой агрегированной пары
    for (frm, to, currency), total_amount in agg.items():
        # currency coming from optimize_transfers should already be normalized, but normalize again to be safe
        cur = (currency or 'RUB').strip().upper()
        remaining = round(float(total_amount), 2)
        allocs = []

        cache_key = (frm, cur)
        if cache_key not in debtor_cache:
            # match currency same way as in optimize_transfers: COALESCE(UPPER(TRIM(e.currency)), 'RUB')
            cursor.execute('''
                SELECT ep.id, ep.amount, ep.expense_id
                FROM expense_participant ep
                JOIN expense e ON ep.expense_id = e.id
                WHERE ep.is_paid = 0 AND ep.user_id = ? AND COALESCE(UPPER(TRIM(e.currency)), 'RUB') = ?
                ORDER BY ep.id ASC
            ''', (frm, cur))
            debtor_cache[cache_key] = list(cursor.fetchall())

        rows = debtor_cache[cache_key]
        idx = 0
        while remaining > 0.005 and idx < len(rows):
            ep_id, ep_amount, expense_id = rows[idx]
            available = float(ep_amount)
            if available <= 0.005:
                idx += 1
                continue
            take = min(available, remaining)
            take = round(take, 2)
            if take <= 0:
                idx += 1
                continue
            allocs.append((ep_id, take, expense_id, float(ep_amount)))
            remaining = round(remaining - take, 2)
            # update cached available amount
            rows[idx] = (ep_id, round(available - take, 2), expense_id)
            if rows[idx][1] <= 0.005:
                idx += 1

        # Если не получилось закрыть всю сумму — это ошибка согласованности данных
        if remaining > 0.005:
            conn.close()
            raise Exception(f"Не удалось собрать сумму {frm}->{to} {total_amount} {cur}: осталось {remaining}")

        detailed.append({'from': frm, 'to': to, 'amount': round(total_amount, 2), 'currency': cur, 'allocs': allocs})
    if os.environ.get('DEBTS_DEBUG'):
        print('optimize_transfers_with_allocations detailed=', detailed)

    conn.close()
    return detailed


def mark_allocations_paid(db_path, transfers_with_allocs):
    """Применяет аллокации к базе в одной транзакции с проверками.

    Поведение:
    - Выполняет BEGIN IMMEDIATE, чтобы избежать конкурентных записей в SQLite.
    - Для каждой аллокации проверяет, что запись существует и не помечена как оплаченная,
      и что в ней достаточно суммы для списания.
    - Если проверка не проходит — откатывает транзакцию и выбрасывает исключение.
    - В противном случае уменьшает/помечает записи и добавляет запись о погашённой части (is_paid=1) при частичном списании.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        # Начинаем немедленную транзакцию (блокировка для записи)
        conn.execute('BEGIN IMMEDIATE')

        for t in transfers_with_allocs:
            allocs = t.get('allocs', [])
            for ep_id, used, expense_id, original in allocs:
                used = round(float(used), 2)

                # Получим текущую запись и проверим состояние
                cursor.execute('SELECT amount, is_paid, user_id FROM expense_participant WHERE id = ?', (ep_id,))
                row = cursor.fetchone()
                if not row:
                    raise Exception(f'Запись expense_participant id={ep_id} не найдена')
                cur_amount, cur_is_paid, cur_user_id = row
                cur_amount = float(cur_amount)
                if cur_is_paid:
                    raise Exception(f'Запись expense_participant id={ep_id} уже помечена как оплаченная')
                if cur_amount + 1e-9 < used:
                    raise Exception(f'Недостаточно средств в записи id={ep_id}: доступно {cur_amount}, требуется {used}')

                if used >= cur_amount - 0.005:
                    # Полное списание текущей записи
                    cursor.execute('UPDATE expense_participant SET is_paid = 1 WHERE id = ?', (ep_id,))
                else:
                    # Частичное списание: уменьшаем оригинальную строку и добавляем помеченную часть
                    remaining = round(cur_amount - used, 2)
                    cursor.execute('UPDATE expense_participant SET amount = ? WHERE id = ?', (remaining, ep_id))
                    # Вставляем запись, которая будет помечена как оплаченная
                    cursor.execute(
                        'INSERT INTO expense_participant (expense_id, user_id, amount, is_paid) VALUES (?, ?, ?, ?)',
                        (expense_id, cur_user_id, used, 1)
                    )

        conn.commit()
    except Exception as e:
        conn.rollback()
        # Логируем и пробрасываем ошибку дальше
        print(f"mark_allocations_paid error: {e}")
        raise
    finally:
        conn.close()


def mark_all_unpaid_as_paid(db_path='expenses.db'):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute('UPDATE expense_participant SET is_paid = 1 WHERE is_paid = 0')
    conn.commit()
    conn.close()


def get_all_users(db_path='expenses.db'):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute('SELECT id, name FROM user')
    rows = cursor.fetchall()
    conn.close()
    return [(r[0], r[1]) for r in rows]
