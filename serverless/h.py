import base64
import os
import glob
from loguru import logger
from datetime import datetime, timedelta
import json
from pathlib import Path
import traceback
import shutil
import subprocess

from typing import List
from typing import Union

from tqdm import tqdm

import const

TMP_DIR_NAME = "tmp"


def delete_old_files(directory, file_mask, days, recursive=False):
    """
    Удаляет файлы и каталоги, соответствующие заданной маске, если они
    были изменены более указанного количества дней назад.

    Параметры:
        directory (str): Путь к директории, в которой искать файлы и каталоги.
        file_mask (str): Маска имени (например, '*.log', '*_tmp_*').
        days (int): Количество дней; элементы, изменённые более чем `days` дней назад, будут удалены.
        recursive (bool): Если True, поиск будет выполняться рекурсивно по подкаталогам.

    Исключения:
        ValueError: Если указанный путь не является директорией.
        Логируются ошибки, возникшие при попытке удалить отдельные элементы.

    Поведение:
        - Находит файлы и каталоги по заданной маске.
        - Проверяет дату последнего изменения каждого найденного элемента.
        - Удаляет файлы и каталоги (со всем содержимым), если они старше
          заданного порога времени.
        - Пропускает элементы, не являющиеся файлами или каталогами.
        - Логирует общее количество удалённых файлов и каталогов.
    """
    try:
        # Проверка входных данных
        if not os.path.isdir(directory):
            raise ValueError(f"The specified path '{directory}' is not a directory.")

        # Расчёт времени отсечения
        cutoff_time = datetime.now() - timedelta(days=days)

        # Составление пути с маской
        search_pattern = os.path.join(directory, "**", file_mask) if recursive else os.path.join(directory, file_mask)

        # Поиск файлов и каталогов
        files = glob.glob(search_pattern, recursive=recursive)
        # logger.info(f"Found items: {len(files)}")

        deleted_files = 0
        deleted_dirs = 0
        for file_path in files:
            try:
                # Каталог
                if os.path.isdir(file_path):
                    dir_mod_time = datetime.fromtimestamp(os.path.getmtime(file_path))
                    if dir_mod_time < cutoff_time:
                        shutil.rmtree(file_path)
                        logger.debug(f"Deleted directory: {file_path}")
                        deleted_dirs += 1
                    continue

                # Файл
                if os.path.isfile(file_path):
                    file_mod_time = datetime.fromtimestamp(os.path.getmtime(file_path))
                    if file_mod_time < cutoff_time:
                        os.remove(file_path)
                        logger.debug(f"Deleted file: {file_path}")
                        deleted_files += 1
                    continue

                logger.info(f"Skip: '{file_path}' is not a file or directory.")
            except Exception as e:
                logger.error(f"Error during file processing '{file_path}': {e}")
        if deleted_files > 0 or deleted_dirs > 0:
            logger.info(
                f"From {directory} deleted {deleted_files} old files and {deleted_dirs} old directories."
            )
    except Exception as e:
        logger.error(f"Function Execution Error: {e}")


def clean_folder(directory, days):
    """
    Очищает указанную папку от временных и промежуточных файлов,
    удаляя файлы по определённым маскам, если они были изменены
    более `days` дней назад.

    Параметры:
        directory (str): Путь к директории, в которой выполнять очистку.
        days (int): Количество дней; файлы, изменённые более чем `days` дней назад, будут удалены.

    Поведение:
        - Если в пути присутствует подстрока 'output', дополнительно удаляются файлы по маске '*_face*'.
        - Независимо от условия, удаляются файлы по маскам:
            - 'tmp_*' (временные файлы)
            - '*_temp_*' (промежуточные или временные данные)
        - Удаление выполняется с помощью функции `delete_old_files`, которая учитывает дату последнего изменения файла.
    """
    if 'output' in directory:
        mask = '*_face*'
        delete_old_files(directory=directory, file_mask=mask, days=days)
    mask = 'tmp_*'
    delete_old_files(directory=directory, file_mask=mask, days=days)
    mask = '*_temp_*'
    delete_old_files(directory=directory, file_mask=mask, days=days)
    if 'tmp' in directory:
        mask = 'lora_*' # картинки от обучения wan_lora
        delete_old_files(directory=directory, file_mask=mask, days=days)
        mask = 'wan_*' # удаляем остатки от обучения wan_lora
        delete_old_files(directory=directory, file_mask=mask, days=days)


