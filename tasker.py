"""
python -m ~/tasker.py <tasker_id> <mode>
"""
import argparse
import functools
import json
import subprocess
import time
from pathlib import Path

from .logger import Logger

CWD_PATH = Path.cwd()

def lock() -> bool:
    if lock_file.exists():
        logger.logger.warning("Lock file already exists. Another instance may be running.")
        return False
    lock_file.touch(exist_ok=False)
    return True

def check_lock() -> bool:
    return lock_file.exists()

def unlock():
    if not lock_file.exists():
        logger.logger.warning("Failed to unlock - lock file not exists.")
    lock_file.unlink()

def synchronized(level="negative"):
    """
    Decorator factory that returns a decorator which
    level is positive: lock the function while running;
             negative: wait for the lock to be released before running.
    """
    assert level in ["positive", "negative"]
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            while not lock() if level == "positive" else check_lock():
                time.sleep(1)  # Wait for the lock to be released
            result = func(*args, **kwargs)
            if level == "positive": unlock()
            return result
        return wrapper
    return decorator

class Task:
    def __init__(self, task_id: int, work_dir: str, command: str, status: str):
        self.task_id: int = task_id
        self.work_dir: str = work_dir
        self.command: str = command
        self.status: str = status
        assert self.status in ["pending", "running", "completed", "failed", "canceled"], "Invalid status"
    
    @synchronized("positive")
    def save(self):
        try:
            with open(tasker_file, 'r') as f:
                tasks = json.load(f)
            tasks[str(self.task_id)]["status"] = self.status
            with open(tasker_file, 'w') as f:
                json.dump(tasks, f, indent=4)
        except Exception as e:
            logger.logger.error(f"Error saving task {self.task_id}: {e}")

    def run(self):
        """
        Run the task command in its work directory.
        """
        if not self.status == "pending":
            logger.logger.warning(f"Task {self.task_id} is not pending: {self.status}")
            self.save()
            return
        
        if not Path(self.work_dir).exists():  # Ensure the work directory exists
            self.status = "failed"
            logger.logger.error(f"Task {self.task_id} failed - work directory '{self.work_dir}' does not exist.")
            self.save()
            return
        
        # Run the command
        self.status = "running"
        logger.logger.info(f"Running task {self.task_id} in '{self.work_dir}': {self.command}")
        self.save()
        try:
            result = subprocess.run(self.command, shell=True, cwd=self.work_dir)
            if result.returncode == 0:
                self.status = "completed"
            else:
                self.status = "failed"
        except Exception as e:
            self.status = "failed"
            logger.logger.error(f"Error running task {self.task_id}: {e}")
        finally:
            self.save()
            logger.logger.info(f"Task {self.task_id} finished with status: {self.status}")

class Tasker:
    def __init__(self):
        self.task: Task = None
    
    @synchronized("positive")
    def load_task(self, task_id: int) -> str:
        try:
            with open(tasker_file, 'r') as f:
                tasks = json.load(f)
            if str(task_id) not in tasks: return "invalid-key"
            self.task = Task(
                task_id=task_id,
                work_dir=tasks[str(task_id)]["wd"],
                command=tasks[str(task_id)]["cmd"],
                status=tasks[str(task_id)]["status"]
            )
            return "done"
        except Exception as e:
            logger.logger.error(f"Error loading task {task_id}: {e}")
            return "error"
    
    def run(self) -> int:
        """
        Run all pending tasks, and return the count of tasks run. 
        """
        i, count = 0, 0
        while True:
            i += 1
            status = self.load_task(i)
            if status == "invalid-key": break
            elif status == "error": 
                count += 1
                continue

            if self.task.status == "pending":
                count += 1
                self.task.run()
        return count

