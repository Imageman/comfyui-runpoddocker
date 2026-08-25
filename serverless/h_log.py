# -*- coding: utf-8 -*-
"""
Parameter summarizer for API logging.

This module provides a robust function `summarize_params` that:
- Produces a one-line, JSON-like (actually valid JSON) string for logs.
- Preserves Unicode (ensure_ascii=False).
- Iteratively reduces detail to satisfy max length (default 10_000 chars).
- Masks sensitive keys (password, token, bearer, etc.) as "***redacted***".
- Truncates long lists/strings; compacts large dicts; summarizes arrays/tensors.
- Handles cycles, recursion limits, and unserializable objects safely.
- Logs anomalies and reduction decisions via loguru without printing.

Python 3.11+ is recommended.

Example:
    from loguru import logger

    payload = {
        "task1": {
            "lora_name": "Buscemi_3photo",
            "prompt": "Man <Zu1vs> ... (very long) ...",
            "video_man": "mask_video_man.mp4",
            "video_mask_man": "mask_only_man.mp4",
            "video_woman": "mask_video_woman.mp4",
            "video_mask_woman": "mask_only_woman.mp4",
            "steps": 5,
            "causvid_strength": 0.6,
            "long_video": "false",
            "model": "wan_1_3b"
        }
    }

    summary = summarize_params(payload, max_len=10_000)
    logger.info("Request params: {}", summary)
"""

from __future__ import annotations

# Стандартная библиотека
import json
import math
import os
import re
import uuid
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, MutableSequence, Optional, Sequence, Tuple, Union
from typing import Any, Dict, Tuple

import traceback

# Сторонние библиотеки
from flask import Request, request, jsonify
from loguru import logger

import const
from runpod_m import s3_minio

try:
    import numpy as np  # type: ignore

    _NP_AVAILABLE = True
except Exception:
    _NP_AVAILABLE = False

try:
    import torch  # type: ignore

    _TORCH_AVAILABLE = True
except Exception:
    _TORCH_AVAILABLE = False

# -----------------------------
# Константы и настройки по умолчанию
# -----------------------------

MAX_JSON_LEN_DEFAULT: int = 10_000
LIST_EDGE_DEFAULT: int = 5
DICT_EDGE_DEFAULT: int = 30
STR_EDGE_DEFAULT: int = 50
ELLIPSIS: str = " ... "
MAX_RECURSION_DEFAULT: int = 10

# Регулярка для маскирования секретов (регистронезависимая)
# Важно: \b после "auth" чтобы не совпадало с "author"
SENSITIVE_KEY_REGEX: re.Pattern[str] = re.compile(
    r"(?i)(password|passwd|passphrase|secret|token|api[_-]?key|access.{0,19}key|authorization|auth\b|bearer|cookie)"
)


# -----------------------------
# Вспомогательные типы
# -----------------------------

class ArraysMode(Enum):
    """How to represent arrays/tensors."""
    SAMPLE = "sample"  # значения + метаданные (сэмпл)
    META_ONLY = "meta"  # только метаданные


class SummarizeConfig:
    """Configuration for a single summarization attempt."""

    def __init__(
            self,
            list_edge: int = LIST_EDGE_DEFAULT,
            dict_edge: int = DICT_EDGE_DEFAULT,
            str_edge: int = STR_EDGE_DEFAULT,
            max_recursion: int = MAX_RECURSION_DEFAULT,
            arrays_mode: ArraysMode = ArraysMode.SAMPLE,
    ) -> None:
        self.list_edge = list_edge
        self.dict_edge = dict_edge
        self.str_edge = str_edge
        self.max_recursion = max_recursion
        self.arrays_mode = arrays_mode


# -----------------------------
# Утилиты
# -----------------------------

def _format_float(value: float) -> Union[float, str]:
    """
    Format a float with at most 5 significant digits.
    Returns a float when finite, otherwise string for NaN/Infinity.
    """
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    # Ограничение до 5 значащих цифр
    # Преобразуем обратно в float, чтобы в JSON было числом (не строкой)
    try:
        s = f"{value:.5g}"
        return float(s)
    except Exception:
        # В случае проблем возвращаем строку-представление
        return f"{value:.5g}"


