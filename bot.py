import config
import logging
import sqlite3
import html
import debts_optimizer
from datetime import datetime
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler
)
import ocr

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = config.BOT_TOKEN

user_states = {}

# =========================
# DB INIT & HELPERS
# =========================

def init_database():
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()

    # events
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS event (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL
        )
    ''')

    # users
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        )
    ''')

    # categories
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS category (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        )
    ''')

    # expenses
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS expense (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'RUB',
            event_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            paid_date TEXT,
            user_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            category_id INTEGER,
            FOREIGN KEY (event_id) REFERENCES event (id),
            FOREIGN KEY (user_id) REFERENCES user (id),
            FOREIGN KEY (category_id) REFERENCES category (id)
        )
    ''')

    # add category_id to expense if missing (migration)
    cursor.execute("PRAGMA table_info(expense)")
    cols = [r[1] for r in cursor.fetchall()]
    if 'category_id' not in cols:
        try:
            cursor.execute("ALTER TABLE expense ADD COLUMN category_id INTEGER")
        except Exception as e:
            logger.warning("ALTER TABLE expense add category_id failed: %s", e)

    # participants
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS expense_participant (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount REAL NOT NULL,
            expense_id INTEGER NOT NULL,
            is_paid BOOLEAN,
            user_id INTEGER NOT NULL,
            FOREIGN KEY (expense_id) REFERENCES expense (id),
            FOREIGN KEY (user_id) REFERENCES user (id)
        )
    ''')

    # default event
    cursor.execute('SELECT id FROM event WHERE id = 1')
    if not cursor.fetchone():
        cursor.execute('INSERT INTO event (id, name) VALUES (1, "Основное мероприятие")')

    # default categories
    default_categories = ["Еда", "Транспорт", "Жилье", "Развлечения", "Прочее"]
    cursor.execute('SELECT COUNT(*) FROM category')
    cnt = cursor.fetchone()[0]
    if cnt == 0:
        cursor.executemany('INSERT INTO category (name) VALUES (?)', [(n,) for n in default_categories])

    conn.commit()
    conn.close()

def get_or_create_user(user_id, user_name):
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()

    cursor.execute('SELECT id FROM user WHERE id = ?', (user_id,))
    user = cursor.fetchone()

    if not user:
        cursor.execute('INSERT INTO user (id, name) VALUES (?, ?)', (user_id, user_name))
        conn.commit()

    conn.close()
    return user_id

def get_categories_from_db():
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()
    cursor.execute('SELECT id, name FROM category ORDER BY id')
    rows = cursor.fetchall()
    conn.close()
    return rows  # list of (id, name)

def get_category_keyboard():
    cats = get_categories_from_db()
    # по одному в ряд для наглядности
    keyboard = [[InlineKeyboardButton(name, callback_data=f"category_{cid}")]
                for cid, name in cats]
    return InlineKeyboardMarkup(keyboard)

def get_currency_keyboard():
    keyboard = [
        [InlineKeyboardButton("RUB ₽", callback_data="currency_RUB")],
        [InlineKeyboardButton("USD $", callback_data="currency_USD")],
        [InlineKeyboardButton("EUR €", callback_data="currency_EUR")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_main_keyboard():
    keyboard = [
        [KeyboardButton("Создать платеж"), KeyboardButton("Баланс")],
        [KeyboardButton("Мой долг"), KeyboardButton("Общий долг")],
        [KeyboardButton("Мой долг по категориям"), KeyboardButton("Общий долг по категориям")],
        [KeyboardButton("История платежей")],
        [KeyboardButton("Оптимизация долгов")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_confirmation_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("Подтвердить", callback_data="confirm_payment"),
            InlineKeyboardButton("Отменить", callback_data="cancel_payment")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def save_payment_to_db(payment_data, message_id=None):
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()

    cursor.execute('''
        INSERT INTO expense (event_id, name, user_id, paid_date, amount, currency, message_id, category_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        1,
        payment_data['description'],
        payment_data['user_id'],
        payment_data['timestamp'],
        payment_data['amount'],
        payment_data.get('currency', 'RUB'),
        message_id,
        payment_data.get('category_id')
    ))

    payment_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return payment_id

