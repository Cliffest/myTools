"""
以 source 目录为参考, 增量同步到 sync 目录下, 并记录每次同步的更改信息到 source/synclog.txt 和 sync/synclog.txt
可识别 source/.syncignore 下的忽略规则 (完整路径)
支持并行处理以提高同步性能
"""
import argparse
import fnmatch
import hashlib
import logging
import os
import re
import stat
import shutil
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import List, Optional, Tuple, Dict
from queue import Queue
import multiprocessing


class FileComparer:
    """文件比较类，提供不同比较策略"""
    
    @staticmethod
    def compare_by_date(file1: str, file2: str, time_factor: int) -> bool:
        """基于修改日期比较文件"""
        micro_error = 2  # 允许误差: micro_error/time_factor (s)
        return time_factor * abs(os.path.getmtime(file1) - os.path.getmtime(file2)) <= micro_error
    
    @staticmethod
    def compare_by_hash(file1: str, file2: str, chunk_size: int = 8192) -> bool:
        """基于文件内容哈希比较文件"""
        if os.path.getsize(file1) != os.path.getsize(file2):
            return False  # 文件大小不同，快速返回
            
        hash1 = FileComparer._calculate_hash(file1, chunk_size)
        hash2 = FileComparer._calculate_hash(file2, chunk_size)
        return hash1 == hash2
        
    @staticmethod
    def _calculate_hash(file_path: str, chunk_size: int = 8192) -> str:
        """计算文件哈希值，使用分块读取提高大文件性能"""
        hash_md5 = hashlib.md5()
        try:
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(chunk_size), b""):
                    hash_md5.update(chunk)
            return hash_md5.hexdigest()
        except IOError as e:
            print(f"警告: 计算文件哈希失败 {file_path}: {e}")
            return ""