def _truncate_string(s: str, str_edge: int) -> str:
    """
    Truncate long strings keeping first/last `str_edge` chars with ELLIPSIS in the middle.
    Trigger when len(s) > 2*str_edge + len(ELLIPSIS).
    """
    threshold = 2 * str_edge + len(ELLIPSIS)
    if len(s) > threshold:
        head = s[:str_edge]
        tail = s[-str_edge:]
        return f"{head}{ELLIPSIS}{tail}"
    return s


def _bytes_to_hex_spaced(data: Union[bytes, bytearray]) -> str:
    """
    Convert bytes to uppercase hex string with spaces: e.g., 'FF 00 1D'.
    """
    # Быстрый и компактный способ: форматирование по байту
    return " ".join(f"{b:02X}" for b in data)


def _mask_if_sensitive(key_str: str, value: Any) -> Any:
    """
    Return masked value if key looks sensitive.
    """
    try:
        if SENSITIVE_KEY_REGEX.search(key_str):
            return "***redacted***"
    except Exception:
        # На всякий случай не ломаемся на странных ключах
        pass
    return value


def _json_dumps_one_line(obj: Any) -> str:
    """
    Dump object to a single-line JSON with Unicode preserved (ensure_ascii=False).
    """
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# -----------------------------
# Основной класс-сериализатор
# -----------------------------