def save_share_to_db(payment_id, user_id, user_name, amount):
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()

    get_or_create_user(user_id, user_name)

    cursor.execute('''
        INSERT INTO expense_participant (expense_id, user_id, amount, is_paid)
        VALUES (?, ?, ?, ?)
    ''', (payment_id, user_id, amount, 0))

    conn.commit()
    conn.close()

def get_payments_from_db(limit=5):
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()

    cursor.execute('''
        SELECT e.*, u.name, c.name
        FROM expense e
        JOIN user u ON e.user_id = u.id
        LEFT JOIN category c ON e.category_id = c.id
        ORDER BY e.paid_date DESC LIMIT ?
    ''', (limit,))

    payments = cursor.fetchall()
    conn.close()
    return payments

def get_shares_for_payment(payment_id):
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()

    cursor.execute('''
        SELECT ep.*, u.name
        FROM expense_participant ep
        JOIN user u ON ep.user_id = u.id
        WHERE ep.expense_id = ?
    ''', (payment_id,))

    shares = cursor.fetchall()
    conn.close()
    return shares

# =========================
# HANDLERS
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    get_or_create_user(user.id, user.first_name)

    if user.id in user_states:
        del user_states[user.id]

    welcome_text = "Привет!\n\nЯ бот для учета совместных расходов.\n\nВыберите действие ниже"
    await update.message.reply_text(welcome_text, reply_markup=get_main_keyboard())

async def handle_main_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    text = update.message.text

    if user.id in user_states:
        del user_states[user.id]

    if text == "Создать платеж":
        await create_payment(update, context)
    elif text == "Баланс":
        await show_balance(update, context)
    elif text == "Мой долг":
        await show_my_debt(update, context)
    elif text == "Общий долг":
        await show_total_debt(update, context)
    elif text == "История платежей":
        await show_payment_history(update, context)
    elif text == "Оптимизация долгов":
        await optimize_debts(update, context)
    elif text == "Мой долг по категориям":
        await show_my_debt_by_category(update, context)
    elif text == "Общий долг по категориям":
        await show_total_debt_by_category(update, context)

async def create_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user

    if update.message.chat.type == "private":
        await update.message.reply_text(
            "Создание платежей работает только в групповых чатах",
            reply_markup=get_main_keyboard()
        )
        return

    user_states[user.id] = "waiting_title"
    await update.message.reply_text("Введите название платежа или прикрепите чек:")

