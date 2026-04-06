import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Literal

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("seerr-bot")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID", "")
SEERR_URL = os.getenv("SEERR_URL", "").rstrip("/")
SEERR_API_KEY = os.getenv("SEERR_API_KEY", "")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))
RESULT_LIMIT = max(1, min(int(os.getenv("RESULT_LIMIT", "10")), 25))

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing")
if not SEERR_URL:
    raise RuntimeError("SEERR_URL is missing")
if not SEERR_API_KEY:
    raise RuntimeError("SEERR_API_KEY is missing")


@dataclass
class SearchItem:
    media_id: int
    media_type: Literal["movie", "tv"]
    title: str
    year: str
    overview: str
    poster_path: str | None
    is_available: bool
    is_requested: bool


def parse_season_input(text: str) -> str | list[int]:
    """
    Accepts:
      all
      1
      1,2,3
      1-4
      1,3,5-7

    Returns:
      "all" or sorted list[int]
    """
    raw = text.strip().lower()

    if raw == "all":
        return "all"

    if not raw:
        raise ValueError("Season input cannot be empty.")

    parts = [p.strip() for p in raw.split(",") if p.strip()]
    seasons: set[int] = set()

    for part in parts:
        if re.fullmatch(r"\d+", part):
            num = int(part)
            if num < 1:
                raise ValueError("Season numbers must be 1 or higher.")
            seasons.add(num)
            continue

        if re.fullmatch(r"\d+\s*-\s*\d+", part):
            start_str, end_str = re.split(r"\s*-\s*", part)
            start = int(start_str)
            end = int(end_str)
            if start < 1 or end < 1:
                raise ValueError("Season numbers must be 1 or higher.")
            if start > end:
                raise ValueError(f"Invalid range: {part}")
            for num in range(start, end + 1):
                seasons.add(num)
            continue

        raise ValueError(
            f"Invalid season entry: '{part}'. Use all, 1, 1,2,3, 1-4, or 1,3,5-7."
        )

    if not seasons:
        raise ValueError("No valid seasons were provided.")

    return sorted(seasons)


class SeerrClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 20) -> None:
        self.base_url = f"{base_url}/api/v1"
        self.headers = {
            "X-Api-Key": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers=self.headers,
                timeout=self.timeout,
            )

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def _get(self, path: str, **params: Any) -> Any:
        if not self.session:
            raise RuntimeError("HTTP session not started")

        url = f"{self.base_url}{path}"
        async with self.session.get(url, params=params) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"GET {path} failed [{resp.status}]: {text}")
            return await resp.json()

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        if not self.session:
            raise RuntimeError("HTTP session not started")

        url = f"{self.base_url}{path}"
        async with self.session.post(url, json=payload) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"POST {path} failed [{resp.status}]: {text}")
            if text:
                return await resp.json()
            return {}

    async def search(self, query: str, wanted_type: Literal["movie", "tv"]) -> list[SearchItem]:
        data = await self._get("/search", query=query, page=1, language="en")
        results = data.get("results", [])

        parsed: list[SearchItem] = []

        for item in results:
            media_type = item.get("mediaType")
            if media_type != wanted_type:
                continue

            if media_type == "movie":
                title = item.get("title", "Unknown Title")
                release_date = item.get("releaseDate") or ""
            else:
                title = item.get("name", "Unknown Title")
                release_date = item.get("firstAirDate") or ""

            year = release_date[:4] if len(release_date) >= 4 else "Unknown"
            media_info = item.get("mediaInfo") or {}

            status = str(media_info.get("status", "")).lower()
            requests = media_info.get("requests") or []

            parsed.append(
                SearchItem(
                    media_id=int(item["id"]),
                    media_type=media_type,
                    title=title,
                    year=year,
                    overview=item.get("overview", "No overview provided."),
                    poster_path=item.get("posterPath"),
                    is_available=status == "available",
                    is_requested=len(requests) > 0,
                )
            )

        parsed.sort(key=lambda x: (x.title.lower(), x.year))
        return parsed[:RESULT_LIMIT]

    async def request_movie(self, media_id: int) -> dict[str, Any]:
        payload = {
            "mediaType": "movie",
            "mediaId": media_id,
        }
        return await self._post("/request", payload)

    async def request_series(self, media_id: int, seasons: str | list[int]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "mediaType": "tv",
            "mediaId": media_id,
            "seasons": seasons,
        }
        return await self._post("/request", payload)