class ParamSummarizer:
    """
    Summarizes arbitrary Python objects into a compact, JSON-like one-line string
    suitable for logging. Applies masking, truncation, recursion/cycle handling,
    and optionally array/tensor sampling.

    Notes:
        - All log messages are in English.
        - Unicode in output is preserved.
        - Uses loguru for logging anomalies and decisions.
    """

    def __init__(self, max_len: int = MAX_JSON_LEN_DEFAULT) -> None:
        self.max_len = max_len

        # Счетчики для отчёта в лог
        self._masked_count: int = 0
        self._circular_count: int = 0
        self._meta_only_count: int = 0
        self._omitted_count: int = 0
        self._unserializable_count: int = 0
        self._hard_truncated: bool = False

    # -------------------------
    # Публичный метод
    # -------------------------

    def summarize(self, obj: Any) -> str:
        """
        Summarize the given object into a one-line JSON string within `self.max_len`.
        Iteratively reduces details until constraints are met.
        """
        try:
            # Если верхнеуровневая строка — пытаемся распарсить JSON
            obj = self._maybe_parse_top_level_json(obj)

            # Генерация конфигураций "от подробной к компактной"
            for cfg in self._iter_configs():
                try:
                    sanitized = self._to_jsonable(obj=obj, cfg=cfg, depth=0, ancestors=set())
                    s = _json_dumps_one_line(sanitized)

                    if len(s) <= self.max_len:
                        self._log_summary(cfg, final_len=len(s))
                        return s

                    # Длина превышена — продолжаем итерации
                    logger.trace(
                        "Summary length {} exceeds max_len {} (list_edge={}, str_edge={}, dict_edge={}, arrays_mode={}, max_rec={})",
                        len(s), self.max_len, cfg.list_edge, cfg.str_edge, cfg.dict_edge, cfg.arrays_mode.value,
                        cfg.max_recursion,
                    )
                except Exception as inner_exc:
                    # Логируем и пробуем следующую конфигурацию
                    logger.info(
                        "Summarization attempt failed (config {}) — falling back. Error: {}",
                        cfg.__dict__, str(inner_exc)
                    )
                    logger.debug("Traceback:\n{}", traceback.format_exc())

            # Если не удалось уложиться — жёстко обрезаем последнюю успешную попытку
            try:
                # Последняя попытка: максимально агрессивные настройки
                cfg = SummarizeConfig(
                    list_edge=2, dict_edge=10, str_edge=30, max_recursion=4, arrays_mode=ArraysMode.META_ONLY
                )
                sanitized = self._to_jsonable(obj=obj, cfg=cfg, depth=0, ancestors=set())
                s = _json_dumps_one_line(sanitized)
            except Exception:
                logger.info("Final summarization failed; returning a safe error JSON.")
                logger.debug("Traceback:\n{}", traceback.format_exc())
                return self._safe_error_json("serialization_failed")

            if len(s) > self.max_len:
                tail = " ... (truncated)"
                s = s[: max(0, self.max_len - len(tail))] + tail
                self._hard_truncated = True

            self._log_summary(cfg, final_len=len(s))
            return s

        except Exception as exc:
            logger.info("Unexpected error in summarize(): {}", str(exc))
            logger.debug("Traceback:\n{}", traceback.format_exc())
            return self._safe_error_json("unexpected_error")

    # -------------------------
    # Внутренние методы
    # -------------------------

    def _iter_configs(self) -> Iterable[SummarizeConfig]:
        """
        Yield configurations from detailed to compact, to try fitting into max_len.
        """
        # 1) Базовая
        yield SummarizeConfig()

        # 2) Чуть менее подробные шаги
        yield SummarizeConfig(list_edge=3, dict_edge=30, str_edge=150, max_recursion=10, arrays_mode=ArraysMode.SAMPLE)
        yield SummarizeConfig(list_edge=3, dict_edge=30, str_edge=70, max_recursion=10, arrays_mode=ArraysMode.SAMPLE)
        yield SummarizeConfig(list_edge=3, dict_edge=15, str_edge=70, max_recursion=10, arrays_mode=ArraysMode.SAMPLE)

        # 3) Переключаем массивы/тензоры в метаданные
        yield SummarizeConfig(list_edge=3, dict_edge=15, str_edge=70, max_recursion=10,
                              arrays_mode=ArraysMode.META_ONLY)

        # 4) Ещё компактнее
        yield SummarizeConfig(list_edge=2, dict_edge=15, str_edge=70, max_recursion=8, arrays_mode=ArraysMode.META_ONLY)
        yield SummarizeConfig(list_edge=2, dict_edge=10, str_edge=30, max_recursion=6, arrays_mode=ArraysMode.META_ONLY)
        yield SummarizeConfig(list_edge=2, dict_edge=10, str_edge=30, max_recursion=4, arrays_mode=ArraysMode.META_ONLY)

    def _maybe_parse_top_level_json(self, obj: Any) -> Any:
        """
        If top-level input is a string, try to json.loads it; on success, use parsed object.
        """
        if isinstance(obj, str):
            try:
                parsed = json.loads(obj)
                logger.debug("Top-level string successfully parsed as JSON.")
                return parsed
            except Exception:
                logger.debug("Top-level string is not a valid JSON; treat as plain string.")
                return obj
        return obj

    def _to_jsonable(self, obj: Any, cfg: SummarizeConfig, depth: int, ancestors: set[int]) -> Any:
        """
        Convert arbitrary object to a JSON-serializable structure,
        applying masking/truncation rules. Recursively processes mappings
        and sequences while tracking cycles and respecting recursion limit.
        """
        # Лимит глубины рекурсии
        if depth >= cfg.max_recursion:
            self._omitted_count += 1
            return "<omitted>"

        # Решаем, нужно ли отслеживать цикл для этого obj
        def _trackable(o: Any) -> bool:
            # Контейнеры и структуры, способные содержать ссылки на себя
            from collections.abc import Mapping, Sequence
            return (
                    isinstance(o, Mapping)
                    or isinstance(o, (list, tuple, set))
                    or is_dataclass(o)
                # numpy/torch обычно не образуют ссылочных циклов с родителем;
                # при желании можно добавить их сюда, но практической нужды нет.
            )

        track = _trackable(obj)
        oid: Optional[int] = None
        if track:
            oid = id(obj)
            if oid in ancestors:
                self._circular_count += 1
                return "<circular_ref>"
            ancestors.add(oid)

        try:
            # Примитивы
            if obj is None:
                return None
            if isinstance(obj, bool):
                return obj
            if isinstance(obj, int):
                return obj
            if isinstance(obj, float):
                return _format_float(obj)
            if isinstance(obj, str):
                # JSON dumps сам экранирует \n\t и т.д.; порог: 2*STR_EDGE + len(ELLIPSIS)
                return _truncate_string(obj, cfg.str_edge)

            # bytes / bytearray → hex + тип
            if isinstance(obj, (bytes, bytearray)):
                hex_str = _bytes_to_hex_spaced(obj)
                hex_str = _truncate_string(hex_str, cfg.str_edge)
                return {"type": "bytes", "len": len(obj), "hex": hex_str}

            # datetime / date / time / timedelta → строки
            if isinstance(obj, (datetime, date, time)):
                try:
                    # Для datetime приводим к ISO 8601
                    if isinstance(obj, datetime):
                        # Сохраняем информацию о таймзоне, если есть
                        iso = obj.isoformat()
                        return iso
                    if isinstance(obj, date):
                        return obj.isoformat()
                    if isinstance(obj, time):
                        return obj.isoformat()
                except Exception:
                    return str(obj)

            if isinstance(obj, timedelta):
                try:
                    # Человекочитаемо: total seconds
                    return f"<timedelta: {obj.total_seconds()}s>"
                except Exception:
                    return str(obj)

            if isinstance(obj, Path):
                return str(obj)

            if isinstance(obj, Decimal):
                # Decimal логируем строкой, чтобы не терять точность
                return str(obj)

            if isinstance(obj, Enum):
                try:
                    return f"{obj.__class__.__name__}.{obj.name}"
                except Exception:
                    return str(obj)

            if is_dataclass(obj):
                try:
                    return self._to_jsonable(asdict(obj), cfg, depth + 1, ancestors)
                except Exception:
                    # Если dataclass падает — отдадим repr
                    return f"<unserializable_dataclass: {obj.__class__.__name__}>"

            # numpy ndarray
            if _NP_AVAILABLE and isinstance(obj, np.ndarray):  # type: ignore
                meta: Dict[str, Any] = {
                    "type": "ndarray",
                    "shape": list(obj.shape),
                    "dtype": str(obj.dtype),
                }
                if cfg.arrays_mode is ArraysMode.SAMPLE:
                    try:
                        flat = obj.ravel()
                        edge = cfg.list_edge
                        n = flat.size
                        if n == 0:
                            meta["values"] = []
                        elif n <= 2 * edge:
                            meta["values"] = [self._to_jsonable(v.item(), cfg, depth + 1, ancestors) for v in flat]
                        else:
                            head_vals = [self._to_jsonable(v.item(), cfg, depth + 1, ancestors) for v in flat[:edge]]
                            tail_vals = [self._to_jsonable(v.item(), cfg, depth + 1, ancestors) for v in flat[-edge:]]
                            meta["values"] = head_vals + [ELLIPSIS] + tail_vals
                    except Exception:
                        logger.debug("Failed to sample numpy array values; using meta only.")
                        self._meta_only_count += 1
                        return meta
                    return meta
                else:
                    # Только метаданные
                    self._meta_only_count += 1
                    return meta

            # torch.Tensor
            if _TORCH_AVAILABLE and isinstance(obj, torch.Tensor):  # type: ignore
                try:
                    device = str(obj.device)
                except Exception:
                    device = "unknown"

                meta_t: Dict[str, Any] = {
                    "type": "tensor",
                    "shape": list(obj.shape),
                    "dtype": str(obj.dtype),
                    "device": device,
                }
                if cfg.arrays_mode is ArraysMode.SAMPLE:
                    try:
                        # Перенос на CPU только для выборки, без .numpy() (избегаем зависимости от numpy)
                        flat_t = obj.reshape(-1).detach().cpu()
                        edge = cfg.list_edge
                        n = flat_t.numel()
                        if n == 0:
                            meta_t["values"] = []
                        elif n <= 2 * edge:
                            meta_t["values"] = [self._to_jsonable(_format_float(float(v)), cfg, depth + 1, ancestors) for v
                                                in flat_t]
                        else:
                            head_vals = [self._to_jsonable(_format_float(float(v)), cfg, depth + 1, ancestors) for v in
                                         flat_t[:edge]]
                            tail_vals = [self._to_jsonable(_format_float(float(v)), cfg, depth + 1, ancestors) for v in
                                         flat_t[-edge:]]
                            meta_t["values"] = head_vals + [ELLIPSIS] + tail_vals
                    except Exception:
                        logger.debug("Failed to sample torch tensor values; using meta only.")
                        self._meta_only_count += 1
                        return meta_t
                    return meta_t
                else:
                    self._meta_only_count += 1
                    return meta_t

            # Mapping (dict-подобное)
            if isinstance(obj, Mapping):
                items: List[Tuple[str, Any]] = []
                # Сохраняем порядок вставки
                for k, v in obj.items():
                    # Преобразуем ключ к строке для JSON
                    k_str = str(k)
                    # Маскируем чувствительные значения
                    v_masked = _mask_if_sensitive(k_str, v)
                    if v_masked is not v:
                        self._masked_count += 1
                    items.append((k_str, v_masked))

                # Усечение больших словарей: первые/последние dict_edge
                total = len(items)
                kept: List[Tuple[str, Any]]
                if total > 2 * cfg.dict_edge:
                    head = items[: cfg.dict_edge]
                    tail = items[-cfg.dict_edge:]
                    # Преобразуем head/tail, середину заменяем маркером
                    result_map: Dict[str, Any] = {}
                    for k, v in head:
                        result_map[k] = self._to_jsonable(v, cfg, depth + 1, ancestors)
                    result_map[ELLIPSIS] = ELLIPSIS
                    for k, v in tail:
                        result_map[k] = self._to_jsonable(v, cfg, depth + 1, ancestors)
                    return result_map

                # Небольшой словарь — преобразуем полностью
                result_map_full: Dict[str, Any] = {}
                for k, v in items:
                    result_map_full[k] = self._to_jsonable(v, cfg, depth + 1, ancestors)
                return result_map_full

            # Sequence (list/tuple/set/...) — обрабатываем как список
            if isinstance(obj, (list, tuple, set)):
                seq_list = list(obj) if not isinstance(obj, list) else obj
                n = len(seq_list)
                edge = cfg.list_edge

                if n == 0:
                    return []

                if n <= 2 * edge:
                    # Преобразуем всё
                    return [self._to_jsonable(x, cfg, depth + 1, ancestors) for x in seq_list]

                # Преобразуем только края, середина — маркер
                head_transformed = [self._to_jsonable(x, cfg, depth + 1, ancestors) for x in seq_list[:edge]]
                tail_transformed = [self._to_jsonable(x, cfg, depth + 1, ancestors) for x in seq_list[-edge:]]
                return head_transformed + [ELLIPSIS] + tail_transformed

            # Любой другой объект — пытаемся взять repr, иначе тип
            try:
                return f"<unserializable: {obj.__class__.__name__}>"
            except Exception:
                self._unserializable_count += 1
                return "<unserializable: unknown>"

        except Exception as exc:
            # Внутренняя ошибка сериализации — безопасный ответ и лог
            self._unserializable_count += 1
            logger.info("Error while converting object of type {}: {}", type(obj), str(exc))
            logger.debug("Traceback:\n{}", traceback.format_exc())
            return f"<unserializable: {obj.__class__.__name__}>"

        finally:
            if track and oid is not None:
                # ВАЖНО: снимаем метку при выходе — отслеживаем цикл только по текущему пути
                ancestors.discard(oid)

    def _safe_error_json(self, reason: str) -> str:
        """
        Return a minimal safe JSON string when summarization fails unexpectedly.
        """
        obj = {"error": "serialization_failed", "reason": reason}
        try:
            return _json_dumps_one_line(obj)
        except Exception:
            return '{"error":"serialization_failed"}'

    def _log_summary(self, cfg: SummarizeConfig, final_len: int) -> None:
        """
        Log summarization stats (single-line, English).
        """
        logger.trace(
            "Params summarized: len={} (max_len={}), list_edge={}, str_edge={}, dict_edge={}, arrays_mode={}, max_rec={}, "
            "masked={}, cycles={}, meta_only={}, omitted={}, hard_truncated={}",
            final_len,
            self.max_len,
            cfg.list_edge,
            cfg.str_edge,
            cfg.dict_edge,
            cfg.arrays_mode.value,
            cfg.max_recursion,
            self._masked_count,
            self._circular_count,
            self._meta_only_count,
            self._omitted_count,
            self._hard_truncated,
        )


