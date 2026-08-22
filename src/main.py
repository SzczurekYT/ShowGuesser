import asyncio
import json
import os
import random
import re
import sys
from io import BytesIO

import discord
from discord.ext import commands

from extractor import random_frame
from indexer import index_movies, natural_sort_key

CONFIG_PATH = "config.json"
STATS_PATH = "stats.json"

with open(CONFIG_PATH) as f:
    config = json.load(f)

index = {}
current = 0
best = 0
score = 0
max_score = 0
round_number = 0

BUCKETS = {
    "episode_blind": ("✅ Guessed the episode blind!", 5, True, discord.Colour.green()),
    "episode_seen": ("✅ Episode after seeing", 2, True, discord.Colour.green()),
    "season_blind_only": ("⚠️ Only the season, blind", 3, False, discord.Colour.orange()),
    "season_seen_only": ("⚠️ Only the season, after seeing", 1, False, discord.Colour.orange()),
    "fail": ("❌ Total miss", 0, False, discord.Colour.red()),
}


class SkipView(discord.ui.View):
    def __init__(self, skip_event):
        super().__init__(timeout=None)
        self.skip_event = skip_event

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.danger)
    async def on_skip(self, interaction, button):
        self.skip_event.set()
        await interaction.response.defer()


def load_stats():
    global current, best, score, max_score, round_number
    try:
        with open(STATS_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except json.JSONDecodeError:
        print(f"warning: {STATS_PATH} is corrupt, starting with zeroed stats", file=sys.stderr)
        current = best = score = max_score = round_number = 0
        return
    current = int(data.get("current", 0))
    best = int(data.get("best", 0))
    score = int(data.get("score", 0))
    max_score = int(data.get("max_score", 0))
    round_number = int(data.get("round_number", 0))


def save_stats():
    data = {
        "current": current,
        "best": best,
        "score": score,
        "max_score": max_score,
        "round_number": round_number,
    }
    with open(STATS_PATH, "w") as f:
        json.dump(data, f, indent=4)


def compile_patterns(value, field, required_groups):
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    patterns = []
    for pattern in value:
        try:
            compiled = re.compile(pattern)
        except re.error as e:
            print(f"invalid {field} pattern {pattern!r}: {e}", file=sys.stderr)
            sys.exit(1)
        missing = set(required_groups) - set(compiled.groupindex)
        if missing:
            print(
                f"{field} pattern {pattern!r} lacks named group(s): {', '.join(sorted(missing))}",
                file=sys.stderr,
            )
            sys.exit(1)
        patterns.append(compiled)
    return patterns or None


def parse_name(name, patterns):
    if patterns is None:
        return {}
    for pattern in patterns:
        match = pattern.search(name)
        if match:
            return match.groupdict()
    return None


def build_index(raw, season_patterns, episode_patterns):
    problems = []
    parsed = {}
    for folder in sorted(raw, key=natural_sort_key):
        season_meta = parse_name(folder, season_patterns)
        if season_meta is None:
            problems.append(f"season folder does not match season_regex: {folder}")
            continue
        episodes = {}
        for filename in raw[folder]:
            stem = os.path.splitext(filename)[0]
            episode_meta = parse_name(stem, episode_patterns)
            if episode_meta is None:
                problems.append(
                    f"episode file does not match episode_regex: {folder}/{filename}"
                )
                continue
            episodes[filename] = {"name": stem, "meta": episode_meta}
        parsed[folder] = {"name": folder, "meta": season_meta, "episodes": episodes}
    return parsed, problems


def season_label(entry):
    meta = entry["meta"] or {}
    title = meta.get("title") or entry["name"]
    season = meta.get("season")
    return f"{title} (S{season})" if season is not None else title


def episode_label(entry):
    meta = entry["meta"] or {}
    title = meta.get("title") or entry["name"]
    episode = meta.get("episode")
    return f"{title} (E{episode})" if episode is not None else title


def format_timestamp(seconds):
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def random_video(index):
    folder = random.choice(list(index))
    name = random.choice(list(index[folder]["episodes"]))
    return folder, name


def numbered_list(items):
    return "\n".join(f"{i}: {item}" for i, item in enumerate(items))


def drop_video(folder, name):
    del index[folder]["episodes"][name]
    if not index[folder]["episodes"]:
        del index[folder]


async def _wait_for_guess(bot, channel, valid_keys, start, skip_event):
    def check(m):
        content = m.content.strip()
        return (
            m.channel == channel
            and m.author != bot.user
            and m.created_at >= start
            and content.isdigit()
            and int(content) in valid_keys
        )

    msg_task = asyncio.create_task(bot.wait_for("message", check=check))
    skip_task = asyncio.create_task(skip_event.wait())

    done, pending = await asyncio.wait(
        {msg_task, skip_task}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()

    if skip_task in done:
        return None
    return int(msg_task.result().content.strip())


async def play_round(bot, images_channel, guessing_channel):
    global round_number

    folder, name = random_video(index)
    try:
        frame, timestamp = random_frame(os.path.join(config["base_dir"], folder, name))
    except (OSError, RuntimeError, ValueError) as e:
        print(f"dropping {folder}/{name}: {e}", file=sys.stderr)
        drop_video(folder, name)
        return False

    round_number += 1
    ts = format_timestamp(timestamp)
    season_data = index[folder]
    seasons = sorted(index, key=natural_sort_key)
    season_labels = [season_label(index[s]) for s in seasons]
    episodes = sorted(season_data["episodes"], key=natural_sort_key)
    episode_labels = [episode_label(season_data["episodes"][e]) for e in episodes]
    actual = f"{season_label(season_data)} — {episode_label(season_data['episodes'][name])}"

    season_keys = [_as_int(index[s]["meta"].get("season")) for s in seasons]
    season_by_key = (
        dict(zip(season_keys, seasons))
        if all(k is not None for k in season_keys)
        else None
    )
    season_valid = set(season_keys) if season_by_key else set(range(len(seasons)))

    episode_keys = [
        _as_int(season_data["episodes"][e]["meta"].get("episode")) for e in episodes
    ]
    episode_by_key = (
        dict(zip(episode_keys, episodes))
        if all(k is not None for k in episode_keys)
        else None
    )
    episode_valid = set(episode_keys) if episode_by_key else set(range(len(episodes)))

    def key_list(keys, labels):
        return "\n".join(f"{k}: {label}" for k, label in zip(keys, labels))

    def season_matches(key):
        return season_by_key[key] == folder if season_by_key else seasons[key] == folder

    def season_label_for(key):
        return (
            season_label(index[season_by_key[key]])
            if season_by_key
            else season_labels[key]
        )

    def episode_matches(key):
        return (
            episode_by_key[key] == name if episode_by_key else episodes[key] == name
        )

    def episode_label_for(key):
        return (
            episode_label(season_data["episodes"][episode_by_key[key]])
            if episode_by_key
            else episode_labels[key]
        )

    skip_event = asyncio.Event()
    view = SkipView(skip_event)
    start = discord.utils.utcnow()

    image_embed = discord.Embed(
        title=f"🎬 Round {round_number}",
        description=f"{actual}\nTimestamp: {ts}",
        colour=discord.Colour.blurple(),
    )
    image_embed.set_image(url="attachment://frame.png")
    image_msg = await images_channel.send(
        file=discord.File(BytesIO(frame), filename="frame.png"),
        embed=image_embed,
        view=view,
    )

    async def finish(bucket_key, guessed):
        global current, best, score, max_score
        heading, pts, success, colour = BUCKETS[bucket_key]
        current = current + 1 if success else 0
        best = max(best, current)
        score += pts
        max_score += 5

        result = (
            f"Guessed: **{guessed}**\n"
            f"Answer: ||[{actual}]||\n"
            f"Timestamp: {ts}\n"
            f"Points: **+{pts}** | Score: **{score}/{max_score}**\n"
            f"Streak: **{current}** | Best streak: **{best}**"
        )
        result_embed = discord.Embed(title=heading, description=result, colour=colour)
        await guessing_channel.send(
            file=discord.File(BytesIO(frame), filename="SPOILER_frame.png"),
            embed=result_embed,
        )

        summary_embed = discord.Embed(
            title=heading,
            description=(
                f"Guessed: {guessed}\n"
                f"Points: +{pts} | Score: **{score}/{max_score}**\n"
                f"Streak: **{current}** | Best streak: **{best}**\n"
                f"Timestamp: {ts}"
            ),
            colour=colour,
        )
        summary_embed.set_image(url="attachment://frame.png")
        await image_msg.edit(embed=summary_embed)

    async def ask_seasons(title):
        embed = discord.Embed(
            title=title,
            description=(
                key_list(season_keys, season_labels)
                if season_by_key
                else numbered_list(season_labels)
            ),
            colour=discord.Colour.blurple(),
        )
        await guessing_channel.send(embed=embed)
        return await _wait_for_guess(bot, guessing_channel, season_valid, start, skip_event)

    async def ask_episodes(title):
        embed = discord.Embed(
            title=title,
            description=(
                key_list(episode_keys, episode_labels)
                if episode_by_key
                else numbered_list(episode_labels)
            ),
        )
        await guessing_channel.send(embed=embed)
        return await _wait_for_guess(bot, guessing_channel, episode_valid, start, skip_event)

    async def round_skipped():
        embed = discord.Embed(
            title="⏭️ Round skipped",
            description="Jumping straight to the next frame.",
            colour=discord.Colour.blurple(),
        )
        await images_channel.send(embed=embed)
        await guessing_channel.send(embed=embed)
        return True

    g_season = await ask_seasons(f"🎬 Round {round_number} — new frame! Guess the season:")
    if g_season is None:
        return await round_skipped()

    season_idx = g_season
    need_season = not season_matches(season_idx)

    if not need_season:
        g_episode = await ask_episodes("Correct, guess the episode:")
        if g_episode is None:
            return await round_skipped()
        if episode_matches(g_episode):
            await finish(
                "episode_blind",
                f"{season_label_for(season_idx)} — {episode_label_for(g_episode)}",
            )
            return False

    seen_embed = discord.Embed(
        title="👀 Second chance",
        description=f"Reveal the spoiler to see the frame, then guess again.\nTimestamp: {ts}",
        colour=discord.Colour.gold(),
    )
    await guessing_channel.send(
        file=discord.File(BytesIO(frame), filename="SPOILER_frame.png"),
        embed=seen_embed,
    )

    if need_season:
        g_season = await ask_seasons("Guess the season again:")
        if g_season is None:
            return await round_skipped()
        season_idx = g_season
        if not season_matches(season_idx):
            await finish("fail", f"{season_label_for(season_idx)} — ?")
            return False

    g_episode = await ask_episodes("Guess the episode again:")
    if g_episode is None:
        return await round_skipped()

    if episode_matches(g_episode):
        await finish(
            "episode_seen",
            f"{season_label_for(season_idx)} — {episode_label_for(g_episode)}",
        )
    elif need_season:
        await finish("season_seen_only", f"{season_label_for(season_idx)} — ?")
    else:
        await finish("season_blind_only", f"{season_label_for(season_idx)} — ?")
    return False


async def game_loop(bot, images_channel, guessing_channel):
    while index:
        try:
            was_skipped = await play_round(bot, images_channel, guessing_channel)
        except Exception as e:
            print(f"round failed: {e}", file=sys.stderr)
            await asyncio.sleep(1)
            was_skipped = False
        await asyncio.sleep(0 if was_skipped else config.get("round_delay", 5))
    print("no videos left, stopping", file=sys.stderr)


def main():
    load_stats()

    base_dir = config.get("base_dir")
    if not base_dir:
        print("missing base_dir in config.json", file=sys.stderr)
        sys.exit(1)
    if not os.path.isdir(base_dir):
        print(f"base_dir is not a directory: {base_dir!r}", file=sys.stderr)
        sys.exit(1)

    round_delay = config.get("round_delay", 5)
    if not isinstance(round_delay, (int, float)) or round_delay <= 0:
        print("round_delay must be a positive number", file=sys.stderr)
        sys.exit(1)

    season_patterns = compile_patterns(
        config.get("season_regex"), "season_regex", ("title", "season")
    )
    episode_patterns = compile_patterns(
        config.get("episode_regex"), "episode_regex", ("title", "episode")
    )

    raw = index_movies(base_dir, config.get("exclude"))
    if not raw:
        print(f"no video files found under {base_dir!r}", file=sys.stderr)
        sys.exit(1)

    global index
    index, problems = build_index(raw, season_patterns, episode_patterns)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        print(
            f"{len(problems)} item(s) failed regex matching — fix the names,"
            " adjust the regexes or add exclude patterns",
            file=sys.stderr,
        )
        sys.exit(1)

    token = config.get("token")
    if not token:
        print("missing bot token in config.json", file=sys.stderr)
        sys.exit(1)

    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready():
        images_channel = bot.get_channel(config["images_channel"])
        guessing_channel = bot.get_channel(config["guessing_channel"])
        if not isinstance(images_channel, discord.TextChannel) or not isinstance(
            guessing_channel, discord.TextChannel
        ):
            print("could not resolve images/guessing channels from config", file=sys.stderr)
            await bot.close()
            return
        print(f"logged in as {bot.user}")
        print(f"posting to #{images_channel.name} and #{guessing_channel.name}")
        asyncio.create_task(game_loop(bot, images_channel, guessing_channel))

    try:
        bot.run(token)
    finally:
        save_stats()


if __name__ == "__main__":
    main()