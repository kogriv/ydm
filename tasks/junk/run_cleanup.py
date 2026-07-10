#!/usr/bin/env python3
import argparse
import os
import subprocess
import urllib.request
import urllib.parse
import time
import sys

# Конфигурация - paths relative to project root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIST_FILE = os.path.join(PROJECT_ROOT, 'var', 'junk_list.txt')
LOG_FILE = os.path.join(PROJECT_ROOT, 'var', 'deleted.log')
ENV_FILE = os.path.join(PROJECT_ROOT, '.env')

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
from ydm import DEFAULT_CONFIG  # noqa: E402


class RcloneJunkClient:
    """--backend rclone counterpart of YandexClient: same .delete(path)
    interface/status codes (OK/404/429/ERR_*), so main()'s retry/resume/log
    logic below needs zero changes. Verified against the real yandex:
    remote (see tasks/rclone_backend/README.md, Этап 5): rclone's default
    delete behavior for this backend sends to Trash, same as the API's
    permanently=false — confirmed by inspecting trash:/ via the REST API
    after a test `rclone purge`.

    A path can be either a file or a directory (plan_cleanup.py mixes
    both) — `rclone deletefile` handles files but errors ambiguously
    ("is a directory or doesn't exist") for both directories AND
    already-gone paths, so on that error we fall back to `rclone purge`,
    which *does* give a distinguishable 404 for genuinely-missing paths.
    """

    def __init__(self, remote):
        self.remote = remote

    def delete(self, path):
        remote_path = f"{self.remote}:{path.lstrip('/')}"
        result = subprocess.run(
            ["rclone", "deletefile", remote_path],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return "OK"

        stderr = result.stderr
        if "is a directory" in stderr or "doesn't exist" in stderr:
            purge_result = subprocess.run(
                ["rclone", "purge", remote_path],
                capture_output=True, text=True,
            )
            if purge_result.returncode == 0:
                return "OK"
            purge_stderr = purge_result.stderr
            if "404" in purge_stderr or "not found" in purge_stderr.lower():
                return "404"
            if "429" in purge_stderr or "too many requests" in purge_stderr.lower():
                return "429"
            return f"ERR_{purge_stderr.strip().splitlines()[-1][:200]}"

        if "429" in stderr or "too many requests" in stderr.lower():
            return "429"
        return f"ERR_{stderr.strip().splitlines()[-1][:200]}"


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

def parse_args():
    parser = argparse.ArgumentParser(description="Execute the junk cleanup plan (delete via API or rclone)")
    parser.add_argument("--backend", choices=["api", "rclone"], default="api",
                         help="'api' uses YANDEX_DISK_TOKEN/.env (default, unchanged behavior); "
                              "'rclone' uses the rclone.conf remote instead "
                              "(see tasks/rclone_backend/README.md)")
    parser.add_argument("--remote", default=DEFAULT_CONFIG["rclone_remote"],
                         help="rclone remote name (--backend rclone only)")
    return parser.parse_args()


def main():
    args = parse_args()

    # 1. Проверки
    if args.backend == "rclone":
        client = RcloneJunkClient(args.remote)
    else:
        token = load_token()
        if not token:
            print("Error: No token in .env")
            return
        client = YandexClient(token)

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