# -----------------------------
# Публичная функция
# -----------------------------

def summarize_params(obj: Any, max_len: int = MAX_JSON_LEN_DEFAULT) -> str:
    """
    Summarize input parameters for API logging.

    Behavior:
        - If top-level input is a string and valid JSON, it is parsed and summarized as object.
        - Long strings are truncated to keep first/last 100 chars with an ELLIPSIS (" ... ") in the middle
          when len(s) > 100 + 100 + len(" ... ").
        - Long lists keep first/last 5 items with ELLIPSIS in the middle (if len > 10).
        - Large dicts keep first/last 30 entries in insertion order with ELLIPSIS marker in the middle (if len > 60).
        - Floats are formatted to at most 5 significant digits (non-finite become strings).
        - bytes/bytearray are represented as {"type":"bytes","len":N,"hex":"AA BB ..."} with hex truncated as string.
        - numpy arrays / torch tensors are summarized with metadata and, when allowed, a sampled value list.
        - Cycles are reported as "<circular_ref>"; too deep recursion as "<omitted>".
        - Sensitive keys (password/token/bearer/...) are masked as "***redacted***".
        - Detail is reduced iteratively to fit `max_len`; final hard truncation appends " ... (truncated)".

    Args:
        obj: Any input — JSON-like structure or arbitrary Python objects.
        max_len: Maximum allowed length of the resulting JSON string (default 10_000).

    Returns:
        JSON string (one line, ensure_ascii=False) ready to be written to logs.
    """
    summarizer = ParamSummarizer(max_len=max_len)
    return summarizer.summarize(obj)


