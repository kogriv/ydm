#!/usr/bin/env python3
import os
import urllib.request
import urllib.parse
import time
import sys

# Конфигурация - paths relative to project root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIST_FILE = os.path.join(PROJECT_ROOT, 'var', 'junk_list.txt')
LOG_FILE = os.path.join(PROJECT_ROOT, 'var', 'deleted.log')
ENV_FILE = os.path.join(PROJECT_ROOT, '.env')

class YandexClient:
    API_URL = "https://cloud-api.yandex.net/v1/disk/resources"

    def __init__(self, token):
        self.headers = {"Authorization": f"OAuth {token}"}

    def delete(self, path):
        params = urllib.parse.urlencode({"path": path, "permanently": "false"})
        url = f"{self.API_URL}?{params}"
        req = urllib.request.Request(url, headers=self.headers, method="DELETE")
        
        try:
            with urllib.request.urlopen(req) as response:
                if response.status in [202, 204]:
                    return "OK"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "404" # Already gone
            if e.code == 429:
                return "429" # Rate limit
            return f"ERR_{e.code}"
        except Exception as e:
            return f"ERR_{str(e)}"
        return "UNKNOWN"

def load_token():
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            for line in f:
                if line.startswith('YANDEX_DISK_TOKEN='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'")
    return None

def load_done_set():
    done = set()
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r') as f:
            for line in f:
                done.add(line.strip())
    return done

def main():
    # 1. Проверки
    token = load_token()
    if not token:
        print("Error: No token in .env")
        return
    
    if not os.path.exists(LIST_FILE):
        print(f"Error: {LIST_FILE} not found. Run plan_cleanup.py first.")
        return

    # 2. Загрузка задач
    with open(LIST_FILE, 'r') as f:
        tasks = [line.strip() for line in f if line.strip()]
    
    done_set = load_done_set()
    
    todo = [t for t in tasks if t not in done_set]
    
    print(f"Total tasks: {len(tasks)}")
    print(f"Already done: {len(done_set)}")
    print(f"Remaining:    {len(todo)}")
    print("-" * 40)
    
    if not todo:
        print("Nothing to do!")
        return

    # 3. Исполнение
    client = YandexClient(token)
    log_handle = open(LOG_FILE, 'a', buffering=1) # Line buffering
    
    try:
        for i, path in enumerate(todo):
            status = client.delete(path)
            
            # Логика повторов при Rate Limit
            while status == "429":
                print(f"\nRate limit hit. Sleeping 5s...")
                time.sleep(5)
                status = client.delete(path)

            # Вывод
            progress = f"[{i+1}/{len(todo)}]"
            if status in ["OK", "404"]:
                # Успех
                log_handle.write(path + "\n")
                print(f"{progress} {status}: {path}")
            else:
                # Ошибка (не пишем в лог, чтобы повторить потом)
                print(f"{progress} FAILED ({status}): {path}")
            
            # Небольшая пауза чтобы не спамить
            time.sleep(0.1)
            
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    finally:
        log_handle.close()
        print("\nLog saved.")

if __name__ == "__main__":
    main()
