import urllib3
import requests

# Отключаем предупреждения SSL для локальных/внутренних сервисов
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

url = "https://pim.almi.odesa.ua/apollo/1cema.xml"

try:
  response = requests.get(url, verify=False, timeout=10)
  response.raise_for_status()

  # Сохраняем локально
  with open("1cema.xml", "w", encoding="utf-8") as f:
    f.write(response.text)

  print("Файл успешно скачан! Первые 500 символов:\n")
  print(response.text[:500])
except Exception as e:
  print(f"Ошибка при загрузке: {e}")