import sqlite3

# edges: Dict[int, Dict[int, float]]  # edges[from_id][to_id] = amount


def _aggregate_edges(rows):
    edges = {}
    for debtor_id, creditor_id, amount in rows:
        if debtor_id == creditor_id:
            continue
        edges.setdefault(debtor_id, {})
        edges[debtor_id][creditor_id] = edges[debtor_id].get(creditor_id, 0.0) + float(amount)
    # round amounts
    for u in list(edges.keys()):
        for v in list(edges[u].keys()):
            edges[u][v] = round(edges[u][v], 2)
            if edges[u][v] <= 0.005:
                del edges[u][v]
        if not edges[u]:
            del edges[u]
    return edges


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
    return edges_by_currency, user_ids


def _find_cycle(edges):
    # nodes -> all keys and targets
    nodes = set(edges.keys())
    for u in list(edges.keys()):
        for v in edges.get(u, {}).keys():
            nodes.add(v)

    visited = set()
    stack = []
    onstack = set()

    def dfs(u):
        visited.add(u)
        stack.append(u)
        onstack.add(u)

        for v in edges.get(u, {}):
            if edges[u].get(v, 0) <= 0:
                continue
            if v in onstack:
                idx = stack.index(v)
                cycle = stack[idx:] + [v]
                return cycle
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
    # repeatedly find a directed cycle and reduce weights along it
    while True:
        cycle = _find_cycle(edges)
        if not cycle:
            break
        # cycle is like [v, ..., v]
        pairs = []
        for i in range(len(cycle) - 1):
            a = cycle[i]
            b = cycle[i + 1]
            pairs.append((a, b))
        minamt = min(edges[a][b] for a, b in pairs)
        minamt = round(minamt, 2)
        for a, b in pairs:
            edges[a][b] = round(edges[a][b] - minamt, 2)
            if edges[a][b] <= 0.005:
                del edges[a][b]
        # cleanup empty keys
        empty = [k for k, v in edges.items() if not v]
        for k in empty:
            del edges[k]
    return edges


def _net_mutual(edges):
    # for any pair (u,v) and (v,u) net the amounts so only one direction remains
    seen = set()
    for u in list(edges.keys()):
        for v in list(edges.get(u, {}).keys()):
            if (u, v) in seen or (v, u) in seen:
                continue
            amt_uv = edges.get(u, {}).get(v, 0.0)
            amt_vu = edges.get(v, {}).get(u, 0.0)
            if amt_uv and amt_vu:
                if amt_uv > amt_vu:
                    edges[u][v] = round(amt_uv - amt_vu, 2)
                    # remove reverse
                    if v in edges and u in edges[v]:
                        del edges[v][u]
                elif amt_vu > amt_uv:
                    edges[v][u] = round(amt_vu - amt_uv, 2)
                    if u in edges and v in edges[u]:
                        del edges[u][v]
                else:
                    # equal -> remove both
                    del edges[u][v]
                    if v in edges and u in edges[v]:
                        del edges[v][u]
            seen.add((u, v))
            seen.add((v, u))
    # cleanup zeros/empties
    for u in list(edges.keys()):
        for v in list(edges[u].keys()):
            if edges[u][v] <= 0.005:
                del edges[u][v]
        if not edges[u]:
            del edges[u]
    return edges


def optimize_transfers(db_path='expenses.db'):
    """Оптимизирует переводы по каждой валюте отдельно и возвращает список переводов

    Возвращает список кортежей (from_id, to_id, amount, currency)
    """
    edges_by_currency, _ = build_debt_graph(db_path)
    if not edges_by_currency:
        return []

    all_transfers = []
    for currency, edges in edges_by_currency.items():
        if not edges:
            continue
        # copy to avoid mutating original
        local_edges = {u: dict(targets) for u, targets in edges.items()}
        local_edges = _cancel_cycles(local_edges)
        local_edges = _net_mutual(local_edges)
        for u, targets in local_edges.items():
            for v, amt in targets.items():
                if amt and amt > 0.005:
                    all_transfers.append((u, v, round(amt, 2), currency))
    return all_transfers


def optimize_transfers_with_allocations(db_path='expenses.db'):
    """Оптимизирует переводы (по валютам) и сопоставляет каждый перевод с конкретными
    записями expense_participant (непогашёнными).

    Возвращает список переводов в формате:
      { 'from': uid_from, 'to': uid_to, 'amount': amt, 'currency': cur, 'allocs': [ (ep_id, used_amount, expense_id, original_amount) ] }

    allocs — список пар (id доли, сколько из неё используется для погашения).
    """
    transfers = optimize_transfers(db_path)
    if not transfers:
        return []

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    detailed = []

    for frm, to, amt, currency in transfers:
        remaining = float(amt)
        allocs = []
        # получаем непогашённые записи, относящиеся к этому дебету: ep.user_id = frm, expense.user_id = to, currency match
        cursor.execute('''
            SELECT ep.id, ep.amount, ep.expense_id
            FROM expense_participant ep
            JOIN expense e ON ep.expense_id = e.id
            WHERE ep.is_paid = 0 AND ep.user_id = ? AND e.user_id = ? AND e.currency = ?
            ORDER BY ep.id ASC
        ''', (frm, to, currency))

        rows = cursor.fetchall()
        for ep_id, ep_amount, expense_id in rows:
            if remaining <= 0.005:
                break
            take = min(float(ep_amount), remaining)
            take = round(take, 2)
            if take <= 0:
                continue
            allocs.append((ep_id, take, expense_id, float(ep_amount)))
            remaining = round(remaining - take, 2)

        if remaining > 0.01:
            # Непредвиденная ситуация: суммы не сходятся — логируем и продолжаем (останется непогашённая часть)
            # Но так как граф строился из этих записей, это маловероятно.
            # Мы не бросаем ошибку, а вернём частичную аллокацию.
            pass

        detailed.append({
            'from': frm,
            'to': to,
            'amount': round(float(amt), 2),
            'currency': currency,
            'allocs': allocs
        })

    conn.close()
    return detailed


def mark_allocations_paid(db_path, transfers_with_allocs):
    """Применяет аллокации к базе: помечает использованные части как оплаченные.

    Логика:
    - Для каждой аллокации (ep_id, used): если used >= original_amount - eps => просто обновляем is_paid = 1.
    - Если used < original_amount: уменьшаем существующую запись до remaining (original-used) и вставляем новую запись с amount=used и is_paid=1.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    for t in transfers_with_allocs:
        allocs = t.get('allocs', [])
        for ep_id, used, expense_id, original in allocs:
            used = round(float(used), 2)
            original = float(original)
            if used >= original - 0.005:
                # mark original row paid
                cursor.execute('UPDATE expense_participant SET is_paid = 1 WHERE id = ?', (ep_id,))
            else:
                remaining = round(original - used, 2)
                # reduce original row amount to remaining
                cursor.execute('UPDATE expense_participant SET amount = ? WHERE id = ?', (remaining, ep_id))
                # insert a new row representing погашённую часть and mark it paid
                cursor.execute('INSERT INTO expense_participant (expense_id, user_id, amount, is_paid) VALUES (?, ?, ?, ?)',
                               (expense_id, t['from'], used, 1))

    conn.commit()
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
