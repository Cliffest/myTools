"""
python tasker.py <tasker_id> <mode>

You should NOT manually change the files under ~/opt/.tasker/ directory:
* Do NOT manually MODIFY the tasker file 'tasker.<id>.json'
* Do NOT manually REMOVE the run file '.tasker.<id>.run'
* Do NOT manually REMOVE the lock file '.tasker.<id>.lock'
"""
import argparse
import functools
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import List
try:
    from logger import Logger
except ImportError:
    from .logger import Logger

log_level = "INFO"
CWD_PATH = Path.cwd()

def lock() -> bool:
    if lock_file.exists():
        logger.logger.debug("Lock file already exists. Another instance may be running.")
        return False
    lock_file.touch(exist_ok=False)
    return True

def check_lock() -> bool:
    return lock_file.exists()

def unlock():
    if not lock_file.exists():
        logger.logger.warning("Failed to unlock - lock file not exists.")
    else: lock_file.unlink()

def synchronized(level="positive"):
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

def load_tasks() -> dict:
    """ **Should be wrapped with lock**. Return 'failure' on error. """
    try:
        with open(tasker_file, 'r') as f:
            tasks = json.load(f)
        return tasks
    except FileNotFoundError:
        logger.logger.warning(f"Tasks file not found. Creating a new one.")
        with open(tasker_file, 'w') as f:
            json.dump({}, f)
        return {}
    
    except Exception as e:
        logger.logger.error(f"Error loading tasks: {e}")
        return "failure"
    except KeyboardInterrupt:
        logger.logger.info("Keyboard interrupt received while loading tasks.")
        return "failure"
    finally: pass

class Task:
    def __init__(self, task_id: int, work_dir: str, command: str, status: str):
        self.task_id: int = task_id
        self.work_dir: str = work_dir
        self.command: str = command
        self.status: str = status
        assert self.status in ["pending", "running", "completed", "failed"], "Invalid status"
    
    @synchronized()
    def save(self, require: str):
        try:
            tasks = load_tasks()
            if not isinstance(tasks, dict) or not require in ["pending", "running"]: 
                raise ValueError("")
            found = False
            for k, v in tasks.items():  # First task whose content is same with the current task
                if v["cmd"] == self.command and v["wd"] == self.work_dir and v["status"] == require:
                    tasks[k]["status"], found = self.status, True
                    break
            if not found:  # If not found, add a new task
                task_id: int = len(tasks) + 1
                while str(task_id) in tasks:  # Find a new task ID
                    task_id += 1
                tasks[str(task_id)] = {
                    "wd": self.work_dir,
                    "cmd": self.command,
                    "status": self.status
                }
            shutil.copy(str(tasker_file), str(tasker_file) + ".copy")
            with open(tasker_file, 'w') as f:
                json.dump(tasks, f, indent=4)
        
        except Exception as e:
            logger.logger.error(f"Error saving task `{self.command}`: {e}. "
                                "This may cause fatal error. Please check.")
        except KeyboardInterrupt:
            logger.logger.error(f"Keyboard interrupt received while saving task `{self.command}`. "
                                "This may cause fatal error. Please check.")
        finally: pass

    def run(self):
        """
        Run the task command in its work directory.
        """
        if not self.status == "pending":
            logger.logger.warning(f"Task {self.task_id} is not pending: {self.status}")
            return
        
        if not Path(self.work_dir).exists():  # Ensure the work directory exists
            self.status = "failed"
            logger.logger.error(f"Task {self.task_id} failed - work directory '{self.work_dir}' does not exist.")
            self.save(require="pending")
            return
        
        # Run the command
        self.status = "running"
        logger.logger.info(f"Running task {self.task_id}: `{self.command}` in '{self.work_dir}'")
        self.save(require="pending")
        try:
            if log_level == "DEBUG":
                result = subprocess.run(self.command.split(), cwd=self.work_dir, capture_output=True, text=True)
            else:
                result = subprocess.run(self.command.split(), cwd=self.work_dir)
            if result.stdout: logger.logger.debug(f"Task `{self.command}` stdout: {result.stdout}")
            if result.stderr: logger.logger.debug(f"Task `{self.command}` stderr: {result.stderr}")
            if result.returncode == 0:
                self.status = "completed"
            else:
                self.status = "failed"
        except Exception as e:
            self.status = "failed"
            logger.logger.error(f"Error running task `{self.command}`: {e}")
        except KeyboardInterrupt:
            self.status = "failed"
            logger.logger.info(f"Task `{self.command}` interrupted by user.")
        finally:
            self.save(require="running")  # Save the task status after running
            logger.logger.info(f"Task `{self.command}` finished with status: {self.status}")