###################################################
# -*- coding: utf-8 -*-
"""
Flask request payload logging via summarize_params.

- Reads request body safely (up to BODY_MAX_BYTES).
- Handles JSON, form-urlencoded, multipart (files -> metadata only), and raw bytes.
- Preserves Unicode and logs in a single line (INFO).
- Masks sensitive headers/fields; uses summarize_params(max_len=2_000).
- Never raises to the caller; logs internal errors with traceback.
"""

# -----------------------
# Константы
# -----------------------

REQUEST_LOG_MAX_LEN: int = 3_500
BODY_MAX_BYTES: int = 100 * 1024 * 1024  # 100 MB
ELLIPSIS_MARK: str = " ... "

# Регексы и правила маскирования те же, что и в вашем summarize_params (включая bearer)
# Здесь маскируем только по имени заголовка/поля формы, регистронезависимо
SENSITIVE_HEADER_REGEX = re.compile(
    r"(?i)(password|passwd|passphrase|secret|token|api[_-]?key|authorization|auth\b|bearer|cookie)"
)


def _mask_if_sensitive_header(name: str, value: Any) -> Any:
    """
    Mask value if header/form field name looks sensitive.
    """
    try:
        if SENSITIVE_HEADER_REGEX.search(name):
            return "***redacted***"
    except Exception:
        # Не ломаемся на странных именах
        pass
    return value