class ThreadSafeLogger:
    """线程安全的日志记录类"""
    
    def __init__(self, log_file: str, copy_path=None):
        self.log_file = log_file
        self.copy_file = os.path.join(copy_path, os.path.basename(log_file)) if copy_path is not None else None
        self.logs = []  # 缓存日志
        self._lock = threading.Lock()  # 线程锁
        
        # 确保日志目录存在
        log_dir = os.path.dirname(log_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir)
            
        # 设置日志格式
        logging.basicConfig(
            level=logging.INFO,
            format='%(message)s'
        )
        
    def log(self, action: str, path: str):
        """记录并输出一条日志（线程安全）"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        path = path.replace("\\", "/")
        log_entry = f"[{timestamp}] {action} {path}"
        
        with self._lock:
            self.logs.append(log_entry)
            logging.info(log_entry)
        
    def log_summary(self, summary: str):
        """记录摘要信息（线程安全）"""
        with self._lock:
            self.logs.append(summary)
            logging.info(summary)
        
    def save(self):
        """保存所有缓存的日志到文件（线程安全）"""
        with self._lock:
            try:
                # 写入新日志
                with open(self.log_file, "a", encoding="utf-8") as f:
                    for log in self.logs:
                        f.write(f"{log}\n")
                
                if self.copy_file is not None:
                    shutil.copy2(self.log_file, self.copy_file)
                        
                self.logs = []  # 清空缓存
            except IOError as e:
                print(f"错误: 无法写入日志文件: {e}")


class ThreadSafeCounter:
    """线程安全的计数器"""
    
    def __init__(self):
        self.added = 0
        self.modified = 0
        self.failed = 0
        self.deleted_files = 0
        self.deleted_dirs = 0
        self._lock = threading.Lock()
    
    def increment(self, counter_type: str, count: int = 1):
        """增加计数器值"""
        with self._lock:
            if counter_type == "added":
                self.added += count
            elif counter_type == "modified":
                self.modified += count
            elif counter_type == "failed":
                self.failed += count
            elif counter_type == "deleted_files":
                self.deleted_files += count
            elif counter_type == "deleted_dirs":
                self.deleted_dirs += count
    
    def get_counts(self) -> Dict[str, int]:
        """获取所有计数器的值"""
        with self._lock:
            return {
                "added": self.added,
                "modified": self.modified,
                "failed": self.failed,
                "deleted_files": self.deleted_files,
                "deleted_dirs": self.deleted_dirs
            }


class NoTracebackError(Exception):
    def __init__(self, message):
        self.message = message
    def __str__(self):
        return self.message


class Source:
    def __init__(self, source_root_path: str):
        """
        初始化 Source 目录信息
        :param source_root_path: 源目录路径
        """
        self.source_root_path = os.path.abspath(source_root_path)  # 使用绝对路径
        self.syncignore_path = os.path.join(self.source_root_path, ".syncignore")
        
        self.syncignore_mtime = 0
        self.ignore_rules = []
        self.cache_ignore = {}  # key: path, value: True/False
        self._cache_lock = threading.Lock()  # 缓存锁

    def reset_ignore(self):
        with self._cache_lock:
            self.syncignore_mtime = os.path.getmtime(self.syncignore_path) if os.path.exists(self.syncignore_path) else 0
            self.ignore_rules = self.get_ignore_rules()
            self.cache_ignore = {}

    def get_ignore_rules(self) -> List[str]:
        """
        读取 syncignore 文件并返回忽视规则列表
        :return: List[str] 规则列表
        """
        if not os.path.exists(self.syncignore_path):
            return []
        try:
            with open(self.syncignore_path, "r", encoding="utf-8") as f:
                rules = []
                for line in f:
                    if line.strip() and not line.strip().startswith("#"):
                        if line.strip().startswith("**/"):
                            rules.append(line.strip()[1:])
                            rules.append(line.strip()[3:])
                            if line.strip().endswith("/"):
                                rules.append(line.strip()[1:] + "*")
                                rules.append(line.strip()[3:] + "*")
                        elif line.strip().endswith("/"):
                            rules.append(line.strip())
                            rules.append(line.strip() + "*")
                        else:
                            rules.append(line.strip())
                return rules
                
        except IOError as e:
            print(f"警告: 无法读取忽略规则文件: {e}")
            return []
    
    def is_satisfy_rule(self, path: str, rule: str, root_path: str) -> bool:
        """
        检查 path 是否匹配 rule 的表达式
        :param path: 文件/文件夹路径
        :param rule: 一条表达式规则
        :param root_path: source/sync 根目录路径，用于判断一个路径是否是文件夹
        :return: True - 满足规则, False - 不满足规则
        """
        if os.path.isdir(os.path.join(root_path, path)):
            path = path + "/"

        path = path.replace("\\", "/")  # 兼容Windows路径
        rule = rule.replace("\\", "/")
        root_path = root_path.replace("\\", "/")

        # 处理根目录下的文件 (.DS_Store 等)
        if path == rule:
            return True
        
        # 检查完整路径匹配
        if fnmatch.fnmatch(path, rule):
            return True
            
        return False

    def is_ignore(self, path: str, root_path: str) -> bool:
        """
        检查 path 是否匹配忽视规则（线程安全）
        :param path: 文件/文件夹路径
        :param root_path: source/sync 根目录路径
        :return: True - 忽视, False - 不忽视
        """
        assert os.path.exists(root_path), f"错误: {root_path} 路径不存在"
        assert path.startswith(root_path), f"错误: {root_path} 不是 {path} 的根目录"

        if os.path.samefile(path, root_path) if os.path.exists(path) else False:
            return False  # 不忽略根目录
        
        relative_path = os.path.relpath(path, root_path)
        
        with self._cache_lock:
            if relative_path in self.cache_ignore:
                return self.cache_ignore[relative_path]
            
            result = any(self.is_satisfy_rule(relative_path, rule, root_path) for rule in self.ignore_rules)
            self.cache_ignore[relative_path] = result
            return result


class ParallelSync(Source):
    def __init__(self, source_root_path: str, sync_root_path: str, mode: str, interval: int, delete: bool, time_factor: int, max_workers: int = None):
        """
        初始化 ParallelSync 目录信息
        :param source_root_path: 源目录路径
        :param sync_root_path: 目标同步目录路径
        :param mode: 同步模式 ('date', 'file', 'reset')
        :param interval: 同步间隔时间（秒）
        :param max_workers: 最大工作线程数，None表示自动检测
        """
        super().__init__(source_root_path)
        self.sync_root_path = os.path.abspath(sync_root_path)  # 使用绝对路径
        self.mode = mode  # str ('date', 'file', 'reset')
        self.interval = interval  # int
        self.delete = delete  # True/False
        self.time_factor = time_factor  # int
        
        # 自动检测最佳线程数
        if max_workers is None:
            # 基于CPU核心数和I/O密集型任务特点
            cpu_count = multiprocessing.cpu_count()
            self.max_workers = min(max(cpu_count * 2, 4), 32)  # 2-32个线程
        else:
            self.max_workers = max_workers
        
        self.log_file = os.path.join(self.sync_root_path, "synclog.txt")
        self.logger = ThreadSafeLogger(self.log_file, copy_path=self.source_root_path)
        self.file_comparer = FileComparer()
        self.counter = ThreadSafeCounter()

    def confirm_sync(self) -> bool:
        """
        输出同步信息，并要求用户确认
        若输入不为Enter则终止同步的运行
        """
        print(f"\n{'='*60}")
        print(f"同步信息:")
        print(f"  源 目 录: {self.source_root_path}")
        print(f"  目标目录: {self.sync_root_path}")
        print(f"  同步模式: {self.mode}")
        print(f"  并行线程: {self.max_workers}")
        print(f"  同步间隔: {self.interval}秒" if self.interval > 0 else "  执行一次后退出")
        print(f" *忽略模式: {'删除' if self.delete else '忽略'}")
        print(f"{'='*60}")
        
        user_input = input("按回车键继续，输入任意内容退出: ")
        return user_input == ""

    def compare_files(self, src_file: str, dst_file: str) -> bool:
        """
        比较两个文件是否一致（基于模式选择）
        :param src_file: 源文件路径
        :param dst_file: 目标文件路径
        :return: True - 文件相同, False - 文件不同
        """
        if not os.path.exists(dst_file):
            return False

        try:
            if self.mode == "date":
                is_git_objects_file = (".git/objects/" in src_file.replace("\\", "/")) and (".git/objects/" in dst_file.replace("\\", "/"))
                is_same_file = self.file_comparer.compare_by_date(src_file, dst_file, time_factor=self.time_factor)
                # 若是 .git/objects/ 下的文件, 并且日期判断为不同文件后, 用文件内容进行二次判断 (规避权限问题)
                return is_same_file if not is_git_objects_file or is_same_file else self.file_comparer.compare_by_hash(src_file, dst_file)
            elif self.mode == "file":
                return self.file_comparer.compare_by_hash(src_file, dst_file)
            return False
        except Exception as e:
            print(f"警告: 比较文件失败 {src_file} 和 {dst_file}: {e}")
            return False

    def rm_git_objects_file(self, file_path):  # 绝对路径
        """
        安全删除 Git 对象文件。
        返回: True (成功) / False (失败)
        """
        if not os.path.isfile(file_path):
            self.logger.log("ERROR", f"{file_path} 不是单个文件")
            return False

        # 校验路径安全性
        if not ".git/objects/" in file_path.replace("\\", "/"):
            self.logger.log("ERROR", f"非法路径: {file_path} 必须位于 .git/objects/ 下")
            return False

        try:
            # 尝试 os.remove
            try:
                os.chmod(file_path, stat.S_IWRITE)
                os.remove(file_path)
                return True
            except Exception as e:
                self.logger.log("Warning", f"os.remove({file_path}) 失败: {e}, 尝试目录级删除...")

            # 谨慎使用 rmtree
            dir_path = os.path.dirname(file_path)
            if os.path.exists(file_path) and len(os.listdir(dir_path)) == 1:
                shutil.rmtree(dir_path)
                os.makedirs(dir_path)  # 只删除文件, 恢复上级目录
                return True
            else:
                self.logger.log("ERROR", f"无法删除: {file_path} 所在目录文件数量不为 1")
                return False
        except Exception as e:
            self.logger.log("ERROR", f"删除 {file_path} 失败: {e}")
            return False

    def sync_file_task(self, src_file: str, dst_file: str) -> Tuple[bool, str]:
        """
        同步单个文件的任务函数（用于线程池）
        :return: (是否成功同步, 同步类型 - "added"或"modified"或"unchanged")
        """
        try:
            if not os.path.exists(dst_file):
                # 确保目标文件所在目录存在 (线程安全)
                dst_dir = os.path.dirname(dst_file)
                if not os.path.exists(dst_dir):
                    try:
                        os.makedirs(dst_dir, exist_ok=True)
                    except FileExistsError:
                        pass  # 目录已存在，忽略错误
                    
                shutil.copy2(src_file, dst_file)
                self.logger.log("A", os.path.relpath(dst_file, self.sync_root_path))
                self.counter.increment("added")
                return True, "added"
            elif not self.compare_files(src_file, dst_file):
                # 处理 .git/objects 下的文件, 先删除再复制
                if ".git/objects/" in dst_file.replace("\\", "/"):
                    if not self.rm_git_objects_file(dst_file):
                        self.counter.increment("failed")
                        return False, "error"

                shutil.copy2(src_file, dst_file)
                self.logger.log("M", os.path.relpath(dst_file, self.sync_root_path))
                self.counter.increment("modified")
                return True, "modified"
            return True, "unchanged"
        except Exception as e:
            self.logger.log("ERROR", f"同步文件失败: {os.path.relpath(dst_file, self.sync_root_path)} - {str(e)}")
            self.counter.increment("failed")
            return False, "error"

    def collect_sync_tasks(self, src_dir: str, dst_dir: str) -> List[Tuple[str, str]]:
        """
        收集所有需要同步的文件任务
        :return: [(src_file, dst_file), ...]
        """
        tasks = []
        
        try:
            for root, dirs, files in os.walk(src_dir):
                # 过滤忽略的目录
                dirs[:] = [d for d in dirs if not self.is_ignore(os.path.join(root, d), self.source_root_path)]
                
                for file in files:
                    src_file = os.path.join(root, file)
                    if self.is_ignore(src_file, self.source_root_path):
                        continue
                    
                    relative_path = os.path.relpath(src_file, src_dir)
                    dst_file = os.path.join(dst_dir, relative_path)
                    tasks.append((src_file, dst_file))
                    
        except Exception as e:
            self.logger.log("ERROR", f"收集同步任务失败: {str(e)}")
        
        return tasks

    def sync_directory_parallel(self, src_dir: str, dst_dir: str):
        """
        并行同步目录，包括子目录
        """
        # 确保目标目录存在
        if not os.path.exists(dst_dir):
            try:
                os.makedirs(dst_dir, exist_ok=True)
            except Exception as e:
                self.logger.log("ERROR", f"无法创建目录: {os.path.relpath(dst_dir, self.sync_root_path)} - {str(e)}")
                self.counter.increment("failed")
                return

        # 收集所有文件同步任务
        print("收集同步任务...")
        tasks = self.collect_sync_tasks(src_dir, dst_dir)
        
        if not tasks:
            print("没有文件需要同步")
            return
        
        print(f"开始并行同步 {len(tasks)} 个文件，使用 {self.max_workers} 个线程...")
        
        # 使用线程池并行处理文件同步
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # 提交所有任务
            future_to_task = {executor.submit(self.sync_file_task, src_file, dst_file): (src_file, dst_file) 
                             for src_file, dst_file in tasks}
            
            # 处理完成的任务
            completed = 0
            for future in as_completed(future_to_task):
                completed += 1
                if completed % 100 == 0 or completed == len(tasks):
                    print(f"进度: {completed}/{len(tasks)} ({completed/len(tasks)*100:.1f}%)")
                
                try:
                    future.result()  # 获取结果，如果有异常会在这里抛出
                except Exception as e:
                    src_file, dst_file = future_to_task[future]
                    self.logger.log("ERROR", f"同步任务异常: {os.path.relpath(src_file, self.source_root_path)} - {str(e)}")
                    self.counter.increment("failed")

    def collect_delete_tasks(self) -> List[Tuple[str, str, bool]]:
        """
        收集所有需要删除的文件/目录任务
        :return: [(sync_path, relative_path, is_file), ...]
        """
        tasks = []
        special_files = [os.path.basename(self.log_file)]
        
        for root, dirs, files in os.walk(self.sync_root_path, topdown=False):
            for name in files:
                sync_path = os.path.join(root, name)
                relative_path = os.path.relpath(sync_path, self.sync_root_path)
                source_path = os.path.join(self.source_root_path, relative_path)
                
                if relative_path in special_files:
                    continue  # 跳过日志文件
                    
                if self.is_redundant(source_path, sync_path):
                    tasks.append((sync_path, relative_path, True))  # True表示是文件

            for name in dirs:
                sync_path = os.path.join(root, name)
                relative_path = os.path.relpath(sync_path, self.sync_root_path)
                source_path = os.path.join(self.source_root_path, relative_path)

                if self.is_redundant(source_path, sync_path):
                    tasks.append((sync_path, relative_path, False))  # False表示是目录
                    
        return tasks

    def delete_task(self, sync_path: str, relative_path: str, is_file: bool) -> bool:
        """
        删除单个文件或目录的任务函数
        :return: 是否成功删除
        """
        try:
            if is_file:
                if ".git/objects/" in sync_path.replace("\\", "/"):
                    if not self.rm_git_objects_file(sync_path):
                        raise NoTracebackError(f"Remove {sync_path} failed")
                else: 
                    os.remove(sync_path)
                self.logger.log("D", relative_path)
                self.counter.increment("deleted_files")
            else:
                # 确保目录为空
                if not os.listdir(sync_path):
                    os.rmdir(sync_path)
                    self.logger.log("D", f"{relative_path}/")
                    self.counter.increment("deleted_dirs")
                else:
                    self.logger.log("ERROR", f"删除目录失败: {relative_path}/ - 目录不为空")
                    self.counter.increment("failed")
                    return False
            return True
        except Exception as e:
            self.logger.log("ERROR", f"删除失败: {relative_path} - {str(e)}")
            self.counter.increment("failed")
            return False

    def remove_extra_files_parallel(self):
        """
        并行删除 sync 目录中多余的文件/目录
        """
        print("收集删除任务...")
        tasks = self.collect_delete_tasks()
        
        if not tasks:
            print("没有多余文件需要删除")
            return
        
        print(f"开始并行删除 {len(tasks)} 个文件/目录...")
        
        # 分离文件和目录任务，先删除文件再删除目录
        file_tasks = [(path, rel_path, is_file) for path, rel_path, is_file in tasks if is_file]
        dir_tasks = [(path, rel_path, is_file) for path, rel_path, is_file in tasks if not is_file]
        
        # 并行删除文件
        if file_tasks:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = [executor.submit(self.delete_task, sync_path, relative_path, is_file) 
                          for sync_path, relative_path, is_file in file_tasks]
                
                completed = 0
                for future in as_completed(futures):
                    completed += 1
                    if completed % 50 == 0 or completed == len(file_tasks):
                        print(f"删除文件进度: {completed}/{len(file_tasks)}")
        
        # 串行删除目录（避免父子目录冲突）
        if dir_tasks:
            print(f"删除 {len(dir_tasks)} 个目录...")
            for sync_path, relative_path, is_file in dir_tasks:
                self.delete_task(sync_path, relative_path, is_file)

    def _is_redundant(self, source_path, sync_path):  # 绝对路径
        """
        判断 sync_path 是否属于 多余文件/目录
        即: 不在 source 中且不匹配 syncignore 规则，或启用了 delete 并匹配 syncignore 规则
        匹配 syncignore 规则: 判断文件/目录的相对路径
        """ 
        ignore = self.is_ignore(sync_path, self.sync_root_path)  # bool
        return (not os.path.exists(source_path) and not ignore) or (self.delete and ignore)

    def is_redundant(self, source_path, sync_path):  # 绝对路径
        """
        判断 sync_path 是否属于 多余文件/目录, 并对匹配忽视规则的文件/目录进行再判断: 
        若上级目录(非根目录)中存在多余目录, 则判定为 多余文件/目录
        """
        # 如果文件已经不存在则不进行二次删除
        if not os.path.exists(sync_path):
            return False

        ignore = self.is_ignore(sync_path, self.sync_root_path)
        redundant = self._is_redundant(source_path, sync_path)
        
        while (not self.delete and ignore) and not redundant:
            source_path = os.path.dirname(source_path)
            sync_path = os.path.dirname(sync_path)
            if sync_path == self.sync_root_path:
                break
            redundant = self._is_redundant(source_path, sync_path)
        
        return redundant

    def reset_sync(self) -> bool:
        """
        直接清空 sync 目录并复制 source 目录
        :return: 是否成功重置
        """
        # 保存旧日志
        old_logs = []
        if os.path.exists(self.log_file):
            try:
                with open(self.log_file, "r", encoding="utf-8") as f:
                    old_logs = f.readlines()
            except Exception as e:
                self.logger.log("WARNING", f"无法读取旧日志: {str(e)}")
        
        self.logger.log("RESET", self.sync_root_path)
        
        try:
            # 如果目标目录已存在，先删除
            if os.path.exists(self.sync_root_path):
                shutil.rmtree(self.sync_root_path)
                
            # 复制源目录到目标目录，忽略符合规则的文件
            def ignore_func(src, names):
                return [name for name in names if self.is_ignore(os.path.join(src, name), self.source_root_path)]
                
            shutil.copytree(self.source_root_path, self.sync_root_path, ignore=ignore_func)
            
            # 恢复旧日志
            if old_logs:
                try:
                    with open(self.log_file, "w", encoding="utf-8") as f:
                        f.writelines(old_logs)
                except Exception as e:
                    self.logger.log("WARNING", f"无法恢复旧日志: {str(e)}")
                    
            return True
        except Exception as e:
            self.logger.log("ERROR", f"重置同步失败: {str(e)}")
            return False

    def run_sync(self):
        """
        运行同步流程
        """
        synchronization = True
        if not self.confirm_sync():
            print("同步操作被用户终止。")
            synchronization = False
    
        sync_count = 0
        
        while synchronization:
            sync_time_start = time.time()
            sync_count += 1
            
            # 重置计数器
            self.counter = ThreadSafeCounter()
            
            self.logger.log_summary(f"\n{'='*60}")
            sync_start_message = f"开始第 {sync_count} 次同步 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"
            self.logger.log_summary(sync_start_message)
            source_sync_message = f"源目录 -> 目标目录: {self.source_root_path} -> {self.sync_root_path}\n"
            self.logger.log_summary(source_sync_message)

            if not os.path.exists(self.syncignore_path) or os.path.getmtime(self.syncignore_path) > self.syncignore_mtime:
                # 如果上次和这次都不存在 syncignore 文件, 跳过载入忽略规则
                if not os.path.exists(self.syncignore_path) and self.syncignore_mtime == 0:
                    pass
                else:
                    self.reset_ignore()
                    self.logger.log_summary(f"{'重新' if sync_count > 1 else ''}载入忽略规则完成")
                
            try:
                if self.mode == "reset":
                    # 重置模式不需要再调用remove_extra_files
                    print("执行重置模式，清空目标目录并重新复制...")
                    if self.reset_sync():
                        self.logger.log_summary("重置同步完成")
                    else:
                        self.logger.log_summary("重置同步失败")
                else:
                    # 普通增量同步 - 使用并行处理
                    print("执行增量同步...")
                    self.sync_directory_parallel(self.source_root_path, self.sync_root_path)
                    self.remove_extra_files_parallel()
                    
                    # 获取计数器结果
                    counts = self.counter.get_counts()
                    summary = (f"同步完成: 新增 {counts['added']} 个文件, 更新 {counts['modified']} 个文件, " 
                               f"删除 {counts['deleted_files']} 个文件和 {counts['deleted_dirs']} 个目录, 失败 {counts['failed']} 项")
                    self.logger.log_summary(summary)
                    
                # 保存日志
                self.logger.save()
                
                # 计算耗时
                sync_time = time.time() - sync_time_start
                time_summary = f"同步耗时: {sync_time:.2f} 秒"
                self.logger.log_summary(time_summary)
                self.logger.save()

                # 记录同步任务
                self.logger.log_summary(f"同步任务 {self.source_root_path} -> {self.sync_root_path} 完成")
                self.logger.save()
                
                # 如果间隔为0，则只执行一次
                if self.interval <= 0:
                    end_message = "已完成单次同步，程序退出"
                    self.logger.log_summary(end_message)
                    self.logger.save()
                    break
                    
                # 显示等待信息
                next_sync_time = datetime.fromtimestamp(time.time() + self.interval)
                wait_message = f"\n下次同步将在 {next_sync_time.strftime('%Y-%m-%d %H:%M:%S')} 开始，等待中..."
                self.logger.log_summary(wait_message)
                self.logger.save()
                time.sleep(self.interval)
                
            except KeyboardInterrupt:
                interrupt_message = "\n同步被用户中断。"
                self.logger.log_summary(interrupt_message)
                self.logger.save()
                break
            except Exception as e:
                error_message = f"同步过程中发生错误: {e}"
                self.logger.log_summary(error_message)
                # 错误后也保存日志
                self.logger.save()
                if self.interval <= 0:
                    break
                time.sleep(self.interval)


def main():
    parser = argparse.ArgumentParser(description="并行增量同步工具")
    parser.add_argument("source", type=str, help="源目录路径")
    parser.add_argument("sync", type=str, help="同步目标目录路径")
    
    parser.add_argument("-m", "--mode", choices=["date", "file", "reset"], required=True, help="同步模式, date: 按最新修改日期增量同步, file: 比对文件内容增量同步, reset: 删除后复制源文件过去")
    parser.add_argument("-f", "--time_factor", type=int, default=1e6, help="处理不同设备的时间精度, 应 =1/精度. 操作系统间可设 1e6; U盘基于文件系统, 例如 exFAT 应设 1")
    
    parser.add_argument("-i", "--interval", type=int, default=0, help="同步间隔时间(s), 0 表示仅执行一次; 默认为 0")
    parser.add_argument("-D", "--delete", action="store_true", help="删除目标目录中匹配忽视规则的所有文件")
    parser.add_argument("-w", "--workers", type=int, default=None, help="最大工作线程数，默认为自动检测 (CPU核心数 * 2)")
    
    args = parser.parse_args()

    # 检查源目录是否存在
    if not os.path.exists(args.source):
        print(f"错误: 源目录 '{args.source}' 不存在")
        return 1
        
    # 检查源目录与目标目录是否相同
    if os.path.abspath(args.source) == os.path.abspath(args.sync):
        print("错误: 源目录与目标目录不能相同")
        return 1
    
    # 检查时间精度系数是否合法
    if args.time_factor <= 0:
        print(f"错误: 时间精度系数 {args.time_factor} 不是正整数")
        return 1
        
    # 检查工作线程数是否合法
    if args.workers is not None and args.workers <= 0:
        print(f"错误: 工作线程数 {args.workers} 必须为正整数")
        return 1

    try:
        sync_task = ParallelSync(args.source, args.sync, args.mode, args.interval, args.delete, args.time_factor, args.workers)
        sync_task.run_sync()
        return 0
    except Exception as e:
        print(f"程序执行错误: {e}")
        return 1


if __name__ == "__main__":
    exit_code = main()
    exit(exit_code)