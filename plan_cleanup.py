#!/usr/bin/env python3
import sqlite3
import os

DB_PATH = '/home/kogriv/infra/ya_disk/tools/monitor.db'
OUTPUT_FILE = '/home/kogriv/infra/ya_disk/tools/junk_list.txt'

def reduce_paths(paths):
    """
    Оставляет только самые верхние уровни вложенности.
    Возвращает: (оставленные_пути, отброшенные_пути)
    """
    if not paths:
        return [], []
    
    # Сортировка важна! Родитель всегда будет раньше ребенка.
    sorted_paths = sorted(list(paths))
    
    kept_paths = []
    dropped_paths = []
    
    last_kept = None
    
    for path in sorted_paths:
        # Если у нас есть сохраненный родитель, и текущий путь лежит внутри него
        if last_kept and (path == last_kept or path.startswith(last_kept + "/")):
            dropped_paths.append(path)
            continue 
        
        kept_paths.append(path)
        last_kept = path
        
    return kept_paths, dropped_paths

def get_junk_paths(cursor, scan_id):
    paths_to_delete = set()
    print("Collecting paths from DB...")

    # 1. Папки с ТОЧНЫМ совпадением имени
    exact_dir_names = [
        'vcpkg', 'node_modules', '.git', '__pycache__', 'bin', 'obj'
    ]
    query_exact = " OR ".join([f"name = '{n}'" for n in exact_dir_names])

    rows = cursor.execute(f"""
        SELECT parent_path, name
        FROM files
        WHERE scan_id = ? AND type = 'dir' AND ({query_exact})
    """, (scan_id,)).fetchall()

    print(f"Found {len(rows)} exact match dirs")
    for p, n in rows:
        full_path = f"{p}/{n}".replace("//", "/")
        paths_to_delete.add(full_path)

    # 2. Папки виртуальных окружений (по ПАТТЕРНУ)
    # Ловим: venv, venv_*, .venv, .venv_*, virtualenv, virtualenv_*, ENV, ENV_*
    venv_patterns = ['venv%', '.venv%', 'virtualenv%', 'ENV%']
    query_venv = " OR ".join([f"name LIKE '{p}'" for p in venv_patterns])

    rows_venv = cursor.execute(f"""
        SELECT parent_path, name
        FROM files
        WHERE scan_id = ? AND type = 'dir' AND ({query_venv})
    """, (scan_id,)).fetchall()

    print(f"Found {len(rows_venv)} venv pattern dirs")
    for p, n in rows_venv:
        full_path = f"{p}/{n}".replace("//", "/")
        paths_to_delete.add(full_path)

    # 3. Файлы по расширениям
    ext_names = ["%.tmp", "%.log"]
    query_ext = " OR ".join([f"name LIKE '{e}'" for e in ext_names])

    rows_files = cursor.execute(f"""
        SELECT parent_path, name
        FROM files
        WHERE scan_id = ? AND type = 'file' AND ({query_ext})
    """, (scan_id,)).fetchall()

    print(f"Found {len(rows_files)} junk files")
    for p, n in rows_files:
        full_path = f"{p}/{n}".replace("//", "/")
        paths_to_delete.add(full_path)

    return paths_to_delete

def main():
    if not os.path.exists(DB_PATH):
        print(f"Error: DB not found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Ищем последний CLOUD скан (независимо от статуса, главное чтобы файлы были)
    scan = cursor.execute("SELECT id FROM scans WHERE scan_type='cloud' ORDER BY id DESC LIMIT 1").fetchone()
    if not scan:
        print("No cloud scan found.")
        return
    scan_id = scan[0]
    print(f"Using Scan ID: {scan_id}")

    # Сбор
    raw_paths = get_junk_paths(cursor, scan_id)
    print(f"Total RAW matches found: {len(raw_paths)}")
    
    # Оптимизация
    optimized_list, dropped_list = reduce_paths(raw_paths)
    
    print("-" * 50)
    print(f"Optimized count: {len(optimized_list)}")
    print(f"Dropped count:   {len(dropped_list)}")
    print("-" * 50)

    # Демонстрация примеров отброшенного
    if dropped_list:
        print("EXAMPLES OF DROPPED PATHS (Nested inside others):")
        for i, path in enumerate(dropped_list[:5]):
            # Найдем родителя, из-за которого отбросили
            # Это не супер-эффективно, но для лога ок
            parent = next((p for p in optimized_list if path.startswith(p + "/")), "UNKNOWN")
            print(f"Dropped: {path}")
            print(f"  -> Because of Parent: {parent}")
        if len(dropped_list) > 5:
            print("...")
    else:
        print("No nested paths found. All paths are distinct roots.")

    # Сохранение
    with open(OUTPUT_FILE, 'w') as f:
        for path in optimized_list:
            f.write(path + '\n')
            
    print("-" * 50)
    print(f"List saved to: {OUTPUT_FILE}")

    conn.close()

if __name__ == "__main__":
    main()
