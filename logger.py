import logging
import sys
import textwrap
from typing import Tuple


class LogDivider:
    def __init__(self, log_file: str, width=80):
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

class WrappingFormatter(logging.Formatter):
    def __init__(self, fmt, datefmt=None, width=80, start_from=30):
        super().__init__(fmt, datefmt)
        self.width = width
        self.start_from = start_from
        
    def format(self, record):
        # Get the formatted message using the parent formatter
        formatted = super().format(record)
        
        # Split into prefix (everything before the message) and the actual message
        parts = formatted.split(' | ', 1)
        if len(parts) != 2:
            return formatted  # Fallback if format doesn't match expected pattern
            
        prefix, message = parts
        prefix_with_separator = prefix + ' | '
        
        # Calculate available width for the first line (actual prefix length)
        first_line_width = self.width - len(prefix_with_separator)
        # Calculate available width for continuation lines (considering start_from indentation)
        continuation_width = self.width - self.start_from
        
        if first_line_width <= 0 or continuation_width <= 0:
            return formatted  # Not enough space for wrapping
        
        # First, try to fit as much as possible on the first line
        if len(message) <= first_line_width:
            return formatted  # No wrapping needed
        
        if message[first_line_width] != ' ':
            # Find the last space before the first line width to avoid breaking words
            last_space = message.rfind(' ', 0, first_line_width)
            if last_space == -1:
                last_space = first_line_width
            first_line_width = last_space + 1
        
        # Split the message: first line gets first_line_width, rest gets continuation_width
        first_line = message[:first_line_width]
        remaining = message[first_line_width:]
        
        # Wrap the remaining text for continuation lines
        wrapped_lines = textwrap.wrap(remaining, width=continuation_width)
        
        # Create the final formatted output
        result = prefix_with_separator + first_line
        
        # Add continuation lines with fixed indentation
        indent = ' ' * self.start_from
        for line in wrapped_lines:
            result += '\n' + indent + line
            
        return result

def get_logger(name: str, datefmt="%m-%d,%H:%M:%S", level=logging.INFO, 
               width=80, start_from=30) -> Tuple[logging.Logger, LogDivider]:
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

    # Create the wrapping formatter
    formatter = WrappingFormatter(
        fmt="%(levelname)-8s %(asctime)s.%(msecs)03d | %(message)s",
        datefmt=datefmt,
        width=width,
        start_from=start_from
    )

    file_handler = logging.FileHandler(log_divider.log_file, mode="a", encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger, log_divider

class Logger:
    def __init__(self, name: str, datefmt="%m-%d,%H:%M:%S", level=logging.INFO, 
                       width=80, start_from=30):
        assert level in [
                logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL,
                "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
            ], f"Invalid logger level: {level}"
        self.logger, self.divider = get_logger(name, datefmt, level, width, start_from)


if __name__ == "__main__":
    # Test with start_from=30
    logger = Logger("test", width=80, start_from=30)
    logger.logger.info("this is a log message this is a log message thisemoveeeee is a log message this is a log message this is a log message this is a log message this is a log message this is a log message.")
    
    print("\n" + "="*80 + "\n")
    
    # Test with different start_from
    logger2 = Logger("test2", width=80, start_from=25, datefmt="%Y-%m-%d %H:%M:%S")
    logger2.logger.info("Remove the task at position POS (1-based). Removeeeee the task at position POS (1-based).Remove the task at position POS (1-based).Remove the task at position POS (1-based).Remove the task at position POS (1-based).")