#!/usr/bin/env python3
"""
Анализ мусорных файлов и папок в Яндекс.Диске на основе БД сканирования.
"""
import sqlite3
import json
from collections import defaultdict
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'monitor.db')

# Паттерны мусорных папок и файлов
JUNK_PATTERNS = {
    'python_cache': ['__pycache__', '.pytest_cache', '.tox', '.mypy_cache', 'htmlcov', '.coverage'],
    'python_venv': ['.venv', 'venv', 'env', 'virtualenv', '.virtualenv', 'ENV'],
    'node_modules': ['node_modules'],
    'git': ['.git'],
    'build_artifacts': ['build', 'dist', '.egg-info', 'target', 'out', 'bin', 'obj'],
    'ide_settings': ['.vscode', '.idea', '.vs', '__MACOSX', '.DS_Store'],
    'temp_files': ['tmp', 'temp', '.tmp', '.cache'],
}

JUNK_FILE_EXTENSIONS = {
    'python_compiled': ['.pyc', '.pyo', '.pyd'],
    'compiled': ['.o', '.so', '.dll', '.dylib', '.class'],
    'logs': ['.log', '.log.gz', '.log.1'],
    'backups': ['.bak', '.backup', '~'],
}

def analyze_db(scan_id=5):
    """Анализирует БД и находит мусорные паттерны."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    results = {
        'scan_id': scan_id,
        'junk_folders': defaultdict(lambda: {'count': 0, 'total_size': 0, 'paths': []}),
        'junk_files': defaultdict(lambda: {'count': 0, 'total_size': 0, 'samples': []}),
        'large_folders': [],
        'statistics': {}
    }

    # 1. Поиск мусорных папок по паттернам
    for category, patterns in JUNK_PATTERNS.items():
        for pattern in patterns:
            # Поиск папок с точным совпадением имени
            rows = cursor.execute("""
                SELECT parent_path, name,
                       (SELECT COUNT(*) FROM files f2
                        WHERE f2.scan_id = ?
                        AND (f2.parent_path = files.parent_path || '/' || files.name
                             OR f2.parent_path LIKE files.parent_path || '/' || files.name || '/%')
                       ) as files_inside,
                       (SELECT SUM(size) FROM files f2
                        WHERE f2.scan_id = ?
                        AND (f2.parent_path = files.parent_path || '/' || files.name
                             OR f2.parent_path LIKE files.parent_path || '/' || files.name || '/%')
                       ) as total_size
                FROM files
                WHERE scan_id = ? AND type = 'dir' AND name = ?
                ORDER BY total_size DESC
                LIMIT 100
            """, (scan_id, scan_id, scan_id, pattern)).fetchall()

            for row in rows:
                full_path = f"{row['parent_path']}/{row['name']}".strip('/')
                size = row['total_size'] or 0
                results['junk_folders'][category]['count'] += 1
                results['junk_folders'][category]['total_size'] += size
                if len(results['junk_folders'][category]['paths']) < 20:
                    results['junk_folders'][category]['paths'].append({
                        'path': full_path,
                        'files': row['files_inside'],
                        'size_mb': round(size / 1024 / 1024, 2)
                    })

    # 2. Поиск мусорных файлов по расширениям
    for category, extensions in JUNK_FILE_EXTENSIONS.items():
        for ext in extensions:
            rows = cursor.execute("""
                SELECT name, size, parent_path
                FROM files
                WHERE scan_id = ? AND type = 'file' AND name LIKE ?
                ORDER BY size DESC
                LIMIT 100
            """, (scan_id, f'%{ext}')).fetchall()

            for row in rows:
                results['junk_files'][category]['count'] += 1
                results['junk_files'][category]['total_size'] += row['size'] or 0
                if len(results['junk_files'][category]['samples']) < 10:
                    results['junk_files'][category]['samples'].append({
                        'path': f"{row['parent_path']}/{row['name']}".strip('/'),
                        'size_mb': round((row['size'] or 0) / 1024 / 1024, 2)
                    })

    # 3. Топ самых тяжелых папок (верхнего уровня в /pro)
    rows = cursor.execute("""
        SELECT name,
               (SELECT COUNT(*) FROM files f2
                WHERE f2.scan_id = ?
                AND (f2.parent_path = '/pro/' || files.name
                     OR f2.parent_path LIKE '/pro/' || files.name || '/%')
               ) as files_inside,
               (SELECT SUM(size) FROM files f2
                WHERE f2.scan_id = ?
                AND (f2.parent_path = '/pro/' || files.name
                     OR f2.parent_path LIKE '/pro/' || files.name || '/%')
               ) as total_size
        FROM files
        WHERE scan_id = ? AND type = 'dir' AND parent_path = '/pro'
        ORDER BY total_size DESC
        LIMIT 30
    """, (scan_id, scan_id, scan_id)).fetchall()

    for row in rows:
        size = row['total_size'] or 0
        results['large_folders'].append({
            'name': row['name'],
            'files': row['files_inside'],
            'size_gb': round(size / 1024 / 1024 / 1024, 2)
        })

    # 4. Общая статистика
    total = cursor.execute("""
        SELECT COUNT(*) as count, SUM(size) as size
        FROM files WHERE scan_id = ?
    """, (scan_id,)).fetchone()

    results['statistics'] = {
        'total_files': total['count'],
        'total_size_gb': round((total['size'] or 0) / 1024 / 1024 / 1024, 2)
    }

    return results

def format_report(results):
    """Форматирует отчет в читаемый вид."""
    output = []
    output.append("=" * 80)
    output.append(f"АНАЛИЗ МУСОРНЫХ ФАЙЛОВ В ЯНДЕКС.ДИСКЕ (Scan #{results['scan_id']})")
    output.append("=" * 80)

    output.append(f"\n📊 ОБЩАЯ СТАТИСТИКА:")
    output.append(f"  Всего файлов в скане: {results['statistics']['total_files']:,}")
    output.append(f"  Общий размер: {results['statistics']['total_size_gb']:.2f} GB")

    output.append(f"\n🗑️  МУСОРНЫЕ ПАПКИ:")
    total_junk_size = 0
    total_junk_count = 0

    for category, data in sorted(results['junk_folders'].items(),
                                  key=lambda x: x[1]['total_size'], reverse=True):
        if data['count'] > 0:
            size_gb = data['total_size'] / 1024 / 1024 / 1024
            total_junk_size += data['total_size']
            total_junk_count += data['count']
            output.append(f"\n  [{category}]: {data['count']} папок, {size_gb:.2f} GB")
            for path_info in data['paths'][:5]:
                output.append(f"    - {path_info['path']} ({path_info['size_mb']:.1f} MB, {path_info['files']} файлов)")

    output.append(f"\n  💾 ИТОГО МУСОРНЫХ ПАПОК: {total_junk_count}, {total_junk_size/1024/1024/1024:.2f} GB")

    output.append(f"\n📄 МУСОРНЫЕ ФАЙЛЫ:")
    total_files_junk = 0
    total_files_size = 0

    for category, data in sorted(results['junk_files'].items(),
                                  key=lambda x: x[1]['total_size'], reverse=True):
        if data['count'] > 0:
            size_mb = data['total_size'] / 1024 / 1024
            total_files_junk += data['count']
            total_files_size += data['total_size']
            output.append(f"\n  [{category}]: {data['count']} файлов, {size_mb:.2f} MB")
            for sample in data['samples'][:3]:
                output.append(f"    - {sample['path']} ({sample['size_mb']:.2f} MB)")

    output.append(f"\n  💾 ИТОГО МУСОРНЫХ ФАЙЛОВ: {total_files_junk}, {total_files_size/1024/1024:.2f} MB")

    output.append(f"\n📁 ТОП-15 САМЫХ ТЯЖЕЛЫХ ПАПОК В /pro:")
    for folder in results['large_folders'][:15]:
        output.append(f"  {folder['name']:30} {folder['size_gb']:8.2f} GB  ({folder['files']:,} файлов)")

    output.append(f"\n" + "=" * 80)
    output.append(f"💡 РЕКОМЕНДАЦИИ ПО ОЧИСТКЕ:")
    output.append("=" * 80)

    if results['junk_folders']['python_venv']['count'] > 0:
        output.append("\n1. КРИТИЧНО: Удалить виртуальные окружения Python:")
        output.append("   - .venv, venv, env - их легко пересоздать локально")
        output.append(f"   - Экономия: ~{results['junk_folders']['python_venv']['total_size']/1024/1024/1024:.2f} GB")

    if results['junk_folders']['node_modules']['count'] > 0:
        output.append("\n2. КРИТИЧНО: Удалить node_modules:")
        output.append("   - Восстанавливаются через npm install")
        output.append(f"   - Экономия: ~{results['junk_folders']['node_modules']['total_size']/1024/1024/1024:.2f} GB")

    if results['junk_folders']['python_cache']['count'] > 0:
        output.append("\n3. Удалить кеш-папки Python:")
        output.append("   - __pycache__, .pytest_cache и т.п.")
        output.append(f"   - Экономия: ~{results['junk_folders']['python_cache']['total_size']/1024/1024:.2f} MB")

    if results['junk_folders']['git']['count'] > 0:
        output.append("\n4. ВНИМАНИЕ: Найдены .git папки:")
        output.append("   - Если проект не на GitHub/GitLab, оставьте .git")
        output.append("   - Если есть в remote репозитории, можно удалить")
        output.append(f"   - Потенциальная экономия: ~{results['junk_folders']['git']['total_size']/1024/1024/1024:.2f} GB")

    if results['junk_folders']['build_artifacts']['count'] > 0:
        output.append("\n5. Удалить артефакты сборки:")
        output.append("   - build/, dist/, target/ - пересоздаются при компиляции")
        output.append(f"   - Экономия: ~{results['junk_folders']['build_artifacts']['total_size']/1024/1024/1024:.2f} GB")

    total_savings = (
        results['junk_folders']['python_venv']['total_size'] +
        results['junk_folders']['node_modules']['total_size'] +
        results['junk_folders']['python_cache']['total_size'] +
        results['junk_folders']['build_artifacts']['total_size']
    ) / 1024 / 1024 / 1024

    output.append(f"\n💰 ОБЩАЯ ПОТЕНЦИАЛЬНАЯ ЭКОНОМИЯ: ~{total_savings:.2f} GB")
    output.append("=" * 80)

    return "\n".join(output)

def generate_cleanup_list(results, output_file='cleanup_paths.txt'):
    """Генерирует список путей для удаления."""
    paths = []

    # Собираем пути из критичных категорий
    critical_categories = ['python_venv', 'node_modules', 'python_cache', 'build_artifacts']

    for category in critical_categories:
        if category in results['junk_folders']:
            for path_info in results['junk_folders'][category]['paths']:
                paths.append(path_info['path'])

    with open(output_file, 'w') as f:
        f.write("# Список путей для удаления из Яндекс.Диска\n")
        f.write("# Создан автоматически анализом БД\n\n")
        for path in sorted(paths):
            f.write(f"{path}\n")

    return len(paths)

if __name__ == "__main__":
    import sys

    scan_id = 5  # По умолчанию последний скан
    if len(sys.argv) > 1:
        scan_id = int(sys.argv[1])

    print("🔍 Анализирую базу данных...")
    results = analyze_db(scan_id)

    print("\n" + format_report(results))

    # Сохраняем JSON для дальнейшего анализа
    with open('junk_analysis.json', 'w') as f:
        # Конвертируем defaultdict в обычный dict
        output_data = {
            'scan_id': results['scan_id'],
            'statistics': results['statistics'],
            'junk_folders': dict(results['junk_folders']),
            'junk_files': dict(results['junk_files']),
            'large_folders': results['large_folders']
        }
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Детальный JSON сохранен в junk_analysis.json")

    # Генерируем список путей для удаления
    paths_count = generate_cleanup_list(results)
    print(f"✅ Список путей для удаления сохранен в cleanup_paths.txt ({paths_count} путей)")