def write_variables_to_json(variables, filename):
    """
    Сохраняет переданные переменные в JSON-файл, исключая служебные (внутренние) переменные.

    Параметры:
        variables (dict): Словарь переменных, например, из `locals()` или `globals()`.
        filename (str): Путь к файлу, в который будет произведена запись.

    Поведение:
        - Из словаря исключаются ключи, начинающиеся с '__' (служебные переменные).
        - Оставшиеся переменные сериализуются и сохраняются в JSON-файл.
        - Значения приводятся к строковому представлению при необходимости (`default=str`).
        - В случае ошибки при записи логируется сообщение с деталями исключения.

    Примечания:
        - Кодировка файла: UTF-8.
        - Формат JSON: читаемый (отступ 4 пробела, без экранирования юникода).
    # Получение всех глобальных или локальных переменных и запись их в файл
    # write_variables_to_json( globals() , 'globals.json' )
    # write_variables_to_json( locals() , 'locals.json' )
    """
    # Фильтрация только пользовательских переменных (исключение служебных)
    user_variables = {key: value for key, value in variables.items() if not key.startswith("__")}
    # Запись переменных в формате JSON
    try:
        with open(filename, "w", encoding="utf-8") as file:
            json.dump(user_variables, file, ensure_ascii=False, indent=4, default=str)
        # print(f"Данные успешно сохранены в файл: {filename}")
    except Exception as e:
        logger.error(f"Error write (filename {filename}): {e}")


def _read_text_file_if_exists(path: str) -> Union[str, None]:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception as e:
        logger.debug(f"Failed to read '{path}': {e}")
    return None


def _parse_cgroup_bytes(raw_value: Union[str, None]) -> Union[int, None]:
    if not raw_value:
        return None

    if raw_value.lower() == "max":
        return None

    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None

    if value <= 0:
        return None

    # "Unlimited" sentinel in some cgroup v1 setups.
    if value >= (1 << 60):
        return None

    return value


def _parse_non_negative_int(raw_value: Union[str, None]) -> Union[int, None]:
    if raw_value is None:
        return None
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


def _get_cgroup_memory_limit_bytes(host_total_bytes: int) -> Union[int, None]:
    """
    Reads container memory limit (if present) for cgroup v2/v1.
    Returns None when no effective limit is configured.
    """
    candidates = (
        "/sys/fs/cgroup/memory.max",                     # cgroup v2
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",  # cgroup v1
    )

    for path in candidates:
        raw = _read_text_file_if_exists(path)
        limit = _parse_cgroup_bytes(raw)
        if limit is None:
            continue

        if host_total_bytes > 0:
            # If limit is above host RAM, treat host RAM as effective upper bound.
            return min(limit, host_total_bytes)
        return limit

    return None


def _get_cgroup_memory_usage_bytes() -> Union[int, None]:
    candidates = (
        "/sys/fs/cgroup/memory.current",                # cgroup v2
        "/sys/fs/cgroup/memory/memory.usage_in_bytes", # cgroup v1
    )

    for path in candidates:
        raw = _read_text_file_if_exists(path)
        usage = _parse_cgroup_bytes(raw)
        if usage is not None:
            return usage

    return None


def _get_cgroup_inactive_file_bytes() -> Union[int, None]:
    """
    Returns reclaimable file cache for current cgroup, if available.
    cgroup v2 uses 'inactive_file', v1 often exposes 'total_inactive_file'.
    """
    candidates = (
        ("/sys/fs/cgroup/memory.stat", "inactive_file"),                  # cgroup v2
        ("/sys/fs/cgroup/memory/memory.stat", "total_inactive_file"),     # cgroup v1
        ("/sys/fs/cgroup/memory/memory.stat", "inactive_file"),           # fallback for some v1 setups
    )

    for path, key_name in candidates:
        raw = _read_text_file_if_exists(path)
        if not raw:
            continue

        for line in raw.splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            if parts[0] != key_name:
                continue

            value = _parse_non_negative_int(parts[1])
            if value is not None:
                return value

    return None


