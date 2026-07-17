"""
SourceIndex: walks a directory and builds a token-efficient map
of file paths → content. Used by the Diagnostician to give the
LLM precise source context without blowing the context window.
"""
import os
import fnmatch
from dataclasses import dataclass

SKIP_PATTERNS = [
    "*.pyc", "__pycache__", ".git", "*.egg-info",
    "node_modules", ".venv", "venv", "*.lock",
]
MAX_FILE_BYTES = 20_000  # truncate large files


@dataclass
class SourceFile:
    path: str          # relative path
    content: str
    language: str


def _lang(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {".py": "python", ".ts": "typescript", ".js": "javascript",
            ".go": "go", ".rs": "rust", ".java": "java"}.get(ext, "text")


def _skip(name: str) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in SKIP_PATTERNS)


class SourceIndex:
    def __init__(self, root: str):
        self.root = root
        self.files: list[SourceFile] = []
        self._build()

    def _build(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not _skip(d)]
            for fname in filenames:
                if _skip(fname):
                    continue
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, self.root)
                try:
                    with open(full, "r", errors="replace") as f:
                        content = f.read(MAX_FILE_BYTES)
                    self.files.append(SourceFile(path=rel, content=content, language=_lang(fname)))
                except Exception:
                    pass

    def format_for_llm(self, relevant_paths: list[str] | None = None) -> str:
        """Return a compact XML-ish block for LLM context."""
        files = self.files
        if relevant_paths:
            files = [f for f in files if any(p in f.path for p in relevant_paths)]
        chunks = []
        for sf in files:
            chunks.append(
                f"<file path=\"{sf.path}\" lang=\"{sf.language}\">\n{sf.content}\n</file>"
            )
        return "\n\n".join(chunks)

    def paths(self) -> list[str]:
        return [f.path for f in self.files]
