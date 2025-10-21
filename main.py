import logging
from datetime import datetime, timedelta
import sqlite3
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ConversationHandler

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Определение состояний для ConversationHandler
TIME_IN, TIME_OUT, LUNCH_START, LUNCH_END = range(4)


# Чтение токена из файла
def get_token():
    try:
        with open('.token', 'r') as file:
            return file.read().strip()
    except FileNotFoundError:
        logger.error("Файл .token не найден! Создайте файл .token с токеном бота.")
        return None


# Инициализация базы данных
def init_db():
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            date TEXT,
            time_in TEXT,
            time_out TEXT,
            lunch_start TEXT,
            lunch_end TEXT,
            total_hours REAL
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
    cursor.execute(
        'SELECT id, time_in, lunch_start, lunch_end FROM records WHERE user_id=? AND date=? AND time_out IS NULL',
        (user_id, date))
    result = cursor.fetchone()

    if result:
        record_id, time_in, lunch_start, lunch_end = result
        total_hours = calculate_work_hours(time_in, time_out, lunch_start, lunch_end)

        cursor.execute('''UPDATE records SET time_out=?, total_hours=?
                       WHERE id=?''', (time_out, total_hours, record_id))
    conn.commit()
    conn.close()


# Добавление времени начала обеда
def add_lunch_start(user_id, date, lunch_start):
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('SELECT id, time_in FROM records WHERE user_id=? AND date=? AND time_out IS NULL',
                   (user_id, date))
    result = cursor.fetchone()

    if result:
        record_id, time_in = result
        cursor.execute('UPDATE records SET lunch_start=? WHERE id=?', (lunch_start, record_id))
    conn.commit()
    conn.close()


# Добавление времени конца обеда
def add_lunch_end(user_id, date, lunch_end):
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    cursor.execute('SELECT id, time_in, lunch_start FROM records WHERE user_id=? AND date=? AND time_out IS NULL',
                   (user_id, date))
    result = cursor.fetchone()

    if result:
        record_id, time_in, lunch_start = result
        cursor.execute('UPDATE records SET lunch_end=? WHERE id=?', (lunch_end, record_id))
    conn.commit()
    conn.close()


# Расчет рабочих часов с учетом обеда
def calculate_work_hours(time_in, time_out, lunch_start=None, lunch_end=None):
    try:
        # Преобразуем время в объекты datetime
        time_in_dt = datetime.strptime(time_in, '%H:%M')
        time_out_dt = datetime.strptime(time_out, '%H:%M')

        # Общее время между входом и выходом
        total_time = (time_out_dt - time_in_dt).total_seconds() / 3600

        # Вычитаем время обеда, если оно указано
        if lunch_start and lunch_end:
            lunch_start_dt = datetime.strptime(lunch_start, '%H:%M')
            lunch_end_dt = datetime.strptime(lunch_end, '%H:%M')
            lunch_duration = (lunch_end_dt - lunch_start_dt).total_seconds() / 3600
            total_time -= lunch_duration

        return max(0, total_time)  # Не допускаем отрицательные значения
    except ValueError:
        return 0


# Генерация отчетов за период
def generate_report(user_id, period):
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()

    if period == 'today':
        current_date = datetime.now().strftime('%Y-%m-%d')
        cursor.execute('''SELECT SUM(total_hours) FROM records 
                       WHERE user_id=? AND date=?''', (user_id, current_date))
    elif period == 'week':
        start_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        cursor.execute('''SELECT SUM(total_hours) FROM records 
                       WHERE user_id=? AND date >= ?''', (user_id, start_date))
    elif period == 'month':
        start_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
        cursor.execute('''SELECT SUM(total_hours) FROM records 
                       WHERE user_id=? AND date >= ?''', (user_id, start_date))
    else:  # year
        start_date = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
        cursor.execute('''SELECT SUM(total_hours) FROM records 
                       WHERE user_id=? AND date >= ?''', (user_id, start_date))

    result = cursor.fetchone()
    conn.close()

    return result[0] or 0


# Получение деталей за сегодня
def get_today_details(user_id):
    conn = sqlite3.connect('timesheet.db')
    cursor = conn.cursor()
    current_date = datetime.now().strftime('%Y-%m-%d')

    cursor.execute('''SELECT time_in, time_out, lunch_start, lunch_end, total_hours FROM records 
                   WHERE user_id=? AND date=? ORDER BY time_in''', (user_id, current_date))
    records = cursor.fetchall()
    conn.close()

    return records


