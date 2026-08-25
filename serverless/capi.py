import json
import time
import urllib
import websocket
import uuid
import ssl
import requests_toolbelt
import os
import re
from loguru import logger
import base64
from io import BytesIO
from tenacity import retry, stop_after_attempt, wait_fixed, before_sleep_log
import logging

import mimetypes
import requests
import traceback
from typing import Any, Dict, List, Optional, TypedDict

try:
    import h_log
except ImportError:
    class _FallbackHLog:
        @staticmethod
        def summarize_params(value):
            return repr(value)[:500]

    h_log = _FallbackHLog()

try:
    import jobs
except ImportError:
    class _LocalJobManager:
        """No-op compatibility layer for the standalone RunPod worker."""

        def __init__(self):
            self._jobs = {}

        def get_jobs(self):
            return self._jobs

        def add_job(self, job_id, status):
            self._jobs[job_id] = status

        def update_job(self, job_id, status):
            self._jobs[job_id] = status

        def acquire_gpu(self, _job_id, _timeout):
            return True

        def release_gpu(self, _job_id):
            return None

        def remove_job(self, job_id):
            self._jobs.pop(job_id, None)

    class _LocalJobs:
        job_manager = _LocalJobManager()

    jobs = _LocalJobs()

try:
    import h
except ImportError:
    class _FallbackH:
        @staticmethod
        def write_variables_to_json(_variables, _filename):
            return None

    h = _FallbackH()

if not hasattr(websocket, '__version__'):
    logger.error("Founded 'websocket', but not 'websocket-client'.")

class AbortException(Exception):
    """Исключение, выбрасываемое при необходимости прервать процесс."""
    pass


class WorkflowResult(TypedDict):
    """Структура результата, возвращаемого :pymeth:`do_workflow`."""
    history: Dict[str, Any]
    image_filenames: List[str]
    images: Optional[List[str]]


