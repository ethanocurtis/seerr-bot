import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

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

TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

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

    @property
    def poster_url(self) -> str | None:
        if not self.poster_path:
            return None
        return f"{TMDB_IMAGE_BASE}{self.poster_path}"


def parse_season_input(text: str) -> str | list[int]:
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


def short_overview(text: str, limit: int = 100) -> str:
    text = (text or "").strip()
    if not text:
        return "No overview provided."
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


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


def build_confirm_embed(item: SearchItem, kind: Literal["movie", "series"]) -> discord.Embed:
    if item.is_available:
        status_text = "Already available"
    elif item.is_requested:
        status_text = "Already requested"
    else:
        status_text = "Ready to request"

    embed = discord.Embed(
        title=f"{item.title} ({item.year})",
        description=item.overview or "No overview provided.",
    )
    embed.add_field(name="Type", value=kind.capitalize(), inline=True)
    embed.add_field(name="Status", value=status_text, inline=True)

    if item.poster_url:
        embed.set_image(url=item.poster_url)

    return embed


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
        encoded_query = quote(query, safe="")
        data = await self._get("/search", query=encoded_query, page=1, language="en")
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

            log.info("Search result media_info for %s: %s", title, media_info)

            raw_status = media_info.get("status")
            status_text = str(raw_status).strip().lower() if raw_status is not None else ""

            raw_status_4k = media_info.get("status4k")
            status_4k_text = str(raw_status_4k).strip().lower() if raw_status_4k is not None else ""

            download_status = media_info.get("downloadStatus")
            download_status_text = (
                str(download_status).strip().lower() if download_status is not None else ""
            )

            requests = media_info.get("requests") or []

            is_available = any(
                [
                    status_text == "available",
                    status_4k_text == "available",
                    download_status_text == "available",
                    bool(media_info.get("canWatch")),
                    bool(media_info.get("available")),
                ]
            )

            is_requested = any(
                [
                    len(requests) > 0,
                    status_text == "requested",
                    bool(media_info.get("requested")),
                ]
            )

            parsed.append(
                SearchItem(
                    media_id=int(item["id"]),
                    media_type=media_type,
                    title=title,
                    year=year,
                    overview=item.get("overview", "No overview provided."),
                    poster_path=item.get("posterPath"),
                    is_available=is_available,
                    is_requested=is_requested,
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
            if self.item.is_available:
                await interaction.response.send_message(
                    f"**{self.item.title} ({self.item.year})** is already available.",
                    ephemeral=True,
                )
                return

            if self.item.is_requested:
                await interaction.response.send_message(
                    f"**{self.item.title} ({self.item.year})** has already been requested.",
                    ephemeral=True,
                )
                return

            parsed = parse_season_input(str(self.season_input))

            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.seerr.request_series(self.item.media_id, parsed)

            seasons_text = (
                "all seasons"
                if parsed == "all"
                else f"seasons: {', '.join(map(str, parsed))}"
            )

            embed = discord.Embed(
                title="Series request sent",
                description=f"Requested **{self.item.title} ({self.item.year})** with **{seasons_text}**.",
            )
            if self.item.poster_url:
                embed.set_thumbnail(url=self.item.poster_url)

            await interaction.followup.send(embed=embed, ephemeral=True)

        except ValueError as exc:
            if interaction.response.is_done():
                await interaction.followup.send(
                    f"Invalid season input: {exc}",
                    ephemeral=True,
                )
            else:
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


class MovieConfirmView(discord.ui.View):
    def __init__(self, seerr: SeerrClient, item: SearchItem, requester_id: int) -> None:
        super().__init__(timeout=300)
        self.seerr = seerr
        self.item = item
        self.requester_id = requester_id

        button_label = "Request Movie"
        if item.is_available:
            button_label = "Already Available"
        elif item.is_requested:
            button_label = "Already Requested"

        request_button = discord.ui.Button(
            label=button_label,
            style=discord.ButtonStyle.green,
            disabled=item.is_available or item.is_requested,
        )
        request_button.callback = self.request_callback
        self.add_item(request_button)

    async def request_callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who ran the command can use this button.",
                ephemeral=True,
            )
            return

        if self.item.is_available:
            await interaction.response.send_message(
                f"**{self.item.title} ({self.item.year})** is already available.",
                ephemeral=True,
            )
            return

        if self.item.is_requested:
            await interaction.response.send_message(
                f"**{self.item.title} ({self.item.year})** has already been requested.",
                ephemeral=True,
            )
            return

        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.seerr.request_movie(self.item.media_id)

            success_embed = discord.Embed(
                title="Movie request sent",
                description=f"Requested **{self.item.title} ({self.item.year})**.",
            )
            if self.item.poster_url:
                success_embed.set_thumbnail(url=self.item.poster_url)

            await interaction.followup.send(embed=success_embed, ephemeral=True)
        except Exception as exc:
            log.exception("Failed to create movie request")
            await interaction.followup.send(
                f"Movie request failed: `{exc}`",
                ephemeral=True,
            )


class SeriesConfirmView(discord.ui.View):
    def __init__(self, seerr: SeerrClient, item: SearchItem, requester_id: int) -> None:
        super().__init__(timeout=300)
        self.seerr = seerr
        self.item = item
        self.requester_id = requester_id

        button_label = "Request Series"
        if item.is_available:
            button_label = "Already Available"
        elif item.is_requested:
            button_label = "Already Requested"

        request_button = discord.ui.Button(
            label=button_label,
            style=discord.ButtonStyle.green,
            disabled=item.is_available or item.is_requested,
        )
        request_button.callback = self.request_callback
        self.add_item(request_button)

    async def request_callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who ran the command can use this button.",
                ephemeral=True,
            )
            return

        if self.item.is_available:
            await interaction.response.send_message(
                f"**{self.item.title} ({self.item.year})** is already available.",
                ephemeral=True,
            )
            return

        if self.item.is_requested:
            await interaction.response.send_message(
                f"**{self.item.title} ({self.item.year})** has already been requested.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(SeasonRequestModal(self.seerr, self.item))


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
            label = f"{item.title} ({item.year})"
            description = short_overview(item.overview, 100)
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
        embed = build_confirm_embed(selected, "movie")
        view = MovieConfirmView(self.seerr, selected, self.requester_id)

        await interaction.response.edit_message(embed=embed, view=view)


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
            label = f"{item.title} ({item.year})"
            description = short_overview(item.overview, 100)
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
        embed = build_confirm_embed(selected, "series")
        view = SeriesConfirmView(self.seerr, selected, self.requester_id)

        await interaction.response.edit_message(embed=embed, view=view)


class MovieRequestView(discord.ui.View):
    def __init__(self, seerr: SeerrClient, items: list[SearchItem], requester_id: int) -> None:
        super().__init__(timeout=120)
        self.add_item(MovieSelect(seerr, items, requester_id))


class SeriesRequestView(discord.ui.View):
    def __init__(self, seerr: SeerrClient, items: list[SearchItem], requester_id: int) -> None:
        super().__init__(timeout=120)
        self.add_item(SeriesSelect(seerr, items, requester_id))


class RequestGroup(app_commands.Group):
    def __init__(self) -> None:
        super().__init__(name="request", description="Request movies and series from Seerr")

    @app_commands.command(name="movie", description="Search Seerr and request a movie")
    @app_commands.describe(title="Movie title to search for")
    async def movie(self, interaction: discord.Interaction, title: str) -> None:
        bot_instance = interaction.client
        if not isinstance(bot_instance, SeerrBot):
            await interaction.response.send_message("Bot client error.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            results = await bot_instance.seerr.search(title, "movie")
            if not results:
                await interaction.followup.send(
                    f'No movie results found for **"{title}"**.',
                    ephemeral=True,
                )
                return

            embed = build_results_embed(title, "movies", results)
            view = MovieRequestView(bot_instance.seerr, results, interaction.user.id)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        except Exception as exc:
            log.exception("Movie search failed")
            await interaction.followup.send(
                f"Movie search failed: `{exc}`",
                ephemeral=True,
            )

    @app_commands.command(name="series", description="Search Seerr and request a TV series")
    @app_commands.describe(title="Series title to search for")
    async def series(self, interaction: discord.Interaction, title: str) -> None:
        bot_instance = interaction.client
        if not isinstance(bot_instance, SeerrBot):
            await interaction.response.send_message("Bot client error.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            results = await bot_instance.seerr.search(title, "tv")
            if not results:
                await interaction.followup.send(
                    f'No series results found for **"{title}"**.',
                    ephemeral=True,
                )
                return

            embed = build_results_embed(title, "series", results)
            view = SeriesRequestView(bot_instance.seerr, results, interaction.user.id)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        except Exception as exc:
            log.exception("Series search failed")
            await interaction.followup.send(
                f"Series search failed: `{exc}`",
                ephemeral=True,
            )


class SeerrBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.seerr = SeerrClient(SEERR_URL, SEERR_API_KEY, REQUEST_TIMEOUT)

    async def setup_hook(self) -> None:
        await self.seerr.start()

        request_group = RequestGroup()

        if DISCORD_GUILD_ID:
            guild = discord.Object(id=int(DISCORD_GUILD_ID))
            self.tree.add_command(request_group, guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %s guild commands to %s", len(synced), DISCORD_GUILD_ID)
        else:
            self.tree.add_command(request_group)
            synced = await self.tree.sync()
            log.info("Synced %s global commands", len(synced))

    async def close(self) -> None:
        await self.seerr.close()
        await super().close()


bot = SeerrBot()


@bot.event
async def on_ready() -> None:
    if bot.user:
        log.info("Logged in as %s (%s)", bot.user, bot.user.id)


async def main() -> None:
    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())