class Operator:
    def __init__(self, _tasker_id: str):
        global tasker_id, logger, tasker_file, lock_file
        tasker_id = _tasker_id
        logger = Logger(name=str(Path.home() / "opt" / f"_tasker_{tasker_id}"), 
                        level="INFO", width=50)
        tasker_file = Path.home() / "opt" / f".tasker_{tasker_id}.json"
        lock_file = Path.home() / "opt" / f".tasker.{tasker_id}.lock"
        self.run_file = Path.home() / "opt" / f".tasker.{tasker_id}.run"
        tasker_file.parent.mkdir(parents=True, exist_ok=True)

        self.tasker = Tasker()
        self.tasks: dict = None
    
    def load_tasks(self) -> int:
        try:
            with open(tasker_file, 'r') as f:
                self.tasks = json.load(f)
            return len(self.tasks)
        except FileNotFoundError:
            logger.logger.warning(f"Tasks file not found. Creating a new one.")
            self.tasks = {}
            with open(tasker_file, 'w') as f:
                json.dump({}, f)  # Create an empty task file
            return 0
        except Exception as e:
            logger.logger.critical(f"Error loading tasks: {e}")
            raise SystemError
        
    def check_valid_tasks(self) -> bool:
        """Check if all task keys are ordered"""
        if self.tasks is None:
            return False
        # Convert string keys to integers for comparison
        try:
            int_keys = [int(k) for k in self.tasks.keys()]
            return int_keys == list(range(1, len(self.tasks) + 1))
        except ValueError:
            return False
    
    def save(self):
        try:
            with open(tasker_file, 'w') as f:
                json.dump(self.tasks, f, indent=4)
        except Exception as e:
            logger.logger.critical(f"Error saving tasks: {e}")
            raise SystemError
        
        if not self.check_valid_tasks():
            logger.logger.critical(f"Invalid tasks.")
            raise SystemError
        
    def run(self):
        """Run all pending tasks."""
        if self.run_file.exists():
            logger.logger.error(f"Tasker {tasker_id} is running. Exiting.")
            return
        self.run_file.touch(exist_ok=False)  # Create a run file to indicate running state
        
        flag, max_wait = False, 3 * (24*(60*60))
        start_wait = None  # Initialize start_wait
        while True:
            n_runs = self.tasker.run()
            
            if n_runs == 0:
                if not flag:  # First time no tasks to run
                    logger.logger.info("All tasks done. Waiting for new tasks...")
                    start_wait = time.time()
                    flag = True
                else:
                    if start_wait is not None and time.time() - start_wait > max_wait:
                        logger.logger.info("No tasks run for a long time, exiting.")
                        break
                    else: 
                        time.sleep(10)  # Wait before checking again
            else:
                flag = False  # Reset flag if tasks were run
        
        self.run_file.unlink()  # Remove run file when done
    
    @synchronized("positive")
    def list(self, only_pending=True):
        """
        List all tasks.
        'only_pending' to show pending and running tasks.
        """
        logger.logger.info(f"[USER OPERATION] List all {'pending' if only_pending else ''} tasks")
        logger.divider.word_line("list")
        
        n_tasks = self.load_tasks()
        if n_tasks == 0 or ( only_pending and 
                sum(1 for task in self.tasks.values() if task['status'] == 'pending') == 0 ):
            logger.divider.write("No tasks in the queue.")
        else:
            for task_id, task_info in self.tasks.items():
                if only_pending and task_info['status'] not in ['pending', 'running']:
                    continue
                logger.divider.write(f"{task_id:>5} | {task_info['status']}\n"
                                     f"      | {task_info['cmd']}\n"
                                     f"      | {task_info['wd']}\n")

        logger.divider.word_line("list")
    
    @synchronized("positive")
    def append(self, command: str, work_dir: str=CWD_PATH):
        """Append a new task to the queue."""
        logger.logger.info(f"[USER OPERATION] Appended task")
        logger.divider.word_line("append")

        work_dir = Path(work_dir).resolve()
        n_tasks = self.load_tasks()
        task_id = n_tasks + 1
        self.tasks[str(task_id)] = {
            "wd": str(work_dir),
            "cmd": command,
            "status": "pending"
        }
        self.save()

        logger.divider.write(f"Appended task {task_id}:\n"
                             f"    Command: {command}\n"
                             f"    Work Directory: {work_dir}\n")
        logger.divider.word_line("append")
    
    @synchronized("positive")
    def insert(self, pos: int, command: str, work_dir: str=CWD_PATH):
        """Insert a new task before position POS (1-based)."""
        logger.logger.info(f"[USER OPERATION] Insert task at position {pos}")
        logger.divider.word_line("insert")

        work_dir = Path(work_dir).resolve()
        n_tasks = self.load_tasks()
        if pos < 1 or pos > n_tasks + 1:
            logger.logger.error(f"Invalid position {pos} for insertion.")
            return
        
        new_tasks = {}
        for i in range(1, pos):
            new_tasks[str(i)] = self.tasks[str(i)]
        new_tasks[str(pos)] = {
            "wd": str(work_dir),
            "cmd": command,
            "status": "pending"
        }
        for i in range(pos, n_tasks + 1):
            new_tasks[str(i + 1)] = self.tasks[str(i)]
        self.tasks = new_tasks
        self.save()

        logger.divider.write(f"Inserted task {pos}:\n"
                             f"    Command: {command}\n"
                             f"    Work Directory: {work_dir}\n")
        logger.divider.word_line("insert")

    @synchronized("positive")
    def remove(self, pos: int):
        """Remove the task at position POS (1-based)."""
        logger.logger.info(f"[USER OPERATION] Remove task at position {pos}")
        logger.divider.word_line("remove")

        n_tasks = self.load_tasks()
        if pos < 1 or pos > n_tasks:
            logger.logger.error(f"Invalid position {pos} for removal.")
            return
        
        if self.tasks[str(pos)]["status"] == "pending":
            self.tasks[str(pos)]["status"] = "canceled"
            self.save()
            removed_task = self.tasks[str(pos)]

            logger.divider.write(f"Removed task at position {pos}:\n"
                                 f"    Command: {removed_task['cmd']}\n"
                                 f"    Work Directory: {removed_task['wd']}\n")
        else:
            logger.logger.error(f"Error removing task: Not pending.")
        logger.divider.word_line("remove")

    @synchronized("positive")
    def swap(self, pos1: int, pos2: int):
        logger.logger.info(f"[USER OPERATION] Swap tasks at position {pos1} and {pos2}")
        logger.divider.word_line("swap")

        n_tasks = self.load_tasks()
        if pos1 < 1 or pos1 > n_tasks or pos2 < 1 or pos2 > n_tasks or pos1 == pos2:
            logger.logger.error(f"Invalid position {pos1} and {pos2} for swap.")
            return
        
        # Swap the tasks
        self.tasks[str(pos1)], self.tasks[str(pos2)] = self.tasks[str(pos2)], self.tasks[str(pos1)]
        self.save()

        logger.divider.write(f"Swapped tasks at positions {pos1} and {pos2}, now:\n"
                             f"{pos1:>5}: {self.tasks[str(pos1)]['cmd']}\n"
                             f"{pos2:>5}: {self.tasks[str(pos2)]['cmd']}\n")
        logger.divider.word_line("swap")

