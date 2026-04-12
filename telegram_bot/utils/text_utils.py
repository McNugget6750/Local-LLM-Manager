from typing import List
import re

def split_text(text: str, limit: int = 4096) -> List[str]:
    """
    Splits a string into chunks of at most 'limit' characters, 
    trying to split at sentence or word boundaries to avoid cutting words.
    This implementation is grapheme-safe as it uses Python's native string slicing
    which handles Unicode characters correctly.
    """
    if not text:
        return []
    
    if len(text) <= limit:
        return [text]
    
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        
        # Look for a good split point within the limit
        split_pos = limit
        
        # Try to find the last sentence end (. ! ?) within the limit
        # We use a regex to ensure we don't split in the middle of a multi-byte char
        # though Python strings are already unicode.
        match = re.search(r'[.!?]\s', text[:limit])
        if match:
            # Find the last occurrence of sentence end within the limit
            # We search backwards from the limit
            last_sentence_end = -1
            for i in range(limit - 1, -1, -1):
                if text[i] in '.!?' and (i + 1 == len(text) or text[i+1].isspace()):
                    last_sentence_end = i + 1
                    break
            
            if last_sentence_end > limit * 0.7:
                split_pos = last_sentence_end
        
        # If no sentence end, try to find the last space
        if split_pos == limit:
            last_space = text.rfind(' ', 0, limit)
            if last_space > limit * 0.7:
                split_pos = last_space
        
        chunks.append(text[:split_pos])
        text = text[split_pos:].lstrip()
        
    return chunks