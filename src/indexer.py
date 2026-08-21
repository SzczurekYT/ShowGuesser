import fnmatch
import os
import re

MOVIE_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v"}


def natural_sort_key(s):
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", s)]


def index_movies(path, exclude=None):
    exclude = exclude or []

    def excluded(name):
        return any(fnmatch.fnmatch(name, pattern) for pattern in exclude)

    result = {}
    for entry in os.scandir(path):
        if entry.is_dir() and not excluded(entry.name):
            movies = sorted(
                (e.name for e in os.scandir(entry.path) if e.is_file()),
                key=natural_sort_key,
            )
            movies = [
                m
                for m in movies
                if os.path.splitext(m)[1].lower() in MOVIE_EXTENSIONS
                and not excluded(f"{entry.name}/{m}")
            ]
            if movies:
                result[entry.name] = movies
    return result