# Команда старт
async def start(update, context):
    keyboard = [['Вход', 'Выход', 'Обед'], ['Отчет']]
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


# Обработчик кнопки "Обед"
async def lunch(update, context):
    keyboard = [['Начало обеда', 'Конец обеда'], ['Назад']]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        'Выберите действие для обеда:',
        reply_markup=reply_markup
    )


# Обработчик начала обеда
async def lunch_start(update, context):
    await update.message.reply_text(
        'Введите время начала обеда в формате ЧЧ:ММ (например, 13:00):',
        reply_markup=ReplyKeyboardRemove()
    )
    return LUNCH_START


# Обработчик конца обеда
async def lunch_end(update, context):
    await update.message.reply_text(
        'Введите время конца обеда в формате ЧЧ:ММ (например, 14:00):',
        reply_markup=ReplyKeyboardRemove()
    )
    return LUNCH_END


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


# Сохранение времени начала обеда
async def save_lunch_start(update, context):
    user_id = update.message.from_user.id
    current_date = datetime.now().strftime('%Y-%m-%d')
    lunch_start_str = update.message.text

    try:
        datetime.strptime(lunch_start_str, '%H:%M')
        add_lunch_start(user_id, current_date, lunch_start_str)
        await update.message.reply_text('Время начала обеда сохранено!', reply_markup=main_keyboard())
    except ValueError:
        await update.message.reply_text('Неверный формат времени! Используйте ЧЧ:ММ')
        return LUNCH_START

    return ConversationHandler.END


# Сохранение времени конца обеда
async def save_lunch_end(update, context):
    user_id = update.message.from_user.id
    current_date = datetime.now().strftime('%Y-%m-%d')
    lunch_end_str = update.message.text

    try:
        datetime.strptime(lunch_end_str, '%H:%M')
        add_lunch_end(user_id, current_date, lunch_end_str)
        await update.message.reply_text('Время конца обеда сохранено!', reply_markup=main_keyboard())
    except ValueError:
        await update.message.reply_text('Неверный формат времени! Используйте ЧЧ:ММ')
        return LUNCH_END

    return ConversationHandler.END


# Главное меню
def main_keyboard():
    keyboard = [['Вход', 'Выход', 'Обед'], ['Отчет']]
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
                    time_in, time_out, lunch_start, lunch_end, hours = record
                    if time_out and hours:
                        message += f"{i}. ⏰ {time_in} - {time_out}"
                        if lunch_start and lunch_end:
                            message += f" | 🍽 {lunch_start}-{lunch_end}"
                        message += f" | {hours:.2f} ч.\n"
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


# Обработчик кнопки "Назад" в меню обеда
async def lunch_back(update, context):
    await update.message.reply_text('Главное меню', reply_markup=main_keyboard())


# Отмена диалога
async def cancel(update, context):
    await update.message.reply_text('Отменено', reply_markup=main_keyboard())
    return ConversationHandler.END


def main():
    # Получаем токен из файла
    token = get_token()
    if not token:
        print("Не удалось загрузить токен бота. Убедитесь, что файл .token существует и содержит токен.")
        return

    init_db()

    application = Application.builder().token(token).build()

    # ConversationHandler для входа/выхода
    time_conv_handler = ConversationHandler(
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

    # ConversationHandler для обеда
    lunch_conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex('^Начало обеда$'), lunch_start),
            MessageHandler(filters.Regex('^Конец обеда$'), lunch_end)
        ],
        states={
            LUNCH_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_lunch_start)],
            LUNCH_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_lunch_end)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(time_conv_handler)
    application.add_handler(lunch_conv_handler)
    application.add_handler(MessageHandler(filters.Regex('^Обед$'), lunch))
    application.add_handler(MessageHandler(filters.Regex('^Назад$'), lunch_back))
    application.add_handler(MessageHandler(filters.Regex('^Отчет$'), report_menu))
    application.add_handler(
        MessageHandler(filters.Regex('^(Сегодня|Неделя|Месяц|Год|Назад)$'), generate_report_handler))

    application.run_polling()


if __name__ == '__main__':
    main()