def get_health():
    """
    Получает обобщённую информацию о состоянии системы, включая:

    - Свободное место на системном диске
    - Объём и доступность оперативной памяти
    - Состояние первого доступного GPU NVIDIA (если доступен)

    Используемые источники данных:
        - `psutil` для информации о CPU, диске и памяти
        - `py3nvml` или, при его отсутствии/ошибке, утилита `nvidia-smi` для данных GPU

    Возвращает:
        dict: Словарь со следующими ключами:
            - "free_disk_gb": свободное место на системном диске (в ГБ)
            - "total_memory_gb": общий объём оперативной памяти (в ГБ)
            - "available_memory_gb": доступный объём оперативной памяти (в ГБ)
            - "free_ram_gb": физически свободная оперативная память (в ГБ)
            - "gpu": вложенный словарь с информацией о первом GPU:
                - "total_memory_gb": общий объём видеопамяти (в ГБ)
                - "free_memory_gb": свободная видеопамять (в ГБ)
                - "gpu_load_percent": загрузка GPU (в процентах)
                - "temperature_celsius": температура GPU (в °C)
              Либо ключ "error" в случае невозможности получить данные о GPU.

    Исключения:
        - Если `psutil` не установлен, возвращается словарь с ключом "error".
        - Ошибки получения данных GPU логируются и агрегируются.

    Примечания:
        - Функция предполагает наличие хотя бы одного GPU NVIDIA.
        - Если оба метода GPU-инспекции завершаются с ошибкой, информация о GPU не возвращается.
    """
    try:
        import psutil
    except ImportError:
        return {
            "error": "Failed to retrieve system data. The 'psutil' library is not installed."
        }

    # Основная информация о системе
    try:
        # Определяем корневой путь в зависимости от операционной системы
        if os.name == 'nt':
            root_path = 'C:\\'
        else:
            root_path = '/'

        # Информация о диске
        disk = psutil.disk_usage(root_path)
        free_disk_gb = disk.free / (1024 ** 3)

        # Информация о памяти (container-aware):
        # в Docker/Runpod psutil может вернуть память хоста, а не лимит контейнера.
        mem = psutil.virtual_memory()
        host_total_bytes = int(mem.total)
        host_available_bytes = int(mem.available)
        host_free_bytes = int(mem.free)

        cgroup_limit_bytes = _get_cgroup_memory_limit_bytes(host_total_bytes)
        cgroup_usage_bytes = _get_cgroup_memory_usage_bytes()
        cgroup_inactive_file_bytes = _get_cgroup_inactive_file_bytes()

        if cgroup_limit_bytes is not None:
            total_mem_bytes = cgroup_limit_bytes
            if cgroup_usage_bytes is not None:
                used_bytes = min(max(cgroup_usage_bytes, 0), total_mem_bytes)
                # Строгий "free" внутри контейнера без учета reclaimable cache.
                free_ram_bytes = max(total_mem_bytes - used_bytes, 0)

                # Оценка "available" с учетом reclaimable page cache (как более практичная метрика).
                if cgroup_inactive_file_bytes is not None:
                    reclaimable_cache_bytes = min(max(cgroup_inactive_file_bytes, 0), used_bytes)
                    available_mem_bytes = min(
                        total_mem_bytes,
                        free_ram_bytes + reclaimable_cache_bytes
                    )
                else:
                    available_mem_bytes = free_ram_bytes
            else:
                available_mem_bytes = min(host_available_bytes, total_mem_bytes)
                free_ram_bytes = min(host_free_bytes, total_mem_bytes)
        else:
            total_mem_bytes = host_total_bytes
            available_mem_bytes = host_available_bytes
            free_ram_bytes = host_free_bytes

        total_mem_gb = total_mem_bytes / (1024 ** 3)
        available_mem_gb = available_mem_bytes / (1024 ** 3)
        free_ram_gb = free_ram_bytes / (1024 ** 3)

        system_health = {
            "free_disk_gb": free_disk_gb,
            "total_memory_gb": total_mem_gb,
            "available_memory_gb": available_mem_gb,
            "free_ram_gb": free_ram_gb,
        }
    except Exception as e:
        return {
            "error": f"Error retrieving system data: {str(e)}"
        }

    # Дополнительная информация о GPU (только для NVIDIA)
    gpu_info = {}
    gpu_errors = []

    # Первый метод: попытка использовать py3nvml
    try:
        from py3nvml import py3nvml
        py3nvml.nvmlInit()
        # Получаем дескриптор первого GPU (предполагается, что он есть)
        handle = py3nvml.nvmlDeviceGetHandleByIndex(0)
        mem_info = py3nvml.nvmlDeviceGetMemoryInfo(handle)
        total_gpu_mem = mem_info.total / (1024 ** 3)
        free_gpu_mem = mem_info.free / (1024 ** 3)
        gpu_load = py3nvml.nvmlDeviceGetUtilizationRates(handle).gpu  # в процентах
        temperature = py3nvml.nvmlDeviceGetTemperature(handle, py3nvml.NVML_TEMPERATURE_GPU)
        py3nvml.nvmlShutdown()

        gpu_info = {
            "total_memory_gb": total_gpu_mem,
            "free_memory_gb": free_gpu_mem,
            "gpu_load_percent": gpu_load,
            "temperature_celsius": temperature,
        }
    except Exception as e:
        gpu_errors.append(f"py3nvml method error: {str(e)}")
        # Второй метод: использование nvidia-smi через subprocess
        try:
            # Запрашиваем необходимые параметры без единиц измерения
            command = [
                "nvidia-smi",
                "--query-gpu=memory.total,memory.free,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits"
            ]
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if result.returncode != 0:
                raise Exception(result.stderr.strip())

            # Ожидается, что результат имеет одну строку для первого GPU
            output = result.stdout.strip().split('\n')[0]
            # Парсим значения
            fields = [field.strip() for field in output.split(',')]
            if len(fields) < 4:
                raise ValueError("Недостаточно данных, полученных от nvidia-smi")
            total_gpu_mem = float(fields[0]) / 1024  # перевод из MB в GB
            free_gpu_mem = float(fields[1]) / 1024   # перевод из MB в GB
            gpu_load = float(fields[2])              # уже в процентах
            temperature = float(fields[3])           # в градусах Цельсия

            gpu_info = {
                "total_memory_gb": total_gpu_mem,
                "free_memory_gb": free_gpu_mem,
                "gpu_load_percent": gpu_load,
                "temperature_celsius": temperature,
            }
        except Exception as e2:
            logger.warning(f"py3nvml method error: {str(e)} AND nvidia-smi method error: {str(e2)}")
            gpu_errors.append(f"nvidia-smi method error: {str(e2)}")

    # Если никакой информации о GPU не удалось получить, добавляем ошибку
    if not gpu_info and gpu_errors:
        gpu_info = {"error": "; ".join(gpu_errors)}

    # Объединяем данные о системе и GPU
    system_health["gpu"] = gpu_info

    return system_health


