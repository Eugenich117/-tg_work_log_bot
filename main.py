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
    ADD_RECORD_LUNCH_START, ADD_RECORD_LUNCH_END, ADD_RECORD_LUNCH_MINUTES,
    CALC_TIME_IN, CALC_TIME_OUT, CALC_LUNCH_MINUTES,
    DELETE_RECORD_DATE, DELETE_CONFIRM,
    SET_NORM  # недельная норма
) = range(16)


# Функция для преобразования часов в формате float в строку времени (ЧЧ:ММ)
def float_hours_to_time_str(hours_float):
    """Преобразует часы в формате float в строку времени ЧЧ:ММ"""
    if hours_float is None:
        return "0:00"

    hours = int(hours_float)
    minutes = int(round((hours_float - hours) * 60))

    # Обработка случая, когда минуты достигают 60
    if minutes >= 60:
        hours += 1
        minutes = 0

    return f"{hours}:{minutes:02d}"


# Функция для преобразования минут в строку времени (ЧЧ:ММ)
def minutes_to_time_str(total_minutes):
    """Преобразует минуты в строку времени ЧЧ:ММ"""
    if total_minutes is None:
        return "0:00"
    hours = total_minutes // 60
    minutes = total_minutes % 60
    return f"{hours}:{minutes:02d}"


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
    # ---- ТАБЛИЦА настроек пользователя (недельная норма) ----
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            weekly_norm REAL
        )
    ''')
    # Миграция: если в таблице есть старый столбец monthly_norm - переименуем
    try:
        cursor.execute('ALTER TABLE user_settings RENAME COLUMN monthly_norm TO weekly_norm')
        conn.commit()
    except Exception:
        pass  # столбец уже weekly_norm или таблица только что создана
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


# ---- ФУНКЦИИ: недельная норма ----

def get_weekly_norm(user_id):
    """Получить недельную норму часов для пользователя (None если не задана)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT weekly_norm FROM user_settings WHERE user_id=?', (user_id,))
    row = cursor.fetchone()
    return row[0] if row else None


def set_weekly_norm(user_id, norm_hours):
    """Сохранить недельную норму часов для пользователя."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO user_settings (user_id, weekly_norm)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET weekly_norm=excluded.weekly_norm
    ''', (user_id, norm_hours))
    conn.commit()


def get_working_days_in_month(year, month):
    """Подсчитать количество рабочих дней (пн-пт) в заданном месяце, без учёта праздников."""
    if month == 12:
        first_day = datetime(year, month, 1).date()
        last_day = datetime(year + 1, 1, 1).date() - timedelta(days=1)
    else:
        first_day = datetime(year, month, 1).date()
        last_day = datetime(year, month + 1, 1).date() - timedelta(days=1)
    count = 0
    current = first_day
    while current <= last_day:
        if current.weekday() < 5:  # 0=пн, 4=пт
            count += 1
        current += timedelta(days=1)
    return count


def calc_monthly_norm_from_weekly(weekly_norm, year, month):
    """Рассчитать месячную норму: недельная_норма / 5 * рабочих_дней_в_месяце."""
    working_days = get_working_days_in_month(year, month)
    return round(weekly_norm / 5 * working_days, 2)


# Расчет рабочих часов с учетом обеда (только если >4 часов)
def calculate_work_hours(time_in, time_out, lunch_start=None, lunch_end=None, lunch_minutes=None):
    try:
        time_in_dt = datetime.strptime(time_in, '%H:%M')
        time_out_dt = datetime.strptime(time_out, '%H:%M')
        total_time = (time_out_dt - time_in_dt).total_seconds() / 3600
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


def get_records_by_date(user_id, date):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''SELECT id, time_in, time_out, lunch_start, lunch_end, lunch_minutes, total_hours
                      FROM records WHERE user_id=? AND date=? ORDER BY time_in''', (user_id, date))
    return cursor.fetchall()


def get_detailed_records_period(user_id, start_date, end_date):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''SELECT date, time_in, time_out, lunch_start, lunch_end, lunch_minutes, total_hours
                      FROM records WHERE user_id=? AND date BETWEEN ? AND ?
                      ORDER BY date, time_in''', (user_id, start_date, end_date))
    return cursor.fetchall()


