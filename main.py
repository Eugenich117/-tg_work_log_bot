import logging
from datetime import datetime, timedelta
import sqlite3
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ConversationHandler
import asyncio
from pathlib import Path

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Определение состояний для ConversationHandler
(
    TIME_IN, TIME_OUT, LUNCH_START, LUNCH_END,
    ADD_RECORD_DATE, ADD_RECORD_TIME_IN, ADD_RECORD_TIME_OUT,
    ADD_RECORD_LUNCH_START, ADD_RECORD_LUNCH_END, ADD_RECORD_LUNCH_MINUTES
) = range(10)


# Чтение токена из файла
def get_token():
    base_dir = Path(__file__).resolve().parent
    token_file = base_dir / ".token.txt"
    try:
        token = token_file.read_text().strip()
        return token
    except Exception as e:
        logger.error(f"Ошибка чтения файла .token: {e}")
        return None


# Инициализация базы данных с оптимизацией
def init_db():
    conn = sqlite3.connect('timesheet.db', check_same_thread=False)
    conn.execute('PRAGMA journal_mode=WAL')
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
            lunch_minutes INTEGER,
            total_hours REAL
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_date ON records (user_id, date)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_user ON records (user_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_date ON records (date)')
    conn.commit()
    conn.close()


# Глобальное соединение с БД
_db_connection = None


def get_db_connection():
    global _db_connection
    if _db_connection is None:
        _db_connection = sqlite3.connect('timesheet.db', check_same_thread=False)
        _db_connection.execute('PRAGMA journal_mode=WAL')
    return _db_connection


# Расчет рабочих часов с учетом обеда (только если >4 часов)
def calculate_work_hours(time_in, time_out, lunch_start=None, lunch_end=None, lunch_minutes=None):
    try:
        time_in_dt = datetime.strptime(time_in, '%H:%M')
        time_out_dt = datetime.strptime(time_out, '%H:%M')

        # Общее время между входом и выходом
        total_time = (time_out_dt - time_in_dt).total_seconds() / 3600

        # Вычитаем время обеда только если рабочее время больше 4 часов
        if total_time > 4:
            if lunch_start and lunch_end:
                lunch_start_dt = datetime.strptime(lunch_start, '%H:%M')
                lunch_end_dt = datetime.strptime(lunch_end, '%H:%M')
                lunch_duration = (lunch_end_dt - lunch_start_dt).total_seconds() / 3600
                total_time -= lunch_duration
            elif lunch_minutes:
                total_time -= lunch_minutes / 60

        return max(0, round(total_time, 2))
    except ValueError:
        return 0


# Добавление полной записи
def add_complete_record(user_id, date, time_in, time_out, lunch_start=None, lunch_end=None, lunch_minutes=None):
    conn = get_db_connection()
    cursor = conn.cursor()

    total_hours = calculate_work_hours(time_in, time_out, lunch_start, lunch_end, lunch_minutes)

    cursor.execute('''INSERT INTO records 
                   (user_id, date, time_in, time_out, lunch_start, lunch_end, lunch_minutes, total_hours) 
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                   (user_id, date, time_in, time_out, lunch_start, lunch_end, lunch_minutes, total_hours))
    conn.commit()
    return total_hours


# Добавление записи о входе
def add_time_in(user_id, date, time_in):
    conn = get_db_connection()
    cursor = conn.cursor()

    # Проверяем, есть ли уже запись на эту дату
    cursor.execute('SELECT id FROM records WHERE user_id=? AND date=? AND time_out IS NULL',
                   (user_id, date))
    existing = cursor.fetchone()

    if existing:
        cursor.execute('UPDATE records SET time_in=? WHERE id=?', (time_in, existing[0]))
    else:
        cursor.execute('INSERT INTO records (user_id, date, time_in) VALUES (?, ?, ?)',
                       (user_id, date, time_in))
    conn.commit()


# Обновление записи о выходе и расчет часов
def add_time_out(user_id, date, time_out):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT id, time_in, lunch_start, lunch_end, lunch_minutes FROM records WHERE user_id=? AND date=? AND time_out IS NULL',
        (user_id, date))
    result = cursor.fetchone()

    if result:
        record_id, time_in, lunch_start, lunch_end, lunch_minutes = result
        total_hours = calculate_work_hours(time_in, time_out, lunch_start, lunch_end, lunch_minutes)

        cursor.execute('''UPDATE records SET time_out=?, total_hours=?
                       WHERE id=?''', (time_out, total_hours, record_id))
    conn.commit()


# Добавление времени начала обеда
def add_lunch_start(user_id, date, lunch_start):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT id, time_in FROM records WHERE user_id=? AND date=? AND time_out IS NULL',
                   (user_id, date))
    result = cursor.fetchone()

    if result:
        record_id, time_in = result
        cursor.execute('UPDATE records SET lunch_start=? WHERE id=?', (lunch_start, record_id))
    conn.commit()


# Добавление времени конца обеда
def add_lunch_end(user_id, date, lunch_end):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT id, time_in, lunch_start FROM records WHERE user_id=? AND date=? AND time_out IS NULL',
                   (user_id, date))
    result = cursor.fetchone()

    if result:
        record_id, time_in, lunch_start = result
        cursor.execute('UPDATE records SET lunch_end=? WHERE id=?', (lunch_end, record_id))
    conn.commit()


# Добавление минут обеда
def add_lunch_minutes(user_id, date, lunch_minutes):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT id, time_in FROM records WHERE user_id=? AND date=? AND time_out IS NULL',
                   (user_id, date))
    result = cursor.fetchone()

    if result:
        record_id, time_in = result
        cursor.execute('UPDATE records SET lunch_minutes=? WHERE id=?', (lunch_minutes, record_id))
    conn.commit()


# Генерация отчетов за период
def generate_report(user_id, period):
    conn = get_db_connection()
    cursor = conn.cursor()

    today = datetime.now().date()

    if period == 'today':
        current_date = today.strftime('%Y-%m-%d')
        cursor.execute('''SELECT SUM(total_hours) FROM records 
                       WHERE user_id=? AND date=?''', (user_id, current_date))
    elif period == 'week':
        # Начало недели (понедельник)
        start_of_week = today - timedelta(days=today.weekday())
        # Конец недели (воскресенье)
        end_of_week = start_of_week + timedelta(days=6)
        cursor.execute('''SELECT SUM(total_hours) FROM records 
                       WHERE user_id=? AND date BETWEEN ? AND ?''',
                       (user_id, start_of_week.strftime('%Y-%m-%d'), end_of_week.strftime('%Y-%m-%d')))
    elif period == 'month':
        # Начало месяца
        start_of_month = today.replace(day=1)
        # Конец месяца
        if today.month == 12:
            end_of_month = today.replace(year=today.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            end_of_month = today.replace(month=today.month + 1, day=1) - timedelta(days=1)
        cursor.execute('''SELECT SUM(total_hours) FROM records 
                       WHERE user_id=? AND date BETWEEN ? AND ?''',
                       (user_id, start_of_month.strftime('%Y-%m-%d'), end_of_month.strftime('%Y-%m-%d')))
    else:  # year
        start_date = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
        cursor.execute('''SELECT SUM(total_hours) FROM records 
                       WHERE user_id=? AND date >= ?''', (user_id, start_date))

    result = cursor.fetchone()
    return result[0] or 0


# Получение деталей за сегодня
def get_today_details(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    current_date = datetime.now().strftime('%Y-%m-%d')

    cursor.execute('''SELECT time_in, time_out, lunch_start, lunch_end, lunch_minutes, total_hours FROM records 
                   WHERE user_id=? AND date=? ORDER BY time_in''', (user_id, current_date))
    records = cursor.fetchall()
    return records


# Команда старт
async def start(update, context):
    keyboard = [['Вход', 'Выход', 'Обед'], ['Добавить запись', 'Отчет']]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        'Выберите действие:',
        reply_markup=reply_markup
    )


# Главное меню
def main_keyboard():
    keyboard = [['Вход', 'Выход', 'Обед'], ['Добавить запись', 'Отчет']]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


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
    keyboard = [['Начало обеда', 'Конец обеда', 'Минуты обеда'], ['Назад']]
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


# Обработчик минут обеда
async def lunch_minutes(update, context):
    await update.message.reply_text(
        'Введите продолжительность обеда в минутах (например, 60):',
        reply_markup=ReplyKeyboardRemove()
    )
    return ADD_RECORD_LUNCH_MINUTES


# Сохранение времени входа
async def save_time_in(update, context):
    user_id = update.message.from_user.id
    current_date = datetime.now().strftime('%Y-%m-%d')
    time_in_str = update.message.text

    try:
        datetime.strptime(time_in_str, '%H:%M')

        await asyncio.get_event_loop().run_in_executor(
            None, add_time_in, user_id, current_date, time_in_str
        )

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

        await asyncio.get_event_loop().run_in_executor(
            None, add_time_out, user_id, current_date, time_out_str
        )

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

        await asyncio.get_event_loop().run_in_executor(
            None, add_lunch_start, user_id, current_date, lunch_start_str
        )

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

        await asyncio.get_event_loop().run_in_executor(
            None, add_lunch_end, user_id, current_date, lunch_end_str
        )

        await update.message.reply_text('Время конца обеда сохранено!', reply_markup=main_keyboard())
    except ValueError:
        await update.message.reply_text('Неверный формат времени! Используйте ЧЧ:ММ')
        return LUNCH_END

    return ConversationHandler.END


# Сохранение минут обеда
async def save_lunch_minutes(update, context):
    user_id = update.message.from_user.id
    current_date = datetime.now().strftime('%Y-%m-%d')
    lunch_minutes_str = update.message.text

    try:
        lunch_minutes = int(lunch_minutes_str)
        if lunch_minutes < 0:
            raise ValueError("Отрицательное значение")

        await asyncio.get_event_loop().run_in_executor(
            None, add_lunch_minutes, user_id, current_date, lunch_minutes
        )

        await update.message.reply_text('Продолжительность обеда сохранена!', reply_markup=main_keyboard())
    except ValueError:
        await update.message.reply_text('Неверный формат! Введите целое число минут')
        return ADD_RECORD_LUNCH_MINUTES

    return ConversationHandler.END


# Обработчик кнопки "Добавить запись"
async def add_record(update, context):
    context.user_data['adding_record'] = {}
    await update.message.reply_text(
        'Введите дату в формате ДД.ММ.ГГГГ (например, 15.11.2023):\n'
        'Или нажмите /cancel для отмены',
        reply_markup=ReplyKeyboardRemove()
    )
    return ADD_RECORD_DATE


# Обработчик ввода даты для новой записи
async def add_record_date(update, context):
    date_str = update.message.text

    try:
        date_obj = datetime.strptime(date_str, '%d.%m.%Y')
        date_db = date_obj.strftime('%Y-%m-%d')
        context.user_data['adding_record']['date'] = date_db

        await update.message.reply_text(
            'Введите время входа в формате ЧЧ:ММ (например, 09:00):\n'
            'Или нажмите /cancel для отмены'
        )
        return ADD_RECORD_TIME_IN
    except ValueError:
        await update.message.reply_text(
            'Неверный формат даты! Используйте ДД.ММ.ГГГГ (например, 15.11.2023):\n'
            'Или нажмите /cancel для отмены'
        )
        return ADD_RECORD_DATE


# Обработчик ввода времени входа для новой записи
async def add_record_time_in(update, context):
    time_in_str = update.message.text

    try:
        datetime.strptime(time_in_str, '%H:%M')
        context.user_data['adding_record']['time_in'] = time_in_str

        await update.message.reply_text(
            'Введите время выхода в формате ЧЧ:ММ (например, 18:00):\n'
            'Или нажмите /cancel для отмены'
        )
        return ADD_RECORD_TIME_OUT
    except ValueError:
        await update.message.reply_text(
            'Неверный формат времени! Используйте ЧЧ:ММ (например, 09:00):\n'
            'Или нажмите /cancel для отмены'
        )
        return ADD_RECORD_TIME_IN


# Обработчик ввода времени выхода для новой записи
async def add_record_time_out(update, context):
    time_out_str = update.message.text

    try:
        datetime.strptime(time_out_str, '%H:%M')
        context.user_data['adding_record']['time_out'] = time_out_str

        keyboard = [['Время обеда', 'Минуты обеда'], ['Пропустить обед']]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

        await update.message.reply_text(
            'Выберите способ указания обеда:\n'
            'Или нажмите /cancel для отмены',
            reply_markup=reply_markup
        )
        return ADD_RECORD_LUNCH_START
    except ValueError:
        await update.message.reply_text(
            'Неверный формат времени! Используйте ЧЧ:ММ (например, 18:00):\n'
            'Или нажмите /cancel для отмены'
        )
        return ADD_RECORD_TIME_OUT


# Обработчик выбора типа ввода обеда
async def add_record_lunch_type(update, context):
    choice = update.message.text

    if choice == 'Время обеда':
        await update.message.reply_text(
            'Введите время начала обеда в формате ЧЧ:ММ (например, 13:00):\n'
            'Или нажмите /cancel для отмены',
            reply_markup=ReplyKeyboardRemove()
        )
        return ADD_RECORD_LUNCH_START
    elif choice == 'Минуты обеда':
        await update.message.reply_text(
            'Введите продолжительность обеда в минутах (например, 60):\n'
            'Или нажмите /cancel для отмены',
            reply_markup=ReplyKeyboardRemove()
        )
        return ADD_RECORD_LUNCH_MINUTES
    elif choice == 'Пропустить обед':
        return await save_complete_record(update, context)

    return ADD_RECORD_LUNCH_START


# Обработчик ввода времени начала обеда для новой записи
async def add_record_lunch_start(update, context):
    lunch_start_str = update.message.text

    try:
        datetime.strptime(lunch_start_str, '%H:%M')
        context.user_data['adding_record']['lunch_start'] = lunch_start_str

        await update.message.reply_text(
            'Введите время конца обеда в формате ЧЧ:ММ (например, 14:00):\n'
            'Или нажмите /cancel для отмены'
        )
        return ADD_RECORD_LUNCH_END
    except ValueError:
        await update.message.reply_text(
            'Неверный формат времени! Используйте ЧЧ:ММ (например, 13:00):\n'
            'Или нажмите /cancel для отмены'
        )
        return ADD_RECORD_LUNCH_START


# Обработчик ввода времени конца обеда для новой записи
async def add_record_lunch_end(update, context):
    lunch_end_str = update.message.text

    try:
        datetime.strptime(lunch_end_str, '%H:%M')
        context.user_data['adding_record']['lunch_end'] = lunch_end_str

        return await save_complete_record(update, context)
    except ValueError:
        await update.message.reply_text(
            'Неверный формат времени! Используйте ЧЧ:ММ (например, 14:00):\n'
            'Или нажмите /cancel для отмены'
        )
        return ADD_RECORD_LUNCH_END


# Обработчик ввода минут обеда для новой записи
async def add_record_lunch_minutes(update, context):
    lunch_minutes_str = update.message.text

    try:
        lunch_minutes = int(lunch_minutes_str)
        if lunch_minutes < 0:
            raise ValueError("Отрицательное значение")

        context.user_data['adding_record']['lunch_minutes'] = lunch_minutes

        return await save_complete_record(update, context)
    except ValueError:
        await update.message.reply_text(
            'Неверный формат! Введите целое число минут (например, 60):\n'
            'Или нажмите /cancel для отмены'
        )
        return ADD_RECORD_LUNCH_MINUTES


# Сохранение полной записи
async def save_complete_record(update, context):
    user_id = update.message.from_user.id
    record_data = context.user_data['adding_record']

    total_hours = await asyncio.get_event_loop().run_in_executor(
        None, add_complete_record,
        user_id,
        record_data['date'],
        record_data['time_in'],
        record_data['time_out'],
        record_data.get('lunch_start'),
        record_data.get('lunch_end'),
        record_data.get('lunch_minutes')
    )

    message = f"✅ Запись успешно добавлена!\n\n"
    message += f"📅 Дата: {datetime.strptime(record_data['date'], '%Y-%m-%d').strftime('%d.%m.%Y')}\n"
    message += f"⏰ Время: {record_data['time_in']} - {record_data['time_out']}\n"

    if record_data.get('lunch_start') and record_data.get('lunch_end'):
        message += f"🍽 Обед: {record_data['lunch_start']} - {record_data['lunch_end']}\n"
    elif record_data.get('lunch_minutes'):
        message += f"🍽 Обед: {record_data['lunch_minutes']} минут\n"
    else:
        message += f"🍽 Обед: не указан\n"

    message += f"⏱ Отработано: {total_hours:.2f} часов"

    context.user_data.pop('adding_record', None)

    await update.message.reply_text(message, reply_markup=main_keyboard())
    return ConversationHandler.END


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

        total_hours = await asyncio.get_event_loop().run_in_executor(
            None, generate_report, user_id, period
        )

        if period == 'today':
            details = await asyncio.get_event_loop().run_in_executor(
                None, get_today_details, user_id
            )

            if details:
                message = f"📊 Отчет за сегодня ({datetime.now().strftime('%d.%m.%Y')}):\n\n"
                total_day_hours = 0

                for i, record in enumerate(details, 1):
                    time_in, time_out, lunch_start, lunch_end, lunch_minutes, hours = record
                    if time_out and hours is not None:
                        message += f"{i}. ⏰ {time_in} - {time_out}"
                        if lunch_start and lunch_end:
                            message += f" | 🍽 {lunch_start}-{lunch_end}"
                        elif lunch_minutes:
                            message += f" | 🍽 {lunch_minutes} мин"
                        message += f" | ⏱ {hours:.2f} ч.\n"
                        total_day_hours += hours
                    else:
                        message += f"{i}. ⏰ {time_in} - --:-- | ❌ незавершенный вход\n"

                message += f"\n📈 Всего за день: {total_day_hours:.2f} часов"
            else:
                message = "ℹ️ За сегодня нет записей о рабочем времени."
        else:
            period_names = {
                'week': 'неделю',
                'month': 'месяц',
                'year': 'год'
            }
            message = f'📊 Отработано за {period_names[period]}: {total_hours:.2f} часов'

        await update.message.reply_text(message, reply_markup=main_keyboard())
    else:
        await update.message.reply_text('Неверный период отчета')


# Обработчик кнопки "Назад" в меню обеда
async def lunch_back(update, context):
    await update.message.reply_text('Главное меню', reply_markup=main_keyboard())


# Отмена диалога
async def cancel(update, context):
    if 'adding_record' in context.user_data:
        context.user_data.pop('adding_record', None)

    await update.message.reply_text('Операция отменена', reply_markup=main_keyboard())
    return ConversationHandler.END


# Закрытие соединения с БД при завершении
def close_db_connection():
    global _db_connection
    if _db_connection:
        _db_connection.close()


def main():
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
            MessageHandler(filters.Regex('^Конец обеда$'), lunch_end),
            MessageHandler(filters.Regex('^Минуты обеда$'), lunch_minutes)
        ],
        states={
            LUNCH_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_lunch_start)],
            LUNCH_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_lunch_end)],
            ADD_RECORD_LUNCH_MINUTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_lunch_minutes)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    # ConversationHandler для добавления полной записи
    add_record_conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex('^Добавить запись$'), add_record)
        ],
        states={
            ADD_RECORD_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_record_date)],
            ADD_RECORD_TIME_IN: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_record_time_in)],
            ADD_RECORD_TIME_OUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_record_time_out)],
            ADD_RECORD_LUNCH_START: [
                MessageHandler(filters.Regex('^(Время обеда|Минуты обеда|Пропустить обед)$'), add_record_lunch_type),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_record_lunch_start)
            ],
            ADD_RECORD_LUNCH_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_record_lunch_end)],
            ADD_RECORD_LUNCH_MINUTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_record_lunch_minutes)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(time_conv_handler)
    application.add_handler(lunch_conv_handler)
    application.add_handler(add_record_conv_handler)
    application.add_handler(MessageHandler(filters.Regex('^Обед$'), lunch))
    application.add_handler(MessageHandler(filters.Regex('^Назад$'), lunch_back))
    application.add_handler(MessageHandler(filters.Regex('^Отчет$'), report_menu))
    application.add_handler(
        MessageHandler(filters.Regex('^(Сегодня|Неделя|Месяц|Год|Назад)$'), generate_report_handler))

    try:
        application.run_polling()
    finally:
        close_db_connection()


if __name__ == '__main__':
    main()