def get_latest_file_content(folder_name: str, file_pattern: str = "*.log") -> str:
    """
    Ищет последний (по времени модификации) файл, соответствующий заданному шаблону, в указанной директории
    и возвращает его содержимое.

    Параметры:
        folder_name (str): Путь к директории, в которой выполнять поиск файлов.
        file_pattern (str): Маска (glob-шаблон) для фильтрации файлов. По умолчанию — '*.log'.

    Возвращает:
        str: Содержимое найденного файла. Если файл не найден или произошла ошибка — возвращается пустая строка.

    Поведение:
        - Используется `Path.glob()` для поиска файлов по шаблону в указанной папке (не рекурсивно).
        - Определяется файл с максимальным временем последней модификации (`st_mtime`).
        - Если такой файл найден, его содержимое читается с кодировкой UTF-8 и возвращается.
        - Все этапы логируются. При ошибках — подробный traceback через `logger`.

    Исключения:
        - Ошибки при чтении файлов обрабатываются и логируются; функция возвращает пустую строку.
    """
    try:
        folder: Path = Path(folder_name)
        logger.info(f"Searching for files with pattern '{file_pattern}' in folder: {folder.resolve()}")

        files = list(folder.glob(file_pattern))
        if not files:
            logger.error("No files matching the pattern were found.")
            return ""

        latest_file: Path = max(files, key=lambda f: f.stat().st_mtime)
        logger.info(f"Latest file found: {latest_file.name}")

        with latest_file.open("r", encoding="utf-8") as file:
            content: str = file.read()

        logger.debug("File content successfully read.")
        return content

    except Exception as e:
        logger.error(
            f"Error occurred while processing file in folder '{folder_name}' with pattern '{file_pattern}'. Error: {e}"
        )
        logger.error(traceback.format_exc())
        return ""

