import logging
import math
from datetime import datetime, timedelta

import motor.motor_asyncio
from cachetools import TTLCache
from discord import NotFound
from discord.ext import commands
from discord.ext.commands import Context
from discord.ext.commands import hybrid_command

from utils import reporter
from utils.cfg import cfg
from utils.embeds import Embed
from utils.reporter import report_error
from utils.visibility import is_hidden

log = logging.getLogger("metrics")
log.setLevel(cfg["log_level"])


class Metrics(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.notice_ttl_cache = TTLCache(math.inf, ttl=60 * 15)
        self.mongo = motor.motor_asyncio.AsyncIOMotorClient(cfg["mongodb_uri"])
        self.db = self.mongo.rocketwatch
        self.collection = self.db.command_metrics

        self.bot.tree.on_error = self.on_command_error

    @hybrid_command()
    async def metrics(self, ctx: Context):
        """
        Show various metrics about the bot.
        """
        await ctx.defer(ephemeral=is_hidden(ctx))
        try:
            e = Embed(title="Metrics from the last 7 days")
            desc = "```\n"
            # last 7 days
            start = datetime.utcnow() - timedelta(days=7)

            # get the total number of processed events from the event_queue in the last 7 days
            total_events_processed = await self.db.event_queue.count_documents({'time_seen': {'$gte': start}})
            desc += f"Total Events Processed:\n\t{total_events_processed}\n\n"

            # get the total number of handled commands in the last 7 days
            total_commands_handled = await self.collection.count_documents({'timestamp': {'$gte': start}})
            desc += f"Total Commands Handled:\n\t{total_commands_handled}\n\n"

            # get the average command response time in the last 7 days
            avg_response_time = await self.collection.aggregate([
                {'$match': {'timestamp': {'$gte': start}}},
                {'$group': {'_id': None, 'avg': {'$avg': '$took'}}}
            ]).to_list(length=1)
            if avg_response_time[0]['avg'] is not None:
                desc += f"Average Command Response Time:\n\t{avg_response_time[0]['avg']:.03} seconds\n\n"

            # get completed rate in the last 7 days
            completed_rate = await self.collection.aggregate([
                {'$match': {'timestamp': {'$gte': start}, 'status': 'completed'}},
                {'$group': {'_id': None, 'count': {'$sum': 1}}}
            ]).to_list(length=1)
            if completed_rate:
                percent = completed_rate[0]['count'] / (total_commands_handled - 1)
                desc += f"Command Success Rate:\n\t{percent:.03%}\n\n"

            # get the 5 most used commands of the last 7 days
            most_used_commands = await self.collection.aggregate([
                {'$match': {'timestamp': {'$gte': start}}},
                {'$group': {'_id': '$command', 'count': {'$sum': 1}}},
                {'$sort': {'count': -1}}
            ]).to_list(length=5)
            desc += "Top 5 Commands based on usage:\n"
            for command in most_used_commands:
                desc += f" - {command['_id']}: {command['count']}\n"

            # get the top 5 channels of the last 7 days
            top_channels = await self.collection.aggregate([
                {'$match': {'timestamp': {'$gte': start}}},
                {'$group': {'_id': '$channel', 'count': {'$sum': 1}}},
                {'$sort': {'count': -1}}
            ]).to_list(length=5)
            desc += "\nTop 5 Channels based on commands handled:\n"
            for channel in top_channels:
                desc += f" - {channel['_id']['name']}: {channel['count']}\n"
            e.description = f"{desc}```"
            await ctx.send(embed=e)
        except Exception as e:
            log.error(f"Failed to get command metrics: {e}")
            await report_error(e)

    @commands.Cog.listener()
    async def on_command(self, ctx):
        log.info(f"/{ctx.command.name} triggered by {ctx.author} in #{ctx.channel.name} ({ctx.guild})")
        try:
            await self.collection.insert_one({
                '_id'      : ctx.interaction.id,
                'command'  : ctx.command.name,
                'options'  : ctx.interaction.data.get("options", []),
                'user'     : {
                    'id'  : ctx.author.id,
                    'name': ctx.author.name,
                },
                'guild'    : {
                    'id'  : ctx.guild.id,
                    'name': ctx.guild.name,
                },
                'channel'  : {
                    'id'  : ctx.channel.id,
                    'name': ctx.channel.name,
                },
                "timestamp": datetime.utcnow(),
                'status'   : 'pending'
            })
        except Exception as e:
            log.error(f"Failed to insert command into database: {e}")
            await report_error(e)

    @commands.Cog.listener()
    async def on_command_completion(self, ctx):
        log.info(
            f"/{ctx.command.name} called by {ctx.author} in #{ctx.channel.name} ({ctx.guild}) completed successfully")
        if not is_hidden(ctx) and ctx.author not in self.notice_ttl_cache:
            self.notice_ttl_cache[ctx.author] = True
            e = Embed()
            e.title = 'Did you know?'
            e.description = "Calling this command (or any!) in other channels will make them only appear for you! " \
                            "Give it a try next time!"
            await ctx.reply(embed=e, ephemeral=True)

        try:
            # get the timestamp of when the command was called from the db
            data = await self.collection.find_one({'_id': ctx.interaction.id})
            await self.collection.update_one({'_id': ctx.interaction.id},
                                             {
                                                 '$set': {
                                                     'status': 'completed',
                                                     'took'  : (datetime.utcnow() - data['timestamp']).total_seconds()
                                                 }
                                             })
        except Exception as e:
            log.error(f"Failed to update command status to completed: {e}")
            await report_error(e)

    @commands.Cog.listener()
    async def on_command_error(self, ctx, excep):
        log.info(f"/{ctx.command.name} called by {ctx.author} in #{ctx.channel.name} ({ctx.guild}) failed")

        msg = f'{ctx.author.mention} An unexpected error occurred. This Error has been automatically reported.'
        try:
            # try to inform the user. this might fail if it took too long to respond
            await ctx.send(content=msg)
        except NotFound:
            # so fall back to a normal channel message if that happens
            try:
                await ctx.channel.send(msg)
            except Exception as e:
                log.error(f"Failed to inform user of command error: {e}")
                await report_error(e)

        try:
            # get the timestamp of when the command was called from the db
            data = await self.collection.find_one({'_id': ctx.interaction.id})
            await self.collection.update_one({'_id': ctx.interaction.id},
                                             {
                                                 '$set': {
                                                     'status': 'error',
                                                     'took'  : (datetime.utcnow() - data['timestamp']).total_seconds(),
                                                     'error' : str(excep)
                                                 }
                                             })
        except Exception as e:
            log.error(f"Failed to update command status to error: {e}")
            await report_error(e)

        await reporter.report_error(excep, ctx=ctx)

    @commands.Cog.listener()
    async def on_ready(self, ):
        log.info(f'Logged in as {self.bot.user.name} ({self.bot.user.id})')


async def setup(bot):
    await bot.add_cog(Metrics(bot))
