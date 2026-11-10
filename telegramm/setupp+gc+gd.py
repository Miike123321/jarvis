# -*- coding: utf-8 -*-

import os.path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Нужны права только на чтение/поиск в Drive
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

def main():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            # Если вы меняете SCOPES, удалите старый файл token.json
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())

    try:
        drive_service = build('drive', 'v3', credentials=creds)

        # --- БЛОК ПОИСКА ФАЙЛА ---
        file_name_to_find = "Развитие Интернет Магазина"

        # Формируем запрос: ищем по точному имени, типу "Google Документ" и исключаем файлы из корзины.
        query = f"name='{file_name_to_find}' and mimeType='application/vnd.google-apps.document' and trashed=false"
        
        response = drive_service.files().list(
            q=query,
            spaces='drive',
            fields='files(id, name)'
        ).execute()
        
        files_found = response.get('files', [])

        if not files_found:
            print(f"Файл с названием '{file_name_to_find}' не найден.")
            print("Проверьте, пожалуйста, точное название файла и что это Google Документ.")
        else:
            print(f"Найдены следующие файлы:")
            for file in files_found:
                print(f"  - Имя: {file.get('name')}, ID: {file.get('id')}")

    except HttpError as error:
        print(f'Произошла ошибка: {error}')

if __name__ == '__main__':
    main()