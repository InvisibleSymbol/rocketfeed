import logging
import re
import time
from io import BytesIO

import aiohttp
import matplotlib as mpl
import numpy as np
from PIL import Image
from discord import File
from discord.ext import commands
from discord.ext.commands import Context
from discord.ext.commands import hybrid_command
from matplotlib import pyplot as plt
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReplaceOne
from wordcloud import WordCloud

from utils.cfg import cfg
from utils.embeds import Embed
from utils.rocketpool import rp
from utils.time_debug import timerun
from utils.visibility import is_hidden

log = logging.getLogger("proposals")
log.setLevel(cfg["log_level"])

LOOKUP = {
    "consensus": {
        "N": "Nimbus",
        "P": "Prysm",
        "L": "Lighthouse",
        "T": "Teku"
    },
    "execution": {
        "I": "Infura",
        "P": "Pocket",
        "G": "Geth",
        "B": "Besu",
        "N": "Nethermind",
        "X": "External"
    }
}

COLORS = {
    "Nimbus"          : "#cc9133",
    "Prysm"           : "#40bfbf",
    "Lighthouse"      : "#9933cc",
    "Teku"            : "#3357cc",

    "Infura"          : "#ff2f00",
    "Pocket"          : "#e216e9",
    "Geth"            : "#808080",
    "Besu"            : "#55aa7a",
    "Nethermind"      : "#2688d9",
    "External"        : "#000000",

    "Smart Node"      : "#cc6e33",
    "Allnodes"        : "#4533cc",
    "No proposals yet": "#E0E0E0",
    "Unknown"         : "#AAAAAA",
}

PROPOSAL_TEMPLATE = {
    "type"            : "Unknown",
    "consensus_client": "Unknown",
    "execution_client": "Unknown",
}

# noinspection RegExpUnnecessaryNonCapturingGroup
SMARTNODE_REGEX = re.compile(r"^RP(?:(?:-)([A-Z])([A-Z])?)? (?:v)?(\d+\.\d+\.\d+(?:-\w+)?)(?:(?: \()(.+)(?:\)))?$")


def parse_propsal(entry):
    graffiti = bytes.fromhex(entry["validator"]["graffiti"][2:]).decode("utf-8").rstrip('\x00')
    data = {
        "slot"     : int(entry["number"]),
        "validator": int(entry["validator"]["index"]),
        "graffiti" : graffiti,
    }
    if m := SMARTNODE_REGEX.findall(graffiti):
        groups = m[0]
        # smart node proposal
        data["type"] = "Smart Node"
        data["version"] = groups[2]
        if groups[1]:
            data["consensus_client"] = LOOKUP["consensus"].get(groups[1], "Unknown")
            data["execution_client"] = LOOKUP["execution"].get(groups[0], "Unknown")
        elif groups[0]:
            data["consensus_client"] = LOOKUP["consensus"].get(groups[0], "Unknown")
        if groups[3]:
            data["comment"] = groups[3]
    elif "⚡️Allnodes" in graffiti:
        # Allnodes proposal
        data["type"] = "Allnodes"
        data["consensus_client"] = "Teku"
        data["execution_client"] = "Infura"
    else:
        # normal proposal
        # try to detect the client from the graffiti
        graffiti = graffiti.lower()
        for client in LOOKUP["consensus"].values():
            if client.lower() in graffiti:
                data["consensus_client"] = client
                break
        for client in LOOKUP["execution"].values():
            if client.lower() in graffiti:
                data["execution_client"] = client
                break
    return data


