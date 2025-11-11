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
    keyboard = [[
        InlineKeyboardButton("Подтвердить", callback_data="confirm_payment"),
        InlineKeyboardButton("Отменить", callback_data="cancel_payment")
    ]]
    return InlineKeyboardMarkup(keyboard)

def save_payment_to_db(payment_data, message_id=None):
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()
    cursor.execute('''
                   INSERT INTO expense (event_id, name, user_id, paid_date, amount, message_id)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ''', (1, payment_data['description'], payment_data['user_id'], payment_data['timestamp'], payment_data['amount'], message_id))
    payment_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return payment_id

def save_share_to_db(payment_id, user_id, user_name, amount):
    get_or_create_user(user_id, user_name)
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()
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
    user_states.pop(user.id, None)
    welcome_text = f"Привет, {user.first_name}!\n\nЯ бот для учета совместных расходов.\nВыберите действие ниже"
    await update.message.reply_text(welcome_text, reply_markup=get_main_keyboard())

async def handle_main_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    text = update.message.text
    user_states.pop(user.id, None)
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
        await update.message.reply_text("Создание платежей работает только в групповых чатах", reply_markup=get_main_keyboard())
        return
    user_states[user.id] = "waiting_title"
    await update.message.reply_text("Введите название платежа:")

async def handle_payment_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    text = update.message.text
    if update.message.reply_to_message:
        return
    state = user_states.get(user.id)
    if state == "waiting_title":
        context.user_data['pending_payment'] = {
            'description': text.strip(),
            'user_id': user.id,
            'created_by': user.first_name,
            'chat_id': update.message.chat.id,
            'timestamp': datetime.now().strftime("%d.%m.%Y %H:%M")
        }
        user_states[user.id] = "waiting_amount"
        await update.message.reply_text("Введите сумму:")
    elif state == "waiting_amount":
        try:
            amount = float(text.replace(",", ".").strip())
            if amount <= 0:
                await update.message.reply_text("Сумма должна быть больше 0. Попробуйте снова:")
                return
        except ValueError:
            await update.message.reply_text("Введите корректное число:")
            return
        context.user_data['pending_payment']['amount'] = amount
        del user_states[user.id]
        payment = context.user_data['pending_payment']
        await update.message.reply_text(
            f"Проверьте данные:\n\nНазвание: {payment['description']}\nСумма: {payment['amount']} руб.\nСоздал: {payment['created_by']}\n\nПодтвердить создание платежа?",
            reply_markup=get_confirmation_keyboard()
        )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    data = query.data
    if data == "confirm_payment":
        payment_data = context.user_data.get('pending_payment')
        if payment_data and payment_data.get('user_id') == user.id:
            await query.edit_message_text("Платеж подтвержден! Выберите категорию:", reply_markup=get_payment_types_keyboard())
        else:
            await query.edit_message_text("Данные платежа не найдены или платеж принадлежит другому пользователю")
    elif data == "cancel_payment":
        payment_data = context.user_data.get('pending_payment')
        if payment_data and payment_data.get('user_id') == user.id:
            context.user_data.pop('pending_payment')
            await query.edit_message_text("Создание платежа отменено")
        else:
            await query.edit_message_text("Платеж принадлежит другому пользователю")
    elif data.startswith("type_"):
        payment_type = data.replace("type_", "")
        type_names = {'food': 'Еда', 'transport': 'Транспорт', 'accommodation': 'Жилье', 'entertainment': 'Развлечения', 'other': 'Прочее'}
        payment_data = context.user_data.get('pending_payment')
        if payment_data and payment_data.get('user_id') == user.id:
            payment_data['type'] = type_names.get(payment_type, 'Прочее')
            sent = await query.message.reply_text(
                f"Платеж создан!\n\nНазвание: {payment_data['description']}\nСумма: {payment_data['amount']} руб.\nКатегория: {payment_data['type']}\nСоздал: {payment_data['created_by']}\nВремя: {payment_data['timestamp']}\n\nУчастники могут ответить на это сообщение числом — их долю в платеже."
            )
            save_payment_to_db(payment_data, sent.message_id)
            context.user_data.pop('pending_payment')
            await query.edit_message_text("Платёж сохранён.")

