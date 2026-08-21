import json
import random
import subprocess


def random_frame(path):
    duration = _duration(path)
    if duration <= 0.5:
        raise ValueError(f"video too short to sample a random frame: {path!r}")

    start = random.uniform(0, duration - 0.5)
    result = subprocess.run(
        [
            "ffmpeg",
            "-ss",
            str(start),
            "-i",
            path,
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed for {path!r}: {result.stderr.decode(errors='replace')}"
        )
    return result.stdout, start


def _duration(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=duration:format=duration",
            "-of",
            "json",
            path,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for {path!r}: {result.stderr}"
        )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ValueError(f"could not determine duration of {path!r}") from None

    for stream in data.get("streams", []):
        duration = stream.get("duration")
        if duration:
            return float(duration)

    format_duration = data.get("format", {}).get("duration")
    if format_duration:
        return float(format_duration)

    raise ValueError(f"could not determine duration of {path!r}")