def safe_file_move(src, dst):
    """
    Безопасно перемещает файл из `src` в `dst` с минимальным риском потери данных.

    Механизм:
        - Файл сначала копируется по пути `dst + ".tmp"` с сохранением атрибутов (`shutil.copy2`).
        - Затем временный файл переименовывается в `dst` с использованием `os.rename()`, что обеспечивает атомарную замену
          (при условии, что `src` и `dst` находятся на одной файловой системе).
        - После успешного переименования исходный файл `src` удаляется (`os.unlink`).

    Параметры:
        src (str): Путь к исходному файлу.
        dst (str): Целевой путь, по которому файл должен быть перемещён.

    Требования:
        - В случае ошибки на любом этапе файл `src` не будет удалён, что снижает риск потери данных.

    Исключения:
        - Ошибки при копировании, переименовании или удалении не обрабатываются внутри функции — вызывающий код должен учитывать это.
    """
    tmp_dst = dst + ".tmp"
    shutil.copy2(src, tmp_dst)      # копируем с атрибутами
    os.rename(tmp_dst, dst)         # атомарно заменяем имя внутри ТОЙ ЖЕ FS
    os.unlink(src)                  # удаляем исходник


def delete_by_mask(
    root_path: Union[str, Path],
    pattern: str,
    recursive: bool = True
) -> None:
    """
    Delete all files and directories matching a given pattern within a specified path.

    :param root_path: Root path where the deletion should begin.
    :param pattern: Glob pattern to match files or directories.
    :param recursive: Whether to search recursively in subdirectories.

    Example:
        delete_by_mask("/tmp/test_dir", "*.tmp", recursive=True)
    """
    logger.info(f"Starting deletion process in path: {root_path} with pattern: '{pattern}' (recursive={recursive})")

    try:
        base_path = Path(root_path)

        if not base_path.exists():
            logger.warning(f"The specified path does not exist: {base_path.resolve()}")
            return

        search_pattern = pattern if recursive else f"*/{pattern}"
        logger.debug(f"Using search pattern: {search_pattern}")

        targets = list(base_path.rglob(search_pattern) if recursive else base_path.glob(search_pattern))

        if not targets:
            logger.info("No files or directories matched the pattern.")
            return

        targets.sort(key=lambda p: (p.is_dir(), -len(p.parts))) # сортировка так, что бы сначала удалить файлы

        for target in targets:
            try:
                if target.is_symlink():
                    target.unlink()
                    logger.debug(f"Deleted symlink: {target.resolve()}")
                elif target.is_dir():
                    shutil.rmtree(target)
                    logger.debug(f"Deleted directory: {target.resolve()}")
                else:
                    target.unlink()
                    logger.debug(f"Deleted file: {target.resolve()}")
            except Exception as e:
                logger.error(f"Failed to delete {target.resolve()}: {e}\n{traceback.format_exc()}")

        logger.info("Deletion process completed.")

    except Exception as outer_exception:
        logger.error(f"Unexpected error during deletion in {root_path}: {outer_exception}\n{traceback.format_exc()}")