def delete_records_by_date(user_id, date):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM records WHERE user_id=? AND date=?', (user_id, date))
    deleted_count = cursor.rowcount
    conn.commit()
    return deleted_count


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


def add_time_in(user_id, date, time_in):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM records WHERE user_id=? AND date=? AND time_out IS NULL', (user_id, date))
    existing = cursor.fetchone()
    if existing:
        cursor.execute('UPDATE records SET time_in=? WHERE id=?', (time_in, existing[0]))
    else:
        cursor.execute('INSERT INTO records (user_id, date, time_in) VALUES (?, ?, ?)', (user_id, date, time_in))
    conn.commit()


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
        cursor.execute('UPDATE records SET time_out=?, total_hours=? WHERE id=?', (time_out, total_hours, record_id))
    conn.commit()


def add_lunch_start(user_id, date, lunch_start):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM records WHERE user_id=? AND date=? AND time_out IS NULL', (user_id, date))
    result = cursor.fetchone()
    if result:
        cursor.execute('UPDATE records SET lunch_start=? WHERE id=?', (lunch_start, result[0]))
    conn.commit()


def add_lunch_end(user_id, date, lunch_end):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM records WHERE user_id=? AND date=? AND time_out IS NULL', (user_id, date))
    result = cursor.fetchone()
    if result:
        cursor.execute('UPDATE records SET lunch_end=? WHERE id=?', (lunch_end, result[0]))
    conn.commit()


def add_lunch_minutes(user_id, date, lunch_minutes):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM records WHERE user_id=? AND date=? AND time_out IS NULL', (user_id, date))
    result = cursor.fetchone()
    if result:
        cursor.execute('UPDATE records SET lunch_minutes=? WHERE id=?', (lunch_minutes, result[0]))
    conn.commit()


