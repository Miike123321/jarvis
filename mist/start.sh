#!/bin/bash

# 1. Проверка наличия Python 3
if ! command -v python3 &> /dev/null; then
    echo "Python 3 не найден! Установите Python 3.10+ перед запуском."
    exit 1
fi

# 2. Создание виртуального окружения venv, если его нет
if [ ! -d "venv" ]; then
    echo "Создание виртуального окружения venv..."
    python3 -m venv venv
fi

# 3. Активация окружения и установка зависимостей
source venv/bin/activate
echo "Проверка и установка зависимостей из requirements.txt..."
pip install --upgrade pip
pip install -r requirements.txt

# 4. Запуск приложения
echo "Запуск приложения Streamlit..."
streamlit run app.py
