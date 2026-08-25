from loguru import logger
import threading
import traceback
from typing import Any, Dict, List, Optional
import time

class JobManager:
    """
    Thread-safe job manager class for maintaining information about running Flask server tasks.

    This class stores jobs with a unique job id (key) and an associated value. It provides
    methods to add a new job, remove an existing job, update an existing job's status,
    retrieve a list of all current jobs, and fetch a single job's details.

    Example usage:

        job_manager = JobManager()
        job_manager.add_job("job1", {"task": "data processing", "status": "running"})
        job_manager.update_job("job1", {"status": "completed"})
        single_job = job_manager.get_job("job1")
        all_jobs = job_manager.get_jobs()
        job_manager.remove_job("job1")

    Attributes:
        _jobs (Dict[str, Any]): Internal dictionary to store job information.
        _lock (threading.Lock): Lock to ensure thread-safety.
    """

    def __init__(self) -> None:
        """Initialize the JobManager with an empty job dictionary and a lock for thread safety."""
        self._jobs: Dict[str, Any] = {} # тут хранится список работ
        self._lock: threading.Lock = threading.Lock()
        # GPU status list where each element represents the owner of a queue.
        # Index 0 is the default queue, index 1 is reserved for sound processing.
        # A value of None means the queue is free.
        self.GPU_status: List[Optional[str]] = [None, None]
        # Separate condition per queue to allow independent waiting/notification
        self._gpu_status_lock = threading.Lock()
        self._gpu_conds: List[threading.Condition] = [
            threading.Condition(threading.Lock()) for _ in self.GPU_status
        ]
        logger.debug("JobManager initialized with an empty job list.")

    def acquire_gpu(self, owner: str, timeout: float = None, device: int = -1) -> bool:
        """
        Попытаться занять GPU под именем owner.
        Если очередь занята, ждём не дольше, чем timeout (в секундах).
        Возвращает True при успешном захвате, False при таймауте.

        Args:
            owner (str): имя владельца, который пытается занять GPU.
            timeout (float, optional): максимальное время ожидания в секундах.
            device (int, optional): номер очереди. Если -1, очередь определяется
                автоматически на основе owner.
        """
        if device < 0:
            sound_markers = ["Sound API", "suppress_echo"]
            device = 1 if any(marker in owner for marker in sound_markers) else 0

        with self._gpu_status_lock:
            if device >= len(self.GPU_status):
                # расширяем список очередей при необходимости
                self.GPU_status.extend([None] * (device + 1 - len(self.GPU_status)))
                self._gpu_conds.extend(
                    [threading.Condition(threading.Lock()) for _ in range(device + 1 - len(self._gpu_conds))]
                )
            cond = self._gpu_conds[device]

        start = time.monotonic()
        logger.debug(
            f"Try GPU acquire for {owner} on device {device} with timeout {timeout} seconds..."
        )

        with cond:
            while self.GPU_status[device] is not None:
                remaining = None
                if timeout is not None:
                    elapsed = time.monotonic() - start
                    remaining = timeout - elapsed
                    if remaining <= 0:
                        logger.warning(
                            f"Timeout: {owner} unable to acquire the GPU device {device} within {timeout} seconds."
                        )
                        return False
                # ждём освобождения GPU
                cond.wait(timeout=remaining)
            # теперь GPU свободен
            self.GPU_status[device] = owner
            logger.debug(f"GPU device {device} acquired by {owner}")
            return True

    def release_gpu(self, owner: str) -> bool:
        """
        Освободить GPU, если текущий владелец совпадает с owner.
        Уведомляет один ожидающий поток (если есть).
        Возвращает True при успешном освобождении, False если владелец не найден.
        """
        with self._gpu_status_lock:
            try:
                device = self.GPU_status.index(owner)
                cond = self._gpu_conds[device]
            except ValueError:
                logger.error(
                    f"Unable to release GPU: {owner} is not the current owner (current: {self.GPU_status})."
                )
                return False

        with cond:
            self.GPU_status[device] = None
            # пробуждаем одного ожидающего потока
            cond.notify()
            logger.debug(f"GPU device {device} released by {owner}")
            return True

    def add_job(self, job_id: str, job_info: Any) -> None:
        """
        Add a new job to the manager.

        Args:
            job_id (str): Unique identifier for the job.
            job_info (Any): Information associated with the job.

        Raises:
            ValueError: If the job_id already exists.
        """
        with self._lock:
            try:
                if job_id in self._jobs:
                    raise ValueError(f"Job with id '{job_id}' already exists.")
                self._jobs[job_id] = job_info
                logger.info(f"Job '{job_id}' added successfully with info: {job_info}")
            except Exception as e:
                logger.error(f"Error adding job '{job_id}': {str(e)}")
                logger.debug(traceback.format_exc())
                raise

    def remove_job(self, job_id: str) -> None:
        """
        Remove a job from the manager.

        Args:
            job_id (str): Unique identifier for the job to remove.

        Raises:
            KeyError: If the job_id is not found.
        """
        with self._lock:
            try:
                if job_id not in self._jobs:
                    raise KeyError(f"Job with id '{job_id}' not found.")
                del self._jobs[job_id]
                logger.info(f"Job '{job_id}' removed successfully.")
            except Exception as e:
                logger.error(f"Error removing job '{job_id}': {str(e)}")
                logger.debug(traceback.format_exc())
                raise

    def update_job(self, job_id: str, update_info: Dict[str, Any]) -> None:
        """
        Update the information of an existing job.

        Args:
            job_id (str): Unique identifier for the job to update.
            update_info (Dict[str, Any]): Dictionary with the fields to update.
                For example, {"status": "completed"}.

        Raises:
            KeyError: If the job_id is not found.
        """
        with self._lock:
            try:
                if job_id not in self._jobs:
                    raise KeyError(f"Job with id '{job_id}' not found.")
                # Update job information if it is a dictionary; otherwise, overwrite it.
                if isinstance(self._jobs[job_id], dict):
                    self._jobs[job_id].update(update_info)
                else:
                    self._jobs[job_id] = update_info
                # logger.info(f"Job '{job_id}' updated successfully with info: {update_info}")
            except Exception as e:
                logger.error(f"Error updating job '{job_id}': {str(e)}")
                logger.debug(traceback.format_exc())
                raise

    def get_jobs(self) -> List[Dict[str, Any]]:
        """
        Retrieve a list of current jobs.

        Returns:
            List[Dict[str, Any]]: A list of dictionaries with job ids and their corresponding information.
        """
        with self._lock:
            try:
                jobs_list = [{"job_id": job_id, "job_info": info} for job_id, info in self._jobs.items()]
                # logger.debug("Job list retrieved successfully.")
                return jobs_list
            except Exception as e:
                logger.error(f"Error retrieving job list: {str(e)}")
                logger.debug(traceback.format_exc())
                raise

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve the information for a single job.

        Args:
            job_id (str): Unique identifier for the job.

        Returns:
            Optional[Dict[str, Any]]: A dictionary with job id and its corresponding information if found,
            otherwise None.
        """
        with self._lock:
            try:
                if job_id not in self._jobs:
                    logger.info(f"Job '{job_id}' not found.")
                    return None
                job_details = {"job_id": job_id, "job_info": self._jobs[job_id]}
                logger.debug(f"Job '{job_id}' retrieved successfully.")
                return job_details
            except Exception as e:
                logger.error(f"Error retrieving job '{job_id}': {str(e)}")
                logger.debug(traceback.format_exc())
                raise


job_manager = JobManager()

# Example usage of the JobManager class
if __name__ == "__main__":
    try:
        # Add a new job
        job_manager.add_job("job1", {"task": "data processing", "status": "running"})
        # Update the status of the job
        job_manager.update_job("job1", {"status": "completed"})
        # Retrieve a single job
        single_job = job_manager.get_job("job1")
        logger.info(f"Single job details: {single_job}")
        # Retrieve all jobs
        all_jobs = job_manager.get_jobs()
        logger.info(f"All jobs: {all_jobs}")
        # Remove the job
        job_manager.remove_job("job1")
    except Exception as main_e:
        logger.error(f"An error occurred in the main execution: {str(main_e)}")
        logger.debug(traceback.format_exc())
