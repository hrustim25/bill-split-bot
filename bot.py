import config
import logging
import sqlite3
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

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = config.BOT_TOKEN

user_states = {}

def init_database():
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS event (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS expense (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount REAL NOT NULL,
            event_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            paid_date TEXT,
            user_id INTEGER NOT NULL,
            FOREIGN KEY (event_id) REFERENCES event (id),
            FOREIGN KEY (user_id) REFERENCES user (id)
        )
    ''')

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

    cursor.execute('SELECT id FROM event WHERE id = 1')
    if not cursor.fetchone():
        cursor.execute('INSERT INTO event (id, name) VALUES (1, "Основное мероприятие")')

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

def get_main_keyboard():
    keyboard = [
        [KeyboardButton("Создать платеж"), KeyboardButton("Баланс")],
        [KeyboardButton("Мой долг"), KeyboardButton("Общий долг")],
        [KeyboardButton("История платежей")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_payment_types_keyboard():
    keyboard = [
        [InlineKeyboardButton("Еда", callback_data="type_food")],
        [InlineKeyboardButton("Транспорт", callback_data="type_transport")],
        [InlineKeyboardButton("Жилье", callback_data="type_accommodation")],
        [InlineKeyboardButton("Развлечения", callback_data="type_entertainment")],
        [InlineKeyboardButton("Прочее", callback_data="type_other")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_confirmation_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("Подтвердить", callback_data="confirm_payment"),
            InlineKeyboardButton("Отменить", callback_data="cancel_payment")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def save_payment_to_db(payment_data):
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()

    cursor.execute('''
        INSERT INTO expense (event_id, name, user_id, paid_date, amount)
        VALUES (?, ?, ?, ?, ?)
    ''', (
        1,
        payment_data['description'],
        payment_data['user_id'],
        payment_data['timestamp'],
        payment_data['amount']
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
        SELECT e.*, u.name
        FROM expense e
        JOIN user u ON e.user_id = u.id
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    get_or_create_user(user.id, user.first_name)

    if user.id in user_states:
        del user_states[user.id]

    welcome_text = f"""Привет, {user.first_name}!

Я бот для учета совместных расходов.

Выберите действие ниже"""

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

async def create_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_states[user.id] = "waiting_payment"

    if update.message.chat.type == "private":
        await update.message.reply_text(
            "Создание платежей работает только в групповых чатах",
            reply_markup=get_main_keyboard()
        )
        del user_states[user.id]
        return

    await update.message.reply_text(
        "Введите данные платежа в формате: название; сумма\n\nПример: Обед в кафе; 1500\nПример: Билеты на поезд; 3200"
    )

async def handle_payment_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user

    if user.id not in user_states or user_states[user.id] != "waiting_payment":
        if update.message.chat.type == "private":
            await handle_main_buttons(update, context)
        return

    try:
        text = update.message.text

        if text in ["Создать платеж", "Баланс", "Мой долг", "Общий долг", "История платежей"]:
            if update.message.chat.type == "private":
                await handle_main_buttons(update, context)
            return

        if ';' not in text:
            await update.message.reply_text("Неверный формат. Используйте: название; сумма\nПопробуйте еще раз:")
            return

        description, amount_str = text.split(';', 1)
        description = description.strip()
        amount = float(amount_str.strip())

        if amount <= 0:
            await update.message.reply_text("Сумма должна быть больше 0")
            return

        context.user_data['pending_payment'] = {
            'description': description,
            'amount': amount,
            'created_by': user.first_name,
            'user_id': user.id,
            'chat_id': update.message.chat.id,
            'timestamp': datetime.now().strftime("%d.%m.%Y %H:%M")
        }

        del user_states[user.id]

        await update.message.reply_text(
            f"Данные платежа:\n\nНазвание: {description}\nСумма: {amount} руб.\nСоздал: {user.first_name}\n\nПодтвердите создание платежа:",
            reply_markup=get_confirmation_keyboard()
        )

    except ValueError:
        await update.message.reply_text("Ошибка! Сумма должна быть числом. Попробуйте еще раз:")

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
                    reply_markup=get_payment_types_keyboard()
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

    elif callback_data.startswith('type_'):
        payment_type = callback_data.replace('type_', '')
        type_names = {
            'food': 'Еда',
            'transport': 'Транспорт',
            'accommodation': 'Жилье',
            'entertainment': 'Развлечения',
            'other': 'Прочее'
        }

        if 'pending_payment' in context.user_data:
            payment_data = context.user_data['pending_payment']
            if payment_data.get('user_id') == user.id:
                payment_data['type'] = type_names.get(payment_type, 'Прочее')
                payment_data['message_id'] = query.message.message_id

                payment_id = save_payment_to_db(payment_data)

                response_text = f"Платеж создан!\n\nНазвание: {payment_data['description']}\nСумма: {payment_data['amount']} руб.\nКатегория: {payment_data['type']}\nСоздал: {payment_data['created_by']}\nВремя: {payment_data['timestamp']}\n\nУчастники могут ответить на это сообщение числом - их долю в платеже."

                await query.edit_message_text(response_text)

                del context.user_data['pending_payment']
            else:
                await query.edit_message_text("Этот платеж принадлежит другому пользователю")

async def handle_reply_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.reply_to_message:
        try:
            reply_text = update.message.reply_to_message.text
            if "Платеж создан!" in reply_text:
                share_amount = float(update.message.text)
                user = update.message.from_user

                conn = sqlite3.connect('expenses.db')
                cursor = conn.cursor()

                cursor.execute('SELECT id FROM expense ORDER BY id DESC LIMIT 1')
                last_payment = cursor.fetchone()
                conn.close()

                if last_payment:
                    payment_id = last_payment[0]
                    save_share_to_db(payment_id, user.id, user.first_name, share_amount)
                    await update.message.reply_text(f"Записан ваш долг: {share_amount} руб.")
                else:
                    await update.message.reply_text("Не удалось найти платеж")

        except ValueError:
            await update.message.reply_text("Ошибка! Введите число - вашу долю в платеже")

async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user

    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()

    cursor.execute('''
        SELECT
            SUM(CASE WHEN e.user_id = ? THEN e.amount ELSE 0 END) as paid_total,
            SUM(CASE WHEN ep.user_id = ? THEN ep.amount ELSE 0 END) as debt_total
        FROM expense e
        LEFT JOIN expense_participant ep ON e.id = ep.expense_id
        WHERE e.event_id = 1
    ''', (user.id, user.id))

    result = cursor.fetchone()
    conn.close()

    if result and (result[0] or result[1]):
        paid_total = result[0] or 0
        debt_total = result[1] or 0
        balance = paid_total - debt_total

        if balance > 0:
            balance_text = f"Ваш баланс: +{balance:.2f} руб.\n\nВам должны: {balance:.2f} руб."
        elif balance < 0:
            balance_text = f"Ваш баланс: {balance:.2f} руб.\n\nВы должны: {abs(balance):.2f} руб."
        else:
            balance_text = "Ваш баланс: 0 руб.\n\nВсе расчеты сведены."
    else:
        balance_text = "Нет данных о платежах"

    await update.message.reply_text(balance_text)

async def show_my_debt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user

    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()

    cursor.execute('''
        SELECT e.name, ep.amount
        FROM expense_participant ep
        JOIN expense e ON ep.expense_id = e.id
        WHERE ep.user_id = ? AND ep.is_paid = 0
    ''', (user.id,))

    debts = cursor.fetchall()
    conn.close()

    if debts:
        debt_text = f"Ваши долги, {user.first_name}:\n\n"
        total = 0
        for debt in debts:
            debt_text += f"{debt[0]}: {debt[1]} руб.\n"
            total += debt[1]
        debt_text += f"\nОбщая сумма: {total} руб."
    else:
        debt_text = "У вас нет долгов"

    await update.message.reply_text(debt_text)

async def show_total_debt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()

    cursor.execute('''
        SELECT u.name, SUM(ep.amount) as total_debt
        FROM expense_participant ep
        JOIN user u ON ep.user_id = u.id
        WHERE ep.is_paid = 0
        GROUP BY u.id, u.name
    ''')

    debts = cursor.fetchall()
    conn.close()

    if debts:
        debt_text = "Общие долги:\n\n"
        for debt in debts:
            debt_text += f"{debt[0]}: {debt[1]} руб.\n"
    else:
        debt_text = "Нет активных долгов"

    await update.message.reply_text(debt_text)

async def show_payment_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payments = get_payments_from_db()

    if not payments:
        history_text = "История платежей пуста"
    else:
        history_text = "Последние платежи:\n\n"
        for payment in payments:
            history_text += f"• {payment[3]}\n   {payment[1]} руб. | {payment[4]}\n   {payment[6]} | {payment[5]}\n\n"

    await update.message.reply_text(history_text)

async def handle_unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user

    if user.id in user_states and user_states[user.id] == "waiting_payment":
        await handle_payment_input(update, context)
    else:
        if update.message.chat.type == "private":
            await update.message.reply_text("Выберите действие из меню ниже:", reply_markup=get_main_keyboard())

def main():
    init_database()
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("balance", show_balance))
    application.add_handler(CommandHandler("my_debt", show_my_debt))
    application.add_handler(CommandHandler("total_debt", show_total_debt))
    application.add_handler(CommandHandler("history", show_payment_history))

    application.add_handler(MessageHandler(
        filters.Text(["Создать платеж", "Баланс", "Мой долг", "Общий долг", "История платежей"]),
        handle_main_buttons
    ))

    application.add_handler(CallbackQueryHandler(button_callback))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown_message))

    application.add_handler(MessageHandler(filters.TEXT & filters.REPLY, handle_reply_message))

    application.run_polling()

if __name__ == '__main__':
    main()
