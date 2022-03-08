import json

from discord.ext import commands
from web3.datastructures import MutableAttributeDict as aDict

from utils.embeds import assemble
from utils.slash_permissions import owner_only_slash


class Mock(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

        with open("./plugins/mock/mock_data.json") as f:
            data = json.load(f)

        self.mock_mapping = data["mapping"]
        self.mock_data = data["data"]

    @owner_only_slash()
    async def mock(self, ctx, event_name):
        await ctx.defer()
        if event_name not in self.mock_mapping:
            return await ctx.respond("No Mock Mapping available for this Event")

        args = aDict({})
        args.event_name = event_name
        for arg in self.mock_mapping[event_name]:
            args[arg] = self.mock_data[arg]

        embed = assemble(args)
        # add note to footer about it being a mock
        embed.set_footer(text=embed._footer["text"] + " · This is a mocked Event!")

        # trick to remove the command call message
        tmp = await ctx.respond("done")
        await tmp.delete()

        await ctx.channel.send(embed=embed)


def setup(bot):
    bot.add_cog(Mock(bot))