def _collect_headers(req: Request) -> Dict[str, Any]:
    """
    Collect headers into a dict while masking sensitive ones.
    """
    headers: Dict[str, Any] = {}
    try:
        for k, v in req.headers.items():
            masked = _mask_if_sensitive_header(k, v)
            headers[str(k)] = masked
    except Exception as exc:
        logger.info("Failed to collect headers: {}", str(exc))
        logger.debug("Traceback:\n{}", traceback.format_exc())
    return headers


def _collect_query(req: Request) -> Dict[str, Any]:
    """
    Collect query parameters (supporting multi-values).
    """
    query: Dict[str, Any] = {}
    try:
        # lists() -> {key: [v1, v2, ...]}
        for k, vals in req.args.lists():
            query[str(k)] = vals if len(vals) > 1 else (vals[0] if vals else None)
    except Exception as exc:
        logger.info("Failed to collect query params: {}", str(exc))
        logger.debug("Traceback:\n{}", traceback.format_exc())
    return query


def _collect_form_and_files(req: Request) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Collect form fields and files metadata (no file content).
    """
    form_fields: Dict[str, Any] = {}
    files_meta: Dict[str, Any] = {}

    # Поля формы
    try:
        for k, vals in req.form.lists():
            v = vals if len(vals) > 1 else (vals[0] if vals else None)
            form_fields[str(k)] = _mask_if_sensitive_header(str(k), v)
    except Exception as exc:
        logger.info("Failed to collect form fields: {}", str(exc))
        logger.debug("Traceback:\n{}", traceback.format_exc())

    # Метаданные файлов
    try:
        for name, storage in req.files.items():
            # Не читаем содержимое, только метаданные
            files_meta[str(name)] = {
                "filename": storage.filename,
                "mimetype": storage.mimetype,
                # В некоторых серверах content_length может быть None
                "content_length": getattr(storage, "content_length", None),
            }
    except Exception as exc:
        logger.info("Failed to collect files metadata: {}", str(exc))
        logger.debug("Traceback:\n{}", traceback.format_exc())

    return form_fields, files_meta


def _collect_body(req: Request) -> Any:
    """
    Collect request body as an object suitable for summarize_params:
    - JSON -> dict/list
    - form-urlencoded -> fields dict
    - multipart -> {"form":..., "files":...}
    - other -> raw bytes (summarize_params will hex+truncate)
    Applies BODY_MAX_BYTES limit.
    """
    try:
        content_length = req.content_length
        if content_length is not None and content_length > BODY_MAX_BYTES:
            return {
                "type": "body_meta",
                "reason": "body_too_large",
                "content_length": content_length,
                "content_type": req.content_type,
            }

        # JSON — парсим "мягко"
        if req.is_json:
            payload = req.get_json(silent=True)
            if payload is not None:
                return payload
            # Если парсинг не удался — пойдём далее как сырьё

        # multipart/form-data
        if req.mimetype and req.mimetype.startswith("multipart/"):
            form_fields, files_meta = _collect_form_and_files(req)
            return {
                "type": "multipart",
                "form": form_fields,
                "files": files_meta,
            }

        # application/x-www-form-urlencoded
        if req.mimetype == "application/x-www-form-urlencoded":
            fields: Dict[str, Any] = {}
            for k, vals in req.form.lists():
                v = vals if len(vals) > 1 else (vals[0] if vals else None)
                fields[str(k)] = _mask_if_sensitive_header(str(k), v)
            return fields

        # Остальное — как "сырые" байты; Flask кэширует тело при cache=True
        raw: bytes = req.get_data(cache=True, as_text=False)  # может быть пустым
        return raw

    except Exception as exc:
        logger.info("Failed to collect body: {}", str(exc))
        logger.debug("Traceback:\n{}", traceback.format_exc())
        return {"type": "body_error", "error": str(exc)}


def _build_request_envelope(req: Request) -> Dict[str, Any]:
    """
    Compose an envelope with meta + query + headers + body to feed summarize_params.
    """
    try:
        envelope: Dict[str, Any] = {
            "meta": {
                "ip": req.remote_addr,
                "method": req.method,
                "path": req.path,
                "content_type": req.content_type,
                "content_length": req.content_length,
            },
            "query": _collect_query(req),
            "headers": _collect_headers(req),
            "body": _collect_body(req),
        }
        return envelope
    except Exception as exc:
        logger.info("Failed to build request envelope: {}", str(exc))
        logger.debug("Traceback:\n{}", traceback.format_exc())
        return {"error": "envelope_build_failed", "detail": str(exc)}


def log_request_payload(req: Request) -> None:
    """
    Summarize and log the request payload in one line.
    """
    try:
        if "/api/stat" in req.path or "/files/byid" in req.path or '/swap_result' in req.path or req.path=='/':
            return
        envelope = _build_request_envelope(req)
        summary = summarize_params(envelope, max_len=REQUEST_LOG_MAX_LEN)
        # Единообразная запись: одна строка, человекочитаемый JSON-подобный формат
        # показать вызывающую функцию уровнем выше
        logger.opt(depth=1).info("Incoming request: {}", summary)
    except Exception as exc:
        logger.info("Request logging failed: {}", str(exc))
        logger.debug("Traceback:\n{}", traceback.format_exc())
        # Никогда не бросаем исключение наружу


# -----------------------------
# Пример использования
# -----------------------------
def upload_app_log_to_s3_if_runpod(
    log_filename: str = "app.log",
    *,
    s3_log_filename_suffix: str = "",
    max_log_bytes: int = 1_000_000,
) -> Optional[str]:
    """
    Upload app log with env block to S3 if runpod mode is enabled.

    Returns S3 key on success, otherwise None.
    """
    if not (const.runpod_pod_mode or const.runpod_serverless_mode):
        return None

    def _safe_id(raw: Optional[str]) -> Optional[str]:
        if not raw:
            return None
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", raw.strip())
        cleaned = cleaned.strip("._-")
        return cleaned or None

    def _mask_sensitive_env_value(value: str) -> str:
        raw = value or ""
        max_prefix = len(raw) // 2
        prefix_len = min(6, max_prefix)
        return f"{raw[:prefix_len]}xxxxxx"

    def _is_sensitive_env_key(key: str) -> bool:
        return re.search(r"(?i)(api_key|secret|open_button)", key) is not None

    pod_id = os.getenv("RUNPOD_POD_ID")
    endpoint_id = os.getenv("RUNPOD_ENDPOINT_ID")
    safe_id = _safe_id(pod_id) or _safe_id(endpoint_id)
    if not safe_id:
        safe_id = uuid.uuid4().hex

    if s3_log_filename_suffix != '':
        safe_id += s3_log_filename_suffix
        safe_id = _safe_id(safe_id)
    s3_key = f"tmp/{safe_id}.log"

    env_lines: List[str] = []
    for k, v in sorted(os.environ.items(), key=lambda kv: kv[0]):
        value = v if isinstance(v, str) else str(v)
        if _is_sensitive_env_key(k):
            value = _mask_sensitive_env_value(value)
        value = value.replace("\n", "\\n")
        env_lines.append(f"{k}={value}")

    env_block = "\n".join(
        ["--- ENV START ---", *env_lines, "--- ENV END ---", ""]
    )

    log_path = Path(log_filename)
    if not log_path.is_file():
        logger.warning("Log file not found: {}", str(log_path))
        log_text = ""
    else:
        try:
            if max_log_bytes is not None and max_log_bytes > 0:
                with log_path.open("rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    if size > max_log_bytes:
                        logger.warning(
                            "Log file too large ({} bytes); using last {} bytes.",
                            size,
                            max_log_bytes,
                        )
                        f.seek(-max_log_bytes, os.SEEK_END)
                        data = f.read(max_log_bytes)
                    else:
                        f.seek(0)
                        data = f.read()
                log_text = data.decode("utf-8", errors="replace")
            else:
                log_text = log_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning("Failed to read log file {}: {}", str(log_path), str(exc))
            log_text = ""

    payload = env_block + log_text

    tmp_dir = Path("./tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"{safe_id}.log"

    try:
        tmp_path.write_text(payload, encoding="utf-8")
        if s3_minio.store is None:
            s3_minio.init_store()
        if s3_minio.store is None:
            logger.warning("S3 store is not initialized; cannot upload log.")
            return None
        s3_minio.store.upload(tmp_path, s3_key)
        return s3_key
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    # Пример из запроса
    payload_for_summ = {
        "task1": {
            "lora_name": "Buscemi_3photo",
            "prompt": (
                "Man <Zu1vs> slowly walk  slowly walk slowly walk slowly walk slowly walk slowly walk slowly walk | "
                "Woman <Je5va> jump Woman <Je5va> jumpWoman <Je5va> <Je5va> jumpWoman <Je5va> jumpWoman <Je5va> jump"
            ),
            "video_man": "mask_video_man.mp4",
            "video_mask_man": "mask_only_man.mp4",
            "video_woman": "mask_video_woman.mp4",
            "video_mask_woman": "mask_only_woman.mp4",
            "steps": 5,
            "causvid_strength": 0.6,
            "long_video": "false",
            "model": "wan_1_3b",
            # Демонстрация маскирования
            "authToken": "abc123",
            "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
        }
    }

    summary = summarize_params(payload_for_summ, max_len=500)
    print("Request params summary: {}", summary)
