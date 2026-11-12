"""Супервизор Ады: перезапускает main.py при падении с нарастающей паузой.

Запуск: python run.py
- 0 → процесс завершился штатно (SystemExit/техработы) — не перезапускаем.
- Иначе экспоненциальная пауза: 5s, 15s, 30s, 60s, максимум 120s.
- Пять падений подряд без успешного запуска → остановка с ошибкой.
"""

import subprocess
import sys
import time

MAX_CONSECUTIVE_CRASHES = 5
BASE_DELAY = 5
MAX_DELAY = 120


def main() -> None:
    crashes = 0
    delay = BASE_DELAY
    while crashes < MAX_CONSECUTIVE_CRASHES:
        print(f"[supervisor] Запуск Ады (попытка {crashes + 1})...")
        start = time.time()
        result = subprocess.run([sys.executable, "main.py"])
        uptime = time.time() - start
        if result.returncode == 0:
            print("[supervisor] Ада завершилась штатно; супервизор останавливается.")
            return
        crashes += 1
        print(f"[supervisor] Ада упала (код {result.returncode}) после {uptime:.0f}s; перезапуск через {delay}s.")
        time.sleep(delay)
        delay = min(MAX_DELAY, max(BASE_DELAY, int(delay * 2)))
        if uptime > 300:
            # Процесс жил достаточно долго — считаем старт успешным.
            crashes = 1
            delay = BASE_DELAY
    print("[supervisor] Слишком много падений подряд; супервизор останавливается.")
    sys.exit(1)


if __name__ == "__main__":
    main()