async def handle_payment_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    text = update.message.text

    if update.message.reply_to_message:
        return

    if user_states.get(user.id) == "waiting_title":
        context.user_data['pending_payment'] = {
            'description': text.strip(),
            'user_id': user.id,
            'created_by': user.first_name,
            'chat_id': update.message.chat.id,
            'timestamp': datetime.now().strftime("%d.%m.%Y %H:%M")
        }
        user_states[user.id] = "waiting_amount"
        await update.message.reply_text("Введите сумму:")
        return

    elif user_states.get(user.id) == "waiting_amount":
        try:
            amount = float(text.replace(",", ".").strip())
            if amount <= 0:
                await update.message.reply_text("Сумма должна быть больше 0. Попробуйте снова:")
                return
        except ValueError:
            await update.message.reply_text("Введите корректное число:")
            return

        context.user_data['pending_payment']['amount'] = amount
        user_states[user.id] = "waiting_currency"

        await update.message.reply_text(
            "Выберите валюту:",
            reply_markup=get_currency_keyboard()
        )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    callback_data = query.data
    user = query.from_user

    if callback_data == "confirm_payment":
        if 'pending_payment' in context.user_data:
            payment_data = context.user_data['pending_payment']
            if payment_data.get('user_id') == user.id:
                await query.edit_message_text(
                    "Платеж подтвержден! Выберите категорию:",
                    reply_markup=get_category_keyboard()
                )
            else:
                await query.edit_message_text("Этот платеж принадлежит другому пользователю")
        else:
            await query.edit_message_text("Данные платежа не найдены")

    elif callback_data == "cancel_payment":
        if 'pending_payment' in context.user_data:
            payment_data = context.user_data['pending_payment']
            if payment_data.get('user_id') == user.id:
                del context.user_data['pending_payment']
                await query.edit_message_text("Создание платежа отменено")
            else:
                await query.edit_message_text("Этот платеж принадлежит другому пользователю")

    elif callback_data.startswith('category_'):
        # dynamic category
        try:
            cat_id = int(callback_data.split('_', 1)[1])
        except Exception:
            await query.edit_message_text("Некорректная категория")
            return

        # получим имя категории для отображения
        cats = dict(get_categories_from_db())
        cat_name = cats.get(cat_id, 'Категория')

        if 'pending_payment' in context.user_data:
            payment_data = context.user_data['pending_payment']
            if payment_data.get('user_id') == user.id:
                payment_data['category_id'] = cat_id
                payment_data['type'] = cat_name

                sent = await query.message.reply_text(
                    f"Платеж создан!\n\n"
                    f"Название: {payment_data['description']}\n"
                    f"Сумма: {payment_data['amount']} {payment_data.get('currency', 'RUB')}\n"
                    f"Категория: {payment_data['type']}\n"
                    f"Создал: {payment_data['created_by']}\n"
                    f"Время: {payment_data['timestamp']}\n\n"
                    f"Участники могут ответить на это сообщение числом — их долю в платеже."
                )

                save_payment_to_db(payment_data, sent.message_id)

                del context.user_data['pending_payment']
                await query.edit_message_text("Платёж сохранён.")
        else:
            await query.edit_message_text("Данные платежа не найдены")

    elif callback_data.startswith('currency_'):
        currency = callback_data.replace('currency_', '')
        if 'pending_payment' in context.user_data:
            payment_data = context.user_data['pending_payment']
            payment_data['currency'] = currency
            del user_states[user.id]

            await query.message.reply_text(
                f"Валюта выбрана: {currency}\n\n"
                f"Название: {payment_data['description']}\n"
                f"Сумма: {payment_data['amount']} {currency}\n"
                f"Создал: {payment_data['created_by']}\n\n"
                f"Подтвердите создание платежа:",
                reply_markup=get_confirmation_keyboard()
            )
        else:
            await query.edit_message_text("Данные платежа не найдены")

async def handle_reply_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пользователь отвечает числом на сообщение 'Платеж создан!' — записываем его долю к нужному платежу по message_id."""
    if update.message.reply_to_message:
        try:
            share_amount = float(update.message.text.replace(",", ".").strip())
            if share_amount <= 0:
                await update.message.reply_text("Сумма долга должна быть больше 0. Попробуйте ещё раз.")
                return

            user = update.message.from_user
            replied_id = update.message.reply_to_message.message_id

            conn = sqlite3.connect('expenses.db')
            cursor = conn.cursor()

            cursor.execute('SELECT id, currency FROM expense WHERE message_id = ? LIMIT 1', (replied_id,))
            row = cursor.fetchone()

            if row:
                payment_id, currency = row
                save_share_to_db(payment_id, user.id, user.first_name, share_amount)
                await update.message.reply_text(f"Записан ваш долг: {share_amount:.2f} {currency}")
            else:
                await update.message.reply_text("Не удалось найти платеж по этому сообщению")

            conn.close()
        except ValueError:
            await update.message.reply_text("Ошибка! Введите число - вашу долю в платеже")

async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()

    cursor.execute('''
        SELECT e.currency,
               SUM(CASE WHEN e.user_id = ? THEN e.amount ELSE 0 END) as paid_total,
               SUM(CASE WHEN ep.user_id = ? THEN ep.amount ELSE 0 END) as debt_total
        FROM expense e
        LEFT JOIN expense_participant ep ON e.id = ep.expense_id AND ep.is_paid = 0
        WHERE e.event_id = 1
        GROUP BY e.currency
    ''', (user.id, user.id))

    results = cursor.fetchall()
    conn.close()

    if not results:
        await update.message.reply_text("Нет данных о платежах")
        return

    balance_text = ""
    for currency, paid_total, debt_total in results:
        paid_total = paid_total or 0
        debt_total = debt_total or 0
        balance = paid_total - debt_total
        if balance > 0:
            balance_text += f"{currency}: +{balance:.2f} (вам должны)\n"
        elif balance < 0:
            balance_text += f"{currency}: {balance:.2f} (вы должны)\n"
        else:
            balance_text += f"{currency}: 0\n"

    await update.message.reply_text(balance_text or "0")

async def show_my_debt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Все НЕоплаченные долги пользователя по каждому платежу с указанием кому должен."""
    user = update.message.from_user
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()

    cursor.execute('''
        SELECT e.name, ep.amount, e.currency, u_payer.name
        FROM expense_participant ep
        JOIN expense e ON ep.expense_id = e.id
        JOIN user u_payer ON e.user_id = u_payer.id
        WHERE ep.user_id = ? AND ep.is_paid = 0
        ORDER BY e.paid_date DESC, e.id DESC
    ''', (user.id,))
    debts = cursor.fetchall()
    conn.close()

    if debts:
        debt_text = f"Ваши долги, {user.first_name}:\n\n"
        total_by_currency = {}
        for name, amount, currency, payer_name in debts:
            debt_text += f"{name}: {amount:.2f} {currency} (кому: {payer_name})\n"
            total_by_currency[currency] = total_by_currency.get(currency, 0) + float(amount)
        debt_text += "\nИтого:\n"
        for cur, total in total_by_currency.items():
            debt_text += f"{cur}: {total:.2f}\n"
    else:
        debt_text = "У вас нет долгов"

    await update.message.reply_text(debt_text)

