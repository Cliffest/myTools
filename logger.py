import logging
import sys
from typing import Tuple


class LogDivider:
    def __init__(self, log_file: str, width=50):
        self.log_file = log_file
        self.width = width

    def write(self, message: str):
        with open(self.log_file, "a") as f:
            f.write(message)
        print(message, end="")

    def blank(self):
        """
        Single line - A blank line.
        """
        self.write("\n")

    def _line(self, char: str):
        """
        Single line - A line filled with `char` characters.
        """
        self.write(char[0] * self.width + "\n")

    def _word_line(self, word: str, char: str) -> str:
        """
        Single line - Centralize the word and fill the line with `char` characters.
        """
        self.write(f" {word} ".center(self.width, char[0]) + "\n")

    def line(self):
        self._line("-")

    def dline(self):
        self._line("=")

    def word_line(self, word: str):
        self._word_line(word, "-")

def get_logger(name: str, level=logging.INFO, width=50) -> Tuple[logging.Logger, LogDivider]:
    """
    Get a logger by a specified name.
    return: logger & divider (log_divider)
    """
    log_divider = LogDivider(name + ".log", width)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    
    for h in list(logger.handlers):  # Clear old handlers
        logger.removeHandler(h)

    file_handler = logging.FileHandler(log_divider.log_file, mode="a", encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(
        fmt="%(levelname)-8s %(asctime)s.%(msecs)03d | %(message)s",
        datefmt="%y-%m-%d,%H:%M:%S"
    ))

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(file_handler.formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger, log_divider

class Logger:
    def __init__(self, name: str, level=logging.INFO, width=50):
        assert level in [
                logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL,
                "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
            ], f"Invalid logger level: {level}"
        self.logger, self.divider = get_logger(name, level, width)