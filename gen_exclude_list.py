#!/usr/bin/env python3
import urllib.request
import json
import sys
import os

# Настройки
KEEP_FOLDERS = ['pro']  # Папки, которые НУЖНО оставить (не исключать)
API_URL = "https://cloud-api.yandex.net/v1/disk/resources?path=/&limit=1000&fields=_embedded.items.name,_embedded.items.type"
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')

def load_token_from_env():
    """Читает токен из .env файла вручную, без внешних библиотек."""
    token = None
    if os.path.exists(ENV_FILE):
        try:
            with open(ENV_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    # Пропускаем комментарии и пустые строки
                    if not line or line.startswith('#'):
                        continue
                    if line.startswith('YANDEX_DISK_TOKEN='):
                        token = line.split('=', 1)[1].strip()
                        # Удаляем кавычки, если они есть
                        if (token.startswith('"') and token.endswith('"')) or \
                           (token.startswith("'" ) and token.endswith("'" )):
                            token = token[1:-1]
                        return token
        except Exception as e:
            print(f"Ошибка чтения .env файла: {e}")
    return token

def get_folders(token):
    headers = {
        "Authorization": f"OAuth {token}",
        "Accept": "application/json"
    }
    
    req = urllib.request.Request(API_URL, headers=headers)
    
    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
            items = data.get('_embedded', {}).get('items', [])
            
            # Фильтруем: оставляем только папки
            all_folders = [item['name'] for item in items if item['type'] == 'dir']
            
            # Формируем список исключений
            folders_to_exclude = set(all_folders) - set(KEEP_FOLDERS)
            return sorted(list(folders_to_exclude))
            
    except urllib.error.HTTPError as e:
        print(f"Ошибка API: {e.code} {e.reason}")
        if e.code == 401:
            print("Неверный токен. Проверьте файл .env")
        sys.exit(1)
    except Exception as e:
        print(f"Ошибка: {e}")
        sys.exit(1)

def main():
    # 1. Пробуем найти токен в .env
    token = load_token_from_env()

    # 2. Если нет в .env, смотрим аргументы командной строки (как запасной вариант)
    if not token and len(sys.argv) > 1:
        token = sys.argv[1]

    if not token or token == "вставь_свой_токен_сюда":
        print("ОШИБКА: Токен не найден.")
        print(f"\n1. Откройте файл: {ENV_FILE}")
        print("2. Вставьте туда токен: YANDEX_DISK_TOKEN=ваш_токен")
        print("3. Или передайте токен аргументом: python3 gen_exclude_list.py <TOKEN>")
        print("\nПолучить токен: https://yandex.ru/dev/disk/poligon/ (кнопка 'Получить OAuth-токен')")
        return

    print("✅ Токен найден, запрашиваем список папок...")
    exclude_list = get_folders(token)
    
    if exclude_list:
        config_str = ",".join(exclude_list)
        print("\n--- СКОПИРУЙТЕ ЭТУ СТРОКУ В КОНФИГ ---")
        print(f"exclude-dirs={config_str}")
        print("--------------------------------------")
        print(f"\nВсего папок в корне: {len(exclude_list) + len(KEEP_FOLDERS)}")
        print(f"Будет исключено: {len(exclude_list)}")
        print(f"Останется: {KEEP_FOLDERS}")
    else:
        print("Папок для исключения не найдено (или все папки в белом списке).")

if __name__ == "__main__":
    main()