def generate_report(user_id, period):
    conn = get_db_connection()
    cursor = conn.cursor()
    today = datetime.now().date()
    if period == 'today':
        current_date = today.strftime('%Y-%m-%d')
        cursor.execute('SELECT SUM(total_hours) FROM records WHERE user_id=? AND date=?', (user_id, current_date))
    elif period == 'week':
        start_of_week = today - timedelta(days=today.weekday())
        end_of_week = start_of_week + timedelta(days=6)
        cursor.execute('SELECT SUM(total_hours) FROM records WHERE user_id=? AND date BETWEEN ? AND ?',
                       (user_id, start_of_week.strftime('%Y-%m-%d'), end_of_week.strftime('%Y-%m-%d')))
    elif period == 'month':
        start_of_month = today.replace(day=1)
        if today.month == 12:
            end_of_month = today.replace(year=today.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            end_of_month = today.replace(month=today.month + 1, day=1) - timedelta(days=1)
        cursor.execute('SELECT SUM(total_hours) FROM records WHERE user_id=? AND date BETWEEN ? AND ?',
                       (user_id, start_of_month.strftime('%Y-%m-%d'), end_of_month.strftime('%Y-%m-%d')))
    else:  # year
        start_date = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
        cursor.execute('SELECT SUM(total_hours) FROM records WHERE user_id=? AND date>=?', (user_id, start_date))
    result = cursor.fetchone()
    return result[0] or 0


def get_today_details(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    current_date = datetime.now().strftime('%Y-%m-%d')
    cursor.execute('''SELECT time_in, time_out, lunch_start, lunch_end, lunch_minutes, total_hours
                      FROM records WHERE user_id=? AND date=? ORDER BY time_in''', (user_id, current_date))
    return cursor.fetchall()


# ---- КЛАВИАТУРЫ ----

def main_keyboard():
    keyboard = [
        ['Вход', 'Выход', 'Обед'],
        ['Добавить запись', 'Отчет'],
        ['Расчет рабочего времени', 'Коррекция журнала'],
        ['⚙️ Норма часов']   # <-- новая кнопка
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# ---- ОБРАБОТЧИКИ ----

async def start(update, context):
    await update.message.reply_text('Выберите действие:', reply_markup=main_keyboard())


# ---- НОРМА ВЫРАБОТКИ ----

async def set_norm_start(update, context):
    user_id = update.message.from_user.id
    current_norm = await asyncio.get_event_loop().run_in_executor(None, get_weekly_norm, user_id)
    if current_norm:
        today = datetime.now()
        monthly = calc_monthly_norm_from_weekly(current_norm, today.year, today.month)
        norm_info = (f"Текущая недельная норма: {float_hours_to_time_str(current_norm)} ч.\n"
                     f"Месячная норма на {today.strftime('%B %Y')}: {float_hours_to_time_str(monthly)} ч.\n\n")
    else:
        norm_info = ""
    await update.message.reply_text(
        f'{norm_info}Введите недельную норму рабочих часов (например: 40 или 24):\n'
        'Или нажмите /cancel для отмены',
        reply_markup=ReplyKeyboardRemove()
    )
    return SET_NORM


async def set_norm_save(update, context):
    user_id = update.message.from_user.id
    text = update.message.text.replace(',', '.')
    try:
        norm = float(text)
        if norm <= 0:
            raise ValueError("Норма должна быть положительной")
        await asyncio.get_event_loop().run_in_executor(None, set_weekly_norm, user_id, norm)
        today = datetime.now()
        monthly = calc_monthly_norm_from_weekly(norm, today.year, today.month)
        working_days = get_working_days_in_month(today.year, today.month)
        await update.message.reply_text(
            f'✅ Недельная норма установлена: {float_hours_to_time_str(norm)} ч.\n'
            f'📅 Рабочих дней в {today.strftime("%B")}: {working_days}\n'
            f'🎯 Месячная норма: {float_hours_to_time_str(monthly)} ч.',
            reply_markup=main_keyboard()
        )
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text(
            'Неверный формат! Введите число часов (например: 40 или 24):\n'
            'Или нажмите /cancel для отмены'
        )
        return SET_NORM


# ---- КОРРЕКЦИЯ ЖУРНАЛА ----

async def journal_correction(update, context):
    await update.message.reply_text(
        'Введите дату в формате ДД.ММ.ГГГГ (например, 15.11.2023) для удаления записей:\n'
        'Или нажмите /cancel для отмены',
        reply_markup=ReplyKeyboardRemove()
    )
    return DELETE_RECORD_DATE


async def delete_record_date(update, context):
    date_str = update.message.text
    user_id = update.message.from_user.id
    try:
        date_obj = datetime.strptime(date_str, '%d.%m.%Y')
        date_db = date_obj.strftime('%Y-%m-%d')
        records = await asyncio.get_event_loop().run_in_executor(None, get_records_by_date, user_id, date_db)
        if not records:
            await update.message.reply_text(f'За {date_str} нет записей для удаления.', reply_markup=main_keyboard())
            return ConversationHandler.END
        context.user_data['delete_date'] = date_db
        context.user_data['delete_date_display'] = date_str
        message = f"Найдены записи за {date_str}:\n\n"
        total_hours = 0
        for i, record in enumerate(records, 1):
            record_id, time_in, time_out, lunch_start, lunch_end, lunch_minutes, hours = record
            message += f"{i}. ⏰ {time_in} - {time_out}"
            if lunch_start and lunch_end:
                message += f" | 🍽 {lunch_start}-{lunch_end}"
            elif lunch_minutes:
                message += f" | 🍽 {lunch_minutes} мин"
            if hours:
                message += f" | ⏱ {float_hours_to_time_str(hours)} ч.\n"
                total_hours += hours
            else:
                message += " | ⏱ расчет...\n"
        message += f"\n📈 Всего за день: {float_hours_to_time_str(total_hours)} часов\n\n"
        message += "Вы уверены, что хотите удалить эти записи? (да/нет)"
        await update.message.reply_text(message)
        return DELETE_CONFIRM
    except ValueError:
        await update.message.reply_text(
            'Неверный формат даты! Используйте ДД.ММ.ГГГГ (например, 15.11.2023):\n'
            'Или нажмите /cancel для отмены'
        )
        return DELETE_RECORD_DATE


async def delete_confirm(update, context):
    user_id = update.message.from_user.id
    choice = update.message.text.lower()
    if choice == 'да':
        date_db = context.user_data['delete_date']
        date_display = context.user_data['delete_date_display']
        deleted_count = await asyncio.get_event_loop().run_in_executor(
            None, delete_records_by_date, user_id, date_db)
        context.user_data.pop('delete_date', None)
        context.user_data.pop('delete_date_display', None)
        await update.message.reply_text(f'✅ Удалено {deleted_count} записей за {date_display}.', reply_markup=main_keyboard())
        return ConversationHandler.END
    elif choice == 'нет':
        context.user_data.pop('delete_date', None)
        context.user_data.pop('delete_date_display', None)
        await update.message.reply_text('Удаление отменено.', reply_markup=main_keyboard())
        return ConversationHandler.END
    else:
        await update.message.reply_text('Пожалуйста, введите "да" или "нет":')
        return DELETE_CONFIRM


# ---- РАСЧЕТ РАБОЧЕГО ВРЕМЕНИ ----

async def worktime_calculation(update, context):
    await update.message.reply_text('Введите время входа в формате ЧЧ:ММ (например, 09:00):',
                                    reply_markup=ReplyKeyboardRemove())
    return CALC_TIME_IN


async def calc_time_in(update, context):
    time_in_str = update.message.text
    try:
        datetime.strptime(time_in_str, '%H:%M')
        context.user_data['calc_time_in'] = time_in_str
        await update.message.reply_text('Введите время выхода в формате ЧЧ:ММ (например, 18:00):')
        return CALC_TIME_OUT
    except ValueError:
        await update.message.reply_text('Неверный формат времени! Используйте ЧЧ:ММ (например, 09:00):')
        return CALC_TIME_IN


async def calc_time_out(update, context):
    time_out_str = update.message.text
    try:
        datetime.strptime(time_out_str, '%H:%M')
        context.user_data['calc_time_out'] = time_out_str
        await update.message.reply_text('Введите продолжительность обеда в минутах (например, 60):\nЕсли обеда не было, введите 0')
        return CALC_LUNCH_MINUTES
    except ValueError:
        await update.message.reply_text('Неверный формат времени! Используйте ЧЧ:ММ (например, 18:00):')
        return CALC_TIME_OUT


async def calc_lunch_minutes(update, context):
    lunch_minutes_str = update.message.text
    try:
        lunch_minutes = int(lunch_minutes_str)
        if lunch_minutes < 0:
            raise ValueError("Отрицательное значение")
        time_in = context.user_data.get('calc_time_in')
        time_out = context.user_data.get('calc_time_out')
        total_hours = calculate_work_hours(time_in, time_out, lunch_minutes=lunch_minutes)
        total_time_str = float_hours_to_time_str(total_hours)
        message = (f"📊 Результат расчета:\n\n"
                   f"⏰ Время входа: {time_in}\n"
                   f"⏰ Время выхода: {time_out}\n"
                   f"🍽 Обед: {lunch_minutes} минут\n"
                   f"⏱ Отработано: {total_time_str} часов")
        context.user_data.pop('calc_time_in', None)
        context.user_data.pop('calc_time_out', None)
        await update.message.reply_text(message, reply_markup=main_keyboard())
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text('Неверный формат! Введите целое число минут (например, 60):')
        return CALC_LUNCH_MINUTES


# ---- ВХОД / ВЫХОД ----

async def time_in(update, context):
    await update.message.reply_text('Введите время входа в формате ЧЧ:ММ (например, 09:00):',
                                    reply_markup=ReplyKeyboardRemove())
    return TIME_IN


async def time_out(update, context):
    await update.message.reply_text('Введите время выхода в формате ЧЧ:ММ (например, 18:00):',
                                    reply_markup=ReplyKeyboardRemove())
    return TIME_OUT


async def lunch(update, context):
    keyboard = [['Начало обеда', 'Конец обеда', 'Минуты обеда'], ['Назад']]
    await update.message.reply_text('Выберите действие для обеда:',
                                    reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))


async def lunch_start(update, context):
    await update.message.reply_text('Введите время начала обеда в формате ЧЧ:ММ (например, 13:00):',
                                    reply_markup=ReplyKeyboardRemove())
    return LUNCH_START


async def lunch_end(update, context):
    await update.message.reply_text('Введите время конца обеда в формате ЧЧ:ММ (например, 14:00):',
                                    reply_markup=ReplyKeyboardRemove())
    return LUNCH_END


async def lunch_minutes(update, context):
    await update.message.reply_text('Введите продолжительность обеда в минутах (например, 60):',
                                    reply_markup=ReplyKeyboardRemove())
    return ADD_RECORD_LUNCH_MINUTES


async def save_time_in(update, context):
    user_id = update.message.from_user.id
    current_date = datetime.now().strftime('%Y-%m-%d')
    time_in_str = update.message.text
    try:
        datetime.strptime(time_in_str, '%H:%M')
        await asyncio.get_event_loop().run_in_executor(None, add_time_in, user_id, current_date, time_in_str)
        await update.message.reply_text('Время входа сохранено!', reply_markup=main_keyboard())
    except ValueError:
        await update.message.reply_text('Неверный формат времени! Используйте ЧЧ:ММ')
        return TIME_IN
    return ConversationHandler.END


async def save_time_out(update, context):
    user_id = update.message.from_user.id
    current_date = datetime.now().strftime('%Y-%m-%d')
    time_out_str = update.message.text
    try:
        datetime.strptime(time_out_str, '%H:%M')
        await asyncio.get_event_loop().run_in_executor(None, add_time_out, user_id, current_date, time_out_str)
        await update.message.reply_text('Время выхода сохранено!', reply_markup=main_keyboard())
    except ValueError:
        await update.message.reply_text('Неверный формат времени! Используйте ЧЧ:ММ')
        return TIME_OUT
    return ConversationHandler.END


async def save_lunch_start(update, context):
    user_id = update.message.from_user.id
    current_date = datetime.now().strftime('%Y-%m-%d')
    lunch_start_str = update.message.text
    try:
        datetime.strptime(lunch_start_str, '%H:%M')
        await asyncio.get_event_loop().run_in_executor(None, add_lunch_start, user_id, current_date, lunch_start_str)
        await update.message.reply_text('Время начала обеда сохранено!', reply_markup=main_keyboard())
    except ValueError:
        await update.message.reply_text('Неверный формат времени! Используйте ЧЧ:ММ')
        return LUNCH_START
    return ConversationHandler.END


async def save_lunch_end(update, context):
    user_id = update.message.from_user.id
    current_date = datetime.now().strftime('%Y-%m-%d')
    lunch_end_str = update.message.text
    try:
        datetime.strptime(lunch_end_str, '%H:%M')
        await asyncio.get_event_loop().run_in_executor(None, add_lunch_end, user_id, current_date, lunch_end_str)
        await update.message.reply_text('Время конца обеда сохранено!', reply_markup=main_keyboard())
    except ValueError:
        await update.message.reply_text('Неверный формат времени! Используйте ЧЧ:ММ')
        return LUNCH_END
    return ConversationHandler.END


async def save_lunch_minutes(update, context):
    user_id = update.message.from_user.id
    current_date = datetime.now().strftime('%Y-%m-%d')
    lunch_minutes_str = update.message.text
    try:
        lunch_minutes_val = int(lunch_minutes_str)
        if lunch_minutes_val < 0:
            raise ValueError("Отрицательное значение")
        await asyncio.get_event_loop().run_in_executor(None, add_lunch_minutes, user_id, current_date, lunch_minutes_val)
        await update.message.reply_text('Продолжительность обеда сохранена!', reply_markup=main_keyboard())
    except ValueError:
        await update.message.reply_text('Неверный формат! Введите целое число минут')
        return ADD_RECORD_LUNCH_MINUTES
    return ConversationHandler.END


# ---- ДОБАВИТЬ ЗАПИСЬ ----

async def add_record(update, context):
    context.user_data['adding_record'] = {}
    await update.message.reply_text(
        'Введите дату в формате ДД.ММ.ГГГГ (например, 15.11.2023):\nИли нажмите /cancel для отмены',
        reply_markup=ReplyKeyboardRemove()
    )
    return ADD_RECORD_DATE


async def add_record_date(update, context):
    date_str = update.message.text
    try:
        date_obj = datetime.strptime(date_str, '%d.%m.%Y')
        context.user_data['adding_record']['date'] = date_obj.strftime('%Y-%m-%d')
        await update.message.reply_text('Введите время входа в формате ЧЧ:ММ (например, 09:00):\nИли нажмите /cancel для отмены')
        return ADD_RECORD_TIME_IN
    except ValueError:
        await update.message.reply_text('Неверный формат даты! Используйте ДД.ММ.ГГГГ:\nИли нажмите /cancel для отмены')
        return ADD_RECORD_DATE


async def add_record_time_in(update, context):
    time_in_str = update.message.text
    try:
        datetime.strptime(time_in_str, '%H:%M')
        context.user_data['adding_record']['time_in'] = time_in_str
        await update.message.reply_text('Введите время выхода в формате ЧЧ:ММ (например, 18:00):\nИли нажмите /cancel для отмены')
        return ADD_RECORD_TIME_OUT
    except ValueError:
        await update.message.reply_text('Неверный формат времени! Используйте ЧЧ:ММ:\nИли нажмите /cancel для отмены')
        return ADD_RECORD_TIME_IN


async def add_record_time_out(update, context):
    time_out_str = update.message.text
    try:
        datetime.strptime(time_out_str, '%H:%M')
        context.user_data['adding_record']['time_out'] = time_out_str
        keyboard = [['Время обеда', 'Минуты обеда'], ['Пропустить обед']]
        await update.message.reply_text(
            'Выберите способ указания обеда:\nИли нажмите /cancel для отмены',
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return ADD_RECORD_LUNCH_START
    except ValueError:
        await update.message.reply_text('Неверный формат времени! Используйте ЧЧ:ММ:\nИли нажмите /cancel для отмены')
        return ADD_RECORD_TIME_OUT


async def add_record_lunch_type(update, context):
    choice = update.message.text
    if choice == 'Время обеда':
        await update.message.reply_text('Введите время начала обеда в формате ЧЧ:ММ (например, 13:00):\nИли нажмите /cancel для отмены',
                                        reply_markup=ReplyKeyboardRemove())
        return ADD_RECORD_LUNCH_START
    elif choice == 'Минуты обеда':
        await update.message.reply_text('Введите продолжительность обеда в минутах (например, 60):\nИли нажмите /cancel для отмены',
                                        reply_markup=ReplyKeyboardRemove())
        return ADD_RECORD_LUNCH_MINUTES
    elif choice == 'Пропустить обед':
        return await save_complete_record(update, context)
    return ADD_RECORD_LUNCH_START


async def add_record_lunch_start(update, context):
    lunch_start_str = update.message.text
    try:
        datetime.strptime(lunch_start_str, '%H:%M')
        context.user_data['adding_record']['lunch_start'] = lunch_start_str
        await update.message.reply_text('Введите время конца обеда в формате ЧЧ:ММ (например, 14:00):\nИли нажмите /cancel для отмены')
        return ADD_RECORD_LUNCH_END
    except ValueError:
        await update.message.reply_text('Неверный формат времени! Используйте ЧЧ:ММ:\nИли нажмите /cancel для отмены')
        return ADD_RECORD_LUNCH_START


async def add_record_lunch_end(update, context):
    lunch_end_str = update.message.text
    try:
        datetime.strptime(lunch_end_str, '%H:%M')
        context.user_data['adding_record']['lunch_end'] = lunch_end_str
        return await save_complete_record(update, context)
    except ValueError:
        await update.message.reply_text('Неверный формат времени! Используйте ЧЧ:ММ:\nИли нажмите /cancel для отмены')
        return ADD_RECORD_LUNCH_END


async def add_record_lunch_minutes(update, context):
    lunch_minutes_str = update.message.text
    try:
        lunch_minutes_val = int(lunch_minutes_str)
        if lunch_minutes_val < 0:
            raise ValueError("Отрицательное значение")
        context.user_data['adding_record']['lunch_minutes'] = lunch_minutes_val
        return await save_complete_record(update, context)
    except ValueError:
        await update.message.reply_text('Неверный формат! Введите целое число минут:\nИли нажмите /cancel для отмены')
        return ADD_RECORD_LUNCH_MINUTES


async def save_complete_record(update, context):
    user_id = update.message.from_user.id
    record_data = context.user_data['adding_record']
    total_hours = await asyncio.get_event_loop().run_in_executor(
        None, add_complete_record,
        user_id, record_data['date'], record_data['time_in'], record_data['time_out'],
        record_data.get('lunch_start'), record_data.get('lunch_end'), record_data.get('lunch_minutes')
    )
    total_time_str = float_hours_to_time_str(total_hours)
    message = f"✅ Запись успешно добавлена!\n\n"
    message += f"📅 Дата: {datetime.strptime(record_data['date'], '%Y-%m-%d').strftime('%d.%m.%Y')}\n"
    message += f"⏰ Время: {record_data['time_in']} - {record_data['time_out']}\n"
    if record_data.get('lunch_start') and record_data.get('lunch_end'):
        message += f"🍽 Обед: {record_data['lunch_start']} - {record_data['lunch_end']}\n"
    elif record_data.get('lunch_minutes'):
        message += f"🍽 Обед: {record_data['lunch_minutes']} минут\n"
    else:
        message += f"🍽 Обед: не указан\n"
    message += f"⏱ Отработано: {total_time_str} часов"
    context.user_data.pop('adding_record', None)
    await update.message.reply_text(message, reply_markup=main_keyboard())
    return ConversationHandler.END


# ---- ОТЧЕТЫ ----

async def report_menu(update, context):
    keyboard = [['Сегодня', 'Неделя', 'Месяц'], ['Год', 'Назад']]
    await update.message.reply_text('Выберите период для отчета:',
                                    reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))


async def generate_report_handler(update, context):
    user_id = update.message.from_user.id
    period_text = update.message.text.lower()

    if period_text == 'назад':
        await update.message.reply_text('Главное меню', reply_markup=main_keyboard())
        return

    period_map = {'сегодня': 'today', 'неделя': 'week', 'месяц': 'month', 'год': 'year'}

    if period_text not in period_map:
        await update.message.reply_text('Неверный период отчета')
        return

    period = period_map[period_text]

    if period == 'today':
        details = await asyncio.get_event_loop().run_in_executor(None, get_today_details, user_id)
        if details:
            message = f"📊 Отчет за сегодня ({datetime.now().strftime('%d.%m.%Y')}):\n\n"
            total_day_hours = 0
            for i, record in enumerate(details, 1):
                time_in, time_out, lunch_start, lunch_end, lunch_minutes, hours = record
                if time_out and hours is not None:
                    time_str = float_hours_to_time_str(hours)
                    message += f"{i}. ⏰ {time_in} - {time_out}"
                    if lunch_start and lunch_end:
                        message += f" | 🍽 {lunch_start}-{lunch_end}"
                    elif lunch_minutes:
                        message += f" | 🍽 {lunch_minutes} мин"
                    message += f" | ⏱ {time_str} ч.\n"
                    total_day_hours += hours
                else:
                    message += f"{i}. ⏰ {time_in} - --:-- | ❌ незавершенный вход\n"
            message += f"\n📈 Всего за день: {float_hours_to_time_str(total_day_hours)} часов"
        else:
            message = "ℹ️ За сегодня нет записей о рабочем времени."

    elif period in ['week', 'month']:
        today = datetime.now().date()
        if period == 'week':
            start_date = today - timedelta(days=today.weekday())
            end_date = start_date + timedelta(days=6)
            period_name = 'неделю'
        else:
            start_date = today.replace(day=1)
            if today.month == 12:
                end_date = today.replace(year=today.year + 1, month=1, day=1) - timedelta(days=1)
            else:
                end_date = today.replace(month=today.month + 1, day=1) - timedelta(days=1)
            period_name = 'месяц'

        detailed_records = await asyncio.get_event_loop().run_in_executor(
            None, get_detailed_records_period, user_id,
            start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')
        )

        records_by_date = {}
        for record in detailed_records:
            date_str, time_in, time_out, lunch_start, lunch_end, lunch_minutes, hours = record
            if date_str not in records_by_date:
                records_by_date[date_str] = []
            records_by_date[date_str].append({
                'time_in': time_in, 'time_out': time_out,
                'lunch_start': lunch_start, 'lunch_end': lunch_end,
                'lunch_minutes': lunch_minutes, 'hours': hours
            })

        total_period_hours = 0
        message = f"📊 Детализированный отчет за {period_name} "
        message += f"(с {start_date.strftime('%d.%m.%Y')} по {end_date.strftime('%d.%m.%Y')}):\n\n"

        for date_str in sorted(records_by_date.keys()):
            date_display = datetime.strptime(date_str, '%Y-%m-%d').strftime('%d.%m.%Y')
            day_records = records_by_date[date_str]
            day_total = 0
            message += f"📅 {date_display}:\n"
            for i, record in enumerate(day_records, 1):
                time_in = record['time_in']
                time_out = record['time_out']
                lunch_start = record['lunch_start']
                lunch_end = record['lunch_end']
                lunch_minutes = record['lunch_minutes']
                hours = record['hours']
                if time_out and hours is not None:
                    message += f"  {i}. ⏰ {time_in} - {time_out}"
                    if lunch_start and lunch_end:
                        message += f" | 🍽 {lunch_start}-{lunch_end}"
                    elif lunch_minutes:
                        message += f" | 🍽 {lunch_minutes} мин"
                    message += f" | ⏱ {float_hours_to_time_str(hours)} ч.\n"
                    day_total += hours
                else:
                    message += f"  {i}. ⏰ {time_in} - --:-- | ❌ незавершенный вход\n"
            if day_total > 0:
                message += f"  📈 Итого за день: {float_hours_to_time_str(day_total)} часов\n"
                total_period_hours += day_total
            message += "\n"

        total_time_str = float_hours_to_time_str(total_period_hours)
        message += f"📊 Всего за {period_name}: {total_time_str} часов"

        # ---- НОРМА: только для месячного отчёта ----
        if period == 'month':
            weekly_norm = await asyncio.get_event_loop().run_in_executor(None, get_weekly_norm, user_id)
            if weekly_norm:
                working_days = get_working_days_in_month(today.year, today.month)
                monthly_norm = calc_monthly_norm_from_weekly(weekly_norm, today.year, today.month)
                delta = total_period_hours - monthly_norm
                delta_str = float_hours_to_time_str(abs(delta))
                message += (f"\n\n🎯 Норма за месяц: {float_hours_to_time_str(monthly_norm)} ч."
                            f"  (неделя {float_hours_to_time_str(weekly_norm)} ч. × {working_days} р.дней / 5)")
                if delta >= 0:
                    message += f"\n✅ Переработка: +{delta_str} ч."
                else:
                    message += f"\n⏳ Осталось отработать: {delta_str} ч."
            else:
                message += f"\n\nℹ️ Норма не задана. Установите её кнопкой «⚙️ Норма часов»."

    else:  # year
        total_hours = await asyncio.get_event_loop().run_in_executor(None, generate_report, user_id, period)
        message = f'📊 Отработано за год: {float_hours_to_time_str(total_hours)} часов'

    await update.message.reply_text(message, reply_markup=main_keyboard())

# Обработчик кнопки "Назад" в меню обеда
async def lunch_back(update, context):
    await update.message.reply_text('Главное меню', reply_markup=main_keyboard())


async def cancel(update, context):
    context.user_data.pop('adding_record', None)
    context.user_data.pop('delete_date', None)
    context.user_data.pop('delete_date_display', None)
    context.user_data.pop('calc_time_in', None)
    context.user_data.pop('calc_time_out', None)

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

    # ---- ConversationHandler для нормы выработки ----
    set_norm_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^⚙️ Норма часов$'), set_norm_start)],
        states={
            SET_NORM: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_norm_save)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True
    )

    delete_record_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^Коррекция журнала$'), journal_correction)],
        states={
            DELETE_RECORD_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_record_date)],
            DELETE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_confirm)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True
    )

    # ConversationHandler для расчета рабочего времени
    calc_worktime_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^Расчет рабочего времени$'), worktime_calculation)],
        states={
            CALC_TIME_IN: [MessageHandler(filters.TEXT & ~filters.COMMAND, calc_time_in)],
            CALC_TIME_OUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, calc_time_out)],
            CALC_LUNCH_MINUTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, calc_lunch_minutes)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True
    )

    # ConversationHandler для добавления полной записи
    add_record_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^Добавить запись$'), add_record)],
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
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True
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

    application.add_handler(set_norm_handler)          # <-- новый обработчик
    application.add_handler(delete_record_handler)
    application.add_handler(calc_worktime_handler)
    application.add_handler(add_record_conv_handler)
    application.add_handler(lunch_conv_handler)
    application.add_handler(time_conv_handler)

    # Затем общие обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Regex('^Обед$'), lunch))
    application.add_handler(MessageHandler(filters.Regex('^Назад$'), lunch_back))
    application.add_handler(MessageHandler(filters.Regex('^Отчет$'), report_menu))
    application.add_handler(MessageHandler(filters.Regex('^(Сегодня|Неделя|Месяц|Год)$'), generate_report_handler))

    try:
        application.run_polling()
    finally:
        close_db_connection()


if __name__ == '__main__':
    main()