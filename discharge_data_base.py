import sqlite3


"""Выводит все данные из всех таблиц в консоль"""
with sqlite3.connect('timesheet.db') as conn:
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Получаем список всех таблиц
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = cursor.fetchall()

    print("\n" + "=" * 50)
    print("ПОЛНЫЙ ДАМП БАЗЫ ДАННЫХ")
    print("=" * 50)

    for table in tables:
        table_name = table['name']
        print(f"\nТаблица: {table_name}")
        print("-" * 50)

        # Получаем данные из таблицы
        cursor.execute(f"SELECT * FROM {table_name}")
        rows = cursor.fetchall()

        # Выводим заголовки столбцов
        if rows:
            columns = rows[0].keys()
            print(" | ".join(columns))
            print("-" * 50)

            # Выводим данные
            for row in rows:
                print(" | ".join(str(row[col]) for col in columns))
        else:
            print("Таблица пуста")