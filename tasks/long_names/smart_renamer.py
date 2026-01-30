#!/usr/bin/env python3
import sqlite3
import json
import os
import sys
import time
import urllib.request
import urllib.parse
import re
from datetime import datetime

# --- Configuration ---
# Пути относительны к расположению скрипта: /data/pro/ydm/tasks/long_names/
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, "../../"))
DB_PATH = os.path.join(PROJECT_ROOT, "monitor.db")
VAR_DIR = os.path.join(PROJECT_ROOT, "var")
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")

# Настройки AI
OLLAMA_API_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "qwen2.5:7b"

# Настройки фильтрации
BYTE_LIMIT = 200  # Лимит байт, после которого начинаем беспокоиться (ext4 = 255)
EXCLUDED_PATTERNS = ['_files', '.tmp', '__MACOSX', '.git', '.vscode', '.idea']

class OllamaClient:
    """Simple wrapper for local Ollama API using standard library."""
    def __init__(self, model=MODEL_NAME):
        self.model = model
        self.api_url = OLLAMA_API_URL

    def generate_short_name(self, filename):
        prompt = f"""Task: Extremely shorten the filename to under 50 characters.
Rules:
1. Use ONLY lowercase English letters, numbers, and underscores (strict ASCII). 
2. NEVER use Russian/Cyrillic characters.
3. Keep the original file extension.
4. Keep only the most important 2-3 keywords and the year/volume if present.
5. Use snake_case.
6. OUTPUT ONLY THE NEW FILENAME. NO MARKDOWN. NO EXPLANATIONS.

Original: "{filename}"
New name:"""

        data = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.3  # Deterministic output
            }
        }

        try:
            req = urllib.request.Request(
                self.api_url, 
                data=json.dumps(data).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )
            with urllib.request.urlopen(req) as response:
                result = json.loads(response.read().decode('utf-8'))
                new_name = result.get('response', '').strip()
                # Cleanup potential AI chatty output
                new_name = new_name.split('\n')[0].strip('`"\' ')
                return new_name
        except Exception as e:
            print(f"  [AI Error] {e}", file=sys.stderr)
            return None

class YandexClientMinimal:
    """Minimal Yandex Disk Client for renaming."""
    API_URL = "https://cloud-api.yandex.net/v1/disk"

    def __init__(self, token):
        self.token = token
        self.headers = {
            "Authorization": f"OAuth {token}",
            "Accept": "application/json"
        }

    def move(self, old_path, new_path):
        params = urllib.parse.urlencode({
            "from": old_path,
            "path": new_path,
            "overwrite": "false"
        })
        url = f"{self.API_URL}/resources/move?{params}"
        req = urllib.request.Request(url, headers=self.headers, method='POST')
        
        try:
            with urllib.request.urlopen(req) as response:
                if response.status in (201, 202):
                    return True
                return False
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"  [API Error] Source not found: {old_path}")
            elif e.code == 409:
                print(f"  [API Error] Target already exists: {new_path}")
            elif e.code == 429:
                print(f"  [API Error] Rate limit exceeded!")
                raise e # Let caller handle backoff
            else:
                print(f"  [API Error] {e}")
            return False

def load_token():
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, 'r') as f:
            for line in f:
                if line.startswith('YANDEX_DISK_TOKEN='):
                    return line.split('=', 1)[1].strip().strip('"\'')
    return os.environ.get('YANDEX_DISK_TOKEN')

def get_long_files(db_path, byte_limit=200, scan_id=44, max_files=500):
    if not os.path.exists(db_path):
        print(f"Database not found: {db_path}")
        return []
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    # Find files where name length in bytes > limit
    query = """
    SELECT parent_path, name, length(cast(name as blob)) as bytes, type
    FROM files
    WHERE scan_id = ? AND bytes > ? AND type IN ('file', 'dir')
    ORDER BY bytes DESC
    LIMIT ?
    """
    cursor.execute(query, (scan_id, byte_limit, max_files))
    results = cursor.fetchall()
    conn.close()
    return results

def should_skip(parent_path, name):
    full_path = f"{parent_path}/{name}"
    for pattern in EXCLUDED_PATTERNS:
        if pattern in full_path:
            return True
    return False

def build_path(parent, name):
    """Normalize path construction to avoid double slashes."""
    parent = parent.rstrip('/')
    if parent == '' or parent == '/':
        return f"/{name}"
    return f"{parent}/{name}"

