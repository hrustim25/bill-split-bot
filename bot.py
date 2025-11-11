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
import ocr

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
                                                          currency TEXT NOT NULL DEFAULT 'RUB',
                                                          event_id INTEGER NOT NULL,
                                                          name TEXT NOT NULL,
                                                          paid_date TEXT,
                                                          user_id INTEGER NOT NULL,
                                                          message_id INTEGER NOT NULL,
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

def get_currency_keyboard():
    keyboard = [
        [InlineKeyboardButton("RUB ₽", callback_data="currency_RUB")],
        [InlineKeyboardButton("USD $", callback_data="currency_USD")],
        [InlineKeyboardButton("EUR €", callback_data="currency_EUR")]
    ]
    return InlineKeyboardMarkup(keyboard)


def save_payment_to_db(payment_data, message_id=None):
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()

    cursor.execute('''
                   INSERT INTO expense (event_id, name, user_id, paid_date, amount, currency, message_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ''', (
                       1,
                       payment_data['description'],
                       payment_data['user_id'],
                       payment_data['timestamp'],
                       payment_data['amount'],
                       payment_data.get('currency', 'RUB'),
                       message_id
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

    welcome_text = f"""Привет!

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


async def handle_reply_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.reply_to_message:
        try:
            reply_text = update.message.reply_to_message.text
            if "Платеж создан!" in reply_text:
                share_amount = float(update.message.text)

                if share_amount <= 0:
                    await update.message.reply_text("Сумма долга должна быть больше 0. Попробуйте ещё раз.")
                    return

                user = update.message.from_user

                conn = sqlite3.connect('expenses.db')
                cursor = conn.cursor()

                cursor.execute('SELECT id, currency FROM expense ORDER BY id DESC LIMIT 1')
                last_payment = cursor.fetchone()

                if last_payment:
                    payment_id, currency = last_payment
                    save_share_to_db(payment_id, user.id, user.first_name, share_amount)
                    await update.message.reply_text(f"Записан ваш долг: {share_amount} {currency}")
                else:
                    await update.message.reply_text("Не удалось найти платеж")

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
                            LEFT JOIN expense_participant ep ON e.id = ep.expense_id
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

    await update.message.reply_text(balance_text)

async def show_my_debt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()

    cursor.execute('''
                   SELECT e.name, ep.amount, e.currency
                   FROM expense_participant ep
                            JOIN expense e ON ep.expense_id = e.id
                   WHERE ep.user_id = ? AND ep.is_paid = 0
                   ''', (user.id,))
    debts = cursor.fetchall()
    conn.close()

    if debts:
        debt_text = f"Ваши долги, {user.first_name}:\n\n"
        total_by_currency = {}
        for name, amount, currency in debts:
            debt_text += f"{name}: {amount} {currency}\n"
            total_by_currency[currency] = total_by_currency.get(currency, 0) + amount
        debt_text += "\nОбщие суммы:\n"
        for cur, total in total_by_currency.items():
            debt_text += f"{cur}: {total}\n"
    else:
        debt_text = "У вас нет долгов"

    await update.message.reply_text(debt_text)

async def show_total_debt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()

    cursor.execute('''
                   SELECT u.name, SUM(ep.amount) as total_debt, e.currency
                   FROM expense_participant ep
                            JOIN user u ON ep.user_id = u.id
                            JOIN expense e ON ep.expense_id = e.id
                   WHERE ep.is_paid = 0
                   GROUP BY u.id, u.name, e.currency
                   ''')

    debts = cursor.fetchall()
    conn.close()

    if debts:
        debt_text = "Общие долги:\n\n"
        for name, total, currency in debts:
            debt_text += f"{name}: {total} {currency}\n"
    else:
        debt_text = "Нет активных долгов"

    await update.message.reply_text(debt_text)


async def show_payment_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payments = get_payments_from_db()
    if not payments:
        await update.message.reply_text("История платежей пуста")
        return

    history_text = "Последние платежи:\n\n"
    for p in payments:
        history_text += f"• {p[3]} | {p[5]} | {p[6]}\n   {p[1]} {p[2]} | {p[7]}\n\n"
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
    if state == "waiting_title" and photos and len(photos) > 1:
        description, amount = await get_payment_from_photo(update, context)
        if amount is not None:
            payment = {
                'description': description,
                'user_id': user.id,
                'created_by': user.first_name,
                'chat_id': update.message.chat.id,
                'timestamp': datetime.now().strftime("%d.%m.%Y %H:%M"),
                'amount': amount,
            }
            context.user_data['pending_payment'] = payment
            del user_states[user.id]
            await update.message.reply_text(
                f"Проверьте данные:\n\nНазвание: {payment['description']}\nСумма: {payment['amount']} руб.\nСоздал: {payment['created_by']}\n\nПодтвердить создание платежа?",
                reply_markup=get_confirmation_keyboard()
            )
        else:
            await update.message.reply_text(f"Не удалось распознать данные из чека, напишите текстом")


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
    application.add_handler(MessageHandler(filters.TEXT & filters.REPLY, handle_reply_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_payment_input))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown_message))

    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_photo))
    application.run_polling()

if __name__ == '__main__':
    main()
