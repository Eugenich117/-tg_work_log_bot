import logging
from datetime import datetime, timedelta
import sqlite3
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ConversationHandler

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Определение состояний для ConversationHandler
TIME_IN, TIME_OUT, REPORT_PERIOD = range(3)


# Инициализация базы данных
def init_db():
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS records (
            user_id INTEGER,
            date TEXT,
            time_in TEXT,
            time_out TEXT,
            hours REAL
        )
    ''')
    conn.commit()
    conn.close()


# Добавление записи о входе
def add_time_in(user_id, date, time_in):
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('INSERT INTO records (user_id, date, time_in) VALUES (?, ?, ?)',
                   (user_id, date, time_in))
    conn.commit()
    conn.close()


# Обновление записи о выходе и расчет часов
def add_time_out(user_id, date, time_out):
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('SELECT time_in FROM records WHERE user_id=? AND date=? AND time_out IS NULL',
                   (user_id, date))
    result = cursor.fetchone()

    if result:
        time_in = datetime.strptime(result[0], '%H:%M')
        time_out_dt = datetime.strptime(time_out, '%H:%M')
        hours = (time_out_dt - time_in).total_seconds() / 3600

        cursor.execute('''UPDATE records SET time_out=?, hours=?
                       WHERE user_id=? AND date=? AND time_out IS NULL''',
                       (time_out, hours, user_id, date))
    conn.commit()
    conn.close()


# Генерация отчетов за период
def generate_report(user_id, period):
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()

    if period == 'today':
        current_date = datetime.now().strftime('%Y-%m-%d')
        cursor.execute('''SELECT SUM(hours) FROM records 
                       WHERE user_id=? AND date=?''', (user_id, current_date))
    elif period == 'week':
        start_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        cursor.execute('''SELECT SUM(hours) FROM records 
                       WHERE user_id=? AND date >= ?''', (user_id, start_date))
    elif period == 'month':
        start_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
        cursor.execute('''SELECT SUM(hours) FROM records 
                       WHERE user_id=? AND date >= ?''', (user_id, start_date))
    else:  # year
        start_date = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
        cursor.execute('''SELECT SUM(hours) FROM records 
                       WHERE user_id=? AND date >= ?''', (user_id, start_date))

    result = cursor.fetchone()
    conn.close()

    return result[0] or 0


# Получение деталей за сегодня
def get_today_details(user_id):
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    current_date = datetime.now().strftime('%Y-%m-%d')

    cursor.execute('''SELECT time_in, time_out, hours FROM records 
                   WHERE user_id=? AND date=? ORDER BY time_in''', (user_id, current_date))
    records = cursor.fetchall()
    conn.close()

    return records


# Команда старт
async def start(update, context):
    keyboard = [['Вход', 'Выход'], ['Отчет']]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        'Выберите действие:',
        reply_markup=reply_markup
    )


# Обработчик кнопки "Вход"
async def time_in(update, context):
    await update.message.reply_text(
        'Введите время входа в формате ЧЧ:ММ (например, 09:00):',
        reply_markup=ReplyKeyboardRemove()
    )
    return TIME_IN


# Обработчик кнопки "Выход"
async def time_out(update, context):
    await update.message.reply_text(
        'Введите время выхода в формате ЧЧ:ММ (например, 18:00):',
        reply_markup=ReplyKeyboardRemove()
    )
    return TIME_OUT


# Сохранение времени входа
async def save_time_in(update, context):
    user_id = update.message.from_user.id
    current_date = datetime.now().strftime('%Y-%m-%d')
    time_in_str = update.message.text

    try:
        datetime.strptime(time_in_str, '%H:%M')
        add_time_in(user_id, current_date, time_in_str)
        await update.message.reply_text('Время входа сохранено!', reply_markup=main_keyboard())
    except ValueError:
        await update.message.reply_text('Неверный формат времени! Используйте ЧЧ:ММ')
        return TIME_IN

    return ConversationHandler.END


# Сохранение времени выхода
async def save_time_out(update, context):
    user_id = update.message.from_user.id
    current_date = datetime.now().strftime('%Y-%m-%d')
    time_out_str = update.message.text

    try:
        datetime.strptime(time_out_str, '%H:%M')
        add_time_out(user_id, current_date, time_out_str)
        await update.message.reply_text('Время выхода сохранено!', reply_markup=main_keyboard())
    except ValueError:
        await update.message.reply_text('Неверный формат времени! Используйте ЧЧ:ММ')
        return TIME_OUT

    return ConversationHandler.END


# Главное меню
def main_keyboard():
    keyboard = [['Вход', 'Выход'], ['Отчет']]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# Меню отчетов
async def report_menu(update, context):
    keyboard = [['Сегодня', 'Неделя', 'Месяц'], ['Год', 'Назад']]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        'Выберите период для отчета:',
        reply_markup=reply_markup
    )


# Генерация отчета
async def generate_report_handler(update, context):
    user_id = update.message.from_user.id
    period_text = update.message.text.lower()

    if period_text == 'назад':
        await update.message.reply_text('Главное меню', reply_markup=main_keyboard())
        return

    period_map = {
        'сегодня': 'today',
        'неделя': 'week',
        'месяц': 'month',
        'год': 'year'
    }

    if period_text in period_map:
        period = period_map[period_text]
        total_hours = generate_report(user_id, period)

        if period == 'today':
            # Для отчета за сегодня показываем детали
            details = get_today_details(user_id)
            if details:
                message = f"Отчет за сегодня ({datetime.now().strftime('%d.%m.%Y')}):\n\n"
                for i, record in enumerate(details, 1):
                    time_in, time_out, hours = record
                    if time_out and hours:
                        message += f"{i}. ⏰ {time_in} - {time_out} | {hours:.2f} ч.\n"
                    else:
                        message += f"{i}. ⏰ {time_in} - --:-- | незавершенный вход\n"
                message += f"\nВсего за день: {total_hours:.2f} часов"
            else:
                message = "За сегодня нет записей о рабочем времени."
        else:
            message = f'Отработано за {period_text}: {total_hours:.2f} часов'

        await update.message.reply_text(message, reply_markup=main_keyboard())
    else:
        await update.message.reply_text('Неверный период отчета')


# Отмена диалога
async def cancel(update, context):
    await update.message.reply_text('Отменено', reply_markup=main_keyboard())
    return ConversationHandler.END


def main():
    init_db()

    # Замените "YOUR_BOT_TOKEN" на токен вашего бота
    application = Application.builder().token("8412675372:AAHadq57Jw0r2GWl1VkhZwT8xZs9j_GJWpk").build()

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex('^Вход$'), time_in),
            MessageHandler(filters.Regex('^Выход$'), time_out)
        ],
        states={
            TIME_IN: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_time_in)],
            TIME_OUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_time_out)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)
    application.add_handler(MessageHandler(filters.Regex('^Отчет$'), report_menu))
    application.add_handler(
        MessageHandler(filters.Regex('^(Сегодня|Неделя|Месяц|Год|Назад)$'), generate_report_handler))

    application.run_polling()


if __name__ == '__main__':
    main()