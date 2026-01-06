#!/usr/bin/env python3
import sqlite3
import os
import urllib.request
import urllib.parse
import json
import time

DB_PATH = '/home/kogriv/infra/ya_disk/tools/monitor.db'
ENV_PATH = '/home/kogriv/infra/ya_disk/tools/.env'

def load_token():
    if os.path.exists(ENV_PATH):
        try:
            with open(ENV_PATH, 'r') as f:
                for line in f:
                    if line.startswith('YANDEX_DISK_TOKEN='):
                        return line.split('=', 1)[1].strip().strip('"').strip("'")
        except: pass
    return None

class YandexCleaner:
    API_URL = "https://cloud-api.yandex.net/v1/disk/resources"

    def __init__(self, token):
        self.token = token
        self.headers = {
            "Authorization": f"OAuth {token}"
        }

    def delete_path(self, path):
        # Удаление через API
        params = urllib.parse.urlencode({"path": path, "permanently": "false"})
        url = f"{self.API_URL}?{params}"
        req = urllib.request.Request(url, headers=self.headers, method="DELETE")
        
        try:
            with urllib.request.urlopen(req) as response:
                if response.status in [202, 204]:
                    return "DELETED"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "ALREADY GONE (404)"
            return f"ERROR {e.code}: {e.read().decode('utf-8')}"
        except Exception as e:
            return f"ERROR: {str(e)}"
        
        return "UNKNOWN"

def reduce_paths(paths):
    """
    Оставляет только самые верхние уровни вложенности.
    Если есть '/a' и '/a/b', оставит только '/a'.
    """
    if not paths:
        return []
    
    # Сортировка по алфавиту гарантирует, что родитель идет перед детьми
    # '/folder' будет перед '/folder/sub'
    sorted_paths = sorted(list(paths))
    
    kept_paths = []
    last_path = None
    
    for path in sorted_paths:
        if last_path and (path == last_path or path.startswith(last_path + "/")):
            continue # Пропускаем, так как это подпапка уже сохраненного родителя
        
        kept_paths.append(path)
        last_path = path
        
    return kept_paths

def get_junk_paths(cursor, scan_id):
    paths_to_delete = set()
    
    print("Collecting junk paths from DB...")

    # 1. Папки по именам (Exact Match)
    dir_names = [
        'vcpkg', 'node_modules', '.venv', 'venv', 
        '.git', '__pycache__', 'bin', 'obj'
    ]
    query_dirs = " OR ".join([f"name = '{n}'" for n in dir_names])
    
    rows = cursor.execute(f"""
        SELECT parent_path, name 
        FROM files 
        WHERE scan_id = ? AND type = 'dir' AND ({query_dirs})
    """, (scan_id,)).fetchall()
    
    for p, n in rows:
        full_path = f"{p}/{n}".replace("//", "/")
        paths_to_delete.add(full_path)

    # 2. Файлы по маскам (Extensions)
    ext_names = ["%.tmp", "%.log"]
    query_ext = " OR ".join([f"name LIKE '{e}'" for e in ext_names])
    
    rows_files = cursor.execute(f"""
        SELECT parent_path, name 
        FROM files 
        WHERE scan_id = ? AND type = 'file' AND ({query_ext})
    """, (scan_id,)).fetchall()

    for p, n in rows_files:
        full_path = f"{p}/{n}".replace("//", "/")
        paths_to_delete.add(full_path)

    return paths_to_delete

def main():
    token = load_token()
    if not token:
        print("Error: Token not found!")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    scan = cursor.execute("SELECT id FROM scans WHERE scan_type='cloud' ORDER BY id DESC LIMIT 1").fetchone()
    if not scan:
        print("No cloud scan found.")
        return
    scan_id = scan[0]

    raw_paths = get_junk_paths(cursor, scan_id)
    print(f"Total raw junk items found: {len(raw_paths)}")
    
    # ПРИМЕНЯЕМ ФИЛЬТРАЦИЮ
    optimized_list = reduce_paths(raw_paths)
    print(f"Optimized to top-level items: {len(optimized_list)}")
    
    conn.close()

    cleaner = YandexCleaner(token)
    
    counter = 0
    
    for path in optimized_list:
        counter += 1
        res = cleaner.delete_path(path)
        
        print(f"[{counter}/{len(optimized_list)}] {path} -> {res}")
        
        # Небольшая задержка
        if "DELETED" in res or "ERROR" in res:
             time.sleep(0.1) 

    print("-" * 40)
    print("Cleanup Complete.")

if __name__ == "__main__":
    main()