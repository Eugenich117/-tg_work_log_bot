import logging
from datetime import datetime, timedelta
import sqlite3
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ConversationHandler
import os
from pathlib import Path

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Определение состояний для ConversationHandler
TIME_IN, TIME_OUT, LUNCH_DURATION = range(3)


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


# Инициализация базы данных
def init_db():
    conn = sqlite3.connect('timesheet.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            date TEXT,
            time_in TEXT,
            time_out TEXT,
            lunch_duration INTEGER DEFAULT 0,
            total_hours REAL,
            lunch_applied BOOLEAN DEFAULT FALSE
        )
    ''')
    conn.commit()
    conn.close()


# Добавление записи о входе
def add_time_in(user_id, date, time_in):
    conn = sqlite3.connect('timesheet.db', check_same_thread=False)
    cursor = conn.cursor()
    try:
        cursor.execute('INSERT INTO records (user_id, date, time_in) VALUES (?, ?, ?)',
                       (user_id, date, time_in))
        conn.commit()
        logger.info(f"Добавлен вход: user_id={user_id}, date={date}, time_in={time_in}")
    except Exception as e:
        logger.error(f"Ошибка при добавлении входа: {e}")
    finally:
        conn.close()


# Получение текущей активной записи (без времени выхода)
def get_active_record(user_id, date):
    conn = sqlite3.connect('timesheet.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute(
        'SELECT id, time_in, lunch_duration FROM records WHERE user_id=? AND date=? AND time_out IS NULL',
        (user_id, date))
    result = cursor.fetchone()
    conn.close()
    return result


# Расчет рабочих часов с учетом обеда (только если работа > 4 часов)
def calculate_work_hours(time_in, time_out, lunch_duration=0):
    try:
        # Преобразуем время в объекты datetime
        time_in_dt = datetime.strptime(time_in, '%H:%M')
        time_out_dt = datetime.strptime(time_out, '%H:%M')

        # Если время выхода раньше времени входа, добавляем день
        if time_out_dt < time_in_dt:
            time_out_dt += timedelta(days=1)

        # Общее время между входом и выходом в часах
        total_minutes = (time_out_dt - time_in_dt).total_seconds() / 60
        total_hours = total_minutes / 60

        # Проверяем, превышает ли рабочее время 4 часа
        lunch_applied = False
        if total_hours > 4 and lunch_duration:
            lunch_hours = lunch_duration / 60.0
            total_hours -= lunch_hours
            lunch_applied = True
            logger.info(
                f"Обед применен: общее время {total_hours + lunch_hours:.2f}ч - обед {lunch_hours:.2f}ч = {total_hours:.2f}ч")
        else:
            if lunch_duration and total_hours <= 4:
                logger.info(f"Обед не применен: рабочее время {total_hours:.2f}ч <= 4 часов")
            elif not lunch_duration:
                logger.info(f"Обед не указан: общее время {total_hours:.2f}ч")

        return max(0, round(total_hours, 2)), lunch_applied
    except Exception as e:
        logger.error(f"Ошибка расчета времени: {e}")
        return 0, False


# Обновление записи о выходе и расчет часов
def add_time_out(user_id, date, time_out):
    conn = sqlite3.connect('timesheet.db', check_same_thread=False)
    cursor = conn.cursor()
    try:
        # Находим активную запись
        record = get_active_record(user_id, date)
        if not record:
            logger.error(f"Не найдена активная запись для user_id={user_id}, date={date}")
            return False

        record_id, time_in, lunch_duration = record
        logger.info(f"Найдена запись: time_in={time_in}, lunch_duration={lunch_duration}")

        # Рассчитываем общее время
        total_hours, lunch_applied = calculate_work_hours(time_in, time_out, lunch_duration)
        logger.info(f"Рассчитано total_hours: {total_hours}, lunch_applied: {lunch_applied}")

        # Обновляем запись
        cursor.execute(
            'UPDATE records SET time_out=?, total_hours=?, lunch_applied=? WHERE id=?',
            (time_out, total_hours, lunch_applied, record_id)
        )
        conn.commit()
        logger.info(f"Обновлена запись выхода: time_out={time_out}, total_hours={total_hours}")
        return True
    except Exception as e:
        logger.error(f"Ошибка при добавлении выхода: {e}")
        return False
    finally:
        conn.close()


# Добавление продолжительности обеда
def add_lunch_duration(user_id, date, lunch_duration):
    conn = sqlite3.connect('timesheet.db', check_same_thread=False)
    cursor = conn.cursor()
    try:
        # Находим активную запись
        record = get_active_record(user_id, date)
        if not record:
            logger.error(f"Не найдена активная запись для добавления обеда: user_id={user_id}, date={date}")
            return False

        record_id, time_in, current_lunch = record
        cursor.execute(
            'UPDATE records SET lunch_duration=? WHERE id=?',
            (lunch_duration, record_id)
        )
        conn.commit()
        logger.info(f"Добавлена продолжительность обеда: {lunch_duration} минут")
        return True
    except Exception as e:
        logger.error(f"Ошибка при добавлении обеда: {e}")
        return False
    finally:
        conn.close()


# Получение деталей за сегодня
def get_today_details(user_id):
    conn = sqlite3.connect('timesheet.db', check_same_thread=False)
    cursor = conn.cursor()
    current_date = datetime.now().strftime('%Y-%m-%d')

    cursor.execute(
        '''SELECT time_in, time_out, lunch_duration, total_hours, lunch_applied FROM records 
        WHERE user_id=? AND date=? ORDER BY time_in''',
        (user_id, current_date)
    )
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
    await update.message.reply_text(
        'Введите продолжительность обеда в минутах (например, 45):',
        reply_markup=ReplyKeyboardRemove()
    )
    return LUNCH_DURATION


# Сохранение времени входа
async def save_time_in(update, context):
    user_id = update.message.from_user.id
    current_date = datetime.now().strftime('%Y-%m-%d')
    time_in_str = update.message.text.strip()

    try:
        # Проверяем формат времени
        datetime.strptime(time_in_str, '%H:%M')

        # Проверяем, нет ли уже активной записи
        active_record = get_active_record(user_id, current_date)
        if active_record:
            await update.message.reply_text(
                'У вас уже есть незавершенный рабочий день. Сначала завершите его.',
                reply_markup=main_keyboard()
            )
            return ConversationHandler.END

        add_time_in(user_id, current_date, time_in_str)
        await update.message.reply_text(
            f'Время входа {time_in_str} сохранено!',
            reply_markup=main_keyboard()
        )
    except ValueError:
        await update.message.reply_text(
            'Неверный формат времени! Используйте ЧЧ:ММ (например, 09:00)'
        )
        return TIME_IN

    return ConversationHandler.END


# Сохранение времени выхода
async def save_time_out(update, context):
    user_id = update.message.from_user.id
    current_date = datetime.now().strftime('%Y-%m-%d')
    time_out_str = update.message.text.strip()

    try:
        # Проверяем формат времени
        datetime.strptime(time_out_str, '%H:%M')

        # Проверяем, есть ли активная запись
        active_record = get_active_record(user_id, current_date)
        if not active_record:
            await update.message.reply_text(
                'У вас нет активного рабочего дня. Сначала начните рабочий день.',
                reply_markup=main_keyboard()
            )
            return ConversationHandler.END

        # Сохраняем время выхода
        success = add_time_out(user_id, current_date, time_out_str)

        if success:
            # Получаем обновленные данные для отображения
            records = get_today_details(user_id)
            if records:
                # Берем последнюю запись
                time_in, time_out, lunch_duration, total_hours, lunch_applied = records[-1]

                message = f'✅ Время выхода сохранено!\n\n'
                message += f'⏰ Рабочее время: {time_in} - {time_out}\n'

                # Рассчитываем общее время без обеда для информации
                time_in_dt = datetime.strptime(time_in, '%H:%M')
                time_out_dt = datetime.strptime(time_out, '%H:%M')
                if time_out_dt < time_in_dt:
                    time_out_dt += timedelta(days=1)
                total_without_lunch = (time_out_dt - time_in_dt).total_seconds() / 3600

                if lunch_duration:
                    if lunch_applied:
                        message += f'🍽 Обед: {lunch_duration} мин. (учтен)\n'
                    else:
                        message += f'🍽 Обед: {lunch_duration} мин. (не учтен - работа < 4 часов)\n'

                message += f'📊 Итого отработано: {total_hours:.2f} часов'

                # Добавляем информацию о расчете
                if lunch_duration and not lunch_applied:
                    message += f'\n\nℹ️ Обед не вычитался, так как рабочее время ({total_without_lunch:.2f} ч) меньше 4 часов'

                await update.message.reply_text(message, reply_markup=main_keyboard())
            else:
                await update.message.reply_text(
                    'Время выхода сохранено!',
                    reply_markup=main_keyboard()
                )
        else:
            await update.message.reply_text(
                'Ошибка при сохранении времени выхода.',
                reply_markup=main_keyboard()
            )

    except ValueError:
        await update.message.reply_text(
            'Неверный формат времени! Используйте ЧЧ:ММ (например, 18:00)'
        )
        return TIME_OUT

    return ConversationHandler.END


# Сохранение продолжительности обеда
async def save_lunch_duration(update, context):
    user_id = update.message.from_user.id
    current_date = datetime.now().strftime('%Y-%m-%d')
    lunch_duration_str = update.message.text.strip()

    try:
        lunch_duration = int(lunch_duration_str)
        if lunch_duration <= 0:
            await update.message.reply_text('Продолжительность обеда должна быть положительным числом!')
            return LUNCH_DURATION

        # Проверяем, есть ли активная запись
        active_record = get_active_record(user_id, current_date)
        if not active_record:
            await update.message.reply_text(
                'У вас нет активного рабочего дня. Сначала начните рабочий день.',
                reply_markup=main_keyboard()
            )
            return ConversationHandler.END

        success = add_lunch_duration(user_id, current_date, lunch_duration)

        if success:
            await update.message.reply_text(
                f'🍽 Продолжительность обеда ({lunch_duration} минут) сохранена!\n'
                f'Обед будет вычтен из рабочего времени, только если вы отработаете более 4 часов.',
                reply_markup=main_keyboard()
            )
        else:
            await update.message.reply_text(
                'Ошибка при сохранении продолжительности обеда.',
                reply_markup=main_keyboard()
            )

    except ValueError:
        await update.message.reply_text('Неверный формат! Введите целое число минут.')
        return LUNCH_DURATION

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


# Генерация отчета за период
def generate_report(user_id, period):
    conn = sqlite3.connect('timesheet.db', check_same_thread=False)
    cursor = conn.cursor()

    if period == 'today':
        current_date = datetime.now().strftime('%Y-%m-%d')
        cursor.execute(
            '''SELECT SUM(total_hours) FROM records 
            WHERE user_id=? AND date=? AND total_hours IS NOT NULL''',
            (user_id, current_date)
        )
    elif period == 'week':
        start_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        cursor.execute(
            '''SELECT SUM(total_hours) FROM records 
            WHERE user_id=? AND date >= ? AND total_hours IS NOT NULL''',
            (user_id, start_date)
        )
    elif period == 'month':
        start_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
        cursor.execute(
            '''SELECT SUM(total_hours) FROM records 
            WHERE user_id=? AND date >= ? AND total_hours IS NOT NULL''',
            (user_id, start_date)
        )
    else:  # year
        start_date = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
        cursor.execute(
            '''SELECT SUM(total_hours) FROM records 
            WHERE user_id=? AND date >= ? AND total_hours IS NOT NULL''',
            (user_id, start_date)
        )

    result = cursor.fetchone()
    conn.close()
    return result[0] or 0


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
                message = f"📊 Отчет за сегодня ({datetime.now().strftime('%d.%m.%Y')}):\n\n"
                total_day_hours = 0

                for i, record in enumerate(details, 1):
                    time_in, time_out, lunch_duration, hours, lunch_applied = record
                    if time_out and hours is not None:
                        message += f"{i}. ⏰ {time_in} - {time_out}"
                        if lunch_duration:
                            if lunch_applied:
                                message += f" | 🍽 {lunch_duration} мин (учтен)"
                            else:
                                message += f" | 🍽 {lunch_duration} мин (не учтен)"
                        message += f" | {hours:.2f} ч\n"
                        total_day_hours += hours
                    else:
                        message += f"{i}. ⏰ {time_in} - --:-- | незавершенный вход\n"

                message += f"\nВсего за день: {total_day_hours:.2f} часов"
            else:
                message = "За сегодня нет записей о рабочем времени."
        else:
            message = f'📊 Отработано за {period_text}: {total_hours:.2f} часов'

        await update.message.reply_text(message, reply_markup=main_keyboard())
    else:
        await update.message.reply_text('Неверный период отчета')


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

    # ConversationHandler для входа/выхода/обеда
    time_conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex('^Вход$'), time_in),
            MessageHandler(filters.Regex('^Выход$'), time_out),
            MessageHandler(filters.Regex('^Обед$'), lunch)
        ],
        states={
            TIME_IN: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_time_in)],
            TIME_OUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_time_out)],
            LUNCH_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_lunch_duration)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(time_conv_handler)
    application.add_handler(MessageHandler(filters.Regex('^Отчет$'), report_menu))
    application.add_handler(
        MessageHandler(filters.Regex('^(Сегодня|Неделя|Месяц|Год|Назад)$'), generate_report_handler)
    )

    application.run_polling()


if __name__ == '__main__':
    main()