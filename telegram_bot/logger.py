import logging
import sys

def get_logger(name: str) -> logging.Logger:
    """
    Returns a configured logger instance.
    
    Args:
        name (str): The name of the logger, typically __name__.
        
    Returns:
        logging.Logger: A logger configured with a StreamHandler to stdout.
    """
    logger = logging.getLogger(name)
    
    # Set log level to INFO
    logger.setLevel(logging.INFO)
    
    # Prevent duplicate handlers if get_logger is called multiple times for the same name
    if not logger.handlers:
        # Create a StreamHandler pointing to sys.stdout
        handler = logging.StreamHandler(sys.stdout)
        
        # Define the format: %(asctime)s - %(name)s - %(levelname)s - %(message)s
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        
        # Add the handler to the logger
        logger.addHandler(handler)
        
    return logger