async def show_total_debt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отображает долги всех пользователей: Должник -> Кредитор: сумма валюта (только активные долги)."""
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()

    cursor.execute('''
        SELECT u_debtor.name, u_payer.name, SUM(ep.amount) as total_debt, e.currency
        FROM expense_participant ep
        JOIN user u_debtor ON ep.user_id = u_debtor.id
        JOIN expense e ON ep.expense_id = e.id
        JOIN user u_payer ON e.user_id = u_payer.id
        WHERE ep.is_paid = 0
        GROUP BY u_debtor.id, u_debtor.name, u_payer.id, u_payer.name, e.currency
        ORDER BY u_debtor.name, u_payer.name
    ''')

    debts = cursor.fetchall()
    conn.close()

    if debts:
        debt_text = "Общие долги:\n\n"
        for debtor_name, payer_name, total, currency in debts:
            debt_text += f"{debtor_name} → {payer_name}: {float(total):.2f} {currency}\n"
    else:
        debt_text = "Нет активных долгов"

    await update.message.reply_text(debt_text)

async def show_my_debt_by_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Мои долги, сгруппированные по категориям и валютам."""
    user = update.message.from_user
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()

    cursor.execute('''
        SELECT COALESCE(c.name, 'Без категории') as cat_name, e.currency, SUM(ep.amount)
        FROM expense_participant ep
        JOIN expense e ON ep.expense_id = e.id
        LEFT JOIN category c ON e.category_id = c.id
        WHERE ep.user_id = ? AND ep.is_paid = 0
        GROUP BY cat_name, e.currency
        ORDER BY cat_name, e.currency
    ''', (user.id,))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("У вас нет активных долгов по категориям")
        return

    lines = [f"Ваши долги по категориям, {user.first_name}:"]
    cur_cat = None
    for cat_name, currency, total in rows:
        if cat_name != cur_cat:
            lines.append(f"\n— {cat_name} —")
            cur_cat = cat_name
        lines.append(f"{currency}: {float(total):.2f}")
    await update.message.reply_text("\n".join(lines))