class Tasker:
    def __init__(self):
        self.task: Task = None
    
    @synchronized()
    def load_1st_pending_task(self) -> bool:
        """ Return 'failure' on error. """
        tasks = load_tasks()
        if not isinstance(tasks, dict): return "failure"
        
        try:
            for task_id in sorted(tasks.keys(), key=int):
                if tasks[task_id]["status"] == "pending":
                    self.task = Task(
                        task_id=int(task_id),
                        work_dir=tasks[task_id]["wd"],
                        command=tasks[task_id]["cmd"],
                        status=tasks[task_id]["status"]
                    )
                    return True
            return False  # No pending tasks found
        
        except Exception as e:
            logger.logger.error(f"Error loading pending task: {e}")
            return "failure"
        except KeyboardInterrupt:
            logger.logger.info("Keyboard interrupt received while loading pending task.")
            return "failure"
        finally: pass
    
    def run(self) -> int:
        """
        Run all pending tasks, and return the count of tasks run. 
        Return 'failure' on error.
        """
        count = 0
        while True:
            status = self.load_1st_pending_task()
            
            if status == "failure" or status is None:  # Error occurred
                return "failure"
            elif not status:  # No more pending tasks
                break
            
            else:
                count += 1
                self.task.run()
        return count

def timed_input(prompt: str, timeout: float) -> str:
    result = {"value": None}

    def _worker():
        result["value"] = input(prompt)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError
    return result["value"]

def confirm_input(prompt: str) -> str:
    try:
        return timed_input(prompt, timeout=10)
    except TimeoutError:
        print()
        return "n"
    except KeyboardInterrupt:
        print()
        return "n"