class Proposals(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.rocketscan_proposals_url = "https://rocketscan.io/api/mainnet/beacon/blocks/all"
        self.last_chore_run = 0
        self.validator_url = "https://beaconcha.in/api/v1/validator/"
        # connect to local mongodb
        self.db = AsyncIOMotorClient(cfg["mongodb_uri"]).get_database("rocketwatch")

    async def gather_all_proposals(self):
        log.info("getting all proposals using the rocketscan.dev API")
        async with aiohttp.ClientSession() as session:
            async with session.get(self.rocketscan_proposals_url) as resp:
                if resp.status != 200:
                    log.error("failed to get proposals using the rocketscan.dev API")
                    return
                proposals = await resp.json()
        log.info("got all proposals using the rocketscan.dev API")
        await self.db.proposals.bulk_write([ReplaceOne({"slot": int(entry["number"])},
                                                       PROPOSAL_TEMPLATE | parse_propsal(entry),
                                                       upsert=True) for entry in proposals])
        log.info("finished gathering all proposals")

    async def chore(self, ctx: Context):
        msg = await ctx.send(content="doing chores...")
        # only run if self.last_chore_run timestamp is older than 1 hour
        if (time.time() - self.last_chore_run) > 3600:
            self.last_chore_run = time.time()
            await msg.edit(content="gathering proposals...")
            await self.gather_all_proposals()
        else:
            log.debug("skipping chore")
        return msg

    @timerun
    async def gather_attribute(self, attribute):
        distribution = await self.db.minipools.aggregate([
            {
                '$match': {
                    'node_operator': {
                        '$ne': None
                    }
                }
            }, {
                '$lookup': {
                    'from'        : 'proposals',
                    'localField'  : 'validator',
                    'foreignField': 'validator',
                    'as'          : 'proposals',
                    'pipeline'    : [
                        {
                            '$sort': {
                                'slot': -1
                            }
                        }, {
                            '$match': {
                                attribute: {
                                    '$exists': 1
                                }
                            }
                        }
                    ]
                }
            }, {
                '$project': {
                    'node_operator': 1,
                    'validator'    : 1,
                    'proposal'     : {
                        '$arrayElemAt': [
                            '$proposals', 0
                        ]
                    }
                }
            }, {
                '$project': {
                    'node_operator': 1,
                    'validator'    : 1,
                    'slot'         : '$proposal.slot'
                }
            }, {
                '$group': {
                    '_id'            : '$node_operator',
                    'slot'           : {
                        '$max': '$slot'
                    },
                    'validator_count': {
                        '$sum': 1
                    }
                }
            }, {
                '$match': {
                    'slot': {
                        '$ne': None
                    }
                }
            }, {
                '$lookup': {
                    'from'        : 'proposals',
                    'localField'  : 'slot',
                    'foreignField': 'slot',
                    'as'          : 'proposal'
                }
            }, {
                '$project': {
                    'node_operator'  : 1,
                    'proposal'       : {
                        '$arrayElemAt': [
                            '$proposal', 0
                        ]
                    },
                    'validator_count': 1
                }
            }, {
                '$project': {
                    'attribute'      : f'$proposal.{attribute}',
                    'validator_count': 1
                }
            }, {
                '$group': {
                    '_id'            : '$attribute',
                    'count'          : {
                        '$sum': 1
                    },
                    'validator_count': {
                        '$sum': '$validator_count'
                    }
                }
            }, {
                '$sort': {
                    'count': 1
                }
            }
        ]).to_list(length=None)
        return distribution

    @hybrid_command()
    async def version_chart(self, ctx: Context):
        """
        Show a historical chart of used Smart Node versions
        """
        await ctx.defer(ephemeral=is_hidden(ctx))
        msg = await self.chore(ctx)
        await msg.edit(content="generating version chart...")

        e = Embed(title="Version Chart")

        # get proposals
        proposals = await self.db.proposals.find({"version": {"$exists": 1}}).sort("slot", 1).to_list(None)
        look_back = int(60 / 12 * 60 * 24 * 5)  # last 5 days
        max_slot = proposals[-1]["slot"]
        # get version used after max_slot - look_back
        # and have at least 10 occurrences
        start_slot = max_slot - look_back
        recent_versions = await self.db.proposals.aggregate([
            {
                '$match': {
                    'slot'   : {
                        '$gte': start_slot
                    },
                    'version': {
                        '$exists': 1
                    }
                }

            }, {
                '$group': {
                    '_id'  : '$version',
                    'count': {
                        '$sum': 5
                    }
                }
            }, {
                '$match': {
                    'count': {
                        '$gte': 10
                    }
                }
            }, {
                '$sort': {
                    '_id': 1
                }
            }
        ]).to_list(None)
        recent_versions = [v['_id'] for v in recent_versions]
        data = {}
        versions = []
        proposal_buffer = []
        tmp_data = {}
        for i, proposal in enumerate(proposals):
            proposal_buffer.append(proposal)
            if proposal["version"] not in versions:
                versions.append(proposal["version"])
            tmp_data[proposal["version"]] = tmp_data.get(proposal["version"], 0) + 1
            slot = proposal["slot"]
            if i < 200:
                continue
            while proposal_buffer[0]["slot"] < slot - (60 / 12 * 60 * 24 * 5):
                to_remove = proposal_buffer.pop(0)
                tmp_data[to_remove["version"]] -= 1
            data[slot] = tmp_data.copy()

        # normalize data
        for slot, value in data.items():
            total = sum(data[slot].values())
            for version in data[slot]:
                value[version] /= total

        # use plt.stackplot to stack the data
        x = list(data.keys())
        y = {v: [] for v in versions}
        for slot, value_ in data.items():
            for version in versions:
                y[version].append(value_.get(version, 0))

        # matplotlib default color
        matplotlib_colors = [color['color'] for color in list(mpl.rcParams['axes.prop_cycle'])]
        # cap recent versions to available colors
        recent_versions = recent_versions[:len(matplotlib_colors)]
        recent_colors = [matplotlib_colors[i] for i in range(len(recent_versions))]
        # generate color mapping
        colors = ["white"] * len(versions)
        for i, version in enumerate(versions):
            if version in recent_versions:
                colors[i] = recent_colors[recent_versions.index(version)]

        labels = [v if v in recent_versions else "_nolegend_" for v in versions]
        plt.stackplot(x, *y.values(), labels=labels, colors=colors)
        plt.title("Version Chart")
        plt.xlabel("slot")
        plt.ylabel("Percentage")
        plt.legend(loc="upper left")
        plt.tight_layout()

        # respond with image
        img = BytesIO()
        plt.savefig(img, format="png")
        img.seek(0)
        plt.close()
        e.set_image(url="attachment://chart.png")

        # send data
        await msg.edit(content="", embed=e, attachments=[File(img, filename="chart.png")])
        img.close()

    async def plot_axes_with_data(self, attr: str, ax1, ax2, name):
        # group by client and get count
        data = await self.gather_attribute(attr)

        minipools = [(x['_id'], x["validator_count"]) for x in data]
        minipools = sorted(minipools, key=lambda x: x[1])

        # get total minipool count from rocketpool
        unobserved_minipools = rp.call("rocketMinipoolManager.getStakingMinipoolCount") - sum(d[1] for d in minipools)
        minipools.insert(0, ("No proposals yet", unobserved_minipools))

        # get node operators
        node_operators = [(x['_id'], x["count"]) for x in data]
        node_operators = sorted(node_operators, key=lambda x: x[1])

        # get total node operator count from rp
        unobserved_node_operators = rp.call("rocketNodeManager.getNodeCount") - sum(d[1] for d in node_operators)

        # sort data
        node_operators.insert(0, ("No proposals yet", unobserved_node_operators))
        ax1.pie(
            [x[1] for x in minipools],
            colors=[COLORS.get(x[0], "red") for x in minipools],
            autopct=lambda pct: ('%.1f%%' % pct) if pct > 5 else '',
            startangle=90,
            textprops={'fontsize': '12'},
        )
        # legend
        total_minipols = sum(x[1] for x in minipools)
        # legend in the top left corner of the plot
        ax1.legend(
            [f"{x[1]} {x[0]} ({x[1] / total_minipols:.2%})" for x in minipools],
            fontsize=11,
            loc='lower left',
        )
        ax1.set_title(f"{name} Distribution based on Minipools", fontsize=16)

        ax2.pie(
            [x[1] for x in node_operators],
            colors=[COLORS.get(x[0], "#fb5b9d") for x in node_operators],
            autopct=lambda pct: ('%.1f%%' % pct) if pct > 5 else '',
            startangle=90,
            textprops={'fontsize': '12'},
        )
        # legend
        total_node_operators = sum(x[1] for x in node_operators)
        ax2.legend(
            [f"{x[1]} {x[0]} ({x[1] / total_node_operators:.2%})" for x in node_operators],
            loc="lower right",
            fontsize=11
        )
        ax2.set_title(f"{name} Distribution based on Node Operators", fontsize=16)

    async def proposal_vs_node_operators_embed(self, attribute, name, msg):
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 8))
        # iterate axes in pairs
        await msg.edit(content=f"generating {attribute} distribution graph...")
        await self.plot_axes_with_data(attribute, ax1, ax2, name)

        e = Embed(title=f"{name} Distribution")

        fig.subplots_adjust(left=0, right=1, top=0.9, bottom=0, wspace=0)

        # respond with image
        img = BytesIO()
        plt.savefig(img, format="png")
        img.seek(0)
        plt.close()
        e.set_image(url=f"attachment://{attribute}.png")

        # send data
        f = File(img, filename=f"{attribute}.png")
        img.close()
        return e, f

    @hybrid_command()
    async def client_distribution(self, ctx: Context):
        """
        Generate a distribution graph of clients.
        """
        await ctx.defer(ephemeral=is_hidden(ctx))
        msg = await self.chore(ctx)
        embeds, files = [], []
        for attr, name in [["consensus_client", "Consensus Client"], ["execution_client", "Execution Client"]]:
            e, f = await self.proposal_vs_node_operators_embed(attr, name, msg)
            embeds.append(e)
            files.append(f)
        await msg.edit(content="", embeds=embeds, attachments=files)

    @hybrid_command()
    async def user_distribution(self, ctx: Context):
        """
        Generate a distribution graph of users.
        """
        await ctx.defer(ephemeral=is_hidden(ctx))
        msg = await self.chore(ctx)
        e, f = await self.proposal_vs_node_operators_embed("type", "User", msg)
        await msg.edit(content="", embed=e, attachments=[f])

    @hybrid_command()
    async def comments(self, ctx: Context):
        """
        Generate a world cloud of comments.
        """
        await ctx.defer(ephemeral=is_hidden(ctx))
        await self.chore(ctx)
        await ctx.send(content="generating comments word cloud...")

        # load image
        mask = np.array(Image.open("./plugins/proposals/assets/logo-words.png"))

        # load font
        font_path = "./plugins/proposals/assets/noto.ttf"

        wc = WordCloud(max_words=2000,
                       mask=mask,
                       max_font_size=100,
                       background_color="white",
                       relative_scaling=0,
                       font_path=font_path,
                       color_func=lambda *args, **kwargs: "rgb(235, 142, 85)")

        # aggregate comments with their count
        comments = await self.db.proposals.aggregate([
            {"$match": {"comment": {"$exists": 1}}},
            {"$group": {"_id": "$comment", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}}
        ]).to_list(None)
        comment_words = {x['_id']: x["count"] for x in comments}

        # generate word cloud
        wc.fit_words(comment_words)

        # respond with image
        img = BytesIO()
        wc.to_image().save(img, format="png")
        img.seek(0)
        plt.close()
        e = Embed(title="Rocket Pool Proposal Comments")
        e.set_image(url="attachment://image.png")
        await ctx.send(content="", embed=e, attachments=[File(img, filename="image.png")])
        img.close()


async def setup(bot):
    await bot.add_cog(Proposals(bot))