async def handle_reply_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.reply_to_message:
        try:
            if "Платеж создан!" in update.message.reply_to_message.text:
                share_amount = float(update.message.text)
                if share_amount <= 0:
                    await update.message.reply_text("Сумма долга должна быть больше 0.")
                    return
                user = update.message.from_user
                conn = sqlite3.connect('expenses.db')
                cursor = conn.cursor()
                cursor.execute('SELECT id FROM expense ORDER BY id DESC LIMIT 1')
                last_payment = cursor.fetchone()
                conn.close()
                if last_payment:
                    save_share_to_db(last_payment[0], user.id, user.first_name, share_amount)
                    await update.message.reply_text(f"Записан ваш долг: {share_amount} руб.")
                else:
                    await update.message.reply_text("Не удалось найти платеж")
        except ValueError:
            await update.message.reply_text("Ошибка! Введите число.")

async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()
    cursor.execute('''
                   SELECT SUM(CASE WHEN e.user_id = ? THEN e.amount ELSE 0 END) as paid_total,
                          SUM(CASE WHEN ep.user_id = ? THEN ep.amount ELSE 0 END) as debt_total
                   FROM expense e
                            LEFT JOIN expense_participant ep ON e.id = ep.expense_id
                   WHERE e.event_id = 1
                   ''', (user.id, user.id))
    result = cursor.fetchone()
    conn.close()
    paid_total = result[0] or 0
    debt_total = result[1] or 0
    balance = paid_total - debt_total
    if balance > 0:
        balance_text = f"Ваш баланс: +{balance:.2f} руб.\nВам должны: {balance:.2f} руб."
    elif balance < 0:
        balance_text = f"Ваш баланс: {balance:.2f} руб.\nВы должны: {abs(balance):.2f} руб."
    else:
        balance_text = "Ваш баланс: 0 руб."
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
        debt_text = f"Ваши долги, {user.first_name}:\n"
        total = sum(d[1] for d in debts)
        for d in debts:
            debt_text += f"{d[0]}: {d[1]} руб.\n"
        debt_text += f"Общая сумма: {total} руб."
    else:
        debt_text = "У вас нет долгов"
    await update.message.reply_text(debt_text)

async def show_total_debt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect('expenses.db')
    cursor = conn.cursor()
    cursor.execute('''
                   SELECT u.name, SUM(ep.amount)
                   FROM expense_participant ep
                            JOIN user u ON ep.user_id = u.id
                   WHERE ep.is_paid = 0
                   GROUP BY u.id, u.name
                   ''')
    debts = cursor.fetchall()
    conn.close()
    if debts:
        debt_text = "Общие долги:\n"
        for d in debts:
            debt_text += f"{d[0]}: {d[1]} руб.\n"
    else:
        debt_text = "Нет активных долгов"
    await update.message.reply_text(debt_text)

async def show_payment_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payments = get_payments_from_db()
    if payments:
        history_text = "Последние платежи:\n"
        for p in payments:
            history_text += f"• {p[3]}\n  {p[1]} руб. | {p[4]}\n  {p[6]} | {p[5]}\n"
    else:
        history_text = "История платежей пуста"
    await update.message.reply_text(history_text)

async def handle_unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if user.id in user_states:
        await handle_payment_input(update, context)
    elif update.message.chat.type == "private":
        await update.message.reply_text("Выберите действие из меню ниже:", reply_markup=get_main_keyboard())

def main():
    init_database()
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("balance", show_balance))
    application.add_handler(CommandHandler("my_debt", show_my_debt))
    application.add_handler(CommandHandler("total_debt", show_total_debt))
    application.add_handler(CommandHandler("history", show_payment_history))
    application.add_handler(MessageHandler(filters.Text(["Создать платеж","Баланс","Мой долг","Общий долг","История платежей"]), handle_main_buttons))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & filters.REPLY, handle_reply_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_payment_input))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown_message))
    application.run_polling()

if __name__ == "__main__":
    main()