async def show_total_debt_by_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Долги всех пользователей, сгруппированные по категориям и валютам."""
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()

    cursor.execute('''
        SELECT COALESCE(c.name, 'Без категории') as cat_name, e.currency, SUM(ep.amount)
        FROM expense_participant ep
        JOIN expense e ON ep.expense_id = e.id
        LEFT JOIN category c ON e.category_id = c.id
        WHERE ep.is_paid = 0
        GROUP BY cat_name, e.currency
        ORDER BY cat_name, e.currency
    ''')
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("Нет активных долгов по категориям")
        return

    lines = ["Общие долги по категориям:"]
    cur_cat = None
    for cat_name, currency, total in rows:
        if cat_name != cur_cat:
            lines.append(f"\n— {cat_name} —")
            cur_cat = cat_name
        lines.append(f"{currency}: {float(total):.2f}")
    await update.message.reply_text("\n".join(lines))

async def show_payment_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payments = get_payments_from_db()
    if not payments:
        await update.message.reply_text("История платежей пуста")
        return

    # e.* = [0:id,1:amount,2:currency,3:event_id,4:name,5:paid_date,6:user_id,7:message_id,8:category_id]
    # затем JOIN user u.name -> index 9, LEFT JOIN category c.name -> index 10
    history_text = "Последние платежи:\n\n"
    for p in payments:
        payment_id = p[0]
        amount = p[1]
        currency = p[2]
        name = p[4]
        paid_date = p[5] or ""
        payer_name = p[9]
        category_name = p[10] or "Без категории"

        history_text += (
            f"• {paid_date} | {payer_name} | {category_name}\n"
            f"   {amount:.2f} {currency} — {name}\n"
        )

        # Подтянем должников по этому платежу (только активные долги)
        shares = get_shares_for_payment(payment_id)
        # shares: ep.* + u.name => [0:id,1:amount,2:expense_id,3:is_paid,4:user_id,5:user_name]
        debtors_lines = []
        for s in shares:
            is_paid = s[3]
            if is_paid:  # показываем только должников с не закрытым долгом
                continue
            debtor_name = s[5]
            debtor_amount = float(s[1])
            debtors_lines.append(f"   • {debtor_name}: {debtor_amount:.2f}")

        if debtors_lines:
            history_text += "   Список должников:\n" + "\n".join(debtors_lines) + "\n"

        history_text += "\n"

    await update.message.reply_text(history_text)

async def handle_unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user

    if user.id in user_states:
        await handle_payment_input(update, context)
        return
    else:
        if update.message.chat.type == "private":
            await update.message.reply_text("Выберите действие из меню ниже:", reply_markup=get_main_keyboard())

async def get_payment_from_photo(update, context):
    photo = update.message.photo[-1]
    file_id = photo.file_id
    file = await context.bot.get_file(file_id)
    file_url = file.file_path
    description = 'Данные из чека'
    amount = ocr.get_total_by_url(file_url)
    return description, amount

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if update.message.reply_to_message:
        return
    state = user_states.get(user.id)
    photos = update.message.photo
    if state == "waiting_title" and photos:
        wait_message = await update.message.reply_text(
            f"Обрабатываю изображение..."
        )
        description, amount = await get_payment_from_photo(update, context)
        await context.bot.deleteMessage(message_id=wait_message.message_id, chat_id=update.message.chat_id)
        if amount is not None:
            payment = {
                'description': description,
                'user_id': user.id,
                'created_by': user.first_name,
                'chat_id': update.message.chat.id,
                'timestamp': datetime.now().strftime("%d.%m.%Y %H:%M"),
                'amount': float(amount),
            }
            context.user_data['pending_payment'] = payment
            del user_states[user.id]
            await update.message.reply_text(
                f"Проверьте данные:\n\nНазвание: {payment['description']}\nСумма: {payment['amount']:.2f} руб.\nСоздал: {payment['created_by']}\n\nПодтвердить создание платежа?",
                reply_markup=get_confirmation_keyboard()
            )
        else:
            await update.message.reply_text("Не удалось распознать данные из чека, напишите текстом")

# =========================
# OPTIMIZER
# =========================

async def optimize_debts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запустить оптимизацию долгов: получить план переводов, отправить и закрепить сообщение с упоминаниями, затем пометить старые долги как оплаченные."""
    if update.effective_chat.type == 'private':
        await update.message.reply_text('Оптимизация долгов доступна только в групповых чатах.')
        return

    chat_id = update.effective_chat.id
    await update.message.reply_text('Формирую план переводов...')

    try:
        transfers = debts_optimizer.optimize_transfers_with_allocations('expenses.db')
    except Exception as e:
        logger.exception('Ошибка при запуске оптимизатора: %s', e)
        await update.message.reply_text('Ошибка при формировании плана переводов. Смотрите логи.')
        return

    if not transfers:
        await update.message.reply_text('Нет активных задолженностей для оптимизации.')
        return

    by_currency = {}
    involved = set()
    for t in transfers:
        cur = t.get('currency', 'RUB')
        by_currency.setdefault(cur, []).append(t)
        involved.add(t['from'])
        involved.add(t['to'])

    try:
        users = dict(debts_optimizer.get_all_users('expenses.db'))
    except Exception:
        users = {}

    lines = ['План переводов (оптимизация):']
    currency_labels = {'RUB': 'RUB ₽', 'USD': 'USD $', 'EUR': 'EUR €'}

    for cur in sorted(by_currency.keys()):
        label = currency_labels.get(cur, cur)
        lines.append(f'=== {label} ===')
        for t in by_currency[cur]:
            name_from = html.escape(users.get(t['from'], str(t['from'])))
            name_to = html.escape(users.get(t['to'], str(t['to'])))
            lines.append(f"{name_from} -> {name_to}: {t['amount']:.2f} {cur}")

    mentions = []
    for uid in sorted(involved):
        pname = html.escape(users.get(uid, str(uid)))
        mentions.append(f'<a href="tg://user?id={uid}">{pname}</a>')

    full_text = "\n".join(lines)
    if mentions:
        full_text += "\n\nУчастники: " + ", ".join(mentions)

    try:
        sent = await context.bot.send_message(chat_id=chat_id, text=full_text, parse_mode='HTML', disable_web_page_preview=True)
    except Exception as e:
        logger.exception('Не удалось отправить сообщение с планом: %s', e)
        await update.message.reply_text('Не удалось отправить план переводов. Смотрите логи.')
        return

    try:
        await context.bot.pin_chat_message(chat_id=chat_id, message_id=sent.message_id)
    except Exception as e:
        logger.warning('Не удалось закрепить сообщение: %s', e)

    try:
        debts_optimizer.mark_allocations_paid('expenses.db', transfers)
    except Exception as e:
        logger.exception('Ошибка при пометке аллокаций как оплаченных: %s', e)
        await update.message.reply_text('План сформирован, но не удалось пометить задействованные доли как оплаченные. Смотрите логи.')
        return

    await update.message.reply_text('Задействованные долги помечены как оплаченные')