class SeasonRequestModal(discord.ui.Modal, title="Request Seasons"):
    season_input = discord.ui.TextInput(
        label="Seasons",
        placeholder="all  OR  1,2,3  OR  1-4  OR  1,3,5-7",
        required=True,
        max_length=100,
    )

    def __init__(self, seerr: SeerrClient, item: SearchItem) -> None:
        super().__init__(timeout=300)
        self.seerr = seerr
        self.item = item

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            parsed = parse_season_input(str(self.season_input))

            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.seerr.request_series(self.item.media_id, parsed)

            seasons_text = "all seasons" if parsed == "all" else f"seasons: {', '.join(map(str, parsed))}"

            embed = discord.Embed(
                title="Series request sent",
                description=f"Requested **{self.item.title} ({self.item.year})** with **{seasons_text}**.",
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        except ValueError as exc:
            await interaction.response.send_message(
                f"Invalid season input: {exc}",
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("Failed to request series")
            if interaction.response.is_done():
                await interaction.followup.send(
                    f"Series request failed: `{exc}`",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    f"Series request failed: `{exc}`",
                    ephemeral=True,
                )


class MovieSelect(discord.ui.Select):
    def __init__(
        self,
        seerr: SeerrClient,
        items: list[SearchItem],
        requester_id: int,
    ) -> None:
        self.seerr = seerr
        self.items = items
        self.requester_id = requester_id

        options: list[discord.SelectOption] = []
        for idx, item in enumerate(items):
            status_bits = []
            if item.is_available:
                status_bits.append("available")
            elif item.is_requested:
                status_bits.append("already requested")

            status_text = f" • {', '.join(status_bits)}" if status_bits else ""
            label = f"{item.title} ({item.year})"
            description = (item.overview[:80] + "...") if len(item.overview) > 80 else item.overview
            if not description:
                description = f"MOVIE{status_text}"

            options.append(
                discord.SelectOption(
                    label=label[:100],
                    description=description[:100],
                    value=str(idx),
                )
            )

        super().__init__(
            placeholder="Choose the correct movie...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who ran the command can use this menu.",
                ephemeral=True,
            )
            return

        selected = self.items[int(self.values[0])]

        if selected.is_available:
            await interaction.response.send_message(
                f"**{selected.title} ({selected.year})** is already available.",
                ephemeral=True,
            )
            return

        if selected.is_requested:
            await interaction.response.send_message(
                f"**{selected.title} ({selected.year})** has already been requested.",
                ephemeral=True,
            )
            return

        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.seerr.request_movie(selected.media_id)

            embed = discord.Embed(
                title="Movie request sent",
                description=f"Requested **{selected.title} ({selected.year})**.",
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

            if self.view:
                for child in self.view.children:
                    child.disabled = True
                await interaction.message.edit(view=self.view)

        except Exception as exc:
            log.exception("Failed to create movie request")
            await interaction.followup.send(
                f"Movie request failed: `{exc}`",
                ephemeral=True,
            )


class SeriesSelect(discord.ui.Select):
    def __init__(
        self,
        seerr: SeerrClient,
        items: list[SearchItem],
        requester_id: int,
    ) -> None:
        self.seerr = seerr
        self.items = items
        self.requester_id = requester_id

        options: list[discord.SelectOption] = []
        for idx, item in enumerate(items):
            status_bits = []
            if item.is_available:
                status_bits.append("available")
            elif item.is_requested:
                status_bits.append("already requested")

            status_text = f" • {', '.join(status_bits)}" if status_bits else ""
            label = f"{item.title} ({item.year})"
            description = (item.overview[:80] + "...") if len(item.overview) > 80 else item.overview
            if not description:
                description = f"SERIES{status_text}"

            options.append(
                discord.SelectOption(
                    label=label[:100],
                    description=description[:100],
                    value=str(idx),
                )
            )

        super().__init__(
            placeholder="Choose the correct series...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who ran the command can use this menu.",
                ephemeral=True,
            )
            return

        selected = self.items[int(self.values[0])]

        if selected.is_available:
            await interaction.response.send_message(
                f"**{selected.title} ({selected.year})** is already available.",
                ephemeral=True,
            )
            return

        if selected.is_requested:
            await interaction.response.send_message(
                f"**{selected.title} ({selected.year})** has already been requested.",
                ephemeral=True,
            )
            return

        modal = SeasonRequestModal(self.seerr, selected)
        await interaction.response.send_modal(modal)


class MovieRequestView(discord.ui.View):
    def __init__(self, seerr: SeerrClient, items: list[SearchItem], requester_id: int) -> None:
        super().__init__(timeout=120)
        self.add_item(MovieSelect(seerr, items, requester_id))

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True


class SeriesRequestView(discord.ui.View):
    def __init__(self, seerr: SeerrClient, items: list[SearchItem], requester_id: int) -> None:
        super().__init__(timeout=120)
        self.add_item(SeriesSelect(seerr, items, requester_id))

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True


class SeerrBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.seerr = SeerrClient(SEERR_URL, SEERR_API_KEY, REQUEST_TIMEOUT)

    async def setup_hook(self) -> None:
        await self.seerr.start()

        if DISCORD_GUILD_ID:
            guild = discord.Object(id=int(DISCORD_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced commands to guild %s", DISCORD_GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Synced global commands")

    async def close(self) -> None:
        await self.seerr.close()
        await super().close()


bot = SeerrBot()


def build_results_embed(query: str, media_type: str, items: list[SearchItem]) -> discord.Embed:
    embed = discord.Embed(
        title=f"Search results for {media_type}",
        description=f'Query: **{query}**\nChoose the correct result from the dropdown below.',
    )

    preview_lines = []
    for idx, item in enumerate(items[:10], start=1):
        flags = []
        if item.is_available:
            flags.append("available")
        elif item.is_requested:
            flags.append("requested")

        suffix = f" — {', '.join(flags)}" if flags else ""
        preview_lines.append(f"{idx}. **{item.title} ({item.year})**{suffix}")

    embed.add_field(
        name="Top matches",
        value="\n".join(preview_lines) if preview_lines else "No results.",
        inline=False,
    )
    return embed


@bot.tree.command(name="movie", description="Search Seerr and request a movie")
@app_commands.describe(title="Movie title to search for")
async def movie(interaction: discord.Interaction, title: str) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        results = await bot.seerr.search(title, "movie")
        if not results:
            await interaction.followup.send(
                f'No movie results found for **"{title}"**.',
                ephemeral=True,
            )
            return

        embed = build_results_embed(title, "movies", results)
        view = MovieRequestView(bot.seerr, results, interaction.user.id)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    except Exception as exc:
        log.exception("Movie search failed")
        await interaction.followup.send(
            f"Movie search failed: `{exc}`",
            ephemeral=True,
        )


@bot.tree.command(name="series", description="Search Seerr and request a TV series")
@app_commands.describe(title="Series title to search for")
async def series(interaction: discord.Interaction, title: str) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        results = await bot.seerr.search(title, "tv")
        if not results:
            await interaction.followup.send(
                f'No series results found for **"{title}"**.',
                ephemeral=True,
            )
            return

        embed = build_results_embed(title, "series", results)
        view = SeriesRequestView(bot.seerr, results, interaction.user.id)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    except Exception as exc:
        log.exception("Series search failed")
        await interaction.followup.send(
            f"Series search failed: `{exc}`",
            ephemeral=True,
        )


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")


async def main() -> None:
    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())