def main(args):
    operator = Operator(args.tasker_id)

    if args.mode == "run":
        operator.run()
    
    elif args.mode == "ls":
        operator.list(only_pending=True)
    
    elif args.mode == "lsall":
        operator.list(only_pending=False)
    
    elif args.mode == "add":
        command = input("Command: ").strip()
        work_dir = input(f"Work directory: (default '.') ").strip() or CWD_PATH
        operator.append(command, work_dir)
    
    elif args.mode == "in":
        position = int(input("Position to insert at (1-based): "))
        command = input("Command: ").strip()
        work_dir = input(f"Work directory: (default '.') ").strip() or CWD_PATH
        operator.insert(position, command, work_dir)
    
    elif args.mode == "rm":
        position = int(input("Position to remove (1-based): "))
        operator.remove(position)

    elif args.mode == "swap":
        pos1 = int(input("Position 1 to swap (1-based): "))
        pos2 = int(input("Position 2 to swap (1-based): "))
        operator.swap(pos1, pos2)
    
    else:
        logger.logger.error(f"Unknown mode: {args.mode}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simple queue-based task runner.")
    parser.add_argument("tasker_id", type=str, help="Tasker ID to identify the task queue")
    parser.add_argument("mode", type=str, help="Execute mode")
    args = parser.parse_args()
    main(args)