# =========================
# MAIN
# =========================

def main():
    init_database()
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("balance", show_balance))
    application.add_handler(CommandHandler("my_debt", show_my_debt))
    application.add_handler(CommandHandler("total_debt", show_total_debt))
    application.add_handler(CommandHandler("my_debt_by_category", show_my_debt_by_category))
    application.add_handler(CommandHandler("total_debt_by_category", show_total_debt_by_category))
    application.add_handler(CommandHandler("optimize", optimize_debts))
    application.add_handler(CommandHandler("optimize_debts", optimize_debts))
    application.add_handler(CommandHandler("history", show_payment_history))

    application.add_handler(MessageHandler(
        filters.Text([
            "Создать платеж", "Баланс", "Мой долг", "Общий долг",
            "История платежей", "Оптимизация долгов",
            "Мой долг по категориям", "Общий долг по категориям"
        ]),
        handle_main_buttons
    ))

    application.add_handler(CallbackQueryHandler(button_callback))
    # порядок важен: ответ на сообщение -> попытка записать долю
    application.add_handler(MessageHandler(filters.TEXT & filters.REPLY, handle_reply_message))
    # ввод при создании платежа
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.REPLY, handle_payment_input))
    # "непонятные" личные сообщения
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown_message))
    # фото для чеков
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_photo))

    application.run_polling()

if __name__ == '__main__':
    main()