def delete_files_with_prefix(
        folder_path: str,
        prefix_filename: str
) -> None:
    """
    Удаляет все файлы в директории destination_lora_path, имена которых начинаются с
    destination_lora_prefix_filename.

    Args:
        folder_path (str): Путь к директории, где нужно искать файлы.
        prefix_filename (str): Префикс имени файлов для удаления.

    Raises:
        FileNotFoundError: Если директория destination_lora_path не существует или не является директорией.
    """
    dir_path: Path = Path(folder_path)

    # Проверяем, что директория существует и является директорией
    if not dir_path.exists() or not dir_path.is_dir():
        error_msg = f"Destination directory does not exist or is not a directory: {dir_path}"
        logger.error(error_msg)
        raise FileNotFoundError(error_msg)
    logger.info(f"Checking directory: {dir_path}")

    # Собираем список файлов для удаления
    try:
        files_to_delete: List[Path] = [
            f for f in dir_path.iterdir()
            if f.is_file() and f.name.startswith(prefix_filename)
        ]
        logger.info(f"Found {len(files_to_delete)} file(s) to delete with prefix '{prefix_filename}'.")
    except Exception as e:
        logger.error(f"Failed to list files in directory: {dir_path} | Error: {e}")
        logger.debug(traceback.format_exc())
        return

    # Если нет файлов, можно сразу выйти
    if not files_to_delete:
        logger.trace(f"No files found in {folder_path}/{prefix_filename}* to delete.")
        return

    # Удаляем каждый файл с прогресс-баром
    for file_path in tqdm(files_to_delete, desc="Deleting files"):
        try:
            file_path.unlink()
            logger.info(f"Deleted file: {file_path} | Size: {file_path.stat().st_size} bytes")
        except Exception as e:
            logger.warning(f"Failed to delete file: {file_path} | Error: {e}")
            # logger.debug(traceback.format_exc())
            # Продолжаем со следующими файлами
            continue


def safe_join(base_dir: str, user_filename: str) -> str:
    """
    Формирует безопасный абсолютный путь на основе базовой директории и пользовательского имени файла.

    Гарантирует, что итоговый путь остаётся внутри `base_dir`.
    Предотвращает попытки обхода ограничений через относительные ссылки вида '../' (атаки типа path traversal).

    Параметры:
        base_dir (str): Базовая директория, в рамках которой разрешён доступ.
        user_filename (str): Относительное имя файла, заданное пользователем (или внешним источником).

    Возвращает:
        str: Абсолютный путь к файлу внутри `base_dir`.

    Исключения:
        ValueError: Если попытка сформировать путь приводит к выходу за пределы `base_dir`.

    Пример:
        safe_join("/app/uploads", "../etc/passwd") → ValueError
        safe_join("/app/uploads", "user/file.txt") → "/app/uploads/user/file.txt"
    """
    base_abs  = os.path.abspath(base_dir)
    file_abs  = os.path.abspath(os.path.join(base_dir, user_filename))

    # Вариант 1 — через commonpath
    if os.path.commonpath([base_abs]) != os.path.commonpath([base_abs, file_abs]):
        raise ValueError("Path traversal detected")

    return file_abs


def ensure_tmp_dir(script_dir: Path) -> Path:
    """Создать tmp-директорию рядом со скриптом, если её нет."""
    tmp_dir = script_dir / TMP_DIR_NAME
    try:
        tmp_dir.mkdir(exist_ok=True)
        logger.debug(f"TMP dir ready at {tmp_dir}")
    except Exception as e:
        logger.error(f"Cannot create tmp directory {tmp_dir}: {e}")
        raise
    return tmp_dir


def read_base64(path: Path) -> str:
    """Читает файл и возвращает base64-encoded string."""
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def cleanup_files(files: List[Path]) -> None:
    """Удаляет все файлы из списка, не трогая директорию."""
    for p in files:
        try:
            p.unlink(missing_ok=True)
            logger.debug(f"Deleted temp file {p}")
        except Exception as e:
            logger.warning(f"Cannot delete temp file {p}: {e}")


def _get_clean_folders() -> List[str]:
    return [
        const.COMFY_FOLDER + '/output',
        const.COMFY_FOLDER + '/input',
        const.COMFY_FOLDER + '/temp',
        './tmp',
    ]


def clean_tmp_files(days=1):
    clean_folders = _get_clean_folders()
    logger.info(f'Clean old files for {clean_folders}')
    for folder in clean_folders:
        try:
            clean_folder(folder, days)
        except Exception as e:
            logger.error(f'Failed to clean folder: {folder}. Error: {e}')
            logger.debug(traceback.format_exc())