def ensure_unique(new_name, parent_path, used_names_in_folder):
    """Prevents collisions in the same folder during planning."""
    base, ext = os.path.splitext(new_name)
    counter = 1
    candidate = new_name
    
    # Check against the plan's history for this folder
    while candidate in used_names_in_folder:
        candidate = f"{base}_{counter}{ext}"
        counter += 1
    
    used_names_in_folder.add(candidate)
    return candidate

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Smart Renamer for Long Filenames")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Generate plan only (default)")
    parser.add_argument("--apply", action="store_true", help="Execute the plan (requires --plan-file)")
    parser.add_argument("--plan-file", help="Path to JSON plan file to execute")
    parser.add_argument("--limit", type=int, default=10, help="Max files to process in discovery")
    parser.add_argument("--scan-id", type=int, default=44, help="Scan ID to use (default: 44)")
    args = parser.parse_args()

    # --- EXECUTION MODE ---
    if args.apply:
        if not args.plan_file:
            print("Error: --apply requires --plan-file")
            sys.exit(1)
        
        token = load_token()
        if not token:
            print("Error: YANDEX_DISK_TOKEN not found in .env")
            sys.exit(1)
            
        client = YandexClientMinimal(token)
        
        with open(args.plan_file, 'r') as f:
            plan = json.load(f)
            
        print(f"Loaded plan with {len(plan)} items.")
        success_count = 0
        success_log = []
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        for item in plan:
            old_path = build_path(item['parent'], item['old_name'])
            new_path = build_path(item['parent'], item['new_name'])
            
            # Check if already renamed (new name exists)
            check_url = f"{client.API_URL}/resources?path={urllib.parse.quote(new_path.encode('utf-8'))}"
            check_req = urllib.request.Request(check_url, headers=client.headers)
            try:
                with urllib.request.urlopen(check_req) as check_resp:
                    print(f"Already renamed: {item['new_name'][:50]}... (skipping)")
                    success_count += 1
                    success_log.append({
                        "timestamp": datetime.now().isoformat(),
                        "old_path": old_path,
                        "new_path": new_path,
                        "status": "already_renamed"
                    })
                    continue
            except urllib.error.HTTPError:
                pass  # File doesn't exist with new name, proceed with rename
            
            print(f"Renaming: {item['old_name'][:50]}... -> {item['new_name']}")
            try:
                # 429 Retry logic with exponential backoff
                retries = 3
                while retries > 0:
                    try:
                        ok = client.move(old_path, new_path)
                        if ok:
                            print("  -> Success")
                            success_count += 1
                            success_log.append({
                                "timestamp": datetime.now().isoformat(),
                                "old_path": old_path,
                                "new_path": new_path
                            })
                        break # Break retry loop on success or non-429 error
                    except urllib.error.HTTPError as e:
                        if e.code == 429:
                            wait_time = 10 * (2 ** (3 - retries))  # 10s, 20s, 40s
                            print(f"  -> Rate limit. Waiting {wait_time}s...")
                            time.sleep(wait_time)
                            retries -= 1
                        else:
                            break
            except Exception as e:
                print(f"  -> Failed: {e}")
            
            time.sleep(0.5) # Gentle rate limit
        
        # Save success log for potential rollback
        if success_log:
            log_file = os.path.join(VAR_DIR, f"rename_success_{timestamp}.json")
            with open(log_file, 'w', encoding='utf-8') as f:
                json.dump(success_log, f, indent=2, ensure_ascii=False)
            print(f"\nSuccess log saved to: {log_file}")
            
        print(f"\nDone. Successfully renamed: {success_count}/{len(plan)}")
        sys.exit(0)

    # --- PLANNING MODE ---
    print(f"Scanning for long files (> {BYTE_LIMIT} bytes) in {DB_PATH} using scan #{args.scan_id}...")
    long_files = get_long_files(DB_PATH, BYTE_LIMIT, args.scan_id, args.limit)
    
    if not long_files:
        print("No long files found.")
        sys.exit(0)

    # Test Ollama availability before starting
    ai = OllamaClient()
    print("Testing Ollama connection...")
    try:
        test_result = ai.generate_short_name("test_file.pdf")
        if not test_result:
            print("Error: Ollama is not responding. Start it with: ollama serve")
            sys.exit(1)
        print(f"Ollama OK (model: {MODEL_NAME})")
    except Exception as e:
        print(f"Error: Cannot connect to Ollama at {OLLAMA_API_URL}")
        print(f"Details: {e}")
        print("Start Ollama with: ollama serve")
        sys.exit(1)
    
    plan = []
    # Registry to track name collisions per folder: {parent_path: {set_of_new_names}}
    folder_registries = {}

    print(f"Found {len(long_files)} candidates. Processing top {args.limit}...")
    
    count = 0
    for parent, name, size, obj_type in long_files:
        if count >= args.limit:
            break
            
        if should_skip(parent, name):
            print(f"[SKIP] {name[:30]}... (Matched exclude pattern)")
            continue

        print(f"\n[{count+1}] Processing {obj_type.upper()} ({size} bytes):")
        print(f"   Old: {name}")
        
        # Init registry for this folder if needed
        if parent not in folder_registries:
            folder_registries[parent] = set()

        # AI Generation
        short_name = ai.generate_short_name(name)
        
        if not short_name:
            print("   -> AI Failed")
            continue
        
        # Validate AI result length
        new_name_bytes = len(short_name.encode('utf-8'))
        if new_name_bytes > 200:  # Safety margin (ext4 limit is 255)
            print(f"   -> AI returned too long name ({new_name_bytes} bytes), skipping")
            continue
            
        # Validation & Uniqueness
        final_name = ensure_unique(short_name, parent, folder_registries[parent])
        
        print(f"   New: {final_name}")
        
        plan.append({
            "type": obj_type,
            "parent": parent,
            "old_name": name,
            "new_name": final_name,
            "bytes_old": size,
            "bytes_new": len(final_name.encode('utf-8'))
        })
        count += 1

    # Save Plan
    if not os.path.exists(VAR_DIR):
        os.makedirs(VAR_DIR)
        
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    plan_file = os.path.join(VAR_DIR, f"rename_plan_{timestamp}.json")
    
    with open(plan_file, 'w', encoding='utf-8') as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)
    
    # Summary statistics
    if plan:
        total_bytes_old = sum(item['bytes_old'] for item in plan)
        total_bytes_new = sum(item['bytes_new'] for item in plan)
        savings = total_bytes_old - total_bytes_new
        
        print(f"\n{'='*60}")
        print(f"Plan Summary")
        print(f"{'='*60}")
        print(f"Files to rename: {len(plan)}")
        print(f"Total bytes old: {total_bytes_old:,}")
        print(f"Total bytes new: {total_bytes_new:,}")
        print(f"Bytes saved:      {savings:,} ({100*savings/total_bytes_old:.1f}%)")
        print(f"{'='*60}")
        
    print(f"\nPlan saved to: {plan_file}")
    print(f"Review it, then run:\npython3 smart_renamer.py --apply --plan-file {plan_file}")

if __name__ == "__main__":
    main()
