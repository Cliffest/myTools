from enum import Enum

class ExecutionStatus(Enum):
    error = "ERROR"
    warning = "Warning"

def try_except(throw_status, print_error=True):
    def inner(func):
    
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if print_error:
                    print(f"{throw_status.value}: {e}")
                return throw_status
        return wrapper
    
    return inner

def interrupt_continue(func):
    throw_status = ExecutionStatus.warning
    
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except KeyboardInterrupt:
            print(f"{throw_status.value}: KeyboardInterrupt")
            return throw_status
    return wrapper