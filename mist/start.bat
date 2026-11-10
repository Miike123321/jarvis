@echo off
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Python не найден! Установите Python 3.10+ и добавьте его в PATH.
    pause
    exit /b
)

if not exist "venv" (
    echo Создание виртуального окружения venv...
    python -m venv venv
)

call venv\Scripts\activate
echo Проверка и установка зависимостей...
python -m pip install --upgrade pip
pip install -r requirements.txt

echo Запуск приложения...
streamlit run app.py
pause