class Operator:
    def __init__(self, _tasker_id: str):
        files_dir = Path.home() / "opt" / ".tasker"
        files_dir.mkdir(parents=True, exist_ok=True)

        global tasker_id, logger, tasker_file, lock_file, run_file
        tasker_id = _tasker_id
        logger = Logger(name=str(files_dir / f"tasker_{tasker_id}"), 
                        level=log_level, width=80, start_from=9)
        tasker_file = files_dir / f"tasker.{tasker_id}.json"
        lock_file = files_dir / f".tasker.{tasker_id}.lock"
        run_file = files_dir / f".tasker.{tasker_id}.run"

        self.tasker = Tasker()
        self.tasks: dict = None
        self.n_tasks: int = None
        
    def run(self):
        """ Run all pending tasks. """
        if run_file.exists():
            logger.logger.error(f"Tasker {tasker_id} is running. Exiting.")
            return
        try:
            run_file.touch(exist_ok=False)  # Create a run file to indicate running state
            
            flag, start_wait, max_wait = False, None, 3 * (24*(60*60))
            while True:
                run_file.touch(exist_ok=True)  # Make sure the run file exists
                
                n_runs = self.tasker.run()
                try:
                    if n_runs == "failure":
                        logger.logger.critical("Error running tasks. Require manual intervention.")
                        break
                    
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
                                run_file.touch(exist_ok=True)  # Make sure the run file exists
                                time.sleep(10)  # Wait before checking again
                    else:
                        flag = False  # Reset flag if tasks were run
                
                except KeyboardInterrupt:
                    logger.logger.info("Keyboard interrupt received. Exiting.")
                    break
                except Exception as e:
                    logger.logger.error(f"Unexpected error when running: {e}")
                    break
        finally:
            if run_file.exists(): run_file.unlink()  # Remove run file when done
            else: logger.logger.warning("Run file not found after the run done.")
    
    def _load_tasks(self) -> bool:
        """ **Should be wrapped with lock** """
        self.tasks = load_tasks()
        if not isinstance(self.tasks, dict): 
            return False
        self.n_tasks = len(self.tasks)
        return True
    
    def check_valid_tasks(self) -> bool:
        """ Check if all task keys are ordered. Run after loading tasks. """
        if self.tasks is None:
            return False
        # Convert string keys to integers for comparison
        try:
            int_keys = [int(k) for k in self.tasks.keys()]
            return int_keys == list(range(1, len(self.tasks) + 1))
        except ValueError:
            return False
    
    @synchronized()
    def list(self, only_pending=True):
        """
        List all tasks.
        'only_pending' to show pending and running tasks.
        """
        logger.logger.info(f"[USER OPERATION] List all {'pending ' if only_pending else ''}tasks")
        logger.divider.word_line("list")
        try:
            if self._load_tasks():
                if not self.check_valid_tasks():
                    logger.logger.error(f"Task keys are not ordered. Please run `tasker.py {tasker_id} fix` to fix it.")
                    
                elif self.n_tasks == 0 or ( only_pending and 
                        sum(1 for task in self.tasks.values() if task['status'] in ['pending', 'running']) == 0 ):
                    logger.divider.write("No tasks in the queue.")
                else:
                    for task_id, task_info in self.tasks.items():
                        if only_pending and task_info['status'] not in ['pending', 'running']:
                            continue
                        logger.divider._write(f"{task_id:>5} | ---[ {task_info['status']} ]---\n"
                                              f"      | {task_info['cmd']}\n"
                                              f"      | {task_info['wd']}\n")
            else: logger.logger.error("Failed to load tasks. Please check the tasker file.")
        except Exception as e:
            logger.logger.error(f"Unexpected error when listing tasks: {e}")
        finally:
            logger.divider.word_line("list")
    
    def save(self) -> bool:
        """ **Should be wrapped with lock** """
        try:
            shutil.copy(str(tasker_file), str(tasker_file) + ".copy")
            with open(tasker_file, 'w') as f:
                json.dump(self.tasks, f, indent=4)
            return True
        except Exception as e:
            logger.logger.error(f"Error saving tasks: {e}")
            return False
        except KeyboardInterrupt:
            logger.logger.info(f"Keyboard interrupt received while saving tasks. "
                               "This may cause fatal error. Please check.")
    
    def check_load_tasks(self) -> bool:
        """
        Run before inserting or removing a task.
        Check if tasks are loaded and consistent.
        """
        if self.tasks is None or self.n_tasks is None:
            logger.logger.error("Tasks not loaded. Please load tasks first.")
            return False
        
        if len(self.tasks) != self.n_tasks:
            logger.logger.error("Tasks and n_tasks are not consistent. Please load again.")
            return False

        return True
    
    def check_position(self, pos: int, max_pos: int, min_pos: int=1) -> bool:
        if not isinstance(pos, int) or not isinstance(max_pos, int) or not isinstance(min_pos, int):
            return False
        
        if pos < min_pos or pos > max_pos:
            logger.logger.error(f"Invalid position {pos}.")
            return False
        return True
    
    def insert_task(self, pos: int, task_info: dict) -> bool:
        """
        **Should be wrapped with lock**.
        Insert a task at position POS.
        Task info should be a dictionary with keys 'wd', 'cmd', and 'status'.
        """
        if not self.check_load_tasks(): return False
        if not self.check_position(pos, self.n_tasks + 1):
            return False
        
        try:
            for i in reversed(range(pos+1, self.n_tasks+1 + 1)):
                self.tasks[str(i)] = self.tasks[str(i - 1)]
            self.tasks[str(pos)] = task_info
            self.n_tasks += 1
            return True
        except Exception as e:
            return False
    
    def remove_task(self, pos: int) -> dict:
        """
        **Should be wrapped with lock**.
        Remove the task at position POS and return its task info.
        Return 'False' on error.
        """
        if not self.check_load_tasks(): return False
        if not self.check_position(pos, self.n_tasks):
            return False
        
        try:
            removed_task = self.tasks.pop(str(pos))
            self.n_tasks -= 1
            # Shift tasks down
            for i in range(pos, self.n_tasks + 1):
                self.tasks[str(i)] = self.tasks.pop(str(i + 1))
            return removed_task
        except Exception as e:
            return False
    
    @synchronized()
    def append(self, command: str, work_dir: str=CWD_PATH):
        """ Append a new task to the queue. """
        logger.logger.info(f"[USER OPERATION] Appended task")
        logger.divider.word_line("append")
        try:
            work_dir = str(Path(work_dir).resolve())
            if self._load_tasks():
                if not self.check_valid_tasks():
                    logger.logger.error(f"Task keys are not ordered. Please run `tasker.py {tasker_id} fix` to fix it.")

                elif self.insert_task(self.n_tasks + 1, {
                        "wd": work_dir,
                        "cmd": command,
                        "status": "pending"
                    }):
                    if self.save():
                        logger.divider._write(f"Appended task {self.n_tasks}:\n"
                                              f"    Command: {command}\n"
                                              f"    Work Directory: {work_dir}\n")
                    else: logger.logger.error("Error saving tasks after appending.")
                else: logger.logger.error("Error appending task. Exiting.")
            else: logger.logger.error("Failed to load tasks. Please check the tasker file.")
        except Exception as e:
            logger.logger.error(f"Unexpected error when appending task: {e}")
        finally:
            logger.divider.word_line("append")
    
    @synchronized()
    def insert(self, pos: int, command: str, work_dir: str=CWD_PATH):
        """ Insert a new task before position POS. """
        logger.logger.info(f"[USER OPERATION] Insert task at position {pos}")
        logger.divider.word_line("insert")
        try:
            work_dir = str(Path(work_dir).resolve())
            if self._load_tasks():
                if pos == -1: pos = self.n_tasks
                if not self.check_valid_tasks():
                    logger.logger.error(f"Task keys are not ordered. Please run `tasker.py {tasker_id} fix` to fix it.")

                elif self.insert_task(pos, {
                        "wd": work_dir,
                        "cmd": command,
                        "status": "pending"
                    }):
                    if self.save():
                        logger.divider._write(f"Inserted task at position {pos}:\n"
                                              f"    Command: {command}\n"
                                              f"    Work Directory: {work_dir}\n")
                    else: logger.logger.error("Error saving tasks after inserting.")
                else: logger.logger.error("Error inserting task. Exiting.")
            else: logger.logger.error("Failed to load tasks. Please check the tasker file.")
        except Exception as e:
            logger.logger.error(f"Unexpected error when inserting task: {e}")
        finally:
            logger.divider.word_line("insert")

    @synchronized()
    def remove(self, pos: int):
        """ Remove the task at position POS. """
        logger.logger.info(f"[USER OPERATION] Remove task at position {pos}")
        logger.divider.word_line("remove")
        try:
            if self._load_tasks():
                if pos == -1: pos = self.n_tasks
                if not self.check_valid_tasks():
                    logger.logger.error(f"Task keys are not ordered. Please run `tasker.py {tasker_id} fix` to fix it.")
                
                elif not self.check_position(pos, self.n_tasks): pass  # Invalid position
                elif not run_file.exists() or self.tasks[str(pos)]['status'] != "running":
                    confirm = confirm_input(f"Task status at position {pos} is {self.tasks[str(pos)]['status']}. \n"
                                            "Are you sure you want to remove it? (y/n): ").strip().lower()
                    if confirm == 'y':
                        removed_task = self.remove_task(pos)
                        if removed_task:
                            if self.save():
                                logger.divider._write(f"Removed task at position {pos}:\n"
                                                      f"    Command: {removed_task['cmd']}\n"
                                                      f"    Work Directory: {removed_task['wd']}\n"
                                                      f"    Status: {removed_task['status']}\n")
                            else: logger.logger.error("Error saving tasks after removing.")
                        else: logger.logger.error("Error removing task. Exiting.")
                    else:logger.logger.info(f"Canceled removing task at position {pos}.")
                else: logger.logger.error("Cannot removing running task.")
            else: logger.logger.error("Failed to load tasks. Please check the tasker file.")
        except Exception as e:
            logger.logger.error(f"Unexpected error when removing task: {e}")
        finally:
            logger.divider.word_line("remove")

    @synchronized()
    def move(self, pos: int, target_pos: int):
        """
        Move the task at position POS to TARGET_POS.
        """
        logger.logger.info(f"[USER OPERATION] Move task from position {pos} to {target_pos}")
        logger.divider.word_line("move")
        try:
            if self._load_tasks():
                if pos == -1: pos = self.n_tasks
                if target_pos == -1: target_pos = self.n_tasks
                if not self.check_valid_tasks():
                    logger.logger.error(f"Task keys are not ordered. Please run `tasker.py {tasker_id} fix` to fix it.")
                
                elif pos == target_pos:
                    logger.logger.info(f"Task already at position {pos}.")
                elif not self.check_position(pos, self.n_tasks): pass  # Invalid position
                elif self.insert_task(target_pos + 1 if pos < target_pos else target_pos, self.tasks[str(pos)]) and (
                        self.remove_task(pos if pos < target_pos else pos + 1) ):
                    if self.save():
                        logger.divider._write(f"Moved task from position {pos} to {target_pos}:\n"
                                              f"    Command: {self.tasks[str(target_pos)]['cmd']}\n"
                                              f"    Work Directory: {self.tasks[str(target_pos)]['wd']}\n")
                    else: logger.logger.error("Error saving tasks after moving.")
                else: logger.logger.error("Error moving task. Exiting.")
            else: logger.logger.error("Failed to load tasks. Please check the tasker file.")
        except Exception as e:
            logger.logger.error(f"Unexpected error when moving task: {e}")
        finally:
            logger.divider.word_line("move")

    @synchronized()
    def rerun(self, pos: int):
        """ Rerun the task at position POS. """
        logger.logger.info(f"[USER OPERATION] Rerun task at position {pos}")
        logger.divider.word_line("rerun")
        try:
            if self._load_tasks():
                if pos == -1: pos = self.n_tasks
                if not self.check_valid_tasks():
                    logger.logger.error(f"Task keys are not ordered. Please run `tasker.py {tasker_id} fix` to fix it.")

                elif self.check_position(pos, self.n_tasks): 
                    self.tasks[str(pos)]["status"] = "pending"
                    if self.save():
                        logger.divider._write(f"Rerun task at position {pos}:\n"
                                              f"    Command: {self.tasks[str(pos)]['cmd']}\n"
                                              f"    Work Directory: {self.tasks[str(pos)]['wd']}\n")
                    else: logger.logger.error("Error saving tasks after rerun.")
                # else: logger.logger.error(f"Invalid position {pos} for rerun.")
            else: logger.logger.error("Failed to load tasks. Please check the tasker file.")
        except Exception as e:
            logger.logger.error(f"Unexpected error when rerunning task: {e}")
        finally:
            logger.divider.word_line("rerun")

    @synchronized()
    def swap(self, pos1: int, pos2: int):
        logger.logger.info(f"[USER OPERATION] Swap tasks at position {pos1} and {pos2}")
        logger.divider.word_line("swap")
        try:
            if self._load_tasks():
                if pos1 == -1: pos1 = self.n_tasks
                if pos2 == -1: pos2 = self.n_tasks
                if not self.check_valid_tasks():
                    logger.logger.error(f"Task keys are not ordered. Please run `tasker.py {tasker_id} fix` to fix it.")
                
                elif pos1 == pos2 or not self.check_position(pos1, self.n_tasks) or not self.check_position(pos2, self.n_tasks):
                    logger.logger.error(f"Invalid position {pos1} and {pos2} for swap.")
                else:
                    self.tasks[str(pos1)], self.tasks[str(pos2)] = self.tasks[str(pos2)], self.tasks[str(pos1)]
                    if self.save():
                        logger.divider._write(f"Swapped tasks at positions {pos1} and {pos2}, now:\n"
                                              f"{pos1:>5}: {self.tasks[str(pos1)]['cmd']}\n"
                                              f"{pos2:>5}: {self.tasks[str(pos2)]['cmd']}\n")
                    else: logger.logger.error("Error saving tasks after swap.")
            else: logger.logger.error("Failed to load tasks. Please check the tasker file.")
        except Exception as e:
            logger.logger.error(f"Unexpected error when swapping tasks: {e}")
        finally:
            logger.divider.word_line("swap")
    
    @synchronized()
    def clear(self, status: List[str]=["completed"]):
        """
        Clear tasks with the given status.
        Status can be 'pending', 'completed', or 'failed'.
        """
        logger.logger.info(f"[USER OPERATION] Clear tasks with status: {','.join(status)}")
        logger.divider.word_line("clear")
        try:
            if not all(s in ["pending", "completed", "failed"] for s in status):
                logger.logger.error(f"Invalid status {status}. Must be one of 'pending', 'completed', or 'failed'.")

            elif self._load_tasks():
                if not self.check_valid_tasks():
                    logger.logger.error(f"Task keys are not ordered. Please run `tasker.py {tasker_id} fix` to fix it.")
                
                else:
                    tasks_to_remove = [k for k, v in self.tasks.items() if v['status'] in status]
                    if not tasks_to_remove:
                        logger.logger.info("No tasks with specific status found.")
                    else:
                        print(f"{len(tasks_to_remove)} tasks:")
                        for task_id in tasks_to_remove: print(f"  `{self.tasks[task_id]['cmd']}`")
                        confirm = confirm_input("will be removed. Are you sure you want to remove them? (y/n): ").strip().lower()
                        if confirm == 'y':
                            cleared = []
                            for task_id in reversed(tasks_to_remove):
                                if self.remove_task(int(task_id)):
                                    cleared.append(task_id)
                                else: logger.logger.error(f"Error removing task {task_id}.")
                            if self.save():
                                logger.divider.write(f"Cleared {len(cleared)} tasks: " 
                                                     f"{', '.join(reversed(cleared))}.")
                            else: logger.logger.error("Error saving tasks after clearing.")
                        else: logger.logger.info(f"Canceled clearing tasks with specific status.")
            else: logger.logger.error("Failed to load tasks. Please check the tasker file.")
        except Exception as e:
            logger.logger.error(f"Unexpected error when clearing tasks: {e}")
        finally:
            logger.divider.word_line("clear")
    
    @synchronized()
    def fix(self):
        """
        Fix the task keys to be ordered.
        This should be run if the task keys are not ordered.
        """
        logger.logger.info("[USER OPERATION] Fix task keys")
        logger.divider.word_line("fix")
        try:
            if self._load_tasks():
                if self.check_valid_tasks():
                    logger.logger.info("Task keys are already ordered.")
                else:
                    task_ids = sorted(self.tasks.keys(), key=int)
                    if len(task_ids) == self.n_tasks:
                        new_tasks = {str(i): self.tasks[task_ids[i - 1]] 
                                    for i in range(1, self.n_tasks + 1)}
                        self.tasks = new_tasks
                        if self.save():
                            logger.divider.write(f"Fixed task keys. Now they are ordered: "
                                                 f"{', '.join(self.tasks.keys())}.")
                        else: logger.logger.error("Error saving tasks after fixing.")
                    else: logger.logger.error("Failed to fix task keys. Number of task IDs and tasks not match.")
            else: logger.logger.error("Failed to load tasks. Please check the tasker file.")
        except Exception as e:
            logger.logger.error(f"Unexpected error when fixing task keys: {e}")
        finally:
            logger.divider.word_line("fix")