class ConnectionManager:
    def __init__(self, server_url, open_button_token, use_https=True, use_wss=True, allow_self_signed_cert=False,
                 certificate_path='', job_name=''):
        self.server_url = server_url
        self.protocol_http = "https" if use_https else "http"
        self.protocol_ws = "wss" if use_wss else "ws"
        self.allow_self_signed_cert = allow_self_signed_cert
        self.timeout = 60
        self.open_button_token = open_button_token
        self.headers = {
            'Authorization': f'Bearer {self.open_button_token}',
        }
        logger.debug(
            f'{self.protocol_http}+{self.protocol_ws}://{self.server_url} OpenButton token: {self.open_button_token[:5]}... ')
        self.certificate_path = certificate_path
        self.ssl_context = self._create_ssl_context()
        self.ws = None
        self.callback = None  # заготовка для каких-то действий во время ожидания завершения
        self.client_id = ''
        self.job_name = job_name
        if self.job_name == '':
            self.job_name = str(uuid.uuid4())
        self.errors=''

    def _create_ssl_context(self):
        if self.certificate_path != '':
            ssl_context = ssl.create_default_context()
            ssl_context.load_verify_locations(cafile=self.certificate_path)
            return ssl_context

        if self.allow_self_signed_cert:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            return context
        return None

    @retry(
        reraise=True,  # чтобы наружу прилетало исходное исключение (HTTPError), а не RetryError
        stop=stop_after_attempt(3),               # максимум 4 попытки (3 retry)
        wait=wait_fixed(1),                       # пауза секунд между попытками
        before_sleep=before_sleep_log(logger, logging.WARNING)  # лог перед retry
    )
    def open_websocket_connection(self):
        if self.ws != None:
            logger.debug('Websocket opened!')
            self.close_ws()
        client_id = str(uuid.uuid4())
        # Преобразование словаря заголовков в список строк
        header_list = [f'{key}: {value}' for key, value in self.headers.items()]

        logger.debug(
            f'ws.open {self.protocol_http} + {self.protocol_ws}://{self.server_url} with timeout={self.timeout}')
        ws = websocket.WebSocket(sslopt={"context": self.ssl_context} if self.ssl_context else None)
        ws.connect(f"{self.protocol_ws}://{self.server_url}/ws?clientId={client_id}", timeout=self.timeout,
                   header=header_list)
        self.ws = ws
        self.client_id = client_id
        return ws, self.server_url, client_id

    def close_ws(self):
        if self.ws is not None:
            if self.ws.connected:
                try:
                    self.ws.close()
                    logger.debug('Websocket connection closed.')
                    self.ws = None
                except Exception as e:
                    logger.error(f"Failed to close websocket: {e}")
            else:
                logger.warning('Websocket is already closed.')
        else:
            logger.debug('No websocket connection to close.')

    @retry(
        reraise=True,  # чтобы наружу прилетало исходное исключение (HTTPError), а не RetryError
        stop=stop_after_attempt(3),               # максимум 4 попытки (3 retry)
        wait=wait_fixed(4),                       # пауза 4 секунд между попытками
        before_sleep=before_sleep_log(logger, logging.WARNING)  # лог перед retry
    )
    def queue_prompt(self, prompt):
        client_id = self.client_id
        if client_id == '':
            raise ValueError('client_id empty')
        url = f"{self.protocol_http}://{self.server_url}/prompt"
        data = json.dumps({"prompt": prompt, "client_id": client_id}).encode('utf-8')
        request = urllib.request.Request(url, data=data, headers=self.headers)
        with urllib.request.urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
            return json.loads(response.read())

    @retry(
        reraise=True,  # чтобы наружу прилетало исходное исключение (HTTPError), а не RetryError
        stop=stop_after_attempt(3),               # максимум 4 попытки (3 retry)
        wait=wait_fixed(2),                       # пауза 4 секунд между попытками
        before_sleep=before_sleep_log(logger, logging.WARNING)  # лог перед retry
    )
    def get_history(self, prompt_id):
        if not prompt_id:
            raise ValueError("prompt_id is required.")
        if not isinstance(prompt_id, str):
            logger.error(f"prompt_id is : {str(prompt_id)[:1500]}")
            logger.error(traceback.format_exc())
            raise TypeError(f"Expected prompt_id as string, but got {type(prompt_id)}: {repr(prompt_id)}")

        url = f"{self.protocol_http}://{self.server_url}/history/{prompt_id}"
        request = urllib.request.Request(url, headers=self.headers)

        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
                if response.status == 200:
                    return json.loads(response.read())
                else:
                    logger.warning(f"Unexpected HTTP status code: {response.status}")
                    return None
        except urllib.error.HTTPError as e:
            logger.error(f"HTTPError: {e.code} - {e.reason}")
            raise
        except urllib.error.URLError as e:
            logger.error(f"URLError: {e.reason}")
            raise
        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode JSON response: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error in get_history: {e}")
            raise

    def interrupt_all(self, prompt_id):
        """
        Cancel the current processing.
        :param prompt_id: Identifier of the prompt to interrupt.
        :return: Parsed JSON response.
        """
        if not prompt_id:
            raise ValueError("prompt_id is required.")
        client_id = self.client_id
        if client_id == '':
            raise ValueError("client_id empty")
        # Используем URL без prompt_id в пути, так как сервер ожидает POST /interrupt
        url = f"{self.protocol_http}://{self.server_url}/interrupt"
        # В теле запроса можно передать prompt_id, если нужно
        data = json.dumps({"client_id": client_id, "prompt_id": prompt_id}).encode("utf-8")
        # TODO: похоже data не нужно
        request = urllib.request.Request(url, data=data, headers=self.headers, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
            return response.read()

    def modify_queue(self, clear=False, delete_ids=None):
        # TODO: похоже не работает, возвращает пустое сообщение
        """
        Modify the prompt queue on the server.

        :param clear: Если True, очищает всю очередь.
        :param delete_ids: Список prompt_id для удаления из очереди.
        :return: Разобранный JSON-ответ сервера.
        """
        client_id = self.client_id
        if client_id == '':
            raise ValueError("client_id empty")

        url = f"{self.protocol_http}://{self.server_url}/queue"

        # Формируем тело запроса
        payload = {"client_id": client_id}
        if clear:
            payload["clear"] = True
        if delete_ids:
            payload["delete"] = [delete_ids]

        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=self.headers, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
            return response.read()

    @retry(
        reraise=True,  # чтобы наружу прилетало исходное исключение (HTTPError), а не RetryError
        stop=stop_after_attempt(3),               # максимум 4 попытки (3 retry)
        wait=wait_fixed(4),                       # пауза 4 секунд между попытками
        before_sleep=before_sleep_log(logger, logging.WARNING)  # лог перед retry
    )
    def history_delete(self, prompt_ids):
        """
        Удаление истории, передавая список prompt_id в теле POST-запроса.

        :param prompt_ids: Список идентификаторов для удаления.
        """
        if not prompt_ids:
            raise ValueError("Список prompt_ids не может быть пустым.")

        # Например, удаление по адресу /history (без prompt_id в URL)
        url = f"{self.protocol_http}://{self.server_url}/history"

        # Формируем JSON-полезную нагрузку вида {"delete": [...список ID...]}
        data_dict = {"delete": [prompt_ids]}
        data_bytes = json.dumps(data_dict).encode("utf-8")

        # Создаем POST-запрос
        request = urllib.request.Request(
            url,
            data=data_bytes,
            headers={**self.headers, "Content-Type": "application/json"},
            method="POST"
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
                if response.status == 200:
                    logger.info(f"History for prompt_ids={prompt_ids} successfully deleted.")
                    return True
                else:
                    logger.warning(f"Unexpected HTTP status code: {response.status}")
                    return False

        except urllib.error.HTTPError as e:
            logger.error(f"HTTPError: {e.code} - {e.reason}")
            raise
        except urllib.error.URLError as e:
            logger.error(f"URLError: {e.reason}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error in delete_history: {e}")
            raise

    @retry(
        reraise=True,  # чтобы наружу прилетало исходное исключение (HTTPError), а не RetryError
        stop=stop_after_attempt(3),               # максимум 4 попытки (3 retry)
        wait=wait_fixed(4),                       # пауза 4 секунд между попытками
        before_sleep=before_sleep_log(logger, logging.WARNING)  # лог перед retry
    )
    def get_image(self, filename, subfolder, folder_type):
        data = {"filename": filename, "subfolder": subfolder, "type": folder_type}
        url_values = urllib.parse.urlencode(data)
        url = f"{self.protocol_http}://{self.server_url}/view?{url_values}"
        request = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
            return response.read()

    @retry(
        reraise=True,  # чтобы наружу прилетало исходное исключение (HTTPError), а не RetryError
        stop=stop_after_attempt(3),               # максимум 4 попытки (3 retry)
        wait=wait_fixed(4),                       # пауза 4 секунд между попытками
        before_sleep=before_sleep_log(logger, logging.WARNING)  # лог перед retry
    )
    def upload_image(
            self,
            input_path: str,
            name: str | None = None,
            image_type: str = "input",
            overwrite: bool = True,
    ):
        """
        Загружает изображение на сервер ComfyUI.

        :param input_path: локальный путь к файлу
        :param name: имя файла на сервере (по умолч. берётся из input_path)
        :param image_type: 'input' или 'output' и т. д.
        :param overwrite: перезаписать существующий файл
        :return: bytes ответа сервера
        """
        if name is None:
            name = os.path.basename(input_path)

        mime, _ = mimetypes.guess_type(name)
        if mime is None:
            mime = "application/octet-stream"

        url = f"{self.protocol_http}://{self.server_url}/upload/image"

        # Формируем настройки TLS: True, путь к CA или отключаем проверку
        verify_opt: bool | str = True
        if self.certificate_path:
            verify_opt = self.certificate_path
        elif self.allow_self_signed_cert:
            verify_opt = False

        headers = {"Authorization": f"Bearer {self.open_button_token}"}

        with open(input_path, "rb") as f:
            files = {"image": (name, f, mime)}
            data = {"type": image_type, "overwrite": str(overwrite).lower()}
            resp = requests.post(
                url,
                headers=headers,
                files=files,
                data=data,
                timeout=self.timeout,
                verify=verify_opt,
            )

        resp.raise_for_status()  # выбросит исключение при 4xx/5xx
        return resp.content

    def deprecated_upload_image(self, input_path, name, image_type="input", overwrite=True):
        with open(input_path, 'rb') as file:
            multipart_data = requests_toolbelt.MultipartEncoder(
                fields={
                    'image': (name, file, 'image/jpeg'),
                    'type': image_type,
                    'overwrite': str(overwrite).lower()
                }
            )
            data = multipart_data
            headers = {**self.headers, 'Content-Type': multipart_data.content_type}
            url = f"{self.protocol_http}://{self.server_url}/upload/image"
            request = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
                return response.read()

    @retry(
        reraise=True,  # чтобы наружу прилетало исходное исключение (HTTPError), а не RetryError
        stop=stop_after_attempt(3),               # максимум 4 попытки (3 retry)
        wait=wait_fixed(4),                       # пауза 4 секунд между попытками
        before_sleep=before_sleep_log(logger, logging.WARNING)  # лог перед retry
    )
    def upload_image64(self, base64_data, name, image_type="input", overwrite=True):
        """
        Загружаем на сервер ComfyUI в папку image_type картинку
        :param base64_data: тело картинки в base64
        :param name: имя картинки
        :param image_type: папка назначения
        :param overwrite: bool
        :return:
        """
        decoded_image = base64.b64decode(base64_data)

        image_file = BytesIO(decoded_image)
        image_file.name = name  # Указываем имя файла для отправки

        # Определяем MIME-тип по расширению файла
        import mimetypes
        mime_type, _ = mimetypes.guess_type(name)
        if mime_type is None:
            mime_type = 'application/octet-stream'  # Тип по умолчанию, если не удалось определить
            logger.warning(f'Error get file type for {name}')

        # Формируем Multipart данные
        multipart_data = requests_toolbelt.MultipartEncoder(
            fields={
                'image': (name, image_file, mime_type),
                'type': image_type,
                'overwrite': str(overwrite).lower()
            }
        )

        # Настройка заголовков и URL_PROCESS
        headers = {**self.headers, 'Content-Type': multipart_data.content_type}
        url = f"{self.protocol_http}://{self.server_url}/upload/image"
        logger.debug(f'url: {url}')

        # Отправка запроса
        request = urllib.request.Request(url, data=multipart_data, headers=headers)
        with urllib.request.urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
            return response.read()

    def load_workflow(self, workflow_path):
        try:
            with open(workflow_path, 'r', encoding="utf-8") as file:
                workflow = json.load(file)
                return workflow  # json.dumps(workflow)
        except FileNotFoundError:
            logger.error(f"The file {workflow_path} was not found.")
            raise FileNotFoundError(f"The file {workflow_path} was not found.")
        except json.JSONDecodeError:
            logger.error(f"The file {workflow_path} contains invalid JSON.")
            raise json.JSONDecodeError(f"The file {workflow_path} contains invalid JSON.", workflow_path, 0)

    def track_progress(self, prompt, prompt_id):
        """
        Отслеживаем выполнение промпта, пока промпт работает мы ждем его завершения
        jobs.job_manager - кидаем туда callback()
        В самом начале в jobs.job_manager создается новая задача (и она обновляется по мере прохождения промпта)
        :param prompt:
        :param prompt_id:
        :return:
        """
        ws = self.ws
        node_ids = list(prompt.keys())
        finished_nodes = []
        job_id = f'ComfyUI progress {self.job_name}'
        info_str = ''
        self.errors=''

        node_time_start = time.time()
        steps=0

        MAX_RECV_RETRIES = 5  # “парочка”
        RECV_RETRY_SLEEP_SEC = 1.0  # пауза между попытками

        try:
            if job_id in jobs.job_manager.get_jobs():
                logger.warning(f'{job_id} in jobs.job_manager.get_jobs(): {jobs.job_manager.get_jobs()}')
            else:
                jobs.job_manager.add_job(job_id, 'Started')
            if not jobs.job_manager.acquire_gpu(job_id, 10 * 3600):
                raise TimeoutError('Error acquire_gpu!')
            while True:
                out = None
                last_err = None
                for attempt in range(MAX_RECV_RETRIES + 1):
                    try:
                        out = ws.recv()
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                        logger.warning(f"ws.recv() failed (attempt {attempt + 1}/{MAX_RECV_RETRIES + 1}): {e}")
                        if attempt < MAX_RECV_RETRIES:
                            time.sleep(RECV_RETRY_SLEEP_SEC)

                if last_err is not None:
                    # дальше уже решай: либо мягко выйти, либо упасть с понятной ошибкой
                    raise TimeoutError("ws.recv() repeatedly failed") from last_err

                if isinstance(out, str):
                    message = json.loads(out)
                    try:
                        if self.callback is not None:
                            self.callback(self, message)
                    except Exception as e:
                        logger.error("Error callback:", e)
                    # logger.debug(f'{message}')
                    if message['type'] == 'execution_error':
                        logger.error(f"Error workflow execution:\n{message}\n============================")
                    if message['type'] == 'progress':
                        data = message['data']
                        current_step = data['value']
                        logger.trace(f"    Step: {current_step} of: {data['max']}")
                        steps += 1
                        jobs.job_manager.update_job(job_id, info_str + f"; Step: {current_step} of: {data['max']}")
                    if message['type'] == 'execution_cached':
                        data = message['data']
                        for itm in data['nodes']:
                            if itm not in finished_nodes:
                                finished_nodes.append(itm)
                                time_delta = time.time() - node_time_start
                                node_time_start = time.time()
                                info_str = (
                                    f'ComfyUI cached progess: {len(finished_nodes)} / {len(node_ids)} Tasks done; time={time_delta:.1f} steps={steps}')
                                steps = 0
                                logger.debug(info_str)
                                jobs.job_manager.update_job(job_id, info_str)
                    if message['type'] == 'executing':
                        data = message['data']
                        if data['node'] not in finished_nodes:
                            finished_nodes.append(data['node'])
                            time_delta = time.time() - node_time_start
                            node_time_start = time.time()
                            info_str = (f'ComfyUI progess: node #{data["node"]}  {len(finished_nodes)} / {len(node_ids)} Tasks done; time={time_delta:.1f} steps={steps}')
                            steps = 0
                            logger.debug(info_str)
                            jobs.job_manager.update_job(job_id, info_str)

                        if data['node'] is None and data['prompt_id'] == prompt_id:
                            break  # Execution is done
                else:
                    continue
        finally:
            jobs.job_manager.release_gpu(job_id)
            jobs.job_manager.remove_job(job_id)

    def get_images(self, prompt_id, allow_preview=False):
        """
        Получение изображений по prompt_id.

        :param prompt_id: Идентификатор запроса
        :param allow_preview: Разрешить загрузку временных изображений (предпросмотр)
        :return: Список изображений с данными
        """
        if not isinstance(prompt_id, str):
            logger.error(f"prompt_id is : {str(prompt_id)[:1500]}")
            logger.error(traceback.format_exc())
            raise TypeError(f"Expected prompt_id as string, but got {type(prompt_id)}: {repr(prompt_id)}")

        output_images = []
        history = self.get_history(prompt_id)[prompt_id]

        for node_id in history['outputs']:
            node_output = history['outputs'][node_id]

            if 'images' in node_output:
                for image in node_output['images']:
                    output_data = {}
                    if allow_preview and image['type'] == 'temp':
                        preview_data = self.get_image(
                            image['filename'],
                            image['subfolder'],
                            image['type']
                        )
                        output_data['image_data'] = preview_data
                    if image['type'] == 'output':
                        image_data = self.get_image(
                            image['filename'],
                            image['subfolder'],
                            image['type']
                        )
                        output_data['image_data'] = image_data

                    output_data['file_name'] = image['filename']
                    output_data['type'] = image['type']
                    output_images.insert(0, output_data)
                    # output_images.append(output_data)

        if len(output_images) < 1:
            logger.info('output_images empty')

        return output_images

    def do_workflow(self, workflow: dict[str, Any], need_return_images=True) -> WorkflowResult:
        """
        Запускает процесс рендеринга на сервере **ComfyUI** и при необходимости
        возвращает сгенерированные изображения. По сути комбайн "всё в одном": открываем соединение, закидываем промпт,
        ждем завершения, скачиваем результат.

        Args:
            workflow (dict[str, Any]): Сериализованный workflow, подготовленный для ComfyUI.
            need_return_images (bool, optional):
                * **True** — после завершения рендера дополнительно загрузить все
                  созданные изображения и вернуть их в формате *Base-64*
                * **False** — вернуть только историю выполнения и список имён файлов.
                По умолчанию — ``True``.

        Returns:
            WorkflowResult:
            Словарь с ключами

            * ``history`` — результат :py:meth:`get_history`, содержащий подробную
              информацию о ходе выполнения запроса.
            * ``image_filenames`` — список имён файлов, полученных из
              :py:meth:`get_media_names`.
            * ``images`` — список строк *Base-64*, полученных из
              :py:meth:`get_images64`, либо ``None``, если ``need_return_images``
              установлено в ``False``.

        Notes:
            * Значение ``history`` не типизировано жёстко, так как структура,
              возвращаемая ComfyUI, может изменяться между версиями.
            * Использование `TypedDict` повышает статическую проверяемость кода и
              упрощает автодополнение в IDE.

        """

        # Запуск генерации
        result: WorkflowResult = {}
        self.open_websocket_connection()
        resp = self.queue_prompt(workflow)
        prompt_id = resp['prompt_id']
        self.track_progress(workflow, prompt_id)
        hist = self.get_history(prompt_id)
        result['history'] = hist
        logger.debug(f'hist={str(hist)[:650]}')
        image_names = self.get_media_names(prompt_id)
        result['image_filenames'] = image_names
        logger.debug(f'ended with len(images)={len(image_names)} images={str(image_names)[:610]} ')
        if len(image_names) < 1:
            logger.warning('image_filenames empty!')
            logger.debug(f"history\n{'=' * 50}\n{hist}\n{'=' * 50}\n")
        if not need_return_images:
            result['images'] = None
        else:
            result['images'] = self.get_images64(image_names)
        return result

    def get_media_names(self, prompt_id, allow_preview=False):
        """
        Получение изображений и GIF-файлов по prompt_id.

        :param prompt_id: Идентификатор запроса
        :param allow_preview: Разрешить загрузку временных данных (предпросмотр)
        :return: Список медиафайлов с данными
        """
        media_files = []
        history = self.get_history(prompt_id)[prompt_id]

        debug_str = ''

        for node_output in history['outputs'].values():
            for media_type in ['images', 'gifs', 'audio']:
                if media_type in node_output:
                    for media in node_output[media_type]:
                        if allow_preview and media['type'] == 'temp':
                            media_data = self.get_image(
                                media['filename'],
                                media.get('subfolder', ''),
                                media['type']
                            )
                        elif media['type'] == 'output':
                            media_data = self.get_image(
                                media['filename'],
                                media.get('subfolder', ''),
                                media['type']
                            )
                        else:
                            continue

                        media_files.append({
                            'file_name': media['filename'],
                            'type': media['type'],
                            'media_data': media_data
                        })
        if len(media_files) < 1:
            logger.warning('media files empty! see locals_get_media.json')
            h.write_variables_to_json(locals(), 'locals_get_media.json')
        return media_files

    def save_image(self, images, output_path, save_previews=False):
        """
        Сохранение изображений на диск.

        :param images: Список изображений с данными (каждый элемент должен содержать 'image_data', 'file_name' и 'type')
        :param output_path: Путь для сохранения
        :param save_previews: Сохранять ли временные изображения (предпросмотр)
        """
        results = []
        for itm in images:
            if itm['type'] == 'temp' and not save_previews:
                continue
            directory = os.path.join(output_path, 'temp/') if itm['type'] == 'temp' and save_previews else output_path
            os.makedirs(directory, exist_ok=True)

            img_path = os.path.join(directory, itm['file_name'])
            try:
                with open(img_path, 'wb') as f:
                    f.write(itm['image_data'])
                results.append(img_path)
            except Exception as e:
                logger.error(f"Failed to process image {itm['file_name']} to {output_path}: {e} for {h_log.summarize_params(itm)}")
        return results

    def get_images64(self, images, save_previews=False):
        """
        Возвращает изображения в виде списка Base64-encoded строк.

        :param images: Список изображений с данными
        :param save_previews: Включать ли временные изображения (предпросмотр)
        :return: Список Base64-encoded строк
        """
        results = []
        for itm in images:
            # Пропускаем временные изображения, если save_previews=False
            if itm['type'] == 'temp' and not save_previews:
                continue

            try:
                base64_image = base64.b64encode(itm['image_data']).decode('utf-8')
                results.append(base64_image)
            except Exception as e:
                logger.error(f"Failed to process image {itm['file_name']}: {e} for {h_log.summarize_params(itm)}")
        if len(results) < 1:
            logger.info('output_images empty')

        return results

    @retry(
        reraise=True,  # чтобы наружу прилетало исходное исключение (HTTPError), а не RetryError
        stop=stop_after_attempt(3),               # максимум 4 попытки (3 retry)
        wait=wait_fixed(4),                       # пауза 4 секунд между попытками
        before_sleep=before_sleep_log(logger, logging.WARNING)  # лог перед retry
    )
    def get_prompts(self):
        """
        Возвращает информацию о текущей очереди запросов (prompt queue).
        :return:
        """
        url = f"{self.protocol_http}://{self.server_url}/prompt"
        request = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
            return json.loads(response.read())

    @retry(
        reraise=True,  # чтобы наружу прилетало исходное исключение (HTTPError), а не RetryError
        stop=stop_after_attempt(3),               # максимум 4 попытки (3 retry)
        wait=wait_fixed(4),                       # пауза 4 секунд между попытками
        before_sleep=before_sleep_log(logger, logging.WARNING)  # лог перед retry
    )
    def get_prompt(self):
        if self.client_id == '':
            raise ValueError('client_id empty')
        # Добавляем client_id в параметры запроса, если требуется
        url = f"{self.protocol_http}://{self.server_url}/prompt?client_id={self.client_id}"
        request = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
            return json.loads(response.read())

    @retry(
        reraise=True,  # чтобы наружу прилетало исходное исключение (HTTPError), а не RetryError
        stop=stop_after_attempt(3),               # максимум 4 попытки (3 retry)
        wait=wait_fixed(4),                       # пауза 4 секунд между попытками
        before_sleep=before_sleep_log(logger, logging.WARNING)  # лог перед retry
    )
    def get_system_stats(self):
        url = f"{self.protocol_http}://{self.server_url}/system_stats"
        request = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
            return json.loads(response.read())

    def __del__(self):
        try:
            self.close_ws()
            logger.info('ConnectionManager instance destroyed, websocket closed.')
        except Exception as e:
            logger.error(f"Error during cleanup in __del__: {e}")


def find_replace(input_obj, regex_pattern, replacement_string):
    """
    Функция для поиска и замены с использованием регулярных выражений.
    Если входное значение не строка, оно преобразуется в JSON.

    :param input_obj: Исходная строка или объект, который можно преобразовать в JSON
    :param regex_pattern: Регулярное выражение для поиска
    :param replacement_string: Строка для замены
    :return: Строка или объект с заменой
    """
    was_converted = False

    # Если input_obj не строка, преобразуем его в JSON
    if not isinstance(input_obj, str):
        try:
            input_obj = json.dumps(input_obj, ensure_ascii=False, indent=2)
            was_converted = True
        except (TypeError, ValueError) as e:
            logger.error(f"Error when converting to JSON: {e}")
            return input_obj  # Возвращаем исходное значение, если преобразование невозможно

    # Применяем замену по регулярному выражению с использованием re.subn для получения количества замен
    try:
        result, count = re.subn(regex_pattern, replacement_string, input_obj)
        if count == 0:
            logger.warning(f"The pattern {regex_pattern} was not found in the source text.")
    except re.error as e:
        logger.error(f"Error in regular expression: {e}")
        return input_obj

    if was_converted:
        try:
            return json.loads(result)
        except json.JSONDecodeError as e:
            logger.error(f"Error during inverse conversion from JSON: {e}")
            return result  # Возвращаем строку, если декодировать обратно невозможно

    return result