def main(args):
    operator = Operator(args.tasker_id)

    if args.mode == "run":
        operator.run()
    
    elif args.mode == "ls":
        operator.list(only_pending=True)
    
    elif args.mode == "la":
        operator.list(only_pending=False)
    
    elif args.mode == "add":
        command = input("Command: ").strip()
        work_dir = input(f"Work directory: (default '.') ").strip() or CWD_PATH
        operator.append(command, work_dir)
    
    elif args.mode == "in":
        position = int(input("Position to insert at: "))
        command = input("Command: ").strip()
        work_dir = input(f"Work directory: (default '.') ").strip() or CWD_PATH
        operator.insert(position, command, work_dir)
    
    elif args.mode == "rm":
        position = int(input("Position to remove: "))
        operator.remove(position)

    elif args.mode == "mv":
        pos = int(input("Position to move: "))
        target_pos = int(input("Target position: "))
        operator.move(pos, target_pos)
    
    elif args.mode == "rerun":
        position = int(input("Position to rerun: "))
        operator.rerun(position)

    elif args.mode == "swap":
        pos1 = int(input("Position 1 to swap: "))
        pos2 = int(input("Position 2 to swap: "))
        operator.swap(pos1, pos2)
    
    elif args.mode == "clr":
        status_str = input("Status to clear: p - pending, \n"
                           "                 c - completed, \n"
                           "                 f - failed. \n"
                           "(default is c): ").strip().lower() or "c"
        status = []
        if "p" in status_str: status.append("pending")
        if "c" in status_str: status.append("completed")
        if "f" in status_str: status.append("failed")
        operator.clear(status)
    
    elif args.mode == "fix":
        operator.fix()
    
    elif args.mode == "help":
        print("Available modes:")
        print("  run       - Run all pending tasks")
        print("  ls        - List all pending tasks")
        print("  la        - List all tasks")
        print("  add       - Add a new task")
        print("  in        - Insert a task at a specific position")
        print("  rm        - Remove a task at a specific position")
        print("  mv        - Move a task to a different position")
        print("  rerun     - Rerun a task at a specific position")
        print("  swap      - Swap two tasks at specified positions")
        print("  clr       - Clear tasks with specific status")
        print("  fix       - Fix task keys to be ordered")
    
    else:
        logger.logger.error(f"Unknown mode: {args.mode}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simple queue-based task runner.")
    parser.add_argument("tasker_id", type=str, help="Tasker ID to identify the task queue")
    parser.add_argument("mode", type=str, help="Execute mode")
    args = parser.parse_args()
    main(args)